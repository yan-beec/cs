-- Applied migrations are immutable. Query projections are rebuildable; evidence is not.
BEGIN;
SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(175607, 1);
SET LOCAL ROLE archive_owner;
SELECT EXISTS (SELECT 1 FROM ops.schema_migrations WHERE version = '002_query') AS already_applied \gset
\if :already_applied
    SELECT checksum_sha256 = :'migration_checksum' AS checksum_matches
    FROM ops.schema_migrations WHERE version = '002_query' \gset
    \if :checksum_matches
        \echo '002_query already applied; unchanged migration skipped.'
    \else
        \echo 'ERROR: applied migration checksum differs; restore it and add a new migration.'
        \quit 3
    \endif
\else

CREATE TABLE ops.customer_index (
    customer_id TEXT PRIMARY KEY,
    source_key TEXT NOT NULL,
    account_id BIGINT NOT NULL CHECK (account_id > 0),
    contact_id TEXT,
    business_user_id TEXT,
    display_name TEXT,
    last_message_at TIMESTAMPTZ,
    data JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(data) = 'object'),
    UNIQUE (customer_id, source_key, account_id)
);
CREATE INDEX customer_index_contact_idx ON ops.customer_index (source_key, account_id, contact_id);
CREATE INDEX customer_index_time_idx ON ops.customer_index (last_message_at DESC, customer_id);

CREATE TABLE ops.identity_map (
    source_key TEXT NOT NULL,
    account_id BIGINT NOT NULL CHECK (account_id > 0),
    identity_kind TEXT NOT NULL CHECK (identity_kind IN ('contact', 'verified_business')),
    identity_value TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    evidence_event_id BIGINT NOT NULL,
    PRIMARY KEY (source_key, account_id, identity_kind, identity_value),
    FOREIGN KEY (customer_id, source_key, account_id)
        REFERENCES ops.customer_index (customer_id, source_key, account_id),
    FOREIGN KEY (evidence_event_id, source_key, account_id)
        REFERENCES archive.events (id, source_key, account_id)
);

CREATE TABLE ops.round_index (
    round_id TEXT PRIMARY KEY,
    source_key TEXT NOT NULL,
    account_id BIGINT NOT NULL CHECK (account_id > 0),
    conversation_id BIGINT NOT NULL CHECK (conversation_id > 0),
    customer_id TEXT NOT NULL,
    round_no INTEGER NOT NULL CHECK (round_no > 0),
    inbox_id BIGINT CHECK (inbox_id > 0),
    channel_key TEXT,
    started_at TIMESTAMPTZ NOT NULL,
    last_message_at TIMESTAMPTZ NOT NULL,
    reception_type TEXT NOT NULL CHECK (reception_type IN ('ai_only', 'human', 'unknown')),
    reply_composition TEXT NOT NULL CHECK (reply_composition IN ('ai_only', 'agent_only', 'mixed', 'none')),
    status TEXT NOT NULL CHECK (status IN ('active', 'resolved', 'idle_boundary', 'unknown')),
    rating_status TEXT NOT NULL CHECK (rating_status IN ('not_invited', 'pending', 'rated', 'unlinked')),
    rating SMALLINT CHECK (rating BETWEEN 1 AND 5),
    data JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(data) = 'object'),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (round_id, source_key, account_id, conversation_id),
    UNIQUE (source_key, account_id, conversation_id, round_no),
    FOREIGN KEY (customer_id, source_key, account_id)
        REFERENCES ops.customer_index (customer_id, source_key, account_id)
);
CREATE INDEX round_index_customer_idx ON ops.round_index (customer_id, started_at DESC, round_id);
CREATE INDEX round_index_filter_idx ON ops.round_index (channel_key, reception_type, started_at DESC);
CREATE INDEX round_index_conversation_idx ON ops.round_index (source_key, account_id, conversation_id);

CREATE TABLE ops.message_index (
    source_key TEXT NOT NULL,
    account_id BIGINT NOT NULL CHECK (account_id > 0),
    message_id BIGINT NOT NULL CHECK (message_id > 0),
    conversation_id BIGINT NOT NULL CHECK (conversation_id > 0),
    customer_id TEXT NOT NULL,
    round_id TEXT,
    sent_at TIMESTAMPTZ,
    sender_id TEXT,
    sender_name TEXT,
    sender_role TEXT NOT NULL CHECK (sender_role IN ('contact','agent','bot','system','unknown')),
    direction TEXT NOT NULL CHECK (direction IN ('incoming','outgoing','activity','unknown')),
    is_private BOOLEAN NOT NULL,
    content TEXT,
    original_snapshot_id BIGINT NOT NULL REFERENCES archive.message_snapshots (id),
    latest_snapshot_id BIGINT NOT NULL REFERENCES archive.message_snapshots (id),
    is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
    data JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(data) = 'object'),
    PRIMARY KEY (source_key, account_id, message_id),
    FOREIGN KEY (customer_id, source_key, account_id)
        REFERENCES ops.customer_index (customer_id, source_key, account_id),
    FOREIGN KEY (round_id, source_key, account_id, conversation_id)
        REFERENCES ops.round_index (round_id, source_key, account_id, conversation_id)
);
CREATE INDEX message_index_round_idx ON ops.message_index (round_id, sent_at, message_id);
CREATE INDEX message_index_customer_idx ON ops.message_index (customer_id, sent_at DESC, message_id);

