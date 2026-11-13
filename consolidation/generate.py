"""
Generate the consolidation SQL for merging the MTR Phase 3 source databases
into one target database.

    python generate.py            # reads sources.json + each source's schema
                                  # writes sql/01_attach_sources.sql
                                  #        sql/02_preflight.sql
                                  #        sql/03_merge.sql
                                  #        sql/03_prepare.sql   (two-step mode, step 1)
                                  #        sql/05_verify.sql
                                  #        sql/06_detach_sources.sql

00_target_schema.sql and 04_finalize.sql are hand-written and static.

Why generated: the 9 sources are at different schema versions (cwp2/cwp/xrl
lack several columns and tables). Each source's foreign tables only expose the
columns that source really has, so every INSERT ... SELECT needs an explicit,
per-source column list. The generator reads the schema of each source and the
target schema, and fails loudly if a source column has no home in the target
(i.e. data would be lost).

Source schema: from ../pgdump_schema/<dump> when the source has a "dump"
entry, otherwise read live from the database with psql. A source can be just
its database name:  "sources": ["cwp_ecr", "cwp_opspj3", ...]
Live mode (CLI) uses env: MTR_PSQL (default psql), MTR_SRC_USER, MTR_SRC_PASSWORD,
MTR_SCHEMA_HOST / MTR_SCHEMA_PORT (connect here instead of a "localhost" source
host), MTR_DOCKER (run psql inside this container).

Tables with a rule in LOAD_ORDER are merged by those rules. Any other table
(except bak_*) is copied as-is into a generated table: union of its columns
over all sources plus a source tag column; no de-duplication.

"skip_tables" in sources.json leaves tables out as if no source had them: nothing is
linked, staged or copied. A copied-as-is table is then not created either; a table
with merge rules still exists, empty, because 00_target_schema.sql and
04_finalize.sql are static (its keys, triggers and view need it).

Staging layout ("Through this PC": MtrDbMigration, run_consolidation.ps1 -Staging):
for a login that may only create tables in schema public. No postgres_fdw and no
new schemas: every source table is copied by the client into a staging table
public."src_<source>__<table>" (see stage() / copy_to_stage()), and the _merge
schema becomes tables public._merge_* (flatten_merge()). The merge SQL is the same.

    python generate.py --staging          # writes every script, static ones included,
                                          # rewritten for staging into sql_staging/
    python generate.py --staging --copy   # copy the sources into the staging tables
                                          # (after sql_staging/01_attach_sources.sql);
                                          # target from env MTR_TARGET_DB, MTR_TARGET_USER,
                                          # MTR_TARGET_HOST, MTR_TARGET_PORT, PGPASSWORD,
                                          # PGOPTIONS / PGKRBSRVNAME (target only)
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
DUMP_DIR = os.path.join(HERE, "..", "pgdump_schema")
SQL_DIR = os.path.join(HERE, "sql")

INT_MAX = 2147483647

# ---------------------------------------------------------------------------
# Mapping rules
# ---------------------------------------------------------------------------

# ID domains: surrogate keys that are re-numbered with a per-source offset.
# domain -> list of (table, column) whose values live in that domain. The
# offset is computed from the MAX over ALL these columns so that dangling
# references (e.g. MigrationTasks_Parsed rows for deleted tasks) still can't
# collide with another source's range.
ID_DOMAINS = {
    "Files": [("Files", "FileId"), ("Metadata", "FileId"), ("Permissions", "FileId"),
              ("MigrationTasks", "FileId"), ("MigrationTasks_Parsed", "FileId"),
              ("ScanProgress", "LastResolvedFileId")],
    "Metadata": [("Metadata", "MetadataId")],
    "Permissions": [("Permissions", "PermissionId")],
    "ADGroups": [("ADGroups", "ADGroupId"), ("ADGroupMembers", "ADGroupId")],
    "ADGroupMembers": [("ADGroupMembers", "MemberId")],
    "PermissionSets": [("PermissionSets", "PermissionSetId"), ("PermissionSetEntries", "PermissionSetId")],
    "PermissionSetEntries": [("PermissionSetEntries", "PermissionSetEntryId")],
    "FolderPermissions": [("FolderPermissions", "FolderPermissionId")],
    "PermissionMatrixSummaries": [("PermissionMatrixSummaries", "PermissionMatrixSummaryId")],
    "MigrationTasks": [("MigrationTasks", "TaskId"), ("MigrationTasks_Parsed", "TaskId"),
                       ("NewChangePath_NEW", "TaskId"), ("Migration_Path_History", "TaskId"),
                       ("break_path_mapping", "task_id")],
    "ScanProgress": [("ScanProgress", "ScanProgressId")],
    "FolderInheritanceBreaks": [("FolderInheritanceBreaks", "FolderInheritanceBreakId")],
    "Migration_Operation_Logs": [("Migration_Operation_Logs", "OpId"), ("Migration_Path_History", "OpId")],
    "Migration_Path_History": [("Migration_Path_History", "HistoryId")],
    "PostgreSQL_PermissionMatrix_20260503": [("PostgreSQL_PermissionMatrix_20260503", "ID")],
    "PostgreSQL_SiteGroupPermission_20260503": [("PostgreSQL_SiteGroupPermission_20260503", "ID")],
    "nas_permission_matrix": [("nas_permission_matrix", "id")],
    "sg_permission_matrix": [("sg_permission_matrix", "id")],
}
# Columns protected by a real FK in every source are <= the parent's MAX, so
# they are skipped when computing the offset (avoids full scans of big tables).
FK_BOUNDED = {("Metadata", "FileId"), ("Permissions", "FileId"), ("MigrationTasks", "FileId"),
              ("ADGroupMembers", "ADGroupId"), ("PermissionSetEntries", "PermissionSetId"),
              ("Migration_Path_History", "OpId")}
# (table, column) -> domain   (built from ID_DOMAINS)
COL_DOMAIN = {tc: d for d, tcs in ID_DOMAINS.items() for tc in tcs}

# Duplicate files ARE skipped: one copy per OriginalSharePointPath is kept. A Completed
# copy (MigrationStatus = 2) wins over one that is not; otherwise the source listed first
# wins. The losing copy is skipped together with its Metadata, Permissions and
# MigrationTasks (and the task rows of MigrationTasks_Parsed, NewChangePath_NEW,
# break_path_mapping), all logged in _merge.conflict_log. See replace_files().
# Every other table is NOT de-duplicated: all rows are loaded, tagged with source_db.
# Natural keys that can then repeat (ADGroups.GroupName, ScanProgress (LibraryName,
# SiteName), ActiveLanIds.LanId) have no unique key until 07_restore_unique.sql.

# Rows dropped because a row they belong to was skipped:
# table -> list of (table whose skipped rows are in conflict_log, column here holding its old id)
SKIP_CASCADE = {
    "Files": [("Files", "FileId")],
    "Metadata": [("Files", "FileId")],
    "Permissions": [("Files", "FileId")],
    "MigrationTasks": [("MigrationTasks", "TaskId")],
    "MigrationTasks_Parsed": [("MigrationTasks", "TaskId")],
    "NewChangePath_NEW": [("MigrationTasks", "TaskId")],
    "break_path_mapping": [("MigrationTasks", "task_id")],
}
# 08_restore_skipped.sql: the tables that lost rows to a skipped file, parents first.
RESTORE_TABLES = ["Files", "Metadata", "Permissions", "MigrationTasks",
                  "MigrationTasks_Parsed", "NewChangePath_NEW", "break_path_mapping"]
# Tables whose sources must be copied in sources.json order: between two copies that are
# both Completed (or both not), the first source wins.
ORDERED_TABLES = {"Files"}

# Values for target columns that a (older) source does not have.
ABSENT_DEFAULTS = {
    ("Files", "HasUniquePermissions"): "false",
    # Old sources (cwp2/cwp/xrl) predate the post-scan resolution pass: their
    # Permissions rows were written per file at scan time. Marking them
    # resolved stops the app from re-resolving every file on startup (which
    # would add duplicate Inherited permission rows and block NTFS apply).
    ("ScanProgress", "PermissionsResolved"): "true",
    ("ScanProgress", "LastResolvedFileId"): "NULL",
    ("NewChangePath_NEW", "ExtendFolder"): "NULL",
}

# Tables with GENERATED ALWAYS identity in the target.
IDENTITY_ALWAYS = {"PostgreSQL_PermissionMatrix_20260503", "PostgreSQL_SiteGroupPermission_20260503"}

# Load order (parents before children). Tables not listed are not migrated.
LOAD_ORDER = [
    "Files", "Metadata", "Permissions", "MigrationTasks",
    "ADGroups", "ADGroupMembers",
    "PermissionSets", "PermissionSetEntries", "FolderPermissions", "PermissionMatrixSummaries",
    "ScanProgress", "FolderInheritanceBreaks", "ActiveLanIds",
    "Migration_Operation_Logs", "Migration_Path_History",
    "MigrationTasks_Parsed", "NewChangePath_NEW", "Folder_Structure_Final_Analysis",
    "PostgreSQL_PermissionMatrix_20260503", "PostgreSQL_SiteGroupPermission_20260503",
    "Folders", "break_path_mapping", "nas_permission_matrix", "sg_permission_matrix", "sitemapping",
]

# Every migrated table gets a source tag column (value = source name). Sites holds a
# list of every source that has the site ("cwp_ecr, cwp_opspj"); see setup_parts().
SOURCE_TAG = {t: "source_db" for t in LOAD_ORDER}
SOURCE_TAG.update({"PostgreSQL_PermissionMatrix_20260503": "SourceDb",
                   "PostgreSQL_SiteGroupPermission_20260503": "SourceDb"})

# Source tables intentionally NOT migrated.
EXCLUDED = re.compile(r"^bak_")

# Two-step mode: a table can only be loaded with (or after) the tables it needs:
# its FK parents (04_finalize.sql) and the parents in SKIP_CASCADE, whose skipped
# rows decide which of its rows drop.
DEPENDS = {
    "Metadata": ["Files"], "Permissions": ["Files"], "MigrationTasks": ["Files"],
    "ADGroupMembers": ["ADGroups"], "PermissionSetEntries": ["PermissionSets"],
    "Migration_Path_History": ["Migration_Operation_Logs"],
    "MigrationTasks_Parsed": ["MigrationTasks"], "NewChangePath_NEW": ["MigrationTasks"],
    "break_path_mapping": ["MigrationTasks"],
}


# Two-step mode: which source each table was copied from (step 2 can copy one source at a time).
LOADED_SOURCE_DDL = ("CREATE TABLE _merge.loaded_source (\n    table_name text NOT NULL REFERENCES _merge.table_plan,\n"
                     "    source_db varchar(64) NOT NULL REFERENCES _merge.source,\n"
                     "    loaded_at timestamptz NOT NULL DEFAULT now(),\n"
                     "    PRIMARY KEY (table_name, source_db)\n);\n")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_tables(sql_text):
    """Return {table: [(column, definition), ...]} from pg_dump-style CREATE TABLEs."""
    tables = {}
    for m in re.finditer(r'CREATE (?:UNLOGGED )?TABLE public\.("?\w+"?) \((.*?)\n\);', sql_text, re.S):
        name = m.group(1).strip('"')
        cols = []
        for line in m.group(2).strip().split("\n"):
            line = line.strip().rstrip(",")
            cm = re.match(r'("?\w+"?) (.*)', line)
            if cm:
                cols.append((cm.group(1).strip('"'), cm.group(2)))
        tables[name] = cols
    return tables


SCHEMA_QUERY = """SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, d.adrelid IS NOT NULL
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition
ORDER BY c.relname, a.attnum"""
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def parse_schema_rows(text):
    """psql -tA -F<tab> output of SCHEMA_QUERY -> {table: [(column, definition), ...]}."""
    tables = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        rel, col, typ, notnull, hasdef = line.split("\t")
        defn = typ + (" NOT NULL" if notnull == "t" else "") + (" DEFAULT" if hasdef == "t" else "")
        tables.setdefault(rel, []).append((col, defn))
    return tables


def psql_schema_loader(cmd_for):
    """cmd_for(source) -> (argv up to and incl. psql connection args, env). Returns a loader."""
    def load(s):
        argv, env = cmd_for(s)
        # search_path '' makes format_type schema-qualify user types (enums...), see col_type().
        r = subprocess.run(argv + ["-X", "-w", "-q", "-tA", "-F", "\t", "-v", "ON_ERROR_STOP=1",
                                   "-c", "SET search_path = ''", "-c", SCHEMA_QUERY],
                           env=env, capture_output=True, stdin=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            err = r.stderr.decode("utf-8", errors="replace").strip()
            sys.exit(f"cannot read schema of {s['name']} (db {s['dbname']}): {err}")
        return parse_schema_rows(r.stdout.decode("utf-8", errors="replace"))
    return load


def cli_schema_loader():
    """Live loader for `python generate.py`, configured from MTR_* env vars."""
    return psql_schema_loader(cli_source_cmd())


def cli_source_cmd():
    """cmd_for(source) -> (psql argv, env) from MTR_* env vars (CLI)."""
    user = os.environ.get("MTR_SRC_USER", "postgres")
    env = dict(os.environ, PGPASSWORD=os.environ.get("MTR_SRC_PASSWORD", os.environ.get("PGPASSWORD", "")),
               PGCLIENTENCODING="UTF8")
    env.pop("PGOPTIONS", None)  # the target's role (run_consolidation.ps1 -Role) is not for the sources
    if not env["PGPASSWORD"]:
        del env["PGPASSWORD"]  # no password: Kerberos/SSPI or trust decides
    docker = os.environ.get("MTR_DOCKER")

    def cmd_for(s):
        host, port = s["host"], s["port"]
        if docker:
            return ["docker", "exec", "-i", "-e", "PGPASSWORD=" + env.get("PGPASSWORD", ""), docker, "psql",
                    "-h", host, "-p", str(port), "-U", user, "-d", s["dbname"]], env
        if host in LOCAL_HOSTS and os.environ.get("MTR_SCHEMA_HOST"):
            host = os.environ["MTR_SCHEMA_HOST"]
            port = os.environ.get("MTR_SCHEMA_PORT", port)
        return [os.environ.get("MTR_PSQL", "psql"), "-h", host, "-p", str(port), "-U", user, "-d", s["dbname"]], env
    return cmd_for


def user_type_base(defn):
    """Column type from a definition, as written."""
    return re.split(r" (?:NOT NULL|DEFAULT|GENERATED|COLLATE)\b", defn, maxsplit=1)[0].strip()


def col_type(defn):
    """Column type from a definition; user-defined (schema-qualified) types become text."""
    t = user_type_base(defn)
    return "text" if "." in t.split("(")[0] else t


def source_name(dbname):
    return re.sub(r"[^a-z0-9_]", "_", dbname.lower())


def q(ident):
    return '"' + ident.replace('"', '""') + '"'


def lit(s):
    return "'" + str(s).replace("'", "''") + "'"


def flatten_merge(sql_text):
    """Staging layout: schema _merge -> tables public._merge_* (no CREATE SCHEMA needed)."""
    sql_text = re.sub(r"^CREATE SCHEMA _merge;\n", "", sql_text, flags=re.M)
    return re.sub(r"\b_merge\.", "public._merge_", sql_text)


def stage_table(source, table):
    """Staging table name for a source table; over 63 bytes it is cut and given a hash."""
    name = f"src_{source}__{table}"
    if len(name.encode("utf-8")) > 63:
        h = hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
        name = name.encode("utf-8")[:54].decode("utf-8", errors="ignore") + "_" + h
    return name


# ---------------------------------------------------------------------------
# SQL builders
# ---------------------------------------------------------------------------

class Gen:
    def __init__(self, cfg, schema_loader=None, staging=False):
        self.cfg = cfg
        self.staging = staging
        self.sources = []
        self.skip = set(cfg.get("skip_tables") or [])
        found = set()
        for s in cfg["sources"]:
            s = {"dbname": s} if isinstance(s, str) else dict(s)
            if not s.get("enabled", True):
                continue
            s.setdefault("name", source_name(s["dbname"]))
            s.setdefault("host", cfg.get("source_host", "localhost"))
            s.setdefault("port", cfg.get("source_port", 5432))
            self.sources.append(s)
        if not self.sources:
            sys.exit("no enabled sources in sources.json")
        names = [s["name"] for s in self.sources]
        if len(set(names)) != len(names):
            sys.exit("duplicate source names in sources.json")
        for s in self.sources:
            if not re.match(r"^[a-z0-9_]+$", s["name"]):
                sys.exit(f"source name must be [a-z0-9_]: {s['name']}")
            s["schema"] = "src_" + s["name"]
            s["server"] = "srv_" + s["name"]
            if s.get("dump"):
                with open(os.path.join(DUMP_DIR, s["dump"]), encoding="utf-8") as f:
                    tables = parse_tables(f.read())
            else:
                if schema_loader is None:
                    sys.exit(f"{s['name']}: no \"dump\" file and no live connection to read its schema")
                tables = schema_loader(s)
            found.update(tables)
            s["tables"] = {t: dict(c) for t, c in tables.items() if t not in self.skip}
        with open(os.path.join(SQL_DIR, "00_target_schema.sql"), encoding="utf-8") as f:
            self.target = parse_tables(f.read())
        self.build_generic()
        self.check_coverage()
        self.check_skip(found)

    # -- tables without a rule: copied as-is --------------------------------
    def build_generic(self):
        """self.generic: {table: {"cols": [(col, type)], "tag": tag column}} for every
        non-excluded source table not in LOAD_ORDER. Column types that differ between
        sources become text."""
        self.generic = {}
        errors = []
        for s in self.sources:
            for t, cols in s["tables"].items():
                if EXCLUDED.match(t) or t in LOAD_ORDER:
                    continue
                if t in self.target:
                    errors.append(f"{s['name']}.{t}: name clashes with a target table that has no merge rule")
                    continue
                g = self.generic.setdefault(t, {"types": {}})
                for c, d in cols.items():
                    g["types"].setdefault(c, set()).add(col_type(d))
        if errors:
            sys.exit("Mapping errors:\n  " + "\n  ".join(errors))
        for t, g in self.generic.items():
            g["cols"] = [(c, next(iter(ts)) if len(ts) == 1 else "text") for c, ts in g["types"].items()]
            g["tag"] = next(n for n in ("source_db", "_source_db", "__source_db") if n not in g["types"])

    def generic_create(self, t):
        g = self.generic[t]
        cols = [f"{q(c)} {typ}" for c, typ in g["cols"]] + [f"{q(g['tag'])} text NOT NULL"]
        return (f"-- copied as-is from: " + ", ".join(s["name"] for s in self.sources if self.has(s, t))
                + f"\nCREATE TABLE public.{q(t)} (\n    " + ",\n    ".join(cols) + "\n);\n"
                f"CREATE INDEX ON public.{q(t)} ({q(g['tag'])});")

    def generic_insert(self, s, t):
        g = self.generic[t]
        exprs = []
        for c, typ in g["cols"]:
            if not self.has(s, t, c):
                exprs.append("NULL")
            elif col_type(s["tables"][t][c]) != typ:
                exprs.append(f"s.{q(c)}::{typ}")
            else:
                exprs.append(f"s.{q(c)}")
        cols = [q(c) for c, _ in g["cols"]] + [q(g["tag"])]
        return (f"INSERT INTO public.{q(t)} (\n    " + ", ".join(cols) + "\n)\nSELECT\n    "
                + ",\n    ".join(exprs + [lit(s["name"])]) + f"\nFROM {self.ft(s, t)} s;")

    # -- validation ---------------------------------------------------------
    def check_coverage(self):
        errors = []
        for s in self.sources:
            for t, cols in s["tables"].items():
                if EXCLUDED.match(t) or t in self.generic:
                    continue
                tcols = dict(self.target[t])
                for c in cols:
                    if c not in tcols:
                        errors.append(f"{s['name']}.{t}.{c}: column missing in target schema (data loss)")
        for t in LOAD_ORDER:
            if t not in self.target:
                errors.append(f"{t}: in LOAD_ORDER but not in 00_target_schema.sql")
                continue
            for s in self.sources:
                if t not in s["tables"]:
                    continue
                for c, d in self.target[t]:
                    if c in s["tables"][t] or c == SOURCE_TAG.get(t):
                        continue
                    if (t, c) not in ABSENT_DEFAULTS and "NOT NULL" in d and "DEFAULT" not in d:
                        errors.append(f"{s['name']}.{t}.{c}: NOT NULL target column absent in source and no default rule")
        if errors:
            sys.exit("Mapping errors:\n  " + "\n  ".join(errors))

    def check_skip(self, found):
        """A table cannot be copied without the tables it needs (DEPENDS)."""
        if not self.plan_tables():
            sys.exit("skip_tables in sources.json leaves no table to migrate")
        errors = [f"{t} needs {p}, which is in skip_tables" for t in self.plan_tables()
                  for p in DEPENDS.get(t, []) if p in self.skip and p in found]
        if errors:
            sys.exit("Table selection errors:\n  " + "\n  ".join(errors))

    def has(self, s, table, col=None):
        if table not in s["tables"]:
            return False
        return col is None or col in s["tables"][table]

    def ft(self, s, table):
        if self.staging:
            return f"public.{q(stage_table(s['name'], table))}"
        return f"{q(s['schema'])}.{q(table)}"

    @staticmethod
    def off(domain, source):
        return f"(SELECT id_offset FROM _merge.id_offset WHERE table_name = {lit(domain)} AND source_db = {lit(source)})"

    # -- 01 attach ----------------------------------------------------------
    def attach(self):
        out = [HEADER.format(name="01_attach_sources.sql", what=
            "Attach every source DB through postgres_fdw as schema src_<name>.\n"
            "-- Run on the TARGET database. Requires psql variables:\n"
            "--   -v src_user=<login on source server> -v src_password=<password>\n"
            "-- (superusers connecting over trusted local auth may pass an empty password).\n"
            "-- Optional -v src_kerberos=true (superuser): no password in the user mapping and\n"
            "-- password_required off, so the source's pg_hba (sspi/gss, trust) decides."),
            "\\if :{?src_kerberos}\n\\else\n\\set src_kerberos false\n\\endif\n",
            "CREATE EXTENSION IF NOT EXISTS postgres_fdw;\n"]
        for s in self.sources:
            tables = [t for t in s["tables"] if not EXCLUDED.match(t)]
            out.append(f"-- {s['name']}  ({len(tables)} tables)")
            out.append(f"CREATE SERVER {q(s['server'])} FOREIGN DATA WRAPPER postgres_fdw\n"
                       f"    OPTIONS (host {lit(s['host'])}, port {lit(s['port'])}, dbname {lit(s['dbname'])}, fetch_size '50000');")
            out.append(f"\\if :src_kerberos\n"
                       f"CREATE USER MAPPING FOR CURRENT_USER SERVER {q(s['server'])}\n"
                       f"    OPTIONS (user :'src_user', password_required 'false');\n"
                       f"\\else\n"
                       f"CREATE USER MAPPING FOR CURRENT_USER SERVER {q(s['server'])}\n"
                       f"    OPTIONS (user :'src_user', password :'src_password');\n"
                       f"\\endif")
            out.append(f"CREATE SCHEMA {q(s['schema'])};")
            # IMPORT fails on user-defined column types (enums, domains) that don't exist
            # here, so those tables are declared by hand with such columns read as text.
            custom = [t for t in sorted(tables) if any(col_type(d) != user_type_base(d) for d in s["tables"][t].values())]
            plain = [t for t in sorted(tables) if t not in custom]
            if plain:
                out.append(f"IMPORT FOREIGN SCHEMA public LIMIT TO (\n    "
                           + ",\n    ".join(q(t) for t in plain)
                           + f"\n) FROM SERVER {q(s['server'])} INTO {q(s['schema'])};")
            for t in custom:
                cols = [f"{q(c)} {col_type(d)} OPTIONS (column_name {lit(c)})" for c, d in s["tables"][t].items()]
                out.append(f"CREATE FOREIGN TABLE {self.ft(s, t)} (\n    " + ",\n    ".join(cols)
                           + f"\n) SERVER {q(s['server'])} OPTIONS (schema_name 'public', table_name {lit(t)});")
            out.append(f"INSERT INTO _merge.source (source_db, load_order, foreign_schema) VALUES "
                       f"({lit(s['name'])}, {self.sources.index(s) + 1}, {lit(s['schema'])});\n")
        return "\n".join(out)

    # -- 01 staging layout: tables the client fills with COPY -------------------
    def staged_tables(self, s):
        return [t for t in sorted(s["tables"]) if not EXCLUDED.match(t)]

    def stage(self):
        out = [HEADER.format(name="01_attach_sources.sql", what=
            "Staging layout: one table public.\"src_<source>__<table>\" per source table.\n"
            "-- MtrDbMigration fills them through the client (COPY source TO STDOUT | COPY target FROM STDIN).\n"
            "-- User-defined column types (enums, domains) are stored as text."),
            "CREATE TABLE _merge.stage (\n    source_db   varchar(64) NOT NULL REFERENCES _merge.source,\n"
            "    table_name  text NOT NULL,\n    stage_table text NOT NULL UNIQUE,\n    row_count   bigint,\n"
            "    PRIMARY KEY (source_db, table_name)\n);\n"]
        seen = {}
        for i, s in enumerate(self.sources, 1):
            tables = self.staged_tables(s)
            out.append(f"-- {s['name']}  ({len(tables)} tables)")
            out.append(f"INSERT INTO _merge.source (source_db, load_order, foreign_schema) VALUES "
                       f"({lit(s['name'])}, {i}, 'public');")
            for t in tables:
                st = stage_table(s["name"], t)
                if st in seen:
                    sys.exit(f"staging table name {st} is used by {seen[st]} and {s['name']}.{t}")
                seen[st] = f"{s['name']}.{t}"
                cols = [f"{q(c)} {col_type(d)}" for c, d in s["tables"][t].items()]
                out.append(f"CREATE UNLOGGED TABLE public.{q(st)} (\n    " + ",\n    ".join(cols) + "\n);")
                out.append(f"INSERT INTO _merge.stage (source_db, table_name, stage_table) VALUES "
                           f"({lit(s['name'])}, {lit(t)}, {lit(st)});")
            out.append("")
        return "\n".join(out)

    def stage_copies(self, only=None):
        """[(source, table, staging table, COPY ... TO STDOUT for the source, COPY ... FROM STDIN for the target)]
        `only`: just these staging tables."""
        out = []
        for s in self.sources:
            for t in self.staged_tables(s):
                cols = s["tables"][t]
                sel = ", ".join(q(c) + ("::text" if col_type(d) != user_type_base(d) else "") for c, d in cols.items())
                st = stage_table(s["name"], t)
                if only is not None and st not in only:
                    continue
                out.append((s, t, st, f"COPY (SELECT {sel} FROM public.{q(t)}) TO STDOUT",
                            f"COPY public.{q(st)} (" + ", ".join(q(c) for c in cols) + ") FROM STDIN"))
        return out

    def stage_counts_sql(self, counts):
        """After copy_to_stage(): record the row counts, then ANALYZE the staging tables."""
        if not counts:
            return ""
        return ("UPDATE _merge.stage g SET row_count = v.n FROM (VALUES\n    "
                + ",\n    ".join(f"({lit(st)}, {n})" for st, n in counts) + "\n) v(st, n) WHERE g.stage_table = v.st;\n"
                + "ANALYZE " + ",\n    ".join(f"public.{q(st)}" for st, _ in counts) + ";\n")

    def detach(self):
        if self.staging:
            out = [HEADER.format(name="06_detach_sources.sql", what=
                "Drop the staging copies of the sources once 05_verify.sql is clean. Keeps the _merge audit tables."),
                "SET client_min_messages = warning;"]
            out.extend(f"DROP TABLE IF EXISTS public.{q(stage_table(s['name'], t))};"
                       for s in self.sources for t in self.staged_tables(s))
            return "\n".join(out) + "\n"
        out = [HEADER.format(name="06_detach_sources.sql", what=
            "Remove the postgres_fdw links once 05_verify.sql is clean. Keeps the _merge audit schema."),
            "SET client_min_messages = warning;"]
        for s in self.sources:
            out.append(f"DROP SCHEMA IF EXISTS {q(s['schema'])} CASCADE;")
            out.append(f"DROP SERVER IF EXISTS {q(s['server'])} CASCADE;")
        out.append("DROP EXTENSION IF EXISTS postgres_fdw;")
        return "\n".join(out) + "\n"

    # -- offsets ------------------------------------------------------------
    def max_expr(self, s, domain):
        parts = [f"(SELECT MAX({q(c)}) FROM {self.ft(s, t)})"
                 for t, c in ID_DOMAINS[domain] if self.has(s, t, c) and (t, c) not in FK_BOUNDED]
        if not parts:
            return None
        return "COALESCE(GREATEST(" + ", ".join(parts) + "), 0)" if len(parts) > 1 else f"COALESCE({parts[0]}, 0)"

    def offsets_sql(self, into_table=True):
        out = []
        for domain in ID_DOMAINS:
            rows = []
            for i, s in enumerate(self.sources, 1):
                e = self.max_expr(s, domain)
                if e:
                    rows.append(f"    ({lit(s['name'])}, {i}, {e}::bigint)")
            if not rows:
                continue
            sel = (f"SELECT {lit(domain)}, src, COALESCE(SUM(mx) OVER (ORDER BY ord ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0), mx\n"
                   f"FROM (VALUES\n" + ",\n".join(rows) + "\n) v(src, ord, mx)")
            if into_table:
                out.append(f"INSERT INTO _merge.id_offset (table_name, source_db, id_offset, src_max_id)\n{sel};\n")
            else:
                out.append(sel)
        return out

    # -- column expressions -------------------------------------------------
    def select_list(self, s, table, alias="s"):
        cols, exprs = [], []
        for c, d in self.target[table]:
            cols.append(q(c))
            if c == SOURCE_TAG.get(table):
                exprs.append(lit(s["name"]))
            elif c == "SiteId":
                exprs.append('st."SiteId"')  # via SITE_JOIN
            elif not self.has(s, table, c):
                exprs.append(ABSENT_DEFAULTS.get((table, c), "DEFAULT" if "DEFAULT" in d else "NULL"))
            elif (table, c) in COL_DOMAIN:
                exprs.append(f"{alias}.{q(c)} + {self.off(COL_DOMAIN[(table, c)], s['name'])}")
            else:
                exprs.append(f"{alias}.{q(c)}")
        return cols, exprs

    def has_site_id(self, table):
        return any(c == "SiteId" for c, _ in self.target[table])

    def site_tables(self):
        return [t for t in self.target if t != "Sites" and self.has_site_id(t)]

    def skip_where(self, s, table, alias="s", restore=False):
        """Conditions that leave out the rows of `table` belonging to a skipped file / task.
        restore=True: the opposite, only those rows (08_restore_skipped.sql)."""
        conds = [f"EXISTS (SELECT 1 FROM _merge.conflict_log c WHERE c.table_name = {lit(parent)} "
                 f"AND c.source_db = {lit(s['name'])} AND c.action = 'skipped' AND c.old_id = {alias}.{q(col)})"
                 for parent, col in SKIP_CASCADE.get(table, []) if self.has(s, table, col)]
        if restore:
            return ["(" + " OR ".join(conds) + ")"] if conds else ["false"]
        return ["NOT " + c for c in conds]

    def insert(self, s, table, restore=False):
        """The rows of `table` in source `s`, IDs shifted by the source's offset, without
        the rows of skipped duplicate files (SKIP_CASCADE). restore=True: only those rows,
        and only if `table` was already copied from `s` (08_restore_skipped.sql)."""
        extra_from = SITE_JOIN if self.has_site_id(table) else ""
        cols, exprs = self.select_list(s, table)
        if "DEFAULT" in exprs:  # can't use DEFAULT inside SELECT: drop those columns instead
            keep = [i for i, e in enumerate(exprs) if e != "DEFAULT"]
            cols, exprs = [cols[i] for i in keep], [exprs[i] for i in keep]
        overriding = " OVERRIDING SYSTEM VALUE" if table in IDENTITY_ALWAYS else ""
        where = self.skip_where(s, table, restore=restore)
        if restore:
            where.append(f"EXISTS (SELECT 1 FROM restore_scope r WHERE r.table_name = {lit(table)} "
                         f"AND r.source_db = {lit(s['name'])})")
        return (f"INSERT INTO public.{q(table)} (\n    " + ", ".join(cols) + f"\n){overriding}\nSELECT\n    "
                + ",\n    ".join(exprs) + f"\nFROM {self.ft(s, table)} s{extra_from}"
                + ("\nWHERE " + "\n  AND ".join(where) if where else "")
                + ("\nON CONFLICT DO NOTHING" if restore else "") + ";")

    def log(self, table, s, old_id, natural_key, winner_src, winner_old, reason, from_sql, where=""):
        return (f"INSERT INTO _merge.conflict_log (table_name, source_db, old_id, natural_key, winner_source_db, winner_old_id, action, reason)\n"
                f"SELECT {lit(table)}, {lit(s['name'])}, {old_id}, {natural_key}, {winner_src}, {winner_old}, 'skipped',\n"
                f"       {lit(reason)}\n{from_sql}" + (f"\nWHERE {where}" if where else "") + ";")

    def replace_files(self, s):
        """A Completed copy (MigrationStatus = 2) beats a copy that is not: files of an earlier
        source that this source has Completed while the loaded copy is not are taken out again,
        with every row hanging off them, and logged as skipped (winner = this source). Their
        rows not loaded yet are then skipped by SKIP_CASCADE, as for any skipped file."""
        n, ft = s["name"], self.ft(s, "Files")
        r = f"_merge_replace_{n}"
        tasks = f'SELECT t."TaskId" FROM public."MigrationTasks" t JOIN {r} r ON t."FileId" = r.new_id'
        return [
            f"-- {n}: its Completed copies replace loaded copies that are not Completed",
            f"CREATE TEMP TABLE {r} ON COMMIT DROP AS\n"
            f'SELECT t."FileId" AS new_id, t.source_db AS old_src, _merge.old_id_of(\'Files\', t."FileId") AS old_id,\n'
            f'       t."OriginalSharePointPath" AS path, s."FileId" AS winner_old_id\n'
            f'FROM {ft} s JOIN public."Files" t ON t."OriginalSharePointPath" = s."OriginalSharePointPath"\n'
            f'WHERE s."MigrationStatus" = 2 AND t."MigrationStatus" IS DISTINCT FROM 2;',
            f"INSERT INTO _merge.conflict_log (table_name, source_db, old_id, natural_key, winner_source_db, winner_old_id, action, reason)\n"
            f"SELECT 'Files', old_src, old_id, path, {lit(n)}, winner_old_id, 'skipped',\n"
            f"       'Not Completed, and the copy in {n} is Completed: replaced by it; its Metadata, Permissions and MigrationTasks are removed too'\n"
            f"FROM {r};",
            "-- copies skipped earlier in favour of a replaced row now point to the new winner",
            f"UPDATE _merge.conflict_log c SET winner_source_db = {lit(n)}, winner_old_id = r.winner_old_id\n"
            f"FROM {r} r WHERE c.table_name = 'Files' AND c.natural_key = r.path\n"
            f"  AND c.winner_source_db = r.old_src AND c.winner_old_id = r.old_id;",
            f"INSERT INTO _merge.conflict_log (table_name, source_db, old_id, natural_key, winner_source_db, winner_old_id, action, reason)\n"
            f"SELECT 'MigrationTasks', t.source_db, _merge.old_id_of('MigrationTasks', t.\"TaskId\"), t.\"ServerRelativeUrl\", NULL, NULL, 'skipped',\n"
            f"       'Task belongs to a Files row replaced by a Completed copy'\n"
            f'FROM public."MigrationTasks" t JOIN {r} r ON t."FileId" = r.new_id;',
            f'DELETE FROM public."MigrationTasks_Parsed" WHERE "TaskId" IN ({tasks});',
            f'DELETE FROM public."NewChangePath_NEW" WHERE "TaskId" IN ({tasks});',
            f"DELETE FROM public.break_path_mapping WHERE task_id IN ({tasks});",
            f'DELETE FROM public."MigrationTasks" WHERE "FileId" IN (SELECT new_id FROM {r});',
            f'DELETE FROM public."Metadata" WHERE "FileId" IN (SELECT new_id FROM {r});',
            f'DELETE FROM public."Permissions" WHERE "FileId" IN (SELECT new_id FROM {r});',
            f'DELETE FROM public."Files" WHERE "FileId" IN (SELECT new_id FROM {r});',
        ]

    # -- per-table merge ----------------------------------------------------
    def merge_table(self, s, table):
        n, ft = s["name"], self.ft(s, table)
        parts = []
        if table == "Files":
            if self.has(s, "Files", "MigrationStatus"):
                parts.extend(self.replace_files(s))
            parts.append(self.log("Files", s, 's."FileId"', 's."OriginalSharePointPath"',
                                  't.source_db', '_merge.old_id_of(\'Files\', t."FileId")',
                                  "OriginalSharePointPath already loaded (Completed, or neither copy Completed and that source is listed earlier); "
                                  "its Metadata, Permissions and MigrationTasks are skipped too",
                                  f'FROM {ft} s JOIN public."Files" t ON t."OriginalSharePointPath" = s."OriginalSharePointPath"'))
        elif table == "MigrationTasks":
            parts.append(self.log("MigrationTasks", s, 's."TaskId"', 's."ServerRelativeUrl"', "NULL", "NULL",
                                  "Task belongs to a skipped duplicate Files row", f"FROM {ft} s",
                                  f"""EXISTS (SELECT 1 FROM _merge.conflict_log c WHERE c.table_name = 'Files' AND c.source_db = {lit(n)} AND c.action = 'skipped' AND c.old_id = s."FileId")"""))
        parts.append(self.insert(s, table))
        return parts

    def merge(self):
        out = [HEADER.format(name="03_merge.sql", what=
            "Copy all rows from the attached sources into the target (run after 01, review 02 first).\n"
            "-- Single transaction: any error rolls back everything; rerun on a fresh target.")]
        out.append("\\set ON_ERROR_STOP on\n\\timing on\nBEGIN;\n")
        out.extend(self.setup_parts())
        for table in LOAD_ORDER + sorted(self.generic):
            out.extend(self.table_parts(table))
        out.append("COMMIT;\n")
        return "\n".join(out)

    # -- 08: put the skipped duplicate files back ---------------------------
    def restore_needs(self):
        """[(source, table)] the restore reads: the tables SKIP_CASCADE filters."""
        return [(s, t) for t in RESTORE_TABLES for s in self.sources if self.has(s, t)]

    def restore_skipped(self):
        """Insert every row the merge skipped as a duplicate file (and its Metadata, Permissions,
        MigrationTasks and their children), with the same new IDs it would have had. Nothing
        already in the target is changed or deleted; only the unique key on
        Files.OriginalSharePointPath is replaced by a plain index, since the same path is now
        held once per source. Re-runnable: restored rows are marked 'restored' in conflict_log."""
        needs = self.restore_needs()
        out = [HEADER.format(name="08_restore_skipped.sql", what=
            "Re-insert the duplicate files skipped by the merge, with their Metadata, Permissions,\n"
            "-- MigrationTasks (+ MigrationTasks_Parsed, NewChangePath_NEW, break_path_mapping).\n"
            "-- Nothing already in the target is deleted or changed. The sources (links or staging\n"
            "-- copies) must still be there. Every table and source commits on its own, so a run that\n"
            "-- stops (e.g. disk full) keeps what it did: run it again and it carries on from there.")]
        out.append("\\set ON_ERROR_STOP on\n\\pset footer off\n\\timing on\n")
        vac = ", ".join([f"public.{q(t)}" for t in dict.fromkeys(t for _, t in needs)] + ["_merge.conflict_log"])
        out.append("-- give back the space of an earlier run that failed (its rolled-back rows)")
        out.append(f"VACUUM {vac};\n")
        out.append("\\echo '=== A. Rows skipped by the merge, to re-insert now ==='")
        out.append("SELECT table_name, source_db, count(*) AS skipped_rows FROM _merge.conflict_log\n"
                   "WHERE action = 'skipped' GROUP BY 1, 2 ORDER BY 1, 2;\n")
        missing = ",\n    ".join(f"({lit(self.ft(s, t))})" for s, t in needs)
        out.append("DO $$\nDECLARE m text;\nBEGIN\n"
                   f"  SELECT string_agg(t, ', ') INTO m FROM (VALUES\n    {missing}\n  ) v(t) WHERE to_regclass(t) IS NULL;\n"
                   "  IF m IS NOT NULL THEN\n"
                   "    RAISE EXCEPTION 'The source tables are not reachable (links / staging copies removed?): %', m;\n"
                   "  END IF;\nEND $$;\n")
        out.append("-- which tables were copied from which source: only those get their skipped rows back\n"
                   "-- (a table copied later takes them anyway, because they are no longer marked 'skipped')")
        pairs = ",\n        ".join(f"({lit(t)}, {lit(s['name'])})" for s, t in needs)
        out.append("CREATE TEMP TABLE restore_scope (table_name text, source_db text);\n"
                   "DO $$\nBEGIN\n  IF to_regclass('_merge.loaded_source') IS NOT NULL THEN\n"
                   "    INSERT INTO restore_scope SELECT table_name, source_db FROM _merge.loaded_source;\n"
                   "  ELSE  -- built by the one-shot 03_merge.sql: every table from every source\n"
                   f"    INSERT INTO restore_scope VALUES\n        {pairs};\n  END IF;\nEND $$;\n")
        out.append("-- the same path is now held once per source: unique key -> plain index")
        out.append('ALTER TABLE public."Files" DROP CONSTRAINT IF EXISTS "Files_OriginalSharePointPath_key";')
        out.append('CREATE INDEX IF NOT EXISTS "IX_Files_OriginalSharePointPath" ON public."Files" '
                   'USING btree ("OriginalSharePointPath");\n')
        for t in RESTORE_TABLES:
            srcs = [s for s, tt in needs if tt == t]
            if not srcs:
                continue
            out.append(f"-- ===== {t} " + "=" * max(3, 70 - len(t)))
            for s in srcs:
                out.append(f"\\echo '{t} <- {s['name']} (skipped rows)'")
                out.append(self.insert(s, t, restore=True) + "\n")
        out.append("-- mark what is back (only after every child table is in); a row the source no longer\n"
                   "-- has stays 'skipped'. One source at a time, with a VACUUM in between, so each\n"
                   "-- UPDATE reuses the space the previous one freed.")
        for t, idcol in (("Files", "FileId"), ("MigrationTasks", "TaskId")):
            for s in self.sources:
                out.append(f"\\echo 'mark {t} of {s['name']} as restored'")
                out.append(f"UPDATE _merge.conflict_log c SET action = 'restored'\n"
                           f"WHERE c.action = 'skipped' AND c.table_name = {lit(t)} AND c.source_db = {lit(s['name'])} AND EXISTS (\n"
                           f"    SELECT 1 FROM public.{q(t)} x\n"
                           f"    WHERE x.{q(idcol)} = c.old_id + (SELECT id_offset FROM _merge.id_offset o WHERE o.table_name = {lit(t)} AND o.source_db = {lit(s['name'])})\n"
                           f"      AND x.source_db = c.source_db);")
            out.append("VACUUM _merge.conflict_log;\n")
        out.append("\\echo '=== B. Result ==='")
        out.append("SELECT table_name, action, count(*) FROM _merge.conflict_log GROUP BY 1, 2 ORDER BY 1, 2;")
        out.append("SELECT count(*) AS paths_held_more_than_once, sum(n) AS file_rows_with_those_paths\n"
                   'FROM (SELECT count(*) AS n FROM public."Files" GROUP BY "OriginalSharePointPath" HAVING count(*) > 1) d;\n')
        out.append("\\echo 'Done. Rows still marked skipped in B were not found in their source: their source table (staging copy) may be empty.'")
        return "\n".join(out)

    def restage_sql(self, tables):
        """Staging layout, after Finish dropped the copies: re-create the staging tables of
        `tables` ({staging table name}) so copy_to_stage() can fill them again."""
        out = []
        for s in self.sources:
            for t in self.staged_tables(s):
                st = stage_table(s["name"], t)
                if st in tables:
                    cols = [f"{q(c)} {col_type(d)}" for c, d in s["tables"][t].items()]
                    out.append(f"CREATE UNLOGGED TABLE IF NOT EXISTS public.{q(st)} (\n    " + ",\n    ".join(cols) + "\n);")
        return "\n".join(out) + "\n"

    # -- two-step mode (MtrDbMigration): prepare once, then load chosen tables ----
    def plan_tables(self):
        """Tables to offer for loading, in load order (parents first)."""
        return [t for t in LOAD_ORDER if any(self.has(s, t) for s in self.sources)] + sorted(self.generic)

    def prepare(self):
        out = [HEADER.format(name="03_prepare.sql", what=
            "Step 1 of the two-step mode: everything 03_merge.sql does except copying rows.\n"
            "-- Creates the copied-as-is tables, the ID offsets, Sites and the load plan.")]
        out.append("\\set ON_ERROR_STOP on\n\\timing on\nBEGIN;\n")
        out.append("CREATE TABLE _merge.table_plan (\n    table_name text PRIMARY KEY,\n    load_order int NOT NULL,\n"
                   "    kind text NOT NULL,\n    depends_on text[] NOT NULL,\n    sources text NOT NULL,\n    source_rows bigint\n);")
        out.append("CREATE TABLE _merge.loaded_table (\n    table_name text PRIMARY KEY REFERENCES _merge.table_plan,\n"
                   "    loaded_at timestamptz NOT NULL DEFAULT now(),\n    row_count bigint NOT NULL\n);\n")
        out.append(LOADED_SOURCE_DDL)
        out.extend(self.setup_parts())
        out.append("-- new sites added later by the set_site_id() trigger must not reuse an ID")
        out.append("SELECT setval('public.\"Sites_SiteId_seq\"', COALESCE((SELECT MAX(\"SiteId\") FROM public.\"Sites\"), 0) + 1, false);\n")
        out.append("-- ===== Load plan (source_rows counted on the sources now) ===============")
        rows = []
        for i, t in enumerate(self.plan_tables(), 1):
            srcs = [s for s in self.sources if self.has(s, t)]
            deps = [p for p in DEPENDS.get(t, []) if p in self.plan_tables()]
            rows.append(f"    ({lit(t)}, {i}, {lit('copied as-is' if t in self.generic else 'merge rules')}, "
                        f"ARRAY[{', '.join(lit(p) for p in deps)}]::text[], {lit(', '.join(s['name'] for s in srcs))}, "
                        + " + ".join(f"(SELECT COUNT(*) FROM {self.ft(s, t)})" for s in srcs) + ")")
        out.append("INSERT INTO _merge.table_plan (table_name, load_order, kind, depends_on, sources, source_rows) VALUES\n"
                   + ",\n".join(rows) + ";\n")
        out.append("COMMIT;\n")
        return "\n".join(out)

    def load_batches(self, copies, reload=()):
        """[(source name, SQL)], one transaction per source in priority order, for `copies`
        {table: source names} (a closed set: parents loaded or included, see
        migrate_app.plan_copy). A failure loses only the source it happens in, and each table
        records the sources it holds, so the next run carries on from there. Tables in `reload`
        are emptied (TRUNCATE ... CASCADE) with their merge log in the first batch, so its
        failure leaves them as they were; they may have nothing to copy."""
        batches, pending = [], set(reload)
        for s in self.sources:
            tables = [t for t in self.plan_tables() if s["name"] in copies.get(t, ())]
            if tables:
                batches.append((s["name"], self.load_batch(s, tables, pending)))
                pending = set()
        return batches

    def load_batch(self, s, tables, reload):
        """One source's tables, parents first, as one transaction, emptying `reload` first."""
        n = s["name"]
        out = [HEADER.format(name="load.sql", what=f"Step 2 of the two-step mode: copy from {n}: " + ", ".join(tables))]
        out.append("\\set ON_ERROR_STOP on\n\\timing on\nBEGIN;\n")
        if reload:
            rl = [t for t in self.plan_tables() if t in reload]
            out.append("-- ===== Reload: empty these tables first =====")
            out.append("TRUNCATE " + ", ".join(f"public.{q(t)}" for t in rl) + " CASCADE;")
            out.append("DELETE FROM _merge.conflict_log WHERE table_name IN (" + ", ".join(lit(t) for t in rl) + ");")
            out.append("DELETE FROM _merge.loaded_source WHERE table_name IN (" + ", ".join(lit(t) for t in rl) + ");")
            out.append("DELETE FROM _merge.loaded_table WHERE table_name IN (" + ", ".join(lit(t) for t in rl) + ");\n")
        for t in tables:
            out.extend(self.table_parts(t, [n]))
            out.append(f"INSERT INTO _merge.loaded_source (table_name, source_db) VALUES ({lit(t)}, {lit(n)});")
            out.append(f"INSERT INTO _merge.loaded_table (table_name, row_count) SELECT {lit(t)}, COUNT(*) FROM public.{q(t)}\n"
                       "ON CONFLICT (table_name) DO UPDATE SET row_count = EXCLUDED.row_count, loaded_at = now();\n")
        out.append("-- the next source's duplicate checks join against these tables: give the planner their new size")
        out.append("ANALYZE " + ", ".join(f"public.{q(t)}" for t in tables) + ";\n")
        out.append("COMMIT;\n")
        return "\n".join(out)

    def setup_parts(self):
        """Copied-as-is tables, ID offsets and Sites: everything before the first row copy."""
        out = []
        if self.generic:
            out.append("-- ===== 0. Tables without a merge rule: created from the sources' columns =====")
            out.extend(self.generic_create(t) + "\n" for t in sorted(self.generic))
        out.append("-- ===== 1. ID offsets ======================================================")
        out.extend(self.offsets_sql())
        out.append(OVERFLOW_GUARD)
        out.append("-- ===== 2. Sites: every distinct non-empty SiteName from every source table =====")
        out.append("-- (GROUP BY is pushed down to the source server, so only the distinct names travel)")
        out.append("-- source_db lists every source that has the site, in priority order")
        parts = [f'SELECT "SiteName" AS n, {i} AS o, {lit(s["name"])} AS src FROM {self.ft(s, t)} GROUP BY 1'
                 for t in self.site_tables() for i, s in enumerate(self.sources, 1) if self.has(s, t, "SiteName")]
        if parts:
            out.append('INSERT INTO public."Sites" ("SiteId", "SiteName", source_db)\n'
                       "SELECT row_number() OVER (ORDER BY n), n, string_agg(src, ', ' ORDER BY o) FROM (\n    "
                       + "\n    UNION ".join(parts)
                       + "\n) u WHERE n IS NOT NULL AND n <> '' GROUP BY n;\n")
        return out

    def table_parts(self, table, sources=None):
        """The rows of `table` from `sources` (names; default all), highest priority first."""
        chosen = [s for s in self.sources if sources is None or s["name"] in sources]
        out = []
        if table in self.generic:
            out.append(f"-- ===== {table} (copied as-is) " + "=" * max(3, 56 - len(table)))
            for s in chosen:
                if self.has(s, table):
                    out.append(f"\\echo '{table} <- {s['name']}'")
                    out.append(self.generic_insert(s, table) + "\n")
            return out
        srcs = [s for s in chosen if self.has(s, table)]
        out.append(f"-- ===== {table} " + "=" * max(3, 70 - len(table)))
        if not srcs:
            out.append("-- (no source has this table)\n")
            return out
        for s in srcs:
            out.append(f"\\echo '{table} <- {s['name']}'")
            out.extend(self.merge_table(s, table))
            out.append("")
        return out

    # -- 02 preflight -------------------------------------------------------
    def preflight(self):
        n_src = len(self.sources)
        out = [HEADER.format(name="02_preflight.sql", what=
            "READ-ONLY report. Run after 01_attach_sources.sql and BEFORE 03_merge.sql.\n"
            "-- Shows row counts, cross-source duplicates (C: skipped; D-H: all loaded) and\n"
            "-- whether the re-numbered IDs fit in integer.")]
        out.append("\\pset footer off\n")

        out.append("\\echo '=== A. Source row counts ==='")
        rows = []
        for s in self.sources:
            for t in LOAD_ORDER + sorted(self.generic):
                if self.has(s, t):
                    rows.append(f"SELECT {lit(s['name'])} AS source_db, {lit(t)} AS table_name, (SELECT COUNT(*) FROM {self.ft(s, t)}) AS row_count")
        if rows:
            out.append("SELECT * FROM (\n" + "\nUNION ALL ".join(rows) + "\n) x ORDER BY table_name, source_db;\n")
        if self.generic:
            out.append("\\echo '=== A2. Tables without a merge rule: copied as-is, tagged with the source, no de-duplication ==='")
            out.append("SELECT * FROM (VALUES\n    " + ",\n    ".join(
                f"({lit(t)}, {lit(', '.join(s['name'] for s in self.sources if self.has(s, t)))})" for t in sorted(self.generic))
                + "\n) x(table_name, sources);\n")

        rows = [f'SELECT DISTINCT {lit(s["name"])} AS source_db, "SiteName" FROM {self.ft(s, "Files")}'
                for s in self.sources if self.has(s, "Files")]
        if rows:
            out.append("\\echo '=== B. SiteName present in more than one source (Files) - main overlap signal ==='")
            out.append("SELECT \"SiteName\", string_agg(source_db, ', ' ORDER BY source_db) AS sources\nFROM (\n"
                       + "\nUNION ALL ".join(rows) + "\n) x GROUP BY \"SiteName\" HAVING COUNT(*) > 1 ORDER BY 1;\n")

        def overlap(title, table, keyexpr):
            srcs = [s for s in self.sources if self.has(s, table)]
            if len(srcs) < 2:
                return
            # Keys are hashed to a 16-byte uuid and grouped once (no self-join): on ~25M long
            # paths the old self-join spilled tens of GB to pgsql_tmp and filled the disk.
            rows = [f"SELECT DISTINCT {lit(s['name'])} AS source_db, md5({keyexpr})::uuid AS h FROM {self.ft(s, table)}"
                    for s in srcs]
            out.append(f"\\echo '=== {title} ==='")
            out.append("WITH g AS (\n  SELECT array_agg(source_db) AS srcs FROM (\n  "
                       + "\n  UNION ALL ".join(rows) + "\n  ) k WHERE h IS NOT NULL GROUP BY h HAVING COUNT(*) > 1\n)\n"
                       "SELECT sa.source_db, sb.source_db AS also_in, COUNT(*) AS duplicate_keys\n"
                       "FROM g CROSS JOIN LATERAL unnest(g.srcs) a(s) CROSS JOIN LATERAL unnest(g.srcs) b(s)\n"
                       "JOIN _merge.source sa ON sa.source_db = a.s\n"
                       "JOIN _merge.source sb ON sb.source_db = b.s AND sb.load_order > sa.load_order\n"
                       "GROUP BY 1, 2 ORDER BY 1, 2;\n")

        dup = " - ALL LOADED, clean up before 07_restore_unique.sql"
        overlap("C. Files.OriginalSharePointPath in more than one source - one copy is kept (Completed first, "
                "else the first source), the others are SKIPPED with their Metadata/Permissions/Tasks",
                "Files", '"OriginalSharePointPath"')
        overlap("D. ADGroups.GroupName in more than one source" + dup, "ADGroups", '"GroupName"')
        overlap("E. ScanProgress (LibraryName, SiteName) in more than one source" + dup, "ScanProgress",
                '"LibraryName" || \' | \' || "SiteName"')
        overlap("F. ActiveLanIds.LanId in more than one source" + dup, "ActiveLanIds", '"LanId"')
        overlap("G. sitemapping.sp2013_url in more than one source" + dup, "sitemapping", "sp2013_url")
        overlap("H. Folders.UniqueId in more than one source" + dup, "Folders", '"UniqueId"::text')

        sels = self.offsets_sql(into_table=False)
        if sels:
            out.append("\\echo '=== I. Projected ID ranges (must stay <= 2147483647) ==='")
            out.append("SELECT domain, source_db, id_offset + 1 AS first_new_id, id_offset + src_max_id AS last_new_id,\n"
                       f"       CASE WHEN id_offset + src_max_id > {INT_MAX} THEN 'OVERFLOW' ELSE 'ok' END AS status\nFROM (\n"
                       + "\nUNION ALL\n".join(sels) + "\n) x(domain, source_db, id_offset, src_max_id)\nORDER BY domain, id_offset;\n")

        rows = [f'SELECT {lit(s["name"])} AS source_db, COUNT(*) AS processing FROM {self.ft(s, "MigrationTasks")} WHERE "Status" = 1'
                for s in self.sources if self.has(s, "MigrationTasks", "Status")]
        if rows:
            out.append("\\echo '=== J. Tasks still Processing (Status=1) in sources - reset_stale_tasks() will requeue them ==='")
            out.append("SELECT * FROM (\n" + "\nUNION ALL ".join(rows) + "\n) x WHERE processing > 0;\n")
        return "\n".join(out)

    # -- 05 verify ----------------------------------------------------------
    def verify(self, tables=None):
        """All tables, or only `tables` {table: source names} (two-step mode: the tables just
        loaded, with every source each now holds)."""
        want = (lambda t, s: True) if tables is None else (lambda t, s: s["name"] in tables.get(t, ()))
        out = [HEADER.format(name="05_verify.sql", what=
            "Row-count reconciliation: every source row is in the target (tagged with its source_db)\n"
            "-- or belongs to a duplicate file skipped in _merge.conflict_log.\n"
            "-- Run after 04_finalize.sql while the sources are still attached.")]
        out.append("\\pset footer off\n")
        rows = []
        for t in LOAD_ORDER + sorted(self.generic):
            tag = q(self.generic[t]["tag"] if t in self.generic else SOURCE_TAG[t])
            for s in self.sources:
                if self.has(s, t) and want(t, s):
                    cnt = f"(SELECT COUNT(*) FROM {self.ft(s, t)})"
                    skip = self.skip_where(s, t, alias="x")
                    exp = f"(SELECT COUNT(*) FROM {self.ft(s, t)} x WHERE " + " AND ".join(skip) + ")" if skip else cnt
                    rows.append(f"SELECT {lit(t)} AS table_name, {lit(s['name'])} AS source_db, {cnt} AS source_rows, "
                                f"{exp} AS expected, (SELECT COUNT(*) FROM public.{q(t)} WHERE {tag} = {lit(s['name'])}) AS loaded")
        out.append("CREATE TEMP TABLE verify_result AS\nSELECT *, source_rows - expected AS skipped_duplicates,\n"
                   "       CASE WHEN expected = loaded THEN 'OK' ELSE 'MISMATCH' END AS status\nFROM (\n"
                   + "\nUNION ALL\n".join(rows) + "\n) v;\n")
        out.append("\\echo '=== Row reconciliation ==='")
        out.append("SELECT * FROM verify_result ORDER BY status DESC, table_name, source_db;\n")
        out.append("\\echo '=== SiteId check: every non-empty SiteName must carry the matching SiteId ==='")
        rows = [f"SELECT {lit(t)} AS table_name, COUNT(*) FILTER (WHERE x.\"SiteName\" <> '') AS rows_with_site,\n"
                f"       COUNT(*) FILTER (WHERE x.\"SiteName\" <> '' AND (st.\"SiteName\" IS DISTINCT FROM x.\"SiteName\")) AS wrong_site_id\n"
                f"FROM public.{q(t)} x LEFT JOIN public.\"Sites\" st ON st.\"SiteId\" = x.\"SiteId\""
                for t in self.site_tables()]
        out.append("CREATE TEMP TABLE verify_site AS\n" + "\nUNION ALL\n".join(rows) + ";")
        out.append("SELECT * FROM verify_site ORDER BY table_name;\n")
        out.append("DO $$\nBEGIN\n  IF EXISTS (SELECT 1 FROM verify_site WHERE wrong_site_id > 0) THEN\n"
                   "    RAISE EXCEPTION 'VERIFY FAILED: SiteId does not match SiteName (see above)';\n  END IF;\nEND $$;\n")
        out.append("\\echo '=== Skipped duplicate files and tasks ==='")
        out.append("SELECT table_name, source_db, COUNT(*) FROM _merge.conflict_log GROUP BY 1, 2 ORDER BY 1, 2;\n")
        out.append("DO $$\nBEGIN\n  IF EXISTS (SELECT 1 FROM verify_result WHERE status <> 'OK') THEN\n"
                   "    RAISE EXCEPTION 'VERIFY FAILED: see MISMATCH rows above';\n  END IF;\n"
                   "  RAISE NOTICE 'VERIFY OK: every source row is loaded or skipped as a duplicate file';\nEND $$;\n")
        return "\n".join(out)


