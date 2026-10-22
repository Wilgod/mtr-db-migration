# MTR Phase 3: consolidating the source DBs into one database

This merges the `cwp*` PostgreSQL databases (schema exports in `../pgdump_schema/`) into a single new database, **`MtrMigration_Consolidated`**. The mtr-phase3 GUI can use that database without any code changes.

## How it works

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

| Step | File | What it does |
|---|---|---|
| generate | `generate.py` | Reads `sources.json` and the 9 dumps, then writes the per-source SQL. It stops with an error if any source column has no place in the target, so data can't be lost silently. |
| schema | `sql/00_target_schema.sql` | Creates the superset schema with PKs, unique constraints and functions, plus the `_merge` bookkeeping schema. |
| attach | `sql/01_attach_sources.sql` | Creates one postgres_fdw server per source and imports it as `src_<name>`. |
| preflight | `sql/02_preflight.sql` | **Read-only.** Reports row counts, cross-source overlaps, and whether the new ID ranges fit in `integer`. |
| merge | `sql/03_merge.sql` | Copies every table from every source in **one transaction**. |
| finalize | `sql/04_finalize.sql` | Adds secondary indexes (the union across all sources, plus 38 new ones based on the app's queries), FKs and `v_MigrationSummary`, and resets the sequences. |
| verify | `sql/05_verify.sql` | Checks that every source row was either loaded or written to `_merge.conflict_log`. If not, it **raises an error**. |
| detach | `sql/06_detach_sources.sql` | Drops the fdw links. The `_merge` audit schema is kept. |

## Running it

Requirements:
- The target server runs PostgreSQL 18, the same version as the sources (the dumps are from 18.4).
- The `postgres_fdw` extension is available. It ships in the standard and alpine images.
- The target server can reach every source server.

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

When everything runs inside the `mtr-postgres` container, add `-DockerContainer mtr-postgres` and leave `host` set to `localhost` in `sources.json`. Logs are written to `logs/`.

Then point the GUI's connection string at `Database=MtrMigration_Consolidated`.

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
- **Stopping writes during the merge**: stop workers and the GUI against the source DBs while it runs. The merge reads the sources live.

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

## Tested

The pipeline was run end to end on PostgreSQL 16.4 against 9 source DBs built from the real dumps. The PG18-only lines (`\restrict`, `transaction_timeout`, named NOT NULL) were stripped for that test. The test data had ID gaps and deliberate overlaps: a shared site, a shared AD group, a GUID clash, a sitemapping clash, and dangling TaskIds.

- `05_verify`: all OK.
- A natural-key comparison of every parent/child relationship per source found 0 differences. It checked Metadata, Permissions and Tasks against file paths, members against group names, the path history against tasks and ops, the `LastResolvedFileId` cursor, and Parsed against Tasks.
- The FKs were created without error, and the sequences continue after the merged max.
