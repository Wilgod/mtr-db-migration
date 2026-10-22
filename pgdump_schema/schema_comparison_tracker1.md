# 9-DB Schema Consolidation — Comparison Tracker

Legend: ✅ present · ❌ absent · (blank) not yet uploaded
Core = identical column/PK/FK/index definitions across all DBs analyzed so far.

## Object presence matrix

| Object | Type | ecr | opspj | opspj2 | DB4 | DB5 | DB6 | DB7 | DB8 | DB9 |
|---|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| Files | Core table | ✅ | ✅ | ✅ | | | | | | |
| Metadata | Core table | ✅ | ✅ | ✅ | | | | | | |
| Permissions | Core table | ✅ | ✅ | ✅ | | | | | | |
| ADGroups | Core table | ✅ | ✅ | ✅ | | | | | | |
| ADGroupMembers | Core table | ✅ | ✅ | ✅ | | | | | | |
| PermissionSets | Core table | ✅ | ✅ | ✅ | | | | | | |
| PermissionSetEntries | Core table | ✅ | ✅ | ✅ | | | | | | |
| FolderPermissions | Core table | ✅ | ✅ | ✅ | | | | | | |
| FolderInheritanceBreaks | Core table | ✅ | ✅ | ✅ | | | | | | |
| PermissionMatrixSummaries | Core table | ✅ | ✅ | ✅ | | | | | | |
| MigrationTasks | Core table | ✅ | ✅ | ✅ | | | | | | |
| ScanProgress | Core table | ✅ | ✅ | ✅ | | | | | | |
| ActiveLanIds | Core table | ✅ | ✅ | ✅ | | | | | | |
| Migration_Operation_Logs | Core table | ✅ | ✅ | ✅ | | | | | | |
| Migration_Path_History | Core table | ✅ | ✅ | ✅ | | | | | | |
| PostgreSQL_PermissionMatrix_20260503 | Source import | ✅ | ✅ | ✅ | | | | | | |
| PostgreSQL_SiteGroupPermission_20260503 | Source import | ✅ | ✅ | ✅ | | | | | | |
| MigrationTasks_Parsed | Derived | ✅ | ✅ | ✅ | | | | | | |
| NewChangePath_NEW | Derived | ✅ | ✅ | ✅ | | | | | | |
| Folder_Structure_Final_Analysis | Derived | ✅ | ✅ | ✅ | | | | | | |
| v_MigrationSummary | View | ✅ | ✅ | ✅ | | | | | | |
| claim_migration_tasks | Function | ✅ | ✅ | ✅ | | | | | | |
| reset_stale_tasks | Function | ✅ | ✅ | ✅ | | | | | | |
| bak_Migration_Path_History_20260616_2258 | Backup (drop) | ❌ | ✅ | ❌ | | | | | | |
| bak_Migration_Path_History_20260612 | Backup (drop) | ❌ | ❌ | ✅ | | | | | | |
| break_path_mapping | Feature (extra) | ❌ | ❌ | ✅ | | | | | | |
| nas_permission_matrix | Feature (extra) | ❌ | ❌ | ✅ | | | | | | |
| sg_permission_matrix | Feature (extra) | ❌ | ❌ | ✅ | | | | | | |
| sitemapping | Feature (extra) | ❌ | ❌ | ✅ | | | | | | |

## Decisions to lock before migration

- Target superset = 20 core tables + view + 2 functions, plus any feature tables you want global
  (break_path_mapping, nas_permission_matrix, sg_permission_matrix, sitemapping).
- Exclude all bak_* dated backup tables from the consolidated DB.
- Add a source_db discriminator column to every core table (PK remap needed — all use identity from 1).
- Feature tables use snake_case + no SiteName; decide tagging strategy for source separation.

## Pending
- Batch 2: DB4, DB5, DB6
- Batch 3: DB7, DB8, DB9
