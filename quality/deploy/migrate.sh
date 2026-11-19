#!/usr/bin/env bash
set -euo pipefail

if [[ $# -gt 1 ]]; then
    printf 'Usage: bash %s [PROJECT_DIRECTORY]\n' "$0" >&2
    exit 2
fi
script_directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
project_directory=$(cd -- "${1:-$script_directory/../..}" && pwd -P)
cd -- "$project_directory"
[[ -f .env ]] || { printf '%s\n' 'Missing project .env; configure server credentials first.' >&2; exit 1; }
[[ -f quality/database/apply-migrations.sh && -f quality/database/verify-schema.sh ]] || {
    printf '%s\n' 'Missing quality database scripts; synchronize the complete release first.' >&2
    exit 1
}
[[ -d deploy/archive/init && -f deploy/archive/init/01-archive-roles.sh ]] || {
    printf '%s\n' 'Missing archive initialization script; synchronize the complete release first.' >&2
    exit 1
}
# Also cover in-place releases and older private checkouts. The initialization
# script reads secrets from the environment; only this public code is exposed.
chmod 644 -- deploy/archive/init/01-archive-roles.sh
chmod 755 -- deploy/archive/init
compose=(docker compose --project-directory "$project_directory" --env-file .env -f docker-compose.yml -f docker-compose.archive.yml)
"${compose[@]}" config --quiet
printf '%s\n' 'Starting archive database and waiting for it to become healthy...'
"${compose[@]}" up -d --no-deps --wait --wait-timeout 120 archive-postgres

# A fresh directory prevents removed or older SQL files from leaking into a release.
container_directory=$("${compose[@]}" exec -T archive-postgres mktemp -d /tmp/ics-quality-migrate.XXXXXX)
[[ "$container_directory" =~ ^/tmp/ics-quality-migrate\.[[:alnum:]]+$ ]] || {
    printf '%s\n' 'Unexpected migration staging directory returned by the container.' >&2
    exit 1
}
cleanup() {
    local saved_status=$?
    trap - EXIT
    "${compose[@]}" exec -T archive-postgres rm -rf -- "$container_directory" >/dev/null 2>&1 || true
    exit "$saved_status"
}
trap cleanup EXIT
"${compose[@]}" cp quality/database/. "archive-postgres:$container_directory"
printf '%s\n' 'Applying quality schema migrations...'
"${compose[@]}" exec -T archive-postgres sh "$container_directory/apply-migrations.sh"
printf '%s\n' 'Verifying schema, append-only permissions and database credentials...'
"${compose[@]}" exec -T archive-postgres sh "$container_directory/verify-schema.sh"
printf '%s\n' 'Quality database migration and verification passed.'