HEADER = """-- =============================================================================
-- {name}  —  GENERATED by generate.py from sources.json; do not edit by hand.
-- {what}
-- =============================================================================
"""

SITE_JOIN = '\n  LEFT JOIN public."Sites" st ON st."SiteName" = s."SiteName"'

OVERFLOW_GUARD = f"""DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT * FROM _merge.id_offset WHERE id_offset + src_max_id > {INT_MAX} LOOP
    RAISE EXCEPTION 'ID overflow: % from % would reach %', r.table_name, r.source_db, r.id_offset + r.src_max_id;
  END LOOP;
END $$;
"""


def _pump(src, dst):
    """Copy COPY-text data from src to dst in chunks. A raw Ctrl-Z (0x1A) in the data is
    written as the COPY escape \\x1a: psql on Windows reads its stdin in text mode, where
    Ctrl-Z means end of file, so the row would be cut off there. Lossless: COPY decodes
    \\x1a back to the same byte, and every real backslash is already escaped as \\\\."""
    try:
        while True:
            chunk = src.read(1 << 20)
            if not chunk:
                break
            dst.write(chunk.replace(b"\x1a", b"\\x1a"))
    except OSError:
        pass  # the target psql stopped (it reports why); the source then fails too
    finally:
        for f in (dst, src):
            try:
                f.close()
            except OSError:
                pass


