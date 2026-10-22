--
-- PostgreSQL database dump
--

\restrict GznnfqE72JmfCEuQmZ6KciONvv9MRlAdi1O63ie8Mgvk6LYNItlSRKrIZSTWBQR

-- Dumped from database version 18.4
-- Dumped by pg_dump version 18.2

-- Started on 2026-09-02 22:46:03

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- TOC entry 242 (class 1259 OID 79076)
-- Name: MigrationTasks; Type: TABLE; Schema: public; Owner: -
--

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
    "IsDeltaSync" boolean DEFAULT false NOT NULL
);


--
-- TOC entry 4301 (class 0 OID 0)
-- Dependencies: 242
-- Name: TABLE "MigrationTasks"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public."MigrationTasks" IS 'Work queue for parallel workers (pub-sub pattern)';


--
-- TOC entry 263 (class 1255 OID 79110)
-- Name: claim_migration_tasks(character varying, integer); Type: FUNCTION; Schema: public; Owner: -
--

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


--
-- TOC entry 264 (class 1255 OID 79111)
-- Name: reset_stale_tasks(integer); Type: FUNCTION; Schema: public; Owner: -
--

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


--
-- TOC entry 232 (class 1259 OID 78957)
-- Name: ADGroupMembers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."ADGroupMembers" (
    "MemberId" integer NOT NULL,
    "ADGroupId" integer NOT NULL,
    "UserPrincipalName" character varying(256) NOT NULL,
    "DisplayName" character varying(256),
    "AddedDate" timestamp without time zone NOT NULL,
    "SiteName" character varying(256)
);


--
-- TOC entry 4302 (class 0 OID 0)
-- Dependencies: 232
-- Name: TABLE "ADGroupMembers"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public."ADGroupMembers" IS 'Members of each AD group';


--
-- TOC entry 231 (class 1259 OID 78956)
-- Name: ADGroupMembers_MemberId_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public."ADGroupMembers_MemberId_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 4303 (class 0 OID 0)
-- Dependencies: 231
-- Name: ADGroupMembers_MemberId_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public."ADGroupMembers_MemberId_seq" OWNED BY public."ADGroupMembers"."MemberId";


--
-- TOC entry 230 (class 1259 OID 78940)
-- Name: ADGroups; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."ADGroups" (
    "ADGroupId" integer NOT NULL,
    "GroupName" character varying(256) NOT NULL,
    "GroupGuid" uuid,
    "Description" character varying(1024),
    "CreatedDate" timestamp without time zone NOT NULL,
    "SourceSharePointGroup" character varying(256),
    "SiteName" character varying(256)
);


--
-- TOC entry 4304 (class 0 OID 0)
-- Dependencies: 230
-- Name: TABLE "ADGroups"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public."ADGroups" IS 'Active Directory groups created for migration';


--
-- TOC entry 229 (class 1259 OID 78939)
-- Name: ADGroups_ADGroupId_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public."ADGroups_ADGroupId_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 4305 (class 0 OID 0)
-- Dependencies: 229
-- Name: ADGroups_ADGroupId_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public."ADGroups_ADGroupId_seq" OWNED BY public."ADGroups"."ADGroupId";


--
-- TOC entry 247 (class 1259 OID 79160)
-- Name: ActiveLanIds; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."ActiveLanIds" (
    "LanId" character varying(100) NOT NULL,
    "UploadedAt" timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- TOC entry 4306 (class 0 OID 0)
-- Dependencies: 247
-- Name: TABLE "ActiveLanIds"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public."ActiveLanIds" IS 'Active employee LAN IDs used to filter permission application';


--
-- TOC entry 224 (class 1259 OID 78868)
-- Name: Files; Type: TABLE; Schema: public; Owner: -
--

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
    "LastSyncedAt" timestamp without time zone,
    "DeltaSyncCount" integer DEFAULT 0 NOT NULL,
    "HasUniquePermissions" boolean DEFAULT false NOT NULL
);


--
-- TOC entry 4307 (class 0 OID 0)
-- Dependencies: 224
-- Name: TABLE "Files"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public."Files" IS 'Migrated files with metadata and status';


--
-- TOC entry 223 (class 1259 OID 78867)
-- Name: Files_FileId_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public."Files_FileId_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 4308 (class 0 OID 0)
-- Dependencies: 223
-- Name: Files_FileId_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public."Files_FileId_seq" OWNED BY public."Files"."FileId";


