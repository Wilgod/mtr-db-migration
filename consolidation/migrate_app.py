r"""
MTR DB Migration — desktop runner for the consolidation, packaged as a single
exe by build_exe.ps1. Two steps:

  Step 1  Prepare target: create the database and ALL tables (known MTR tables
          plus every other source table), link the sources, preflight report,
          ID ranges, Sites, indexes/keys/triggers. No rows are copied.
  Step 2  Copy data: pick tables and copy their rows. Repeatable; tables a
          choice depends on are added automatically; ticking a loaded table
          reloads it. "Build Metadata Matrix" then makes MetadataMatrix: one row
          per file, one column per distinct MetadataKey (Metadata unchanged).

The exe carries everything it needs: generate.py, sql/ and a private copy of
psql.exe + DLLs. Only the source database names are needed: each source's
schema is read live. Next to the exe it keeps:
    sources.json          (rewritten from the "Source databases" list on
                           step 1; host/port per source can be edited there)
    migrate_settings.json (connection fields, never passwords)
Kerberos: tick "Kerberos" to log in as the signed-in Windows (domain) user
through SSPI instead of a password; the server needs a "gss"/"sspi" line in
pg_hba.conf, and Host must be the server's full DNS name (its SPN).
    migration_work\       (the SQL actually run)
    logs\                 (one log per psql run)
"""
import contextlib
import io
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, scrolledtext, ttk

import generate

# metadata_matrix.py lives with the Metadata Horizontal tool (bundled into the exe)
sys.path.insert(1, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "metadata_horizontal"))
import metadata_matrix  # noqa: E402

FROZEN = getattr(sys, "frozen", False)
BUNDLE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))

# Bundled resources (exe) or the repo layout (python migrate_app.py).
RES_CONS = os.path.join(BUNDLE, "consolidation") if FROZEN else APP_DIR
RES_DUMPS = os.path.join(BUNDLE, "pgdump_schema") if FROZEN else os.path.join(APP_DIR, "..", "pgdump_schema")
RES_PGBIN = os.path.join(BUNDLE, "pgbin")

SOURCES_FILE = os.path.join(APP_DIR, "sources.json")
SETTINGS_FILE = os.path.join(APP_DIR, "migrate_settings.json")
WORK_DIR = os.path.join(APP_DIR, "migration_work")
WORK_SQL = os.path.join(WORK_DIR, "sql")
LOG_DIR = os.path.join(APP_DIR, "logs")

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
TITLE = "MTR DB Migration"

# User-made objects in an existing target (empty result = safe to build into it). Extension
# members are skipped; postgres_fdw itself is fine (01_attach uses CREATE EXTENSION IF NOT EXISTS).
EXISTING_TARGET_LEFTOVERS_SQL = r"""
WITH ext AS (SELECT classid, objid FROM pg_depend WHERE deptype = 'e'),
sys AS (SELECT oid FROM pg_namespace WHERE nspname IN ('pg_catalog', 'information_schema', 'pg_toast')
                                         OR nspname LIKE 'pg\_temp\_%' OR nspname LIKE 'pg\_toast\_temp\_%'),
o AS (
  SELECT 'schema ' || quote_ident(n.nspname) AS what FROM pg_namespace n
   WHERE n.oid NOT IN (SELECT oid FROM sys) AND n.nspname <> 'public'
     AND (n.oid NOT IN (SELECT objid FROM ext WHERE classid = 'pg_namespace'::regclass))
  UNION ALL
  SELECT CASE c.relkind WHEN 'v' THEN 'view ' WHEN 'm' THEN 'materialized view ' WHEN 'S' THEN 'sequence '
                        WHEN 'f' THEN 'foreign table ' ELSE 'table ' END || c.oid::regclass::text
    FROM pg_class c WHERE c.relnamespace NOT IN (SELECT oid FROM sys) AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
     AND c.oid NOT IN (SELECT objid FROM ext WHERE classid = 'pg_class'::regclass)
  UNION ALL
  SELECT 'function ' || p.oid::regprocedure::text FROM pg_proc p
   WHERE p.pronamespace NOT IN (SELECT oid FROM sys)
     AND p.oid NOT IN (SELECT objid FROM ext WHERE classid = 'pg_proc'::regclass)
  UNION ALL
  SELECT 'type ' || t.oid::regtype::text FROM pg_type t
   WHERE t.typnamespace NOT IN (SELECT oid FROM sys) AND t.typtype IN ('c', 'e', 'd', 'r')
     AND (t.typrelid = 0 OR (SELECT relkind FROM pg_class WHERE oid = t.typrelid) = 'c')
     AND t.oid NOT IN (SELECT objid FROM ext WHERE classid = 'pg_type'::regclass)
  UNION ALL
  SELECT 'foreign server ' || quote_ident(srvname) FROM pg_foreign_server
  UNION ALL
  SELECT 'extension ' || quote_ident(extname) FROM pg_extension WHERE extname NOT IN ('plpgsql', 'postgres_fdw'))
SELECT coalesce(string_agg(what, E'\n  ' ORDER BY what), '') FROM (SELECT what FROM o ORDER BY what LIMIT 30) s
"""

