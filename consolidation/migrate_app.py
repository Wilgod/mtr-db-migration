r"""
MTR DB Migration — desktop runner for the consolidation, packaged as a single
exe by build_exe.ps1. Two steps:

  Before  "Load table list" reads the tables of every source; untick the ones to
          skip (kept as skip_tables in sources.json, see generate.py).
  Step 1  Prepare target: create the database and the tables (known MTR tables
          plus every other source table not skipped), link the sources, preflight
          report, ID ranges, Sites, indexes/keys/triggers. No rows are copied.
  Step 2  Copy data: pick tables and source databases and copy their rows. Repeatable,
          so the sources can go one at a time (plan_copy has the rules); ticking what
          is already copied reloads it. The list shows the disk space the ticked tables
          should need and what the server has free (estimate_space, Runner.free_space).
          "Build Metadata Matrix" then makes MetadataMatrix: one row
          per file, one column per distinct MetadataKey (Metadata unchanged).
          Double-clicking a table lists its columns and indexes; tick columns there
          to add an index on them.

The "Compare databases" tab lists the tables and columns that differ between
two databases on the target server (e.g. cwp_ecr and cwp) and can add the
missing ones to the second so it matches the first (schema_compare.py).

The "ACL permissions" tab reads the NTFS permissions (Get-Acl, as permission.ps1
does) of a folder path, its subfolders and files, marks permission.ps1's protected
accounts and the ones missing; "Full report to Excel" writes every entry of every
subfolder and file to .xlsx (acl_check.py). Reading changes nothing; "Apply Excel to
folders" puts such a report (edited in Excel if needed) back: every path in it gets
exactly the entries listed for it (Preview first shows what would change).
"Backup (.acl.bak) to Excel" writes the same report from the chosen ACL backups of permission.ps1
(the permissions as they were before it ran), so Apply can also restore them.

The "Upload metadata" tab loads a folder of upload files (.xlsx / .csv) (build_upload_csv.py output, e.g.
compareDanielUpload) into a new wide "Metadata_Extend" table (one column per CSV header + "Source" =
the CSV's path); the original Metadata table is not touched. "Metadata_Extend_Upload" records
each CSV (done / error / stopped), so a run skips what is done and resumes at the file that
failed (metadata_upload.py).

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
import time
import tkinter as tk
import zlib
from datetime import datetime
from tkinter import filedialog, messagebox, scrolledtext, ttk

import acl_check
import generate
import metadata_upload
import schema_compare
import upload_prep

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
# metadata_horizontal\config.json: its columns are the header of the upload CSVs ("Build upload files")
MH_CONFIGS = [os.path.join(APP_DIR, "config.json"),
              os.path.join(APP_DIR, "..", "metadata_horizontal", "config.json"),
              os.path.join(APP_DIR, "..", "..", "metadata_horizontal", "config.json"),
              os.path.join(BUNDLE, "metadata_horizontal", "config.json")]

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
  -- tables and views first: their identity / serial sequences go with them
  FOR st IN SELECT format('DROP %s IF EXISTS %I.%I CASCADE',
                          CASE c.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                                         WHEN 'f' THEN 'FOREIGN TABLE' ELSE 'TABLE' END,
                          n.nspname, c.relname)
              FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE c.relnamespace <> ALL (sys_ns) AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
               AND c.oid <> ALL (ext_objs) LOOP
    EXECUTE st;
  END LOOP;
  -- then the sequences left over; an identity column's sequence can't be dropped on its own
  FOR st IN SELECT format('DROP SEQUENCE IF EXISTS %I.%I CASCADE', n.nspname, c.relname)
              FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE c.relnamespace <> ALL (sys_ns) AND c.relkind = 'S' AND c.oid <> ALL (ext_objs)
               AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass
                               AND d.objid = c.oid AND d.deptype = 'i') LOOP
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
       COALESCE(to_char(l.loaded_at, 'YYYY-MM-DD HH24:MI'), ''), {loaded_from}
FROM _merge.table_plan p LEFT JOIN _merge.loaded_table l USING (table_name)
ORDER BY p.load_order"""
LOADED_FROM = ("COALESCE((SELECT string_agg(x.source_db, ', ') FROM _merge.loaded_source x "
               "WHERE x.table_name = p.table_name), '')")
# A target from before _merge.loaded_source: step 2 only ever copied a table from all its sources.
LOADED_FROM_OLD = "CASE WHEN l.loaded_at IS NULL THEN '' ELSE p.sources END"
HAS_LOADED_SOURCE = "SELECT to_regclass('_merge.loaded_source') IS NOT NULL"
ADD_LOADED_SOURCE_SQL = generate.LOADED_SOURCE_DDL + """INSERT INTO _merge.loaded_source (table_name, source_db)
SELECT p.table_name, x FROM _merge.loaded_table l JOIN _merge.table_plan p USING (table_name),
     unnest(string_to_array(p.sources, ', ')) x;
"""
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
# Indexes of public.<t>; "cols" (key columns, in order) only for a plain btree index, the kind
# the columns window creates, so it can tell when the same index already exists.
INDEXES_QUERY = """SELECT COALESCE(json_agg(json_build_object(
    'name', c.relname, 'def', pg_get_indexdef(i.indexrelid),
    'cols', CASE WHEN i.indpred IS NULL AND i.indexprs IS NULL AND am.amname = 'btree' THEN
        (SELECT json_agg(a.attname::text ORDER BY k.ord)
           FROM unnest(i.indkey::int2[]) WITH ORDINALITY k(attnum, ord)
           JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
          WHERE k.ord <= i.indnkeyatts) END) ORDER BY c.relname), '[]')
FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_am am ON am.oid = c.relam
WHERE i.indrelid = to_regclass(format('public.%I', '{t}'))"""
# Tables of one source; reltuples is the server's estimate, so no table is scanned (-1 = unknown).
SOURCE_TABLES_QUERY = """SELECT c.relname, c.reltuples::bigint FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition"""
# Disk used per table in a source, indexes and TOAST included (a partitioned parent holds nothing itself).
SOURCE_SIZES_QUERY = """SELECT c.relname, COALESCE(CASE c.relkind WHEN 'p' THEN
    (SELECT SUM(pg_total_relation_size(relid))::bigint FROM pg_partition_tree(c.oid))
    ELSE pg_total_relation_size(c.oid) END, 0)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition"""
# data_directory is empty for a login that may not read it (reading it anyway is an error).
DISK_INFO_QUERY = """SELECT pg_size_bytes(current_setting('max_wal_size')),
    (SELECT count(*) FROM pg_replication_slots WHERE NOT active),
    CASE WHEN pg_has_role(current_user, 'pg_read_all_settings', 'USAGE') THEN current_setting('data_directory') ELSE '' END,
    pg_tablespace_location(dattablespace)
FROM pg_database WHERE datname = current_database()"""
SPACE_MARGIN = 0.25  # the target's indexes differ from the source's: 17% more in a test copy of Files
# How many source links (direct) / staging copies (through this PC) are still there.
LINKS_DIRECT = "SELECT count(*) FROM pg_foreign_server WHERE srvname LIKE 'srv\\_%'"
LINKS_STAGING = "SELECT count(*) FROM _merge.stage WHERE to_regclass(format('public.%I', stage_table)) IS NOT NULL"
# Which layout an existing target was built with.
LAYOUT_QUERY = ("SELECT CASE WHEN to_regclass('public._merge_source') IS NOT NULL THEN 'staging' "
                "WHEN to_regclass('_merge.source') IS NOT NULL THEN 'direct' ELSE '' END")


def restore_space_sql(tables):
    """Bytes the skipped duplicates will take once restored: for each table, its size now times
    (skipped parent rows from the sources it was copied from / parent rows loaded), plus a
    rewrite of the conflict-log rows that get marked."""
    parent = {t: generate.SKIP_CASCADE[t][0][0] for t in tables}
    parts = [f"SELECT pg_total_relation_size('public.{generate.q(t)}')::numeric\n"
             f"     * (SELECT count(*) FROM _merge.conflict_log c JOIN _merge.loaded_source l\n"
             f"          ON l.table_name = {generate.lit(t)} AND l.source_db = c.source_db\n"
             f"        WHERE c.action = 'skipped' AND c.table_name = {generate.lit(p)})\n"
             f"     / GREATEST((SELECT reltuples FROM pg_class WHERE oid = 'public.{generate.q(p)}'::regclass), 1) AS b"
             for t, p in parent.items()]
    parts.append("SELECT pg_total_relation_size('_merge.conflict_log')::numeric\n"
                 "     * (SELECT count(*) FROM _merge.conflict_log WHERE action = 'skipped')\n"
                 "     / GREATEST((SELECT count(*) FROM _merge.conflict_log), 1)")
    return "SELECT COALESCE(sum(b), 0)::bigint FROM (\n" + "\nUNION ALL\n".join(parts) + "\n) x"


def find_psql():
    bundled = os.path.join(RES_PGBIN, "psql.exe")
    if os.path.exists(bundled):
        return bundled
    return shutil.which("psql") or r"C:\Program Files\PostgreSQL\18\bin\psql.exe"


def index_name(table, cols, taken=()):
    """IX_<table>_<col>[_<col>...] as 04_finalize.sql names them, within PostgreSQL's 63 bytes
    (a longer one gets a hash, or the server would cut it) and not one of the `taken` names."""
    def cut(text, tail):
        return text.encode("utf-8")[:63 - len(tail)].decode("utf-8", "ignore") + tail
    base = f"IX_{table}_" + "_".join(cols)
    if len(base.encode("utf-8")) > 63:
        base = cut(base, f"_{zlib.crc32(base.encode('utf-8')):08x}")
    name, n = base, 2
    while name in taken:
        name, n = cut(base, f"_{n}"), n + 1
    return name


def tick_tables(kept, table, tick, present):
    """`kept` after (un)ticking `table`: the tables it needs come along when ticked, and
    the tables needing it go along when unticked (generate.DEPENDS)."""
    needs = {t: [p for p in generate.DEPENDS.get(t, []) if p in present] for t in present}
    todo, change = [table], set()
    while todo:
        t = todo.pop()
        if t not in change:
            change.add(t)
            todo += needs[t] if tick else [c for c in present if t in needs[c]]
    return set(kept) | change if tick else set(kept) - change


def fmt_size(n):
    """1536 -> '1.5 KB'."""
    n = float(n)
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def parse_df(text):
    """The available bytes of each disk in `df -Pk` output."""
    free = []
    for line in text.splitlines():
        f = line.split()
        # from the end: a file system name can hold spaces. ... blocks used available use% mount
        for i in range(len(f) - 1, 2, -1):
            if f[i].endswith("%") and f[i - 1].isdigit():
                free.append(int(f[i - 1]) * 1024)
                break
    return free