--
-- TOC entry 246 (class 1259 OID 79137)
-- Name: FolderInheritanceBreaks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."FolderInheritanceBreaks" (
    "FolderInheritanceBreakId" integer NOT NULL,
    "LibraryName" character varying(500) NOT NULL,
    "SiteName" character varying(256) DEFAULT ''::character varying NOT NULL,
    "FolderPath" character varying(2000) NOT NULL,
    "PrincipalName" character varying(500) NOT NULL,
    "PrincipalType" character varying(50) DEFAULT ''::character varying NOT NULL,
    "RoleDefinition" character varying(100) NOT NULL,
    "LoginName" character varying(500) DEFAULT ''::character varying NOT NULL,
    "ScanDate" timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- TOC entry 4309 (class 0 OID 0)
-- Dependencies: 246
-- Name: TABLE "FolderInheritanceBreaks"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public."FolderInheritanceBreaks" IS 'Per-folder explicit permissions for folders that broke inheritance — used by post-scan resolution to compute effective inherited permissions for child files';


--
-- TOC entry 245 (class 1259 OID 79136)
-- Name: FolderInheritanceBreaks_FolderInheritanceBreakId_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public."FolderInheritanceBreaks_FolderInheritanceBreakId_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 4310 (class 0 OID 0)
-- Dependencies: 245
-- Name: FolderInheritanceBreaks_FolderInheritanceBreakId_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public."FolderInheritanceBreaks_FolderInheritanceBreakId_seq" OWNED BY public."FolderInheritanceBreaks"."FolderInheritanceBreakId";


--
-- TOC entry 238 (class 1259 OID 79021)
-- Name: FolderPermissions; Type: TABLE; Schema: public; Owner: -
--

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
    "SiteName" character varying(256)
);


--
-- TOC entry 4311 (class 0 OID 0)
-- Dependencies: 238
-- Name: TABLE "FolderPermissions"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public."FolderPermissions" IS 'Folder permission inheritance tree';


--
-- TOC entry 237 (class 1259 OID 79020)
-- Name: FolderPermissions_FolderPermissionId_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public."FolderPermissions_FolderPermissionId_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 4312 (class 0 OID 0)
-- Dependencies: 237
-- Name: FolderPermissions_FolderPermissionId_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public."FolderPermissions_FolderPermissionId_seq" OWNED BY public."FolderPermissions"."FolderPermissionId";


--
-- TOC entry 255 (class 1259 OID 1336764)
-- Name: Folder_Structure_Final_Analysis; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."Folder_Structure_Final_Analysis" (
    "SiteName" character varying(256),
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


--
-- TOC entry 226 (class 1259 OID 78898)
-- Name: Metadata; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."Metadata" (
    "MetadataId" integer NOT NULL,
    "FileId" integer NOT NULL,
    "MetadataKey" character varying(256) NOT NULL,
    "MetadataValue" character varying(4000),
    "DataType" character varying(50),
    "SiteName" character varying(256)
);


--
-- TOC entry 4313 (class 0 OID 0)
-- Dependencies: 226
-- Name: TABLE "Metadata"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public."Metadata" IS 'SharePoint metadata fields for each file';


--
-- TOC entry 225 (class 1259 OID 78897)
-- Name: Metadata_MetadataId_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public."Metadata_MetadataId_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 4314 (class 0 OID 0)
-- Dependencies: 225
-- Name: Metadata_MetadataId_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public."Metadata_MetadataId_seq" OWNED BY public."Metadata"."MetadataId";


--
-- TOC entry 253 (class 1259 OID 1336751)
-- Name: MigrationTasks_Parsed; Type: TABLE; Schema: public; Owner: -
--

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
    "IsDeltaSync" boolean,
    extracted_fn text,
    final_lv integer,
    "PathStatus" text,
    "BreakfolderLv" integer,
    "TargetPath_Len" integer,
    "FileName_Len" integer,
    "FinalFolderLv" integer
);


--
-- TOC entry 241 (class 1259 OID 79075)
-- Name: MigrationTasks_TaskId_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public."MigrationTasks_TaskId_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 4315 (class 0 OID 0)
-- Dependencies: 241
-- Name: MigrationTasks_TaskId_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public."MigrationTasks_TaskId_seq" OWNED BY public."MigrationTasks"."TaskId";


--
-- TOC entry 249 (class 1259 OID 79169)
-- Name: Migration_Operation_Logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."Migration_Operation_Logs" (
    "OpId" integer NOT NULL,
    "StepName" character varying(100),
    "ExecutionTime" timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    "SiteFilter" text,
    "AffectedRows" integer,
    "Status" character varying(50)
);


--
-- TOC entry 248 (class 1259 OID 79168)
-- Name: Migration_Operation_Logs_OpId_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public."Migration_Operation_Logs_OpId_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 4316 (class 0 OID 0)
-- Dependencies: 248
-- Name: Migration_Operation_Logs_OpId_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public."Migration_Operation_Logs_OpId_seq" OWNED BY public."Migration_Operation_Logs"."OpId";


--
-- TOC entry 251 (class 1259 OID 79180)
-- Name: Migration_Path_History; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."Migration_Path_History" (
    "HistoryId" integer NOT NULL,
    "OpId" integer,
    "TaskId" integer,
    "OldPath" text,
    "NewPath" text
);


--
-- TOC entry 250 (class 1259 OID 79179)
-- Name: Migration_Path_History_HistoryId_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public."Migration_Path_History_HistoryId_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 4317 (class 0 OID 0)
-- Dependencies: 250
-- Name: Migration_Path_History_HistoryId_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public."Migration_Path_History_HistoryId_seq" OWNED BY public."Migration_Path_History"."HistoryId";


--
-- TOC entry 256 (class 1259 OID 1336773)
-- Name: NewChangePath_NEW; Type: TABLE; Schema: public; Owner: -
--

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