CREATE TABLE ops.round_agent_index (
    round_id TEXT NOT NULL REFERENCES ops.round_index (round_id),
    agent_id TEXT NOT NULL,
    agent_name TEXT,
    was_assigned BOOLEAN NOT NULL DEFAULT FALSE,
    replied BOOLEAN NOT NULL DEFAULT FALSE,
    noted BOOLEAN NOT NULL DEFAULT FALSE,
    reply_count INTEGER NOT NULL DEFAULT 0 CHECK (reply_count >= 0),
    first_reply_at TIMESTAMPTZ,
    last_reply_at TIMESTAMPTZ,
    PRIMARY KEY (round_id, agent_id)
);
CREATE INDEX round_agent_lookup_idx ON ops.round_agent_index (agent_id, round_id);

CREATE TABLE archive.rating_snapshots (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id BIGINT NOT NULL,
    source_key TEXT NOT NULL,
    account_id BIGINT NOT NULL,
    conversation_id BIGINT NOT NULL CHECK (conversation_id > 0),
    invitation_message_id BIGINT,
    external_round_id TEXT,
    closed_at TIMESTAMPTZ,
    rating SMALLINT CHECK (rating BETWEEN 1 AND 5),
    feedback TEXT,
    source_time TIMESTAMPTZ,
    data JSONB NOT NULL CHECK (jsonb_typeof(data) = 'object'),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (event_id),
    FOREIGN KEY (event_id, source_key, account_id)
        REFERENCES archive.events (id, source_key, account_id)
);
CREATE TABLE ops.rating_index (
    source_key TEXT NOT NULL,
    account_id BIGINT NOT NULL,
    conversation_id BIGINT NOT NULL,
    rating_key TEXT NOT NULL,
    round_id TEXT,
    invitation_message_id BIGINT,
    external_round_id TEXT,
    rating SMALLINT CHECK (rating BETWEEN 1 AND 5),
    feedback TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending','rated','unlinked')),
    snapshot_id BIGINT NOT NULL REFERENCES archive.rating_snapshots (id),
    data JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(data) = 'object'),
    PRIMARY KEY (source_key, account_id, conversation_id, rating_key),
    FOREIGN KEY (round_id, source_key, account_id, conversation_id)
        REFERENCES ops.round_index (round_id, source_key, account_id, conversation_id)
);
CREATE INDEX rating_index_round_idx ON ops.rating_index (round_id);

REVOKE ALL ON archive.rating_snapshots FROM PUBLIC, archive_ingest, archive_reader, archive_quality;
REVOKE ALL ON SEQUENCE archive.rating_snapshots_id_seq FROM PUBLIC, archive_ingest, archive_reader, archive_quality;
GRANT SELECT, INSERT ON archive.rating_snapshots TO archive_ingest;
GRANT SELECT ON archive.rating_snapshots TO archive_reader, archive_quality;
GRANT USAGE, SELECT ON SEQUENCE archive.rating_snapshots_id_seq TO archive_ingest;
GRANT SELECT, INSERT, UPDATE ON ops.customer_index, ops.identity_map TO archive_ingest;
-- DELETE applies only to derived query projections, permitting atomic replay.
GRANT SELECT, INSERT, UPDATE, DELETE ON ops.round_index, ops.message_index,
    ops.round_agent_index, ops.rating_index TO archive_ingest;
GRANT SELECT ON ops.customer_index, ops.identity_map, ops.round_index,
    ops.message_index, ops.round_agent_index, ops.rating_index TO archive_reader, archive_quality;

COMMENT ON TABLE ops.round_index IS 'Rebuildable consultation rounds. Internal IDs are independent of ICS session UUIDs.';
COMMENT ON TABLE archive.rating_snapshots IS 'Immutable observed invitations and ratings; unresolved linkage stays explicit.';
INSERT INTO ops.schema_migrations (version, checksum_sha256, description)
VALUES ('002_query', :'migration_checksum', 'Customer, consultation round and immutable rating query foundations');
\endif
COMMIT;
