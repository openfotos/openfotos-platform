-- Run once as the Supabase postgres role before the first Django migration.
-- This script creates no password and can safely remain in source control.

BEGIN;

CREATE SCHEMA IF NOT EXISTS extensions;
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions;
CREATE SCHEMA IF NOT EXISTS openfotos AUTHORIZATION postgres;

DO $openfotos$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'openfotos_runtime') THEN
        CREATE ROLE openfotos_runtime
            LOGIN
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOREPLICATION
            NOBYPASSRLS;
    END IF;
END
$openfotos$;

ALTER ROLE openfotos_runtime SET search_path = openfotos, extensions;
ALTER ROLE openfotos_runtime SET statement_timeout = '15s';
ALTER ROLE openfotos_runtime SET idle_in_transaction_session_timeout = '30s';

REVOKE ALL ON SCHEMA openfotos FROM PUBLIC, anon, authenticated, service_role;
GRANT USAGE ON SCHEMA openfotos TO openfotos_runtime;
GRANT USAGE ON SCHEMA extensions TO openfotos_runtime;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

COMMIT;