--
-- TOC entry 240 (class 1259 OID 79048)
-- Name: PermissionMatrixSummaries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."PermissionMatrixSummaries" (
    "PermissionMatrixSummaryId" integer NOT NULL,
    "SiteUrl" character varying(2000) NOT NULL,
    "ScanDate" timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    "UniquePermissionSets" integer DEFAULT 0 NOT NULL,
    "FilesWithUniquePermissions" integer DEFAULT 0 NOT NULL,
    "FilesWithInheritedPermissions" integer DEFAULT 0 CONSTRAINT "PermissionMatrixSummaries_FilesWithInheritedPermission_not_null" NOT NULL,
    "FoldersWithUniquePermissions" integer DEFAULT 0 NOT NULL,
    "TotalFoldersAnalyzed" integer DEFAULT 0 NOT NULL,
    "InheritanceRate" double precision DEFAULT 0 NOT NULL,
    "SiteName" character varying(256)
);


--
-- TOC entry 4318 (class 0 OID 0)
-- Dependencies: 240
-- Name: TABLE "PermissionMatrixSummaries"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public."PermissionMatrixSummaries" IS 'Permission matrix summary statistics per scan';


--
-- TOC entry 239 (class 1259 OID 79047)
-- Name: PermissionMatrixSummaries_PermissionMatrixSummaryId_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public."PermissionMatrixSummaries_PermissionMatrixSummaryId_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 4319 (class 0 OID 0)
-- Dependencies: 239
-- Name: PermissionMatrixSummaries_PermissionMatrixSummaryId_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public."PermissionMatrixSummaries_PermissionMatrixSummaryId_seq" OWNED BY public."PermissionMatrixSummaries"."PermissionMatrixSummaryId";


--
-- TOC entry 236 (class 1259 OID 79002)
-- Name: PermissionSetEntries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."PermissionSetEntries" (
    "PermissionSetEntryId" integer NOT NULL,
    "PermissionSetId" integer NOT NULL,
    "GroupName" character varying(256) NOT NULL,
    "AccessLevel" character varying(256) NOT NULL
);


--
-- TOC entry 4320 (class 0 OID 0)
-- Dependencies: 236
-- Name: TABLE "PermissionSetEntries"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public."PermissionSetEntries" IS 'Group/access level pairs within each permission set';


--
-- TOC entry 235 (class 1259 OID 79001)
-- Name: PermissionSetEntries_PermissionSetEntryId_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public."PermissionSetEntries_PermissionSetEntryId_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 4321 (class 0 OID 0)
-- Dependencies: 235
-- Name: PermissionSetEntries_PermissionSetEntryId_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public."PermissionSetEntries_PermissionSetEntryId_seq" OWNED BY public."PermissionSetEntries"."PermissionSetEntryId";


--
-- TOC entry 234 (class 1259 OID 78977)
-- Name: PermissionSets; Type: TABLE; Schema: public; Owner: -
--

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
    "SiteName" character varying(256)
);


--
-- TOC entry 4322 (class 0 OID 0)
-- Dependencies: 234
-- Name: TABLE "PermissionSets"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public."PermissionSets" IS 'Unique permission combinations found during scan';


--
-- TOC entry 233 (class 1259 OID 78976)
-- Name: PermissionSets_PermissionSetId_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public."PermissionSets_PermissionSetId_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 4323 (class 0 OID 0)
-- Dependencies: 233
-- Name: PermissionSets_PermissionSetId_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public."PermissionSets_PermissionSetId_seq" OWNED BY public."PermissionSets"."PermissionSetId";


--
-- TOC entry 228 (class 1259 OID 78916)
-- Name: Permissions; Type: TABLE; Schema: public; Owner: -
--

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
    "SiteName" character varying(256)
);


--
-- TOC entry 4324 (class 0 OID 0)
-- Dependencies: 228
-- Name: TABLE "Permissions"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public."Permissions" IS 'Per-file permission records for NTFS ACL application';


--
-- TOC entry 227 (class 1259 OID 78915)
-- Name: Permissions_PermissionId_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public."Permissions_PermissionId_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 4325 (class 0 OID 0)
-- Dependencies: 227
-- Name: Permissions_PermissionId_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public."Permissions_PermissionId_seq" OWNED BY public."Permissions"."PermissionId";


--
-- TOC entry 220 (class 1259 OID 78850)
-- Name: PostgreSQL_PermissionMatrix_20260503; Type: TABLE; Schema: public; Owner: -
--

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
    "ImportDate" timestamp without time zone
);


--
-- TOC entry 219 (class 1259 OID 78849)
-- Name: PostgreSQL_PermissionMatrix_20260503_ID_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public."PostgreSQL_PermissionMatrix_20260503" ALTER COLUMN "ID" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public."PostgreSQL_PermissionMatrix_20260503_ID_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- TOC entry 222 (class 1259 OID 78859)
-- Name: PostgreSQL_SiteGroupPermission_20260503; Type: TABLE; Schema: public; Owner: -
--

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
    "ImportDate" timestamp without time zone
);