# "Clear existing target": drop every user-made object (the ones the check above lists, extensions
# aside) but keep the database itself with its owner, grants and extensions. A DO block is atomic.
CLEAR_TARGET_SQL = r"""
DO $clear$
DECLARE
  st text;
  sys_ns oid[] := ARRAY(SELECT oid FROM pg_namespace WHERE nspname IN ('pg_catalog', 'information_schema', 'pg_toast')
                          OR nspname LIKE 'pg\_temp\_%' OR nspname LIKE 'pg\_toast\_temp\_%');
  ext_objs oid[] := ARRAY(SELECT objid FROM pg_depend WHERE deptype = 'e');
BEGIN
  PERFORM set_config('client_min_messages', 'warning', true);  -- no NOTICE per cascaded object
  FOR st IN SELECT format('DROP SERVER IF EXISTS %I CASCADE', srvname) FROM pg_foreign_server LOOP
    EXECUTE st;
  END LOOP;
  FOR st IN SELECT format('DROP SCHEMA IF EXISTS %I CASCADE', nspname) FROM pg_namespace
             WHERE oid <> ALL (sys_ns) AND nspname <> 'public' AND oid <> ALL (ext_objs) LOOP
    EXECUTE st;
  END LOOP;
  FOR st IN SELECT format('DROP %s IF EXISTS %I.%I CASCADE',
                          CASE c.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                                         WHEN 'f' THEN 'FOREIGN TABLE' WHEN 'S' THEN 'SEQUENCE' ELSE 'TABLE' END,
                          n.nspname, c.relname)
              FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE c.relnamespace <> ALL (sys_ns) AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
               AND c.oid <> ALL (ext_objs) LOOP
    EXECUTE st;
  END LOOP;
  FOR st IN SELECT format('DROP ROUTINE IF EXISTS %s CASCADE', p.oid::regprocedure) FROM pg_proc p
             WHERE p.pronamespace <> ALL (sys_ns) AND p.oid <> ALL (ext_objs) LOOP
    EXECUTE st;
  END LOOP;
  FOR st IN SELECT format('DROP %s IF EXISTS %I.%I CASCADE', CASE t.typtype WHEN 'd' THEN 'DOMAIN' ELSE 'TYPE' END,
                          n.nspname, t.typname)
              FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
             WHERE t.typnamespace <> ALL (sys_ns) AND t.typtype IN ('c', 'e', 'd', 'r')
               AND (t.typrelid = 0 OR (SELECT relkind FROM pg_class WHERE oid = t.typrelid) = 'c')
               AND t.oid <> ALL (ext_objs) LOOP
    EXECUTE st;
  END LOOP;
END $clear$;
"""
PLAN_QUERY = """SELECT p.table_name, p.kind, array_to_string(p.depends_on, ', '), p.sources,
       COALESCE(p.source_rows::text, ''), COALESCE(l.row_count::text, ''),
       COALESCE(to_char(l.loaded_at, 'YYYY-MM-DD HH24:MI'), '')
FROM _merge.table_plan p LEFT JOIN _merge.loaded_table l USING (table_name)
ORDER BY p.load_order"""
# Columns of public.<t> (source '') and of each source's linked foreign table src_<name>.<t>.
COLUMNS_QUERY = """SELECT src, attname, typ FROM (
    SELECT '' AS src, 0 AS ord, a.attnum, a.attname, format_type(a.atttypid, a.atttypmod) AS typ
    FROM pg_attribute a WHERE a.attrelid = to_regclass(format('public.%I', '{t}'))
      AND a.attnum > 0 AND NOT a.attisdropped
    UNION ALL
    SELECT s.source_db, s.load_order, a.attnum, a.attname, format_type(a.atttypid, a.atttypmod)
    FROM _merge.source s JOIN pg_attribute a ON a.attrelid = to_regclass(format('%I.%I', s.foreign_schema, '{t}'))
    WHERE a.attnum > 0 AND NOT a.attisdropped
) c ORDER BY ord, attnum"""


def find_psql():
    bundled = os.path.join(RES_PGBIN, "psql.exe")
    if os.path.exists(bundled):
        return bundled
    return shutil.which("psql") or r"C:\Program Files\PostgreSQL\18\bin\psql.exe"


class StepFailed(Exception):
    pass


