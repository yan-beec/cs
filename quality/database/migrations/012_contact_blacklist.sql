-- Append-only ICS contact blacklist history (block / change duration / unblock / expire).
-- Written by the restricted archive_assignment role; archive queries read it for the blacklist filter.
BEGIN;
SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(175607, 1);
SET LOCAL ROLE archive_owner;
SELECT EXISTS (SELECT 1 FROM ops.schema_migrations WHERE version = '012_contact_blacklist') AS already_applied \gset
\if :already_applied
    SELECT checksum_sha256 = :'migration_checksum' AS checksum_matches
    FROM ops.schema_migrations WHERE version = '012_contact_blacklist' \gset
    \if :checksum_matches
        \echo '012_contact_blacklist already applied; unchanged migration skipped.'
    \else
        \echo 'ERROR: applied migration checksum differs; restore it and add a new migration.'
        \quit 3
    \endif
\else

CREATE TABLE archive.contact_block_events (
    id BIGSERIAL PRIMARY KEY,
    source_key TEXT NOT NULL CHECK (btrim(source_key) <> ''),
    account_id BIGINT NOT NULL CHECK (account_id > 0),
    event_key TEXT NOT NULL CHECK (btrim(event_key) <> ''),
    -- ICS 历史表自增编号，同一联系人内按它排序得到最新状态。
    source_seq BIGINT NOT NULL CHECK (source_seq > 0),
    contact_id TEXT NOT NULL CHECK (contact_id ~ '^[1-9][0-9]*$'),
    conversation_id BIGINT CHECK (conversation_id > 0),
    contact_name TEXT,
    contact_identifier TEXT,
    action TEXT NOT NULL CHECK (action IN ('block', 'change', 'unblock', 'expire')),
    source TEXT NOT NULL CHECK (source IN ('client', 'chatwoot', 'system')),
    duration_code TEXT CHECK (duration_code IN ('24h', '72h', 'permanent')),
    blocked_until TIMESTAMPTZ,
    permanent BOOLEAN NOT NULL DEFAULT FALSE,
    previous_blocked_until TIMESTAMPTZ,
    previous_permanent BOOLEAN,
    reason_code TEXT,
    reason_label TEXT,
    remark TEXT NOT NULL DEFAULT '',
    operator_id TEXT,
    operator_name TEXT,
    occurred_at TIMESTAMPTZ NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    UNIQUE (source_key, account_id, event_key)
);
CREATE INDEX contact_block_events_contact_idx
    ON archive.contact_block_events (source_key, account_id, contact_id, source_seq DESC);
CREATE INDEX contact_block_events_time_idx
    ON archive.contact_block_events (occurred_at DESC, id DESC);

COMMENT ON TABLE archive.contact_block_events IS
    'ICS 坐席黑名单操作历史（拉黑原因、时长、解除备注、操作人）；只追加，不修改。';

-- 归档 schema 的默认权限会给聊天采集账号追加权限；黑名单历史只由 ICS 同步账号写入。
REVOKE ALL ON archive.contact_block_events FROM PUBLIC, archive_ingest, archive_reader, archive_quality;
REVOKE ALL ON SEQUENCE archive.contact_block_events_id_seq FROM PUBLIC, archive_ingest, archive_reader, archive_quality;
GRANT SELECT, INSERT ON archive.contact_block_events TO archive_assignment;
GRANT USAGE, SELECT ON SEQUENCE archive.contact_block_events_id_seq TO archive_assignment;
GRANT SELECT ON archive.contact_block_events TO archive_reader, archive_quality;

INSERT INTO ops.schema_migrations (version, checksum_sha256, description)
VALUES ('012_contact_blacklist', :'migration_checksum', 'Append-only ICS contact blacklist history for archive filters');
\endif
COMMIT;