--
-- TOC entry 221 (class 1259 OID 78858)
-- Name: PostgreSQL_SiteGroupPermission_20260503_ID_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public."PostgreSQL_SiteGroupPermission_20260503" ALTER COLUMN "ID" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public."PostgreSQL_SiteGroupPermission_20260503_ID_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- TOC entry 244 (class 1259 OID 79113)
-- Name: ScanProgress; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."ScanProgress" (
    "ScanProgressId" integer NOT NULL,
    "LibraryName" character varying(500) NOT NULL,
    "SiteName" character varying(256) DEFAULT ''::character varying NOT NULL,
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


--
-- TOC entry 4326 (class 0 OID 0)
-- Dependencies: 244
-- Name: TABLE "ScanProgress"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public."ScanProgress" IS 'Tracks per-library scan progress for resumable scanning';


--
-- TOC entry 243 (class 1259 OID 79112)
-- Name: ScanProgress_ScanProgressId_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public."ScanProgress_ScanProgressId_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 4327 (class 0 OID 0)
-- Dependencies: 243
-- Name: ScanProgress_ScanProgressId_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public."ScanProgress_ScanProgressId_seq" OWNED BY public."ScanProgress"."ScanProgressId";


--
-- TOC entry 252 (class 1259 OID 1336729)
-- Name: bak_Migration_Path_History_20260612; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."bak_Migration_Path_History_20260612" (
    "HistoryId" integer,
    "OpId" integer,
    "TaskId" integer,
    "OldPath" text,
    "NewPath" text
);


--
-- TOC entry 258 (class 1259 OID 1338629)
-- Name: break_path_mapping; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.break_path_mapping (
    task_id bigint NOT NULL,
    file_name character varying(1500) NOT NULL,
    ext_dir character varying(1500),
    ext_file_path character varying(1500),
    org_dir character varying(1500),
    org_file_path character varying(1500),
    sp2013_dir character varying(1500),
    sp2013_file_path character varying(1500),
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);


--
-- TOC entry 262 (class 1259 OID 1338746)
-- Name: nas_permission_matrix; Type: TABLE; Schema: public; Owner: -
--

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
    migration_date timestamp with time zone DEFAULT CURRENT_TIMESTAMP
);


--
-- TOC entry 261 (class 1259 OID 1338745)
-- Name: nas_permission_matrix_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.nas_permission_matrix_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 4328 (class 0 OID 0)
-- Dependencies: 261
-- Name: nas_permission_matrix_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.nas_permission_matrix_id_seq OWNED BY public.nas_permission_matrix.id;


--
-- TOC entry 260 (class 1259 OID 1338720)
-- Name: sg_permission_matrix; Type: TABLE; Schema: public; Owner: -
--

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
    migration_date timestamp with time zone DEFAULT CURRENT_TIMESTAMP
);


--
-- TOC entry 259 (class 1259 OID 1338719)
-- Name: sg_permission_matrix_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.sg_permission_matrix_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 4329 (class 0 OID 0)
-- Dependencies: 259
-- Name: sg_permission_matrix_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.sg_permission_matrix_id_seq OWNED BY public.sg_permission_matrix.id;


--
-- TOC entry 257 (class 1259 OID 1338576)
-- Name: sitemapping; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sitemapping (
    sp2013_url character varying(1500) NOT NULL,
    sp2013_sitecollection character varying(1500),
    nas_url character varying(1500),
    site_prefix character varying(50)
);


--
-- TOC entry 254 (class 1259 OID 1336759)
-- Name: v_MigrationSummary; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public."v_MigrationSummary" AS
 SELECT "SiteName",
    count(*) AS "TotalFiles",
    count(*) FILTER (WHERE ("PathStatus" = 'Violations'::text)) AS "TotalViolations",
    count(*) FILTER (WHERE ("PathStatus" = 'Warning Zone'::text)) AS "TotalWarnings",
    max("TargetPath_Len") AS "MaxPathLength",
    (round((((count(*) FILTER (WHERE ("PathStatus" = 'Violations'::text)))::numeric / (NULLIF(count(*), 0))::numeric) * (100)::numeric), 2) || '%'::text) AS "ViolationRate"
   FROM public."MigrationTasks_Parsed"
  GROUP BY "SiteName";


--
-- TOC entry 3941 (class 2604 OID 78960)
-- Name: ADGroupMembers MemberId; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."ADGroupMembers" ALTER COLUMN "MemberId" SET DEFAULT nextval('public."ADGroupMembers_MemberId_seq"'::regclass);


--
-- TOC entry 3940 (class 2604 OID 78943)
-- Name: ADGroups ADGroupId; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."ADGroups" ALTER COLUMN "ADGroupId" SET DEFAULT nextval('public."ADGroups_ADGroupId_seq"'::regclass);


--
-- TOC entry 3932 (class 2604 OID 78871)
-- Name: Files FileId; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."Files" ALTER COLUMN "FileId" SET DEFAULT nextval('public."Files_FileId_seq"'::regclass);


--
-- TOC entry 3977 (class 2604 OID 79140)
-- Name: FolderInheritanceBreaks FolderInheritanceBreakId; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."FolderInheritanceBreaks" ALTER COLUMN "FolderInheritanceBreakId" SET DEFAULT nextval('public."FolderInheritanceBreaks_FolderInheritanceBreakId_seq"'::regclass);


