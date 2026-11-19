-- One statistics definition for reports and the archive (2026-11-10).
-- Every per-round judgement that reports and archive filters share is stored
-- once per round (ops.round_service_facts, "L1"), and every hour in which a
-- round received a public human reply once per round and hour
-- (ops.round_reply_hours, "L2"). Rules live only in
-- quality/processing/service_facts.py (FACT_VERSION 7); the processor writes
-- these rows while it projects a conversation, older rows are refilled in idle
-- time and any round without current rows is computed live by the same SQL.
-- Additions are nullable columns and a new table: no rewrite of existing rows.
BEGIN;
SET LOCAL lock_timeout = '10s';
SET LOCAL maintenance_work_mem = '256MB';
SELECT pg_advisory_xact_lock(175607, 1);
SET LOCAL ROLE archive_owner;
SELECT EXISTS (SELECT 1 FROM ops.schema_migrations WHERE version = '015_aligned_round_facts') AS already_applied \gset
\if :already_applied
    SELECT checksum_sha256 = :'migration_checksum' AS checksum_matches
    FROM ops.schema_migrations WHERE version = '015_aligned_round_facts' \gset
    \if :checksum_matches
        \echo '015_aligned_round_facts already applied; unchanged migration skipped.'
    \else
        \echo 'ERROR: applied migration checksum differs; restore it and add a new migration.'
        \quit 3
    \endif
\else

ALTER TABLE ops.round_service_facts
    ADD COLUMN started_at TIMESTAMPTZ,
    ADD COLUMN channel_key TEXT,
    ADD COLUMN inbox_id BIGINT,
    ADD COLUMN reception_type TEXT,
    ADD COLUMN assigned_agent_id TEXT,
    ADD COLUMN assigned_agent_name TEXT,
    ADD COLUMN assigned_at TIMESTAMPTZ,
    ADD COLUMN assisted BOOLEAN,
    ADD COLUMN responding_seats INTEGER,
    ADD COLUMN first_reply_at TIMESTAMPTZ,
    ADD COLUMN last_reply_at TIMESTAMPTZ,
    ADD COLUMN is_lost BOOLEAN,
    ADD COLUMN is_handoff BOOLEAN,
    ADD COLUMN is_ai_only BOOLEAN,
    ADD COLUMN rating SMALLINT,
    ADD COLUMN first_response_seconds DOUBLE PRECISION,
    ADD COLUMN average_response_seconds DOUBLE PRECISION,
    ADD COLUMN response_samples INTEGER;

COMMENT ON COLUMN ops.round_service_facts.responsible_agent_id IS
    '考核坐席：ICS 已确认分配的坐席；协助轮次（其他坐席转人工后有效公开回复）或无 ICS 分配时为空。';
COMMENT ON COLUMN ops.round_service_facts.assigned_agent_id IS 'ICS 已确认分配的坐席（发配量、流单、咨询时长的归属）。';
COMMENT ON COLUMN ops.round_service_facts.consultation_spans IS
    '本轮咨询活跃段（转人工后有效消息，间隔超过10分钟切段，每段止于 ICS 分配坐席最后一条回复）。';

-- Time clocks of the reports and archive drill-downs.
CREATE INDEX round_service_facts_started_idx ON ops.round_service_facts (started_at);
CREATE INDEX round_service_facts_handoff_idx ON ops.round_service_facts (handoff_at);
CREATE INDEX round_service_facts_assigned_idx ON ops.round_service_facts (assigned_at);
-- Consultation spans and replies end no later than the last reply.
CREATE INDEX round_service_facts_last_reply_idx ON ops.round_service_facts (last_reply_at);
CREATE INDEX round_service_facts_seat_idx ON ops.round_service_facts (source_key, account_id, responsible_agent_id);

-- L2: one row per round and Beijing/UTC hour (both whole-hour offsets) with at
-- least one valid public human reply. Report periods always start on a whole
-- hour, so "rounds replied in the period" equals "rounds with a row in it".
CREATE TABLE ops.round_reply_hours (
    round_id TEXT NOT NULL REFERENCES ops.round_service_facts (round_id) ON DELETE CASCADE,
    hour_at TIMESTAMPTZ NOT NULL,
    any_replies INTEGER NOT NULL CHECK (any_replies > 0),
    -- Replies of the round's assessed seat in this hour (0 without one).
    assessed_replies INTEGER NOT NULL CHECK (assessed_replies >= 0),
    PRIMARY KEY (round_id, hour_at)
);
CREATE INDEX round_reply_hours_window_idx ON ops.round_reply_hours (hour_at, round_id) INCLUDE (assessed_replies);
REVOKE ALL ON ops.round_reply_hours FROM PUBLIC, archive_ingest, archive_reader, archive_quality;
GRANT SELECT, INSERT, UPDATE, DELETE ON ops.round_reply_hours TO archive_ingest;
GRANT SELECT ON ops.round_reply_hours TO archive_reader, archive_quality;

-- Facts now also depend on the round's scalar columns (channel, inbox,
-- reception type, rating): any change to them invalidates the round as well.
CREATE OR REPLACE FUNCTION ops.invalidate_facts_for_rounds() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        DELETE FROM ops.round_service_facts f USING old_rows o JOIN new_rows n USING (round_id)
        WHERE f.round_id = o.round_id
            AND (o.data, o.started_at, o.channel_key, o.inbox_id, o.reception_type, o.rating_status, o.rating,
                 o.source_key, o.account_id)
                IS DISTINCT FROM (n.data, n.started_at, n.channel_key, n.inbox_id, n.reception_type, n.rating_status,
                 n.rating, n.source_key, n.account_id);
    ELSE
        DELETE FROM ops.round_service_facts f USING old_rows o WHERE f.round_id = o.round_id;
    END IF;
    RETURN NULL;
END
$function$;

-- Response timings are now columns of the assessed seat's round row.
DROP TABLE ops.round_seat_timings;

INSERT INTO ops.schema_migrations (version, checksum_sha256, description)
VALUES ('015_aligned_round_facts', :'migration_checksum',
    'Per-round statistics facts and reply hours shared by reports and archive filters');
\endif
COMMIT;
