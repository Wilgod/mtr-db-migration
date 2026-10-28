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
| `build_exe.ps1` | Builds `dist\MetadataHorizontal.exe`, a single-file version of the app. |
| `mh_app.ps1` / `mh_app.cmd` | **PowerShell version** of the desktop app (no Python). Same buttons, same `config.json`. Double-click `mh_app.cmd` to start it. |
| `MetadataHorizontal.psm1` | The logic for the PowerShell version. It gives the same results as `metadata_horizontal.py`. |
| `SharePointExport.psm1` | **Export from SharePoint**: reads every file's metadata from SharePoint 2013 document libraries into one Excel/CSV sheet (see below). |

## Export from SharePoint 2013 (PowerShell app)

**Export from SharePoint** (first button in `mh_app.cmd`) builds the upload file straight from SharePoint:

1. Enter the **Site URL**. A pasted page or library URL also works (`.../Shared Documents/Forms/AllItems.aspx`). The app walks up to the site.
2. Choose the login: **Windows login** (the signed-in user, NTLM/Kerberos) or **DOMAIN\user + password**. With **Include subsites**, every subsite the login can open is included too.
3. Click **Load libraries**. Every visible document library is listed with its item count. SharePoint's own libraries (Style Library, Site Assets, Site Pages, Pages, Form Templates…) start unticked.
4. Click **Export to Excel...** and save as `.xlsx` (needs ImportExcel; max 1,048,575 files) or `.csv` (no limit). When you're connected to the database, the app then offers to **Upload** the file straight away.

What's in the sheet (one row per **file**; folders are left out):

| Column | Value |
|---|---|
| `OriginalSharePointPath`, `UniqueId`, `FileRef` | Identity columns. Upload matches files on these and does not store them. |
| `FileLeafRef`, `FileDirRef`, `FSObjType` | Name, folder, type |
| every library field, by **InternalName** | All visible fields that are not *Computed*, plus `Title`, `ContentType`, `Created`, `Modified`, `Author`, `Editor`, `_UIVersionString`, `CheckoutUser`. The columns are the union over all chosen libraries. |

- **Values** are the text SharePoint shows: people as display names, lookups and managed metadata as labels, multi-values joined with `; `.
- **Dates** are in this PC's local time as `yyyy-MM-dd HH:mm:ss`. SharePoint stores them in UTC.

Things to know:
- **It only reads.** It uses the REST API (`/_api`, `odata=verbose`), 500 items per request, ordered by ID, so the list view threshold doesn't block large libraries. Files the login can't see are not exported.
- **On Upload**, every column that is not a fixed key or in `SkipColumns` becomes a `Metadata` row. If you don't want fields such as `Author`, `Title` or `ContentType` there, add them to `SkipColumns`.

## PowerShell version (no Python)

Copy the folder (`mh_app.cmd`, `mh_app.ps1`, `MetadataHorizontal.psm1`, `SharePointExport.psm1`, `config.json`) to the PC and double-click **`mh_app.cmd`**. It runs on the Windows PowerShell 5.1 built into Windows, or on PowerShell 7.

It needs two things:

- **`psql.exe`** (PostgreSQL command line tools or pgAdmin). The app looks in this order: `"PsqlPath"` in `config.json`, then `PATH`, then `psql\` or `bin\` next to the app, then `C:\Program Files\PostgreSQL\<newest>\bin`, then pgAdmin. The path it uses is shown in the log at start-up.
- **ImportExcel**, only for `.xlsx` / `.xlsm` uploads (CSV works without it). Install it once with `Install-Module ImportExcel -Scope CurrentUser`. On a PC without internet, run `Save-Module ImportExcel -Path <folder>` on another PC and copy `<folder>\ImportExcel` to `modules\ImportExcel` next to the app. The app finds it there automatically.

How it differs from the Python version:

- **Scripts go to `psql` through stdin**, so no temp files are written. Uploaded rows stream straight into the database with `COPY`.
- **Every job that changes data runs between `BEGIN` and `COMMIT`.** If psql stops early, or the app is closed mid-job, the server rolls the whole job back.
- **Jobs run in the background**, so the window stays responsive. Log lines and progress appear as the job runs.
- **Settings:** password, Kerberos (`PGKRBSRVNAME`), `SslMode` and Role (`PGOPTIONS=-c role=...`) are passed to psql as environment variables, never on the command line.
- **XLSX:** the first sheet is read. Dates are formatted as `yyyy-MM-dd HH:mm:ss` and whole numbers have no `.0`, the same as the Python version.

The logic can also be scripted:

```powershell
Import-Module .\MetadataHorizontal.psm1
$cfg  = Read-MhConfig .\config.json
$s    = Get-MhSettings $cfg
$conn = New-MhConnection $cfg $env:PGPASSWORD
Initialize-MhTable $conn $s                          # create / sync table
Import-MhFile -Conn $conn -S $s -Path C:\data\arch.xlsx   # [-Sheet Sheet1]
Convert-MhExisting $conn $s $true                    # $false = keep vertical rows
Update-MhMatrix $conn $cfg                           # rebuild MetadataMatrix
Get-MhDdl $s                                         # print CREATE TABLE
```

## Setup and run (Python version)

```powershell
pip install -r requirements.txt
python mh_app.py
```

**Without Python:** run `.\build_exe.ps1` once to build `dist\MetadataHorizontal.exe`, then copy the exe to the other computer, into a folder you can write to. It keeps `config.json` and `logs\` next to itself. On first start it writes the built-in default `config.json`; to keep your own settings (fixed columns, skip list), copy your `config.json` next to the exe too. Rebuild the exe after any code change.

The password field is pre-filled from `$env:PGPASSWORD` and is **never saved**. Host, port, database, user and role are saved to `config.json` when you connect.

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

Same logic as the app. The connection comes from `config.json` (including `Kerberos` and `Role`) plus `PGPASSWORD`, or from `--url`.

```powershell
python metadata_horizontal.py ensure                    # create / sync table
python metadata_horizontal.py upload C:\data\arch.xlsx  # [--sheet Sheet1]
python metadata_horizontal.py convert                   # [--keep-vertical]
python metadata_horizontal.py ddl                       # print CREATE TABLE
python metadata_horizontal.py matrix                    # rebuild MetadataMatrix (every key a column)
```

## Note for `mtr-Postgres1` (pg_connect)

pg_connect's **Metadata Pivot / Export** reads only the vertical `Metadata` table. Fields moved into `MetadataHorizontal` therefore no longer appear in its pivot or Excel export unless the pivot is changed to also read the new table.
