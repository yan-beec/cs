#!/bin/sh
set -eu

: "${POSTGRES_USER:?Run inside the archive-postgres container}"
: "${POSTGRES_DB:?Missing target archive database}"
quality_database_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

quality_recorded_checksum() {
    psql -X --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --set "migration_version=$quality_version" <<'SQL'
SELECT to_regclass('ops.schema_migrations') IS NOT NULL AS has_history \gset
\if :has_history
SELECT checksum_sha256 FROM ops.schema_migrations WHERE version = :'migration_version';
\endif
SQL
}

for quality_migration in "$quality_database_directory"/migrations/*.sql; do
    [ -f "$quality_migration" ] || { printf '%s\n' 'No schema migrations found.' >&2; exit 1; }
    quality_version=$(basename -- "$quality_migration" .sql)
    quality_checksum=$(sha256sum "$quality_migration" | cut -d ' ' -f 1)
    quality_existing_checksum=$(quality_recorded_checksum)
    if [ -n "$quality_existing_checksum" ]; then
        if [ "$quality_existing_checksum" != "$quality_checksum" ]; then
            printf 'ERROR: %s checksum differs; restore the applied file and add a new migration.\n' "$quality_version" >&2
            exit 1
        fi
        printf '%s already applied; unchanged migration skipped.\n' "$quality_version"
        continue
    fi
    printf 'Applying %s\n' "$(basename -- "$quality_migration")"
    psql -X --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
        --set ON_ERROR_STOP=1 --set "migration_checksum=$quality_checksum" \
        --file "$quality_migration"
    # PostgreSQL 16 psql ignores exit-code arguments to \quit. Verify committed
    # history as well, so an early client exit can never report false success.
    quality_existing_checksum=$(quality_recorded_checksum)
    if [ "$quality_existing_checksum" != "$quality_checksum" ]; then
        printf 'ERROR: %s did not record the expected checksum.\n' "$quality_version" >&2
        exit 1
    fi
done
