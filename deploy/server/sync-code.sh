#!/usr/bin/env bash
set -euo pipefail

# Shared ICS code synchronization. Runtime data is never a synchronization target.
if [[ $# -ne 0 && $# -ne 2 ]]; then
    printf 'Usage: bash %s [SOURCE_DIRECTORY TARGET_DIRECTORY]\n' "$0" >&2
    exit 2
fi
for command_name in rsync realpath; do
    command -v "$command_name" >/dev/null || { printf 'Missing command: %s\n' "$command_name" >&2; exit 1; }
done
source_directory=$(realpath -e -- "${1:-/opt/cs-src}")
target_directory=$(realpath -m -- "${2:-/opt/cs}")
if [[ "$source_directory" == / || "$target_directory" == / ]]; then
    printf '%s\n' 'Source and target must be dedicated project directories.' >&2
    exit 1
fi
case "$source_directory/" in "$target_directory/"*) printf '%s\n' 'Source and target must not overlap.' >&2; exit 1 ;; esac
case "$target_directory/" in "$source_directory/"*) printf '%s\n' 'Source and target must not overlap.' >&2; exit 1 ;; esac

for relative_path in ics_agent knowledge_base_in reminder quality statistics deploy docs; do
    [[ -d "$source_directory/$relative_path" ]] || { printf 'Missing source directory: %s\n' "$relative_path" >&2; exit 1; }
    [[ ! -L "$target_directory/$relative_path" ]] || { printf 'Refusing symlink target: %s\n' "$relative_path" >&2; exit 1; }
done
files=(docker-compose.yml docker-compose.archive.yml .dockerignore requirements.txt .env.example)
for relative_path in "${files[@]}" deploy/server/Dockerfile.backend-only deploy/archive/init/01-archive-roles.sh quality/deploy/migrate.sh quality/database/apply-migrations.sh; do
    [[ -f "$source_directory/$relative_path" ]] || { printf 'Missing source file: %s\n' "$relative_path" >&2; exit 1; }
done
for relative_path in "${files[@]}" Dockerfile; do
    [[ ! -L "$target_directory/$relative_path" ]] || { printf 'Refusing symlink target: %s\n' "$relative_path" >&2; exit 1; }
done

mkdir -p -- "$target_directory"
for relative_path in ics_agent quality statistics; do
    mkdir -p -- "$target_directory/$relative_path"
    rsync -a --delete --exclude='__pycache__/' --exclude='*.pyc' \
        "$source_directory/$relative_path/" "$target_directory/$relative_path/"
done
mkdir -p -- "$target_directory/knowledge_base_in" "$target_directory/reminder" "$target_directory/deploy" "$target_directory/docs"
rsync -a --delete --exclude='/output/' --exclude='/vector_store/' \
    --exclude='__pycache__/' --exclude='*.pyc' \
    "$source_directory/knowledge_base_in/" "$target_directory/knowledge_base_in/"
rsync -a --delete --exclude='/build/' --exclude='/dist/' \
    --exclude='__pycache__/' --exclude='*.pyc' --exclude='.pytest_cache/' \
    "$source_directory/reminder/" "$target_directory/reminder/"
rsync -a "$source_directory/deploy/" "$target_directory/deploy/"
# A private checkout (umask 077) must not make the bind-mounted init scripts
# unreadable to PostgreSQL's container user. These files contain no secrets.
chmod 644 -- "$target_directory/deploy/archive/init/01-archive-roles.sh"
chmod 755 -- "$target_directory/deploy/archive/init"
rsync -a "$source_directory/docs/" "$target_directory/docs/"
for relative_path in "${files[@]}"; do
    cp -- "$source_directory/$relative_path" "$target_directory/$relative_path"
done
cp -- "$source_directory/deploy/server/Dockerfile.backend-only" "$target_directory/Dockerfile"
printf '%s\n' 'ICS code and quality migrations synchronized; runtime configuration and data preserved.'
