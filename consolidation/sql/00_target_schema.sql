-- =============================================================================
-- 00_target_schema.sql — consolidated MTR Phase 3 database (superset schema)
-- =============================================================================
-- Run against the NEW, EMPTY target database (e.g. "MtrMigration_Consolidated").
--
-- Baseline: cwp_opspj4 (richest of the 9 sources), cross-checked against
-- cwp_ecr (newest). Every column of every source table exists here, so no
-- source data is dropped. Rules applied (see ../MAPPING.md):
--   * Core app tables keep the EXACT layout of mtr-phase3 scripts/init.sql +
--     init_pathbreak.sql so the GUI / EF6 / PathBreakRepository work unchanged.
--     No columns are added to them; per-row provenance is recorded in the
--     "_merge" schema instead (ID ranges + conflict log).
--   * Tables NOT used by the app (source imports + feature tables) get a
--     "SourceDb"/source_db column for source separation.
--   * Type drift -> widest type (nas/sg_permission_matrix.given_through = 1000).
--   * bak_* tables and bak_MigrationTasks are excluded.
--
-- Primary keys and unique constraints are created here (they double as a
-- safety net during the load). Secondary indexes, foreign keys and the view
-- are created AFTER the bulk load in 04_finalize.sql.
-- =============================================================================

\set ON_ERROR_STOP on

-- -----------------------------------------------------------------------------
-- Sites lookup (NEW). Every table with "SiteName" also has "SiteId" -> Sites.
-- Filled from all distinct non-empty SiteName values during the merge; kept in
-- sync afterwards by the set_site_id() trigger (04_finalize.sql).
-- -----------------------------------------------------------------------------

