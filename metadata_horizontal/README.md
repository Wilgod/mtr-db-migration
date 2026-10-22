# Metadata Horizontal

The `Metadata` table is **vertical**: if a file has 20 metadata fields, it has 20 rows. This tool adds a **horizontal** table, `MetadataHorizontal`, with **one row per file** and one column for each of the fixed fields you list in `config.json`. Every other field stays in `Metadata`.

```
 upload.csv / upload.xlsx
 ┌──────────┬──────────┬────────────────┬───────────────┬──────────┐
 │ FileRef  │ UniqueId │ ishareFileType │ ishareRemarks │ Custom1  │ ...
 └──────────┴──────────┴────────────────┴───────────────┴──────────┘
        │ match to Files           │ in Columns     │ in Columns  │ not in Columns / not skipped
        ▼                          ▼                ▼             ▼
   "Files"."FileId"  ──►  "MetadataHorizontal" (1 row/file)    "Metadata" (1 row/key)
                          FileId | FileType | Remarks | ...    FileId | MetadataKey | MetadataValue
```

## Files

| File | What it is |
|---|---|
| `mh_app.py` | Desktop app: connect, then **Upload CSV/XLSX**, **Create / Sync Table**, **Convert existing Metadata**, **Build Matrix**, **View Table** / **View Matrix** and **Show DDL**. |
| `metadata_horizontal.py` | All the logic. It can also be run from the command line (see below). |
| `config.json` | Connection settings, the fixed column list, and the skip list. |
| `sql/metadata_horizontal.sql` | Reference DDL for the default config. The app creates the table itself; this file is for review. |

## Setup and run

```powershell
pip install -r requirements.txt
python mh_app.py
```

The password field is pre-filled from `$env:PGPASSWORD` and is **never saved**. Host, port, database and user are saved to `config.json` when you connect.

Tick **Kerberos** to log in as the signed-in Windows (domain) user instead of a password. For this to work, the server needs a `gss` (or `sspi`) line in `pg_hba.conf`, **Host** must be the server's full DNS name (the one in its `postgres/<host>` SPN), and **User** must be the PostgreSQL role your principal maps to. `KrbSrvName` in `config.json` sets the service name (default `postgres`).

**Role** (optional) runs every session as `SET ROLE <role>`, for example `cwp_mig_prd`, so tables are created and changed as that role (a group role is fine). Your login must be a member of the role. **Connect** logs both names: `login ssishadm_prj1, running as cwp_mig_prd`.

## config.json

```jsonc
{
  "Connection": { "Host": "localhost", "Port": 5432, "Database": "MtrMigration_Consolidated", "User": "postgres", "SslMode": "", "Kerberos": false, "KrbSrvName": "postgres", "Role": "" },
  "HorizontalMetadata": {
    "TableName": "MetadataHorizontal",
    "Columns": {                         // metadata key (CSV header)  ->  column in the table
      "ishareFileType": "FileType",
      "ishareDocumentDate": "DocumentDate"
      // ... your fixed columns
    },
    "ColumnMaxLength": 4000,             // varchar size of each column
    "CurrentVersionOnly": true,          // skip rows whose VersionLabel is not "Current"
    "RemoveMovedKeysFromMetadata": true  // default of the "Convert" checkbox
  },
  "SkipColumns": [ "_Author", "GUID", "..." ],   // headers stored nowhere
  "UnmatchedReportFolder": "logs"
}
```

- **`Columns` starts with the 28 `ishare*` entries** copied from `mtr-Postgres1`'s `ColumnMapping`, using the same column names. Cut it down to your 20 fixed columns.
- **`SkipColumns`** is copied from `mtr-Postgres1`'s `Data.SkipColumns`.
- **Header matching** against `Columns` keys and `SkipColumns` ignores case.
- **Applying config changes:** after editing `config.json`, click **Reload config**, then **Create / Sync Table**. New columns are added to the table. A column you remove from `Columns` stays in the table, and the app reports it; nothing is dropped automatically.

## What an upload does

