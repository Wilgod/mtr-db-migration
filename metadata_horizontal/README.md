# Metadata Horizontal

The `Metadata` table is **vertical**: if a file has 20 metadata fields, it has 20 rows. This tool adds a **horizontal** table, `MetadataHorizontal`, with **one row per file** and one column for each of the fixed fields you list in `config.json`. An upload saves only those fields; every other column in the file is ignored.

```
 upload.csv / upload.xlsx
 ┌──────────┬──────────┬────────────────┬───────────────┬──────────┐
 │ FileRef  │ UniqueId │ ishareFileType │ ishareRemarks │ Custom1  │ ...
 └──────────┴──────────┴────────────────┴───────────────┴──────────┘
        │ match to Files           │ in Columns     │ in Columns  │ not in Columns
        ▼                          ▼                ▼             ▼
   "Files"."FileId"  ──►  "MetadataHorizontal" (1 row/file)    ignored
                          FileId | FileType | Remarks | ...
```

## Files

| File | What it is |
|---|---|
| `mh_app.py` | Desktop app: connect, then **Upload CSV/XLSX**, **Upload CSV folder**, **Create / Sync Table**, **Convert existing Metadata**, **Build Matrix**, **View Table** / **View Matrix** and **Show DDL**. |
| `metadata_horizontal.py` | All the logic. It can also be run from the command line (see below). |
| `config.json` | Connection settings and the fixed column list. |
| `sql/metadata_horizontal.sql` | Reference DDL for the default config. The app creates the table itself; this file is for review. |
| `build_exe.ps1` | Builds `dist\MetadataHorizontal.exe` from the **PowerShell** app with ps2exe, plus `dist\psql\`, `dist\modules\ImportExcel` and `dist\MetadataHorizontal.zip` (see below). |
| `build_exe_python.ps1` | The old build: the same exe name, built from the Python app with PyInstaller (no SharePoint export). The last build wins. |
| `mh_app.ps1` / `mh_app.cmd` | **PowerShell version** of the desktop app (no Python). Same buttons, same `config.json`. Double-click `mh_app.cmd` to start it. |
| `MetadataHorizontal.psm1` | The logic for the PowerShell version. It gives the same results as `metadata_horizontal.py`. |
| `SharePointExport.psm1` | **Export from SharePoint**: reads every file's metadata from SharePoint 2013 document libraries into one Excel/CSV sheet (see below). |

## Compare an export with per-library CSV scans (PowerShell app)

**Compare with CSV folder** checks that a combined export has every file from another scanner's per-library CSVs. It needs no database or SharePoint connection, but it does need ImportExcel.

1. Choose one or more combined exports (`.xlsx` or `.csv`; Ctrl/Shift to pick several).
2. Choose the CSV folder. Subfolders are included. This can be the top folder of all scans.
3. Answer **Yes** to also compare the value of every column, file by file (the Value check sheets). **No** checks files and columns only, which is faster.
4. Each export gets its own `sharepoint_compare_<export name>_<time>.xlsx` next to the exports. With one export, the main grid shows the result per library. With several, it shows one row per export, and `sharepoint_compare_summary_<time>.xlsx` lists them all.

**Which CSVs each export is compared with:** an `.xlsx` export lists its sites (subsites included) in the Library columns sheet. A CSV belongs to a site when its folder path ends with `<host name>\<site path>`. For example, `...\ishare_partner\ishare\partner\aro` belongs to `http://ishare.mtr.com.hk/partner/ARO`. Only the site's own folder counts, so a subsite exported to another file is left out. If one export is chosen and none of its sites has a folder, every CSV in the folder is used, as before. If several are chosen, an export whose sites have no folder (or that has no Library columns sheet) is listed in the summary and not compared.

Files match on `UniqueId`, then on `FileRef`, ignoring case. Folder rows are not compared, and a library scanned more than once counts each file once. Columns are checked both ways, by whether a value is present. With the value check, each value of a column in both is compared too, for every file in both:

| Value result | Meaning |
|---|---|
| Same | The same text (spaces at either end are ignored). |
| Same, other format | The same value written differently: case, spaces, `True`/`Yes`, `1.0`/`1`, `1,440`/`1440`, a claims prefix (`i:0#.w\|domain\user`), lookup ids (`12;#Name`) or `;#` separators. |
| Same time, other time zone | Both are dates that differ by whole quarter-hours, at most 14 hours apart (one scanner writes UTC, the other local time). **Time shift** shows the most common difference, for example `xlsx +8h (100%)`. |
| Blank in xlsx / Blank in CSV | Only one side has a value. |
| Different | Anything else. |

| Value sheet | Contents |
|---|---|
| Value check | One row per column, with counts for each result and an example CSV and xlsx value. Differences are listed first. |
| Value check by CSV | The same counts per library, for each column that is not all the same. |
| Value differences | Each cell that is blank on one side or different: library, `FileRef`, CSV value and xlsx value. At most 2,000 per column and 200,000 in all; the counts in Value check include every cell. |

**Where to find a difference:** Value differences gives the **xlsx row** (the Excel row number in the export's Metadata sheet) and the **CSV file** and **CSV line** (the line the record starts on; the header is line 1). Value check gives the same for one example per column. Missing from workbook gives the CSV line of each missing file, and Column check by CSV gives the first xlsx row that has a value in a column the CSV lacks.

With several exports, the summary workbook has these sheets for all of them together, each row starting with the export's name:

| Summary sheet | Contents |
|---|---|
| Summary | One row per export: result, counts, report path. |
| Columns | Each column with a problem in each export: the column check (`Missing in xlsx`, `Missing in CSV`, ...) and the value check counts side by side, with an example xlsx row, CSV file and line, and the two values. |
| Value differences | Up to 100 cells per column per export, with xlsx row, CSV file and CSV line. The full list is in each export's own report. |
| Column check by CSV | As in each report. |
| Missing from workbook | As in each report, with the CSV line. |

The xlsx values are kept as 8-byte keys, so even big exports fit in memory. The text of the listed cells is read in one more pass over the xlsx.

| Report sheet | Contents |
|---|---|
| Summary | The result and the totals. |
| Libraries | Each CSV library: files, how many are in the workbook, how many are missing. |
| Missing from workbook | Files only in the CSVs. Content libraries come first, then SharePoint's system libraries. |
| Only in workbook | Files in no CSV. |
| Path or Id differs | Files that matched but have another path (moved or renamed) or another `UniqueId` (replaced). |
| Column check | Every column once. **Result** says `Missing in xlsx`, `Missing in CSV`, `Missing in CSV (some)` or `In both`, with how many files have a value on each side. A missing column also says how many libraries, and which: **Where missing** lists `site | library`. |
| Column check by CSV | Each problem per CSV, one row per library, so it can be filtered by column, site or library. **Missing in** says which side lacks the column or the value. A column missing from the xlsx is listed even if it is empty in that CSV. CSVs directly in the chosen folder show `(top folder: <name>)` as the site. |
| Problems | CSV files or rows that could not be read. Any problem makes the result "not conclusive". |
| Value check, Value check by CSV, Value differences | See the value check above. |

An `.xlsx` export has a **Library columns** sheet listing each exported library's SharePoint fields. With it, a column counts as missing from a CSV if the library has that field and it was exported, even if every value is empty. Libraries with no CSV are also listed.

An older export or a `.csv` export has no such sheet. The xlsx then only shows all libraries' columns together, so a column counts as missing from a CSV only if the xlsx has a value for one of that CSV's files. Export again as `.xlsx` to get the full check.

## Export from SharePoint 2013 (PowerShell app)

**Export from SharePoint** (first button in `mh_app.cmd`) builds the upload file straight from SharePoint:

1. Enter the **Site URL**. A pasted page or library URL also works (`.../Shared Documents/Forms/AllItems.aspx`). The app walks up to the site.
2. Choose the login: **Windows login** (the signed-in user, NTLM/Kerberos) or **DOMAIN\user + password**. With **Include subsites**, every subsite the login can open is included too.
3. Click **Load libraries**. Every visible document library is listed with its item count. SharePoint's own libraries (Style Library, Site Assets, Site Pages, Pages, Form Templates…) start unticked.
4. Set the **Export name** if you want another name. `{date}` and `{time}` are filled in as `yyyyMMdd` and `HHmm`; end the name with `.csv` for a CSV file.
5. Click **Add to queue** to run several exports one after another (see below), or **Export now...** for just this one. Save as `.xlsx` (max 1,048,575 files) or `.csv` (no limit). After **Export now...**, when you're connected to the database, the app offers to **Upload** the file straight away.

What's in the sheet (one row per **file**; folders are left out):

| Column | Value |
|---|---|
| `OriginalSharePointPath`, `UniqueId`, `FileRef` | Identity columns. Upload matches files on these. `FileRef` is not stored. |
| `FileLeafRef`, `FileDirRef`, `FSObjType` | Name, folder, type |
| `LibraryTitle`, `LibraryId` | Title and list GUID of the file's document library. Added by the export; Compare doesn't expect them in the CSVs. |
| every library field, by **InternalName** | All fields of the chosen libraries, hidden and *Computed* ones included. A field the server won't return is logged, marked `Exported = No` in the Library columns sheet and left out. The columns are the union over all chosen libraries. |

- **Values** are the text SharePoint shows: people as display names, lookups and managed metadata as labels, multi-values joined with `; `.
- **Dates** are in this PC's local time as `yyyy-MM-dd HH:mm:ss`. SharePoint stores them in UTC.

Things to know:
- **Progress** appears in both the SharePoint window and the main window: a bar, the library and step, items read, a rough time left and the elapsed time. Reading fills the bar to 90%. Writing the file fills the rest.
- **Request log:** each request to SharePoint writes a line when it starts and another when it ends, with the time taken, its size or the error. If the last line says `start` with no end, that's the request it's waiting on. Lines appear in the main window (newest part only) and in full in `logs\sharepoint_requests_<time>.log` next to the app. A scheduled run puts them in its own log.
- **Big libraries:** rows go to `sharepoint_export_work` next to the output file, page by page, then are merged into the file, so memory stays low whatever the size. That folder needs free disk space, often more than the finished file, and is deleted when the export ends.
- **Big `.xlsx` files:** Upload and Compare read a sheet of 200 MB or more (once unzipped) row by row, so its size isn't limited by memory. Compare still keeps some details of every file in memory while it checks them.
- **A stopped export can continue:** export the same libraries in the same order to the same folder within 7 days, and the app offers to continue from the last saved page. Values already read aren't read again, so they can be older than the rest. An export started before `LibraryTitle` and `LibraryId` were added lacks them for the libraries it had already started: export again from the start to get them. A failed `.xlsx` can also continue as `.csv`.
- **It only reads.** It uses the REST API (`/_api`, `odata=verbose`), 500 items per request, ordered by ID, so the list view threshold doesn't block large libraries. Files the login can't see are not exported.
- **On Upload**, only the fixed keys are saved. To store another exported field, add it to `Columns`.

### Continue a stopped folder upload

**Upload CSV folder** asks what to do with files it has already loaded. Choose **Yes** to skip them, so a stopped upload continues, or **No** to upload every file again. Each CSV is loaded in its own transaction, so a stopped upload leaves finished files in the table and the file in progress unchanged.

- **What counts as loaded:** the table `<TableName>UploadedFiles` (by default `MetadataHorizontalUploadedFiles`) records each file's path, size and modified time, and the column setup. If any of these change, the file loads again. Regenerating the CSVs changes their modified time, so they all load again.
- **Later scans still win:** in one folder, once a file has to load, the later files in that folder load too, so an older scan can't overwrite a newer one. This covers one folder only. If two folders hold the same file, the one loaded last wins.
- **Older uploads:** files uploaded before this version aren't in the record, so they load once more.
- **Deleted rows:** if the data table is missing or empty, the record is cleared and everything loads. If you delete only some rows, choose **No**.
- **Linked to `Files`:** with `LinkToFiles` set to true, a file with unmatched rows isn't recorded, so it loads again next time and those rows get another chance to match.
- **Failed files** aren't recorded, so they load again next time.

### Export queue

The queue runs your exports one after another, so you don't have to wait for one to finish before starting the next.

1. In **Site URLs**, enter one site per line and click **Load libraries**. A site that can't be loaded is named in a message, and the others are still listed.
2. Tick the subsites and libraries for one output file, then set the **Export name** and **Save in folder**.
3. Click **Add to queue**. The ticks clear, ready for the next file. Repeat for each export.
4. Click **Start queue**. Each export starts when the one before it ends.

- **Adding more:** you can load other sites and add rows while an export runs. Once the queue is started, anything you add runs without another click.
- **A login per load:** each row keeps the login it was loaded with. To use another login, change it and load again. A DOMAIN\user password is stored encrypted in `sharepoint_queue.json` next to the app, so only your Windows login on this PC can read it.
- **A failed row:** the reason shows in **Result**, and the next row still runs. Select the row and click **Retry** to run it again, from its last saved page.
- **Stopping:** **Pause after this export** lets the current one finish. **Stop now** stops it, and **Start queue** continues it from its last saved page. Stopping can take up to 100 seconds if a request is hanging.
- **Closing the app:** the queue only runs while the app is open. Closing the SharePoint window is fine, but closing the main window stops the export after asking you. The list is kept, and nothing restarts until you click **Start queue**, which continues from the last saved page. Saved pages are kept for 7 days.
- **Files:** an existing file is never overwritten; the new one gets `_2`, `_3` and so on. `{date}` and `{time}` are filled in when the row starts.
- **Sleep:** the PC is kept awake during each export, but not after the last one ends. Restarts and sign-outs still stop the app.
- **Logs:** each run writes `logs\sharepoint_queue_<name>_<time>.log` with every request. **Open logs** opens the folder.
- **Excel limit:** if the ticked libraries hold 1,048,575 items or more and the name ends in `.xlsx`, the app warns you before adding the row. Use `.csv` for those.

### Scheduled exports

**Schedule...** runs the export later with no window, through Windows Task Scheduler. Load the libraries, tick them and set the file name first. Then give the schedule a name, choose **Once**, **Daily** or **Weekly**, the first run and the folder, and click **Save schedule**. The same window lists each schedule with its next run and last result, and has **Run now**, **Delete** and **Open logs**.

- **What it saves:** the ticked libraries, the file name and the login, in `schedules\<name>.json` next to the app. A library created later isn't added: save the schedule again to include it.
- **Login:** Windows login runs as you. A DOMAIN\user password is stored encrypted, so only your Windows login on this PC can read it. If that password changes, load the libraries with the new one and save the schedule again.
- **When it runs:** only while you're signed in; a locked screen is fine. If the PC is off or you're signed out at that time, it should run as soon as it can afterwards. The PC is kept awake during the run, and a run that's still going blocks the next one from starting.
- **Files:** it writes `<name>.xlsx.writing` (or `.csv.writing`) and renames it when finished, so a failed run leaves no file that looks complete. The next run of the same schedule continues a failed one, from its last saved page. It never overwrites: an existing name gets `_2`, `_3` and so on. Use `{date}` so each run gets its own name. It doesn't upload to the database.
- **Results:** each run writes `logs\sharepoint_schedule_<name>_<time>.log`, with a progress line about once a minute. Last result shows `OK`, `Failed`, or `Saved, but libraries are missing`, when a chosen library couldn't be read.
- **Moving the app:** the task points to the app's current folder. After moving it, save the schedule again.

To see or change a task yourself, open Task Scheduler and look for `MetadataHorizontal SP export - <name>`.

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

**As one exe:** run `.\build_exe.ps1` (needs `Install-Module ps2exe -Scope CurrentUser`). It builds `dist\MetadataHorizontal.exe` with the app and both modules inside, and copies next to it:

- `psql\`: `psql.exe` and its DLLs (plus the Visual C++ runtime) from the local PostgreSQL install,
- `modules\ImportExcel`: the installed ImportExcel module.

It then zips everything into `dist\MetadataHorizontal.zip`. **Unzip that on the target PC** into a folder you can write to and run `MetadataHorizontal.exe`. Neither the PostgreSQL client nor ImportExcel needs to be installed there. On first start, the exe writes its built-in default `config.json` next to itself (an existing one is kept), and it keeps `logs\` there too. To use your own settings, copy your `config.json` next to the exe. Rebuild after any code change. Options: `-NoPsql`, `-NoImportExcel`, `-PsqlDir <folder>`.

> A module installed with `Install-Module` from **PowerShell 7** lands in `Documents\PowerShell\Modules`, which Windows PowerShell 5.1 (`mh_app.cmd`) does not search. If `mh_app.cmd` says ImportExcel is missing, install it from Windows PowerShell too, or use the exe, which brings its own copy.

The logic can also be scripted:

```powershell
Import-Module .\MetadataHorizontal.psm1
$cfg  = Read-MhConfig .\config.json
$s    = Get-MhSettings $cfg
$conn = New-MhConnection $cfg $env:PGPASSWORD
Initialize-MhTable $conn $s                          # create / sync table
Import-MhFile -Conn $conn -S $s -Path C:\data\arch.xlsx   # [-Sheet Sheet1]
Import-MhFolder -Conn $conn -S $s -Folder C:\data\scans  # every CSV under a folder
Convert-MhExisting $conn $s $true                    # $false = keep vertical rows
Update-MhMatrix $conn $cfg                           # rebuild MetadataMatrix
Get-MhDdl $s                                         # print CREATE TABLE
```

## Setup and run (Python version)

```powershell
pip install -r requirements.txt
python mh_app.py
```

**Without Python:** run `.\build_exe_python.ps1` once to build `dist\MetadataHorizontal.exe`, then copy the exe to the other computer, into a folder you can write to. It keeps `config.json` and `logs\` next to itself. On first start it writes the built-in default `config.json`; to keep your own settings (fixed columns), copy your `config.json` next to the exe too. Rebuild the exe after any code change.

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
  "UnmatchedReportFolder": "logs"
}
```

- **`Columns` has 45 entries:** the 28 `ishare*` entries copied from `mtr-Postgres1`'s `ColumnMapping`, using the same column names, plus `GUID` and `UniqueId`, plus 15 added from `compare/iShare_Archived_Sites_Advanced_Search_Mapping.xlsx` for the Advanced Search gaps: created/modified by and date, document library name and ID, folder, Project Record request fields, original URL, item ID, SharePoint version and file size.
- **Only `Columns` are stored.** Any other column in an upload is ignored.
- **Header matching** against `Columns` keys ignores case.
- **Applying config changes:** after editing `config.json`, click **Reload config**, then **Create / Sync Table**. New columns are added to the table. A column you remove from `Columns` stays in the table, and the app reports it; nothing is dropped automatically.

## Upload a folder of CSVs

**Upload CSV folder** (or `python metadata_horizontal.py upload <folder>`) uploads every `.csv` under a folder, subfolders included, such as a per-library scan.

- **One transaction per file.** This differs from a single upload: a file that fails is rolled back and listed at the end, and the other files still load. Run the folder again to retry; the result is the same.
- **Order:** files load sorted by path. If the same file is in two CSVs, the one loaded last wins, so within a folder a later scan of a library replaces an earlier one.
- **`SourceFile`** holds the CSV's path relative to the chosen folder.
- **Unmatched rows** go to one folder, `logs/unmatched_<folder>_<time>/`, with one report per CSV that has any.
- **Older scans:** a CSV's values replace what the table holds for that file. Uploading an older scan over a newer export brings the older values back. A column the CSV doesn't have keeps its value.

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
4. **Every other column** is ignored, so uploads add nothing new to `Metadata` or to the next **Build Matrix**.
5. **Fixed fields leave `Metadata`:** for each matched file, any old `Metadata` rows for the fixed fields in this upload are deleted, so a field isn't kept in both tables. Other `Metadata` rows are not touched. Uploading the same file twice gives the same result.
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
python metadata_horizontal.py upload C:\data\scans     # every CSV under a folder
python metadata_horizontal.py convert                   # [--keep-vertical]
python metadata_horizontal.py ddl                       # print CREATE TABLE
python metadata_horizontal.py matrix                    # rebuild MetadataMatrix (every key a column)
```

## Note for `mtr-Postgres1` (pg_connect)

pg_connect's **Metadata Pivot / Export** reads only the vertical `Metadata` table. Fields moved into `MetadataHorizontal` therefore no longer appear in its pivot or Excel export unless the pivot is changed to also read the new table.
