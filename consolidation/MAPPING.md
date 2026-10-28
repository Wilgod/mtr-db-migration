# MTR Phase 3: consolidating the source DBs into one database

This merges the `cwp*` PostgreSQL databases (schema exports in `../pgdump_schema/`) into a single new database, **`MtrMigration_Consolidated`**. The mtr-phase3 GUI can use that database without any code changes.

## How it works

There are two copy methods. They run the same merge SQL and give the same result; they differ only in how the target reaches the sources and in the rights they need.

**Direct** (default): the target server reads the sources itself through postgres_fdw.

```
 source DBs (cwp_ecr ... cwp2)                     target: MtrMigration_Consolidated
 ┌──────────────┐  postgres_fdw  ┌──────────────────────────────────────────────┐
 │ cwp_ecr      │ ─────────────▶ │ src_cwp_ecr.*   (foreign tables, read-only)  │
 │ ...          │ ─────────────▶ │ ...                                           │
 │ cwp2         │ ─────────────▶ │ src_cwp2.*                                    │
 └──────────────┘                │                                               │
                                 │ public.*   ← 03_merge.sql (INSERT…SELECT)     │
                                 │ _merge.*   ← ID offsets, group map, conflicts │
                                 └──────────────────────────────────────────────┘
```

**Through this PC** (staging): for a login that may only create tables in schema `public`. No new schemas and no postgres_fdw. The computer running the tool copies every source table into a staging table in `public` (`COPY … TO STDOUT` from the source piped into `COPY … FROM STDIN` on the target), and the `_merge` schema becomes tables named `public._merge_*`.

```
 source DBs              this PC                 target (e.g. cwp2), schema public only
 ┌──────────┐  COPY  ┌──────────┐  COPY  ┌────────────────────────────────────────────┐
 │ cwp_ecr  │ ─────▶ │  psql |  │ ─────▶ │ "src_cwp_ecr__Files", ... (staging tables) │
 │ ...      │ ─────▶ │  psql    │ ─────▶ │ ...                                        │
 └──────────┘        └──────────┘        │ public.*         ← same merge SQL          │
                                         │ public._merge_*  ← ID offsets, conflicts   │
                                         └────────────────────────────────────────────┘
```

It is slower: all source data crosses the network twice, and until **detach** the target holds an extra copy of the source data in the staging tables.