--
-- TOC entry 3948 (class 2604 OID 79024)
-- Name: FolderPermissions FolderPermissionId; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."FolderPermissions" ALTER COLUMN "FolderPermissionId" SET DEFAULT nextval('public."FolderPermissions_FolderPermissionId_seq"'::regclass);


--
-- TOC entry 3936 (class 2604 OID 78901)
-- Name: Metadata MetadataId; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."Metadata" ALTER COLUMN "MetadataId" SET DEFAULT nextval('public."Metadata_MetadataId_seq"'::regclass);


--
-- TOC entry 3962 (class 2604 OID 79079)
-- Name: MigrationTasks TaskId; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."MigrationTasks" ALTER COLUMN "TaskId" SET DEFAULT nextval('public."MigrationTasks_TaskId_seq"'::regclass);


--
-- TOC entry 3983 (class 2604 OID 79172)
-- Name: Migration_Operation_Logs OpId; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."Migration_Operation_Logs" ALTER COLUMN "OpId" SET DEFAULT nextval('public."Migration_Operation_Logs_OpId_seq"'::regclass);


--
-- TOC entry 3985 (class 2604 OID 79183)
-- Name: Migration_Path_History HistoryId; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."Migration_Path_History" ALTER COLUMN "HistoryId" SET DEFAULT nextval('public."Migration_Path_History_HistoryId_seq"'::regclass);


--
-- TOC entry 3954 (class 2604 OID 79051)
-- Name: PermissionMatrixSummaries PermissionMatrixSummaryId; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."PermissionMatrixSummaries" ALTER COLUMN "PermissionMatrixSummaryId" SET DEFAULT nextval('public."PermissionMatrixSummaries_PermissionMatrixSummaryId_seq"'::regclass);


--
-- TOC entry 3947 (class 2604 OID 79005)
-- Name: PermissionSetEntries PermissionSetEntryId; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."PermissionSetEntries" ALTER COLUMN "PermissionSetEntryId" SET DEFAULT nextval('public."PermissionSetEntries_PermissionSetEntryId_seq"'::regclass);


--
-- TOC entry 3942 (class 2604 OID 78980)
-- Name: PermissionSets PermissionSetId; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."PermissionSets" ALTER COLUMN "PermissionSetId" SET DEFAULT nextval('public."PermissionSets_PermissionSetId_seq"'::regclass);


--
-- TOC entry 3937 (class 2604 OID 78919)
-- Name: Permissions PermissionId; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."Permissions" ALTER COLUMN "PermissionId" SET DEFAULT nextval('public."Permissions_PermissionId_seq"'::regclass);


--
-- TOC entry 3970 (class 2604 OID 79116)
-- Name: ScanProgress ScanProgressId; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."ScanProgress" ALTER COLUMN "ScanProgressId" SET DEFAULT nextval('public."ScanProgress_ScanProgressId_seq"'::regclass);


--
-- TOC entry 4003 (class 2604 OID 1338749)
-- Name: nas_permission_matrix id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nas_permission_matrix ALTER COLUMN id SET DEFAULT nextval('public.nas_permission_matrix_id_seq'::regclass);


--
-- TOC entry 3988 (class 2604 OID 1338723)
-- Name: sg_permission_matrix id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sg_permission_matrix ALTER COLUMN id SET DEFAULT nextval('public.sg_permission_matrix_id_seq'::regclass);


--
-- TOC entry 4039 (class 2606 OID 78970)
-- Name: ADGroupMembers ADGroupMembers_ADGroupId_UserPrincipalName_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."ADGroupMembers"
    ADD CONSTRAINT "ADGroupMembers_ADGroupId_UserPrincipalName_key" UNIQUE ("ADGroupId", "UserPrincipalName");


--
-- TOC entry 4041 (class 2606 OID 78968)
-- Name: ADGroupMembers ADGroupMembers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."ADGroupMembers"
    ADD CONSTRAINT "ADGroupMembers_pkey" PRIMARY KEY ("MemberId");


--
-- TOC entry 4032 (class 2606 OID 78954)
-- Name: ADGroups ADGroups_GroupGuid_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."ADGroups"
    ADD CONSTRAINT "ADGroups_GroupGuid_key" UNIQUE ("GroupGuid");


--
-- TOC entry 4034 (class 2606 OID 78952)
-- Name: ADGroups ADGroups_GroupName_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."ADGroups"
    ADD CONSTRAINT "ADGroups_GroupName_key" UNIQUE ("GroupName");


--
-- TOC entry 4036 (class 2606 OID 78950)
-- Name: ADGroups ADGroups_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."ADGroups"
    ADD CONSTRAINT "ADGroups_pkey" PRIMARY KEY ("ADGroupId");


--
-- TOC entry 4078 (class 2606 OID 79167)
-- Name: ActiveLanIds ActiveLanIds_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."ActiveLanIds"
    ADD CONSTRAINT "ActiveLanIds_pkey" PRIMARY KEY ("LanId");


--
-- TOC entry 4016 (class 2606 OID 78891)
-- Name: Files Files_OriginalSharePointPath_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."Files"
    ADD CONSTRAINT "Files_OriginalSharePointPath_key" UNIQUE ("OriginalSharePointPath");


--
-- TOC entry 4018 (class 2606 OID 78889)
-- Name: Files Files_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."Files"
    ADD CONSTRAINT "Files_pkey" PRIMARY KEY ("FileId");


