-- =============================================================================
-- 04_finalize.sql — run after 03_merge.sql (or on the empty tables: MtrDbMigration step 1)
-- Secondary indexes (union of all 9 sources), foreign keys, SiteId trigger, view.
-- Sequence reset, privileges and ANALYZE are in 04b_sequences.sql (run after the data).
-- =============================================================================

\set ON_ERROR_STOP on
\timing on
SET maintenance_work_mem = '1GB';

-- -----------------------------------------------------------------------------
-- Secondary indexes
-- -----------------------------------------------------------------------------
CREATE INDEX "IX_Files_FileName" ON public."Files" USING btree ("FileName");
CREATE INDEX "IX_Files_MigrationStatus" ON public."Files" USING btree ("MigrationStatus");
CREATE INDEX "IX_Files_SiteName" ON public."Files" USING btree ("SiteName");
CREATE INDEX "IX_Files_SharePointUniqueId" ON public."Files" USING btree ("SharePointUniqueId");
CREATE INDEX "IX_Files_PermResolution" ON public."Files" USING btree ("SiteName", "MigrationStatus") WHERE ("HasUniquePermissions" = false);
CREATE INDEX "IX_Metadata_MetadataKey" ON public."Metadata" USING btree ("MetadataKey");
CREATE INDEX "IX_Permissions_OriginalSharePointGroup" ON public."Permissions" USING btree ("OriginalSharePointGroup");
CREATE INDEX "IX_Permissions_ApplyMethod" ON public."Permissions" USING btree ("ApplyMethod");
CREATE INDEX "IX_ADGroups_SiteName" ON public."ADGroups" USING btree ("SiteName");
CREATE INDEX "IX_PermissionSets_PermissionKey" ON public."PermissionSets" USING btree ("PermissionKey");
CREATE INDEX "IX_PermissionSets_SiteUrl" ON public."PermissionSets" USING btree ("SiteUrl");
CREATE INDEX "IX_PermissionSets_ScanDate" ON public."PermissionSets" USING btree ("ScanDate");
CREATE INDEX "IX_PermissionSets_SiteName" ON public."PermissionSets" USING btree ("SiteName");
CREATE INDEX "IX_PermissionSetEntries_PermissionSetId" ON public."PermissionSetEntries" USING btree ("PermissionSetId");
CREATE INDEX "IX_FolderPermissions_FolderPath" ON public."FolderPermissions" USING btree ("FolderPath");
CREATE INDEX "IX_FolderPermissions_SiteUrl" ON public."FolderPermissions" USING btree ("SiteUrl");
CREATE INDEX "IX_FolderPermissions_ScanDate" ON public."FolderPermissions" USING btree ("ScanDate");
CREATE INDEX "IX_FolderPermissions_SiteName" ON public."FolderPermissions" USING btree ("SiteName");
CREATE INDEX "IX_PermissionMatrixSummaries_SiteUrl" ON public."PermissionMatrixSummaries" USING btree ("SiteUrl");
CREATE INDEX "IX_PermissionMatrixSummaries_ScanDate" ON public."PermissionMatrixSummaries" USING btree ("ScanDate");
CREATE INDEX "IX_PermissionMatrixSummaries_SiteName" ON public."PermissionMatrixSummaries" USING btree ("SiteName");
CREATE INDEX "IX_MigrationTasks_Status" ON public."MigrationTasks" USING btree ("Status");
CREATE INDEX "IX_MigrationTasks_Status_Priority" ON public."MigrationTasks" USING btree ("Status", "Priority");
CREATE INDEX "IX_MigrationTasks_WorkerId" ON public."MigrationTasks" USING btree ("WorkerId");
CREATE INDEX "IX_MigrationTasks_TaskType" ON public."MigrationTasks" USING btree ("TaskType");
CREATE INDEX "IX_MigrationTasks_ClaimedAt" ON public."MigrationTasks" USING btree ("ClaimedAt");
CREATE INDEX "IX_MigrationTasks_SiteName" ON public."MigrationTasks" USING btree ("SiteName");
CREATE INDEX "IX_FolderInheritanceBreaks_LibSite" ON public."FolderInheritanceBreaks" USING btree ("LibraryName", "SiteName");
CREATE INDEX "IX_FolderInheritanceBreaks_FolderPath" ON public."FolderInheritanceBreaks" USING btree ("FolderPath");
CREATE INDEX "ix_MigrationOperationLogs_StepStatusTime" ON public."Migration_Operation_Logs" USING btree ("StepName", "Status", "ExecutionTime" DESC);
CREATE INDEX "ix_MigrationPathHistory_OpId" ON public."Migration_Path_History" USING btree ("OpId");
-- feature tables: opspj4 had all four, opspj2 only the sg pair -> union
CREATE INDEX idx_nas_item_path ON public.nas_permission_matrix USING btree (item_path);
CREATE INDEX idx_nas_url_account ON public.nas_permission_matrix USING btree (url, account_name);
CREATE INDEX idx_sg_perm_principal_type ON public.sg_permission_matrix USING btree (principal_type);
CREATE INDEX idx_sg_perm_url_account ON public.sg_permission_matrix USING btree (url, account_name);
-- provenance lookups on the new tag columns
CREATE INDEX "IX_PostgreSQL_PermissionMatrix_20260503_SourceDb" ON public."PostgreSQL_PermissionMatrix_20260503" ("SourceDb");
CREATE INDEX "IX_PostgreSQL_SiteGroupPermission_20260503_SourceDb" ON public."PostgreSQL_SiteGroupPermission_20260503" ("SourceDb");