| Step | File | What it does |
|---|---|---|
| generate | `generate.py` | Reads `sources.json` and the 9 dumps, then writes the per-source SQL. It stops with an error if any source column has no place in the target, so data can't be lost silently. With `--staging` it writes every script, rewritten for the staging layout, to `sql_staging/`. |
| schema | `sql/00_target_schema.sql` | Creates the superset schema with PKs, unique constraints and functions, plus the `_merge` bookkeeping schema. It runs as one transaction, so a failure leaves the target empty. |
| attach | `sql/01_attach_sources.sql` | **Direct:** creates one postgres_fdw server per source and imports it as `src_<name>`. **Through this PC:** creates the staging tables `public."src_<name>__<table>"`, then `generate.py --staging --copy` fills them. |
| preflight | `sql/02_preflight.sql` | **Read-only.** Reports row counts, cross-source overlaps, and whether the new ID ranges fit in `integer`. |
| merge | `sql/03_merge.sql` | Copies every table from every source in **one transaction**. |
| finalize | `sql/04_finalize.sql` | Adds secondary indexes (the union across all sources, plus 38 new ones based on the app's queries), FKs and `v_MigrationSummary`, and resets the sequences. |
| verify | `sql/05_verify.sql` | Checks that every source row was either loaded or written to `_merge.conflict_log`. If not, it **raises an error**. |
| detach | `sql/06_detach_sources.sql` | Drops the fdw links (Direct) or the staging tables (Through this PC). The `_merge` audit tables are kept. |

## Running it

Both the desktop app and the PowerShell script need the target server to run PostgreSQL 18, the same version as the sources (the dumps are from 18.4).

### Rights needed

The target login (or the role it switches to with **Set role** / `-Role`) needs:

| | Direct | Through this PC |
|---|---|---|
| Target database | `CREATE` on the database (for the `_merge` and `src_*` schemas), or `CREATEDB` to create a new one | only `USAGE, CREATE` on schema `public` of an existing, empty database |
| postgres_fdw | the extension installed in the target (a superuser runs `CREATE EXTENSION postgres_fdw`) and `USAGE` on it | not used |
| Source login | a login **with a password** that can `SELECT` the sources (postgres_fdw requires a password for non-superusers) | any login that can `SELECT` the sources, Kerberos included |
| Network | the **target server** reaches every source server | **this PC** reaches the target and every source server |

`../cwp2_migration_grants.sql` is a ready-made grant script for a DBA, for the Direct method into `cwp2`. If the DBA won't grant those rights, use **Through this PC**.

### Desktop app: `dist\MtrDbMigration.exe`

The exe is a single file with its own `psql.exe`; no Python or PostgreSQL install is needed. Copy it to a folder you can write to: it keeps `sources.json`, `migrate_settings.json` (never passwords), `migration_work\` and `logs\` next to itself. Rebuild it with `.\build_exe.ps1`.

| Setting | What it does |
|---|---|
| **Target server**: Host, Port, User, Password | Target login. **Test connection** shows the login, the role it runs as, and the server version. |
| **Kerberos (Windows login, no password)** | Logs in as the signed-in Windows (domain) user. **Host** must be the server's full DNS name (its `postgres/<host>` SPN), and the server needs a `gss`/`sspi` line in `pg_hba.conf` for your PC. |
| **Set role (optional)** | Every target session starts as `SET ROLE <role>`, e.g. the group `cwp_mig_prd`, so all objects are created and owned by that role. Your login must be a member of it. `SET ROLE cwp_mig_prd;` can be pasted as-is. |
| **Source login**: User, Password | Login used to read the sources. **Test source logins** connects to every listed source database and reports OK/FAIL for each. |
| **No password (Kerberos / trust, superuser)** | Direct only: postgres_fdw links without a password (superuser only). |
| **Source databases** | One per line, highest priority first. **Load all from server** fills the list. |
| **Copy method** | **Direct** or **Through this PC** (see above). Step 2, Finish and the table list detect which method a target was built with. |
| **Drop existing target (delete the database)** | Drops and re-creates the target. Only allowed for a database this tool built (one with `_merge` tables), never for a source. |
| **Clear existing target (drop all tables, keep database)** | Drops every table, view, function, schema and foreign server inside the target, but keeps the database, its owner and grants. Use it to rebuild into a database the DBA created, such as `cwp2`. |

An existing target database is used as-is if it is completely empty; otherwise Step 1 stops and lists what is left in it. Then:

1. **Run step 1** creates every table, links or stages the sources, writes the preflight report, and adds indexes and keys. With **Through this PC** it also copies all source rows into the staging tables.
2. **Step 2**: tick tables and click **Copy data of ticked tables**. Repeat as often as needed. Parent tables are added automatically, and ticking a loaded table reloads it (with its children). Each copy is verified.
3. **Build Metadata Matrix** (optional) builds `MetadataMatrix`, with one column per metadata key.
4. **Finish: remove source links / copies** drops the fdw links or the staging tables. To copy more afterwards, run step 1 again.

### PowerShell script: `run_consolidation.ps1`

Needs `psql` and Python on the PC.

```powershell
cd consolidation
# 1. edit sources.json: real dbname/host/port per source, priority order, enabled flags
# 2. report only (nothing is merged):
$env:PGPASSWORD = '<target admin password>'
.\run_consolidation.ps1 -PgHost <server> -SrcUser postgres -SrcPassword '<pw>' -Steps generate,create,schema,attach,preflight
# 3. full build (asks for confirmation after the preflight report):
.\run_consolidation.ps1 -PgHost <server> -SrcUser postgres -SrcPassword '<pw>' -DropExistingTarget
# 4. once verify says OK:
.\run_consolidation.ps1 -PgHost <server> -Steps detach
```

Kerberos login working as the group `cwp_mig_prd`, through this PC, into the existing empty `cwp2` (no new grants needed). Add `-Steps detach` to the same options once verify says OK:

```powershell
.\run_consolidation.ps1 -Kerberos -Role cwp_mig_prd -Staging -UseExistingTarget -TargetDb cwp2 `
    -PgHost HQSCWPD001v.corp.mtrc.com -User ssishadm_prj1 -SrcUser ssishadm_prj1
```

| Option | What it does |
|---|---|
| `-PgHost`, `-Port`, `-User` | Target server and login. The password comes from `$env:PGPASSWORD` (or a `.pgpass`). |
| `-TargetDb` | Target database (default: `target_dbname` in `sources.json`). |
| `-Kerberos`, `-KrbSrvName` | Log in to the target as the signed-in Windows user, with no password (`$env:PGPASSWORD` is ignored). `-PgHost` must be the server's full DNS name. If the server asks for a password anyway, psql fails at once instead of waiting at a prompt. |
| `-Role` | Every target session starts as `SET ROLE <role>`. Source logins don't use it. |
| `-SrcUser`, `-SrcPassword` | Source login. With `-Staging` and no `-SrcPassword`, the sources are read with Kerberos too. |
| `-SrcNoPassword` | Direct only: postgres_fdw user mappings without a password (superuser only). |
| `-Staging` | **Through this PC.** The scripts come from `sql_staging/`, and `attach` copies the sources into staging tables. `sql/` is not touched. |
| `-UseExistingTarget` | Build into an existing **empty** database instead of `CREATE DATABASE`. Refuses if it holds any table, view or sequence. |
| `-DropExistingTarget` | Drop and re-create the target. Only allowed for a database this script built (one with `_merge` tables). |
| `-Steps` | Run only these steps (see the table above). Default: everything except `detach`. |
| `-Yes` | Don't ask for confirmation after the preflight report. |
| `-DockerContainer`, `-PsqlPath` | Run psql inside a container / use a specific `psql.exe`. |

The script changes `PGPASSWORD`, `PGOPTIONS` and `PGKRBSRVNAME` only while it runs, and restores your previous values at the end. It works in Windows PowerShell 5.1 and PowerShell 7. When everything runs inside the `mtr-postgres` container, add `-DockerContainer mtr-postgres` and leave `host` set to `localhost` in `sources.json`. Logs are written to `logs/`.

Then point the GUI's connection string at the target database, e.g. `Database=MtrMigration_Consolidated`.

## ID strategy: per-source offsets

In every source, the surrogate keys start at 1, so they collide when merged. Each source therefore gets an offset for each ID domain:

```
new_id = old_id + offset(domain, source)
offset(source k) = sum of max_id(source 1 .. k-1)        -- ordered by sources.json
```

- Each source keeps its own row order. The resolution cursor `ScanProgress.LastResolvedFileId` depends on that order, and the offset is applied to it as well.
- Each source's rows sit in a contiguous ID range, so provenance can be looked up for any row:
  `SELECT _merge.source_of('Files', "FileId"), _merge.old_id_of('Files', "FileId") FROM "Files" ...`
- Each domain's `max_id` covers every column that references it, including references with no FK. For example, the `MigrationTasks` domain takes the max over `MigrationTasks.TaskId`, `MigrationTasks_Parsed.TaskId`, `NewChangePath_NEW.TaskId`, `Migration_Path_History.TaskId` and `break_path_mapping.task_id`. This means a dangling ID cannot collide with another source's range.
- The merge aborts if any new ID would be greater than 2,147,483,647.

| Domain | Re-numbered columns |
|---|---|
| Files | `Files.FileId`, `Metadata.FileId`, `Permissions.FileId`, `MigrationTasks.FileId`, `MigrationTasks_Parsed.FileId`, `ScanProgress.LastResolvedFileId` |
| MigrationTasks | `MigrationTasks.TaskId`, `MigrationTasks_Parsed.TaskId`, `NewChangePath_NEW.TaskId`, `Migration_Path_History.TaskId`, `break_path_mapping.task_id` |
| PermissionSets | `PermissionSets.PermissionSetId`, `PermissionSetEntries.PermissionSetId` |
| Migration_Operation_Logs | `Migration_Operation_Logs.OpId`, `Migration_Path_History.OpId` |
| ADGroups | `ADGroups.ADGroupId`. `ADGroupMembers.ADGroupId` instead goes through `_merge.adgroup_map`, because groups are de-duplicated. |
| own PK only | `Metadata`, `Permissions`, `ADGroupMembers`, `PermissionSetEntries`, `FolderPermissions`, `PermissionMatrixSummaries`, `ScanProgress`, `FolderInheritanceBreaks`, `Migration_Path_History`, `PostgreSQL_*_20260503`, `nas_/sg_permission_matrix` |

`FolderPermissions.PermissionSetId` is **not** re-numbered. It holds the text label `"Set A"`, `"Set B"`, … rather than an FK, and the app already scopes it by `SiteUrl`/`ScanDate`.

## Table-by-table mapping

**Priority**: the order of sources in `sources.json`. The default is newest schema first: `cwp_ecr, cwp_opspj3, cwp_opspj, cwp_procon, cwp_opspj2, cwp_opspj4, cwp_xrl, cwp, cwp2`. If two sources have the same natural key, the one listed first wins, and the loser is recorded in `_merge.conflict_log`.

| Target table | Sources | Rule |
|---|---|---|
| `Files` | all 9 | Offset. Unique key `OriginalSharePointPath`: when it collides, the lower-priority file is **skipped together with its Metadata, Permissions and MigrationTasks**, because the winning copy already has its own full set. For cwp2/cwp/xrl, `HasUniquePermissions` = `false`. |
| `Metadata`, `Permissions` | all 9 | Offset. Rows of skipped files are dropped. |
| `MigrationTasks` | all 9 | Offset. Tasks of skipped files are dropped and logged. Folder tasks (`FileId` NULL) are always kept. |
| `ADGroups` | all 9 | **De-duplicated by `GroupName`** (unique). Groups with the same name are folded into the first one (logged as `merged`). If a `GroupGuid` is already used under a different name, the group is loaded with `GroupGuid = NULL` (logged as `modified`). |
| `ADGroupMembers` | all 9 | Re-pointed to the surviving group. A duplicate (group, user) pair is skipped (logged). |
| `PermissionSets`, `PermissionSetEntries`, `FolderPermissions`, `PermissionMatrixSummaries` | all 9 | Offset. All rows are kept: they are scan history keyed by `SiteUrl` + `ScanDate`. |
| `ScanProgress` | all 9 | Offset. Unique key `(LibraryName, SiteName)`: the lower-priority row is skipped. `LastResolvedFileId` gets the Files offset. **cwp2/cwp/xrl get `PermissionsResolved = TRUE`** (see below). |
| `FolderInheritanceBreaks` | 7 (not cwp2/cwp) | Offset. Rows for a library whose `ScanProgress` row was skipped are dropped too, so the winner's break set isn't duplicated. |
| `ActiveLanIds` | all 9 | Union on `LanId`. The newest `UploadedAt` is kept. |
| `Migration_Operation_Logs`, `Migration_Path_History` | all 9 | Offset (`OpId`, `HistoryId`, `TaskId`). All audit rows are kept. |
| `MigrationTasks_Parsed`, `NewChangePath_NEW` | 8 (not cwp2) | `TaskId`/`FileId` offset. Rows of skipped tasks are dropped. For cwp/xrl, `ExtendFolder` = NULL. The app regenerates both tables. |
| `Folder_Structure_Final_Analysis` | 8 (not cwp2) | Copied as-is (keyed by `SiteName`; the app regenerates it). |
| `PostgreSQL_PermissionMatrix_20260503`, `PostgreSQL_SiteGroupPermission_20260503` | all 9 | Offset on `ID`, plus the new column **`SourceDb`**. All rows are kept. If the same 0503 import was loaded into several DBs, the same data appears once per `SourceDb`. |
| `break_path_mapping` | opspj2, opspj4 | `task_id` gets the MigrationTasks offset (assumes it holds `MigrationTasks.TaskId`), plus `source_db`. |
| `nas_permission_matrix`, `sg_permission_matrix` | opspj2, opspj4 | Offset on `id`, plus `source_db`. `given_through` is widened to `varchar(1000)` (opspj4 had 100). |
| `sitemapping` | opspj2, opspj4 | PK `sp2013_url`: the first source wins (logged). Adds `source_db`. |
| `Folders` (uuid PK) | opspj4 | PK `UniqueId`: the first source wins. Adds `source_db`. |
| `v_MigrationSummary` | view | Re-created in `04_finalize.sql`. It was missing in cwp2, cwp and procon. |
| `claim_migration_tasks`, `reset_stale_tasks` | functions | Identical in all sources. Copied once. |
| `bak_*`, `bak_MigrationTasks` | — | **Not migrated.** |

### `SiteId` and the `Sites` table

Every table that has `"SiteName" varchar(256)` also gets a `"SiteId" integer` column, placed right after it. There are 13 such tables: `Files`, `Metadata`, `Permissions`, `ADGroups`, `ADGroupMembers`, `PermissionSets`, `FolderPermissions`, `PermissionMatrixSummaries`, `MigrationTasks`, `ScanProgress`, `FolderInheritanceBreaks`, `MigrationTasks_Parsed` and `Folder_Structure_Final_Analysis`. The new column references the new lookup table:

```sql
"Sites" ("SiteId" PK, "SiteName" UNIQUE, "CreatedAt")
```

- **During the merge:** `Sites` is filled first from every distinct non-empty `SiteName` across all sources and tables, numbered 1..n in alphabetical order. Each row's `SiteId` is then set during its insert through a join to `Sites`, so there is no large `UPDATE` afterwards.
- **After the merge:** every one of these tables has a `BEFORE INSERT OR UPDATE OF "SiteName"` trigger that calls `set_site_id()`. It sets `SiteId` from `SiteName` and adds the site to `Sites` the first time a new site name appears. This covers rows the GUI writes through EF6 and the Metadata bulk `COPY`, neither of which knows about `SiteId`. If you need to make a huge Metadata import faster, you can switch the trigger off temporarily with `ALTER TABLE "Metadata" DISABLE TRIGGER "trg_Metadata_site_id"`.
- **No site:** a NULL or `''` `SiteName` gives `SiteId = NULL`.
- **Index and foreign key:** each of the 13 tables gets an index on `SiteId` and a foreign key to `Sites`.
- **Verify:** `05_verify.sql` fails if any row's `SiteId` doesn't match its `SiteName`.
- **Tables the app rebuilds:** *Analyze* rebuilds `MigrationTasks_Parsed` with `SELECT *` from `MigrationTasks`, so it keeps `SiteId`. `Folder_Structure_Final_Analysis` is rebuilt without `SiteId`, so after a re-Analyze, join it to `Sites` on `SiteName` instead.
- **App changes:** none. EF6, `claim_migration_tasks()` and every raw query read columns by name, so they ignore the extra column.

### Why `PermissionsResolved = TRUE` for cwp2 / cwp / xrl

These three databases predate the post-scan permission resolution pass (`migration_permission_resolution.sql`). Their `Permissions` rows were written per file during the scan. If `PermissionsResolved` took the column default (`false`) instead:
- `PermissionResolutionService.ResumePendingAsync` would re-resolve **every** file on startup, because `HasUniquePermissions` is false for all of them. That would insert duplicate `Inherited` permission rows.
- `MigrationOrchestrator` would block the step-2 NTFS apply until those libraries were "resolved".

## No changes to the app tables

The core tables keep the column layout from `mtr-phase3/scripts/init.sql` and `init_pathbreak.sql`. The only addition is the trigger-maintained `SiteId` column described above. No `SourceDb` column is added, so EF6, `claim_migration_tasks()` (`RETURNING *`) and `PathBreakRepository` all behave the same as before. Provenance for those tables comes from the ID ranges in `_merge.id_offset`.

## Things to know after the merge

- **Undo (Path Analysis tab)** reverts the most recent successful *Apply Path Change* across **all** merged sources. Before, it only looked within one DB.
- **Tasks in Status=1 (Processing)** at merge time had been claimed by workers in the old DBs. `reset_stale_tasks()` requeues them; preflight section I counts them.
- **`Folder_Structure_Final_Analysis` / `MigrationTasks_Parsed`** reflect each source's last analysis. Re-run *Analyze* in the GUI for a fresh combined view.
- **Stopping writes during the merge**: stop workers and the GUI against the source DBs while it runs. The Direct method reads the sources live during the merge. Through this PC reads them once, during the copy into the staging tables, so changes made after that are not picked up.

## Audit queries

```sql
-- what was not copied 1:1
SELECT table_name, source_db, action, reason, count(*) FROM _merge.conflict_log GROUP BY 1,2,3,4;
-- where did a file come from
SELECT "FileId", _merge.source_of('Files', "FileId") AS src, _merge.old_id_of('Files', "FileId") AS old_id FROM "Files" WHERE "FileName" = '...';
-- ID ranges per source
SELECT * FROM _merge.id_offset ORDER BY table_name, id_offset;
-- remove the audit trail when no longer needed
DROP SCHEMA _merge CASCADE;
```

A target built **Through this PC** has the same tables and functions in `public` instead: write `public._merge_conflict_log`, `public._merge_source_of(...)`, `public._merge_id_offset` and so on. To remove them, drop the `public._merge_*` tables and functions.

## Tested

The pipeline was run end to end on PostgreSQL 16.4 against 9 source DBs built from the real dumps. The PG18-only lines (`\restrict`, `transaction_timeout`, named NOT NULL) were stripped for that test. The test data had ID gaps and deliberate overlaps: a shared site, a shared AD group, a GUID clash, a sitemapping clash, and dangling TaskIds.

- `05_verify`: all OK.
- A natural-key comparison of every parent/child relationship per source found 0 differences. It checked Metadata, Permissions and Tasks against file paths, members against group names, the path history against tasks and ops, the `LastResolvedFileId` cursor, and Parsed against Tasks.
- The FKs were created without error, and the sequences continue after the merged max.

The two copy methods were checked against each other on PostgreSQL 18 with 3 sources built from the real `cwp_ecr`, `cwp_opspj4` and `cwp2` dumps (1,220 rows with deliberate overlaps):

- **Through this PC** ran with only `CREATE` on schema `public`, in both the app (with **Set role**) and the script (`-Staging`, under PowerShell 5.1 and 7). The script was also run with `-Kerberos -Role`, using Windows SSPI single sign-on in place of a domain Kerberos server.
- Every run passed verify, and Direct and Through this PC produced **identical rows** in every table (same IDs, SiteIds and conflict log).
- Not tested yet: a real Kerberos domain login, Docker in the staging mode, and production data volumes.
