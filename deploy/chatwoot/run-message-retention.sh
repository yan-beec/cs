#!/usr/bin/env bash
set -euo pipefail

# No Compose changes, container rebuilds, or credentials in this wrapper.
# The Ruby source is passed to the existing Rails container through stdin.
retention_directory=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
retention_rails=${CHATWOOT_RAILS_CONTAINER:-chatwoot-rails-1}
retention_sidekiq=${CHATWOOT_SIDEKIQ_CONTAINER:-chatwoot-sidekiq-1}
retention_days=${CHATWOOT_RETENTION_DAYS:-60}
retention_limit=${CHATWOOT_RETENTION_BATCH_SIZE:-500}
retention_preview=${CHATWOOT_RETENTION_DRY_RUN:-1}
: "${CHATWOOT_RETENTION_ACCOUNT_IDS:?Set the Chatwoot account IDs already fully archived in ICS}"

for retention_container in "$retention_rails" "$retention_sidekiq"; do
    if [[ "$(docker inspect --format '{{.State.Running}}' "$retention_container")" != true ]]; then
        printf 'Required container is not running: %s\n' "$retention_container" >&2
        exit 1
    fi
done

docker exec -i \
    -e "CHATWOOT_RETENTION_ACCOUNT_IDS=$CHATWOOT_RETENTION_ACCOUNT_IDS" \
    -e "CHATWOOT_RETENTION_DAYS=$retention_days" \
    -e "CHATWOOT_RETENTION_BATCH_SIZE=$retention_limit" \
    -e "CHATWOOT_RETENTION_DRY_RUN=$retention_preview" \
    "$retention_rails" bundle exec rails runner - < "$retention_directory/message_retention.rb"
