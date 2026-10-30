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

The "Compare databases" tab lists the tables and columns that differ between
two databases on the target server (e.g. cwp_ecr and cwp) and can add the
missing ones to the second so it matches the first (schema_compare.py).

The "ACL permissions" tab reads the NTFS permissions (Get-Acl, as permission.ps1
does) of a folder path, its subfolders and files, marks permission.ps1's protected
accounts and the ones missing; "Full report to Excel" writes every entry of every
subfolder and file to .xlsx (acl_check.py). Reading changes nothing; "Apply Excel to
folders" puts such a report (edited in Excel if needed) back: every path in it gets
exactly the entries listed for it (Preview first shows what would change).
"Backup (.acl.bak) to Excel" writes the same report from permission.ps1's ACL backups
(the permissions as they were before it ran), so Apply can also restore them.

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
import csv
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
from tkinter import filedialog, messagebox, scrolledtext, ttk

import acl_check
import generate
import schema_compare

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
ACL_SHOW_MAX = 5000   # items kept on the ACL tab; the Excel report has all

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
# Columns of public.<t> (source '') and of each source's linked foreign table src_<name>.<t>
# (staging layout: its staging table, from _merge.stage).
COLUMNS_QUERY = """SELECT src, attname, typ FROM (
    SELECT '' AS src, 0 AS ord, a.attnum, a.attname, format_type(a.atttypid, a.atttypmod) AS typ
    FROM pg_attribute a WHERE a.attrelid = to_regclass(format('public.%I', '{t}'))
      AND a.attnum > 0 AND NOT a.attisdropped
    UNION ALL
    SELECT s.source_db, s.load_order, a.attnum, a.attname, format_type(a.atttypid, a.atttypmod)
    FROM _merge.source s JOIN pg_attribute a ON a.attrelid = {source_rel}
    WHERE a.attnum > 0 AND NOT a.attisdropped
) c ORDER BY ord, attnum"""
SOURCE_REL_DIRECT = "to_regclass(format('%I.%I', s.foreign_schema, '{t}'))"
SOURCE_REL_STAGING = ("(SELECT to_regclass(format('public.%I', g.stage_table)) FROM _merge.stage g "
                      "WHERE g.source_db = s.source_db AND g.table_name = '{t}')")
