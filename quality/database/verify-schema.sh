#!/bin/sh
set -eu

quality_database_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
psql -X --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    --set ON_ERROR_STOP=1 --file "$quality_database_directory/verify-schema.sql"

PGPASSWORD="$ARCHIVE_INGEST_PASSWORD" psql -X --host 127.0.0.1 --username archive_ingest \
    --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=1 --command 'SELECT current_user, current_database();'
PGPASSWORD="$ARCHIVE_READER_PASSWORD" psql -X --host 127.0.0.1 --username archive_reader \
    --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=1 --command 'SELECT current_user, current_database();'
PGPASSWORD="$ARCHIVE_QUALITY_PASSWORD" psql -X --host 127.0.0.1 --username archive_quality \
    --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=1 --command 'SELECT current_user, current_database();'
printf '%s\n' 'PASS: capture schema, append-only evidence, task permissions and account authentication.'
