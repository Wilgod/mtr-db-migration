"""
Metadata matrix: one row per file, one text column per distinct
"Metadata"."MetadataKey". "Metadata" is only read, never changed.

Plain SQL text with no dependencies, shared by metadata_horizontal.py
(SQLAlchemy) and the migration exe (psql):

    info = <run info_sql(table), parse the one JSON value>
    for stmt in build_statements(table, info): <run stmt>      # one transaction
    summary = <run summary_sql(), parse the one JSON value>

Keys are written as SQL literals (never bind parameters), so the same text runs
through psql and through SQLAlchemy's exec_driver_sql.
"""
import re

DEFAULT_TABLE = "MetadataMatrix"
MAX_KEYS = 1590                 # PostgreSQL allows 1600 columns per table
BASE_COLUMNS = ("FileId", "SiteName", "SiteId")
MARKER = "Metadata matrix:"     # start of the table comment; only such tables are replaced
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def q(ident):
    return '"' + ident.replace('"', '""') + '"'


def lit(s):
    return "'" + str(s).replace("'", "''") + "'"


def check_table(table, protected=()):
    """Raise ValueError if `table` is not a usable matrix table name."""
    if not _IDENT_RE.match(table or ""):
        raise ValueError(f"Matrix table name '{table}' must be letters/digits/_ and at most 63 chars")
    if table.lower() in {"metadata", "files", *(p.lower() for p in protected)}:
        raise ValueError(f"Matrix table name '{table}' would replace an existing data table")
    return table


def column_names(keys):
    """{key: column}. The column is the key itself, cut to PostgreSQL's 63-byte name
    limit; names that would collide (after cutting, or with FileId/SiteName/SiteId)
    get _2, _3..."""
    used, out = set(BASE_COLUMNS), {}
    for k in keys:
        base = k.encode("utf-8")[:63].decode("utf-8", "ignore") or "key"
        name, n = base, 1
        while name in used:
            n += 1
            sfx = f"_{n}"
            name = base.encode("utf-8")[:63 - len(sfx)].decode("utf-8", "ignore") + sfx
        used.add(name)
        out[k] = name
    return out


def info_sql(table):
    """One JSON value: keys (sorted), files_site_id, dups, exists, comment."""
    reg = lit(f"public.{q(table)}")
    return f"""SELECT json_build_object(
    'keys', COALESCE((SELECT json_agg(k ORDER BY k)
                      FROM (SELECT DISTINCT "MetadataKey" AS k FROM public."Metadata") d), '[]'::json),
    'files_site_id', EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public'
                             AND table_name = 'Files' AND column_name = 'SiteId'),
    'dups', (SELECT count(*) FROM (SELECT 1 FROM public."Metadata"
                                   GROUP BY "FileId", "MetadataKey" HAVING count(*) > 1) x),
    'exists', to_regclass({reg}) IS NOT NULL,
    'comment', obj_description(to_regclass({reg}), 'pg_class'))"""


def summary_sql(table):
    """One JSON value: files (rows in the matrix), orphans (Metadata files not in Files)."""
    return f"""SELECT json_build_object(
    'files', (SELECT count(*) FROM public.{q(table)}),
    'orphans', (SELECT count(DISTINCT m."FileId") FROM public."Metadata" m
                WHERE NOT EXISTS (SELECT 1 FROM public."Files" f WHERE f."FileId" = m."FileId")))"""


def check_info(table, info):
    """Raise ValueError if the matrix can't be built; return {key: column}."""
    keys = info["keys"]
    if not keys:
        raise ValueError('"Metadata" is empty: no keys to turn into columns')
    if len(keys) > MAX_KEYS:
        raise ValueError(f'"Metadata" has {len(keys):,} distinct keys; a table can hold at most '
                         f'{MAX_KEYS:,} key columns')
    if info["exists"] and not (info.get("comment") or "").startswith(MARKER):
        raise ValueError(f'Table "{table}" already exists and was not built by Build Matrix; '
                         f"it is left alone. Rename or drop it first.")
    return column_names(keys)


def build_statements(table, info):
    """SQL statements (run them in one transaction) that rebuild the matrix table.
    If a file has the same key twice, the row with the highest MetadataId wins."""
    cols = check_info(table, info)
    keys = info["keys"]
    t = f"public.{q(table)}"
    site = info["files_site_id"]
    out = ["SET LOCAL client_min_messages = warning", f"DROP TABLE IF EXISTS {t}"]
    out.append(f"CREATE TABLE {t} (\n"
               f'    "FileId" integer PRIMARY KEY REFERENCES public."Files"("FileId") ON DELETE CASCADE,\n'
               f'    "SiteName" varchar(256),\n'
               + ('    "SiteId" integer,\n' if site else "")
               + ",\n".join(f"    {q(cols[k])} text" for k in keys) + "\n)")
    sel = ",\n".join(f'    NULLIF((array_agg(m."MetadataValue" ORDER BY m."MetadataId" DESC) '
                     f'FILTER (WHERE m."MetadataKey" = {lit(k)}))[1], \'\')' for k in keys)
    out.append(f'INSERT INTO {t} ("FileId", "SiteName"' + (', "SiteId"' if site else "") + ", "
               + ", ".join(q(cols[k]) for k in keys) + ")\n"
               f'SELECT m."FileId", MAX(f."SiteName")' + (', MAX(f."SiteId")' if site else "") + f",\n{sel}\n"
               f'FROM public."Metadata" m JOIN public."Files" f ON f."FileId" = m."FileId"\n'
               f'GROUP BY m."FileId"')
    out.append(f'CREATE INDEX {q("IX_" + table + "_SiteName")} ON {t} ("SiteName")')
    out.append(f"COMMENT ON TABLE {t} IS " + lit(
        f'{MARKER} one row per file, one column per distinct "Metadata"."MetadataKey". '
        f'Rebuilt by Build Matrix; "Metadata" itself is unchanged.'))
    out += [f"COMMENT ON COLUMN {t}.{q(cols[k])} IS {lit('Metadata key: ' + k)}" for k in keys]
    return out


def describe(info, cols):
    """Log lines before the build."""
    renamed = sum(1 for k, c in cols.items() if k != c)
    lines = [f"{len(cols):,} distinct MetadataKey values -> {len(cols):,} columns"
             + (f" ({renamed} renamed to fit, see column comments)" if renamed else "")]
    if info["dups"]:
        lines.append(f"{info['dups']:,} file/key pair(s) appear more than once; "
                     f"the latest row (highest MetadataId) is used")
    return lines


def result_line(table, n_keys, summary, seconds):
    return (f"Built {table}: {summary['files']:,} files x {n_keys:,} key columns"
            + (f"; {summary['orphans']:,} file(s) in Metadata without a Files row skipped"
               if summary["orphans"] else "")
            + f"  [{seconds:.1f}s]")
