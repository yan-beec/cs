-- Applied files are immutable; add a new numbered migration for later changes.
BEGIN;
SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(175607, 1);
SET LOCAL ROLE archive_owner;

CREATE TABLE IF NOT EXISTS ops.schema_migrations (
    version TEXT PRIMARY KEY,
    checksum_sha256 TEXT NOT NULL CHECK (checksum_sha256 ~ '^[0-9a-f]{64}$'),
    description TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
REVOKE ALL ON ops.schema_migrations FROM PUBLIC, archive_ingest, archive_reader, archive_quality;

SELECT EXISTS (SELECT 1 FROM ops.schema_migrations WHERE version = '001_capture') AS already_applied \gset
\if :already_applied
    SELECT checksum_sha256 = :'migration_checksum' AS checksum_matches
    FROM ops.schema_migrations WHERE version = '001_capture' \gset
    \if :checksum_matches
        \echo '001_capture already applied; unchanged migration skipped.'
    \else
        \echo 'ERROR: applied migration checksum differs; restore it and add a new migration.'
        \quit 3
    \endif
\else

-- Future ops tables require explicit per-table grants. Quality workers must not
-- inherit write access to ingestion queues or to migration history.
ALTER DEFAULT PRIVILEGES FOR ROLE archive_owner IN SCHEMA ops
    REVOKE ALL ON TABLES FROM archive_ingest, archive_reader, archive_quality;
ALTER DEFAULT PRIVILEGES FOR ROLE archive_owner IN SCHEMA ops
    REVOKE ALL ON SEQUENCES FROM archive_ingest, archive_reader, archive_quality;

CREATE TABLE archive.events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_key TEXT NOT NULL CHECK (btrim(source_key) <> ''),
    account_id BIGINT NOT NULL CHECK (account_id > 0),
    event_key TEXT NOT NULL CHECK (btrim(event_key) <> ''),
    event_type TEXT NOT NULL CHECK (btrim(event_type) <> ''),
    ingestion_kind TEXT NOT NULL CHECK (ingestion_kind IN ('webhook', 'backfill')),
    conversation_id BIGINT CHECK (conversation_id > 0),
    message_id BIGINT CHECK (message_id > 0),
    source_time TIMESTAMPTZ,
    received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    raw_body BYTEA NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    body_sha256 TEXT GENERATED ALWAYS AS (encode(sha256(raw_body), 'hex')) STORED,
    CONSTRAINT events_payload_matches_body CHECK (convert_from(raw_body, 'UTF8')::JSONB = payload),
    CONSTRAINT events_source_key UNIQUE (source_key, account_id, event_key),
    CONSTRAINT events_scope_key UNIQUE (id, source_key, account_id)
);
CREATE INDEX events_received_idx ON archive.events (received_at, id);
CREATE INDEX events_conversation_idx ON archive.events (source_key, account_id, conversation_id, id);

-- Each row is one observed version. The Chatwoot message identity is the tuple
-- (source_key, account_id, message_id), not this table's generated snapshot id.
CREATE TABLE archive.message_snapshots (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id BIGINT NOT NULL,
    source_key TEXT NOT NULL,
    account_id BIGINT NOT NULL,
    conversation_id BIGINT NOT NULL CHECK (conversation_id > 0),
    message_id BIGINT NOT NULL CHECK (message_id > 0),
    inbox_id BIGINT CHECK (inbox_id > 0),
    channel_key TEXT,
    business_user_id TEXT,
    contact_id TEXT,
    snapshot_kind TEXT NOT NULL CHECK (snapshot_kind IN ('created', 'updated', 'deleted', 'backfill')),
    direction TEXT NOT NULL CHECK (direction IN ('incoming', 'outgoing', 'activity', 'unknown')),
    sender_role TEXT NOT NULL CHECK (sender_role IN ('contact', 'agent', 'bot', 'system', 'unknown')),
    sender_id TEXT,
    sender_name TEXT,
    content TEXT,
    content_type TEXT NOT NULL DEFAULT 'text',
    content_attributes JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(content_attributes) = 'object'),
    is_private BOOLEAN NOT NULL DEFAULT FALSE,
    sent_at TIMESTAMPTZ,
    source_updated_at TIMESTAMPTZ,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT message_snapshots_event_fk FOREIGN KEY (event_id, source_key, account_id)
        REFERENCES archive.events (id, source_key, account_id),
    CONSTRAINT message_snapshots_event_message_key UNIQUE (event_id, message_id)
);
CREATE INDEX message_snapshots_history_idx
    ON archive.message_snapshots (source_key, account_id, message_id, id);
