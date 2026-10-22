-- Reference DDL for the default config.json (28 columns). The program creates/extends the
-- table itself from config.json (Create / Sync Table); this file is for review / DBA use.
-- In a DB that has the consolidation's "Sites" table, the program also adds "SiteId" + FK + trigger.

CREATE TABLE IF NOT EXISTS public."MetadataHorizontal" (
    "MetadataHorizontalId" serial PRIMARY KEY,
    "FileId" integer NOT NULL UNIQUE REFERENCES public."Files"("FileId") ON DELETE CASCADE,
    "SiteName" varchar(256),
    "FileType" varchar(4000),
    "ParentDocumentNo" varchar(4000),
    "DocumentDrawingNo" varchar(4000),
    "SequenceNo" varchar(4000),
    "RevisionNo" varchar(4000),
    "AttachmentNo" varchar(4000),
    "ProjectOrganisation" varchar(4000),
    "ContractAgreementNo" varchar(4000),
    "WorkContractNo" varchar(4000),
    "DocumentTypeNature" varchar(4000),
    "Originator" varchar(4000),
    "OriginatorReferenceNo" varchar(4000),
    "Subject" varchar(4000),
    "OtherSubjectMeetingType" varchar(4000),
    "FileTitle" varchar(4000),
    "DocumentLocation" varchar(4000),
    "DocumentDate" varchar(4000),
    "DocumentReceivedDate" varchar(4000),
    "AccessLevel" varchar(4000),
    "DocumentStatus" varchar(4000),
    "PrimaryRecipient" varchar(4000),
    "AdditionalRecipient" varchar(4000),
    "Remarks" varchar(4000),
    "OriginalFilename" varchar(4000),
    "FlagWorkflow" varchar(4000),
    "FlagPadlock" varchar(4000),
    "FlagUnreachable" varchar(4000),
    "FileDescription" varchar(4000),
    "SourceFile" varchar(1000),
    "ImportedAt" timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "UpdatedAt" timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS "IX_MetadataHorizontal_SiteName" ON public."MetadataHorizontal" ("SiteName");