# How many source links (direct) / staging copies (through this PC) are still there.
LINKS_DIRECT = "SELECT count(*) FROM pg_foreign_server WHERE srvname LIKE 'srv\\_%'"
LINKS_STAGING = "SELECT count(*) FROM _merge.stage WHERE to_regclass(format('public.%I', stage_table)) IS NOT NULL"
# Which layout an existing target was built with.
LAYOUT_QUERY = ("SELECT CASE WHEN to_regclass('public._merge_source') IS NOT NULL THEN 'staging' "
                "WHEN to_regclass('_merge.source') IS NOT NULL THEN 'direct' ELSE '' END")


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

    def layout(self, target):
        """'direct' (postgres_fdw + _merge schema), 'staging' (through this PC) or '' (not built)."""
        return self.psql_scalar(target, LAYOUT_QUERY)

    @staticmethod
    def m(sql, staging):
        """SQL naming _merge.<x> -> public._merge_<x> for the staging layout."""
        return generate.flatten_merge(sql) if staging else sql

    # ---------- work ----------
    def generate(self, staging):
        """Refresh migration_work\\ from the bundle and regenerate from sources.json."""
        self.section("generate")
        os.makedirs(WORK_SQL, exist_ok=True)
        for name in os.listdir(os.path.join(RES_CONS, "sql")):
            with open(os.path.join(RES_CONS, "sql", name), encoding="utf-8") as f:
                text = f.read()
            with open(os.path.join(WORK_SQL, name), "w", encoding="utf-8", newline="\n") as f:
                f.write(self.m(text, staging))
        shutil.copy2(SOURCES_FILE, os.path.join(WORK_DIR, "sources.json"))
        generate.HERE, generate.SQL_DIR, generate.DUMP_DIR = WORK_DIR, WORK_SQL, RES_DUMPS
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                return generate.main(generate.psql_schema_loader(self.source_cmd), staging=staging)
        except SystemExit as e:
            raise StepFailed(f"generate failed: {e}")
        finally:
            self.log(buf.getvalue().rstrip())
            self.log("layout: " + ("through this PC (staging tables in public)" if staging
                                   else "direct (postgres_fdw links)"))

    def stage_data(self, g, target):
        """Through this PC: copy every source table into its staging table (generate.copy_to_stage)."""
        def progress(i, n, s, t):
            self.out.put(("step", f"copy to staging {i}/{n}: {s['name']}.{t}"))
        try:
            counts = generate.copy_to_stage(g, self.source_cmd, lambda: (self._cmd(target, []), self.env),
                                            log=self.log, progress=progress)
        except RuntimeError as e:
            raise StepFailed(str(e))
        if counts:  # in a file: hundreds of tables would not fit on a command line
            path = self.sql(f"stage_counts_{self.stamp}.sql")
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(self.m(g.stage_counts_sql(counts), True))
            self.psql_run(target, "stage_counts", file=path, extra_args=["-q"])

    def sql(self, name):
        return os.path.join(WORK_SQL, name)

    def step1(self):
        """Create the target with every table and link the sources; no rows copied."""
        target, staging = self.cfg["target"], self.cfg["staging"]
        g = self.generate(staging)
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
                # Only drop a database this tool built (it has _merge, either layout), never a source.
                if not self.layout(target):
                    raise StepFailed(f"Database {target} exists and was not built by this tool (no _merge tables), "
                                     "so it will not be dropped. Use 'Clear existing target' instead (keeps the "
                                     "database, drops its tables), or pick a new Target DB name.")
                self.psql_run("postgres", "drop", command=f'DROP DATABASE "{target}" WITH (FORCE)')
            self.psql_run("postgres", "create", command=f"CREATE DATABASE \"{target}\" ENCODING 'UTF8' TEMPLATE template0")
        self.section("schema")
        # one transaction: a failure (e.g. no CREATE right for _merge) leaves the target empty, not half-built
        self.psql_run(target, "schema", file=self.sql("00_target_schema.sql"), extra_args=["--single-transaction"])
        if staging:
            self.section("staging tables")
            self.psql_run(target, "stage", file=self.sql("01_attach_sources.sql"), extra_args=["-q", "--single-transaction"])
            self.section("copy sources to staging (through this PC)")
            self.stage_data(g, target)
        else:
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
        layout = self.layout(target)
        if not layout:
            raise StepFailed(f"{target} is not prepared - run step 1 first.")
        staging = layout == "staging"
        g = self.generate(staging)
        self.section("check")
        try:
            db_sources = self.psql_scalar(target, self.m(
                "SELECT string_agg(source_db, ',' ORDER BY load_order) FROM _merge.source", staging))
            db_tables = self.psql_scalar(target, self.m(
                "SELECT string_agg(table_name, ',' ORDER BY load_order) FROM _merge.table_plan", staging))
            links = self.psql_scalar(target, self.m(LINKS_STAGING, True) if staging else LINKS_DIRECT)
        except StepFailed as e:
            raise StepFailed(f"{target} is not prepared - run step 1 first.\n{e}")
        if db_sources != ",".join(s["name"] for s in g.sources):
            raise StepFailed(f"The source list changed since step 1 (then: {db_sources}). Run step 1 again.")
        if db_tables != ",".join(g.plan_tables()):
            raise StepFailed("The tables in the sources changed since step 1. Run step 1 again.")
        if links == "0":
            raise StepFailed("The source links / staging copies were removed (Finish). Run step 1 again to copy more data.")
        self.log("sources and tables match step 1")
        load_file = self.sql(f"load_{self.stamp}.sql")
        verify_file = self.sql(f"verify_{self.stamp}.sql")
        with open(load_file, "w", encoding="utf-8", newline="\n") as f:
            f.write(self.m(g.load_script(set(tables), set(reload)), staging))
        with open(verify_file, "w", encoding="utf-8", newline="\n") as f:
            f.write(self.m(g.verify(set(tables)), staging))
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
            staging = self.layout(target) == "staging"
            migrated = json.loads(self.psql_scalar(target, self.m("SELECT COALESCE(json_agg(table_name), '[]') "
                                                                  "FROM _merge.table_plan", staging)))
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
        target = self.cfg["target"]
        layout = self.layout(target)
        if not layout:
            raise StepFailed(f"{target} is not prepared - nothing to remove.")
        self.generate(layout == "staging")
        self.section("detach")
        self.psql_run(target, "detach", file=self.sql("06_detach_sources.sql"))
        self.log("\nStaging copies of the sources removed. The _merge_* audit tables are kept." if layout == "staging"
                 else "\nSource links removed. The _merge audit schema is kept.")

    def plan(self):
        """[(table, kind, needs, sources, source_rows, loaded_rows, loaded_at)] or None if not prepared."""
        target = self.cfg["target"]
        ok = self.psql_scalar("postgres", f"SELECT 1 FROM pg_database WHERE datname = '{target}'")
        if ok != "1":
            return None
        layout = self.layout(target)
        staging = layout == "staging"
        if not layout or self.psql_scalar(target, self.m(
                "SELECT to_regclass('_merge.table_plan') IS NOT NULL", staging)) != "t":
            return None
        out = self.psql_scalar(target, self.m(PLAN_QUERY, staging), extra=("-F", "\t"))
        return [tuple(line.rstrip("\r").split("\t")) for line in out.splitlines() if line.strip()]

    def columns(self, table):
        """(sources in priority order, [(col, type)] of the target table,
        {source: {col: type}} for sources whose linked (foreign) table exists, linked?)."""
        target = self.cfg["target"]
        staging = self.layout(target) == "staging"
        sources = self.psql_scalar(target, self.m("SELECT source_db FROM _merge.source ORDER BY load_order",
                                                  staging)).splitlines()
        linked = self.psql_scalar(target, self.m(LINKS_STAGING, True) if staging else LINKS_DIRECT) != "0"
        t = table.replace("'", "''")
        rel = (SOURCE_REL_STAGING if staging else SOURCE_REL_DIRECT).format(t=t)
        out = self.psql_scalar(target, self.m(COLUMNS_QUERY.format(t=t, source_rel=rel), staging), extra=("-F", "\t"))
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

    def read_schema(self, database):
        """Tables and columns of `database` (schema_compare.parse)."""
        return schema_compare.parse(self.psql_scalar(database, schema_compare.SCHEMA_SQL,
                                                     extra=("-q", "-c", "SET search_path = ''")))

    def fix_schema(self, database, path):
        """Run a schema_compare.fix_script file in `database`: all of it or nothing."""
        self.section(f"add missing tables / columns to {database}")
        self.psql_run(database, "schema_fix", file=path, extra_args=["--single-transaction"])
        self.log(f"\n{database}: missing tables / columns added (no rows copied).")

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
        self.after_done = None  # called once the running job finished OK
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
        self.copy_mode = tk.StringVar(value="staging" if s.get("staging") else "direct")
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
                                    "kerberos", "krbsrvname", "src_kerberos", "role", "staging")}
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(keep, f, indent=2)
        except OSError:
            pass

    # ---------- layout ----------
    def _build(self):
        pad = {"padx": 6, "pady": 3}
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=14, pady=(8, 0))
        # Connection and source settings, used by every other tab.
        top = ttk.Frame(nb, padding=(0, 8, 0, 6))
        nb.add(top, text="Config settings")

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
        top.rowconfigure(0, weight=1)

        merge = ttk.Frame(nb, padding=(0, 0, 0, 6))
        nb.add(merge, text="Merge databases")

        # Step 1
        s1 = ttk.LabelFrame(merge, text="Step 1 - Prepare target: create ALL tables (no data copied yet)", padding=8)
        s1.pack(fill="x", padx=6, pady=(8, 0))
        mode = ttk.Frame(s1)  # packed first at the bottom so it gets its own full-width row
        mode.pack(side="bottom", fill="x", pady=(6, 0))
        ttk.Label(mode, text="Copy method:").pack(side="left")
        ttk.Radiobutton(mode, text="Direct (server links: needs CREATE on the database + postgres_fdw)",
                        variable=self.copy_mode, value="direct").pack(side="left", padx=(6, 12))
        ttk.Radiobutton(mode, text="Through this PC (only needs CREATE on schema public; step 1 also "
                                   "copies all source rows into staging tables, slower)",
                        variable=self.copy_mode, value="staging").pack(side="left")
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
        s2 = ttk.LabelFrame(merge, text="Step 2 - Copy data: tick tables, then Copy (repeat as often as you like)", padding=8)
        s2.pack(fill="both", expand=True, padx=6, pady=(8, 0))
        bar = ttk.Frame(s2)
        bar.pack(fill="x")
        self.copy_btn = ttk.Button(bar, text="Copy data of ticked tables", command=self._step2)
        self.copy_btn.pack(side="left")
        ttk.Button(bar, text="Refresh list", command=self._refresh).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Tick all", command=lambda: self._tick_all(True)).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Tick not loaded", command=self._tick_not_loaded).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Untick all", command=lambda: self._tick_all(False)).pack(side="left", padx=(8, 0))
        self.detach_btn = ttk.Button(bar, text="Finish: remove source links / copies", command=self._detach)
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

        self._build_compare(nb)
        self._build_acl(nb)
        self.job_btns = (self.step1_btn, self.copy_btn, self.detach_btn, self.matrix_btn, self.fix_btn)

        status = ttk.Frame(self, padding=(14, 4, 14, 0))
        status.pack(fill="x")
        ttk.Button(status, text="Clear log", command=lambda: self.log.delete("1.0", "end")).pack(side="left")
        self.status = ttk.Label(status, text=f"psql: {find_psql()}")
        self.status.pack(side="right")

        self.log = scrolledtext.ScrolledText(self, font=("Consolas", 9), wrap="none", height=10)
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

    # ---------- compare databases ----------
    def _build_compare(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="Compare databases")
        self.cmp_items, self.cmp_checked, self.cmp_dbs = [], set(), None
        names = self._db_list()
        self.cmp_a = tk.StringVar(value=names[0] if names else "")
        self.cmp_b = tk.StringVar(value=names[1] if len(names) > 1 else "")
        self.cmp_only_missing = tk.BooleanVar(value=False)
        self.cmp_filter = tk.StringVar()

        bar = ttk.Frame(tab)
        bar.pack(fill="x")
        ttk.Label(bar, text="Reference (A)").pack(side="left")
        for var, label in ((self.cmp_a, "Compare with (B, gets the missing ones)"), (self.cmp_b, None)):
            box = ttk.Combobox(bar, textvariable=var, width=26)
            box.configure(postcommand=lambda b=box: b.configure(values=self._db_list()))
            box.pack(side="left", padx=(6, 6))
            if label:
                ttk.Button(bar, text="⇄", width=3, command=self._cmp_swap).pack(side="left", padx=(0, 6))
                ttk.Label(bar, text=label).pack(side="left")
        ttk.Button(bar, text="Compare", command=self._compare).pack(side="left", padx=(6, 0))
        ttk.Label(bar, text="Both on the Target server (Config settings tab), same login. The list = Source databases.",
                  foreground="#555").pack(side="left", padx=12)

        flt = ttk.Frame(tab)
        flt.pack(fill="x", pady=(6, 0))
        ttk.Label(flt, text="Filter table").pack(side="left")
        ttk.Entry(flt, textvariable=self.cmp_filter, width=26).pack(side="left", padx=(6, 12))
        self.cmp_filter.trace_add("write", lambda *_: self._render_compare())
        ttk.Checkbutton(flt, text="Only what B is missing", variable=self.cmp_only_missing,
                        command=self._render_compare).pack(side="left")
        self.fix_btn = ttk.Button(flt, text="Create ticked in B", command=self._cmp_fix)
        self.fix_btn.pack(side="right")
        ttk.Button(flt, text="Show SQL of ticked", command=self._cmp_show_sql).pack(side="right", padx=(0, 8))
        ttk.Button(flt, text="Untick all", command=lambda: self._cmp_tick(False)).pack(side="right", padx=(0, 8))
        ttk.Button(flt, text="Tick all missing", command=lambda: self._cmp_tick(True)).pack(side="right", padx=(0, 8))

        self.cmp_info = ttk.Label(tab, text="Pick two databases and click Compare.", foreground="#555")
        self.cmp_info.pack(fill="x", pady=(6, 2))
        frame = ttk.Frame(tab)
        frame.pack(fill="both", expand=True)
        cols = ("tick", "kind", "table", "column", "a", "b", "note")
        heads = ("Add", "Difference", "Table", "Column", "In A", "In B", "Note")
        widths = (44, 150, 230, 180, 260, 260, 260)
        self.cmp_tree = ttk.Treeview(frame, columns=cols, show="headings", height=11, selectmode="none")
        for c, h, w in zip(cols, heads, widths):
            self.cmp_tree.heading(c, text=h)
            self.cmp_tree.column(c, width=w, anchor="center" if c == "tick" else "w",
                                 stretch=c in ("table", "a", "b", "note"))
        self.cmp_tree.tag_configure("missing", foreground="#c00000")
        self.cmp_tree.tag_configure("differs", foreground="#b35900")
        self.cmp_tree.tag_configure("extra", foreground="#888888")
        ys = ttk.Scrollbar(frame, orient="vertical", command=self.cmp_tree.yview)
        xs = ttk.Scrollbar(frame, orient="horizontal", command=self.cmp_tree.xview)
        self.cmp_tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.cmp_tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.cmp_tree.bind("<Button-1>", self._cmp_click)

    def _cmp_swap(self):
        a, b = self.cmp_a.get(), self.cmp_b.get()
        self.cmp_a.set(b)
        self.cmp_b.set(a)

    def _compare(self):
        try:
            cfg = self._config()
        except ValueError as e:
            messagebox.showerror(TITLE, str(e))
            return
        a, b = self.cmp_a.get().strip(), self.cmp_b.get().strip()
        if not a or not b or a == b:
            messagebox.showerror(TITLE, "Pick two different databases: A (reference) and B.")
            return
        self.status.configure(text=f"Comparing {a} and {b}...")

        def work():
            try:
                runner = Runner(cfg, self.queue)
                items = schema_compare.diff(runner.read_schema(a), runner.read_schema(b))
                self.queue.put(("compare", (a, b, items)))
            except (StepFailed, OSError, ValueError) as e:
                self.queue.put(("error", f"Cannot compare {a} and {b}:\n\n{e}"))

        threading.Thread(target=work, daemon=True).start()

    def _show_compare(self, a, b, items):
        self.cmp_dbs, self.cmp_items, self.cmp_checked = (a, b), items, set()
        self.cmp_tree.heading("a", text=f"In A: {a}")
        self.cmp_tree.heading("b", text=f"In B: {b}")
        self._render_compare()

    def _render_compare(self):
        tree = self.cmp_tree
        tree.delete(*tree.get_children())
        if not self.cmp_dbs:
            return
        a, b = self.cmp_dbs
        text = self.cmp_filter.get().strip().lower()
        shown = 0
        for i, it in enumerate(self.cmp_items):
            if self.cmp_only_missing.get() and not it["sql"]:
                continue
            label = schema_compare.table_label(it["schema"], it["table"])
            if text and text not in label.lower():
                continue
            shown += 1
            tag = "missing" if it["sql"] else "differs" if it["kind"] == "differs" else "extra"
            tick = ("☑" if i in self.cmp_checked else "☐") if it["sql"] else ""
            tree.insert("", "end", iid=str(i), tags=(tag,),
                        values=(tick, schema_compare.KINDS[it["kind"]][0], label, it["column"],
                                it["a"], it["b"], it["note"]))
        n = {k: sum(1 for it in self.cmp_items if it["kind"] == k) for k in schema_compare.KINDS}
        if not self.cmp_items:
            self.cmp_info.configure(text=f"{a} and {b} have the same tables and columns.")
            return
        self.cmp_info.configure(
            text=f"A = {a}, B = {b}:  {n['missing_table']} table(s) and {n['missing_column']} column(s) missing in B "
                 f"(red, can be added),  {n['differs']} column(s) with a different type / NOT NULL (orange),  "
                 f"{n['extra_table']} table(s) and {n['extra_column']} column(s) only in B (grey).  "
                 f"Showing {shown}. Click a red row to tick it.")

    def _cmp_click(self, event):
        iid = self.cmp_tree.identify_row(event.y)
        if not iid or not self.cmp_items[int(iid)]["sql"]:
            return
        i = int(iid)
        self.cmp_checked ^= {i}
        self.cmp_tree.set(iid, "tick", "☑" if i in self.cmp_checked else "☐")

    def _cmp_tick(self, on):
        """Tick / untick the missing tables and columns now shown (the filter applies)."""
        for iid in self.cmp_tree.get_children():
            i = int(iid)
            if self.cmp_items[i]["sql"]:
                (self.cmp_checked.add if on else self.cmp_checked.discard)(i)
                self.cmp_tree.set(iid, "tick", "☑" if on else "☐")

    def _cmp_ticked(self):
        """(a, b, ticked items) or None after telling the user what is wrong."""
        if not self.cmp_dbs:
            messagebox.showerror(TITLE, "Click Compare first.")
            return None
        if self.cmp_dbs != (self.cmp_a.get().strip(), self.cmp_b.get().strip()):
            messagebox.showerror(TITLE, "The databases were changed since the last Compare. Click Compare again.")
            return None
        items = [self.cmp_items[i] for i in sorted(self.cmp_checked)]
        if not items:
            messagebox.showerror(TITLE, "Tick at least one missing table or column (click a red row).")
            return None
        return (*self.cmp_dbs, items)

    def _cmp_show_sql(self):
        got = self._cmp_ticked()
        if not got:
            return
        a, b, items = got
        win = tk.Toplevel(self)
        win.title(f"SQL to run in {b}")
        win.geometry("900x600")
        txt = scrolledtext.ScrolledText(win, font=("Consolas", 9), wrap="none")
        txt.pack(fill="both", expand=True, padx=8, pady=8)
        txt.insert("1.0", schema_compare.fix_script(items, a, b))

    def _cmp_fix(self):
        if self.running:
            return
        try:
            cfg = self._config()
        except ValueError as e:
            messagebox.showerror(TITLE, str(e))
            return
        got = self._cmp_ticked()
        if not got:
            return
        a, b, items = got
        n_tab = sum(1 for it in items if it["kind"] == "missing_table")
        if not messagebox.askyesno(
                TITLE, f"Change database {b} so it matches {a}:\n\n"
                       f"  create {n_tab} table(s)\n  add {len(items) - n_tab} column(s)\n\n"
                       "Structure only, no rows are copied. It runs as one transaction: if any statement fails, "
                       f"nothing is changed. Click 'Show SQL of ticked' to see it first.\n\nChange {b} now?",
                icon="warning"):
            return
        os.makedirs(WORK_SQL, exist_ok=True)
        path = os.path.join(WORK_SQL, f"schema_fix_{b}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sql")
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(schema_compare.fix_script(items, a, b))
        except OSError as e:
            messagebox.showerror(TITLE, f"Cannot write {path}:\n\n{e}")
            return
        self.after_done = self._compare  # show what is left
        self._launch("fix_schema", b, path, cfg=cfg)

    # ---- ACL permissions tab (acl_check.py) ---------------------------------------------------

    def _build_acl(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="ACL permissions")
        self.acl_items, self.acl_errors, self.acl_proc, self.acl_report, self.acl_root_path = [], [], None, None, ""
        self.acl_counts = {}
        self.acl_applied = []   # results of the last "Apply Excel to folders"
        self.acl_path = tk.StringVar()
        self.acl_levels = tk.StringVar(value="0")
        self.acl_files = tk.BooleanVar(value=False)
        self.acl_own_only = tk.BooleanVar(value=True)
        self.acl_filter = tk.StringVar()
        self.acl_only_missing = tk.BooleanVar(value=False)

        self.acl_mode = tk.StringVar(value="tree")
        modes = ttk.Frame(tab)
        modes.pack(fill="x")
        ttk.Label(modes, text="Mode:").pack(side="left")
        ttk.Radiobutton(modes, text="Folder tree (a folder and what is below it)", variable=self.acl_mode,
                        value="tree", command=self._acl_mode_changed).pack(side="left", padx=(6, 12))
        ttk.Radiobutton(modes, text="List of paths (only the listed folders / files)", variable=self.acl_mode,
                        value="list", command=self._acl_mode_changed).pack(side="left")
        self.acl_stop_btn = ttk.Button(modes, text="Stop", command=self._acl_stop, state="disabled")
        self.acl_stop_btn.pack(side="right")
        self.acl_full_btn = ttk.Button(modes, text="Full report to Excel...", command=self._acl_full_report)
        self.acl_full_btn.pack(side="right", padx=(0, 6))
        self.acl_apply_btn = ttk.Button(modes, text="Apply Excel to folders...", command=self._acl_apply)
        self.acl_apply_btn.pack(side="right", padx=(0, 6))
        self.acl_backup_btn = ttk.Button(modes, text="Backup (.acl.bak) to Excel...", command=self._acl_backup_report)
        self.acl_backup_btn.pack(side="right", padx=(0, 6))
        self.acl_read_btn = ttk.Button(modes, text="Read ACL", command=self._acl_read)
        self.acl_read_btn.pack(side="right", padx=(0, 6))

        self.acl_tree_bar = bar = ttk.Frame(tab)
        ttk.Label(bar, text="Folder path").pack(side="left")
        path_entry = ttk.Entry(bar, textvariable=self.acl_path, width=50)
        path_entry.pack(side="left", padx=(6, 4), fill="x", expand=True)
        path_entry.bind("<Return>", lambda _: self._acl_read())
        ttk.Button(bar, text="Browse...", command=self._acl_browse).pack(side="left", padx=(0, 12))
        ttk.Label(bar, text="Subfolder levels").pack(side="left")
        ttk.Combobox(bar, textvariable=self.acl_levels, width=6, state="readonly",
                     values=("0", "1", "2", "3", "5", "10", "all")).pack(side="left", padx=(6, 12))
        ttk.Checkbutton(bar, text="Include files", variable=self.acl_files).pack(side="left")
        ttk.Checkbutton(bar, text="Only items with their own permissions", variable=self.acl_own_only).pack(
            side="left", padx=(8, 0))

        self.acl_list_bar = lst = ttk.Frame(tab)
        side = ttk.Frame(lst)
        side.pack(side="right", fill="y", padx=(8, 0))
        ttk.Button(side, text="Load from file...", command=self._acl_load_list).pack(fill="x")
        ttk.Button(side, text="Clear", command=lambda: self.acl_list.delete("1.0", "end")).pack(fill="x", pady=(4, 0))
        self.acl_list_count = ttk.Label(side, text="0 paths", foreground="#555")
        self.acl_list_count.pack(fill="x", pady=(4, 0))
        ttk.Label(lst, text="Paths, one per line (folders or files; .txt / .csv / .xlsx can be loaded):",
                  foreground="#555").pack(anchor="w")
        self.acl_list = scrolledtext.ScrolledText(lst, height=6, font=("Consolas", 9), wrap="none")
        self.acl_list.pack(fill="x", expand=True)
        self.acl_list.bind("<<Modified>>", self._acl_list_changed)
        self._acl_mode_changed()

        self.acl_flt = flt = ttk.Frame(tab)
        flt.pack(fill="x", pady=(6, 0))
        ttk.Label(flt, text="Filter account / path").pack(side="left")
        ttk.Entry(flt, textvariable=self.acl_filter, width=30).pack(side="left", padx=(6, 12))
        self.acl_filter.trace_add("write", lambda *_: self._render_acl())
        ttk.Checkbutton(flt, text="Only items missing a protected account", variable=self.acl_only_missing,
                        command=self._render_acl).pack(side="left")
        ttk.Button(flt, text="Export shown to Excel...", command=self._acl_export).pack(side="right")
        ttk.Button(flt, text="Collapse all", command=lambda: self._acl_expand(False)).pack(side="right", padx=(0, 8))
        ttk.Button(flt, text="Expand all", command=lambda: self._acl_expand(True)).pack(side="right", padx=(0, 8))

        self.acl_info = ttk.Label(tab, text="Enter a folder (e.g. \\\\hqs03nasns.corp.mtrc.com\\CWPSP\\s\\opspj4) and "
                                            "click Read ACL, or Full report to Excel for every subfolder and file. "
                                            "Mode 'List of paths' reads only the paths you list. Reading changes nothing; "
                                            "'Apply Excel to folders' puts a (edited) report back on the folders.",
                                  foreground="#555")
        self.acl_info.pack(fill="x", pady=(6, 2))
        frame = ttk.Frame(tab)
        frame.pack(fill="both", expand=True)
        cols = ("rights", "type", "inherited", "applies", "note")
        heads = ("Rights", "Allow / Deny", "Inherited", "Applies to", "Note")
        widths = (260, 90, 80, 230, 320)
        self.acl_tree = ttk.Treeview(frame, columns=cols, show="tree headings", height=11, selectmode="browse")
        self.acl_tree.heading("#0", text="Folder / File / Account")
        self.acl_tree.column("#0", width=420, stretch=True)
        for c, h, w in zip(cols, heads, widths):
            self.acl_tree.heading(c, text=h)
            self.acl_tree.column(c, width=w, anchor="w", stretch=c in ("rights", "note"))
        self.acl_tree.tag_configure("folder", font=("Segoe UI", 9, "bold"))
        self.acl_tree.tag_configure("missing", foreground="#c00000")
        self.acl_tree.tag_configure("deny", foreground="#c00000")
        self.acl_tree.tag_configure("own", foreground="#1f4e9e")
        self.acl_tree.tag_configure("inherited", foreground="#666666")
        ys = ttk.Scrollbar(frame, orient="vertical", command=self.acl_tree.yview)
        xs = ttk.Scrollbar(frame, orient="horizontal", command=self.acl_tree.xview)
        self.acl_tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.acl_tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

    def _acl_browse(self):
        path = filedialog.askdirectory(parent=self, title="Folder to read the ACL of",
                                       initialdir=self.acl_path.get().strip() or None)
        if path:
            self.acl_path.set(os.path.normpath(path))

    def _acl_mode_changed(self):
        """Show the folder-tree controls or the path list, under the mode row."""
        listed = self.acl_mode.get() == "list"
        show, hide = (self.acl_list_bar, self.acl_tree_bar) if listed else (self.acl_tree_bar, self.acl_list_bar)
        hide.pack_forget()
        kw = {"before": self.acl_flt} if hasattr(self, "acl_flt") else {}
        show.pack(fill="x", pady=(6, 0), **kw)

    def _acl_paths(self):
        return acl_check.clean_paths(self.acl_list.get("1.0", "end").splitlines())

    def _acl_list_changed(self, _=None):
        self.acl_list.edit_modified(False)
        n = len(self._acl_paths())
        self.acl_list_count.configure(text=f"{n:,} path{'s' if n != 1 else ''}")

    def _acl_load_list(self):
        path = filedialog.askopenfilename(parent=self, title="Load a list of paths",
                                          filetypes=[("Path lists", "*.txt *.csv *.xlsx *.xlsm"), ("All files", "*.*")])
        if not path:
            return
        try:
            paths = acl_check.read_path_list(path)
        except Exception as e:
            messagebox.showerror(TITLE, f"Cannot read {path}:\n\n{e}")
            return
        if not paths:
            messagebox.showerror(TITLE, f"No paths found in {path}.\n\nPaths start with \\\\server\\share or X:\\.")
            return
        if self._acl_paths() and messagebox.askyesno(
                TITLE, f"Add the {len(paths):,} path(s) to the list?\n\nYes = add, No = replace the list."):
            paths = acl_check.clean_paths(self._acl_paths() + paths)
        self.acl_list.delete("1.0", "end")
        self.acl_list.insert("1.0", "\n".join(paths))
        self._acl_list_changed()

    def _acl_target(self):
        """(root label, paths or None) for the chosen mode, or None after telling the user what is missing."""
        if self.acl_mode.get() == "list":
            paths = self._acl_paths()
            if not paths:
                messagebox.showerror(TITLE, "Enter at least one path (one per line), or Load from file.")
                return None
            return f"{len(paths):,} listed path(s)", paths
        root = self.acl_path.get().strip().strip('"')
        if not root:
            messagebox.showerror(TITLE, "Enter a folder path.")
            return None
        return root, None

    def _acl_read(self):
        target = None if self.acl_proc else self._acl_target()
        if target:
            levels = -1 if self.acl_levels.get() == "all" else int(self.acl_levels.get())
            self._acl_start(target[0], levels, self.acl_own_only.get(), self.acl_files.get(), None, target[1])

    def _acl_full_report(self):
        """Every entry (inherited too) of every subfolder and file - or of every listed path - to an .xlsx file."""
        target = None if self.acl_proc else self._acl_target()
        if not target:
            return
        root, paths = target
        name = (f"list_{len(paths)}_paths" if paths
                else "".join(c if c.isalnum() else "_" for c in root.rstrip("\\").split("\\")[-1])[:40] or "root")
        os.makedirs(LOG_DIR, exist_ok=True)
        path = filedialog.asksaveasfilename(
            parent=self, title="Save the full ACL report", defaultextension=".xlsx",
            filetypes=[("Excel workbook", "*.xlsx")], initialdir=LOG_DIR,
            initialfile=f"acl_full_{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx")
        if path:
            self._acl_start(root, -1, False, True, path, paths)

    def _acl_backup_report(self):
        """permission.ps1's ACL backups (*.acl.bak in a folder and below) to an .xlsx report, as read before it ran."""
        if self.acl_proc:
            return
        folder = filedialog.askdirectory(parent=self, title="Folder with permission.ps1's ACL backups (*.acl.bak)")
        if not folder:
            return
        backups = acl_check.find_backups(folder)
        if not backups:
            messagebox.showerror(TITLE, f"No *.acl.bak files in {folder} or below.")
            return
        os.makedirs(LOG_DIR, exist_ok=True)
        path = filedialog.asksaveasfilename(
            parent=self, title=f"Save the report of {len(backups):,} ACL backup(s)", defaultextension=".xlsx",
            filetypes=[("Excel workbook", "*.xlsx")], initialdir=LOG_DIR,
            initialfile=f"acl_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx")
        if path:
            # "listed path(s)": Apply puts it back on the paths as written, without asking for a root
            self._acl_start(f"ACL backups in {os.path.normpath(folder)}: {len(backups):,} listed path(s)",
                            -1, False, True, path, backups=backups)

    def _acl_start(self, root, levels, own_only, files, report_path, paths=None, backups=None):
        try:
            report = acl_check.XlsxReport(report_path, root) if report_path else None
        except OSError as e:
            messagebox.showerror(TITLE, f"Cannot write {report_path}:\n\n{e}")
            return
        self.acl_items, self.acl_errors, self.acl_report, self.acl_root_path = [], [], report_path, root
        self.acl_applied = []
        self.acl_counts = {"folders": 0, "files": 0, "skipped": 0, "missing": 0, "hidden": 0}
        self.acl_tree.delete(*self.acl_tree.get_children())
        for b in (self.acl_read_btn, self.acl_full_btn, self.acl_apply_btn, self.acl_backup_btn):
            b.configure(state="disabled")
        self.acl_stop_btn.configure(state="normal")
        self.acl_info.configure(text=f"Reading the ACL of {root}...")
        self.status.configure(text="Reading ACLs...")
        try:
            self.acl_proc = proc = (acl_check.backup_start(backups) if backups
                                    else acl_check.start(root, levels, own_only, files, paths))
        except OSError as e:
            if report:
                report.close()
            self._acl_end(f"Cannot start Windows PowerShell: {e}")
            return

        def work():
            batch, other = [], []
            try:
                for raw in proc.stdout:
                    item = acl_check.parse_line(raw)
                    if item is None:
                        if raw.strip():
                            other.append(raw.decode("utf-8", errors="replace").strip())
                        continue
                    if report:
                        report.write(item)
                    if item["k"] != "done":
                        batch.append(item)
                    if len(batch) >= 200 or item["k"] == "done":
                        self.queue.put(("acl", batch))
                        batch = []
            finally:
                if report:
                    try:
                        report.close()
                    except Exception as e:
                        other.append(f"Cannot save the report {report_path}: {e}")
            if batch:
                self.queue.put(("acl", batch))
            proc.wait()
            self.queue.put(("acl_end", "\n".join(other[-5:])))

        threading.Thread(target=work, daemon=True).start()

    def _acl_stop(self):
        if self.acl_proc:
            self.acl_proc.kill()

    def _acl_add(self, batch):
        n = self.acl_counts
        for item in batch:
            if item["k"] == "skip":
                n["skipped"] += 1
                continue
            if item["k"] == "error":
                self.acl_errors.append(item)
                self._acl_insert(item)
                continue
            n["files" if item["file"] else "folders"] += 1
            n["missing"] += bool(acl_check.missing_protected(item))
            if len(self.acl_items) < ACL_SHOW_MAX:
                self.acl_items.append(item)
                self._acl_insert(item)
            else:
                n["hidden"] += 1
        self.acl_info.configure(text=f"Reading... {n['folders']:,} folder(s), {n['files']:,} file(s), "
                                     f"{n['skipped']:,} only inherit, {len(self.acl_errors):,} error(s).")

    def _acl_end(self, other):
        stopped = self.acl_proc is not None and self.acl_proc.returncode not in (0, None)
        self.acl_proc = None
        for b in (self.acl_read_btn, self.acl_full_btn, self.acl_apply_btn, self.acl_backup_btn):
            b.configure(state="normal")
        self.acl_stop_btn.configure(state="disabled")
        if not self.running:
            self.status.configure(text=f"psql: {find_psql()}")
        self._render_acl()
        if stopped:
            self.acl_info.configure(text="Stopped (the report holds what was read so far).  "
                                         + self.acl_info.cget("text"))
        if "Cannot save the report" in other:
            messagebox.showerror(TITLE, other[other.index("Cannot save the report"):])
        elif other and not self.acl_items and not self.acl_errors:
            messagebox.showerror(TITLE, f"Reading the ACL failed:\n\n{other}")
        elif self.acl_report and not stopped:
            n = self.acl_counts
            if messagebox.askyesno(TITLE, f"Full ACL report saved:\n{self.acl_report}\n\n"
                                          f"{n['folders']:,} folder(s), {n['files']:,} file(s), "
                                          f"{len(self.acl_errors):,} error(s).\n\nOpen it now?"):
                os.startfile(self.acl_report)

    def _acl_matches(self, d):
        """(show item?, entries to show) under the filter boxes."""
        text = self.acl_filter.get().strip().lower()
        if d["k"] == "error":
            return not self.acl_only_missing.get() and (not text or text in d["path"].lower()), []
        if self.acl_only_missing.get() and not acl_check.missing_protected(d):
            return False, []
        if not text or text in d["path"].lower():
            return True, d["aces"]
        aces = [a for a in d["aces"] if text in a["a"].lower()]
        return bool(aces), aces

    def _acl_insert(self, d):
        show, aces = self._acl_matches(d)
        if not show:
            return
        tree = self.acl_tree
        kind = "File" if d.get("file") else "Folder"
        if d["k"] == "error":
            tree.insert("", "end", text=d["path"], tags=("folder", "missing"),
                        values=("", "", "", "", f"{kind} ERROR: {d['error']}"))
            return
        missing = acl_check.missing_protected(d)
        own = sum(1 for a in d["aces"] if not a["i"])
        note = (f"{kind},  " + ("inheritance BROKEN" if d["broken"] else "inherits from parent")
                + f",  owner {d['owner']},  {len(d['aces'])} entries ({own} own)"
                + (f",  missing protected: {', '.join(missing)}" if missing else ""))
        node = tree.insert("", "end", text=d["path"], open=len(self.acl_items) <= 20,
                           tags=("folder", "missing") if missing else ("folder",), values=("", "", "", "", note))
        for a in aces:
            tags = ("deny",) if a["t"] == "Deny" else ("inherited",) if a["i"] else ("own",)
            tree.insert(node, "end", text=a["a"], tags=tags,
                        values=(acl_check.rights(a), a["t"], "yes" if a["i"] else "no",
                                acl_check.applies_to(a, d.get("file")),
                                "protected account" if acl_check.protected(a["a"]) else ""))

    def _render_acl(self):
        self.acl_tree.delete(*self.acl_tree.get_children())
        for d in self.acl_items + self.acl_errors:
            self._acl_insert(d)
        for r in self.acl_applied:
            self._acl_apply_insert(r)
        if self.acl_proc or not self.acl_counts:
            return
        n = self.acl_counts
        self.acl_info.configure(
            text=f"{n['folders']:,} folder(s), {n['files']:,} file(s)"
                 + (f", {n['skipped']:,} more only inherit (not listed)" if n["skipped"] else "")
                 + f", {len(self.acl_errors):,} error(s),  {n['missing']:,} missing a protected account (red)."
                 + (f"  Showing the first {ACL_SHOW_MAX:,}; the Excel report has all." if n["hidden"] else "")
                 + "  Blue = own entry, grey = inherited.")

    def _acl_expand(self, on):
        for iid in self.acl_tree.get_children():
            self.acl_tree.item(iid, open=on)

    def _acl_export(self):
        if not self.acl_items and not self.acl_errors:
            messagebox.showerror(TITLE, "Click Read ACL first.")
            return
        if self.acl_counts.get("hidden") and not messagebox.askyesno(
                TITLE, f"Only the first {ACL_SHOW_MAX:,} items are kept on screen; "
                       f"{self.acl_counts['hidden']:,} more are not. Use 'Full report to Excel' for everything."
                       "\n\nExport the ones on screen anyway?"):
            return
        os.makedirs(LOG_DIR, exist_ok=True)
        path = filedialog.asksaveasfilename(
            parent=self, title="Save ACL report", defaultextension=".xlsx",
            filetypes=[("Excel workbook", "*.xlsx")], initialdir=LOG_DIR,
            initialfile=f"acl_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx")
        if not path:
            return
        try:
            with acl_check.XlsxReport(path, self.acl_root_path) as report:
                for d in self.acl_items + self.acl_errors:
                    report.write(d)
        except Exception as e:
            messagebox.showerror(TITLE, f"Cannot write {path}:\n\n{e}")
            return
        messagebox.showinfo(TITLE, f"Saved {len(self.acl_items):,} item(s) to\n{path}")

    def _acl_apply(self):
        """Put an ACL report (.xlsx of this tab, edited in Excel if needed) back on the folders."""
        if self.acl_proc:
            return
        xlsx = filedialog.askopenfilename(parent=self, title="ACL report to apply to the folders",
                                          initialdir=LOG_DIR if os.path.isdir(LOG_DIR) else None,
                                          filetypes=[("Excel workbook", "*.xlsx *.xlsm")])
        if not xlsx:
            return
        try:
            root, items, problems = acl_check.read_report(xlsx)
        except Exception as e:
            messagebox.showerror(TITLE, f"Cannot read {xlsx}:\n\n{e}")
            return
        if root and items and not root.endswith("listed path(s)") and not messagebox.askyesno(
                TITLE, f"The report was taken from\n{root}\n\nApply it to the same folders?\n\n"
                       "No = pick another root folder (e.g. the migrated copy); the report's paths are moved under it."):
            target = filedialog.askdirectory(parent=self, title=f"Apply the report of {root} to")
            if not target:
                return
            items, moved_out = acl_check.rebase(items, root, os.path.normpath(target))
            problems += moved_out
        if not items:
            messagebox.showerror(TITLE, "Nothing to apply.\n\n" + "\n".join(problems[:10]))
            return
        skipped = f"\n\n{len(problems):,} path(s) are skipped (bad rows) - listed in red." if problems else ""
        dry = messagebox.askyesnocancel(
            TITLE, f"{len(items):,} path(s) in {os.path.basename(xlsx)}.{skipped}\n\n"
                   "Every path gets exactly the entries listed for it: entries not in the report are removed, "
                   "missing ones added, inheritance broken / restored to match 'Inheritance broken'. Rows with "
                   "Inherited = yes come from the parent and are not written (unless inheritance is broken). "
                   "Owner and paths not in the report are not touched.\n\n"
                   "Yes = preview only (nothing is changed)\nNo = apply now\nCancel = do nothing")
        if dry is None:
            return
        if not dry and not messagebox.askyesno(
                TITLE, f"REPLACE the permissions of {len(items):,} path(s) now?\n\n"
                       "The log keeps each changed path's previous permissions (SDDL).", icon="warning"):
            return
        os.makedirs(LOG_DIR, exist_ok=True)
        log_path = os.path.join(LOG_DIR, f"acl_{'preview' if dry else 'apply'}_{datetime.now():%Y%m%d_%H%M%S}.csv")
        try:
            log = open(log_path, "w", encoding="utf-8-sig", newline="")
        except OSError as e:
            messagebox.showerror(TITLE, f"Cannot write {log_path}:\n\n{e}")
            return
        writer = csv.writer(log)
        writer.writerow(["Path", "Status", "Changes", "Before (SDDL)"])
        problem_items = [{"k": "error", "file": False, "path": p.split(": ", 1)[0], "error": p.split(": ", 1)[-1]}
                         for p in problems]
        for d in problem_items:
            writer.writerow([d["path"], "ERROR", d["error"], ""])

        self.acl_items, self.acl_errors, self.acl_counts, self.acl_report = [], [], {}, None
        self.acl_applied = list(problem_items)
        self.acl_apply_dry, self.acl_apply_log = dry, log_path
        self.acl_apply_counts = {"change": 0, "same": 0, "error": len(problems), "hidden": 0}
        self._render_acl()
        for b in (self.acl_read_btn, self.acl_full_btn, self.acl_apply_btn, self.acl_backup_btn):
            b.configure(state="disabled")
        self.acl_stop_btn.configure(state="normal")
        self.acl_info.configure(text=f"{'Previewing' if dry else 'Applying'} {os.path.basename(xlsx)}...")
        self.status.configure(text="Applying ACLs..." if not dry else "Previewing ACL changes...")
        try:
            self.acl_proc = proc = acl_check.apply_start(items, dry)
        except OSError as e:
            log.close()
            self.acl_proc = None
            self._acl_apply_end(f"Cannot start Windows PowerShell: {e}")
            return

        def work():
            batch, other = [], []
            try:
                for raw in proc.stdout:
                    item = acl_check.parse_line(raw)
                    if item is None:
                        if raw.strip():
                            other.append(raw.decode("utf-8", errors="replace").strip())
                        continue
                    if item["k"] == "item" and item["changes"]:
                        writer.writerow([item["path"], "would change" if dry else "changed",
                                         "\n".join(acl_check.change_text(c, item["file"]) for c in item["changes"]),
                                         item["before"]])
                    elif item["k"] == "error":
                        writer.writerow([item["path"], "ERROR", item["error"], ""])
                    if item["k"] != "done":
                        batch.append(item)
                    if len(batch) >= 200 or item["k"] == "done":
                        self.queue.put(("acl_apply", batch))
                        batch = []
            finally:
                log.close()
            if batch:
                self.queue.put(("acl_apply", batch))
            proc.wait()
            self.queue.put(("acl_apply_end", "\n".join(other[-5:])))

        threading.Thread(target=work, daemon=True).start()

    def _acl_apply_add(self, batch):
        n = self.acl_apply_counts
        for item in batch:
            if item["k"] == "item" and not item["changes"]:
                n["same"] += 1
                continue
            n["error" if item["k"] == "error" else "change"] += 1
            if len(self.acl_applied) < ACL_SHOW_MAX:
                self.acl_applied.append(item)
                self._acl_apply_insert(item)
            else:
                n["hidden"] += 1
        self.acl_info.configure(text=f"{'Previewing' if self.acl_apply_dry else 'Applying'}... "
                                     f"{n['change']:,} path(s) {'to change' if self.acl_apply_dry else 'changed'}, "
                                     f"{n['same']:,} unchanged, {n['error']:,} error(s).")

    def _acl_apply_insert(self, d):
        """A changed / failed path of the last apply, its changes below it (blue = added, red = removed)."""
        text = self.acl_filter.get().strip().lower()
        changes = d.get("changes") or []
        if text and text not in d["path"].lower() and not any(text in c.get("a", "").lower() for c in changes):
            return
        tree = self.acl_tree
        if d["k"] == "error":
            tree.insert("", "end", text=d["path"], tags=("folder", "missing"),
                        values=("", "", "", "", f"ERROR: {d['error']}"))
            return
        will = "will be " if self.acl_apply_dry else ""
        node = tree.insert("", "end", text=d["path"], open=len(self.acl_applied) <= 20, tags=("folder",),
                           values=("", "", "", "", f"{len(changes)} change(s) - {'preview' if self.acl_apply_dry else 'done'}"))
        for c in changes:
            if c["op"] in ("break", "inherit"):
                tree.insert(node, "end", text="(inheritance)", tags=("own",),
                            values=("", "", "", "", acl_check.change_text(c, d["file"])))
                continue
            added = c["op"] == "+"
            tree.insert(node, "end", text=c["a"], tags=("own",) if added else ("deny",),
                        values=(acl_check.rights(c), c["t"], "no", acl_check.applies_to(c, d["file"]),
                                f"{will}{'added' if added else 'removed'}"))

    def _acl_apply_end(self, other):
        stopped = self.acl_proc is not None and self.acl_proc.returncode not in (0, None)
        self.acl_proc = None
        for b in (self.acl_read_btn, self.acl_full_btn, self.acl_apply_btn, self.acl_backup_btn):
            b.configure(state="normal")
        self.acl_stop_btn.configure(state="disabled")
        if not self.running:
            self.status.configure(text=f"psql: {find_psql()}")
        n, dry = self.acl_apply_counts, self.acl_apply_dry
        done = (f"{'Preview' if dry else 'Applied'}{' - STOPPED' if stopped else ''}: "
                f"{n['change']:,} path(s) {'would change' if dry else 'changed'}, {n['same']:,} unchanged, "
                f"{n['error']:,} error(s)."
                + (f"  Showing the first {ACL_SHOW_MAX:,}; the log has all." if n["hidden"] else "")
                + ("  Nothing was changed on disk." if dry else "")
                + "  Blue = added, red = removed.")
        self.acl_info.configure(text=done)
        if other and not n["change"] and not n["same"]:
            messagebox.showerror(TITLE, f"Applying the ACL report failed:\n\n{other}")
        elif messagebox.askyesno(TITLE, f"{done}\n\nLog:\n{self.acl_apply_log}\n\nOpen the log now?"):
            os.startfile(self.acl_apply_log)

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
        v["staging"] = self.copy_mode.get() == "staging"
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
        for b in self.job_btns:
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
        if not messagebox.askyesno(TITLE, "Remove the links to the source databases (or, for 'Through this PC', "
                                          "the staging copies of the sources)?\n\n"
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
                elif kind == "compare":
                    if not self.running:
                        self.status.configure(text=f"psql: {find_psql()}")
                    self._show_compare(*data)
                elif kind == "acl":
                    self._acl_add(data)
                elif kind == "acl_end":
                    self._acl_end(data)
                elif kind == "acl_apply":
                    self._acl_apply_add(data)
                elif kind == "acl_apply_end":
                    self._acl_apply_end(data)
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
                    for b in self.job_btns:
                        b.configure(state="normal")
                    self.status.configure(text="Finished OK" if data == "ok" else "FAILED - see log")
                    after, self.after_done = self.after_done, None
                    if data == "ok":
                        self.checked.clear()
                        if after:
                            after()
                    self._refresh(quiet=True)
        except queue.Empty:
            pass
        self.after(100, self._poll)


if __name__ == "__main__":
    App().mainloop()
