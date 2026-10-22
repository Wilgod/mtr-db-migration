# 9-DB Schema Consolidation — FINAL Comparison Tracker

All 9 DBs analyzed. Legend: ✅ present · ❌ absent
Evolution order (by OID / creation time, simplest → richest):
  cwp2 → cwp → xrl → opspj4 → opspj2 → procon → opspj → opspj3 → ecr

## A. Object presence matrix

| Object | Type | cwp2 | cwp | xrl | opspj4 | opspj2 | procon | opspj | opspj3 | ecr |
|---|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| Files | Core | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Metadata | Core | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Permissions | Core | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| ADGroups | Core | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| ADGroupMembers | Core | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| PermissionSets | Core | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| PermissionSetEntries | Core | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| FolderPermissions | Core | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| PermissionMatrixSummaries | Core | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| MigrationTasks | Core | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| ScanProgress | Core | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| ActiveLanIds | Core | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Migration_Operation_Logs | Core | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Migration_Path_History | Core | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| PostgreSQL_PermissionMatrix_20260503 | Import | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| PostgreSQL_SiteGroupPermission_20260503 | Import | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| FolderInheritanceBreaks | Core(new) | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| MigrationTasks_Parsed | Derived | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| NewChangePath_NEW | Derived | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Folder_Structure_Final_Analysis | Derived | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| v_MigrationSummary | View | ❌ | ❌ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ |
| claim_migration_tasks | Function | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| reset_stale_tasks | Function | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Folders (uuid PK) | Feature | ❌ | ❌ | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| break_path_mapping | Feature | ❌ | ❌ | ❌ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |
| nas_permission_matrix | Feature | ❌ | ❌ | ❌ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |
| sg_permission_matrix | Feature | ❌ | ❌ | ❌ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |
| sitemapping | Feature | ❌ | ❌ | ❌ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |
| bak_* / bak_MigrationTasks | Backup(drop) | ❌ | ❌ | ❌ | ✅ | ✅ | ❌ | ✅ | ✅ | ❌ |

## B. Core-table COLUMN drift (critical for migration)

| Table.Column | Absent in | Present in | Migration action |
|---|---|---|---|
| Files.HasUniquePermissions | cwp2, cwp, xrl | 6 newer | Apply default false on insert from old DBs |
| ScanProgress.PermissionsResolved | cwp2, cwp, xrl | 6 newer | Apply default false |
| ScanProgress.LastResolvedFileId | cwp2, cwp, xrl | 6 newer | Insert NULL |
| NewChangePath_NEW.ExtendFolder | cwp, xrl (cwp2 no table) | 6 newer | Insert NULL |

## C. Feature-table TYPE / INDEX drift

| Object | Variant A | Variant B | Target |
|---|---|---|---|
| nas_permission_matrix.given_through | varchar(1000) [opspj2] | varchar(100) [opspj4] | varchar(1000) |
| sg_permission_matrix.given_through | varchar(1000) [opspj2] | varchar(100) [opspj4] | varchar(1000) |
| nas idx (idx_nas_item_path, idx_nas_url_account) | opspj4 only | — | include |
| sg idx (idx_sg_perm_*) | opspj2 + opspj4 | — | include |

## D. Consolidated TARGET schema (superset) — recommendation

RICHEST BASELINE: **opspj4** (all core+new columns, all feature tables, unique Folders).
Cross-check columns against ecr (newest by OID) to confirm latest core definitions.

Include:
- 15 always-present core tables (full latest column set)
- FolderInheritanceBreaks (post-cwp2/cwp addition)
- Analysis layer: MigrationTasks_Parsed, NewChangePath_NEW (with ExtendFolder),
  Folder_Structure_Final_Analysis
- v_MigrationSummary view + both functions
- 2 source-import tables
- Feature cluster: break_path_mapping, nas_permission_matrix, sg_permission_matrix, sitemapping
- Folders (uuid PK) — confirm if wanted globally
- Union of all indexes; widest types on drift

Exclude:
- ALL bak_* dated backups + bak_MigrationTasks

## E. Migration mechanics — locked risks

1. PK collisions: every core table uses identity starting at 1 -> add source_db column
   + remap PKs (surrogate) or use composite (source_db, original_id).
2. FK order: load Files/ADGroups/PermissionSets (parents) before children;
   or defer constraints during bulk load.
3. Old-DB column gaps (section B): use explicit column lists + defaults, never
   positional INSERT ... SELECT *.
4. Feature tables use snake_case + no SiteName -> add source tag for separability.
5. cwp2 lacks analysis layer entirely; nothing to migrate there for those tables.
