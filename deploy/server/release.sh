#!/usr/bin/env bash
set -euo pipefail

source_directory=/opt/cs-src
target_directory=/opt/cs
in_place=false
with_receiver=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --source|--target)
            [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || { printf 'Missing value for %s\n' "$1" >&2; exit 2; }
            if [[ "$1" == --source ]]; then source_directory=$2; else target_directory=$2; fi
            shift 2 ;;
        --in-place) in_place=true; shift ;;
        --with-receiver) with_receiver=true; shift ;;
        --help)
            printf '%s\n' 'Usage: bash release.sh [--source /opt/cs-src] [--target /opt/cs] [--in-place] [--with-receiver]'
            printf '%s\n' 'Use --in-place only after a release package has been extracted into the target.'
            exit 0 ;;
        *) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
    esac
done
for command_name in docker flock realpath; do
    command -v "$command_name" >/dev/null || { printf 'Missing command: %s\n' "$command_name" >&2; exit 1; }
done
target_directory=$(realpath -e -- "$target_directory")
[[ "$target_directory" != / && -f "$target_directory/.env" ]] || {
    printf '%s\n' 'Target must be a configured ICS directory containing its server .env.' >&2
    exit 1
}
# Serialize synchronization, migrations and the application update as one release.
exec 9>"$target_directory/.deployment.lock"
flock -n 9 || { printf '%s\n' 'Another deployment is running for this target.' >&2; exit 1; }
stage='preflight'
trap 'printf "Release stopped during %s; resolve the error before retrying.\n" "$stage" >&2' ERR
source_commit=''
if [[ "$in_place" == false ]]; then
    source_directory=$(realpath -e -- "$source_directory")
    if [[ -e "$source_directory/.git" ]]; then
        source_status=$(git -C "$source_directory" status --porcelain)
        [[ -z "$source_status" ]] || {
            printf '%s\n' 'Source checkout has local changes; preserve and resolve them before publishing.' >&2
            exit 1
        }
        source_commit=$(git -C "$source_directory" rev-parse HEAD)
    fi
    stage='code synchronization'
    bash "$source_directory/deploy/server/sync-code.sh" "$source_directory" "$target_directory"
else
    [[ -f "$target_directory/deploy/server/Dockerfile.backend-only" ]] || { printf '%s\n' 'Missing backend Dockerfile template.' >&2; exit 1; }
    [[ ! -L "$target_directory/Dockerfile" ]] || { printf '%s\n' 'Refusing symlink Dockerfile target.' >&2; exit 1; }
    cp -- "$target_directory/deploy/server/Dockerfile.backend-only" "$target_directory/Dockerfile"
fi
cd -- "$target_directory"
stage='quality database migration and verification'
bash quality/deploy/migrate.sh "$target_directory"

# Maintenance credentials are used only by the database helper, never the bot.
compose=(docker compose --project-directory "$target_directory" --env-file .env -f docker-compose.yml -f docker-compose.archive.yml)
stage='application image build'
"${compose[@]}" build ics-agent
if [[ "$with_receiver" == true ]]; then
    stage='quality receiver update and health verification'
    "${compose[@]}" --profile quality-capture up -d --no-deps --wait --wait-timeout 60 quality-receiver
fi
stage='application container update'
# The existing business database is checked before maintenance. Publishing the
# application must not recreate it because the shared Compose file changed.
"${compose[@]}" up -d --no-deps ics-agent
if [[ -n "$source_commit" ]]; then
    mkdir -p deployment-records
    printf '%s\n' "$source_commit" > deployment-records/source-commit.txt
fi
"${compose[@]}" ps
printf '%s\n' 'Database verified and application container updated. Check application readiness after model warm-up.'
