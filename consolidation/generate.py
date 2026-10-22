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
"""
import json
import os
import re
import subprocess
import sys

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
# ADGroupMembers.ADGroupId is remapped through _merge.adgroup_map instead (dedup by GroupName).
del COL_DOMAIN[("ADGroupMembers", "ADGroupId")]

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

# Non-app tables get a source tag column (value = source name).
SOURCE_TAG = {
    "PostgreSQL_PermissionMatrix_20260503": "SourceDb",
    "PostgreSQL_SiteGroupPermission_20260503": "SourceDb",
    "Folders": "source_db",
    "break_path_mapping": "source_db",
    "nas_permission_matrix": "source_db",
    "sg_permission_matrix": "source_db",
    "sitemapping": "source_db",
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

# Source tables intentionally NOT migrated.
EXCLUDED = re.compile(r"^bak_")

# Child rows dropped because their parent row was skipped:
# table -> list of (parent conflict table, child column)
SKIP_CASCADE = {
    "Files": [("Files", "FileId")],
    "Metadata": [("Files", "FileId")],
    "Permissions": [("Files", "FileId")],
    "MigrationTasks": [("MigrationTasks", "TaskId")],
    "ADGroupMembers": [("ADGroupMembers", "MemberId")],
    "ScanProgress": [("ScanProgress", "ScanProgressId")],
    "MigrationTasks_Parsed": [("MigrationTasks", "TaskId")],
    "NewChangePath_NEW": [("MigrationTasks", "TaskId")],
    "break_path_mapping": [("MigrationTasks", "task_id")],
}

# Two-step mode: a table can only be loaded with (or after) the tables it needs:
# FK parents (04_finalize.sql), ADGroups for the group map, and the parents in
# SKIP_CASCADE / FolderInheritanceBreaks whose skipped rows decide which rows drop.
DEPENDS = {
    "Metadata": ["Files"], "Permissions": ["Files"], "MigrationTasks": ["Files"],
    "ADGroupMembers": ["ADGroups"], "PermissionSetEntries": ["PermissionSets"],
    "Migration_Path_History": ["Migration_Operation_Logs"],
    "MigrationTasks_Parsed": ["MigrationTasks"], "NewChangePath_NEW": ["MigrationTasks"],
    "break_path_mapping": ["MigrationTasks"], "FolderInheritanceBreaks": ["ScanProgress"],
}


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
    user = os.environ.get("MTR_SRC_USER", "postgres")
    env = dict(os.environ, PGPASSWORD=os.environ.get("MTR_SRC_PASSWORD", os.environ.get("PGPASSWORD", "")),
               PGCLIENTENCODING="UTF8")
    docker = os.environ.get("MTR_DOCKER")

    def cmd_for(s):
        host, port = s["host"], s["port"]
        if docker:
            return ["docker", "exec", "-i", "-e", "PGPASSWORD=" + env["PGPASSWORD"], docker, "psql",
                    "-h", host, "-p", str(port), "-U", user, "-d", s["dbname"]], env
        if host in LOCAL_HOSTS and os.environ.get("MTR_SCHEMA_HOST"):
            host = os.environ["MTR_SCHEMA_HOST"]
            port = os.environ.get("MTR_SCHEMA_PORT", port)
        return [os.environ.get("MTR_PSQL", "psql"), "-h", host, "-p", str(port), "-U", user, "-d", s["dbname"]], env
    return psql_schema_loader(cmd_for)


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


# ---------------------------------------------------------------------------
# SQL builders
# ---------------------------------------------------------------------------

class Gen:
    def __init__(self, cfg, schema_loader=None):
        self.cfg = cfg
        self.sources = []
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
            s["tables"] = {t: dict(c) for t, c in tables.items()}
        with open(os.path.join(SQL_DIR, "00_target_schema.sql"), encoding="utf-8") as f:
            self.target = parse_tables(f.read())
        self.build_generic()
        self.check_coverage()

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

    def has(self, s, table, col=None):
        if table not in s["tables"]:
            return False
        return col is None or col in s["tables"][table]

    def ft(self, s, table):
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

    def detach(self):
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
            elif (table, c) == ("ADGroupMembers", "ADGroupId"):
                exprs.append("m.new_id")
            elif not self.has(s, table, c):
                exprs.append(ABSENT_DEFAULTS.get((table, c), "DEFAULT" if "DEFAULT" in d else "NULL"))
            elif (table, c) in COL_DOMAIN:
                exprs.append(f"{alias}.{q(c)} + {self.off(COL_DOMAIN[(table, c)], s['name'])}")
            else:
                exprs.append(f"{alias}.{q(c)}")
        return cols, exprs

    def skip_where(self, s, table, alias="s"):
        conds = []
        for parent, col in SKIP_CASCADE.get(table, []):
            conds.append(f"NOT EXISTS (SELECT 1 FROM _merge.conflict_log c WHERE c.table_name = {lit(parent)} "
                         f"AND c.source_db = {lit(s['name'])} AND c.action = 'skipped' AND c.old_id = {alias}.{q(col)})")
        if table == "FolderInheritanceBreaks":
            conds.append(f"NOT EXISTS (SELECT 1 FROM _merge.conflict_log c WHERE c.table_name = 'ScanProgress' "
                         f"AND c.source_db = {lit(s['name'])} AND c.action = 'skipped' "
                         f"AND c.natural_key = {alias}.\"LibraryName\" || ' | ' || {alias}.\"SiteName\")")
        return conds

    def has_site_id(self, table):
        return any(c == "SiteId" for c, _ in self.target[table])

    def site_tables(self):
        return [t for t in self.target if t != "Sites" and self.has_site_id(t)]

    def insert(self, s, table, extra_from="", extra_where=(), on_conflict=""):
        if self.has_site_id(table):
            extra_from += SITE_JOIN
        cols, exprs = self.select_list(s, table)
        if "DEFAULT" in exprs:  # can't use DEFAULT inside SELECT: drop those columns instead
            keep = [i for i, e in enumerate(exprs) if e != "DEFAULT"]
            cols, exprs = [cols[i] for i in keep], [exprs[i] for i in keep]
        where = self.skip_where(s, table) + list(extra_where)
        overriding = " OVERRIDING SYSTEM VALUE" if table in IDENTITY_ALWAYS else ""
        sql = (f"INSERT INTO public.{q(table)} (\n    " + ", ".join(cols) + f"\n){overriding}\nSELECT\n    "
               + ",\n    ".join(exprs) + f"\nFROM {self.ft(s, table)} s{extra_from}")
        if where:
            sql += "\nWHERE " + "\n  AND ".join(where)
        if on_conflict:
            sql += "\n" + on_conflict
        return sql + ";"

    def log(self, table, s, old_id, natural_key, winner_src, winner_old, action, reason, from_sql, where=""):
        return (f"INSERT INTO _merge.conflict_log (table_name, source_db, old_id, natural_key, winner_source_db, winner_old_id, action, reason)\n"
                f"SELECT {lit(table)}, {lit(s['name'])}, {old_id}, {natural_key}, {winner_src}, {winner_old}, {lit(action)},\n"
                f"       {lit(reason)}\n{from_sql}" + (f"\nWHERE {where}" if where else "") + ";")

    # -- per-table merge ----------------------------------------------------
    def merge_table(self, s, table):
        n = s["name"]
        ft = self.ft(s, table)
        src_of = lambda dom, col: f"_merge.source_of({lit(dom)}, t.{q(col)})"
        old_of = lambda dom, col: f"_merge.old_id_of({lit(dom)}, t.{q(col)})"
        parts = []

        if table == "Files":
            parts.append(self.log("Files", s, 's."FileId"', 's."OriginalSharePointPath"',
                                  src_of("Files", "FileId"), old_of("Files", "FileId"), "skipped",
                                  "OriginalSharePointPath already loaded from a higher-priority source; its Metadata, Permissions and MigrationTasks are skipped too",
                                  f'FROM {ft} s JOIN public."Files" t ON t."OriginalSharePointPath" = s."OriginalSharePointPath"'))
            parts.append(self.insert(s, table))

        elif table == "MigrationTasks":
            parts.append(self.log("MigrationTasks", s, 's."TaskId"', 's."ServerRelativeUrl"', "NULL", "NULL", "skipped",
                                  "Task belongs to a skipped Files row",
                                  f"FROM {ft} s",
                                  f"""EXISTS (SELECT 1 FROM _merge.conflict_log c WHERE c.table_name = 'Files' AND c.source_db = {lit(n)} AND c.action = 'skipped' AND c.old_id = s."FileId")"""))
            parts.append(self.insert(s, table))

        elif table == "ADGroups":
            parts.append(self.log("ADGroups", s, 's."ADGroupId"', 's."GroupName"',
                                  src_of("ADGroups", "ADGroupId"), old_of("ADGroups", "ADGroupId"), "merged",
                                  "GroupName already loaded from a higher-priority source; ADGroupMembers re-pointed to the surviving group",
                                  f'FROM {ft} s JOIN public."ADGroups" t ON t."GroupName" = s."GroupName"'))
            parts.append(self.log("ADGroups", s, 's."ADGroupId"', 's."GroupName"',
                                  src_of("ADGroups", "ADGroupId"), old_of("ADGroups", "ADGroupId"), "modified",
                                  "GroupGuid already used by a different GroupName in the target; loaded with GroupGuid = NULL",
                                  f'FROM {ft} s JOIN public."ADGroups" t ON t."GroupGuid" = s."GroupGuid"',
                                  'NOT EXISTS (SELECT 1 FROM public."ADGroups" t2 WHERE t2."GroupName" = s."GroupName")'))
            cols, exprs = self.select_list(s, table)
            gi = cols.index(q("GroupGuid"))
            exprs[gi] = 'CASE WHEN EXISTS (SELECT 1 FROM public."ADGroups" t WHERE t."GroupGuid" = s."GroupGuid") THEN NULL ELSE s."GroupGuid" END'
            parts.append(f'INSERT INTO public."ADGroups" (\n    ' + ", ".join(cols) + "\n)\nSELECT\n    " + ",\n    ".join(exprs)
                         + f'\nFROM {ft} s{SITE_JOIN}\nWHERE NOT EXISTS (SELECT 1 FROM public."ADGroups" t WHERE t."GroupName" = s."GroupName");')
            parts.append(f"INSERT INTO _merge.adgroup_map (source_db, old_id, new_id)\n"
                         f'SELECT {lit(n)}, s."ADGroupId", t."ADGroupId" FROM {ft} s JOIN public."ADGroups" t ON t."GroupName" = s."GroupName";')

        elif table == "ADGroupMembers":
            join = f' JOIN _merge.adgroup_map m ON m.source_db = {lit(n)} AND m.old_id = s."ADGroupId"'
            parts.append(self.log("ADGroupMembers", s, 's."MemberId"', 's."UserPrincipalName"',
                                  src_of("ADGroupMembers", "MemberId"), old_of("ADGroupMembers", "MemberId"), "skipped",
                                  "User already a member of the merged group",
                                  f'FROM {ft} s{join}\n  JOIN public."ADGroupMembers" t ON t."ADGroupId" = m.new_id AND t."UserPrincipalName" = s."UserPrincipalName"'))
            parts.append(self.insert(s, table, extra_from=join))

        elif table == "ScanProgress":
            parts.append(self.log("ScanProgress", s, 's."ScanProgressId"', 's."LibraryName" || \' | \' || s."SiteName"',
                                  src_of("ScanProgress", "ScanProgressId"), old_of("ScanProgress", "ScanProgressId"), "skipped",
                                  "(LibraryName, SiteName) already loaded from a higher-priority source; FolderInheritanceBreaks of this library skipped too",
                                  f'FROM {ft} s JOIN public."ScanProgress" t ON t."LibraryName" = s."LibraryName" AND t."SiteName" = s."SiteName"'))
            parts.append(self.insert(s, table))

        elif table == "ActiveLanIds":
            parts.append(self.insert(s, table, on_conflict=
                'ON CONFLICT ("LanId") DO UPDATE SET "UploadedAt" = GREATEST(public."ActiveLanIds"."UploadedAt", EXCLUDED."UploadedAt")'))

        elif table in ("sitemapping", "Folders"):
            key = "sp2013_url" if table == "sitemapping" else "UniqueId"
            parts.append(self.log(table, s, "NULL", f"s.{q(key)}::text", "t.source_db", "NULL", "skipped",
                                  f"{key} already loaded from a higher-priority source",
                                  f"FROM {ft} s JOIN public.{q(table)} t ON t.{q(key)} = s.{q(key)}"))
            parts.append(self.insert(s, table, on_conflict=f"ON CONFLICT ({q(key)}) DO NOTHING"))

        else:
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

    def load_script(self, tables, reload=()):
        """Copy the rows of `tables` (a closed set: parents loaded or included). Tables in
        `reload` are emptied first (TRUNCATE ... CASCADE) together with their merge log."""
        order = [t for t in self.plan_tables() if t in tables]
        out = [HEADER.format(name="load.sql", what="Step 2 of the two-step mode: copy the rows of: " + ", ".join(order))]
        out.append("\\set ON_ERROR_STOP on\n\\timing on\nBEGIN;\n")
        if reload:
            rl = [t for t in order if t in reload]
            out.append("-- ===== Reload: empty these tables first =====")
            out.append("TRUNCATE " + ", ".join(f"public.{q(t)}" for t in rl) + " CASCADE;")
            out.append("DELETE FROM _merge.conflict_log WHERE table_name IN (" + ", ".join(lit(t) for t in rl) + ");")
            if "ADGroups" in rl:
                out.append("DELETE FROM _merge.adgroup_map;")
            out.append("DELETE FROM _merge.loaded_table WHERE table_name IN (" + ", ".join(lit(t) for t in rl) + ");\n")
        for t in order:
            out.extend(self.table_parts(t))
            out.append(f"INSERT INTO _merge.loaded_table (table_name, row_count) SELECT {lit(t)}, COUNT(*) FROM public.{q(t)};\n")
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
        parts = [f'SELECT "SiteName" AS n FROM {self.ft(s, t)} GROUP BY 1'
                 for t in self.site_tables() for s in self.sources if self.has(s, t, "SiteName")]
        if parts:
            out.append('INSERT INTO public."Sites" ("SiteId", "SiteName")\n'
                       "SELECT row_number() OVER (ORDER BY n), n FROM (\n    " + "\n    UNION ".join(parts)
                       + "\n) u WHERE n IS NOT NULL AND n <> '';\n")
        return out

    def table_parts(self, table):
        out = []
        if table in self.generic:
            out.append(f"-- ===== {table} (copied as-is) " + "=" * max(3, 56 - len(table)))
            for s in self.sources:
                if self.has(s, table):
                    out.append(f"\\echo '{table} <- {s['name']}'")
                    out.append(self.generic_insert(s, table) + "\n")
            return out
        srcs = [s for s in self.sources if self.has(s, table)]
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
            "-- Shows row counts, cross-source overlaps (which decide conflict handling) and\n"
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
            rows = [f"SELECT {lit(s['name'])} AS source_db, {keyexpr} AS k FROM {self.ft(s, table)}" for s in srcs]
            out.append(f"\\echo '=== {title} ==='")
            out.append("WITH k AS (\n" + "\nUNION ALL ".join(rows) + "\n)\n"
                       "SELECT a.source_db AS higher_priority, b.source_db AS lower_priority, COUNT(*) AS overlapping_keys\n"
                       "FROM k a JOIN k b ON a.k = b.k JOIN _merge.source sa ON sa.source_db = a.source_db\n"
                       "JOIN _merge.source sb ON sb.source_db = b.source_db AND sb.load_order > sa.load_order\n"
                       "GROUP BY 1, 2 ORDER BY 1, 2;\n")

        overlap("C. Files.OriginalSharePointPath overlaps (lower_priority rows will be SKIPPED with their Metadata/Permissions/Tasks)",
                "Files", '"OriginalSharePointPath"')
        overlap("D. ADGroups.GroupName overlaps (will be MERGED; members re-pointed)", "ADGroups", '"GroupName"')
        overlap("E. ScanProgress (LibraryName, SiteName) overlaps (lower_priority rows SKIPPED)", "ScanProgress",
                '"LibraryName" || \' | \' || "SiteName"')
        overlap("F. sitemapping.sp2013_url overlaps", "sitemapping", "sp2013_url")
        overlap("G. Folders.UniqueId overlaps", "Folders", '"UniqueId"::text')

        sels = self.offsets_sql(into_table=False)
        if sels:
            out.append("\\echo '=== H. Projected ID ranges (must stay <= 2147483647) ==='")
            out.append("SELECT domain, source_db, id_offset + 1 AS first_new_id, id_offset + src_max_id AS last_new_id,\n"
                       f"       CASE WHEN id_offset + src_max_id > {INT_MAX} THEN 'OVERFLOW' ELSE 'ok' END AS status\nFROM (\n"
                       + "\nUNION ALL\n".join(sels) + "\n) x(domain, source_db, id_offset, src_max_id)\nORDER BY domain, id_offset;\n")

        rows = [f'SELECT {lit(s["name"])} AS source_db, COUNT(*) AS processing FROM {self.ft(s, "MigrationTasks")} WHERE "Status" = 1'
                for s in self.sources if self.has(s, "MigrationTasks", "Status")]
        if rows:
            out.append("\\echo '=== I. Tasks still Processing (Status=1) in sources - reset_stale_tasks() will requeue them ==='")
            out.append("SELECT * FROM (\n" + "\nUNION ALL ".join(rows) + "\n) x WHERE processing > 0;\n")
        return "\n".join(out)

    # -- 05 verify ----------------------------------------------------------
    def verify(self, tables=None):
        """All tables, or only `tables` (two-step mode: the tables just loaded)."""
        want = (lambda t: True) if tables is None else (lambda t: t in tables)
        out = [HEADER.format(name="05_verify.sql", what=
            "Row-count reconciliation: every source row is either in the target or in _merge.conflict_log.\n"
            "-- Run after 04_finalize.sql while the sources are still attached.")]
        out.append("\\pset footer off\n")
        rows = []
        for t in LOAD_ORDER:
            for s in self.sources:
                if not want(t) or not self.has(s, t) or t in ("ActiveLanIds", "Folder_Structure_Final_Analysis"):
                    continue
                n = s["name"]
                ft = self.ft(s, t)
                src_cnt = f"(SELECT COUNT(*) FROM {ft})"
                # rows we expect to have been dropped
                skip_conds = self.skip_where(s, t, alias="x")
                if t == "ADGroups":
                    exp = f"(SELECT COUNT(*) FROM {ft}) - (SELECT COUNT(*) FROM _merge.conflict_log c WHERE c.table_name = 'ADGroups' AND c.source_db = {lit(n)} AND c.action = 'merged')"
                elif skip_conds:
                    exp = f"(SELECT COUNT(*) FROM {ft} x WHERE " + " AND ".join(skip_conds) + ")"
                elif t in ("sitemapping", "Folders"):
                    exp = f"(SELECT COUNT(*) FROM {ft}) - (SELECT COUNT(*) FROM _merge.conflict_log c WHERE c.table_name = {lit(t)} AND c.source_db = {lit(n)} AND c.action = 'skipped')"
                else:
                    exp = src_cnt
                # rows actually in the target for this source
                if t in SOURCE_TAG:
                    tgt = f"(SELECT COUNT(*) FROM public.{q(t)} WHERE {q(SOURCE_TAG[t])} = {lit(n)})"
                else:
                    idcol = {"MigrationTasks_Parsed": "TaskId", "NewChangePath_NEW": "TaskId"}.get(t) or \
                        next(c for (tt, c) in COL_DOMAIN if tt == t and COL_DOMAIN[(tt, c)] == t)
                    dom = COL_DOMAIN[(t, idcol)]
                    tgt = (f"(SELECT COUNT(*) FROM public.{q(t)} WHERE {q(idcol)} > {self.off(dom, n)} "
                           f"AND {q(idcol)} <= {self.off(dom, n)} + (SELECT src_max_id FROM _merge.id_offset WHERE table_name = {lit(dom)} AND source_db = {lit(n)}))")
                rows.append(f"SELECT {lit(t)} AS table_name, {lit(n)} AS source_db, {src_cnt} AS source_rows, {exp} AS expected, {tgt} AS loaded")
        # union tables
        lan = [f'SELECT "LanId" FROM {self.ft(s, "ActiveLanIds")}' for s in self.sources if self.has(s, "ActiveLanIds")]
        if lan and want("ActiveLanIds"):
            rows.append("SELECT 'ActiveLanIds', '(all, de-duplicated)', (SELECT COUNT(*) FROM (" + " UNION ALL ".join(lan) + ") a), "
                        "(SELECT COUNT(*) FROM (" + " UNION ".join(lan) + ") a), (SELECT COUNT(*) FROM public.\"ActiveLanIds\")")
        fs = [f'SELECT 1 FROM {self.ft(s, "Folder_Structure_Final_Analysis")}' for s in self.sources if self.has(s, "Folder_Structure_Final_Analysis")]
        if fs and want("Folder_Structure_Final_Analysis"):
            rows.append("SELECT 'Folder_Structure_Final_Analysis', '(all)', (SELECT COUNT(*) FROM (" + " UNION ALL ".join(fs) + ") a), "
                        "(SELECT COUNT(*) FROM (" + " UNION ALL ".join(fs) + ") a), (SELECT COUNT(*) FROM public.\"Folder_Structure_Final_Analysis\")")
        for t in sorted(self.generic):
            if not want(t):
                continue
            tag = q(self.generic[t]["tag"])
            for s in self.sources:
                if self.has(s, t):
                    cnt = f"(SELECT COUNT(*) FROM {self.ft(s, t)})"
                    rows.append(f"SELECT {lit(t)}, {lit(s['name'])}, {cnt}, {cnt}, "
                                f"(SELECT COUNT(*) FROM public.{q(t)} WHERE {tag} = {lit(s['name'])})")
        out.append("CREATE TEMP TABLE verify_result AS\nSELECT *, source_rows - expected AS dropped_by_rule,\n"
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
        out.append("\\echo '=== Conflict log summary ==='")
        out.append("SELECT table_name, source_db, action, COUNT(*) FROM _merge.conflict_log GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;\n")
        out.append("DO $$\nBEGIN\n  IF EXISTS (SELECT 1 FROM verify_result WHERE status <> 'OK') THEN\n"
                   "    RAISE EXCEPTION 'VERIFY FAILED: see MISMATCH rows above';\n  END IF;\n"
                   "  RAISE NOTICE 'VERIFY OK: every source row is loaded or accounted for in _merge.conflict_log';\nEND $$;\n")
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


def main(schema_loader=None):
    with open(os.path.join(HERE, "sources.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    g = Gen(cfg, schema_loader or cli_schema_loader())
    files = {
        "01_attach_sources.sql": g.attach(),
        "02_preflight.sql": g.preflight(),
        "03_merge.sql": g.merge(),
        "03_prepare.sql": g.prepare(),
        "05_verify.sql": g.verify(),
        "06_detach_sources.sql": g.detach(),
    }
    for name, text in files.items():
        with open(os.path.join(SQL_DIR, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        print(f"wrote sql/{name}")
    print("sources:", ", ".join(f"{i}.{s['name']}" + ("" if s.get("dump") else " (live schema)")
                                for i, s in enumerate(g.sources, 1)))
    if g.generic:
        print("copied as-is (no merge rule):", ", ".join(sorted(g.generic)))
    return g


if __name__ == "__main__":
    main()
