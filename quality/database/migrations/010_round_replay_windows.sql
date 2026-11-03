-- Bounded replay work and disposable locators. Original evidence stays append-only.
BEGIN;
SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(175607, 1);
SET LOCAL ROLE archive_owner;
SELECT EXISTS (SELECT 1 FROM ops.schema_migrations WHERE version = '010_round_replay_windows') AS already_applied \gset
\if :already_applied
    SELECT checksum_sha256 = :'migration_checksum' AS checksum_matches
    FROM ops.schema_migrations WHERE version = '010_round_replay_windows' \gset
    \if :checksum_matches
        \echo '010_round_replay_windows already applied; unchanged migration skipped.'
    \else
        \echo 'ERROR: applied migration checksum differs; restore it and add a new migration.'
        \quit 3
    \endif
\else
CREATE TABLE ops.event_replay_index (
    event_id BIGINT PRIMARY KEY,
    source_key TEXT NOT NULL,
    account_id BIGINT NOT NULL,
    conversation_id BIGINT NOT NULL,
    event_type TEXT NOT NULL,
    event_at TIMESTAMPTZ NOT NULL,
    state_at TIMESTAMPTZ NOT NULL,
    message_id BIGINT,
    sent_at TIMESTAMPTZ,
    anchor_message_id BIGINT,
    closed_at TIMESTAMPTZ,
    assignment_started_at TIMESTAMPTZ,
    contact_id TEXT,
    is_resolution BOOLEAN NOT NULL,
    payload_bytes BIGINT NOT NULL CHECK (payload_bytes >= 0),
    data JSONB NOT NULL CHECK (jsonb_typeof(data) = 'object'),
    FOREIGN KEY (event_id,source_key,account_id)
        REFERENCES archive.events(id,source_key,account_id)
);
CREATE INDEX event_replay_scope_idx ON ops.event_replay_index
    (source_key,account_id,conversation_id,event_id);
CREATE INDEX event_replay_message_idx ON ops.event_replay_index
    (source_key,account_id,conversation_id,message_id,event_at,event_id);
CREATE INDEX event_replay_state_idx ON ops.event_replay_index
    (source_key,account_id,conversation_id,state_at) WHERE is_resolution;

CREATE TABLE ops.replay_message_index (
    source_key TEXT NOT NULL,
    account_id BIGINT NOT NULL,
    conversation_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    sent_at TIMESTAMPTZ,
    creation_event_id BIGINT,
    creation_at TIMESTAMPTZ,
    is_customer_creation BOOLEAN NOT NULL,
    PRIMARY KEY(source_key,account_id,conversation_id,message_id)
);
CREATE INDEX replay_message_time_idx ON ops.replay_message_index
    (source_key,account_id,conversation_id,sent_at,message_id);

CREATE TABLE ops.replay_round_index (
    source_key TEXT NOT NULL,
    account_id BIGINT NOT NULL,
    conversation_id BIGINT NOT NULL,
    customer_id TEXT NOT NULL,
    round_id TEXT NOT NULL,
    round_no INTEGER NOT NULL CHECK(round_no > 0),
    first_message_id BIGINT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(source_key,account_id,conversation_id,round_id),
    UNIQUE(source_key,account_id,conversation_id,round_no)
);
CREATE INDEX replay_round_customer_idx ON ops.replay_round_index
    (customer_id,started_at DESC,round_id);

CREATE TABLE ops.conversation_replay_state (
    source_key TEXT NOT NULL,
    account_id BIGINT NOT NULL,
    conversation_id BIGINT NOT NULL,
    plan JSONB NOT NULL DEFAULT '{}' CHECK(jsonb_typeof(plan) = 'object'),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(source_key,account_id,conversation_id)
);

-- A late creation can insert a round between existing rounds. Renumber the
-- retained suffix atomically without temporarily violating ordinal uniqueness.
ALTER TABLE ops.round_index DROP CONSTRAINT round_index_source_key_account_id_conversation_id_round_no_key;
ALTER TABLE ops.round_index ADD CONSTRAINT round_index_source_key_account_id_conversation_id_round_no_key
    UNIQUE(source_key,account_id,conversation_id,round_no) DEFERRABLE INITIALLY DEFERRED;

REVOKE ALL ON ops.event_replay_index,ops.replay_message_index,
    ops.replay_round_index,ops.conversation_replay_state FROM PUBLIC,
    archive_ingest,archive_reader,archive_quality;
GRANT SELECT,INSERT,UPDATE,DELETE ON ops.event_replay_index,ops.replay_message_index,
    ops.replay_round_index,ops.conversation_replay_state TO archive_ingest;
GRANT SELECT ON ops.event_replay_index,ops.replay_message_index,
    ops.replay_round_index,ops.conversation_replay_state TO archive_reader,archive_quality;
INSERT INTO ops.schema_migrations(version,checksum_sha256,description)
VALUES('010_round_replay_windows',:'migration_checksum',
    'Recent-round replay windows, bounded bootstrap, durable progress and retained historical projections');
\endif
COMMIT;
