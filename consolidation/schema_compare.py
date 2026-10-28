"""
Schema compare: which tables and columns differ between two databases, and the
SQL that adds what the second one (B) is missing so it matches the reference (A).

Plain SQL text with no dependencies, used by the migration exe (psql):

    a = parse(<run SCHEMA_SQL in A, one JSON value>)
    b = parse(<run SCHEMA_SQL in B>)
    items = diff(a, b)
    script = fix_script([i for i in items if i["sql"]])     # run in B, one transaction

Only missing tables and missing columns get SQL. Type / NOT NULL differences and
what only B has are reported, never changed. Data is never copied.
"""
import json
import re

# Run with search_path '' so format_type and pg_get_expr schema-qualify user types and sequences.
SCHEMA_SQL = r"""SELECT COALESCE(json_agg(t ORDER BY t.s, t.n), '[]') FROM (
  SELECT n.nspname AS s, c.relname AS n,
         (SELECT json_agg(json_build_object(
                   'name', a.attname, 'type', format_type(a.atttypid, a.atttypmod), 'notnull', a.attnotnull,
                   'default', pg_get_expr(d.adbin, d.adrelid), 'identity', a.attidentity::text,
                   'generated', a.attgenerated::text) ORDER BY a.attnum)
            FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
           WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped) AS cols,
         (SELECT pg_get_constraintdef(k.oid) FROM pg_constraint k
           WHERE k.conrelid = c.oid AND k.contype = 'p') AS pk
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition
     AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg\_%'
) t"""

KINDS = {  # kind: (label, sort order)
    "missing_table": ("table missing in B", 0),
    "missing_column": ("column missing in B", 1),
    "differs": ("column differs", 2),
    "extra_table": ("table only in B", 3),
    "extra_column": ("column only in B", 4),
}
_NEXTVAL = re.compile(r"nextval\('((?:[^']|'')+)'::regclass\)")


def q(ident):
    return '"' + ident.replace('"', '""') + '"'


def parse(text):
    """SCHEMA_SQL output -> {(schema, table): {"cols": [col dicts], "pk": "PRIMARY KEY (...)" or None}}."""
    return {(t["s"], t["n"]): {"cols": t["cols"] or [], "pk": t["pk"]} for t in json.loads(text or "[]")}


def table_label(schema, table):
    return table if schema == "public" else f"{schema}.{table}"


def describe(col):
    """Column definition as shown in the list."""
    if col["generated"] == "s":
        extra = f" GENERATED ALWAYS AS ({col['default']}) STORED"
    elif col["identity"]:
        extra = " GENERATED " + ("ALWAYS" if col["identity"] == "a" else "BY DEFAULT") + " AS IDENTITY"
    elif col["default"] is not None:
        extra = f" DEFAULT {col['default']}"
    else:
        extra = ""
    return col["type"] + (" NOT NULL" if col["notnull"] else "") + extra


def column_def(col, adding):
    """(SQL column definition, note). Adding to a table that may hold rows: a NOT NULL
    column without a default would fail, so it is added nullable."""
    d = describe(col)
    has_value = col["generated"] == "s" or col["identity"] or col["default"] is not None
    if adding and col["notnull"] and not has_value:
        return f"{q(col['name'])} {d.replace(' NOT NULL', '', 1)}", "added without NOT NULL (existing rows have no value)"
    return f"{q(col['name'])} {d}", ""


def sequences(cols):
    """CREATE SEQUENCE for the sequences that serial-style defaults use (they may not exist in B)."""
    seqs = []
    for c in cols:
        if c["identity"] or c["generated"] == "s" or not c["default"]:
            continue
        for m in _NEXTVAL.finditer(c["default"]):
            st = f"CREATE SEQUENCE IF NOT EXISTS {m.group(1).replace(chr(39) * 2, chr(39))}"
            if st not in seqs:
                seqs.append(st)
    return seqs


def create_table_sql(schema, table, info):
    rel = f"{q(schema)}.{q(table)}"
    lines = [column_def(c, adding=False)[0] for c in info["cols"]]
    if info["pk"]:
        lines.append(info["pk"])
    out = [] if schema == "public" else [f"CREATE SCHEMA IF NOT EXISTS {q(schema)}"]
    return out + sequences(info["cols"]) + [f"CREATE TABLE {rel} (\n    " + ",\n    ".join(lines) + "\n)"]


def add_column_sql(schema, table, col):
    d, note = column_def(col, adding=True)
    return sequences([col]) + [f"ALTER TABLE {q(schema)}.{q(table)} ADD COLUMN IF NOT EXISTS {d}"], note


def diff(a, b):
    """[{kind, schema, table, column, a, b, sql: [statements] or None, note}] - what differs, B against A."""
    items = []

    def add(kind, key, column="", a_desc="", b_desc="", sql=None, note=""):
        items.append({"kind": kind, "schema": key[0], "table": key[1], "column": column,
                      "a": a_desc, "b": b_desc, "sql": sql, "note": note})

    for key, info in a.items():
        if key not in b:
            add("missing_table", key, a_desc=f"{len(info['cols'])} columns", b_desc="(no table)",
                sql=create_table_sql(*key, info), note="structure only, no rows copied")
            continue
        b_cols = {c["name"]: c for c in b[key]["cols"]}
        a_names = set()
        for c in info["cols"]:
            a_names.add(c["name"])
            other = b_cols.get(c["name"])
            if other is None:
                sql, note = add_column_sql(*key, c)
                add("missing_column", key, c["name"], describe(c), "(no column)", sql, note)
            elif other["type"] != c["type"] or other["notnull"] != c["notnull"]:
                add("differs", key, c["name"], describe(c), describe(other), note="not changed: check by hand")
        for c in b[key]["cols"]:
            if c["name"] not in a_names:
                add("extra_column", key, c["name"], "(no column)", describe(c))
    for key, info in b.items():
        if key not in a:
            add("extra_table", key, a_desc="(no table)", b_desc=f"{len(info['cols'])} columns")
    items.sort(key=lambda i: (i["schema"] != "public", i["schema"], i["table"].lower(),
                              KINDS[i["kind"]][1], i["column"]))
    return items


def fix_script(items, a_name, b_name):
    """SQL file for B (run with --single-transaction): every statement of `items`."""
    out = [f"-- Schema compare: add to {b_name} what it is missing compared with {a_name}.",
           "-- Structure only, no rows are copied.", ""]
    for i in items:
        what = table_label(i["schema"], i["table"]) + (f".{i['column']}" if i["column"] else "")
        out.append(f"-- {KINDS[i['kind']][0]}: {what}" + (f"  ({i['note']})" if i["note"] else ""))
        out += [st + ";" for st in i["sql"]]
        out.append("")
    return "\n".join(out)