--
-- TOC entry 4074 (class 2606 OID 79157)
-- Name: FolderInheritanceBreaks FolderInheritanceBreaks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."FolderInheritanceBreaks"
    ADD CONSTRAINT "FolderInheritanceBreaks_pkey" PRIMARY KEY ("FolderInheritanceBreakId");


--
-- TOC entry 4052 (class 2606 OID 79042)
-- Name: FolderPermissions FolderPermissions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."FolderPermissions"
    ADD CONSTRAINT "FolderPermissions_pkey" PRIMARY KEY ("FolderPermissionId");


--
-- TOC entry 4026 (class 2606 OID 78908)
-- Name: Metadata Metadata_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."Metadata"
    ADD CONSTRAINT "Metadata_pkey" PRIMARY KEY ("MetadataId");


--
-- TOC entry 4086 (class 2606 OID 1336758)
-- Name: MigrationTasks_Parsed MigrationTasks_Parsed_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."MigrationTasks_Parsed"
    ADD CONSTRAINT "MigrationTasks_Parsed_pkey" PRIMARY KEY ("TaskId");


--
-- TOC entry 4069 (class 2606 OID 79098)
-- Name: MigrationTasks MigrationTasks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."MigrationTasks"
    ADD CONSTRAINT "MigrationTasks_pkey" PRIMARY KEY ("TaskId");


--
-- TOC entry 4080 (class 2606 OID 79178)
-- Name: Migration_Operation_Logs Migration_Operation_Logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."Migration_Operation_Logs"
    ADD CONSTRAINT "Migration_Operation_Logs_pkey" PRIMARY KEY ("OpId");


--
-- TOC entry 4083 (class 2606 OID 79188)
-- Name: Migration_Path_History Migration_Path_History_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."Migration_Path_History"
    ADD CONSTRAINT "Migration_Path_History_pkey" PRIMARY KEY ("HistoryId");


--
-- TOC entry 4088 (class 2606 OID 1336781)
-- Name: NewChangePath_NEW NewChangePath_NEW_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."NewChangePath_NEW"
    ADD CONSTRAINT "NewChangePath_NEW_pkey" PRIMARY KEY ("TaskId");


--
-- TOC entry 4061 (class 2606 OID 79071)
-- Name: PermissionMatrixSummaries PermissionMatrixSummaries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."PermissionMatrixSummaries"
    ADD CONSTRAINT "PermissionMatrixSummaries_pkey" PRIMARY KEY ("PermissionMatrixSummaryId");


--
-- TOC entry 4050 (class 2606 OID 79013)
-- Name: PermissionSetEntries PermissionSetEntries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."PermissionSetEntries"
    ADD CONSTRAINT "PermissionSetEntries_pkey" PRIMARY KEY ("PermissionSetEntryId");


--
-- TOC entry 4047 (class 2606 OID 78996)
-- Name: PermissionSets PermissionSets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."PermissionSets"
    ADD CONSTRAINT "PermissionSets_pkey" PRIMARY KEY ("PermissionSetId");


--
-- TOC entry 4030 (class 2606 OID 78931)
-- Name: Permissions Permissions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."Permissions"
    ADD CONSTRAINT "Permissions_pkey" PRIMARY KEY ("PermissionId");


--
-- TOC entry 4012 (class 2606 OID 78857)
-- Name: PostgreSQL_PermissionMatrix_20260503 PostgreSQL_PermissionMatrix_20260503_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."PostgreSQL_PermissionMatrix_20260503"
    ADD CONSTRAINT "PostgreSQL_PermissionMatrix_20260503_pkey" PRIMARY KEY ("ID");


--
-- TOC entry 4014 (class 2606 OID 78866)
-- Name: PostgreSQL_SiteGroupPermission_20260503 PostgreSQL_SiteGroupPermission_20260503_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."PostgreSQL_SiteGroupPermission_20260503"
    ADD CONSTRAINT "PostgreSQL_SiteGroupPermission_20260503_pkey" PRIMARY KEY ("ID");


--
-- TOC entry 4072 (class 2606 OID 79134)
-- Name: ScanProgress ScanProgress_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."ScanProgress"
    ADD CONSTRAINT "ScanProgress_pkey" PRIMARY KEY ("ScanProgressId");


--
-- TOC entry 4098 (class 2606 OID 1338761)
-- Name: nas_permission_matrix nas_permission_matrix_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nas_permission_matrix
    ADD CONSTRAINT nas_permission_matrix_pkey PRIMARY KEY (id);


--
-- TOC entry 4092 (class 2606 OID 1338638)
-- Name: break_path_mapping pk_file_migration_mapping; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.break_path_mapping
    ADD CONSTRAINT pk_file_migration_mapping PRIMARY KEY (task_id, file_name);


--
-- TOC entry 4096 (class 2606 OID 1338742)
-- Name: sg_permission_matrix sg_permission_matrix_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sg_permission_matrix
    ADD CONSTRAINT sg_permission_matrix_pkey PRIMARY KEY (id);


