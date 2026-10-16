-- A dedicated append-only identity for ICS assignment delivery.
BEGIN;
SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(175607, 1);

SELECT EXISTS (SELECT 1 FROM ops.schema_migrations WHERE version = '003_assignment_writer') AS already_applied \gset
\if :already_applied
    SELECT checksum_sha256 = :'migration_checksum' AS checksum_matches
    FROM ops.schema_migrations WHERE version = '003_assignment_writer' \gset
    \if :checksum_matches
        \echo '003_assignment_writer already applied; unchanged migration skipped.'
    \else
        \echo 'ERROR: applied migration checksum differs; restore it and add a new migration.'
        \quit 3
    \endif
\else
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'archive_assignment') THEN
        CREATE ROLE archive_assignment NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION CONNECTION LIMIT 4;
    END IF;
END $$;
SET LOCAL ROLE archive_owner;
SELECT format('GRANT CONNECT ON DATABASE %I TO archive_assignment', current_database()) \gexec
GRANT USAGE ON SCHEMA archive, ops TO archive_assignment;
GRANT SELECT, INSERT ON archive.events, ops.event_jobs TO archive_assignment;
GRANT USAGE, SELECT ON SEQUENCE archive.events_id_seq TO archive_assignment;
REVOKE ALL ON ops.schema_migrations FROM archive_assignment;
INSERT INTO ops.schema_migrations (version, checksum_sha256, description)
VALUES ('003_assignment_writer', :'migration_checksum', 'Restricted append-only delivery of confirmed ICS round assignments');
\endif
COMMIT;