-- -----------------------------------------------------------------------------
-- Additional indexes (NEW — not in any source DB)
-- Chosen from the filters/joins in mtr-phase3 DatabaseRepository.cs and
-- PathBreakRepository.cs. Long path columns (varchar(2000)/text) use HASH
-- indexes for equality lookups: a btree entry is limited to ~2.7 KB, which a
-- 2000-char path with CJK characters can exceed and would abort the build.
-- -----------------------------------------------------------------------------

-- Files: permission-resolution / metadata-backfill chunking (SiteName + FileId > cursor)
CREATE INDEX "IX_Files_SiteName_FileId" ON public."Files" USING btree ("SiteName", "FileId");

-- Metadata: FK column (ON DELETE CASCADE from Files + "files without metadata" backfill)
CREATE INDEX "IX_Metadata_FileId" ON public."Metadata" USING btree ("FileId");
CREATE INDEX "IX_Metadata_SiteName" ON public."Metadata" USING btree ("SiteName");

-- Permissions: FK column (cascade + per-file lookups), per-site apply, pending-apply scan
CREATE INDEX "IX_Permissions_FileId" ON public."Permissions" USING btree ("FileId");
CREATE INDEX "IX_Permissions_SiteName" ON public."Permissions" USING btree ("SiteName");
CREATE INDEX "IX_Permissions_NotApplied" ON public."Permissions" USING btree ("SiteName", "FileId") WHERE ("NtfsPermissionApplied" = false);
CREATE INDEX "IX_Permissions_ADGroupName" ON public."Permissions" USING btree ("ADGroupName");

-- MigrationTasks: FK column + latest FileDownload task per file (File Verify / re-migrate / break-path apply)
CREATE INDEX "IX_MigrationTasks_FileId_TaskType" ON public."MigrationTasks" USING btree ("FileId", "TaskType");
CREATE INDEX "IX_MigrationTasks_Site_Type_Status" ON public."MigrationTasks" USING btree ("SiteName", "TaskType", "Status");
CREATE INDEX "IX_MigrationTasks_ServerRelativeUrl" ON public."MigrationTasks" USING hash ("ServerRelativeUrl");

-- ADGroups / members
CREATE INDEX "IX_ADGroups_SourceSharePointGroup" ON public."ADGroups" USING btree ("SourceSharePointGroup");
CREATE INDEX "IX_ADGroupMembers_UserPrincipalName" ON public."ADGroupMembers" USING btree ("UserPrincipalName");
CREATE INDEX "IX_ADGroupMembers_SiteName" ON public."ADGroupMembers" USING btree ("SiteName");