CREATE TABLE public."Sites" (
    "SiteId" integer NOT NULL,
    "SiteName" character varying(256) NOT NULL,
    "CreatedAt" timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

-- -----------------------------------------------------------------------------
-- Core app tables (mtr-phase3 scripts/init.sql)
-- -----------------------------------------------------------------------------

CREATE TABLE public."Files" (
    "FileId" integer NOT NULL,
    "FileName" character varying(500) NOT NULL,
    "FilePath" character varying(2000) NOT NULL,
    "OriginalSharePointPath" character varying(2000) NOT NULL,
    "SharePointUniqueId" character varying(50),
    "FileSize" bigint NOT NULL,
    "CreatedDate" timestamp without time zone NOT NULL,
    "ModifiedDate" timestamp without time zone NOT NULL,
    "CreatedBy" character varying(256),
    "CreatedByLocalUser" character varying(256),
    "ModifiedBy" character varying(256),
    "ModifiedByLocalUser" character varying(256),
    "ContentType" character varying(256),
    "FileExtension" character varying(50),
    "MigrationDate" timestamp without time zone NOT NULL,
    "MigrationStatus" integer DEFAULT 0 NOT NULL,
    "MigrationError" character varying(2000),
    "SiteName" character varying(256),
    "SiteId" integer,
    "LastSyncedAt" timestamp without time zone,
    "DeltaSyncCount" integer DEFAULT 0 NOT NULL,
    "HasUniquePermissions" boolean DEFAULT false NOT NULL
);

CREATE TABLE public."Metadata" (
    "MetadataId" integer NOT NULL,
    "FileId" integer NOT NULL,
    "MetadataKey" character varying(256) NOT NULL,
    "MetadataValue" character varying(4000),
    "DataType" character varying(50),
    "SiteName" character varying(256),
    "SiteId" integer
);

CREATE TABLE public."Permissions" (
    "PermissionId" integer NOT NULL,
    "FileId" integer NOT NULL,
    "ADGroupName" character varying(256) NOT NULL,
    "ADGroupGuid" uuid,
    "PermissionLevel" character varying(50) NOT NULL,
    "OriginalSharePointGroup" character varying(256),
    "SharePointPrincipalType" character varying(100),
    "SharePointRoleDefinition" character varying(256),
    "NtfsPermissionApplied" boolean DEFAULT false NOT NULL,
    "ApplyMethod" integer DEFAULT 0 NOT NULL,
    "InheritedFromFolder" character varying(2000),
    "NtfsAppliedDate" timestamp without time zone,
    "SiteName" character varying(256),
    "SiteId" integer
);

CREATE TABLE public."ADGroups" (
    "ADGroupId" integer NOT NULL,
    "GroupName" character varying(256) NOT NULL,
    "GroupGuid" uuid,
    "Description" character varying(1024),
    "CreatedDate" timestamp without time zone NOT NULL,
    "SourceSharePointGroup" character varying(256),
    "SiteName" character varying(256),
    "SiteId" integer
);

CREATE TABLE public."ADGroupMembers" (
    "MemberId" integer NOT NULL,
    "ADGroupId" integer NOT NULL,
    "UserPrincipalName" character varying(256) NOT NULL,
    "DisplayName" character varying(256),
    "AddedDate" timestamp without time zone NOT NULL,
    "SiteName" character varying(256),
    "SiteId" integer
);

CREATE TABLE public."PermissionSets" (
    "PermissionSetId" integer NOT NULL,
    "SetId" character varying(50) NOT NULL,
    "PermissionKey" character varying(4000) NOT NULL,
    "Description" character varying(2000),
    "FolderCount" integer DEFAULT 0 NOT NULL,
    "FileCount" integer DEFAULT 0 NOT NULL,
    "TotalSize" bigint DEFAULT 0 NOT NULL,
    "SampleFolders" character varying(4000),
    "ScanDate" timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    "SiteUrl" character varying(2000) NOT NULL,
    "SiteName" character varying(256),
    "SiteId" integer
);

CREATE TABLE public."PermissionSetEntries" (
    "PermissionSetEntryId" integer NOT NULL,
    "PermissionSetId" integer NOT NULL,
    "GroupName" character varying(256) NOT NULL,
    "AccessLevel" character varying(256) NOT NULL
);

CREATE TABLE public."FolderPermissions" (
    "FolderPermissionId" integer NOT NULL,
    "FolderPath" character varying(2000) NOT NULL,
    "Depth" integer DEFAULT 0 NOT NULL,
    "PermissionSetId" character varying(50) NOT NULL,
    "HasUniquePermissions" boolean DEFAULT false NOT NULL,
    "DirectFileCount" integer DEFAULT 0 NOT NULL,
    "TotalFileCount" integer DEFAULT 0 NOT NULL,
    "ParentFolder" character varying(2000),
    "ScanDate" timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    "SiteUrl" character varying(2000) NOT NULL,
    "SiteName" character varying(256),
    "SiteId" integer
);

CREATE TABLE public."PermissionMatrixSummaries" (
    "PermissionMatrixSummaryId" integer NOT NULL,
    "SiteUrl" character varying(2000) NOT NULL,
    "ScanDate" timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    "UniquePermissionSets" integer DEFAULT 0 NOT NULL,
    "FilesWithUniquePermissions" integer DEFAULT 0 NOT NULL,
    "FilesWithInheritedPermissions" integer DEFAULT 0 NOT NULL,
    "FoldersWithUniquePermissions" integer DEFAULT 0 NOT NULL,
    "TotalFoldersAnalyzed" integer DEFAULT 0 NOT NULL,
    "InheritanceRate" double precision DEFAULT 0 NOT NULL,
    "SiteName" character varying(256),
    "SiteId" integer
);

CREATE TABLE public."MigrationTasks" (
    "TaskId" integer NOT NULL,
    "TaskType" integer DEFAULT 0 NOT NULL,
    "FileId" integer,
    "ServerRelativeUrl" character varying(2000),
    "TargetPath" character varying(2000),
    "PermissionsJson" text,
    "Priority" integer DEFAULT 100 NOT NULL,
    "Status" integer DEFAULT 0 NOT NULL,
    "WorkerId" character varying(256),
    "CreatedAt" timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    "ClaimedAt" timestamp without time zone,
    "CompletedAt" timestamp without time zone,
    "ErrorMessage" character varying(2000),
    "RetryCount" integer DEFAULT 0 NOT NULL,
    "MaxRetries" integer DEFAULT 3 NOT NULL,
    "SiteName" character varying(256),
    "SiteId" integer,
    "IsDeltaSync" boolean DEFAULT false NOT NULL
);

CREATE TABLE public."ScanProgress" (
    "ScanProgressId" integer NOT NULL,
    "LibraryName" character varying(500) NOT NULL,
    "SiteName" character varying(256) DEFAULT ''::character varying NOT NULL,
    "SiteId" integer,
    "Status" integer DEFAULT 0 NOT NULL,
    "LastPosition" text,
    "ItemsProcessed" integer DEFAULT 0 NOT NULL,
    "TotalItems" integer DEFAULT 0 NOT NULL,
    "LastUpdatedAt" timestamp without time zone DEFAULT now() NOT NULL,
    "LastScanCompletedAt" timestamp without time zone,
    "ErrorMessage" character varying(2000),
    "PermissionsResolved" boolean DEFAULT false NOT NULL,
    "LastResolvedFileId" integer
);

CREATE TABLE public."FolderInheritanceBreaks" (
    "FolderInheritanceBreakId" integer NOT NULL,
    "LibraryName" character varying(500) NOT NULL,
    "SiteName" character varying(256) DEFAULT ''::character varying NOT NULL,
    "SiteId" integer,
    "FolderPath" character varying(2000) NOT NULL,
    "PrincipalName" character varying(500) NOT NULL,
    "PrincipalType" character varying(50) DEFAULT ''::character varying NOT NULL,
    "RoleDefinition" character varying(100) NOT NULL,
    "LoginName" character varying(500) DEFAULT ''::character varying NOT NULL,
    "ScanDate" timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE TABLE public."ActiveLanIds" (
    "LanId" character varying(100) NOT NULL,
    "UploadedAt" timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

-- -----------------------------------------------------------------------------
-- Break-path audit tables (mtr-phase3 scripts/init_pathbreak.sql)
-- -----------------------------------------------------------------------------

CREATE TABLE public."Migration_Operation_Logs" (
    "OpId" integer NOT NULL,
    "StepName" character varying(100),
    "ExecutionTime" timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    "SiteFilter" text,
    "AffectedRows" integer,
    "Status" character varying(50)
);

CREATE TABLE public."Migration_Path_History" (
    "HistoryId" integer NOT NULL,
    "OpId" integer,
    "TaskId" integer,
    "OldPath" text,
    "NewPath" text
);

-- -----------------------------------------------------------------------------
-- Break-path working tables (regenerated by PathBreakRepository; migrated so
-- the current analysis / pending preview survive the move)
-- -----------------------------------------------------------------------------

CREATE TABLE public."MigrationTasks_Parsed" (
    "TaskId" integer NOT NULL,
    "TaskType" integer,
    "FileId" integer,
    "ServerRelativeUrl" character varying(2000),
    "TargetPath" character varying(2000),
    "PermissionsJson" text,
    "Priority" integer,
    "Status" integer,
    "WorkerId" character varying(256),
    "CreatedAt" timestamp without time zone,
    "ClaimedAt" timestamp without time zone,
    "CompletedAt" timestamp without time zone,
    "ErrorMessage" character varying(2000),
    "RetryCount" integer,
    "MaxRetries" integer,
    "SiteName" character varying(256),
    "SiteId" integer,
    "IsDeltaSync" boolean,
    extracted_fn text,
    final_lv integer,
    "PathStatus" text,
    "BreakfolderLv" integer,
    "TargetPath_Len" integer,
    "FileName_Len" integer,
    "FinalFolderLv" integer
);

CREATE TABLE public."NewChangePath_NEW" (
    "TaskId" integer NOT NULL,
    "OriginalPath" text,
    "SelectedTarget" text,
    "ExtendFolder" text,
    "RootPath" text,
    "RemainPath" text,
    "NewPath" text,
    "NewPath_Len" integer,
    "CreatedAt" timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE public."Folder_Structure_Final_Analysis" (
    "SiteName" character varying(256),
    "SiteId" integer,
    folder_path text,
    folder_level integer,
    max_acc_path_len integer,
    direct_files bigint,
    direct_subfolders bigint,
    direct_violations bigint,
    acc_files bigint,
    acc_subfolders bigint,
    acc_violations bigint
);

-- -----------------------------------------------------------------------------
-- Source-import tables (+ "SourceDb")
-- -----------------------------------------------------------------------------

CREATE TABLE public."PostgreSQL_PermissionMatrix_20260503" (
    "ID" integer NOT NULL,
    "TypeValue" character varying(100),
    "NameValue" character varying(255),
    "UrlValue" character varying(500),
    "Item_path" character varying(500),
    "Inheritance" character varying(50),
    "Details" character varying(500),
    "User_or_group" character varying(255),
    "Principal_type" character varying(50),
    "Account_name" character varying(255),
    "Given_through" character varying(1000),
    "Full_Control" character varying(1000),
    "Design" character varying(1000),
    "Contribute" character varying(1000),
    "EditValue" character varying(1000),
    "ReadValue" character varying(1000),
    "Manage_Hierarchy" character varying(1000),
    "Approve" character varying(1000),
    "Restricted_Read" character varying(1000),
    "Restricted_Interfaces_for_Translation" character varying(1000),
    "View_Only" character varying(1000),
    "Manage_Permission" character varying(1000),
    "Edit_and_Delete" character varying(1000),
    "Edit_Item" character varying(1000),
    "ImportDate" timestamp without time zone,
    "SourceDb" character varying(64) NOT NULL
);

CREATE TABLE public."PostgreSQL_SiteGroupPermission_20260503" (
    "ID" integer NOT NULL,
    "NameValue" character varying(505),
    "Site_address" character varying(1000),
    "Everyone_can_view_membership" character varying(50),
    "Allow_join_or_leave_requests" character varying(50),
    "Members_can_edit_membership" character varying(50),
    "Auto_accept_membership_requests" character varying(50),
    "Membership_request_email" character varying(500),
    "Notes" character varying(1000),
    "OwnerValue" character varying(255),
    "Members" text,
    "ImportDate" timestamp without time zone,
    "SourceDb" character varying(64) NOT NULL
);

-- -----------------------------------------------------------------------------
-- Feature tables (opspj2 + opspj4 only; Folders opspj4 only) (+ source_db)
-- -----------------------------------------------------------------------------

CREATE TABLE public."Folders" (
    "UniqueId" uuid NOT NULL,
    path text NOT NULL,
    source_db character varying(64) NOT NULL
);

CREATE TABLE public.break_path_mapping (
    task_id bigint NOT NULL,
    file_name character varying(1500) NOT NULL,
    ext_dir character varying(1500),
    ext_file_path character varying(1500),
    org_dir character varying(1500),
    org_file_path character varying(1500),
    sp2013_dir character varying(1500),
    sp2013_file_path character varying(1500),
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    source_db character varying(64) NOT NULL
);

CREATE TABLE public.nas_permission_matrix (
    id integer NOT NULL,
    row_num integer,
    item_type character varying(100),
    item_name character varying(255),
    url text,
    item_path text,
    inheritance character varying(50),
    details character varying(255),
    user_group character varying(255),
    principal_type character varying(100),
    account_name character varying(255),
    given_through character varying(1000),
    has_full_control boolean DEFAULT false,
    has_modify boolean DEFAULT false,
    has_read_execute boolean DEFAULT false,
    has_read boolean DEFAULT false,
    has_write boolean DEFAULT false,
    has_special_permissions boolean DEFAULT false,
    migration_date timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    source_db character varying(64) NOT NULL
);

CREATE TABLE public.sg_permission_matrix (
    id integer NOT NULL,
    row_num integer,
    item_type character varying(100),
    item_name character varying(255),
    url text,
    item_path text,
    inheritance character varying(50),
    details character varying(255),
    user_group character varying(255),
    principal_type character varying(100),
    account_name character varying(255),
    given_through character varying(1000),
    has_full_control boolean DEFAULT false,
    has_design boolean DEFAULT false,
    has_contribute boolean DEFAULT false,
    has_edit boolean DEFAULT false,
    has_read boolean DEFAULT false,
    has_manage_hierarchy boolean DEFAULT false,
    has_approve boolean DEFAULT false,
    has_restricted_read boolean DEFAULT false,
    has_restricted_interfaces_translation boolean DEFAULT false,
    has_view_only boolean DEFAULT false,
    has_manage_permission boolean DEFAULT false,
    has_edit_and_delete boolean DEFAULT false,
    has_edit_item boolean DEFAULT false,
    migration_date timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    source_db character varying(64) NOT NULL
);

CREATE TABLE public.sitemapping (
    sp2013_url character varying(1500) NOT NULL,
    sp2013_sitecollection character varying(1500),
    nas_url character varying(1500),
    site_prefix character varying(50),
    source_db character varying(64) NOT NULL
);

-- -----------------------------------------------------------------------------
-- Sequences / identity (values are set to MAX(id) in 04_finalize.sql)
-- -----------------------------------------------------------------------------

CREATE SEQUENCE public."Sites_SiteId_seq" AS integer OWNED BY public."Sites"."SiteId";
ALTER TABLE ONLY public."Sites" ALTER COLUMN "SiteId" SET DEFAULT nextval('public."Sites_SiteId_seq"'::regclass);
CREATE SEQUENCE public."Files_FileId_seq" AS integer OWNED BY public."Files"."FileId";
CREATE SEQUENCE public."Metadata_MetadataId_seq" AS integer OWNED BY public."Metadata"."MetadataId";
CREATE SEQUENCE public."Permissions_PermissionId_seq" AS integer OWNED BY public."Permissions"."PermissionId";
CREATE SEQUENCE public."ADGroups_ADGroupId_seq" AS integer OWNED BY public."ADGroups"."ADGroupId";
CREATE SEQUENCE public."ADGroupMembers_MemberId_seq" AS integer OWNED BY public."ADGroupMembers"."MemberId";
CREATE SEQUENCE public."PermissionSets_PermissionSetId_seq" AS integer OWNED BY public."PermissionSets"."PermissionSetId";
CREATE SEQUENCE public."PermissionSetEntries_PermissionSetEntryId_seq" AS integer OWNED BY public."PermissionSetEntries"."PermissionSetEntryId";
CREATE SEQUENCE public."FolderPermissions_FolderPermissionId_seq" AS integer OWNED BY public."FolderPermissions"."FolderPermissionId";
CREATE SEQUENCE public."PermissionMatrixSummaries_PermissionMatrixSummaryId_seq" AS integer OWNED BY public."PermissionMatrixSummaries"."PermissionMatrixSummaryId";
CREATE SEQUENCE public."MigrationTasks_TaskId_seq" AS integer OWNED BY public."MigrationTasks"."TaskId";
CREATE SEQUENCE public."ScanProgress_ScanProgressId_seq" AS integer OWNED BY public."ScanProgress"."ScanProgressId";
CREATE SEQUENCE public."FolderInheritanceBreaks_FolderInheritanceBreakId_seq" AS integer OWNED BY public."FolderInheritanceBreaks"."FolderInheritanceBreakId";
CREATE SEQUENCE public."Migration_Operation_Logs_OpId_seq" AS integer OWNED BY public."Migration_Operation_Logs"."OpId";
CREATE SEQUENCE public."Migration_Path_History_HistoryId_seq" AS integer OWNED BY public."Migration_Path_History"."HistoryId";
CREATE SEQUENCE public.nas_permission_matrix_id_seq AS integer OWNED BY public.nas_permission_matrix.id;
CREATE SEQUENCE public.sg_permission_matrix_id_seq AS integer OWNED BY public.sg_permission_matrix.id;

ALTER TABLE ONLY public."Files" ALTER COLUMN "FileId" SET DEFAULT nextval('public."Files_FileId_seq"'::regclass);
ALTER TABLE ONLY public."Metadata" ALTER COLUMN "MetadataId" SET DEFAULT nextval('public."Metadata_MetadataId_seq"'::regclass);
ALTER TABLE ONLY public."Permissions" ALTER COLUMN "PermissionId" SET DEFAULT nextval('public."Permissions_PermissionId_seq"'::regclass);
ALTER TABLE ONLY public."ADGroups" ALTER COLUMN "ADGroupId" SET DEFAULT nextval('public."ADGroups_ADGroupId_seq"'::regclass);
ALTER TABLE ONLY public."ADGroupMembers" ALTER COLUMN "MemberId" SET DEFAULT nextval('public."ADGroupMembers_MemberId_seq"'::regclass);
ALTER TABLE ONLY public."PermissionSets" ALTER COLUMN "PermissionSetId" SET DEFAULT nextval('public."PermissionSets_PermissionSetId_seq"'::regclass);
ALTER TABLE ONLY public."PermissionSetEntries" ALTER COLUMN "PermissionSetEntryId" SET DEFAULT nextval('public."PermissionSetEntries_PermissionSetEntryId_seq"'::regclass);
ALTER TABLE ONLY public."FolderPermissions" ALTER COLUMN "FolderPermissionId" SET DEFAULT nextval('public."FolderPermissions_FolderPermissionId_seq"'::regclass);
ALTER TABLE ONLY public."PermissionMatrixSummaries" ALTER COLUMN "PermissionMatrixSummaryId" SET DEFAULT nextval('public."PermissionMatrixSummaries_PermissionMatrixSummaryId_seq"'::regclass);
ALTER TABLE ONLY public."MigrationTasks" ALTER COLUMN "TaskId" SET DEFAULT nextval('public."MigrationTasks_TaskId_seq"'::regclass);
ALTER TABLE ONLY public."ScanProgress" ALTER COLUMN "ScanProgressId" SET DEFAULT nextval('public."ScanProgress_ScanProgressId_seq"'::regclass);
ALTER TABLE ONLY public."FolderInheritanceBreaks" ALTER COLUMN "FolderInheritanceBreakId" SET DEFAULT nextval('public."FolderInheritanceBreaks_FolderInheritanceBreakId_seq"'::regclass);
ALTER TABLE ONLY public."Migration_Operation_Logs" ALTER COLUMN "OpId" SET DEFAULT nextval('public."Migration_Operation_Logs_OpId_seq"'::regclass);
ALTER TABLE ONLY public."Migration_Path_History" ALTER COLUMN "HistoryId" SET DEFAULT nextval('public."Migration_Path_History_HistoryId_seq"'::regclass);
ALTER TABLE ONLY public.nas_permission_matrix ALTER COLUMN id SET DEFAULT nextval('public.nas_permission_matrix_id_seq'::regclass);
ALTER TABLE ONLY public.sg_permission_matrix ALTER COLUMN id SET DEFAULT nextval('public.sg_permission_matrix_id_seq'::regclass);

ALTER TABLE public."PostgreSQL_PermissionMatrix_20260503" ALTER COLUMN "ID" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public."PostgreSQL_PermissionMatrix_20260503_ID_seq"
);
ALTER TABLE public."PostgreSQL_SiteGroupPermission_20260503" ALTER COLUMN "ID" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public."PostgreSQL_SiteGroupPermission_20260503_ID_seq"
);

-- -----------------------------------------------------------------------------
-- Primary keys + unique constraints
-- -----------------------------------------------------------------------------

ALTER TABLE ONLY public."Sites" ADD CONSTRAINT "Sites_pkey" PRIMARY KEY ("SiteId");
ALTER TABLE ONLY public."Sites" ADD CONSTRAINT "Sites_SiteName_key" UNIQUE ("SiteName");
ALTER TABLE ONLY public."Files" ADD CONSTRAINT "Files_pkey" PRIMARY KEY ("FileId");
ALTER TABLE ONLY public."Files" ADD CONSTRAINT "Files_OriginalSharePointPath_key" UNIQUE ("OriginalSharePointPath");
ALTER TABLE ONLY public."Metadata" ADD CONSTRAINT "Metadata_pkey" PRIMARY KEY ("MetadataId");
ALTER TABLE ONLY public."Permissions" ADD CONSTRAINT "Permissions_pkey" PRIMARY KEY ("PermissionId");
ALTER TABLE ONLY public."ADGroups" ADD CONSTRAINT "ADGroups_pkey" PRIMARY KEY ("ADGroupId");
ALTER TABLE ONLY public."ADGroups" ADD CONSTRAINT "ADGroups_GroupName_key" UNIQUE ("GroupName");
ALTER TABLE ONLY public."ADGroups" ADD CONSTRAINT "ADGroups_GroupGuid_key" UNIQUE ("GroupGuid");
ALTER TABLE ONLY public."ADGroupMembers" ADD CONSTRAINT "ADGroupMembers_pkey" PRIMARY KEY ("MemberId");
ALTER TABLE ONLY public."ADGroupMembers" ADD CONSTRAINT "ADGroupMembers_ADGroupId_UserPrincipalName_key" UNIQUE ("ADGroupId", "UserPrincipalName");
ALTER TABLE ONLY public."PermissionSets" ADD CONSTRAINT "PermissionSets_pkey" PRIMARY KEY ("PermissionSetId");
ALTER TABLE ONLY public."PermissionSetEntries" ADD CONSTRAINT "PermissionSetEntries_pkey" PRIMARY KEY ("PermissionSetEntryId");
ALTER TABLE ONLY public."FolderPermissions" ADD CONSTRAINT "FolderPermissions_pkey" PRIMARY KEY ("FolderPermissionId");
ALTER TABLE ONLY public."PermissionMatrixSummaries" ADD CONSTRAINT "PermissionMatrixSummaries_pkey" PRIMARY KEY ("PermissionMatrixSummaryId");
ALTER TABLE ONLY public."MigrationTasks" ADD CONSTRAINT "MigrationTasks_pkey" PRIMARY KEY ("TaskId");
ALTER TABLE ONLY public."ScanProgress" ADD CONSTRAINT "ScanProgress_pkey" PRIMARY KEY ("ScanProgressId");
CREATE UNIQUE INDEX "IX_ScanProgress_Library_Site" ON public."ScanProgress" USING btree ("LibraryName", "SiteName");
ALTER TABLE ONLY public."FolderInheritanceBreaks" ADD CONSTRAINT "FolderInheritanceBreaks_pkey" PRIMARY KEY ("FolderInheritanceBreakId");
ALTER TABLE ONLY public."ActiveLanIds" ADD CONSTRAINT "ActiveLanIds_pkey" PRIMARY KEY ("LanId");
ALTER TABLE ONLY public."Migration_Operation_Logs" ADD CONSTRAINT "Migration_Operation_Logs_pkey" PRIMARY KEY ("OpId");
ALTER TABLE ONLY public."Migration_Path_History" ADD CONSTRAINT "Migration_Path_History_pkey" PRIMARY KEY ("HistoryId");
ALTER TABLE ONLY public."MigrationTasks_Parsed" ADD CONSTRAINT "MigrationTasks_Parsed_pkey" PRIMARY KEY ("TaskId");
ALTER TABLE ONLY public."NewChangePath_NEW" ADD CONSTRAINT "NewChangePath_NEW_pkey" PRIMARY KEY ("TaskId");
ALTER TABLE ONLY public."PostgreSQL_PermissionMatrix_20260503" ADD CONSTRAINT "PostgreSQL_PermissionMatrix_20260503_pkey" PRIMARY KEY ("ID");
ALTER TABLE ONLY public."PostgreSQL_SiteGroupPermission_20260503" ADD CONSTRAINT "PostgreSQL_SiteGroupPermission_20260503_pkey" PRIMARY KEY ("ID");
ALTER TABLE ONLY public."Folders" ADD CONSTRAINT "Folders_pkey" PRIMARY KEY ("UniqueId");
ALTER TABLE ONLY public.break_path_mapping ADD CONSTRAINT pk_file_migration_mapping PRIMARY KEY (task_id, file_name);
ALTER TABLE ONLY public.nas_permission_matrix ADD CONSTRAINT nas_permission_matrix_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.sg_permission_matrix ADD CONSTRAINT sg_permission_matrix_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.sitemapping ADD CONSTRAINT sitemapping_pk PRIMARY KEY (sp2013_url);

-- -----------------------------------------------------------------------------
-- Functions (identical in all 9 sources)
-- -----------------------------------------------------------------------------

CREATE FUNCTION public.claim_migration_tasks(p_worker_id character varying, p_batch_size integer) RETURNS SETOF public."MigrationTasks"
    LANGUAGE plpgsql
    AS $$
DECLARE
    claimed_tasks "MigrationTasks"%ROWTYPE;
BEGIN
    -- Use FOR UPDATE SKIP LOCKED to prevent race conditions
    FOR claimed_tasks IN
        UPDATE "MigrationTasks"
        SET "Status" = 1,
            "WorkerId" = p_worker_id,
            "ClaimedAt" = CURRENT_TIMESTAMP
        WHERE "TaskId" IN (
            SELECT "TaskId"
            FROM "MigrationTasks"
            WHERE "Status" = 0
            ORDER BY "Priority", "TaskId"
            FOR UPDATE SKIP LOCKED
            LIMIT p_batch_size
        )
        RETURNING *
    LOOP
        RETURN NEXT claimed_tasks;
    END LOOP;
    RETURN;
END;
$$;

CREATE FUNCTION public.reset_stale_tasks(p_timeout_minutes integer) RETURNS integer
    LANGUAGE plpgsql
    AS $$
DECLARE
    reset_count INTEGER;
BEGIN
    UPDATE "MigrationTasks"
    SET "Status" = 0,
        "WorkerId" = NULL,
        "ClaimedAt" = NULL,
        "RetryCount" = "RetryCount" + 1
    WHERE "Status" = 1
      AND "ClaimedAt" < CURRENT_TIMESTAMP - (p_timeout_minutes || ' minutes')::INTERVAL
      AND "RetryCount" < "MaxRetries";

    GET DIAGNOSTICS reset_count = ROW_COUNT;

    -- Mark tasks that exceeded retry limit as failed
    UPDATE "MigrationTasks"
    SET "Status" = 3,
        "ErrorMessage" = 'Exceeded maximum retry attempts'
    WHERE "Status" = 1
      AND "ClaimedAt" < CURRENT_TIMESTAMP - (p_timeout_minutes || ' minutes')::INTERVAL
      AND "RetryCount" >= "MaxRetries";

    RETURN reset_count;
END;
$$;

-- -----------------------------------------------------------------------------
-- Comments
-- -----------------------------------------------------------------------------

COMMENT ON TABLE public."Files" IS 'Migrated files with metadata and status';
COMMENT ON TABLE public."Metadata" IS 'SharePoint metadata fields for each file';
COMMENT ON TABLE public."Permissions" IS 'Per-file permission records for NTFS ACL application';
COMMENT ON TABLE public."ADGroups" IS 'Active Directory groups created for migration';
COMMENT ON TABLE public."ADGroupMembers" IS 'Members of each AD group';
COMMENT ON TABLE public."PermissionSets" IS 'Unique permission combinations found during scan';
COMMENT ON TABLE public."PermissionSetEntries" IS 'Group/access level pairs within each permission set';
COMMENT ON TABLE public."FolderPermissions" IS 'Folder permission inheritance tree';
COMMENT ON TABLE public."PermissionMatrixSummaries" IS 'Permission matrix summary statistics per scan';
COMMENT ON TABLE public."MigrationTasks" IS 'Work queue for parallel workers (pub-sub pattern)';
COMMENT ON TABLE public."ScanProgress" IS 'Tracks per-library scan progress for resumable scanning';
COMMENT ON TABLE public."FolderInheritanceBreaks" IS 'Per-folder explicit permissions for folders that broke inheritance — used by post-scan resolution to compute effective inherited permissions for child files';
COMMENT ON TABLE public."ActiveLanIds" IS 'Active employee LAN IDs used to filter permission application';
COMMENT ON TABLE public."Sites" IS 'One row per distinct SiteName; referenced by the SiteId column of every table that has SiteName';

-- -----------------------------------------------------------------------------
-- Merge bookkeeping schema (provenance + conflict audit). Safe to keep; the
-- app never reads it. Drop with:  DROP SCHEMA _merge CASCADE;
-- -----------------------------------------------------------------------------

CREATE SCHEMA _merge;

CREATE TABLE _merge.source (
    source_db     varchar(64) PRIMARY KEY,
    load_order    integer NOT NULL UNIQUE,      -- 1 = highest priority (wins conflicts)
    foreign_schema text NOT NULL
);

-- new_id = old_id + id_offset  (per table, per source). Rows from one source
-- therefore occupy the contiguous range [id_offset+1, id_offset+src_max_id].
CREATE TABLE _merge.id_offset (
    table_name  text NOT NULL,
    source_db   varchar(64) NOT NULL REFERENCES _merge.source,
    id_offset   bigint NOT NULL,
    src_max_id  bigint NOT NULL,
    PRIMARY KEY (table_name, source_db)
);

-- ADGroups are de-duplicated by GroupName: every source group maps to the
-- surviving target ADGroupId.
CREATE TABLE _merge.adgroup_map (
    source_db  varchar(64) NOT NULL,
    old_id     integer NOT NULL,
    new_id     integer NOT NULL,
    PRIMARY KEY (source_db, old_id)
);

-- Every row that was NOT copied 1:1 is recorded here.
--   action = 'skipped'  -> row (and its children) not loaded; a higher-priority
--                          source already owns the natural key
--   action = 'merged'   -> row folded into an existing target row
--   action = 'modified' -> row loaded with a changed value (see reason)
CREATE TABLE _merge.conflict_log (
    conflict_id      bigserial PRIMARY KEY,
    table_name       text NOT NULL,
    source_db        varchar(64) NOT NULL,
    old_id           bigint,
    natural_key      text,
    winner_source_db varchar(64),
    winner_old_id    bigint,
    action           text NOT NULL,
    reason           text NOT NULL
);
CREATE INDEX ix_conflict_log_lookup ON _merge.conflict_log (table_name, source_db, action, old_id);
CREATE INDEX ix_conflict_log_key    ON _merge.conflict_log (table_name, source_db, action, natural_key);

-- Provenance helpers:  _merge.source_of('Files', 123456)  -> 'cwp_opspj'
--                      _merge.old_id_of('Files', 123456)  -> FileId in that source
CREATE FUNCTION _merge.source_of(p_table text, p_new_id bigint) RETURNS varchar
    LANGUAGE sql STABLE AS $$
    SELECT source_db FROM _merge.id_offset
    WHERE table_name = p_table AND p_new_id > id_offset AND p_new_id <= id_offset + src_max_id
$$;

CREATE FUNCTION _merge.old_id_of(p_table text, p_new_id bigint) RETURNS bigint
    LANGUAGE sql STABLE AS $$
    SELECT p_new_id - id_offset FROM _merge.id_offset
    WHERE table_name = p_table AND p_new_id > id_offset AND p_new_id <= id_offset + src_max_id
$$;