--
-- TOC entry 4090 (class 2606 OID 1338583)
-- Name: sitemapping sitemapping_pk; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sitemapping
    ADD CONSTRAINT sitemapping_pk PRIMARY KEY (sp2013_url);


--
-- TOC entry 4037 (class 1259 OID 78955)
-- Name: IX_ADGroups_SiteName; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "IX_ADGroups_SiteName" ON public."ADGroups" USING btree ("SiteName");


--
-- TOC entry 4019 (class 1259 OID 78892)
-- Name: IX_Files_FileName; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "IX_Files_FileName" ON public."Files" USING btree ("FileName");


--
-- TOC entry 4020 (class 1259 OID 78893)
-- Name: IX_Files_MigrationStatus; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "IX_Files_MigrationStatus" ON public."Files" USING btree ("MigrationStatus");


--
-- TOC entry 4021 (class 1259 OID 78896)
-- Name: IX_Files_PermResolution; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "IX_Files_PermResolution" ON public."Files" USING btree ("SiteName", "MigrationStatus") WHERE ("HasUniquePermissions" = false);


--
-- TOC entry 4022 (class 1259 OID 78895)
-- Name: IX_Files_SharePointUniqueId; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "IX_Files_SharePointUniqueId" ON public."Files" USING btree ("SharePointUniqueId");


--
-- TOC entry 4023 (class 1259 OID 78894)
-- Name: IX_Files_SiteName; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "IX_Files_SiteName" ON public."Files" USING btree ("SiteName");


--
-- TOC entry 4075 (class 1259 OID 79159)
-- Name: IX_FolderInheritanceBreaks_FolderPath; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "IX_FolderInheritanceBreaks_FolderPath" ON public."FolderInheritanceBreaks" USING btree ("FolderPath");


--
-- TOC entry 4076 (class 1259 OID 79158)
-- Name: IX_FolderInheritanceBreaks_LibSite; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "IX_FolderInheritanceBreaks_LibSite" ON public."FolderInheritanceBreaks" USING btree ("LibraryName", "SiteName");


--
-- TOC entry 4053 (class 1259 OID 79043)
-- Name: IX_FolderPermissions_FolderPath; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "IX_FolderPermissions_FolderPath" ON public."FolderPermissions" USING btree ("FolderPath");


--
-- TOC entry 4054 (class 1259 OID 79045)
-- Name: IX_FolderPermissions_ScanDate; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "IX_FolderPermissions_ScanDate" ON public."FolderPermissions" USING btree ("ScanDate");


--
-- TOC entry 4055 (class 1259 OID 79046)
-- Name: IX_FolderPermissions_SiteName; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "IX_FolderPermissions_SiteName" ON public."FolderPermissions" USING btree ("SiteName");


--
-- TOC entry 4056 (class 1259 OID 79044)
-- Name: IX_FolderPermissions_SiteUrl; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "IX_FolderPermissions_SiteUrl" ON public."FolderPermissions" USING btree ("SiteUrl");


--
-- TOC entry 4024 (class 1259 OID 78914)
-- Name: IX_Metadata_MetadataKey; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "IX_Metadata_MetadataKey" ON public."Metadata" USING btree ("MetadataKey");


--
-- TOC entry 4062 (class 1259 OID 79108)
-- Name: IX_MigrationTasks_ClaimedAt; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "IX_MigrationTasks_ClaimedAt" ON public."MigrationTasks" USING btree ("ClaimedAt");


--
-- TOC entry 4063 (class 1259 OID 79109)
-- Name: IX_MigrationTasks_SiteName; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "IX_MigrationTasks_SiteName" ON public."MigrationTasks" USING btree ("SiteName");


--
-- TOC entry 4064 (class 1259 OID 79104)
-- Name: IX_MigrationTasks_Status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "IX_MigrationTasks_Status" ON public."MigrationTasks" USING btree ("Status");


--
-- TOC entry 4065 (class 1259 OID 79105)
-- Name: IX_MigrationTasks_Status_Priority; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "IX_MigrationTasks_Status_Priority" ON public."MigrationTasks" USING btree ("Status", "Priority");


--
-- TOC entry 4066 (class 1259 OID 79107)
-- Name: IX_MigrationTasks_TaskType; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "IX_MigrationTasks_TaskType" ON public."MigrationTasks" USING btree ("TaskType");


--
-- TOC entry 4067 (class 1259 OID 79106)
-- Name: IX_MigrationTasks_WorkerId; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "IX_MigrationTasks_WorkerId" ON public."MigrationTasks" USING btree ("WorkerId");


--
-- TOC entry 4057 (class 1259 OID 79073)
-- Name: IX_PermissionMatrixSummaries_ScanDate; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "IX_PermissionMatrixSummaries_ScanDate" ON public."PermissionMatrixSummaries" USING btree ("ScanDate");


--
-- TOC entry 4058 (class 1259 OID 79074)
-- Name: IX_PermissionMatrixSummaries_SiteName; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "IX_PermissionMatrixSummaries_SiteName" ON public."PermissionMatrixSummaries" USING btree ("SiteName");


--
-- TOC entry 4059 (class 1259 OID 79072)
-- Name: IX_PermissionMatrixSummaries_SiteUrl; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "IX_PermissionMatrixSummaries_SiteUrl" ON public."PermissionMatrixSummaries" USING btree ("SiteUrl");