-- Permission matrix: FolderPermissions.PermissionSetId joins PermissionSets.SetId (text label)
CREATE INDEX "IX_PermissionSets_SetId" ON public."PermissionSets" USING btree ("SetId", "SiteUrl");
CREATE INDEX "IX_FolderPermissions_PermissionSetId" ON public."FolderPermissions" USING btree ("PermissionSetId");

-- ScanProgress: per-site status queries (LastScanCompletedAt / PermissionsResolved)
CREATE INDEX "IX_ScanProgress_SiteName" ON public."ScanProgress" USING btree ("SiteName");

-- FolderInheritanceBreaks: per-site lookups
CREATE INDEX "IX_FolderInheritanceBreaks_SiteName" ON public."FolderInheritanceBreaks" USING btree ("SiteName");

-- ActiveLanIds: case-insensitive match
CREATE INDEX "IX_ActiveLanIds_Lower" ON public."ActiveLanIds" USING btree (lower("LanId"));

-- Break-path audit: Undo / shortcut export join history -> MigrationTasks by TaskId
CREATE INDEX "IX_MigrationPathHistory_TaskId" ON public."Migration_Path_History" USING btree ("TaskId");

-- Break-path working tables (dropped & recreated by the app on re-Analyze; useful until then)
CREATE INDEX "IX_MigrationTasks_Parsed_SiteName_PathStatus" ON public."MigrationTasks_Parsed" USING btree ("SiteName", "PathStatus");
CREATE INDEX "IX_MigrationTasks_Parsed_FileId" ON public."MigrationTasks_Parsed" USING btree ("FileId");
CREATE INDEX "IX_Folder_Structure_Final_Analysis_Site_Level" ON public."Folder_Structure_Final_Analysis" USING btree ("SiteName", folder_level);
CREATE INDEX "IX_Folder_Structure_Final_Analysis_Path" ON public."Folder_Structure_Final_Analysis" USING hash (folder_path);

-- Source-import tables
CREATE INDEX "IX_PostgreSQL_PermissionMatrix_20260503_ItemPath" ON public."PostgreSQL_PermissionMatrix_20260503" USING btree ("Item_path");
CREATE INDEX "IX_PostgreSQL_PermissionMatrix_20260503_Url_Account" ON public."PostgreSQL_PermissionMatrix_20260503" USING btree ("UrlValue", "Account_name");
CREATE INDEX "IX_PostgreSQL_SiteGroupPermission_20260503_NameValue" ON public."PostgreSQL_SiteGroupPermission_20260503" USING btree ("NameValue");

-- Feature tables: source tag + path lookups
CREATE INDEX idx_folders_source_db ON public."Folders" USING btree (source_db);
CREATE INDEX idx_folders_path ON public."Folders" USING hash (path);
CREATE INDEX idx_break_path_mapping_source_db ON public.break_path_mapping USING btree (source_db);
CREATE INDEX idx_break_path_mapping_org_file_path ON public.break_path_mapping USING hash (org_file_path);
CREATE INDEX idx_break_path_mapping_sp2013_file_path ON public.break_path_mapping USING hash (sp2013_file_path);
CREATE INDEX idx_nas_source_db ON public.nas_permission_matrix USING btree (source_db);
CREATE INDEX idx_sg_source_db ON public.sg_permission_matrix USING btree (source_db);
CREATE INDEX idx_sg_item_path ON public.sg_permission_matrix USING hash (item_path);
CREATE INDEX idx_sitemapping_source_db ON public.sitemapping USING btree (source_db);
CREATE INDEX idx_sitemapping_site_prefix ON public.sitemapping USING btree (site_prefix);

-- -----------------------------------------------------------------------------
-- Foreign keys (fail here = an orphan slipped through the merge)
-- -----------------------------------------------------------------------------
ALTER TABLE ONLY public."Metadata"
    ADD CONSTRAINT "Metadata_FileId_fkey" FOREIGN KEY ("FileId") REFERENCES public."Files"("FileId") ON DELETE CASCADE;
ALTER TABLE ONLY public."Permissions"
    ADD CONSTRAINT "Permissions_FileId_fkey" FOREIGN KEY ("FileId") REFERENCES public."Files"("FileId") ON DELETE CASCADE;