def _feed(dst, data):
    """Write `data` to a process's stdin, then close it (in a thread: it may be read slowly)."""
    try:
        dst.write(data)
    except OSError:
        pass  # the process stopped (it reports why)
    finally:
        try:
            dst.close()
        except OSError:
            pass


def _copy_one(source_cmd, target_cmd, s, t, st, out_sql, in_sql, feed=None):
    """Pipe one source query into a staging table through this client: source psql
    COPY ... TO STDOUT -> _pump -> target psql COPY ... FROM STDIN. With `feed` (bytes),
    the source psql runs that script from stdin instead of `out_sql`. Returns the row count."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    src_argv, src_env = source_cmd(s)
    dst_argv, dst_env = target_cmd()
    src = subprocess.Popen(src_argv + ["-X", "-w", "-q", "-v", "ON_ERROR_STOP=1"]
                           + (["-f", "-"] if feed is not None else ["-c", out_sql]),
                           env=src_env, creationflags=flags,
                           stdin=subprocess.PIPE if feed is not None else subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    dst = subprocess.Popen(dst_argv + ["-X", "-w", "-v", "ON_ERROR_STOP=1", "-c", in_sql],
                           env=dst_env, creationflags=flags, stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    src_err = []
    threads = [threading.Thread(target=_pump, args=(src.stdout, dst.stdin), daemon=True),
               threading.Thread(target=lambda: src_err.append(src.stderr.read()), daemon=True)]
    if feed is not None:
        threads.append(threading.Thread(target=_feed, args=(src.stdin, feed), daemon=True))
    for th in threads:
        th.start()
    dst_out = dst.stdout.read().decode("utf-8", errors="replace").strip()
    dst.wait()
    if dst.returncode != 0:
        src.kill()  # the target gave up: don't keep the source streaming
    src.wait()
    for th in threads:
        th.join()
    m = re.search(r"^COPY (\d+)$", dst_out, re.M)
    if src.returncode != 0 or dst.returncode != 0 or not m:
        err = b"".join(src_err).decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"copy {s['name']}.{t} -> {st} failed:\n{err}\n{dst_out}".rstrip())
    return int(m.group(1))


def copy_to_stage(g, source_cmd, target_cmd, log=print, progress=None, only=None):
    """Staging layout: pipe every source table (or `only` these staging tables) into its
    staging table through this client (source psql COPY ... TO STDOUT -> _pump -> target
    psql COPY ... FROM STDIN). source_cmd(s) and target_cmd() -> (psql argv up to the
    connection args, env). Returns [(staging table, rows)]; raises RuntimeError on the first failure."""
    copies = g.stage_copies(only)
    counts = []
    for i, (s, t, st, out_sql, in_sql) in enumerate(copies, 1):
        if progress:
            progress(i, len(copies), s, t)
        n = _copy_one(source_cmd, target_cmd, s, t, st, out_sql, in_sql)
        counts.append((st, n))
        log(f"{s['name']}.{t}: {n:,} rows")
    log(f"staged {len(copies)} tables, {sum(n for _, n in counts):,} rows")
    return counts


def copy_ids_to_stage(g, source_cmd, target_cmd, items, log=print, progress=None):
    """Like copy_to_stage(), but only some rows: `items` is [(source, table, id column, ids)]
    with ids as COPY text (one integer per line). In one session on the source, the ids go
    into a temporary table, then only the rows whose id column is in it are copied.
    Returns [(staging table, rows)]."""
    copies = {(s["name"], t): (st, out_sql, in_sql) for s, t, st, out_sql, in_sql in
              g.stage_copies({stage_table(s["name"], t) for s, t, _, _ in items})}
    counts = []
    for i, (s, t, col, ids) in enumerate(items, 1):
        if progress:
            progress(i, len(items), s, t)
        st, out_sql, in_sql = copies[(s["name"], t)]
        sel = out_sql[len("COPY (SELECT "):out_sql.index(f" FROM public.{q(t)})")]
        ids = ids if not ids or ids.endswith("\n") else ids + "\n"
        feed = ("CREATE TEMP TABLE _ids (id bigint PRIMARY KEY);\nCOPY _ids FROM STDIN;\n" + ids + "\\.\n"
                "ANALYZE _ids;\n"
                f"COPY (SELECT {sel} FROM public.{q(t)} WHERE {q(col)} IN (SELECT id FROM _ids)) TO STDOUT;\n")
        n = _copy_one(source_cmd, target_cmd, s, t, st, None, in_sql, feed=feed.encode("utf-8"))
        counts.append((st, n))
        log(f"{s['name']}.{t}: {n:,} rows (only the skipped ones, {ids.count(chr(10)):,} ids)")
    return counts


def cli_target_cmd():
    """Target connection for `python generate.py --staging --copy` (MTR_TARGET_* env;
    PGPASSWORD, PGOPTIONS (role) and PGKRBSRVNAME as run_consolidation.ps1 sets them)."""
    user, db = os.environ.get("MTR_TARGET_USER", "postgres"), os.environ["MTR_TARGET_DB"]
    env = dict(os.environ, PGCLIENTENCODING="UTF8")
    docker = os.environ.get("MTR_DOCKER")
    if docker:
        opts = ["-e", "PGOPTIONS=" + env["PGOPTIONS"]] if env.get("PGOPTIONS") else []
        return lambda: (["docker", "exec", "-i", "-e", "PGPASSWORD=" + env.get("PGPASSWORD", ""), *opts, docker, "psql",
                         "-U", user, "-d", db], env)
    return lambda: ([os.environ.get("MTR_PSQL", "psql"), "-h", os.environ.get("MTR_TARGET_HOST", "localhost"),
                     "-p", os.environ.get("MTR_TARGET_PORT", "5432"), "-U", user, "-d", db], env)


def main(schema_loader=None, staging=False, out_dir=None):
    """Write the scripts to out_dir (default sql/). The CLI's --staging writes to
    sql_staging/ and also copies the static scripts there, rewritten for staging."""
    with open(os.path.join(HERE, "sources.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    out_dir = out_dir or SQL_DIR
    g = Gen(cfg, schema_loader or cli_schema_loader(), staging=staging)
    files = {
        "01_attach_sources.sql": g.stage() if staging else g.attach(),
        "02_preflight.sql": g.preflight(),
        "03_merge.sql": g.merge(),
        "03_prepare.sql": g.prepare(),
        "05_verify.sql": g.verify(),
        "06_detach_sources.sql": g.detach(),
        "08_restore_skipped.sql": g.restore_skipped(),
    }
    if out_dir != SQL_DIR:
        os.makedirs(out_dir, exist_ok=True)
        for name in sorted(os.listdir(SQL_DIR)):  # static scripts (00, 04, 04b) as well
            if name.endswith(".sql") and name not in files:
                with open(os.path.join(SQL_DIR, name), encoding="utf-8") as f:
                    files[name] = f.read()
    for name, text in files.items():
        with open(os.path.join(out_dir, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(flatten_merge(text) if staging else text)
        print(f"wrote {os.path.basename(out_dir)}/{name}")
    print("sources:", ", ".join(f"{i}.{s['name']}" + ("" if s.get("dump") else " (live schema)")
                                for i, s in enumerate(g.sources, 1)))
    if g.generic:
        print("copied as-is (no merge rule):", ", ".join(sorted(g.generic)))
    if g.skip:
        print("skipped (skip_tables):", ", ".join(sorted(g.skip)))
    return g


if __name__ == "__main__":
    if "--staging" not in sys.argv:
        main()
    else:
        out = os.path.join(HERE, "sql_staging")
        gen = main(staging=True, out_dir=out)
        if "--copy" in sys.argv:
            target = cli_target_cmd()
            try:
                counts = copy_to_stage(gen, cli_source_cmd(), target)
            except RuntimeError as e:
                sys.exit(str(e))
            path = os.path.join(out, "01b_stage_counts.sql")
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(flatten_merge(gen.stage_counts_sql(counts)))
            argv, env = target()
            r = subprocess.run(argv + ["-X", "-w", "-q", "-v", "ON_ERROR_STOP=1", "-f", path], env=env)
            if r.returncode != 0:
                sys.exit("recording the staging row counts failed")