1. **Reads the file.** CSV (UTF-8) or XLSX (first sheet). Rows with `FSObjType = 1` (folders) are skipped. When `CurrentVersionOnly` is on, rows whose `VersionLabel` is not `Current` are also skipped.
2. **Finds the file in `Files`**, in this order:

   | # | Upload column | Matches |
   |---|---|---|
   | 1 | `FileId` | `Files.FileId`, only if the row's `UniqueId`, `OriginalSharePointPath` and `FileRef` (where present) agree with that file. FileIds were renumbered by the DB consolidation, so a FileId from an old export can point to a different file. |
   | 2 | `UniqueId` | `Files.SharePointUniqueId` (braces and case don't matter). |
   | 3 | `OriginalSharePointPath` | `Files.OriginalSharePointPath`. |
   | 4 | `FileRef` | The path part of `Files.OriginalSharePointPath`. |

   A row that matches nothing is written to `logs/unmatched_<file>_<time>.csv`. If the same file appears in more than one row, the **last row wins**.
3. **Fixed columns:** one row per file is inserted into `MetadataHorizontal`, or updated if the file already has a row. An empty cell becomes NULL. Fixed fields that aren't in the upload keep their old values.
4. **Every other non-skipped column** becomes a `Metadata` row (empty values are not stored).
5. **No duplicates on re-upload:** for each matched file, any old `Metadata` rows for the fields in this upload (fixed fields included) are deleted first. Uploading the same file twice gives the same result.
6. **All or nothing:** the whole upload runs in **one transaction**. If any step fails, nothing is changed.

**Convert existing Metadata** handles data that is already in `Metadata` (for example, from the phase3 scan). It copies the fixed keys into `MetadataHorizontal`, one row per file. With the checkbox ticked, it then deletes those keys from `Metadata`.

## Metadata Matrix (every key as a column)

**Build Matrix** (or `python metadata_horizontal.py matrix`) builds a separate table, `MetadataMatrix`, with **one row per file and one column for every distinct `MetadataKey`** in `Metadata`. **View Matrix** shows it. `Metadata` is only read, never changed, and `MetadataHorizontal` is not touched.

```
 "Metadata" (vertical, unchanged)              "MetadataMatrix" (rebuilt each run)
 FileId | MetadataKey    | MetadataValue       FileId | SiteName | SiteId | ishareFileType | ishareRemarks | ...
 1      | ishareFileType | Drawing      ──►    1      | SiteA    | 10     | Drawing        | new remark    |
 1      | ishareRemarks  | new remark          2      | SiteB    | 20     | Letter         |               |
```

- **Rebuilt from scratch** each time, so new keys appear as new columns. The rebuild runs in one transaction, so the old table stays readable until the new one is done.
- **Column name = the key itself.** Keys are case-sensitive, so `Title` and `title` are two columns. A key longer than PostgreSQL's 63-byte limit is cut, and one that collides with another name (`FileId`, `SiteName`, `SiteId`, or another cut key) gets `_2`, `_3`… Every key column's comment records its exact key.
- **Values:** all columns are `text`; empty values become NULL. If a file has the same key twice, the newest row (highest `MetadataId`) wins, and the log says how many such pairs there were.
- **Only files that have metadata** get a row. Metadata rows whose `FileId` is not in `Files` are skipped and counted in the log.
- **Limit:** at most 1,590 distinct keys (PostgreSQL allows 1,600 columns per table).
- The table name is set in `config.json`: `"MetadataMatrix": { "TableName": "MetadataMatrix" }`. It can't be `Metadata`, `Files` or the horizontal table.

## Table

| Column | Notes |
|---|---|
| `MetadataHorizontalId` | serial PK |
| `FileId` | **unique**, FK → `Files.FileId` `ON DELETE CASCADE` |
| `SiteName` | Taken from the matched `Files` row (indexed). |
| `SiteId` | Only in the consolidated DB, which has the `Sites` table: indexed, FK → `Sites`, and filled by the same `set_site_id()` trigger as every other table. |
| *(your fixed columns)* | `varchar(ColumnMaxLength)`. Each column's comment records its metadata key. |
| `SourceFile`, `ImportedAt`, `UpdatedAt` | Audit columns. |

## Command line

Same logic as the app. The connection comes from `config.json` plus `PGPASSWORD`, or from `--url`.

```powershell
python metadata_horizontal.py ensure                    # create / sync table
python metadata_horizontal.py upload C:\data\arch.xlsx  # [--sheet Sheet1]
python metadata_horizontal.py convert                   # [--keep-vertical]
python metadata_horizontal.py ddl                       # print CREATE TABLE
python metadata_horizontal.py matrix                    # rebuild MetadataMatrix (every key a column)
```

## Note for `mtr-Postgres1` (pg_connect)

pg_connect's **Metadata Pivot / Export** reads only the vertical `Metadata` table. Fields moved into `MetadataHorizontal` therefore no longer appear in its pivot or Excel export unless the pivot is changed to also read the new table.