ALTER TABLE ONLY public."MigrationTasks"
    ADD CONSTRAINT "MigrationTasks_FileId_fkey" FOREIGN KEY ("FileId") REFERENCES public."Files"("FileId") ON DELETE CASCADE;
ALTER TABLE ONLY public."ADGroupMembers"
    ADD CONSTRAINT "ADGroupMembers_ADGroupId_fkey" FOREIGN KEY ("ADGroupId") REFERENCES public."ADGroups"("ADGroupId") ON DELETE CASCADE;
ALTER TABLE ONLY public."PermissionSetEntries"
    ADD CONSTRAINT "PermissionSetEntries_PermissionSetId_fkey" FOREIGN KEY ("PermissionSetId") REFERENCES public."PermissionSets"("PermissionSetId") ON DELETE CASCADE;
ALTER TABLE ONLY public."Migration_Path_History"
    ADD CONSTRAINT "Migration_Path_History_OpId_fkey" FOREIGN KEY ("OpId") REFERENCES public."Migration_Operation_Logs"("OpId") ON DELETE CASCADE;

-- -----------------------------------------------------------------------------
-- SiteId (NEW): index + FK to "Sites" + trigger on every table with SiteName/SiteId
-- -----------------------------------------------------------------------------
-- The app does not know "SiteId" (EF6 / raw COPY only write SiteName), so a
-- BEFORE INSERT/UPDATE trigger derives it from SiteName, adding the site to
-- "Sites" on first use. NULL or '' SiteName -> NULL SiteId.
CREATE FUNCTION public.set_site_id() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NEW."SiteName" IS NULL OR NEW."SiteName" = '' THEN
        NEW."SiteId" := NULL;
        RETURN NEW;
    END IF;
    SELECT "SiteId" INTO NEW."SiteId" FROM public."Sites" WHERE "SiteName" = NEW."SiteName";
    IF NOT FOUND THEN
        INSERT INTO public."Sites" ("SiteName") VALUES (NEW."SiteName")
        ON CONFLICT ("SiteName") DO NOTHING;
        SELECT "SiteId" INTO NEW."SiteId" FROM public."Sites" WHERE "SiteName" = NEW."SiteName";
    END IF;
    RETURN NEW;
END;
$$;

DO $$
DECLARE t text;
BEGIN
    FOR t IN
        SELECT c.table_name FROM information_schema.columns c
        WHERE c.table_schema = 'public' AND c.column_name = 'SiteId' AND c.table_name <> 'Sites'
          AND c.table_name NOT LIKE 'src\_%\_\_%'  -- staging copies of the sources ("Through this PC")
        ORDER BY 1
    LOOP
        EXECUTE format('CREATE INDEX %I ON public.%I USING btree ("SiteId")', 'IX_' || t || '_SiteId', t);
        EXECUTE format('ALTER TABLE ONLY public.%I ADD CONSTRAINT %I FOREIGN KEY ("SiteId") REFERENCES public."Sites"("SiteId")',
                       t, t || '_SiteId_fkey');
        EXECUTE format('CREATE TRIGGER %I BEFORE INSERT OR UPDATE OF "SiteName" ON public.%I FOR EACH ROW EXECUTE FUNCTION public.set_site_id()',
                       'trg_' || t || '_site_id', t);
        RAISE NOTICE 'SiteId index/FK/trigger: %', t;
    END LOOP;
END $$;

-- -----------------------------------------------------------------------------
-- View (absent in cwp2, cwp, procon; the app re-creates it on every Analyze)
-- -----------------------------------------------------------------------------
CREATE VIEW public."v_MigrationSummary" AS
 SELECT "SiteName",
    count(*) AS "TotalFiles",
    count(*) FILTER (WHERE ("PathStatus" = 'Violations'::text)) AS "TotalViolations",
    count(*) FILTER (WHERE ("PathStatus" = 'Warning Zone'::text)) AS "TotalWarnings",
    max("TargetPath_Len") AS "MaxPathLength",
    (round((((count(*) FILTER (WHERE ("PathStatus" = 'Violations'::text)))::numeric / (NULLIF(count(*), 0))::numeric) * (100)::numeric), 2) || '%'::text) AS "ViolationRate"
   FROM public."MigrationTasks_Parsed"
  GROUP BY "SiteName";
