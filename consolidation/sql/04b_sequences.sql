-- =============================================================================
-- 04b_sequences.sql — run after the data is loaded (after 03_merge.sql, or after
-- every data copy in MtrDbMigration step 2). Safe to run again.
-- Sequence/identity reset, privileges, statistics.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Sequences: continue after the highest merged ID so new rows never collide
-- -----------------------------------------------------------------------------
SELECT setval('public."Sites_SiteId_seq"', COALESCE((SELECT MAX("SiteId") FROM public."Sites"), 0) + 1, false);
SELECT setval('public."Files_FileId_seq"', COALESCE((SELECT MAX("FileId") FROM public."Files"), 0) + 1, false);
SELECT setval('public."Metadata_MetadataId_seq"', COALESCE((SELECT MAX("MetadataId") FROM public."Metadata"), 0) + 1, false);
SELECT setval('public."Permissions_PermissionId_seq"', COALESCE((SELECT MAX("PermissionId") FROM public."Permissions"), 0) + 1, false);
SELECT setval('public."ADGroups_ADGroupId_seq"', COALESCE((SELECT MAX("ADGroupId") FROM public."ADGroups"), 0) + 1, false);
SELECT setval('public."ADGroupMembers_MemberId_seq"', COALESCE((SELECT MAX("MemberId") FROM public."ADGroupMembers"), 0) + 1, false);
SELECT setval('public."PermissionSets_PermissionSetId_seq"', COALESCE((SELECT MAX("PermissionSetId") FROM public."PermissionSets"), 0) + 1, false);
SELECT setval('public."PermissionSetEntries_PermissionSetEntryId_seq"', COALESCE((SELECT MAX("PermissionSetEntryId") FROM public."PermissionSetEntries"), 0) + 1, false);
SELECT setval('public."FolderPermissions_FolderPermissionId_seq"', COALESCE((SELECT MAX("FolderPermissionId") FROM public."FolderPermissions"), 0) + 1, false);
SELECT setval('public."PermissionMatrixSummaries_PermissionMatrixSummaryId_seq"', COALESCE((SELECT MAX("PermissionMatrixSummaryId") FROM public."PermissionMatrixSummaries"), 0) + 1, false);
-- MigrationTasks: also past dangling TaskIds kept in Migration_Path_History / break_path_mapping
SELECT setval('public."MigrationTasks_TaskId_seq"', (SELECT COALESCE(MAX(id_offset + src_max_id), 0) + 1 FROM _merge.id_offset WHERE table_name = 'MigrationTasks'), false);
SELECT setval('public."ScanProgress_ScanProgressId_seq"', COALESCE((SELECT MAX("ScanProgressId") FROM public."ScanProgress"), 0) + 1, false);
SELECT setval('public."FolderInheritanceBreaks_FolderInheritanceBreakId_seq"', COALESCE((SELECT MAX("FolderInheritanceBreakId") FROM public."FolderInheritanceBreaks"), 0) + 1, false);
SELECT setval('public."Migration_Operation_Logs_OpId_seq"', COALESCE((SELECT MAX("OpId") FROM public."Migration_Operation_Logs"), 0) + 1, false);
SELECT setval('public."Migration_Path_History_HistoryId_seq"', COALESCE((SELECT MAX("HistoryId") FROM public."Migration_Path_History"), 0) + 1, false);
SELECT setval('public.nas_permission_matrix_id_seq', COALESCE((SELECT MAX(id) FROM public.nas_permission_matrix), 0) + 1, false);
SELECT setval('public.sg_permission_matrix_id_seq', COALESCE((SELECT MAX(id) FROM public.sg_permission_matrix), 0) + 1, false);
SELECT setval('public."PostgreSQL_PermissionMatrix_20260503_ID_seq"', COALESCE((SELECT MAX("ID") FROM public."PostgreSQL_PermissionMatrix_20260503"), 0) + 1, false);
SELECT setval('public."PostgreSQL_SiteGroupPermission_20260503_ID_seq"', COALESCE((SELECT MAX("ID") FROM public."PostgreSQL_SiteGroupPermission_20260503"), 0) + 1, false);

GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO postgres;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO postgres;

ANALYZE;
