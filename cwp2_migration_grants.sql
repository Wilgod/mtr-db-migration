-- =============================================================================
-- cwp2_migration_grants.sql
-- Rights for the MTR DB migration tool (MtrDbMigration.exe), which builds the
-- consolidated database in cwp2 while running as role cwp_mig_prd
-- (login ssishadm_prj1 + SET ROLE cwp_mig_prd).
--
-- Run as postgres (superuser) on hqscwpd001v:
--     psql -U postgres -f cwp2_migration_grants.sql
--
-- cwp2 itself stays owned by cwppsvc; nothing here changes ownership.
-- Every statement can be run again safely.
-- =============================================================================

\set ON_ERROR_STOP on

-- -----------------------------------------------------------------------------
-- 1. Target database cwp2
-- -----------------------------------------------------------------------------
\c cwp2

-- Create schemas. The tool creates schema _merge (migration audit tables, kept
-- afterwards) and one schema src_<source> per source database (links to the
-- source tables, dropped again when the migration finishes).
-- cwp_mig_prd currently has only CONNECT + TEMPORARY, so CREATE SCHEMA fails:
--   00_target_schema.sql:601: ERROR: permission denied for database cwp2
GRANT CREATE ON DATABASE cwp2 TO cwp_mig_prd;

-- Create the application tables in public (already works; repeated for completeness).
GRANT USAGE, CREATE ON SCHEMA public TO cwp_mig_prd;

-- postgres_fdw links cwp2 to the source databases. Installing the extension
-- needs a superuser; cwp_mig_prd then needs USAGE to create the links.
CREATE EXTENSION IF NOT EXISTS postgres_fdw;
GRANT USAGE ON FOREIGN DATA WRAPPER postgres_fdw TO cwp_mig_prd;

-- -----------------------------------------------------------------------------
-- 2. Source databases (read only)
-- The tool reads these 8 databases. cwp_procon / cwp_opspj / cwp_opspj2 /
-- cwp_opspj3 / cwp_opspj4 already have CONNECT; cwp, cwp_ecr and cwp_xrl
-- did not appear in cwp2_db_creation_script.txt.
-- -----------------------------------------------------------------------------
GRANT CONNECT ON DATABASE cwp, cwp_ecr, cwp_opspj, cwp_opspj2, cwp_opspj3, cwp_opspj4, cwp_procon, cwp_xrl
    TO cwp_mig_prd;

\c cwp
GRANT USAGE ON SCHEMA public TO cwp_mig_prd;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO cwp_mig_prd;
\c cwp_ecr
GRANT USAGE ON SCHEMA public TO cwp_mig_prd;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO cwp_mig_prd;
\c cwp_opspj
GRANT USAGE ON SCHEMA public TO cwp_mig_prd;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO cwp_mig_prd;
\c cwp_opspj2
GRANT USAGE ON SCHEMA public TO cwp_mig_prd;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO cwp_mig_prd;
\c cwp_opspj3
GRANT USAGE ON SCHEMA public TO cwp_mig_prd;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO cwp_mig_prd;
\c cwp_opspj4
GRANT USAGE ON SCHEMA public TO cwp_mig_prd;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO cwp_mig_prd;
\c cwp_procon
GRANT USAGE ON SCHEMA public TO cwp_mig_prd;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO cwp_mig_prd;
\c cwp_xrl
GRANT USAGE ON SCHEMA public TO cwp_mig_prd;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO cwp_mig_prd;

-- -----------------------------------------------------------------------------
-- 3. Login used for the source links  ** DBA decision needed **
-- postgres_fdw connects from the server (cwp2) to each source database on
-- localhost:5432. For a non-superuser PostgreSQL requires that connection to
-- use a PASSWORD (Kerberos/trust is only allowed for superusers), and
-- cwp_mig_prd is a group (NOLOGIN), so it cannot be that login itself.
--
-- Option A (recommended): a dedicated password login that is a member of
-- cwp_mig_prd; the operator types it in the app's "Source login" box.
--     CREATE ROLE cwp_mig_src LOGIN PASSWORD '<strong password>' IN ROLE cwp_mig_prd;
--   and pg_hba.conf must allow it from the server itself with a password, e.g.
--     host  all  cwp_mig_src  127.0.0.1/32  scram-sha-256
--     host  all  cwp_mig_src  ::1/128       scram-sha-256
--
-- Option B: the DBA tells the operator which existing password login to use.
-- -----------------------------------------------------------------------------

-- -----------------------------------------------------------------------------
-- 4. Check (all columns should be t)
-- -----------------------------------------------------------------------------
\c cwp2
SELECT has_database_privilege('cwp_mig_prd', 'cwp2', 'CREATE')                    AS create_schemas,
       has_schema_privilege('cwp_mig_prd', 'public', 'CREATE')                    AS create_tables,
       has_foreign_data_wrapper_privilege('cwp_mig_prd', 'postgres_fdw', 'USAGE') AS use_fdw,
       pg_has_role('ssishadm_prj1', 'cwp_mig_prd', 'SET')                        AS login_can_set_role;