class Runner:
    """Runs psql work on a worker thread; talks to the GUI via a queue."""

    def __init__(self, cfg, out):
        self.cfg, self.out = cfg, out
        self.stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.psql = find_psql()
        # psql must never wait: a password prompt has no visible console here (CREATE_NO_WINDOW)
        # and would hang forever, so psql gets -w and connection attempts time out.
        self.env = dict(os.environ, PGCLIENTENCODING="UTF8", PGCONNECT_TIMEOUT="10")
        if cfg["kerberos"]:
            self.env.pop("PGPASSWORD", None)  # never fall back to a stray password
            self.env["PGKRBSRVNAME"] = cfg["krbsrvname"] or "postgres"
        else:
            self.env["PGPASSWORD"] = cfg["password"]
        # "Set role": every target session starts as SET ROLE <role> (sources keep their own login).
        self.env.pop("PGOPTIONS", None)
        if cfg["role"]:
            self.env["PGOPTIONS"] = f"-c role={cfg['role']}"
        if os.path.isdir(RES_PGBIN):
            self.env["PATH"] = RES_PGBIN + os.pathsep + self.env.get("PATH", "")

    def log(self, text):
        self.out.put(("text", text))

    def section(self, name):
        self.out.put(("step", name))
        self.log(f"\n==== {name} ====")

    # ---------- psql ----------
    def _cmd(self, database, extra):
        c = self.cfg
        args = ["-X", "-w", "-v", "ON_ERROR_STOP=1", "-U", c["user"], "-d", database] + extra
        if c["docker"]:
            pw = [] if c["kerberos"] else ["-e", "PGPASSWORD=" + c["password"]]
            if c["role"]:
                pw += ["-e", "PGOPTIONS=" + self.env["PGOPTIONS"]]
            return ["docker", "exec", "-i", *pw, c["docker"], "psql"] + args
        return [self.psql, "-h", c["host"], "-p", str(c["port"])] + args

    def psql_run(self, database, log_name, file=None, command=None, variables=(), extra_args=()):
        extra = list(extra_args)
        for v in variables:
            extra += ["-v", v]
        if command:
            extra += ["-c", command]
        stdin_data = None
        if file:
            if self.cfg["docker"]:
                extra += ["-f", "-"]
                with open(file, "rb") as f:
                    stdin_data = f.read()
            else:
                extra += ["-f", file]
        os.makedirs(LOG_DIR, exist_ok=True)
        log_path = os.path.join(LOG_DIR, f"{self.stamp}_{log_name}.log")
        proc = subprocess.Popen(self._cmd(database, extra), env=self.env, creationflags=NO_WINDOW,
                                stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if stdin_data is not None:
            threading.Thread(target=self._feed, args=(proc, stdin_data), daemon=True).start()
        with open(log_path, "w", encoding="utf-8") as lf:
            for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                lf.write(line + "\n")
                self.log(line)
        if proc.wait() != 0:
            raise StepFailed(f"psql failed ({log_name}) - see {log_path}")

    @staticmethod
    def _feed(proc, data):
        try:
            proc.stdin.write(data)
        finally:
            proc.stdin.close()

    def psql_scalar(self, database, sql, extra=()):
        r = subprocess.run(self._cmd(database, ["-tA", *extra, "-c", sql]), env=self.env, creationflags=NO_WINDOW,
                           stdin=subprocess.DEVNULL, capture_output=True)
        if r.returncode != 0:
            raise StepFailed(r.stderr.decode("utf-8", errors="replace").strip() or f"psql failed: {sql}")
        return r.stdout.decode("utf-8", errors="replace").strip("\r\n")  # keep tabs of empty trailing fields

    def source_cmd(self, s):
        """psql argv + env reaching source s with the source login. A "localhost"
        source host is relative to the target server, so connect via the target host."""
        c = self.cfg
        env = dict(self.env) if c["kerberos"] else dict(self.env, PGPASSWORD=c["src_password"])
        env.pop("PGOPTIONS", None)  # the target's "Set role" is not for the source login
        if c["docker"]:
            pw = [] if c["kerberos"] else ["-e", "PGPASSWORD=" + c["src_password"]]
            return ["docker", "exec", "-i", *pw, c["docker"], "psql",
                    "-h", s["host"], "-p", str(s["port"]), "-U", c["src_user"], "-d", s["dbname"]], env
        host, port = (c["host"], c["port"]) if s["host"] in generate.LOCAL_HOSTS else (s["host"], s["port"])
        return [self.psql, "-h", host, "-p", str(port), "-U", c["src_user"], "-d", s["dbname"]], env

    # ---------- work ----------
    def generate(self):
        """Refresh migration_work\\ from the bundle and regenerate from sources.json."""
        self.section("generate")
        os.makedirs(WORK_SQL, exist_ok=True)
        for name in os.listdir(os.path.join(RES_CONS, "sql")):
            shutil.copy2(os.path.join(RES_CONS, "sql", name), os.path.join(WORK_SQL, name))
        shutil.copy2(SOURCES_FILE, os.path.join(WORK_DIR, "sources.json"))
        generate.HERE, generate.SQL_DIR, generate.DUMP_DIR = WORK_DIR, WORK_SQL, RES_DUMPS
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                return generate.main(generate.psql_schema_loader(self.source_cmd))
        except SystemExit as e:
            raise StepFailed(f"generate failed: {e}")
        finally:
            self.log(buf.getvalue().rstrip())

    def sql(self, name):
        return os.path.join(WORK_SQL, name)

    def step1(self):
        """Create the target with every table and link the sources; no rows copied."""
        target = self.cfg["target"]
        self.generate()
        self.section("create")
        exists = self.psql_scalar("postgres", f"SELECT 1 FROM pg_database WHERE datname = '{target}'")
        if exists == "1" and not self.cfg["drop_existing"]:
            if self.cfg["clear_existing"]:
                self.log(f"Clearing {target}: dropping every table, view, function, schema and foreign server "
                         "(the database itself is kept).")
                self.psql_run(target, "clear", command=CLEAR_TARGET_SQL)
            # An existing database is used as-is only when nothing user-made is left in it
            # (e.g. one whose tables were removed by hand), so 00_target_schema.sql starts clean.
            left = self.psql_scalar(target, EXISTING_TARGET_LEFTOVERS_SQL)
            if left:
                raise StepFailed(f"Database {target} already exists and is not empty:\n  {left}\n"
                                 "Tick 'Clear existing target' to remove them, or pick a new Target DB name.")
            if self.psql_scalar(target, "SELECT pg_encoding_to_char(encoding) FROM pg_database "
                                        "WHERE datname = current_database()") != "UTF8":
                raise StepFailed(f"Database {target} is not UTF8; the target must be UTF8.")
            self.log(f"Database {target} already exists and is empty: using it (not created, not dropped).")
        else:
            if exists == "1":
                # Only drop a database this tool built (00_target_schema.sql creates _merge), never a source.
                if self.psql_scalar(target, "SELECT 1 FROM pg_namespace WHERE nspname = '_merge'") != "1":
                    raise StepFailed(f"Database {target} exists and was not built by this tool (no _merge schema), "
                                     "so it will not be dropped. Use 'Clear existing target' instead (keeps the "
                                     "database, drops its tables), or pick a new Target DB name.")
                self.psql_run("postgres", "drop", command=f'DROP DATABASE "{target}" WITH (FORCE)')
            self.psql_run("postgres", "create", command=f"CREATE DATABASE \"{target}\" ENCODING 'UTF8' TEMPLATE template0")
        self.section("schema")
        # one transaction: a failure (e.g. no CREATE right for _merge) leaves the target empty, not half-built
        self.psql_run(target, "schema", file=self.sql("00_target_schema.sql"), extra_args=["--single-transaction"])
        self.section("attach")
        self.psql_run(target, "attach", file=self.sql("01_attach_sources.sql"),
                      variables=[f"src_user={self.cfg['src_user']}", f"src_password={self.cfg['src_password']}",
                                 f"src_kerberos={'true' if self.cfg['src_kerberos'] else 'false'}"])
        self.section("preflight")
        self.psql_run(target, "preflight", file=self.sql("02_preflight.sql"))
        self.section("prepare")
        self.psql_run(target, "prepare", file=self.sql("03_prepare.sql"))
        self.section("finalize")
        self.psql_run(target, "finalize", file=self.sql("04_finalize.sql"))
        self.log("\nStep 1 done: every table exists (no rows yet). Pick tables in step 2 to copy their data.")

    def step2(self, tables, reload):
        """Copy the rows of `tables`; `reload` are emptied first."""
        target = self.cfg["target"]
        g = self.generate()
        self.section("check")
        try:
            db_sources = self.psql_scalar(target, "SELECT string_agg(source_db, ',' ORDER BY load_order) FROM _merge.source")
            db_tables = self.psql_scalar(target, "SELECT string_agg(table_name, ',' ORDER BY load_order) FROM _merge.table_plan")
            links = self.psql_scalar(target, "SELECT count(*) FROM pg_foreign_server WHERE srvname LIKE 'srv\\_%'")
        except StepFailed as e:
            raise StepFailed(f"{target} is not prepared - run step 1 first.\n{e}")
        if db_sources != ",".join(s["name"] for s in g.sources):
            raise StepFailed(f"The source list changed since step 1 (then: {db_sources}). Run step 1 again.")
        if db_tables != ",".join(g.plan_tables()):
            raise StepFailed("The tables in the sources changed since step 1. Run step 1 again.")
        if links == "0":
            raise StepFailed("The source links were removed (detach). Run step 1 again to copy more data.")
        self.log("sources and tables match step 1")
        load_file = self.sql(f"load_{self.stamp}.sql")
        verify_file = self.sql(f"verify_{self.stamp}.sql")
        with open(load_file, "w", encoding="utf-8", newline="\n") as f:
            f.write(g.load_script(set(tables), set(reload)))
        with open(verify_file, "w", encoding="utf-8", newline="\n") as f:
            f.write(g.verify(set(tables)))
        self.section("copy data")
        self.psql_run(target, "load", file=load_file)
        self.section("sequences")
        self.psql_run(target, "sequences", file=self.sql("04b_sequences.sql"))
        self.section("verify")
        self.psql_run(target, "verify", file=verify_file)
        self.log(f"\nStep 2 done: {len(tables)} table(s) copied and verified.")

    def matrix(self):
        """Rebuild MetadataMatrix from Metadata (see metadata_matrix.py). Metadata is only read."""
        target, table = self.cfg["target"], metadata_matrix.DEFAULT_TABLE
        self.section("metadata matrix")
        t0 = datetime.now()
        try:
            migrated = json.loads(self.psql_scalar(target, "SELECT COALESCE(json_agg(table_name), '[]') "
                                                           "FROM _merge.table_plan"))
            metadata_matrix.check_table(table, protected=["MetadataHorizontal", *migrated])
            info = json.loads(self.psql_scalar(target, metadata_matrix.info_sql(table)))
            cols = metadata_matrix.check_info(table, info)
        except ValueError as e:
            raise StepFailed(str(e))
        for line in metadata_matrix.describe(info, cols):
            self.log(line)
        os.makedirs(WORK_SQL, exist_ok=True)
        path = self.sql(f"matrix_{self.stamp}.sql")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("BEGIN;\n" + "".join(st + ";\n" for st in metadata_matrix.build_statements(table, info))
                    + "COMMIT;\n")
        self.psql_run(target, "matrix", file=path, extra_args=["-q"])
        summary = json.loads(self.psql_scalar(target, metadata_matrix.summary_sql(table)))
        self.log(metadata_matrix.result_line(table, len(cols), summary, (datetime.now() - t0).total_seconds()))
        self.log("\nMetadata itself is unchanged.")

    def detach(self):
        self.generate()
        self.section("detach")
        self.psql_run(self.cfg["target"], "detach", file=self.sql("06_detach_sources.sql"))
        self.log("\nSource links removed. The _merge audit schema is kept.")

    def plan(self):
        """[(table, kind, needs, sources, source_rows, loaded_rows, loaded_at)] or None if not prepared."""
        target = self.cfg["target"]
        ok = self.psql_scalar("postgres", f"SELECT 1 FROM pg_database WHERE datname = '{target}'")
        if ok != "1":
            return None
        if self.psql_scalar(target, "SELECT to_regclass('_merge.table_plan') IS NOT NULL") != "t":
            return None
        out = self.psql_scalar(target, PLAN_QUERY, extra=("-F", "\t"))
        return [tuple(line.rstrip("\r").split("\t")) for line in out.splitlines() if line.strip()]

    def columns(self, table):
        """(sources in priority order, [(col, type)] of the target table,
        {source: {col: type}} for sources whose linked (foreign) table exists, linked?)."""
        target = self.cfg["target"]
        sources = self.psql_scalar(target, "SELECT source_db FROM _merge.source ORDER BY load_order").splitlines()
        linked = self.psql_scalar(target, "SELECT count(*) FROM pg_foreign_server WHERE srvname LIKE 'srv\\_%'") != "0"
        t = table.replace("'", "''")
        out = self.psql_scalar(target, COLUMNS_QUERY.format(t=t), extra=("-F", "\t"))
        cols, src_cols = [], {}
        for line in out.splitlines():
            if not line.strip():
                continue
            src, col, typ = line.rstrip("\r").split("\t")
            if src == "":
                cols.append((col, typ))
            else:
                src_cols.setdefault(src, {})[col] = typ
        return sources, cols, src_cols, linked

    def run(self, job, *args):
        try:
            job(*args)
            self.out.put(("done", "ok"))
        except StepFailed as e:
            self.log(f"\nERROR: {e}")
            self.out.put(("done", "failed"))
        except Exception as e:  # noqa: BLE001 - show anything unexpected in the log, not a silent crash
            self.log(f"\nERROR: {type(e).__name__}: {e}")
            self.out.put(("done", "failed"))


def closure(checked, plan):
    """Tables to load for `checked`: add parents not loaded yet; reloading a table
    also reloads its loaded children (TRUNCATE ... CASCADE empties them)."""
    deps = {t: [p for p in p_needs.split(", ") if p] for t, _, p_needs, *_ in plan}
    loaded = {t for t, *_, rows, at in plan if at}
    children = {t: [c for c, ps in deps.items() if t in ps] for t in deps}
    need = set(checked)
    while True:
        add = {p for t in need for p in deps.get(t, []) if p not in need and p not in loaded}
        add |= {c for t in need if t in loaded for c in children[t] if c in loaded and c not in need}
        if not add:
            break
        need |= add
    return need, need & loaded


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(TITLE)
        self.geometry("1220x900")
        self.minsize(1040, 700)
        self.queue = queue.Queue()
        self.running = False
        self.after_done = None
        self.plan_rows = []
        self.checked = set()
        self._ensure_sources()
        s = self._load_settings()

        self.vars = {
            "host": tk.StringVar(value=s.get("host", "localhost")),
            "port": tk.StringVar(value=str(s.get("port", 5432))),
            "user": tk.StringVar(value=s.get("user", "postgres")),
            "password": tk.StringVar(value=os.environ.get("PGPASSWORD", "")),
            "target": tk.StringVar(value=s.get("target") or self._target_from_sources()),
            "src_user": tk.StringVar(value=s.get("src_user", "postgres")),
            "src_password": tk.StringVar(value=""),
            "docker": tk.StringVar(value=s.get("docker", "")),
            "krbsrvname": tk.StringVar(value=s.get("krbsrvname", "postgres")),
            "role": tk.StringVar(value=s.get("role", "")),
        }
        self.kerberos = tk.BooleanVar(value=s.get("kerberos", False))
        self.src_kerberos = tk.BooleanVar(value=s.get("src_kerberos", False))
        self.same_pw = tk.BooleanVar(value=True)
        self.drop_existing = tk.BooleanVar(value=False)
        self.clear_existing = tk.BooleanVar(value=False)
        self._build()
        self.after(100, self._poll)

    # ---------- files ----------
    def _ensure_sources(self):
        if not os.path.exists(SOURCES_FILE):
            shutil.copy2(os.path.join(RES_CONS, "sources.json"), SOURCES_FILE)

    def _read_sources(self):
        try:
            with open(SOURCES_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _target_from_sources(self):
        return self._read_sources().get("target_dbname", "MtrMigration_Consolidated")

    def _source_names_from_file(self):
        names = []
        for s in self._read_sources().get("sources", []):
            if isinstance(s, str):
                names.append(s)
            elif s.get("enabled", True) and s.get("dbname"):
                names.append(s["dbname"])
        return names

    def _source_entries(self, dbnames):
        """sources.json entries for the list; keeps name/host/port already set for a database.
        No "dump" key, so every schema is read live. Returns (entries, renamed) where renamed
        is [(dbname, name, other dbname that wanted the same name or None)]."""
        old = {}
        for s in self._read_sources().get("sources", []):
            if isinstance(s, dict) and s.get("dbname"):
                old[s["dbname"]] = s
        entries, owner, renamed = [], {}, []
        for db in dbnames:
            o = old.get(db, {})
            # internal name (schema src_<name>): [a-z0-9_], so "CWP-ECR" and "cwp_ecr" (or two
            # non-Latin names) can map to the same one; make it unique with _2, _3...
            base = generate.source_name(o.get("name") or db)[:40]
            name, n = base, 1
            while name in owner:
                n += 1
                name = f"{base}_{n}"
            owner[name] = db
            if name != generate.source_name(db):
                renamed.append((db, name, owner.get(base) if name != base else None))
            entries.append({"name": name, "dbname": db,
                            "host": o.get("host", "localhost"), "port": o.get("port", 5432), "enabled": True})
        return entries, renamed

    def _write_sources(self, entries, target):
        cfg = {"_comment": ["Written by MtrDbMigration from its 'Source databases' list (order = priority:",
                            "the first source wins when two hold the same record). host/port are as seen",
                            "from the target server; 'localhost' = the same server as the target."],
               "target_dbname": target, "sources": entries}
        with open(SOURCES_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)

    def _load_settings(self):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_settings(self, cfg):
        keep = {k: cfg[k] for k in ("host", "port", "user", "target", "src_user", "docker",
                                    "kerberos", "krbsrvname", "src_kerberos", "role")}
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(keep, f, indent=2)
        except OSError:
            pass

    # ---------- layout ----------
    def _build(self):
        pad = {"padx": 6, "pady": 3}
        top = ttk.Frame(self, padding=(8, 8, 8, 0))
        top.pack(fill="x")

        conn = ttk.LabelFrame(top, text="Target server", padding=8)
        conn.grid(row=0, column=0, sticky="nsew", **pad)
        rows = [("Host", "host"), ("Port", "port"), ("User", "user"), ("Password", "password"),
                ("Set role (optional)", "role"), ("Target DB", "target"), ("Docker container (optional)", "docker"),
                ("Kerberos service name", "krbsrvname")]
        self.entries = {}
        for i, (label, key) in enumerate(rows):
            ttk.Label(conn, text=label).grid(row=i, column=0, sticky="w", **pad)
            self.entries[key] = ttk.Entry(conn, textvariable=self.vars[key], width=30,
                                          show="*" if key == "password" else "")
            self.entries[key].grid(row=i, column=1, sticky="ew", **pad)
        ttk.Checkbutton(conn, text="Kerberos (Windows login, no password)", variable=self.kerberos,
                        command=self._toggle_src_pw).grid(row=len(rows), column=0, columnspan=2, sticky="w", **pad)
        ttk.Button(conn, text="Test connection", command=self._test).grid(
            row=len(rows) + 1, column=0, columnspan=2, sticky="ew", **pad)

        src = ttk.LabelFrame(top, text="Source login (used by postgres_fdw)", padding=8)
        src.grid(row=0, column=1, sticky="nsew", **pad)
        ttk.Label(src, text="User").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(src, textvariable=self.vars["src_user"], width=24).grid(row=0, column=1, **pad)
        ttk.Label(src, text="Password").grid(row=1, column=0, sticky="w", **pad)
        self.src_pw_entry = ttk.Entry(src, textvariable=self.vars["src_password"], width=24, show="*")
        self.src_pw_entry.grid(row=1, column=1, **pad)
        ttk.Checkbutton(src, text="Same password as target", variable=self.same_pw,
                        command=self._toggle_src_pw).grid(row=2, column=0, columnspan=2, sticky="w", **pad)
        # No password in the user mapping: the target server's own login reaches the source
        # and the source's pg_hba decides (sspi/gss for its service account, trust...). Superuser only.
        ttk.Checkbutton(src, text="No password (Kerberos / trust, superuser)", variable=self.src_kerberos,
                        command=self._toggle_src_pw).grid(row=3, column=0, columnspan=2, sticky="w", **pad)
        ttk.Button(src, text="Test source logins", command=self._test_sources).grid(
            row=4, column=0, columnspan=2, sticky="ew", **pad)
        ttk.Button(src, text="Edit sources.json", command=lambda: os.startfile(SOURCES_FILE)).grid(
            row=5, column=0, columnspan=2, sticky="ew", **pad)
        ttk.Button(src, text="Open logs folder", command=self._open_logs).grid(
            row=6, column=0, columnspan=2, sticky="ew", **pad)
        self._toggle_src_pw()

        dbs = ttk.LabelFrame(top, text="Source databases (one per line, top = highest priority)", padding=8)
        dbs.grid(row=0, column=2, sticky="nsew", **pad)
        self.db_text = tk.Text(dbs, width=30, height=9, font=("Consolas", 9))
        self.db_text.grid(row=0, column=0, sticky="nsew")
        self.db_text.insert("1.0", "\n".join(self._source_names_from_file()))
        ttk.Button(dbs, text="Load all from server", command=self._load_dbs).grid(row=1, column=0, sticky="ew", pady=(6, 0))
        dbs.rowconfigure(0, weight=1)
        dbs.columnconfigure(0, weight=1)
        top.columnconfigure(2, weight=1)

        # Step 1
        s1 = ttk.LabelFrame(self, text="Step 1 - Prepare target: create ALL tables (no data copied yet)", padding=8)
        s1.pack(fill="x", padx=14, pady=(8, 0))
        self.step1_btn = ttk.Button(s1, text="Run step 1", command=self._step1)
        self.step1_btn.pack(side="left")
        # the two are alternatives: ticking one unticks the other
        ttk.Checkbutton(s1, text="Drop existing target (delete the database)", variable=self.drop_existing,
                        command=lambda: self.drop_existing.get() and self.clear_existing.set(False)
                        ).pack(side="left", padx=12)
        ttk.Checkbutton(s1, text="Clear existing target (drop all tables, keep database)", variable=self.clear_existing,
                        command=lambda: self.clear_existing.get() and self.drop_existing.set(False)
                        ).pack(side="left", padx=(0, 12))
        ttk.Label(s1, text="Creates the database, every table from every source, the source links, "
                           "preflight report, IDs, indexes and keys.", foreground="#555").pack(side="left")

        # Step 2
        s2 = ttk.LabelFrame(self, text="Step 2 - Copy data: tick tables, then Copy (repeat as often as you like)", padding=8)
        s2.pack(fill="both", expand=True, padx=14, pady=(8, 0))
        bar = ttk.Frame(s2)
        bar.pack(fill="x")
        self.copy_btn = ttk.Button(bar, text="Copy data of ticked tables", command=self._step2)
        self.copy_btn.pack(side="left")
        ttk.Button(bar, text="Refresh list", command=self._refresh).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Tick all", command=lambda: self._tick_all(True)).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Tick not loaded", command=self._tick_not_loaded).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Untick all", command=lambda: self._tick_all(False)).pack(side="left", padx=(8, 0))
        self.detach_btn = ttk.Button(bar, text="Finish: remove source links", command=self._detach)
        self.matrix_btn = ttk.Button(bar, text="Build Metadata Matrix", command=self._matrix)
        self.matrix_btn.pack(side="right", padx=(0, 8))
        self.detach_btn.pack(side="right")
        self.plan_info = ttk.Label(s2, text="Run step 1, or click Refresh list if it is already done.", foreground="#555")
        self.plan_info.pack(fill="x", pady=(6, 2))

        cols = ("tick", "table", "kind", "needs", "sources", "source_rows", "loaded_rows", "loaded_at")
        heads = ("Copy", "Table", "Type", "Needs", "Sources", "Rows in sources", "Rows loaded", "Loaded at")
        widths = (44, 250, 100, 150, 260, 110, 100, 120)
        frame = ttk.Frame(s2)
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=cols, show="headings", height=11, selectmode="none")
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            anchor = "e" if c in ("source_rows", "loaded_rows") else ("center" if c == "tick" else "w")
            self.tree.column(c, width=w, anchor=anchor, stretch=c in ("table", "sources"))
        self.tree.tag_configure("loaded", foreground="#1a7f37")
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Double-Button-1>", self._on_tree_double)
        self.tree.bind("<Button-3>", self._on_tree_columns)

        status = ttk.Frame(self, padding=(14, 4, 14, 0))
        status.pack(fill="x")
        ttk.Button(status, text="Clear log", command=lambda: self.log.delete("1.0", "end")).pack(side="left")
        self.status = ttk.Label(status, text=f"psql: {find_psql()}")
        self.status.pack(side="right")

        self.log = scrolledtext.ScrolledText(self, font=("Consolas", 9), wrap="none", height=12)
        self.log.pack(fill="both", expand=True, padx=14, pady=8)

    def _toggle_src_pw(self):
        krb = self.kerberos.get()
        self.entries["password"].configure(state="disabled" if krb else "normal")
        self.entries["krbsrvname"].configure(state="normal" if krb else "disabled")
        self.src_pw_entry.configure(state="disabled" if self.same_pw.get() or self.src_kerberos.get() else "normal")

    def _open_logs(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        os.startfile(LOG_DIR)

    # ---------- table list ----------
    def _show_plan(self, rows):
        self.tree.delete(*self.tree.get_children())
        if rows is None:
            self.plan_rows = []
            self.checked.clear()
            self.plan_info.configure(text=f"{self.vars['target'].get()} is not prepared yet - run step 1 first.")
            return
        self.plan_rows = rows
        names = {r[0] for r in rows}
        self.checked &= names
        for t, kind, needs, sources, src_rows, loaded_rows, loaded_at in rows:
            self.tree.insert("", "end", iid=t, tags=("loaded",) if loaded_at else (),
                             values=("☑" if t in self.checked else "☐", t, kind, needs, sources,
                                     f"{int(src_rows):,}" if src_rows else "", f"{int(loaded_rows):,}" if loaded_rows else "",
                                     loaded_at or "not loaded"))
        n_loaded = sum(1 for r in rows if r[6])
        self.plan_info.configure(text=f"{len(rows)} tables, {n_loaded} loaded. Click a row to tick it; double-click "
                                      "or right-click it to see its columns per source database. "
                                      "Tables a ticked table needs are added automatically.")

    def _on_tree_click(self, event):
        t = self.tree.identify_row(event.y)
        if not t:
            return
        if t in self.checked:
            self.checked.discard(t)
        else:
            self.checked.add(t)
        self.tree.set(t, "tick", "☑" if t in self.checked else "☐")

    def _on_tree_double(self, event):
        self._on_tree_click(event)  # undo the tick the first click of the double-click made
        self._on_tree_columns(event)

    def _on_tree_columns(self, event):
        t = self.tree.identify_row(event.y)
        if not t:
            return
        try:
            cfg = self._config()
        except ValueError as e:
            messagebox.showerror(TITLE, str(e))
            return
        self.status.configure(text=f"Reading columns of {t}...")

        def work():
            try:
                self.queue.put(("columns", (t, Runner(cfg, self.queue).columns(t))))
            except (StepFailed, OSError) as e:
                self.queue.put(("error", f"Cannot read the columns of {t}:\n\n{e}"))

        threading.Thread(target=work, daemon=True).start()

    def _show_columns(self, table, info):
        """Window: one row per column, one column per source (its type there, blank = missing)."""
        sources, cols, src_cols, linked = info
        have = [s for s in sources if s in src_cols]
        rows = list(cols)
        target_names = {c for c, _ in cols}
        for s in have:  # columns a source has that the target does not (should not happen)
            rows += [(c, "") for c in src_cols[s] if c not in target_names and (c, "") not in rows]

        win = tk.Toplevel(self)
        win.title(f"{table} - columns")
        win.geometry(f"{min(1600, 540 + 160 * len(sources))}x600")
        if not linked:
            note = "The source links were removed (Finish), so only the target columns are known. Run step 1 again to compare."
        else:
            missing = [s for s in sources if s not in src_cols]
            note = (f"{len(rows)} columns. Table found in {len(have)} of {len(sources)} sources"
                    + (f" (not in: {', '.join(missing)})" if missing else "") + ".  "
                    "Cell = column type in that source, blank = column not in that source.  "
                    "Orange = only in some sources, grey = added by the migration, red = not in the target.")
        ttk.Label(win, text=note, wraplength=1100, padding=(8, 8, 8, 4)).pack(fill="x")

        frame = ttk.Frame(win, padding=(8, 0, 8, 8))
        frame.pack(fill="both", expand=True)
        ids = ["column", "target"] + [f"s{i}" for i in range(len(sources))] + ["found"]
        tree = ttk.Treeview(frame, columns=ids, show="headings")
        tree.heading("column", text="Column")
        tree.heading("target", text="Target type")
        tree.column("column", width=240, stretch=False)
        tree.column("target", width=180, stretch=False)
        for i, s in enumerate(sources):
            tree.heading(f"s{i}", text=s if s in src_cols else f"{s} (no table)")
            tree.column(f"s{i}", width=160, stretch=False)
        tree.heading("found", text="Found in")
        tree.column("found", width=80, anchor="center", stretch=False)
        tree.tag_configure("partial", foreground="#b35900")
        tree.tag_configure("added", foreground="#888888")
        tree.tag_configure("srconly", foreground="#c00000")
        for c, typ in rows:
            n = sum(1 for s in have if c in src_cols[s])
            tag = ("srconly",) if not typ else ("added",) if linked and n == 0 else \
                  ("partial",) if n < len(have) else ()
            tree.insert("", "end", tags=tag, values=[c, typ or "(not in target)"]
                        + [src_cols.get(s, {}).get(c, "") for s in sources]
                        + [f"{n}/{len(have)}" if linked else ""])
        ys = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        xs = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

    def _tick_all(self, on):
        self.checked = {r[0] for r in self.plan_rows} if on else set()
        for r in self.plan_rows:
            self.tree.set(r[0], "tick", "☑" if on else "☐")

    def _tick_not_loaded(self):
        self.checked = {r[0] for r in self.plan_rows if not r[6]}
        for r in self.plan_rows:
            self.tree.set(r[0], "tick", "☑" if r[0] in self.checked else "☐")

    def _refresh(self, quiet=False):
        try:
            cfg = self._config()
        except ValueError as e:
            if not quiet:
                messagebox.showerror(TITLE, str(e))
            return
        try:
            self._show_plan(Runner(cfg, self.queue).plan())
        except (StepFailed, OSError) as e:
            if not quiet:
                messagebox.showerror(TITLE, f"Cannot read the table list:\n\n{e}")

    # ---------- actions ----------
    def _config(self):
        v = {k: var.get().strip() for k, var in self.vars.items()}
        v["password"] = self.vars["password"].get()
        v["src_password"] = v["password"] if self.same_pw.get() else self.vars["src_password"].get()
        if not v["port"].isdigit():
            raise ValueError("Port must be a number.")
        v["port"] = int(v["port"])
        if not v["target"] or not all(c.isalnum() or c == "_" for c in v["target"]):
            raise ValueError("Target DB name must be letters, digits or _.")
        # accept "cwp_mig_prd" as well as a pasted "SET ROLE cwp_mig_prd;"
        role = v["role"].rstrip(";").strip()
        if role[:9].lower() == "set role ":
            role = role[9:].strip()
        if role and not all(ch.isalnum() or ch in "_$" for ch in role):
            raise ValueError("Set role: enter just the role name, e.g. cwp_mig_prd (letters, digits, _ or $).")
        v["role"] = role
        v["drop_existing"] = self.drop_existing.get()
        v["clear_existing"] = self.clear_existing.get() and not v["drop_existing"]
        v["kerberos"] = self.kerberos.get()
        v["src_kerberos"] = self.src_kerberos.get()
        if v["src_kerberos"]:
            v["src_password"] = ""
        return v

    def _db_list(self):
        names = []
        for line in self.db_text.get("1.0", "end").splitlines():
            n = line.strip()
            if n and n not in names:
                names.append(n)
        return names

    def _load_dbs(self):
        try:
            cfg = self._config()
        except ValueError as e:
            messagebox.showerror(TITLE, str(e))
            return
        try:
            out = Runner(cfg, self.queue).psql_scalar(
                "postgres", "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate "
                            f"AND datname NOT IN ('postgres', '{cfg['target']}') ORDER BY datname")
        except (StepFailed, OSError) as e:
            messagebox.showerror(TITLE, f"Cannot list databases:\n\n{e}")
            return
        names = [n for n in out.splitlines() if n.strip()]
        if self._db_list() and not messagebox.askyesno(
                TITLE, f"Replace the list with all {len(names)} databases on the server?\n\n"
                       "Remove any you don't want, and put the most important first."):
            return
        self.db_text.delete("1.0", "end")
        self.db_text.insert("1.0", "\n".join(names))

    def _test(self):
        try:
            cfg = self._config()
        except ValueError as e:
            messagebox.showerror(TITLE, str(e))
            return
        self.status.configure(text="Testing connection...")

        def work():  # off the UI thread so the window stays responsive while connecting
            try:
                who = Runner(cfg, self.queue).psql_scalar(
                    "postgres", "SELECT 'Login: ' || session_user || E'\\nRunning as: ' || current_user"
                                " || E'\\n\\n' || version()")
                self.queue.put(("info", f"Connected.\n\n{who}"))
            except (StepFailed, OSError) as e:
                self.queue.put(("error", f"Connection failed:\n\n{e}"))

        threading.Thread(target=work, daemon=True).start()

    def _test_sources(self):
        """Log in to every listed source database with the source login, the way step 1
        reads their schemas. (postgres_fdw itself connects from the target server.)"""
        try:
            cfg = self._config()
        except ValueError as e:
            messagebox.showerror(TITLE, str(e))
            return
        dbnames = self._db_list()
        if not dbnames:
            messagebox.showerror(TITLE, "The Source databases list is empty.")
            return
        entries, _ = self._source_entries(dbnames)
        self.status.configure(text="Testing source logins...")

        def work():
            runner, lines, failed = Runner(cfg, self.queue), [], 0
            for s in entries:
                cmd, env = runner.source_cmd(s)
                try:
                    r = subprocess.run(cmd + ["-X", "-w", "-tA", "-c", "SELECT 1"], env=env, creationflags=NO_WINDOW,
                                       stdin=subprocess.DEVNULL, capture_output=True)
                    err = r.stderr.decode("utf-8", errors="replace").strip() if r.returncode else ""
                except OSError as e:
                    err = str(e)
                if err:
                    failed += 1
                    lines.append(f"FAIL  {s['dbname']}: {err}")
                else:
                    lines.append(f"OK    {s['dbname']}")
            report = f"Source user: {cfg['src_user']}\n\n" + "\n".join(lines)
            runner.log("\n==== test source logins ====\n" + report)
            if failed:
                self.queue.put(("error", f"{failed} of {len(entries)} source logins failed:\n\n{report}"))
            else:
                self.queue.put(("info", f"All {len(entries)} source logins OK.\n\n{report}"))

        threading.Thread(target=work, daemon=True).start()

    def _launch(self, job_name, *args, cfg):
        self._save_settings(cfg)
        self.running = True
        for b in (self.step1_btn, self.copy_btn, self.detach_btn, self.matrix_btn):
            b.configure(state="disabled")
        self.status.configure(text="Running...")
        runner = Runner(cfg, self.queue)
        threading.Thread(target=runner.run, args=(getattr(runner, job_name), *args), daemon=True).start()

    def _step1(self):
        if self.running:
            return
        try:
            cfg = self._config()
        except ValueError as e:
            messagebox.showerror(TITLE, str(e))
            return
        dbnames = self._db_list()
        if not dbnames:
            messagebox.showerror(TITLE, "Enter at least one source database.")
            return
        if cfg["target"] in dbnames:
            messagebox.showerror(TITLE, "The target database can't also be a source.")
            return
        entries, renamed = self._source_entries(dbnames)
        if renamed:
            lines = []
            for db, name, other in renamed:
                lines.append(f"  {db}  ->  {name}" + (f"   (same as {other})" if other else ""))
            if not messagebox.askyesno(
                    TITLE, "These source databases get a different internal name (schema src_<name>), "
                           "because their names only differ in case or symbols, or are too long:\n\n"
                    + "\n".join(lines)
                    + "\n\nIf two of them are really the same database listed twice, click No and "
                      "remove the extra line from the Source databases list.\n\nContinue with step 1?",
                    icon="warning"):
                return
        if cfg["drop_existing"] and not messagebox.askyesno(
                TITLE, f"'Drop existing target' is ticked.\n\n{cfg['target']} will be DROPPED with all its data "
                       "and rebuilt empty if it exists. Continue?", icon="warning"):
            return
        if cfg["clear_existing"] and not messagebox.askyesno(
                TITLE, f"'Clear existing target' is ticked.\n\nIf {cfg['target']} exists, EVERY table, view, "
                       "function, schema and foreign server in it will be DELETED with all data. The database "
                       f"itself is kept.\n\nMake sure {cfg['target']} is not a database you still need. Continue?",
                icon="warning"):
            return
        try:
            self._write_sources(entries, cfg["target"])
        except OSError as e:
            messagebox.showerror(TITLE, f"Cannot write sources.json:\n\n{e}")
            return
        if renamed:
            self.log.insert("end", "Internal source names (schema src_<name>) made unique: "
                                   + "; ".join(f"{db} -> {name}" for db, name, _ in renamed) + "\n")
        self.checked.clear()
        self._launch("step1", cfg=cfg)

    def _step2(self):
        if self.running:
            return
        try:
            cfg = self._config()
        except ValueError as e:
            messagebox.showerror(TITLE, str(e))
            return
        if not self.plan_rows:
            messagebox.showerror(TITLE, "No table list yet. Run step 1, or click Refresh list.")
            return
        if not self.checked:
            messagebox.showerror(TITLE, "Tick at least one table (click its row).")
            return
        need, reload = closure(self.checked, self.plan_rows)
        lines = [f"Copy data of {len(need)} table(s):"]
        added = need - self.checked
        if added:
            lines.append("\nAdded automatically (needed): " + ", ".join(sorted(added)))
        if reload:
            lines.append("\nRELOAD - these already hold data; they are emptied and copied again:\n  "
                         + ", ".join(sorted(reload))
                         + "\nOther tables with a foreign key to them (e.g. MetadataHorizontal) are emptied too.")
        lines.append("\nContinue?")
        if not messagebox.askyesno(TITLE, "\n".join(lines), icon="warning" if reload else "question"):
            return
        self._launch("step2", sorted(need), sorted(reload), cfg=cfg)

    def _matrix(self):
        if self.running:
            return
        try:
            cfg = self._config()
        except ValueError as e:
            messagebox.showerror(TITLE, str(e))
            return
        if not messagebox.askyesno(TITLE, f"Build {metadata_matrix.DEFAULT_TABLE} in {cfg['target']}?\n\n"
                                          "One row per file, one column per distinct MetadataKey in Metadata. "
                                          "It is rebuilt from scratch each time; Metadata is not changed.\n\n"
                                          "Copy the Metadata table (step 2) first."):
            return
        self._launch("matrix", cfg=cfg)

    def _detach(self):
        if self.running:
            return
        try:
            cfg = self._config()
        except ValueError as e:
            messagebox.showerror(TITLE, str(e))
            return
        if not messagebox.askyesno(TITLE, "Remove the links to the source databases?\n\n"
                                          "Do this when all the data you need is copied. "
                                          "To copy more afterwards you must run step 1 again."):
            return
        self._launch("detach", cfg=cfg)

    def _poll(self):
        try:
            while True:
                kind, data = self.queue.get_nowait()
                if kind == "text":
                    self.log.insert("end", data + "\n")
                    self.log.see("end")
                elif kind == "columns":
                    if not self.running:
                        self.status.configure(text=f"psql: {find_psql()}")
                    self._show_columns(*data)
                elif kind in ("info", "error"):
                    if not self.running:
                        self.status.configure(text=f"psql: {find_psql()}")
                    (messagebox.showinfo if kind == "info" else messagebox.showerror)(TITLE, data)
                elif kind == "step":
                    self.status.configure(text=f"Running: {data}")
                elif kind == "done":
                    self.running = False
                    for b in (self.step1_btn, self.copy_btn, self.detach_btn, self.matrix_btn):
                        b.configure(state="normal")
                    self.status.configure(text="Finished OK" if data == "ok" else "FAILED - see log")
                    if data == "ok":
                        self.checked.clear()
                    self._refresh(quiet=True)
        except queue.Empty:
            pass
        self.after(100, self._poll)


if __name__ == "__main__":
    App().mainloop()