--
-- TOC entry 4048 (class 1259 OID 79019)
-- Name: IX_PermissionSetEntries_PermissionSetId; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "IX_PermissionSetEntries_PermissionSetId" ON public."PermissionSetEntries" USING btree ("PermissionSetId");


--
-- TOC entry 4042 (class 1259 OID 78997)
-- Name: IX_PermissionSets_PermissionKey; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "IX_PermissionSets_PermissionKey" ON public."PermissionSets" USING btree ("PermissionKey");


--
-- TOC entry 4043 (class 1259 OID 78999)
-- Name: IX_PermissionSets_ScanDate; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "IX_PermissionSets_ScanDate" ON public."PermissionSets" USING btree ("ScanDate");


--
-- TOC entry 4044 (class 1259 OID 79000)
-- Name: IX_PermissionSets_SiteName; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "IX_PermissionSets_SiteName" ON public."PermissionSets" USING btree ("SiteName");


--
-- TOC entry 4045 (class 1259 OID 78998)
-- Name: IX_PermissionSets_SiteUrl; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "IX_PermissionSets_SiteUrl" ON public."PermissionSets" USING btree ("SiteUrl");


--
-- TOC entry 4027 (class 1259 OID 78938)
-- Name: IX_Permissions_ApplyMethod; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "IX_Permissions_ApplyMethod" ON public."Permissions" USING btree ("ApplyMethod");


--
-- TOC entry 4028 (class 1259 OID 78937)
-- Name: IX_Permissions_OriginalSharePointGroup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "IX_Permissions_OriginalSharePointGroup" ON public."Permissions" USING btree ("OriginalSharePointGroup");


--
-- TOC entry 4070 (class 1259 OID 79135)
-- Name: IX_ScanProgress_Library_Site; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX "IX_ScanProgress_Library_Site" ON public."ScanProgress" USING btree ("LibraryName", "SiteName");


--
-- TOC entry 4093 (class 1259 OID 1338744)
-- Name: idx_sg_perm_principal_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sg_perm_principal_type ON public.sg_permission_matrix USING btree (principal_type);


--
-- TOC entry 4094 (class 1259 OID 1338743)
-- Name: idx_sg_perm_url_account; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sg_perm_url_account ON public.sg_permission_matrix USING btree (url, account_name);


--
-- TOC entry 4081 (class 1259 OID 79194)
-- Name: ix_MigrationOperationLogs_StepStatusTime; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_MigrationOperationLogs_StepStatusTime" ON public."Migration_Operation_Logs" USING btree ("StepName", "Status", "ExecutionTime" DESC);


--
-- TOC entry 4084 (class 1259 OID 79195)
-- Name: ix_MigrationPathHistory_OpId; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_MigrationPathHistory_OpId" ON public."Migration_Path_History" USING btree ("OpId");


--
-- TOC entry 4101 (class 2606 OID 78971)
-- Name: ADGroupMembers ADGroupMembers_ADGroupId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."ADGroupMembers"
    ADD CONSTRAINT "ADGroupMembers_ADGroupId_fkey" FOREIGN KEY ("ADGroupId") REFERENCES public."ADGroups"("ADGroupId") ON DELETE CASCADE;


--
-- TOC entry 4099 (class 2606 OID 78909)
-- Name: Metadata Metadata_FileId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."Metadata"
    ADD CONSTRAINT "Metadata_FileId_fkey" FOREIGN KEY ("FileId") REFERENCES public."Files"("FileId") ON DELETE CASCADE;


--
-- TOC entry 4103 (class 2606 OID 79099)
-- Name: MigrationTasks MigrationTasks_FileId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."MigrationTasks"
    ADD CONSTRAINT "MigrationTasks_FileId_fkey" FOREIGN KEY ("FileId") REFERENCES public."Files"("FileId") ON DELETE CASCADE;


--
-- TOC entry 4104 (class 2606 OID 79189)
-- Name: Migration_Path_History Migration_Path_History_OpId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."Migration_Path_History"
    ADD CONSTRAINT "Migration_Path_History_OpId_fkey" FOREIGN KEY ("OpId") REFERENCES public."Migration_Operation_Logs"("OpId") ON DELETE CASCADE;


--
-- TOC entry 4102 (class 2606 OID 79014)
-- Name: PermissionSetEntries PermissionSetEntries_PermissionSetId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."PermissionSetEntries"
    ADD CONSTRAINT "PermissionSetEntries_PermissionSetId_fkey" FOREIGN KEY ("PermissionSetId") REFERENCES public."PermissionSets"("PermissionSetId") ON DELETE CASCADE;


--
-- TOC entry 4100 (class 2606 OID 78932)
-- Name: Permissions Permissions_FileId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."Permissions"
    ADD CONSTRAINT "Permissions_FileId_fkey" FOREIGN KEY ("FileId") REFERENCES public."Files"("FileId") ON DELETE CASCADE;


-- Completed on 2026-09-02 22:46:04

--
-- PostgreSQL database dump complete
--

\unrestrict GznnfqE72JmfCEuQmZ6KciONvv9MRlAdi1O63ie8Mgvk6LYNItlSRKrIZSTWBQR