def estimate_space(copies, sizes, wal):
    """Rough disk need of `copies` {table: [source]} (plan_copy), from `sizes` {source: {table: bytes}}:
    the size of those tables in the sources, a margin, and room for WAL (`wal`: max_wal_size).
    A reloaded table's old rows are not counted as free: they stay on disk until the first commit.
    Returns {"data", "margin", "wal", "need", "unknown": [(table, source)] with no size}."""
    data, unknown = 0, []
    for t, srcs in copies.items():
        for s in srcs:
            size = sizes.get(s, {}).get(t)
            if size is None:
                unknown.append((t, s))
            else:
                data += size
    margin = int(data * SPACE_MARGIN)
    return {"data": data, "margin": margin, "wal": wal, "need": data + margin + wal, "unknown": unknown}


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

    def source_tables(self, sources):
        """({table: {"sources": [name], "rows": estimate, "unknown": any source without one}},
        how many tables are never migrated (bak_*)). Fails if a source cannot be read:
        a list missing that source's tables would look complete."""
        tables, never = {}, set()
        for s in sources:
            self.out.put(("step", f"reading the tables of {s['dbname']}"))
            cmd, env = self.source_cmd(s)
            r = subprocess.run(cmd + ["-X", "-w", "-q", "-tA", "-F", "\t", "-v", "ON_ERROR_STOP=1",
                                      "-c", SOURCE_TABLES_QUERY],
                               env=env, creationflags=NO_WINDOW, stdin=subprocess.DEVNULL, capture_output=True)
            if r.returncode != 0:
                raise StepFailed(f"{s['dbname']}: " + r.stderr.decode("utf-8", errors="replace").strip())
            for line in r.stdout.decode("utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                name, rows = line.rstrip("\r").split("\t")
                if generate.EXCLUDED.match(name):
                    never.add(name)
                    continue
                t = tables.setdefault(name, {"sources": [], "rows": 0, "unknown": False})
                t["sources"].append(s["name"])
                if int(rows) < 0:
                    t["unknown"] = True
                else:
                    t["rows"] += int(rows)
        return tables, len(never)

    def source_sizes(self, sources):
        """({source name: {table: bytes}}, {source name: why it could not be read})."""
        sizes, errors = {}, {}
        for s in sources:
            cmd, env = self.source_cmd(s)
            try:
                r = subprocess.run(cmd + ["-X", "-w", "-q", "-tA", "-F", "\t", "-v", "ON_ERROR_STOP=1",
                                          "-c", SOURCE_SIZES_QUERY],
                                   env=env, creationflags=NO_WINDOW, stdin=subprocess.DEVNULL, capture_output=True)
            except OSError as e:
                errors[s["name"]] = str(e)
                continue
            if r.returncode != 0:
                errors[s["name"]] = r.stderr.decode("utf-8", errors="replace").strip()
                continue
            sizes[s["name"]] = {}
            for line in r.stdout.decode("utf-8", errors="replace").splitlines():
                if line.strip():
                    name, size = line.rstrip("\r").split("\t")
                    sizes[s["name"]][name] = int(size)
        return sizes, errors

    def free_space(self, target):
        """{"free": bytes on the fullest of the target's data and WAL disks, or None with "why",
        "how", "wal": max_wal_size, "idle_slots": inactive replication slots}. PostgreSQL has no SQL
        for this, so it asks the disk: from this PC when the server is here, through docker, else
        by running one read-only command on the server (needs superuser or pg_execute_server_program)."""
        info = {"free": None, "how": "", "why": "", "wal": 1 << 30, "idle_slots": 0}
        try:
            wal, idle, data_dir, space = self.psql_scalar(target, DISK_INFO_QUERY, extra=("-F", "\t")).split("\t")
        except (StepFailed, OSError, ValueError) as e:
            info["why"] = str(e).splitlines()[0] if str(e) else "could not ask the server"
            return info
        info["wal"], info["idle_slots"] = int(wal), int(idle)
        if not data_dir:
            info["why"] = "this login may not read the server's data folder (needs pg_read_all_settings)"
            return info
        paths = [space or data_dir, data_dir.rstrip("/\\") + "/pg_wal"]
        c = self.cfg
        try:
            if c["docker"]:
                r = subprocess.run(["docker", "exec", c["docker"], "df", "-Pk", *paths], creationflags=NO_WINDOW,
                                   stdin=subprocess.DEVNULL, capture_output=True)
                if r.returncode != 0:
                    raise StepFailed(r.stderr.decode("utf-8", errors="replace").strip())
                free, how = parse_df(r.stdout.decode("utf-8", errors="replace")), "df in the docker container"
            elif c["host"] in generate.LOCAL_HOSTS and all(os.path.exists(p) for p in paths):
                free, how = [shutil.disk_usage(p).free for p in paths], "this PC's disk"
            else:
                free, how = self._server_free(target, paths), "a read-only command on the server"
        except (StepFailed, OSError, ValueError) as e:
            info["why"] = str(e).splitlines()[0] if str(e) else "could not read the disk"
            return info
        if not free:
            info["why"] = "the server's disk command gave no answer"
            return info
        info["free"], info["how"] = min(free), how
        return info

    def _server_free(self, target, paths):
        """Free bytes per disk, by COPY ... FROM PROGRAM into a temporary table that is dropped again."""
        if paths[0][1:2] == ":":  # a Windows server
            drives = ",".join(sorted({f"'{p[0]}'" for p in paths}))
            program = (f'powershell -NoProfile -Command "{drives} | ForEach-Object '
                       '{ ([IO.DriveInfo]$_).AvailableFreeSpace }"')
            parse = lambda text: [int(x) for x in text.split()]  # noqa: E731
        else:
            program = "df -Pk " + " ".join("'" + p.replace("'", "'\\''") + "'" for p in paths)
            parse = parse_df
        sql = program.replace("'", "''")
        out = self.psql_scalar(target, "BEGIN; CREATE TEMP TABLE _disk_free (line text) ON COMMIT DROP; "
                                       f"COPY _disk_free FROM PROGRAM '{sql}'; "
                                       "SELECT line FROM _disk_free; COMMIT;", extra=("-q",))
        return parse(out)

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

    def stage_data(self, g, target, only=None):
        """Through this PC: copy every source table (or `only` these staging tables) into its
        staging table (generate.copy_to_stage)."""
        def progress(i, n, s, t):
            self.out.put(("step", f"copy to staging {i}/{n}: {s['name']}.{t}"))
        try:
            counts = generate.copy_to_stage(g, self.source_cmd, lambda: (self._cmd(target, []), self.env),
                                            log=self.log, progress=progress, only=only)
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
        try:
            # a read-only report: a failure (e.g. no room for its temp files) must not stop step 1
            self.psql_run(target, "preflight", file=self.sql("02_preflight.sql"))
        except StepFailed as e:
            self.log(f"WARNING: preflight report failed ({e}); continuing. Check free disk space on the target.")
        self.section("prepare")
        self.psql_run(target, "prepare", file=self.sql("03_prepare.sql"))
        self.section("finalize")
        self.psql_run(target, "finalize", file=self.sql("04_finalize.sql"))
        self.log("\nStep 1 done: the tables exist (no rows yet). Pick tables in step 2 to copy their data.")

    def _checked_target(self):
        """(Gen, staging?) for a target step 1 prepared, with its sources still reachable and
        still matching what step 1 saw; fails with what to do otherwise."""
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
        return g, staging

    def _sequences_and_verify(self, target, g, staging, holds):
        """`holds`: {table: sources} to reconcile against the sources."""
        self.section("sequences")
        self.psql_run(target, "sequences", file=self.sql("04b_sequences.sql"))
        self._verify(target, g, staging, holds)

    def recheck(self):
        """The end of step 2 again, for all that is copied: reset the sequences, reconcile every
        copied table against its sources. For when a copy committed but this part did not finish.
        No rows are copied or changed."""
        target = self.cfg["target"]
        g, staging = self._checked_target()
        holds = {r[0]: [x for x in r[7].split(", ") if x] for r in self.plan()[1]}
        holds = {t: v for t, v in holds.items() if v}
        if not holds:
            raise StepFailed("Nothing has been copied yet, so there is nothing to re-check.")
        if not staging:
            self._sequences_and_verify(target, g, staging, holds)
        else:
            # staging copies are emptied after each copy: fill, check and empty them one source at a time
            self.section("sequences")
            self.psql_run(target, "sequences", file=self.sql("04b_sequences.sql"))
            present = set(self.psql_scalar(target, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                          .splitlines())
            for s in g.sources:
                tables = [t for t, srcs in holds.items() if s["name"] in srcs]
                names = {generate.stage_table(s["name"], t) for t in tables} & present
                if not names:
                    continue
                self._fill_stage(g, target, names)
                self._verify(target, g, staging, {t: [s["name"]] for t in tables}, f"verify {s['name']}")
                self._free_stage(target, names)
        self.log(f"\nRe-check done: sequences reset, {len(holds)} table(s) match their sources.")

    def step2(self, copies, reload, holds):
        """Copy the rows in `copies` {table: sources}; `reload` are emptied first. `holds` is
        {table: sources} once done, what verify checks (plan_copy returns all three)."""
        target = self.cfg["target"]
        g, staging = self._checked_target()
        if self.psql_scalar(target, self.m(HAS_LOADED_SOURCE, staging)) != "t":
            path = self.sql(f"add_loaded_source_{self.stamp}.sql")
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(self.m(ADD_LOADED_SOURCE_SQL, staging))
            self.psql_run(target, "add_loaded_source", file=path, extra_args=["-q", "--single-transaction"])
        present = set(self.psql_scalar(target, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                      .splitlines()) if staging else set()
        batches = g.load_batches(copies, set(reload))
        done = []
        for i, (source, sql) in enumerate(batches, 1):
            tables = [t for t, srcs in copies.items() if source in srcs]
            # Through this PC: one source's staging copies at a time. They are filled (again) just
            # before its copy and emptied once it is committed and verified, so the disk only ever
            # holds the target plus the staging copies of the source being copied.
            names = {generate.stage_table(source, t) for t in tables} & present
            try:
                if names:
                    self._fill_stage(g, target, names)
            except StepFailed as e:
                self._after_failed_copy(target, done)
                raise StepFailed(f"{e}\n\nCould not copy {source} to staging; nothing from {source} was copied. "
                                 + (f"Committed and kept: {', '.join(done)}. " if done else "")
                                 + "Fix the problem, then click Tick not loaded and Copy to carry on from there.")
            load_file = self.sql(f"load_{self.stamp}_{i}_{source}.sql")
            with open(load_file, "w", encoding="utf-8", newline="\n") as f:
                f.write(self.m(sql, staging))
            self.section(f"copy data {i}/{len(batches)}: {source}")
            try:
                self.psql_run(target, f"load_{i}_{source}", file=load_file)
            except StepFailed as e:
                self._after_failed_copy(target, done)
                raise StepFailed(f"{e}\n\nCopying from {source} failed; nothing from {source} was kept. "
                                 + (f"Committed and kept: {', '.join(done)}. " if done else "")
                                 + "Fix the problem, then click Tick not loaded and Copy to carry on from there.")
            done.append(source)
            self.log(f"{source}: committed ({i}/{len(batches)})")
            if names:
                try:
                    self._verify(target, g, staging, {t: [source] for t in tables}, f"verify {source}")
                except StepFailed as e:
                    self._after_failed_copy(target, done)
                    raise StepFailed(f"{e}\n\nThe rows of {source} are copied and committed, but they do not match "
                                     f"its staging copies (kept for checking). Copies after {source} were not started. "
                                     "Fix the problem, then click Re-check. Do not copy these tables again.")
                self._free_stage(target, names)
        try:
            if staging:
                # each source was verified right after its copy, before its staging copies were emptied
                self.section("sequences")
                self.psql_run(target, "sequences", file=self.sql("04b_sequences.sql"))
            else:
                self._sequences_and_verify(target, g, staging, holds)
        except StepFailed as e:
            raise StepFailed(f"{e}\n\nThe rows are copied and committed; this last part did not finish. "
                             "Fix the problem, then click Re-check. Do not copy these tables again.")
        self.log(f"\nStep 2 done: {len(copies)} table(s) copied and verified.")

    def _fill_stage(self, g, target, names):
        """Fill the staging tables `names` (all existing) in full where they are empty (emptied
        after their copy, or by a crash) or hold only skipped duplicates. Full ones are kept."""
        emptied = self._emptied_stage(target, names)
        partial = self._partial_stage(target, names) - emptied
        if not (emptied or partial):
            return
        self.section("copy to staging (through this PC)")
        if partial:
            self.log("These staging copies hold only the skipped duplicates (from Insert skipped "
                     "duplicates), so they are copied again in full:\n  " + ", ".join(sorted(partial)))
        self._refill_stage(g, target, emptied | partial)

    def _free_stage(self, target, names):
        """Empty the staging tables `names` to give their disk space back. _merge.stage keeps
        their row counts, so _emptied_stage sees them as emptied and the next use fills them again."""
        self.psql_run(target, "free_staging", command="TRUNCATE " + ", ".join(
            f"public.{generate.q(n)}" for n in sorted(names)))
        self.log(f"staging copies emptied to free disk space: {len(names)} table(s)")

    def _verify(self, target, g, staging, holds, label="verify"):
        verify_file = self.sql(f"{label.replace(' ', '_')}_{self.stamp}.sql")
        with open(verify_file, "w", encoding="utf-8", newline="\n") as f:
            f.write(self.m(g.verify(holds), staging))
        self.section(label)
        self.psql_run(target, label.replace(" ", "_"), file=verify_file)

    def _after_failed_copy(self, target, done):
        """The sources already committed stay, so their sequences must still be moved past
        their new IDs. A failure here (the server may be gone) must not hide the first error."""
        if not done:
            return
        self.section("sequences (for the databases already committed)")
        try:
            self.psql_run(target, "sequences", file=self.sql("04b_sequences.sql"))
        except StepFailed as e:
            self.log(f"Could not reset the sequences: {e}. The next successful copy resets them.")

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

    def _emptied_stage(self, target, names):
        """The staging tables in `names` (all existing) that step 1 filled but that are empty now.
        They are UNLOGGED, and PostgreSQL empties every unlogged table when the server restarts
        after a crash (e.g. a full disk), so their rows are gone without any error."""
        return self._stage_where(target, names, "g.row_count > 0 AND NOT EXISTS (SELECT 1 FROM public.{t})")

    def _partial_stage(self, target, names):
        """The staging tables in `names` that hold only the rows of skipped duplicates
        (copied by Insert skipped duplicates), not the whole source table."""
        return self._stage_where(target, names, "g.partial")

    def _stage_where(self, target, names, cond):
        if not names:
            return set()
        self._ensure_partial_flag(target)
        parts = [f"SELECT {generate.lit(n)} FROM _merge.stage g WHERE g.stage_table = {generate.lit(n)} AND "
                 + cond.format(t=generate.q(n)) for n in sorted(names)]
        return set(self.psql_scalar(target, self.m("\nUNION ALL\n".join(parts), True)).splitlines()) - {""}

    def _ensure_partial_flag(self, target):
        self.psql_scalar(target, self.m("ALTER TABLE _merge.stage ADD COLUMN IF NOT EXISTS partial boolean "
                                        "NOT NULL DEFAULT false", True))

    def _refill_stage(self, g, target, names):
        """Copy the staging tables `names` again from the sources, whole (emptied first)."""
        self.psql_run(target, "restage_truncate", command="TRUNCATE " + ", ".join(
            f"public.{generate.q(n)}" for n in sorted(names)))
        self.stage_data(g, target, only=set(names))
        self._ensure_partial_flag(target)
        self.psql_scalar(target, self.m("UPDATE _merge.stage SET partial = false WHERE stage_table = ANY (ARRAY["
                                        + ", ".join(generate.lit(n) for n in sorted(names)) + "])", True))

    def _skipped_ids(self, target, parent, source):
        """The old IDs (COPY text) of the rows of `parent` skipped in `source`."""
        return self.psql_scalar(target, self.m(
            f"COPY (SELECT DISTINCT old_id FROM _merge.conflict_log WHERE action = 'skipped' "
            f"AND table_name = {generate.lit(parent)} AND source_db = {generate.lit(source)} ORDER BY 1) TO STDOUT",
            True)).replace("\r\n", "\n")  # one line style: the ids are fed to COPY ... FROM STDIN

    def _copy_skipped_to_stage(self, g, target, pairs):
        """Fill the staging tables of `pairs` [(source, table)] with only the rows of the skipped
        duplicates (by the skipped Files / MigrationTasks IDs), and flag them partial."""
        names = {generate.stage_table(s["name"], t) for s, t in pairs}
        self.psql_run(target, "restage_truncate", command="TRUNCATE " + ", ".join(
            f"public.{generate.q(n)}" for n in sorted(names)))
        ids, items = {}, []
        for s, t in pairs:
            parent, col = generate.SKIP_CASCADE[t][0]
            if (s["name"], parent) not in ids:
                ids[(s["name"], parent)] = self._skipped_ids(target, parent, s["name"])
            items.append((s, t, col, ids[(s["name"], parent)]))

        def progress(i, n, s, t):
            self.out.put(("step", f"copy skipped rows to staging {i}/{n}: {s['name']}.{t}"))
        try:
            counts = generate.copy_ids_to_stage(g, self.source_cmd, lambda: (self._cmd(target, []), self.env),
                                                items, log=self.log, progress=progress)
        except RuntimeError as e:
            raise StepFailed(str(e))
        self._ensure_partial_flag(target)
        self.psql_run(target, "stage_partial", command=self.m(
            "UPDATE _merge.stage SET partial = true WHERE stage_table = ANY (ARRAY["
            + ", ".join(generate.lit(n) for n in sorted(names)) + "]);\n"
            "ANALYZE " + ", ".join(f"public.{generate.q(st)}" for st, _ in counts), True))

    def verify_counts(self):
        """Exact row count of every table in every source database (read live, so it works
        after Finish too) next to the rows in the target tagged with that source. Read-only.
        Writes the comparison to the log and to logs\\verify_counts_<time>.csv."""
        target = self.cfg["target"]
        layout = self.layout(target)
        if not layout:
            raise StepFailed(f"{target} is not prepared - run step 1 first.")
        staging = layout == "staging"
        g = self.generate(staging)
        tag = {t: g.generic[t]["tag"] if t in g.generic else generate.SOURCE_TAG[t]
               for t in generate.LOAD_ORDER + sorted(g.generic)}
        # --- target: rows per table and source tag
        self.section("count the target")
        have = set(self.psql_scalar(target, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'").splitlines())
        parts = [f"SELECT {generate.lit(t)}, COALESCE({generate.q(c)}::text, ''), count(*) FROM public.{generate.q(t)} GROUP BY 2"
                 for t, c in tag.items() if t in have]
        loaded = {}
        for line in self.psql_scalar(target, "\nUNION ALL\n".join(parts), extra=("-F", "\t")).splitlines():
            if line.strip():
                t, src, n = line.rstrip("\r").split("\t")
                loaded[(t, src)] = int(n)
        skipped = {}
        for line in self.psql_scalar(target, self.m(
                "SELECT table_name, source_db, count(*) FROM _merge.conflict_log WHERE action = 'skipped' GROUP BY 1, 2",
                staging), extra=("-F", "\t")).splitlines():
            if line.strip():
                t, src, n = line.rstrip("\r").split("\t")
                skipped[(t, src)] = int(n)
        copied = {r[0]: {x for x in r[7].split(", ") if x} for r in self.plan()[1]}
        # --- sources: exact counts, read live
        rows = []
        for i, s in enumerate(g.sources, 1):
            self.section(f"count source {i}/{len(g.sources)}: {s['dbname']}")
            self.out.put(("step", f"counting rows in {s['dbname']}"))
            tables = sorted(t for t in s["tables"] if not generate.EXCLUDED.match(t))
            if not tables:
                continue
            sql = "\nUNION ALL\n".join(f"SELECT {generate.lit(t)}, count(*) FROM public.{generate.q(t)}" for t in tables)
            cmd, env = self.source_cmd(s)
            r = subprocess.run(cmd + ["-X", "-w", "-q", "-tA", "-F", "\t", "-v", "ON_ERROR_STOP=1", "-c", sql],
                               env=env, creationflags=NO_WINDOW, stdin=subprocess.DEVNULL, capture_output=True)
            if r.returncode != 0:
                raise StepFailed(f"{s['dbname']}: " + r.stderr.decode("utf-8", errors="replace").strip())
            for line in r.stdout.decode("utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                t, n = line.rstrip("\r").split("\t")
                n = int(n)
                got = loaded.get((t, s["name"]), 0)
                skip = skipped.get((t, s["name"]), 0)
                if t in g.skip or t not in tag:
                    status = "NOT MIGRATED (skipped table)"
                elif s["name"] not in copied.get(t, ()):
                    status = "OK (empty)" if n == 0 and got == 0 else "NOT COPIED YET"
                elif got == n:
                    status = "OK"
                elif skip and got + skip == n:
                    status = f"OK except {skip:,} skipped duplicates"
                elif any(k[1] == s["name"] and v for k, v in skipped.items()):
                    status = "DIFFERENT (rows of skipped duplicate files?)"
                else:
                    status = "DIFFERENT"
                rows.append((t, s["name"], n, got, n - got, status))
        # rows the app wrote after the merge (no source tag)
        app_rows = {t: n for (t, src), n in loaded.items() if src == ""}
        # --- report
        self.section("row counts: source database vs migrated")
        head = ("table", "source_db", "source_rows", "migrated_rows", "difference", "status")
        w = [max(len(head[0]), *(len(r[0]) for r in rows)) if rows else 10,
             max(len(head[1]), *(len(r[1]) for r in rows)) if rows else 10, 12, 13, 10]
        self.log(f"{head[0]:<{w[0]}}  {head[1]:<{w[1]}}  {head[2]:>{w[2]}}  {head[3]:>{w[3]}}  {head[4]:>{w[4]}}  {head[5]}")
        self.log("-" * (sum(w) + 40))
        order = {"DIFFERENT": 0, "NOT COPIED YET": 1}
        rows.sort(key=lambda r: (min((v for k, v in order.items() if r[5].startswith(k)), default=2), r[0], r[1]))
        empty = sum(1 for r in rows if r[2] == 0 and r[3] == 0)
        for t, src, n, got, diff, status in rows:
            if n == 0 and got == 0:
                continue  # empty in both: only in the CSV
            self.log(f"{t:<{w[0]}}  {src:<{w[1]}}  {n:>{w[2]},}  {got:>{w[3]},}  {diff:>{w[4]},}  {status}")
        if empty:
            self.log(f"({empty} table/source pairs empty in both, not listed; they are in the CSV)")
        if app_rows:
            self.log("\nRows written by the app after the merge (no source_db, not compared): "
                     + ", ".join(f"{t} {n:,}" for t, n in sorted(app_rows.items())))
        path = os.path.join(LOG_DIR, f"verify_counts_{self.stamp}.csv")
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            wr = csv.writer(f)
            wr.writerow(head)
            wr.writerows(rows)
        bad = [r for r in rows if r[5].startswith("DIFFERENT")]
        todo = [r for r in rows if r[5] == "NOT COPIED YET"]
        ok = len(rows) - len(bad) - len(todo) - sum(1 for r in rows if r[5].startswith("NOT MIGRATED"))
        self.log(f"\n{ok} table/source pairs match, {len(bad)} different, {len(todo)} not copied yet. Saved: {path}")
        if bad:
            raise StepFailed(f"{len(bad)} table/source pair(s) have different row counts (listed first above).")

    def restore_skipped(self):
        """Insert the duplicate files the merge skipped (logged in _merge.conflict_log), with
        their Metadata, Permissions and MigrationTasks, under the IDs they would have had.
        Nothing already copied is deleted or changed. If Finish removed the source links /
        staging copies, only the tables needed are linked / copied again."""
        target = self.cfg["target"]
        layout = self.layout(target)
        if not layout:
            raise StepFailed(f"{target} is not prepared - run step 1 first.")
        staging = layout == "staging"
        g = self.generate(staging)
        self.section("check")
        db_sources = self.psql_scalar(target, self.m(
            "SELECT string_agg(source_db, ',' ORDER BY load_order) FROM _merge.source", staging))
        if db_sources != ",".join(s["name"] for s in g.sources):
            raise StepFailed(f"The source list is not the one this target was built with ({db_sources}). "
                             "Put the same databases, in the same order, in the list and try again.")
        skipped = self.psql_scalar(target, self.m(
            "SELECT count(*) FROM _merge.conflict_log WHERE action = 'skipped'", staging))
        if skipped == "0":
            self.log("Nothing to restore: no row is marked as a skipped duplicate.")
            return
        self.log(f"{int(skipped):,} skipped rows (files and their tasks) in the conflict log")
        if self.psql_scalar(target, self.m(HAS_LOADED_SOURCE, staging)) != "t":
            path = self.sql(f"add_loaded_source_{self.stamp}.sql")
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(self.m(ADD_LOADED_SOURCE_SQL, staging))
            self.psql_run(target, "add_loaded_source", file=path, extra_args=["-q", "--single-transaction"])
        relinked = False
        # (table, source) pairs that get rows back: copied from that source, and the source has skipped files
        loaded = {tuple(line.split("\t")) for line in self.psql_scalar(target, self.m(
            "SELECT table_name, source_db FROM _merge.loaded_source", staging), extra=("-F", "\t")).splitlines() if line}
        with_skips = set(self.psql_scalar(target, self.m(
            "SELECT DISTINCT source_db FROM _merge.conflict_log WHERE action = 'skipped'", staging)).splitlines())
        work = [(s, t) for s, t in g.restore_needs() if s["name"] in with_skips and (t, s["name"]) in loaded]
        to_copy, create = set(), set()
        if staging:
            names = sorted({generate.stage_table(s["name"], t) for s, t in g.restore_needs()})
            present = set(self.psql_scalar(target, "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                                                   "AND tablename = ANY (ARRAY[" + ", ".join(
                                                       generate.lit(n) for n in names) + "])").splitlines())
            create = set(names) - present  # the script reads every source's table: create them all, even empty
            needed = {generate.stage_table(s["name"], t) for s, t in work}
            to_copy = (needed - present) | self._emptied_stage(target, needed & present)
            if needed & present & to_copy:
                self.log("These staging copies are EMPTY although step 1 filled them: they are UNLOGGED tables, which "
                         "PostgreSQL empties when the server restarts after a crash (e.g. a full disk):\n  "
                         + ", ".join(sorted(needed & present & to_copy)))
            if to_copy:
                self.log("Only the rows of the skipped duplicates are copied to staging (not the whole source "
                         "tables). Step 2 copies such a table in full again before it uses it.")
        self.section("disk space")
        tables = [t for t in generate.RESTORE_TABLES if any(tt == t for _, tt in g.restore_needs())]
        # an earlier run that failed (e.g. disk full) left its rolled-back rows behind: give that space back
        self.psql_run(target, "vacuum", command=self.m(
            "VACUUM (ANALYZE) " + ", ".join([f"public.{generate.q(t)}" for t in tables] + ["_merge.conflict_log"]), staging))
        need = int(self.psql_scalar(target, self.m(restore_space_sql(tables), staging)) or 0)
        copy_size = 0
        if to_copy:
            sizes, errors = self.source_sizes([s for s in g.sources if any(
                generate.stage_table(s["name"], t) in to_copy for t in s["tables"])])
            if errors:
                raise StepFailed("Could not read the size of the source tables: " + "; ".join(
                    f"{k}: {v}" for k, v in errors.items()))
            # only the skipped share of each table is copied: size * skipped ids / rows step 1 copied
            full = {}
            for line in self.psql_scalar(target, self.m(
                    "SELECT stage_table, COALESCE(row_count, 0) FROM _merge.stage", True), extra=("-F", "\t")).splitlines():
                if line:
                    st, n = line.split("\t")
                    full[st] = int(n)
            skips = {}
            for line in self.psql_scalar(target, self.m(
                    "SELECT source_db, table_name, count(*) FROM _merge.conflict_log WHERE action = 'skipped' "
                    "GROUP BY 1, 2", True), extra=("-F", "\t")).splitlines():
                if line:
                    src, t, n = line.split("\t")
                    skips[(src, t)] = int(n)
            for s in g.sources:
                for t, n in sizes.get(s["name"], {}).items():
                    st = generate.stage_table(s["name"], t)
                    if st in to_copy:
                        part = skips.get((s["name"], generate.SKIP_CASCADE[t][0][0]), 0) / max(full.get(st, 0), 1)
                        copy_size += int(n * min(part, 1.0))
            self.log(f"copying the skipped rows of {len(to_copy)} source table(s) to staging needs about "
                     f"{fmt_size(copy_size)}")
        disk = self.free_space(target)
        need += copy_size
        need_all = need + disk["wal"]
        self.log(f"needs about {fmt_size(need)} for the new rows, their indexes, the conflict-log marks "
                 f"and the staging copies, plus up to {fmt_size(disk['wal'])} of WAL (max_wal_size)")
        if disk["free"] is None:
            self.log(f"free disk space unknown ({disk['why']}); make sure the server has at least "
                     f"{fmt_size(need_all * 1.2)} free")
        else:
            self.log(f"free on the server: {fmt_size(disk['free'])} ({disk['how']})")
            if disk["free"] < need_all * 1.2:
                raise StepFailed(f"Not enough disk space on the database server: the restore needs about "
                                 f"{fmt_size(need_all * 1.2)} (20% margin included), {fmt_size(disk['free'])} is free. "
                                 "Nothing was inserted. Free up or add space on the server, then click again.")
        if staging and (create or to_copy):
            self.section("copy the needed source tables to staging again (through this PC)")
            if create:
                path = self.sql(f"restage_{self.stamp}.sql")
                with open(path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(g.restage_sql(create))
                self.psql_run(target, "restage", file=path, extra_args=["-q", "--single-transaction"])
            if to_copy:
                self._copy_skipped_to_stage(g, target, [(s, t) for s, t in work
                                                        if generate.stage_table(s["name"], t) in to_copy])
                relinked = True
        elif not staging and self.psql_scalar(target, LINKS_DIRECT) == "0":
            self.section("attach the sources again")
            self.psql_run(target, "attach", file=self.sql("01_attach_sources.sql"),
                          variables=[f"src_user={self.cfg['src_user']}", f"src_password={self.cfg['src_password']}",
                                     f"src_kerberos={'true' if self.cfg['src_kerberos'] else 'false'}"])
            relinked = True
        self.section("restore skipped duplicates")
        path = self.sql(f"restore_skipped_{self.stamp}.sql")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(self.m(g.restore_skipped(), staging))
        self.psql_run(target, "restore_skipped", file=path)
        left = int(self.psql_scalar(target, self.m(
            "SELECT count(*) FROM _merge.conflict_log c WHERE c.action = 'skipped' AND EXISTS ("
            "SELECT 1 FROM _merge.loaded_source l WHERE l.table_name = c.table_name AND l.source_db = c.source_db)",
            staging)))
        # verify compares with the staging copies, so only pairs whose copy is whole; partial copies
        # (only the skipped rows) can't be compared that way: Verify counts reads the sources instead
        partial = self._partial_stage(target, {generate.stage_table(s["name"], t) for s, t in work}) if staging else set()
        holds = {}
        for s, t in work:
            if generate.stage_table(s["name"], t) not in partial:
                holds.setdefault(t, []).append(s["name"])
        if holds:
            self._sequences_and_verify(target, g, staging, holds)
        else:
            self.section("sequences")
            self.psql_run(target, "sequences", file=self.sql("04b_sequences.sql"))
        if left:
            raise StepFailed(f"{left:,} skipped rows were NOT restored: their rows were not found in the source "
                             "(staging copy empty or changed?). See the log above.")
        self.log("\nSkipped duplicates restored: every copy of a duplicate file is in Files now, with its "
                 "Metadata, Permissions and tasks, tagged with its source_db.")
        if partial:
            self.log("Click Verify counts to compare every table with the source databases.")
        if relinked:
            self.log("The sources were linked / copied again for this. Click Finish to remove them when done.")

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
        """([source names in priority order], [(table, kind, needs, sources, source_rows, loaded_rows,
        loaded_at, loaded_from)]) or None if not prepared."""
        target = self.cfg["target"]
        ok = self.psql_scalar("postgres", f"SELECT 1 FROM pg_database WHERE datname = '{target}'")
        if ok != "1":
            return None
        layout = self.layout(target)
        staging = layout == "staging"
        if not layout or self.psql_scalar(target, self.m(
                "SELECT to_regclass('_merge.table_plan') IS NOT NULL", staging)) != "t":
            return None
        current = self.psql_scalar(target, self.m(HAS_LOADED_SOURCE, staging)) == "t"
        out = self.psql_scalar(target, self.m(PLAN_QUERY.format(loaded_from=LOADED_FROM if current else LOADED_FROM_OLD),
                                              staging), extra=("-F", "\t"))
        sources = self.psql_scalar(target, self.m("SELECT source_db FROM _merge.source ORDER BY load_order",
                                                  staging)).splitlines()
        return sources, [tuple(line.rstrip("\r").split("\t")) for line in out.splitlines() if line.strip()]

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

    def indexes(self, table):
        """[{name, def, cols}] of public.<table>, see INDEXES_QUERY."""
        t = table.replace("'", "''")
        return json.loads(self.psql_scalar(self.cfg["target"], INDEXES_QUERY.format(t=t)))

    def create_index(self, table, cols, name):
        """CREATE INDEX on public.<table>; the file stays in migration_work like all SQL run."""
        target = self.cfg["target"]
        self.section(f"create index {name}")
        collist = ", ".join(generate.q(c) for c in cols)
        os.makedirs(WORK_SQL, exist_ok=True)
        path = self.sql(f"index_{self.stamp}.sql")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\\timing on\nSET maintenance_work_mem = '1GB';\n"
                    f"CREATE INDEX {generate.q(name)} ON public.{generate.q(table)} ({collist});\n")
        self.psql_run(target, "index", file=path)
        self.log(f"\nIndex {name} created on {table} ({', '.join(cols)}).")

    def read_schema(self, database):
        """Tables and columns of `database` (schema_compare.parse)."""
        return schema_compare.parse(self.psql_scalar(database, schema_compare.SCHEMA_SQL,
                                                     extra=("-q", "-c", "SET search_path = ''")))

    def fix_schema(self, database, path):
        """Run a schema_compare.fix_script file in `database`: all of it or nothing."""
        self.section(f"add missing tables / columns to {database}")
        self.psql_run(database, "schema_fix", file=path, extra_args=["--single-transaction"])
        self.log(f"\n{database}: missing tables / columns added (no rows copied).")

    # ---------- upload metadata CSVs (metadata_upload.py) ----------
    def metadata_state(self):
        """{"metadata": missing/ok/other, "rows": estimate, "log": {SourceFile: row} or None}."""
        target = self.cfg["target"]
        state = json.loads(self.psql_scalar(target, metadata_upload.STATE_SQL))
        state["log"] = json.loads(self.psql_scalar(target, metadata_upload.LOG_SQL)) if state["log"] else None
        return state

    def metadata_upload(self, folder, recreate, load, stop_on_error, stop):
        """Create / extend Metadata_Extend + its upload log, then load every file of folder not loaded yet."""
        target, mu = self.cfg["target"], metadata_upload
        t_job = time.perf_counter()
        self.section("upload metadata files")
        files = mu.scan(folder) if load else []
        log = {}
        if not recreate:
            state = self.metadata_state()
            if state["metadata"] == "other":
                raise StepFailed(f'"{mu.TABLE}" in {target} exists but is not an upload table (no "Source" '
                                 "column). Rename or drop it first, or use Recreate.")
            log = state["log"] or {}
        todo = mu.to_load(files, log)
        if load:
            self.log(f"{folder}: {len(files):,} file(s) (.xlsx / .csv), {len(files) - len(todo):,} already loaded, "
                     f"{len(todo):,} to load.")
        heads, bad = {}, {}
        for rel, full, _, _ in todo:
            try:
                heads[rel] = mu.read_header(full)
            except Exception as e:  # noqa: BLE001 - a bad file is recorded, the others still load
                bad[rel] = f"header: {type(e).__name__}: {e}"
        cols = mu.union_columns(heads.values())
        os.makedirs(WORK_SQL, exist_ok=True)
        path = os.path.join(WORK_SQL, f"metadata_setup_{self.stamp}.sql")
        with open(path, "w", encoding="utf-8") as f:
            f.write(mu.setup_sql(cols, recreate))
        if recreate:
            self.log(f'Dropping "{mu.TABLE}" and "{mu.LOG_TABLE}" and creating them new.')
        self.psql_run(target, "metadata_setup", file=path, extra_args=["-q"])
        self.log(f'"{mu.TABLE}" has the columns of the files ({len(cols)} found in the files to load); '
                 f'"{mu.LOG_TABLE}" tracks each file.')
        if not load:
            return
        counts = {"loaded": 0, "rows": 0, "failed": 0}
        t_load = time.perf_counter()
        try:
            self._upload_files(todo, bad, heads, target, stop, stop_on_error, counts)
        finally:
            done = counts["loaded"] + counts["failed"]
            total, loading = time.perf_counter() - t_job, time.perf_counter() - t_load
            self.log("\n==== upload summary ====\n"
                     f"Files loaded:  {counts['loaded']:,} of {len(todo):,} to load ({counts['failed']:,} failed"
                     + (", stopped" if stop.is_set() else "") + ")\n"
                     f"Rows loaded:   {counts['rows']:,}\n"
                     f"Loading time:  {mu.fmt_secs(loading)}"
                     + (f"  (average {mu.fmt_secs(loading / done)} per file)" if done else "") + "\n"
                     f"Total time:    {mu.fmt_secs(total)}  (including reading the folder and preparing the tables)")
        if counts["failed"] and not stop.is_set():
            raise StepFailed(f"{counts['failed']:,} file(s) failed - see {mu.LOG_TABLE} (Status = 'error'). "
                             "Start upload again to retry them.")

    def _upload_files(self, todo, bad, heads, target, stop, stop_on_error, counts):
        """Load each file of todo in turn; counts gets loaded / rows / failed as it goes."""
        mu = metadata_upload
        for i, (rel, full, size, mtime) in enumerate(todo, 1):
            if stop.is_set():
                self.log("\nStopped. The next run starts from the first file not loaded.")
                break
            self.out.put(("mu_row", (rel, "loading", None, "", None)))
            self.out.put(("step", f"upload {i:,}/{len(todo):,}: {rel}"))
            t_file = time.perf_counter()
            rows, err = (None, bad[rel]) if rel in bad else self._load_csv(target, rel, full, size, mtime, heads[rel])
            secs = time.perf_counter() - t_file
            if err is None:
                counts["loaded"] += 1
                counts["rows"] += rows
                self.out.put(("mu_row", (rel, mu.DONE, rows, "", secs)))
                self.log(f"[{i:,}/{len(todo):,}] {mu.fmt_secs(secs):>8}  {rows:,} row(s)  {rel}")
                continue
            counts["failed"] += 1
            status = "stopped" if stop.is_set() else "error"
            self.log(f"[{i:,}/{len(todo):,}] {mu.fmt_secs(secs):>8}  {status.upper()}  {rel}\n    {err}")
            self.out.put(("mu_row", (rel, status, None, err, secs)))
            try:
                self.psql_scalar(target, mu.error_sql(rel, size, mtime, err, status))
            except StepFailed as e:
                raise StepFailed(f"cannot record the error of {rel} in {mu.LOG_TABLE}: {e}") from e
            if stop.is_set():
                self.log("\nStopped. The next run starts again from this file.")
                break
            if stop_on_error:
                raise StepFailed(f"{rel} failed (nothing of it was loaded). Fix it and click Start upload: "
                                 "the run starts again from this file.")

    def prepare_upload(self, raw, upload, config_path, workers, stop, fmt, fill=False):
        """Raw export -> upload files, one pass, no intermediate copies (upload_prep.py). Needs no database."""
        self.section("build upload files")
        self.log(f"raw export: {raw}\nupload:     {upload}  (.{fmt})\nworkers:    {workers}")
        errors = upload_prep.run(raw, upload, config_path, workers, stop, self.log, fmt, fill)
        if errors:
            raise StepFailed(f"{len(errors):,} file(s) failed (listed above). Fix them and click Build again: "
                             "files already built are kept.")

    def _load_csv(self, target, rel, full, size, mtime, cols):
        """(rows, None) or (None, error text). The file goes to psql's stdin (\\copy ... FROM pstdin):
        a CSV as it is, an .xlsx converted to CSV on the fly."""
        args = ["-q", "-tA"]
        for c in metadata_upload.load_commands(rel, size, mtime, cols):
            args += ["-c", c]
        feed_error = None
        try:
            if metadata_upload.is_xlsx(full):
                self.mu_proc = proc = subprocess.Popen(self._cmd(target, args), env=self.env, creationflags=NO_WINDOW,
                                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                       stderr=subprocess.PIPE)
                outs = {}
                readers = [threading.Thread(target=lambda k, s: outs.__setitem__(k, s.read()), args=(k, s), daemon=True)
                           for k, s in (("out", proc.stdout), ("err", proc.stderr))]
                for t in readers:
                    t.start()
                text = io.TextIOWrapper(proc.stdin, encoding="utf-8", newline="")
                try:
                    w = csv.writer(text)
                    for row in metadata_upload.xlsx_rows(full):
                        w.writerow(row)
                except OSError:
                    pass  # psql closed its input (it failed or was stopped): its own exit code / error count
                except Exception as e:  # noqa: BLE001 - unreadable workbook: stop psql, nothing is committed
                    feed_error = f"reading the workbook: {type(e).__name__}: {e}"
                    proc.kill()
                finally:
                    try:
                        text.close()
                    except OSError:
                        pass
                proc.wait()
                for t in readers:
                    t.join()
                out, err = outs.get("out", b""), outs.get("err", b"")
            else:
                with open(full, "rb") as f:
                    self.mu_proc = proc = subprocess.Popen(self._cmd(target, args), env=self.env,
                                                           creationflags=NO_WINDOW, stdin=f,
                                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    out, err = proc.communicate()
        except OSError as e:
            return None, f"{type(e).__name__}: {e}"
        finally:
            self.mu_proc = None
        out = out.decode("utf-8", errors="replace").strip()
        err = err.decode("utf-8", errors="replace").strip()
        if feed_error:
            return None, feed_error[:2000]
        if proc.returncode == 0:
            last = out.splitlines()[-1].strip() if out else ""
            if last.isdigit():
                return int(last), None
            return None, f"unexpected psql output: {out[-500:]}"
        lines = [ln for ln in err.splitlines() if ln.strip()]
        keep = [ln for ln in lines if any(k in ln for k in ("ERROR", "CONTEXT", "DETAIL", "HINT", "FATAL"))]
        return None, ("\n".join(keep or lines[-5:]) or f"psql exit code {proc.returncode}")[:2000]

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


def plan_copy(checked, chosen, plan):
    """What step 2 does for the ticked tables `checked` and source databases `chosen`:
    - a table gets the chosen sources it has not been copied from yet;
    - with none left it is reloaded: emptied (from every source), then copied from the chosen ones;
    - a row needs its parent from the same source, so parents not copied yet are added;
    - reloading a table also reloads its copied children (TRUNCATE ... CASCADE empties them);
    - Files (generate.ORDERED_TABLES) takes its sources in list order, or the wrong source would
      win a tie between duplicate files (a Completed copy always wins). Other tables keep every
      row, so any order will do.
    Returns {"copies": {table: [source]}, "reload": set, "added": set of tables nobody ticked,
    "holds": {table: [source]} after the copy, "lost": {table: [source]} no longer held,
    "errors": [text], "not_there": ticked tables that no chosen source has}."""
    def names(text):
        return [x for x in text.split(", ") if x]
    has = {r[0]: names(r[3]) for r in plan}
    needs = {r[0]: names(r[2]) for r in plan}
    loaded = {r[0]: set(names(r[7])) for r in plan}
    copies, reload, not_there = {}, set(), set()
    for t in checked:
        wanted = set(has[t]) & set(chosen)
        if not wanted:
            not_there.add(t)
            continue
        new = wanted - loaded[t]
        copies[t] = new or wanted
        if not new:
            reload.add(t)
    changed = True
    while changed:
        changed = False
        for t in list(copies):
            for p in needs[t]:
                missing = {s for s in copies[t] & set(has[p]) if p in reload or s not in loaded[p]} - copies.get(p, set())
                if missing:
                    copies.setdefault(p, set()).update(missing)
                    changed = True
        for t in list(reload):
            for c in [c for c in has if t in needs[c] and loaded[c]]:
                again = (loaded[c] & set(chosen)) - copies.get(c, set())
                if c not in reload or again:
                    reload.add(c)
                    if again:
                        copies.setdefault(c, set()).update(again)
                    changed = True
    order = [r[0] for r in plan]
    holds = {t: (set() if t in reload else loaded[t]) | copies.get(t, set()) for t in order if t in copies or t in reload}
    errors = []
    for t in order:
        if t in generate.ORDERED_TABLES and t in copies:
            last = max(has[t].index(s) for s in copies[t])
            before = [s for s in has[t][:last] if s not in holds[t]]
            if before:
                errors.append(f"{t}: copy {', '.join(before)} before {has[t][last]}")

    def in_order(t, sources):
        return [s for s in has[t] if s in sources]
    return {"copies": {t: in_order(t, copies[t]) for t in order if t in copies}, "reload": reload,
            "added": set(copies) - set(checked),
            "holds": {t: in_order(t, holds[t]) for t in holds if holds[t]},
            "lost": {t: in_order(t, loaded[t] - holds[t]) for t in order if t in reload and loaded[t] - holds[t]},
            "errors": errors, "not_there": not_there}


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
        self.plan_sources = []  # databases of the target's plan, in priority order
        self.db_off = set()  # the ones unticked for step 2
        self.space = None  # the last space check: {"sizes", "errors", "disk", "at"}
        self.space_seq = 0  # only the newest check counts
        self._ensure_sources()
        self.skip_tables = set(self._read_sources().get("skip_tables") or [])  # as of the last step 1
        self.picker = None  # the open "Load table list" window
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
        return self._read_sources().get("target_dbname", "cwp2")

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
                            "a Completed copy of a file wins, otherwise the first source). host/port are as seen",
                            "from the target server; 'localhost' = the same server as the target."],
               "target_dbname": target, "sources": entries}
        if self.skip_tables:
            cfg["skip_tables"] = sorted(self.skip_tables)
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
        if hasattr(self, "mu_folder"):
            keep["mu_folder"] = self.mu_folder.get().strip()
            keep["mu_raw"] = self.mu_raw.get().strip()
            keep["mu_format"] = self.mu_format.get()
            keep["mu_fill"] = self.mu_fill.get()
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
        # the line holding the cursor is the one Move up / Move down acts on: highlight it
        self.db_text.tag_configure("current_db", background="#cce4ff")
        for ev in ("<ButtonRelease-1>", "<KeyRelease>", "<FocusIn>"):
            self.db_text.bind(ev, lambda e: self._mark_db_line(), add="+")
        self.db_text.bind("<Alt-Up>", lambda e: self._move_db(-1) or "break")
        self.db_text.bind("<Alt-Down>", lambda e: self._move_db(1) or "break")
        moves = ttk.Frame(dbs)
        moves.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(moves, text="Move up", command=lambda: self._move_db(-1)).pack(side="left", fill="x", expand=True)
        ttk.Button(moves, text="Move down", command=lambda: self._move_db(1)).pack(side="left", fill="x", expand=True, padx=(4, 0))
        ttk.Label(dbs, text="Click a line, then Move up / down (or Alt+Up / Alt+Down).",
                  foreground="#555").grid(row=2, column=0, sticky="w")
        ttk.Button(dbs, text="Load all from server", command=self._load_dbs).grid(row=3, column=0, sticky="ew", pady=(6, 0))
        dbs.rowconfigure(0, weight=1)
        dbs.columnconfigure(0, weight=1)
        top.columnconfigure(2, weight=1)
        top.rowconfigure(0, weight=1)

        merge = ttk.Frame(nb, padding=(0, 0, 0, 6))
        nb.add(merge, text="Merge databases")

        # Step 1
        s1 = ttk.LabelFrame(merge, text="Step 1 - Prepare target: create the tables (no data copied yet)", padding=8)
        s1.pack(fill="x", padx=6, pady=(8, 0))
        choose = ttk.Frame(s1)
        choose.pack(side="top", fill="x", pady=(0, 6))
        self.tables_btn = ttk.Button(choose, text="Load table list", command=self._load_tables)
        self.tables_btn.pack(side="left")
        self.skip_info = ttk.Label(choose)
        self.skip_info.pack(side="left", padx=(10, 0))
        self._show_skip()
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
        ttk.Label(s1, text="Creates the database, the tables you did not skip, the source links, "
                           "preflight report, IDs, indexes and keys.", foreground="#555").pack(side="left")

        # Step 2
        s2 = ttk.LabelFrame(merge, text="Step 2 - Copy data: tick databases and tables, then Copy (repeat as often as you like)", padding=8)
        s2.pack(fill="both", expand=True, padx=6, pady=(8, 0))
        bar = ttk.Frame(s2)
        bar.pack(fill="x")
        self.copy_btn = ttk.Button(bar, text="Copy data of ticked tables", command=self._step2)
        self.copy_btn.pack(side="left")
        self.recheck_btn = ttk.Button(bar, text="Re-check", command=self._recheck)
        self.recheck_btn.pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Refresh list", command=self._refresh).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Tick all", command=lambda: self._tick_all(True)).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Tick not loaded", command=self._tick_not_loaded).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Untick all", command=lambda: self._tick_all(False)).pack(side="left", padx=(8, 0))
        self.detach_btn = ttk.Button(bar, text="Finish: remove source links / copies", command=self._detach)
        self.matrix_btn = ttk.Button(bar, text="Build Metadata Matrix", command=self._matrix)
        self.restore_btn = ttk.Button(bar, text="Insert skipped duplicates", command=self._restore_skipped)
        self.restore_btn.pack(side="right", padx=(0, 8))
        self.counts_btn = ttk.Button(bar, text="Verify counts", command=self._verify_counts)
        self.counts_btn.pack(side="right", padx=(0, 8))
        self.matrix_btn.pack(side="right", padx=(0, 8))
        self.detach_btn.pack(side="right")
        self.plan_info = ttk.Label(s2, text="Run step 1, or click Refresh list if it is already done.", foreground="#555")
        self.plan_info.pack(fill="x", pady=(6, 2))
        sp = ttk.Frame(s2)
        sp.pack(fill="x", pady=(0, 4))
        ttk.Button(sp, text="Check space", command=self._check_space).pack(side="right")
        ttk.Label(sp, text="GB").pack(side="right", padx=(2, 10))
        self.free_gb = tk.StringVar()
        self.free_entry = ttk.Entry(sp, textvariable=self.free_gb, width=8)
        self.free_entry.pack(side="right")
        ttk.Label(sp, text="Free disk, if not detected:").pack(side="right", padx=(10, 4))
        self.space_info = ttk.Label(sp, text="", foreground="#555", wraplength=780)
        self.space_info.pack(side="left", fill="x", expand=True)
        self.free_gb.trace_add("write", lambda *_: self._update_space())

        cols = ("tick", "table", "kind", "needs", "sources", "source_rows", "source_size", "loaded_rows", "loaded_from", "loaded_at")
        heads = ("Copy", "Table", "Type", "Needs", "Sources", "Rows in sources", "Size in sources", "Rows loaded", "Copied from", "Loaded at")
        widths = (44, 200, 90, 120, 180, 100, 90, 90, 160, 120)
        frame = ttk.Frame(s2)
        frame.pack(fill="both", expand=True)
        dbs = ttk.Frame(frame)
        dbs.pack(side="left", fill="y", padx=(0, 8))
        self.db_tree = ttk.Treeview(dbs, columns=("tick", "db", "done", "size"), show="headings", height=11, selectmode="none")
        for c, h, w, a in (("tick", "Copy", 44, "center"), ("db", "Database (priority order)", 170, "w"),
                           ("done", "Tables copied", 90, "center"), ("size", "Size", 70, "e")):
            self.db_tree.heading(c, text=h)
            self.db_tree.column(c, width=w, anchor=a, stretch=False)
        self.db_tree.pack(side="left", fill="y")
        self.db_tree.bind("<Button-1>", self._on_db_click)
        self.tree = ttk.Treeview(frame, columns=cols, show="headings", height=11, selectmode="none")
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            anchor = "e" if c in ("source_rows", "source_size", "loaded_rows") else ("center" if c == "tick" else "w")
            self.tree.column(c, width=w, anchor=anchor, stretch=c in ("table", "sources"))
        self.tree.tag_configure("loaded", foreground="#1a7f37")
        self.tree.tag_configure("partial", foreground="#b35900")
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Double-Button-1>", self._on_tree_double)
        self.tree.bind("<Button-3>", self._on_tree_columns)

        self._build_compare(nb)
        self._build_acl(nb)
        self._build_upload(nb)
        self.job_btns = (self.step1_btn, self.copy_btn, self.recheck_btn, self.detach_btn, self.matrix_btn, self.restore_btn, self.counts_btn, self.fix_btn,
                         self.mu_start_btn, self.mu_forget_btn, self.mu_recreate_btn, self.mu_prep_btn)

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

    # ---------- choose tables before step 1 ----------
    def _show_skip(self):
        names = sorted(self.skip_tables)
        if not names:
            text = "Tables: all are migrated. Click Load table list to choose which to skip."
        else:
            text = (f"Tables: skipping {len(names)}: " + ", ".join(names[:6]) + (", ..." if len(names) > 6 else "")
                    + "  (takes effect when step 1 runs)")
        self.skip_info.configure(text=text)

    def _load_tables(self):
        try:
            cfg = self._config()
        except ValueError as e:
            messagebox.showerror(TITLE, str(e))
            return
        dbnames = self._db_list()
        if not dbnames:
            messagebox.showerror(TITLE, "Enter at least one source database.")
            return
        entries, _ = self._source_entries(dbnames)
        self.status.configure(text="Loading the table list...")
        self.tables_btn.configure(state="disabled")

        def work():
            try:
                self.queue.put(("table_list", Runner(cfg, self.queue).source_tables(entries)))
            except (StepFailed, OSError) as e:
                self.queue.put(("table_list_failed", f"Cannot load the table list:\n\n{e}"))

        threading.Thread(target=work, daemon=True).start()

    def _show_table_picker(self, tables, never):
        """Window: every table of every source; unticked ones are skipped by step 1."""
        if self.picker is not None and self.picker.winfo_exists():
            self.picker.destroy()
        win = self.picker = tk.Toplevel(self)
        win.title("Tables to migrate")
        win.geometry("1000x640")
        present = set(tables)
        order = [t for t in generate.LOAD_ORDER if t in present] + sorted(present - set(generate.LOAD_ORDER))
        kept = set(present)
        for t in self.skip_tables & present:  # the last choice; what a skipped table is needed by goes too
            kept = tick_tables(kept, t, False, present)
        state = {"kept": kept}

        ttk.Label(win, wraplength=960, padding=(8, 8, 8, 4), text=(
            "Click a row to tick or untick it. Unticked tables are skipped: nothing is copied from them. "
            "A 'copied as-is' table is then not created at all; a 'merge rules' table is still created, empty, "
            "because the target's keys, triggers and view need it. Rows are the server's estimate."
            + (f"  {never} bak_* backup table(s) are never migrated and not listed." if never else ""))).pack(fill="x")
        bar = ttk.Frame(win, padding=(8, 4, 8, 8))
        bar.pack(side="bottom", fill="x")
        info = ttk.Label(win, foreground="#555", wraplength=960, padding=(8, 4, 8, 0))
        info.pack(side="bottom", fill="x")
        frame = ttk.Frame(win, padding=(8, 0, 8, 0))
        frame.pack(fill="both", expand=True)
        cols = ("tick", "table", "kind", "needs", "sources", "rows")
        heads = ("Migrate", "Table", "Type", "Needs", "Sources", "Rows (est.)")
        widths = (64, 280, 100, 170, 280, 100)
        tree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="none")
        for c, h, w in zip(cols, heads, widths):
            tree.heading(c, text=h)
            tree.column(c, width=w, anchor="e" if c == "rows" else "center" if c == "tick" else "w",
                        stretch=c in ("table", "sources"))
        tree.tag_configure("skip", foreground="#999999")
        sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        rows = {}
        for t in order:
            d = tables[t]
            est = f"{d['rows']:,}" + ("+ ?" if d["unknown"] and d["rows"] else "") if d["rows"] or not d["unknown"] else "?"
            rows[t] = tree.insert("", "end", values=(
                "", t, "merge rules" if t in generate.LOAD_ORDER else "copied as-is",
                ", ".join(p for p in generate.DEPENDS.get(t, []) if p in present), ", ".join(d["sources"]), est))
        table_of = {iid: t for t, iid in rows.items()}

        def show(note=""):
            for t, iid in rows.items():
                on = t in state["kept"]
                tree.set(iid, "tick", "\u2611" if on else "\u2610")
                tree.item(iid, tags=() if on else ("skip",))
            info.configure(text=f"{len(state['kept'])} to migrate, {len(present) - len(state['kept'])} skipped.  {note}")

        def on_click(event):
            t = table_of.get(tree.identify_row(event.y))
            if t:
                before = state["kept"]
                state["kept"] = tick_tables(before, t, t not in before, present)
                also = sorted((before ^ state["kept"]) - {t})
                show((("Also ticked, it needs: " if t in state["kept"] else "Also skipped, they need it: ")
                      + ", ".join(also)) if also else "")

        def set_all(on):
            state["kept"] = set(present) if on else set()
            show()

        def use():
            if not state["kept"]:
                messagebox.showerror(TITLE, "Tick at least one table to migrate.", parent=win)
                return
            self.skip_tables = present - state["kept"]
            self._show_skip()
            win.destroy()

        tree.bind("<Button-1>", on_click)
        ttk.Button(bar, text="Use this selection", command=use).pack(side="left")
        ttk.Button(bar, text="Cancel", command=win.destroy).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Tick all", command=lambda: set_all(True)).pack(side="left", padx=(16, 0))
        ttk.Button(bar, text="Untick all", command=lambda: set_all(False)).pack(side="left", padx=(8, 0))
        show()

    def _open_logs(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        os.startfile(LOG_DIR)

    # ---------- table list ----------
    def _show_plan(self, plan):
        self.tree.delete(*self.tree.get_children())
        self.db_tree.delete(*self.db_tree.get_children())
        if plan is None:
            self.plan_rows, self.plan_sources = [], []
            self.checked.clear()
            self.plan_info.configure(text=f"{self.vars['target'].get()} is not prepared yet - run step 1 first.")
            self.space = None
            self.space_info.configure(text="")
            return
        self.plan_sources, rows = plan
        self.plan_rows = rows
        names = {r[0] for r in rows}
        self.checked &= names
        n_loaded = 0
        for t, kind, needs, sources, src_rows, loaded_rows, loaded_at, loaded_from in rows:
            n_have, n_done = len(sources.split(", ")), len([x for x in loaded_from.split(", ") if x])
            n_loaded += n_done == n_have
            self.tree.insert("", "end", iid=t, tags=("loaded",) if n_done == n_have else ("partial",) if n_done else (),
                             values=("☑" if t in self.checked else "☐", t, kind, needs, sources,
                                     f"{int(src_rows):,}" if src_rows else "", "", f"{int(loaded_rows):,}" if loaded_rows else "",
                                     "all" if n_done == n_have else loaded_from, loaded_at or "not loaded"))
        for s in self.plan_sources:
            have = [r for r in rows if s in r[3].split(", ")]
            done = sum(1 for r in have if s in r[7].split(", "))
            self.db_tree.insert("", "end", iid=s, values=("☐" if s in self.db_off else "☑", s, f"{done} / {len(have)}", ""))
        self.plan_info.configure(text=f"{len(rows)} tables, {n_loaded} fully loaded. Tick the databases to copy on the "
                                      "left (a Completed copy of a file wins, otherwise the top one, so copy Files in this order). "
                                      "Click a row to tick it; double-click "
                                      "or right-click it to see its columns per source database and add indexes. "
                                      "Tables a ticked table needs are added automatically.")
        self._show_sizes()
        self._update_space()

    # ---------- disk space ----------
    def _check_space(self):
        """Read the table sizes of the sources and the server's free disk, off the UI thread."""
        if not self.plan_rows:
            self.space_info.configure(text="Run step 1, or click Refresh list, first.")
            return
        try:
            cfg = self._config()
        except ValueError as e:
            messagebox.showerror(TITLE, str(e))
            return
        known = {s["name"]: s for s in self._read_sources().get("sources", []) if isinstance(s, dict) and s.get("name")}
        entries = [{"name": n, "dbname": known[n].get("dbname", n), "host": known[n].get("host", "localhost"),
                    "port": known[n].get("port", 5432)} for n in self.plan_sources if n in known]
        missing = {n: "not in sources.json" for n in self.plan_sources if n not in known}
        self.space_seq += 1
        seq = self.space_seq
        self.space_info.configure(text="Reading table sizes and free disk...", foreground="#555")

        def work():
            runner = Runner(cfg, self.queue)
            sizes, errors = runner.source_sizes(entries)
            disk = runner.free_space(cfg["target"])
            self.queue.put(("space", (seq, {"sizes": sizes, "errors": {**missing, **errors}, "disk": disk,
                                            "at": datetime.now().strftime("%H:%M")})))

        threading.Thread(target=work, daemon=True).start()

    def _show_sizes(self):
        """Fill the size columns from the last check."""
        if not self.space:
            return
        sizes = self.space["sizes"]
        for r in self.plan_rows:
            have = [s for s in r[3].split(", ") if s and r[0] in sizes.get(s, {})]
            if self.tree.exists(r[0]) and have:
                self.tree.set(r[0], "source_size", fmt_size(sum(sizes[s][r[0]] for s in have)))
        for s in self.plan_sources:
            if self.db_tree.exists(s) and s in sizes:
                self.db_tree.set(s, "size", fmt_size(sum(sizes[s].get(r[0], 0) for r in self.plan_rows
                                                         if s in r[3].split(", "))))

    def _free_now(self):
        """(free bytes or None, text saying where the number comes from)."""
        disk = self.space["disk"] if self.space else None
        if disk and disk["free"] is not None:
            return disk["free"], f"read at {self.space['at']}"
        try:
            typed = float(self.free_gb.get().strip().replace(",", ""))
        except ValueError:
            return None, ""
        return (int(typed * 1024 ** 3), "typed by you") if typed >= 0 else (None, "")

    def _space_report(self, copies):
        """(one-line summary, more detail for the Copy question, not enough space?) for `copies`."""
        disk = self.space["disk"]
        est = estimate_space(copies, self.space["sizes"], disk["wal"])
        free, source = self._free_now()
        need = ("at least " if est["unknown"] else "about ") + fmt_size(est["need"])
        have = f"{fmt_size(free)} free ({source})" if free is not None else "an unknown amount free"
        short = free is not None and free < est["need"]
        verdict = " - NOT ENOUGH SPACE" if short else " - should fit" if free is not None and not est["unknown"] else ""
        line = (f"Ticked tables need {need} ({fmt_size(est['data'])} data + {fmt_size(est['margin'])} margin + "
                f"{fmt_size(est['wal'])} WAL); server has {have}{verdict}")
        detail = [f"Disk space: needs {need} = {fmt_size(est['data'])} data + {fmt_size(est['margin'])} margin "
                  f"+ {fmt_size(est['wal'])} WAL. Server has {have}.",
                  "This is an estimate from the size of the tables in the sources."]
        if est["unknown"]:
            detail.append("Size not known, so not counted: " + ", ".join(sorted({t for t, _ in est["unknown"]}))
                          + " in " + ", ".join(sorted({s for _, s in est["unknown"]})))
        if disk["idle_slots"]:
            detail.append(f"{disk['idle_slots']} inactive replication slot(s) keep WAL on disk, so the copy can need "
                          "much more than this.")
        if short:
            detail.insert(0, "NOT ENOUGH DISK SPACE: PostgreSQL stops if its disk fills up, for every database on the server.")
        return line, detail, short

    def _update_space(self):
        """The line above the table list; runs on every tick."""
        if not self.space:
            return
        disk = self.space["disk"]
        self.free_entry.configure(state="disabled" if disk["free"] is not None else "normal")
        chosen = [s for s in self.plan_sources if s not in self.db_off]
        copies = plan_copy(self.checked, chosen, self.plan_rows)["copies"] if self.checked and chosen else {}
        short = False
        if copies:
            text, _, short = self._space_report(copies)
        else:
            free, source = self._free_now()
            text = "Tick tables and databases to see the space they need. " + (
                f"Server has {fmt_size(free)} free ({source})." if free is not None else "Free disk unknown.")
        if disk["free"] is None and not self.free_gb.get().strip():
            text += ". Not detected: type it from df -h on the server."
        self.space_info.configure(text=text, foreground="#b00020" if short else "#555")

    def _on_db_click(self, event):
        s = self.db_tree.identify_row(event.y)
        if s:
            self.db_off ^= {s}
            self.db_tree.set(s, "tick", "☐" if s in self.db_off else "☑")
            self._update_space()

    def _on_tree_click(self, event):
        t = self.tree.identify_row(event.y)
        if not t:
            return
        if t in self.checked:
            self.checked.discard(t)
        else:
            self.checked.add(t)
        self.tree.set(t, "tick", "☑" if t in self.checked else "☐")
        self._update_space()

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
                runner = Runner(cfg, self.queue)
                self.queue.put(("columns", (t, runner.columns(t), runner.indexes(t))))
            except (StepFailed, OSError, ValueError) as e:
                self.queue.put(("error", f"Cannot read the columns of {t}:\n\n{e}"))

        threading.Thread(target=work, daemon=True).start()

    def _show_columns(self, table, info, indexes):
        """Window: one row per column, one column per source (its type there, blank = missing);
        tick columns (first ticked = first in the index) to add an index on them."""
        sources, cols, src_cols, linked = info
        have = [s for s in sources if s in src_cols]
        rows = list(cols)
        target_names = {c for c, _ in cols}
        for s in have:  # columns a source has that the target does not (should not happen)
            rows += [(c, "") for c in src_cols[s] if c not in target_names and (c, "") not in rows]

        win = tk.Toplevel(self)
        win.title(f"{table} - columns")
        win.geometry(f"{min(1600, 600 + 160 * len(sources))}x720")
        if not linked:
            note = "The source links were removed (Finish), so only the target columns are known. Run step 1 again to compare."
        else:
            missing = [s for s in sources if s not in src_cols]
            note = (f"{len(rows)} columns. Table found in {len(have)} of {len(sources)} sources"
                    + (f" (not in: {', '.join(missing)})" if missing else "") + ".  "
                    "Cell = column type in that source, blank = column not in that source.  "
                    "Orange = only in some sources, grey = added by the migration, red = not in the target.")
        ttk.Label(win, text=note, wraplength=1100, padding=(8, 8, 8, 4)).pack(fill="x")

        # packed before the column list so they keep their room when the window is small
        idx_box = ttk.LabelFrame(win, text="Indexes on this table", padding=6)
        idx_box.pack(side="bottom", fill="x", padx=8, pady=(0, 8))
        idx_tree = ttk.Treeview(idx_box, columns=("name", "def"), show="headings", height=5, selectmode="none")
        idx_tree.heading("name", text="Index")
        idx_tree.heading("def", text="Definition")
        idx_tree.column("name", width=280, stretch=False)
        idx_tree.column("def", width=700)
        idx_sb = ttk.Scrollbar(idx_box, orient="vertical", command=idx_tree.yview)
        idx_tree.configure(yscrollcommand=idx_sb.set)
        idx_tree.pack(side="left", fill="both", expand=True)
        idx_sb.pack(side="right", fill="y")

        bar = ttk.Frame(win, padding=(8, 0, 8, 6))
        bar.pack(side="bottom", fill="x")
        create_btn = ttk.Button(bar, text="Create index on ticked columns", state="disabled")
        create_btn.pack(side="left")
        idx_label = ttk.Label(bar, foreground="#555")
        idx_label.pack(side="left", padx=(10, 0))

        frame = ttk.Frame(win, padding=(8, 0, 8, 8))
        frame.pack(fill="both", expand=True)
        ids = ["tick", "column", "target"] + [f"s{i}" for i in range(len(sources))] + ["found"]
        tree = ttk.Treeview(frame, columns=ids, show="headings", selectmode="none")
        tree.heading("tick", text="Index")
        tree.column("tick", width=50, anchor="center", stretch=False)
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
        col_of = {}  # tree row -> column, only those in the target can be indexed
        for c, typ in rows:
            n = sum(1 for s in have if c in src_cols[s])
            tag = ("srconly",) if not typ else ("added",) if linked and n == 0 else \
                  ("partial",) if n < len(have) else ()
            iid = tree.insert("", "end", tags=tag, values=["☐" if typ else "", c, typ or "(not in target)"]
                              + [src_cols.get(s, {}).get(c, "") for s in sources]
                              + [f"{n}/{len(have)}" if linked else ""])
            if typ:
                col_of[iid] = c
        ys = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        xs = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        picked = []  # tree rows in the order they were ticked
        current = {"indexes": indexes}

        def show_picked():
            for iid in col_of:
                tree.set(iid, "tick", f"☑ {picked.index(iid) + 1}" if iid in picked else "☐")
            cols = [col_of[i] for i in picked]
            create_btn.configure(state="normal" if cols else "disabled")
            idx_label.configure(text=index_name(table, cols, [x["name"] for x in current["indexes"]]) if cols
                                else "Click columns to tick them; the first ticked comes first in the index.")

        def on_click(event):
            iid = tree.identify_row(event.y)
            if iid in col_of:
                picked.remove(iid) if iid in picked else picked.append(iid)
                show_picked()

        def show_indexes(rows):
            current["indexes"] = rows
            idx_tree.delete(*idx_tree.get_children())
            for x in rows:
                idx_tree.insert("", "end", values=(x["name"], x["def"]))
            picked.clear()
            show_picked()

        tree.bind("<Button-1>", on_click)
        create_btn.configure(command=lambda: self._create_index(table, [col_of[i] for i in picked],
                                                                current["indexes"], show_indexes, win))
        show_indexes(indexes)

    def _create_index(self, table, cols, indexes, show_indexes, win):
        if self.running:
            messagebox.showinfo(TITLE, "Another job is running. Try again when it has finished.", parent=win)
            return
        try:
            cfg = self._config()
        except ValueError as e:
            messagebox.showerror(TITLE, str(e), parent=win)
            return
        name = index_name(table, cols, [x["name"] for x in indexes])
        same = [x["name"] for x in indexes if x["cols"] == cols]
        warn = f"{same[0]} already indexes these columns in this order, so this one would only add cost.\n\n" if same else ""
        if not messagebox.askyesno(
                TITLE, f"{warn}Create index {name} in {cfg['target']}?\n\n"
                       f"  {table} ({', '.join(cols)})\n\n"
                       "The table cannot be written to while it builds, which can take a while on a big table. "
                       "Reloading the table in step 2 keeps the index; running step 1 again with "
                       "Drop or Clear removes it.",
                icon="warning" if same else "question", parent=win):
            return
        ticked = set(self.checked)  # a job that ends OK clears the table ticks; this one should not

        def finished():
            self.checked |= ticked

            def work():
                try:
                    rows = Runner(cfg, self.queue).indexes(table)
                except (StepFailed, OSError, ValueError) as e:
                    self.queue.put(("error", f"Index created, but the list could not be read:\n\n{e}"))
                    return
                self.queue.put(("indexes", (show_indexes, win, rows)))

            threading.Thread(target=work, daemon=True).start()

        self.after_done = finished
        self._launch("create_index", table, cols, name, cfg=cfg)

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
        """The chosen ACL backups of permission.ps1 (*.acl.bak) to an .xlsx report, as read before it ran."""
        if self.acl_proc:
            return
        chosen = filedialog.askopenfilenames(parent=self, title="permission.ps1's ACL backups (Ctrl / Shift for several)",
                                             filetypes=[("ACL backups", "*.acl.bak"), ("All files", "*.*")])
        if not chosen:
            return
        backups = sorted((os.path.normpath(f) for f in chosen), key=str.lower)   # parents first
        folder = os.path.dirname(backups[0])
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

    # ---------- "Upload metadata" tab (metadata_upload.py) ----------
    def _build_upload(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="Upload metadata")
        self.mu_folder = tk.StringVar(value=self._load_settings().get("mu_folder", ""))
        self.mu_stop_on_error = tk.BooleanVar(value=True)
        self.mu_stop = threading.Event()
        self.mu_active = False      # an upload job is running
        self.mu_iid = {}            # SourceFile -> tree item
        self.runner = None

        bar = ttk.Frame(tab)
        bar.pack(fill="x")
        ttk.Label(bar, text="Upload folder").pack(side="left")
        entry = ttk.Entry(bar, textvariable=self.mu_folder, width=60)
        entry.pack(side="left", padx=(6, 4), fill="x", expand=True)
        entry.bind("<Return>", lambda _: self._mu_refresh())
        ttk.Button(bar, text="Browse...", command=self._mu_browse).pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="Scan / refresh status", command=self._mu_refresh).pack(side="left")

        acts = ttk.Frame(tab)
        acts.pack(fill="x", pady=(6, 0))
        self.mu_start_btn = ttk.Button(acts, text="Start upload", command=self._mu_start)
        self.mu_start_btn.pack(side="left")
        self.mu_stop_btn = ttk.Button(acts, text="Stop", command=self._mu_stop_now, state="disabled")
        self.mu_stop_btn.pack(side="left", padx=(6, 0))
        ttk.Checkbutton(acts, text="Stop at the first error", variable=self.mu_stop_on_error).pack(
            side="left", padx=(12, 0))
        self.mu_recreate_btn = ttk.Button(acts, text="Recreate Metadata_Extend table...", command=self._mu_recreate)
        self.mu_recreate_btn.pack(side="right")
        self.mu_forget_btn = ttk.Button(acts, text="Reload selected", command=self._mu_forget)
        self.mu_forget_btn.pack(side="right", padx=(0, 6))

        prep = ttk.LabelFrame(tab, text="Build the upload files from a raw SharePoint export", padding=6)
        prep.pack(fill="x", pady=(8, 0))
        self.mu_raw = tk.StringVar(value=self._load_settings().get("mu_raw", ""))
        self.mu_workers = tk.StringVar(value=str(max(1, min(6, (os.cpu_count() or 2) - 1))))
        self.mu_format = tk.StringVar(value=self._load_settings().get("mu_format", "xlsx"))
        self.mu_fill = tk.BooleanVar(value=self._load_settings().get("mu_fill", False))
        ttk.Label(prep, text="Raw export folder").pack(side="left")
        ttk.Entry(prep, textvariable=self.mu_raw, width=50).pack(side="left", padx=(6, 4), fill="x", expand=True)
        ttk.Button(prep, text="Browse...", command=self._mu_raw_browse).pack(side="left", padx=(0, 12))
        ttk.Label(prep, text="Workers").pack(side="left")
        ttk.Spinbox(prep, from_=1, to=32, width=4, textvariable=self.mu_workers).pack(side="left", padx=(6, 12))
        ttk.Label(prep, text="Save as").pack(side="left")
        ttk.Radiobutton(prep, text=".xlsx", variable=self.mu_format, value="xlsx").pack(side="left", padx=(6, 0))
        ttk.Radiobutton(prep, text=".csv", variable=self.mu_format, value="csv").pack(side="left", padx=(6, 12))
        ttk.Checkbutton(prep, text="Fill empty Created / Modified By from Author / Editor",
                        variable=self.mu_fill).pack(side="left", padx=(0, 12))
        self.mu_prep_btn = ttk.Button(prep, text="Build upload files", command=self._mu_prepare)
        self.mu_prep_btn.pack(side="left")

        self.mu_info = ttk.Label(tab, foreground="#555", justify="left", text=(
            "Pick the folder of upload .xlsx / .csv files (e.g. compareDanielUpload) and "
            "click Scan. Start upload loads every file "
            f"into \"{metadata_upload.TABLE}\" (column Source = the file's path) and records each file in "
            f"\"{metadata_upload.LOG_TABLE}\". Files already done are skipped, so after an error the next run "
            "starts from that file. The original Metadata table is not touched."))
        self.mu_info.pack(fill="x", pady=(6, 2))
        self.mu_info.bind("<Configure>", lambda e: self.mu_info.configure(wraplength=max(e.width - 10, 200)))

        frame = ttk.Frame(tab)
        frame.pack(fill="both", expand=True)
        cols = ("size", "status", "rows", "time", "finished", "error")
        heads = ("Size", "Status", "Rows", "Time", "Finished", "Error")
        widths = (80, 80, 90, 70, 140, 420)
        self.mu_tree = ttk.Treeview(frame, columns=cols, show="tree headings", height=12)
        self.mu_tree.heading("#0", text="File (Source)")
        self.mu_tree.column("#0", width=520, stretch=True)
        for c, h, w in zip(cols, heads, widths):
            self.mu_tree.heading(c, text=h)
            self.mu_tree.column(c, width=w, anchor="e" if c in ("size", "rows", "time") else "w",
                                stretch=c == "error")
        for tag, color in (("done", "#1a7f37"), ("error", "#c00000"), ("stopped", "#b35900"),
                           ("loading", "#1f4e9e"), ("changed", "#b35900")):
            self.mu_tree.tag_configure(tag, foreground=color)
        self.mu_tree.tag_configure("next", font=("Segoe UI", 9, "bold"))
        ys = ttk.Scrollbar(frame, orient="vertical", command=self.mu_tree.yview)
        xs = ttk.Scrollbar(frame, orient="horizontal", command=self.mu_tree.xview)
        self.mu_tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.mu_tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.mu_tree.bind("<Double-Button-1>", self._mu_show_error)

    def _mu_browse(self):
        path = filedialog.askdirectory(parent=self, title="Upload folder (.xlsx / .csv files)",
                                       initialdir=self.mu_folder.get().strip() or None)
        if path:
            self.mu_folder.set(os.path.normpath(path))
            self._mu_refresh()

    def _mu_raw_browse(self):
        path = filedialog.askdirectory(parent=self, title="Raw SharePoint export folder (e.g. compareDaniel)",
                                       initialdir=self.mu_raw.get().strip() or None)
        if path:
            self.mu_raw.set(os.path.normpath(path))
            if not self.mu_folder.get().strip():
                self.mu_folder.set(upload_prep.default_upload(os.path.normpath(path)))

    def _mu_prepare(self):
        if self.running:
            return
        raw = os.path.normpath(self.mu_raw.get().strip()) if self.mu_raw.get().strip() else ""
        if not raw or not os.path.isdir(raw):
            messagebox.showerror(TITLE, "Pick the raw export folder (e.g. compareDaniel) first.")
            return
        if not self.mu_folder.get().strip():
            self.mu_folder.set(upload_prep.default_upload(raw))
        upload = os.path.normpath(self.mu_folder.get().strip())
        fmt = self.mu_format.get()
        nr, nu = os.path.normcase(raw), os.path.normcase(upload)
        if nr == nu or nu.startswith(nr + os.sep) or nr.startswith(nu + os.sep):
            messagebox.showerror(TITLE, "The upload folder (output) must not be the raw export folder or inside it.")
            return
        config = next((os.path.normpath(p) for p in MH_CONFIGS if os.path.isfile(p)), None)
        if not config:
            messagebox.showerror(TITLE, "metadata_horizontal\\config.json (the upload CSV columns) was not found.")
            return
        try:
            workers = max(1, int(self.mu_workers.get()))
        except ValueError:
            workers = 4
        if not messagebox.askyesno(TITLE, f"Build the upload files from\n{raw}\n\ninto\n{upload}  (.{fmt})\n\n"
                                          "Each raw CSV becomes one ." + fmt + " file directly (MetaInfo expanded, "
                                          "columns selected); no intermediate files are made.\n\n"
                                          + (("Empty Created_x0020_By / Modified_x0020_By are filled from Author / "
                                              f"Editor.\nFill log: {upload_prep.fill_log_path(upload)}\n")
                                             if self.mu_fill.get() else
                                             "Author, Created_x0020_By, Created, Editor, Modified_x0020_By and Modified "
                                             "stay as in the raw CSV (no fill).\n")
                                          + f"Column profile: {upload_prep.profile_path(upload)}\n"
                                          f"Columns from {config}.\n\n"
                                          "Files already in the upload folder are kept (delete them to build "
                                          "again). The raw export is only read. A big export takes a while."):
            return
        try:
            cfg = self._config()  # no database is used; the job runner just needs the settings
        except ValueError as e:
            messagebox.showerror(TITLE, str(e))
            return
        self.mu_stop.clear()
        self.mu_active = True
        self._launch("prepare_upload", raw, upload, config, workers, self.mu_stop, fmt, self.mu_fill.get(), cfg=cfg)
        self.mu_stop_btn.configure(state="normal")

    def _mu_refresh(self):
        """Scan the folder and read MetadataUpload (on a thread); shows the result in the list."""
        folder = self.mu_folder.get().strip()
        if not folder:
            return
        try:
            cfg = self._config()
        except ValueError:
            cfg = None
        self.mu_info.configure(text=f"Scanning {folder}...")

        def work():
            files, state, err = [], None, ""
            try:
                files = metadata_upload.scan(folder) if os.path.isdir(folder) else None
            except OSError as e:
                err = f"Cannot read {folder}: {e}"
            if cfg:
                try:
                    state = Runner(cfg, self.queue).metadata_state()
                except (StepFailed, OSError, ValueError) as e:
                    err = (err + "\n" if err else "") + f"Cannot read the upload status from {cfg['target']}: {e}"
            self.queue.put(("mu_scan", (folder, files, state, err)))

        threading.Thread(target=work, daemon=True).start()

    def _mu_show(self, folder, files, state, err):
        self.mu_tree.delete(*self.mu_tree.get_children())
        self.mu_iid = {}
        if files is None:
            self.mu_info.configure(text=f"Folder not found: {folder}")
            return
        log = (state or {}).get("log") or {}
        counts, next_rel = {}, None
        for i, f in enumerate(files):
            rel, _, size, _ = f
            row = log.get(rel) or {}
            status = metadata_upload.status_of(f, log.get(rel))
            counts[status] = counts.get(status, 0) + 1
            tags = [status]
            if next_rel is None and status != metadata_upload.DONE:
                next_rel = rel
                tags.append("next")
            rows = row.get("rows")
            self.mu_iid[rel] = self.mu_tree.insert(
                "", "end", iid=f"mu{i}", text=rel, tags=tags,
                values=(fmt_size(size), status, "" if rows is None else f"{rows:,}",
                        metadata_upload.fmt_secs(row.get("seconds")), row.get("finished") or "",
                        (row.get("error") or "").replace("\n", "  ")))
        if next_rel:
            self.mu_tree.see(self.mu_iid[next_rel])
        parts = [f"{len(files):,} file(s)"] + [f"{n:,} {k}" for k, n in sorted(counts.items())]
        text = ", ".join(parts) + "."
        if state:
            if state["metadata"] == "other":
                text += f'  "{metadata_upload.TABLE}" exists but is not an upload table (no "Source" column).'
            elif state["metadata"] == "missing":
                text += f'  "{metadata_upload.TABLE}" does not exist yet: the first upload creates it.'
        text += f"  Next to load: {next_rel}" if next_rel else ("  All loaded." if files else "")
        if err:
            text += "\n" + err
        self.mu_info.configure(text=text)

    def _mu_set(self, rel, status, rows, error, secs=None):
        iid = self.mu_iid.get(rel)
        if not iid:
            return
        self.mu_tree.item(iid, tags=[status], values=(
            self.mu_tree.set(iid, "size"), status, "" if rows is None else f"{rows:,}", metadata_upload.fmt_secs(secs),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S") if status != "loading" else "", error.replace("\n", "  ")))
        if status == "loading":
            self.mu_tree.see(iid)

    def _mu_show_error(self, event):
        iid = self.mu_tree.identify_row(event.y)
        if iid and self.mu_tree.set(iid, "error"):
            messagebox.showinfo(TITLE, f"{self.mu_tree.item(iid, 'text')}\n\n{self.mu_tree.set(iid, 'error')}")

    def _mu_cfg(self):
        if self.running:
            return None
        try:
            return self._config()
        except ValueError as e:
            messagebox.showerror(TITLE, str(e))
            return None

    def _mu_run(self, cfg, recreate, load):
        self.mu_stop.clear()
        self.mu_active = True
        self._launch("metadata_upload", self.mu_folder.get().strip(), recreate, load, self.mu_stop_on_error.get(),
                     self.mu_stop, cfg=cfg)
        self.mu_stop_btn.configure(state="normal" if load else "disabled")

    def _mu_start(self):
        cfg = self._mu_cfg()
        if not cfg:
            return
        folder = self.mu_folder.get().strip()
        if not folder or not os.path.isdir(folder):
            messagebox.showerror(TITLE, "Pick the upload folder first.")
            return
        runner = Runner(cfg, self.queue)
        try:
            state = runner.metadata_state()
        except (StepFailed, OSError, ValueError) as e:
            if "does not exist" not in str(e) or cfg["target"] not in str(e):
                messagebox.showerror(TITLE, f"Cannot read {cfg['target']}:\n\n{e}")
                return
            if not messagebox.askyesno(TITLE, f'Database "{cfg["target"]}" does not exist on {cfg["host"]}.\n\n'
                                              "Create it now (empty) and upload into it?"):
                return
            try:
                runner.psql_scalar("postgres", f"CREATE DATABASE \"{cfg['target']}\" ENCODING 'UTF8' "
                                               "TEMPLATE template0")
                state = runner.metadata_state()
            except (StepFailed, OSError, ValueError) as e2:
                messagebox.showerror(TITLE, f"Cannot create database {cfg['target']}:\n\n{e2}")
                return
        if state["metadata"] == "other":
            messagebox.showerror(TITLE, f'"{metadata_upload.TABLE}" in {cfg["target"]} exists but is not an upload '
                                        'table (no "Source" column). Rename or drop it first, or use Recreate.')
            return
        if not messagebox.askyesno(TITLE, f"Upload the .xlsx / .csv files of\n{folder}\ninto {cfg['target']}."
                                            f'"{metadata_upload.TABLE}"?\n\n'
                                            "Files already loaded are skipped; the others load one by one, "
                                            "each all or nothing."):
            return
        self._mu_run(cfg, False, True)

    def _mu_stop_now(self):
        self.mu_stop.set()
        proc = getattr(self.runner, "mu_proc", None)
        if proc:
            proc.kill()
        self.mu_stop_btn.configure(state="disabled")

    def _mu_forget(self):
        rels = [self.mu_tree.item(i, "text") for i in self.mu_tree.selection()]
        if not rels:
            messagebox.showinfo(TITLE, "Select the files to load again in the list first.")
            return
        cfg = self._mu_cfg()
        if not cfg or not messagebox.askyesno(TITLE, f"Mark {len(rels):,} file(s) as not loaded?\n\n"
                                                     "The next Start upload loads them again (their rows in "
                                                     f"{metadata_upload.TABLE} are replaced then)."):
            return
        try:
            runner = Runner(cfg, self.queue)
            if runner.metadata_state()["log"] is not None:
                runner.psql_scalar(cfg["target"], metadata_upload.forget_sql(rels))
        except (StepFailed, OSError, ValueError) as e:
            messagebox.showerror(TITLE, str(e))
            return
        self._mu_refresh()

    def _mu_recreate(self):
        cfg = self._mu_cfg()
        if not cfg or not messagebox.askyesno(TITLE, f'DROP "{metadata_upload.TABLE}" and '
                                                     f'"{metadata_upload.LOG_TABLE}" in {cfg["target"]} and create '
                                                     "them new and empty?\n\nEvery uploaded row is lost; the next "
                                                     "Start upload loads all files again. This cannot be undone.",
                                              icon="warning"):
            return
        self._mu_run(cfg, True, False)

    def _tick_all(self, on):
        self.checked = {r[0] for r in self.plan_rows} if on else set()
        for r in self.plan_rows:
            self.tree.set(r[0], "tick", "☑" if on else "☐")
        self._update_space()

    def _tick_not_loaded(self):
        chosen = set(self.plan_sources) - self.db_off
        self.checked = {r[0] for r in self.plan_rows if (set(r[3].split(", ")) & chosen) - set(r[7].split(", "))}
        for r in self.plan_rows:
            self.tree.set(r[0], "tick", "☑" if r[0] in self.checked else "☐")
        self._update_space()

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
            return
        if self.plan_rows:
            self._check_space()

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

    def _mark_db_line(self):
        self.db_text.tag_remove("current_db", "1.0", "end")
        line = self.db_text.index("insert").split(".")[0]
        if self.db_text.get(f"{line}.0", f"{line}.end").strip():
            self.db_text.tag_add("current_db", f"{line}.0", f"{line}.end+1c")

    def _move_db(self, delta):
        """Swap the source on the cursor's line with the one above (-1) or below (+1).
        Blank lines are dropped so the list order is exactly the priority order."""
        cur = self.db_text.get(f"insert linestart", f"insert lineend").strip()
        names = [l.strip() for l in self.db_text.get("1.0", "end").splitlines() if l.strip()]
        if not cur or cur not in names:
            return
        i = names.index(cur)
        j = i + delta
        if not 0 <= j < len(names):
            return
        names[i], names[j] = names[j], names[i]
        self.db_text.delete("1.0", "end")
        self.db_text.insert("1.0", "\n".join(names))
        self.db_text.mark_set("insert", f"{j + 1}.0")
        self.db_text.see("insert")
        self.db_text.focus_set()
        self._mark_db_line()

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
        self.runner = runner = Runner(cfg, self.queue)
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
        self.db_off.clear()
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
        chosen = [s for s in self.plan_sources if s not in self.db_off]
        if not chosen:
            messagebox.showerror(TITLE, "Tick at least one database (click its row, left of the table list).")
            return
        p = plan_copy(self.checked, chosen, self.plan_rows)
        if p["errors"]:
            messagebox.showerror(TITLE, "These databases must be copied in list order, because the first one wins "
                                        "when two hold the same record:\n\n  " + "\n  ".join(p["errors"][:12])
                                 + (f"\n  ... and {len(p['errors']) - 12} more" if len(p["errors"]) > 12 else "")
                                 + "\n\nTick those databases too, or copy them first. To use a different order, change "
                                   "the Source databases list and run step 1 again."
                                 + ("\n\nA ticked table with nothing new to copy would be emptied and reloaded, so even a "
                                    "database already copied can be listed here. To copy only what is new, click "
                                    "Tick not loaded." if p["reload"] else ""))
            return
        if not p["copies"]:
            messagebox.showerror(TITLE, "None of the ticked tables are in the ticked databases.")
            return
        lines = [f"Copy data of {len(p['copies'])} table(s) from: " + ", ".join(
            s for s in chosen if any(s in v for v in p["copies"].values()))]
        lines.append("\nDatabases a table was already copied from are not copied again, unless it is reloaded below.")
        lines.append("\nEach database is committed on its own, in list order. If one fails, the ones before it stay "
                     "and you can carry on from there.")
        if p["added"]:
            lines.append("\nAdded automatically (needed): " + ", ".join(sorted(p["added"])))
        if p["not_there"]:
            lines.append("\nNot in the ticked databases, so not copied: " + ", ".join(sorted(p["not_there"])))
        if p["reload"]:
            lines.append("\nRELOAD - nothing new to copy for these, so they are emptied and copied again:\n  "
                         + ", ".join(sorted(p["reload"]))
                         + "\nOther tables with a foreign key to them (e.g. MetadataHorizontal) are emptied too.")
        if p["lost"]:
            lines.append("\nRows REMOVED by the reload, because these databases are not ticked (copy them again later):\n  "
                         + "\n  ".join(f"{t}: {', '.join(v)}" for t, v in p["lost"].items()))
        short = False
        if self.space:
            _, detail, short = self._space_report(p["copies"])
            lines.append("\n" + "\n".join(detail))
        else:
            lines.append("\nDisk space not checked yet: click Check space to see it first.")
        lines.append("\nContinue?")
        if not messagebox.askyesno(TITLE, "\n".join(lines), icon="warning" if p["reload"] or short else "question",
                                   default="no" if short else "yes"):
            return
        self._launch("step2", p["copies"], sorted(p["reload"]), p["holds"], cfg=cfg)

    def _recheck(self):
        if self.running:
            return
        try:
            cfg = self._config()
        except ValueError as e:
            messagebox.showerror(TITLE, str(e))
            return
        ticked = set(self.checked)  # a job that ends OK clears the table ticks; this one copies nothing

        def finished():
            self.checked |= ticked

        self.after_done = finished
        self._launch("recheck", cfg=cfg)

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

    def _verify_counts(self):
        if self.running:
            return
        try:
            cfg = self._config()
        except ValueError as e:
            messagebox.showerror(TITLE, str(e))
            return
        ticked = set(self.checked)  # read-only job: keep the table ticks

        def finished():
            self.checked |= ticked

        self.after_done = finished
        self._launch("verify_counts", cfg=cfg)

    def _restore_skipped(self):
        if self.running:
            return
        try:
            cfg = self._config()
        except ValueError as e:
            messagebox.showerror(TITLE, str(e))
            return
        if not messagebox.askyesno(TITLE, f"Insert the duplicate files that were skipped into {cfg['target']}?\n\n"
                                          "Every copy of a duplicate file is added, with its Metadata, Permissions and "
                                          "MigrationTasks, tagged with its source_db. Nothing already copied is deleted "
                                          "or changed. Files.OriginalSharePointPath is no longer unique afterwards "
                                          "(the unique key becomes a plain index).\n\n"
                                          "If Finish already removed the sources, the tables needed are linked / "
                                          "copied again first."):
            return
        self._launch("restore_skipped", cfg=cfg)

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
                elif kind == "space":
                    seq, space = data
                    if seq == self.space_seq:
                        self.space = space
                        self._show_sizes()
                        self._update_space()
                        d = space["disk"]
                        self.log.insert("end", "Space check: " + (f"{fmt_size(d['free'])} free, read through {d['how']}"
                                        if d["free"] is not None else f"free disk not detected: {d['why']}")
                                        + "".join(f"\n  sizes not read from {n}: {e}" for n, e in space["errors"].items())
                                        + "\n")
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
                elif kind in ("table_list", "table_list_failed"):
                    self.tables_btn.configure(state="normal")
                    self.status.configure(text="Running..." if self.running else f"psql: {find_psql()}")
                    if kind == "table_list":
                        self._show_table_picker(*data)
                    else:
                        messagebox.showerror(TITLE, data)
                elif kind == "indexes":
                    show, win, rows = data
                    if win.winfo_exists():  # the window may have been closed meanwhile
                        show(rows)
                elif kind in ("info", "error"):
                    if not self.running:
                        self.status.configure(text=f"psql: {find_psql()}")
                    (messagebox.showinfo if kind == "info" else messagebox.showerror)(TITLE, data)
                elif kind == "step":
                    self.status.configure(text=f"Running: {data}")
                elif kind == "mu_row":
                    self._mu_set(*data)
                elif kind == "mu_scan":
                    self._mu_show(*data)
                elif kind == "done":
                    self.running = False
                    if self.mu_active:
                        self.mu_active = False
                        self.mu_stop_btn.configure(state="disabled")
                        self.after(200, self._mu_refresh)
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
    import multiprocessing
    multiprocessing.freeze_support()  # "Build upload files" runs worker processes (the exe starts itself again)
    App().mainloop()