CREATE INDEX message_snapshots_conversation_idx
    ON archive.message_snapshots (source_key, account_id, conversation_id, sent_at, message_id);
CREATE INDEX message_snapshots_channel_idx ON archive.message_snapshots (channel_key, sent_at, id);
CREATE INDEX message_snapshots_user_idx
    ON archive.message_snapshots (channel_key, business_user_id, sent_at, id);

CREATE TABLE archive.conversation_snapshots (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id BIGINT NOT NULL,
    source_key TEXT NOT NULL,
    account_id BIGINT NOT NULL,
    conversation_id BIGINT NOT NULL CHECK (conversation_id > 0),
    inbox_id BIGINT CHECK (inbox_id > 0),
    channel_key TEXT,
    event_type TEXT NOT NULL,
    source_time TIMESTAMPTZ,
    data JSONB NOT NULL CHECK (jsonb_typeof(data) = 'object'),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT conversation_snapshots_event_fk FOREIGN KEY (event_id, source_key, account_id)
        REFERENCES archive.events (id, source_key, account_id),
    CONSTRAINT conversation_snapshots_event_key UNIQUE (event_id, conversation_id)
);
CREATE INDEX conversation_snapshots_history_idx
    ON archive.conversation_snapshots (source_key, account_id, conversation_id, id);

CREATE TABLE archive.attachments (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    snapshot_id BIGINT NOT NULL REFERENCES archive.message_snapshots (id),
    attachment_key TEXT NOT NULL CHECK (btrim(attachment_key) <> ''),
    source_attachment_id TEXT,
    filename TEXT,
    media_type TEXT,
    source_url TEXT,
    expected_byte_count BIGINT CHECK (expected_byte_count >= 0),
    metadata JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(metadata) = 'object'),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT attachments_snapshot_key UNIQUE (snapshot_id, attachment_key)
);

-- Insert only after the file has actually been copied and its bytes checked.
CREATE TABLE archive.attachment_copies (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    attachment_id BIGINT NOT NULL REFERENCES archive.attachments (id),
    storage_backend TEXT NOT NULL CHECK (btrim(storage_backend) <> ''),
    storage_namespace TEXT NOT NULL CHECK (btrim(storage_namespace) <> ''),
    object_key TEXT NOT NULL CHECK (btrim(object_key) <> ''),
    byte_count BIGINT NOT NULL CHECK (byte_count >= 0),
    checksum_sha256 TEXT NOT NULL CHECK (checksum_sha256 ~ '^[0-9a-f]{64}$'),
    archived_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT attachment_copies_storage_key UNIQUE (storage_backend, storage_namespace, object_key)
);
CREATE INDEX attachment_copies_attachment_idx ON archive.attachment_copies (attachment_id, id);

CREATE TABLE ops.event_jobs (
    event_id BIGINT PRIMARY KEY REFERENCES archive.events (id),
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'processing', 'retry', 'completed', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    locked_by TEXT,
    locked_until TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    CHECK ((locked_by IS NULL) = (locked_until IS NULL)),
    CHECK ((status = 'processing') = (locked_by IS NOT NULL)),
    CHECK ((status = 'completed') = (completed_at IS NOT NULL))
);
CREATE INDEX event_jobs_ready_idx ON ops.event_jobs (available_at, event_id)
    WHERE status IN ('queued', 'retry');
CREATE INDEX event_jobs_lease_idx ON ops.event_jobs (locked_until, event_id) WHERE status = 'processing';

CREATE TABLE ops.attachment_jobs (
    attachment_id BIGINT PRIMARY KEY REFERENCES archive.attachments (id),
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'processing', 'retry', 'completed', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    locked_by TEXT,
    locked_until TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    CHECK ((locked_by IS NULL) = (locked_until IS NULL)),
    CHECK ((status = 'processing') = (locked_by IS NOT NULL)),
    CHECK ((status = 'completed') = (completed_at IS NOT NULL))
);
CREATE INDEX attachment_jobs_ready_idx ON ops.attachment_jobs (available_at, attachment_id)
    WHERE status IN ('queued', 'retry');
