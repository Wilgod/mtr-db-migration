# 9-DB Schema Consolidation — Comparison Tracker

Legend: ✅ present · ❌ absent · (blank) not yet uploaded
Core = identical column/PK/FK/index definitions across all DBs analyzed so far.

DBs analyzed: 6 of 9  (ecr, opspj, opspj2, procon, opspj3, opspj4)
Pending: 3 (batch 3)

## Object presence matrix

| Object | Type | ecr | opspj | opspj2 | procon | opspj3 | opspj4 | DB7 | DB8 | DB9 |
|---|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| Files | Core table | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | | | |
| Metadata | Core table | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | | | |
| Permissions | Core table | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | | | |
| ADGroups | Core table | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | | | |
| ADGroupMembers | Core table | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | | | |
| PermissionSets | Core table | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | | | |
| PermissionSetEntries | Core table | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | | | |
| FolderPermissions | Core table | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | | | |
| FolderInheritanceBreaks | Core table | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | | | |
| PermissionMatrixSummaries | Core table | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | | | |
| MigrationTasks | Core table | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | | | |
| ScanProgress | Core table | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | | | |
| ActiveLanIds | Core table | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | | | |
| Migration_Operation_Logs | Core table | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | | | |
| Migration_Path_History | Core table | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | | | |
| PostgreSQL_PermissionMatrix_20260503 | Source import | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | | | |
| PostgreSQL_SiteGroupPermission_20260503 | Source import | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | | | |
| MigrationTasks_Parsed | Derived | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | | | |
| NewChangePath_NEW | Derived | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | | | |
| Folder_Structure_Final_Analysis | Derived | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | | | |
| v_MigrationSummary | View | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | | | |
| claim_migration_tasks | Function | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | | | |
| reset_stale_tasks | Function | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | | | |
| Folders (uuid PK) | Feature (extra) | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ | | | |
| break_path_mapping | Feature (extra) | ❌ | ❌ | ✅ | ❌ | ❌ | ✅ | | | |
| nas_permission_matrix | Feature (extra) | ❌ | ❌ | ✅ | ❌ | ❌ | ✅ | | | |
| sg_permission_matrix | Feature (extra) | ❌ | ❌ | ✅ | ❌ | ❌ | ✅ | | | |
| sitemapping | Feature (extra) | ❌ | ❌ | ✅ | ❌ | ❌ | ✅ | | | |
| bak_Migration_Path_History_20260612 | Backup (drop) | ❌ | ❌ | ✅ | ❌ | ❌ | ❌ | | | |
| bak_Migration_Path_History_20260616_2258 | Backup (drop) | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ | | | |
| bak_Migration_Path_History_20260615_2231 | Backup (drop) | ❌ | ❌ | ❌ | ❌ | ✅ | ❌ | | | |
| bak_MigrationTasks | Backup (drop) | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ | | | |

## Type drift log

| Table.Column | Value A | Value B | Target (widest) |
|---|---|---|---|
| nas_permission_matrix.given_through | varchar(1000) [opspj2] | varchar(100) [opspj4] | varchar(1000) |
| sg_permission_matrix.given_through | varchar(1000) [opspj2] | varchar(100) [opspj4] | varchar(1000) |

## Index drift log

| Table | Index | Present in |
|---|---|---|
| nas_permission_matrix | idx_nas_item_path, idx_nas_url_account | opspj4 only |
| sg_permission_matrix | idx_sg_perm_principal_type, idx_sg_perm_url_account | opspj2, opspj4 |

## Decisions to lock before migration

- Target superset = 20 core tables + view + 2 functions, plus feature-table cluster:
  break_path_mapping, nas_permission_matrix, sg_permission_matrix, sitemapping (recurring in opspj2 + opspj4).
- Decide on Folders (uuid PK) — currently unique to opspj4.
- Use widest type on drift (given_through -> varchar(1000)).
- Include the full index set (union of opspj2 + opspj4 indexes).
- Exclude ALL bak_* dated backups + bak_MigrationTasks from the consolidated DB.
- procon is missing v_MigrationSummary — don't template the target from procon.
- Add source_db discriminator to every core table (PK remap; all identity from 1).
- Feature tables use snake_case + no SiteName; decide tagging strategy for source separation.

## Richest baseline so far: opspj4 (most feature tables + unique Folders table)

## Pending
- Batch 3: DB7, DB8, DB9
