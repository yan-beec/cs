#!/bin/sh
set -eu

: "${ARCHIVE_INGEST_PASSWORD:?Missing archive ingest password}"
: "${ARCHIVE_READER_PASSWORD:?Missing archive reader password}"
: "${ARCHIVE_QUALITY_PASSWORD:?Missing archive quality password}"

# The image runs this once, during initialization of an empty data volume.
# Secrets are read by psql from the container environment, not command arguments.
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=1 <<'SQL'
\getenv archive_db POSTGRES_DB
\getenv ingest_password ARCHIVE_INGEST_PASSWORD
\getenv reader_password ARCHIVE_READER_PASSWORD
\getenv quality_password ARCHIVE_QUALITY_PASSWORD

BEGIN;
CREATE ROLE archive_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE ROLE archive_ingest LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
    PASSWORD :'ingest_password';
CREATE ROLE archive_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
    PASSWORD :'reader_password';
CREATE ROLE archive_quality LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
    PASSWORD :'quality_password';

ALTER DATABASE :"archive_db" OWNER TO archive_owner;
REVOKE ALL ON DATABASE :"archive_db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"archive_db" TO archive_ingest, archive_reader, archive_quality;
REVOKE ALL ON SCHEMA public FROM PUBLIC;

CREATE SCHEMA archive AUTHORIZATION archive_owner;
CREATE SCHEMA ops AUTHORIZATION archive_owner;
CREATE SCHEMA quality AUTHORIZATION archive_owner;

GRANT USAGE ON SCHEMA archive TO archive_ingest, archive_reader, archive_quality;
GRANT USAGE ON SCHEMA ops TO archive_ingest, archive_reader, archive_quality;
GRANT USAGE ON SCHEMA quality TO archive_reader, archive_quality;

-- Evidence and completed quality versions are append-only for runtime users.
ALTER DEFAULT PRIVILEGES FOR ROLE archive_owner IN SCHEMA archive
    GRANT SELECT, INSERT ON TABLES TO archive_ingest;
ALTER DEFAULT PRIVILEGES FOR ROLE archive_owner IN SCHEMA archive
    GRANT SELECT ON TABLES TO archive_reader, archive_quality;
ALTER DEFAULT PRIVILEGES FOR ROLE archive_owner IN SCHEMA archive
    GRANT USAGE, SELECT ON SEQUENCES TO archive_ingest;

ALTER DEFAULT PRIVILEGES FOR ROLE archive_owner IN SCHEMA quality
    GRANT SELECT, INSERT ON TABLES TO archive_quality;
ALTER DEFAULT PRIVILEGES FOR ROLE archive_owner IN SCHEMA quality
    GRANT SELECT ON TABLES TO archive_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE archive_owner IN SCHEMA quality
    GRANT USAGE, SELECT ON SEQUENCES TO archive_quality;

-- Progress, retries and other mutable job state belong in ops.
ALTER DEFAULT PRIVILEGES FOR ROLE archive_owner IN SCHEMA ops
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO archive_ingest, archive_quality;
ALTER DEFAULT PRIVILEGES FOR ROLE archive_owner IN SCHEMA ops
    GRANT SELECT ON TABLES TO archive_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE archive_owner IN SCHEMA ops
    GRANT USAGE, SELECT ON SEQUENCES TO archive_ingest, archive_quality;

ALTER ROLE archive_ingest IN DATABASE :"archive_db" SET search_path = archive, ops;
ALTER ROLE archive_reader IN DATABASE :"archive_db" SET search_path = archive, quality, ops;
ALTER ROLE archive_quality IN DATABASE :"archive_db" SET search_path = quality, archive, ops;
COMMIT;
SQL

printf '%s\n' 'Archive database roles, schemas and default privileges initialized.'