CREATE INDEX attachment_jobs_lease_idx ON ops.attachment_jobs (locked_until, attachment_id)
    WHERE status = 'processing';

-- This projection can be updated or rebuilt; it is not the historical evidence.
CREATE TABLE ops.conversation_index (
    source_key TEXT NOT NULL CHECK (btrim(source_key) <> ''),
    account_id BIGINT NOT NULL CHECK (account_id > 0),
    conversation_id BIGINT NOT NULL CHECK (conversation_id > 0),
    inbox_id BIGINT CHECK (inbox_id > 0),
    channel_key TEXT,
    contact_id TEXT,
    business_user_id TEXT,
    contact_name TEXT,
    current_assignee_id TEXT,
    current_status TEXT NOT NULL DEFAULT 'unknown',
    last_message_at TIMESTAMPTZ,
    last_event_id BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source_key, account_id, conversation_id),
    CONSTRAINT conversation_index_event_fk FOREIGN KEY (last_event_id, source_key, account_id)
        REFERENCES archive.events (id, source_key, account_id)
);
CREATE INDEX conversation_index_channel_idx ON ops.conversation_index (channel_key, last_message_at DESC);
CREATE INDEX conversation_index_user_idx
    ON ops.conversation_index (channel_key, business_user_id, last_message_at DESC);
CREATE INDEX conversation_index_agent_idx
    ON ops.conversation_index (current_assignee_id, last_message_at DESC);

CREATE TABLE ops.backfill_jobs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_key TEXT NOT NULL CHECK (btrim(source_key) <> ''),
    account_id BIGINT NOT NULL CHECK (account_id > 0),
    scope JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(scope) = 'object'),
    checkpoint JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(checkpoint) = 'object'),
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'processing', 'retry', 'completed', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    processed_count BIGINT NOT NULL DEFAULT 0 CHECK (processed_count >= 0),
    available_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    locked_by TEXT,
    locked_until TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    CHECK ((locked_by IS NULL) = (locked_until IS NULL)),
    CHECK ((status = 'processing') = (locked_by IS NOT NULL)),
    CHECK ((status = 'completed') = (completed_at IS NOT NULL))
);
CREATE INDEX backfill_jobs_ready_idx ON ops.backfill_jobs (available_at, id)
    WHERE status IN ('queued', 'retry');
CREATE INDEX backfill_jobs_lease_idx ON ops.backfill_jobs (locked_until, id) WHERE status = 'processing';

REVOKE ALL ON ALL TABLES IN SCHEMA archive FROM PUBLIC, archive_ingest, archive_reader, archive_quality;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA archive FROM PUBLIC, archive_ingest, archive_reader, archive_quality;
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA archive TO archive_ingest;
GRANT SELECT ON ALL TABLES IN SCHEMA archive TO archive_reader, archive_quality;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA archive TO archive_ingest;

REVOKE ALL ON ALL TABLES IN SCHEMA ops FROM PUBLIC, archive_ingest, archive_reader, archive_quality;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA ops FROM PUBLIC, archive_ingest, archive_reader, archive_quality;
GRANT SELECT, INSERT, UPDATE ON ops.event_jobs, ops.attachment_jobs, ops.conversation_index, ops.backfill_jobs
    TO archive_ingest;
GRANT SELECT ON ops.event_jobs, ops.attachment_jobs, ops.conversation_index, ops.backfill_jobs
    TO archive_reader, archive_quality;
GRANT USAGE, SELECT ON SEQUENCE ops.backfill_jobs_id_seq TO archive_ingest;

COMMENT ON SCHEMA archive IS 'Append-only chat evidence. Runtime accounts may not update, delete or truncate.';
COMMENT ON SCHEMA ops IS 'Mutable processing state and query projections; grant access per table.';
COMMENT ON TABLE ops.schema_migrations IS 'Maintenance-only migration history; do not edit applied SQL files.';
COMMENT ON TABLE archive.message_snapshots IS 'Observed message versions, including original creation events and later changes.';
COMMENT ON TABLE archive.attachment_copies IS 'Evidence of successful copies; source links alone are not archived files.';

INSERT INTO ops.schema_migrations (version, checksum_sha256, description)
VALUES ('001_capture', :'migration_checksum', 'Append-only chat capture and operational state');
\endif
COMMIT;
