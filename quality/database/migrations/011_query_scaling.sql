-- Index-backed time windows for archive queries and statistics; projections only, evidence untouched.
-- Building indexes holds SHARE locks: archive reads continue, the processor waits and retries.
BEGIN;
SET LOCAL lock_timeout = '10s';
SET LOCAL maintenance_work_mem = '256MB';
SELECT pg_advisory_xact_lock(175607, 1);
SET LOCAL ROLE archive_owner;
SELECT EXISTS (SELECT 1 FROM ops.schema_migrations WHERE version = '011_query_scaling') AS already_applied \gset
\if :already_applied
    SELECT checksum_sha256 = :'migration_checksum' AS checksum_matches
    FROM ops.schema_migrations WHERE version = '011_query_scaling' \gset
    \if :checksum_matches
        \echo '011_query_scaling already applied; unchanged migration skipped.'
    \else
        \echo 'ERROR: applied migration checksum differs; restore it and add a new migration.'
        \quit 3
    \endif
\else

-- Projection timestamps are ISO 8601 strings written by the processor with an
-- explicit UTC offset. Parsing them never depends on the session TimeZone, so
-- the conversion is deterministic and may back expression indexes. An offset-
-- less value (never produced by the processor) is read as UTC for the same reason.
CREATE FUNCTION ops.archive_ts(value TEXT) RETURNS TIMESTAMPTZ
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
AS $function$
    SELECT CASE
        WHEN value = '' THEN NULL
        WHEN value ~ '(Z|z|[+-][0-9]{2}(:?[0-9]{2}){0,2})$' THEN value::timestamptz
        ELSE value::timestamp AT TIME ZONE 'UTC'
    END
$function$;

-- The statistics transfer clock: confirmed ICS assignment, else the observed
-- request, else an inherited human takeover. Identical to the former inline SQL.
CREATE FUNCTION ops.round_handoff_at(data JSONB, started_at TIMESTAMPTZ) RETURNS TIMESTAMPTZ
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $function$
    SELECT COALESCE(
        CASE WHEN data->>'responsibility_basis' = 'ics_assignment'
            AND ops.archive_ts(data->>'responsible_assigned_at') >= started_at
            THEN ops.archive_ts(data->>'responsible_assigned_at') END,
        CASE WHEN data->>'responsibility_basis' IS DISTINCT FROM 'ics_assignment'
            AND ops.archive_ts(data->>'handoff_requested_at') >= started_at
            THEN ops.archive_ts(data->>'handoff_requested_at') END,
        CASE WHEN data->>'takeover_inherited' = 'true' THEN started_at END)
$function$;

REVOKE ALL ON FUNCTION ops.archive_ts(TEXT), ops.round_handoff_at(JSONB, TIMESTAMPTZ) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ops.archive_ts(TEXT), ops.round_handoff_at(JSONB, TIMESTAMPTZ)
    TO archive_ingest, archive_reader, archive_quality;

-- Consultation rounds by time: reports, overview and newest-first lists.
CREATE INDEX round_index_started_idx ON ops.round_index (started_at, round_id);
CREATE INDEX round_index_last_message_idx ON ops.round_index (last_message_at);
CREATE INDEX round_index_handoff_idx ON ops.round_index (ops.round_handoff_at(data, started_at))
    WHERE ops.round_handoff_at(data, started_at) IS NOT NULL;
CREATE INDEX round_index_assigned_idx ON ops.round_index (ops.archive_ts(data->>'responsible_assigned_at'))
    WHERE data->>'responsibility_basis' = 'ics_assignment';
CREATE INDEX round_index_responsible_idx ON ops.round_index
    (source_key, account_id, (data->>'responsible_agent_id'), started_at DESC, round_id DESC)
    WHERE data->>'responsibility_basis' = 'ics_assignment';
CREATE INDEX round_index_resolved_idx ON ops.round_index
    (ops.archive_ts(COALESCE(data->>'resolved_at', data->>'closed_at')))
    WHERE COALESCE(data->>'resolved_at', data->>'closed_at') IS NOT NULL;
CREATE INDEX round_index_rating_at_idx ON ops.round_index (ops.archive_ts(data->>'rating_at'))
    WHERE data->>'rating_at' IS NOT NULL;
CREATE INDEX round_index_inbox_idx ON ops.round_index (source_key, account_id, inbox_id, channel_key)
    WHERE inbox_id IS NOT NULL;
-- Filter options read distinct inboxes by skipping through these indexes.
CREATE INDEX conversation_index_inbox_idx ON ops.conversation_index (source_key, account_id, inbox_id, channel_key)
    WHERE inbox_id IS NOT NULL;

-- Messages by time; public human replies by seat and time.
CREATE INDEX message_index_sent_idx ON ops.message_index (sent_at, message_id);
CREATE INDEX message_index_agent_reply_idx ON ops.message_index (sender_id, sent_at)
    WHERE sender_role = 'agent' AND direction = 'outgoing' AND NOT is_private;
-- Role, direction and privacy are strongly correlated; without combined
-- statistics the planner multiplies them and underestimates reply sets.
CREATE STATISTICS ops.message_index_role_stats (mcv, dependencies)
    ON sender_role, direction, is_private FROM ops.message_index;

-- Customer list order is DESC NULLS LAST, which the older index cannot serve.
CREATE INDEX customer_index_recent_idx ON ops.customer_index (last_message_at DESC NULLS LAST, customer_id);

-- Small working sets inside large, append-mostly tables.
CREATE INDEX rating_index_unlinked_idx ON ops.rating_index (snapshot_id) WHERE status = 'unlinked';
CREATE INDEX event_jobs_unfinished_idx ON ops.event_jobs (status)
    WHERE status IN ('queued', 'retry', 'processing', 'failed');
CREATE INDEX event_jobs_completed_idx ON ops.event_jobs (completed_at) WHERE completed_at IS NOT NULL;

-- Per-round service facts, derived by the processor from one round's messages
-- with exactly the statistics SQL (quality/processing/service_facts.py).
-- Reports aggregate these rows instead of re-sorting every message of a long
-- period. Rebuildable: a missing row or an older fact_version is recomputed
-- live by reports and backfilled by the processor in the background.
CREATE TABLE ops.round_service_facts (
    round_id TEXT PRIMARY KEY,
    source_key TEXT NOT NULL,
    account_id BIGINT NOT NULL,
    handoff_at TIMESTAMPTZ,
    visitor_messages INTEGER NOT NULL CHECK (visitor_messages >= 0),
    -- Round flags used by time and AI quality reports.
    busy_refused BOOLEAN NOT NULL,
    human_replied BOOLEAN NOT NULL,
    ai_replied BOOLEAN NOT NULL,
    -- Latest message time in the round when computed. Reports evaluated at an
    -- earlier "now" than this fall back to the live rule for the round.
    messages_until TIMESTAMPTZ,
    fact_version SMALLINT NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- One row per seat with a valid public reply after the transfer; timing
-- columns stay NULL/0 when that seat completed no capped response.
CREATE TABLE ops.round_seat_timings (
    round_id TEXT NOT NULL REFERENCES ops.round_service_facts (round_id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL,
    source_key TEXT NOT NULL,
    account_id BIGINT NOT NULL,
    handoff_at TIMESTAMPTZ NOT NULL,
    sender_name TEXT,
    first_response_seconds DOUBLE PRECISION,
    average_response_seconds DOUBLE PRECISION,
    response_samples INTEGER NOT NULL DEFAULT 0 CHECK (response_samples >= 0),
    PRIMARY KEY (round_id, agent_id)
);
CREATE INDEX round_seat_timings_window_idx ON ops.round_seat_timings (handoff_at, source_key, account_id, agent_id);
CREATE INDEX round_seat_timings_seat_idx ON ops.round_seat_timings (source_key, account_id, agent_id, handoff_at);

REVOKE ALL ON ops.round_service_facts, ops.round_seat_timings FROM PUBLIC,
    archive_ingest, archive_reader, archive_quality;
GRANT SELECT, INSERT, UPDATE, DELETE ON ops.round_service_facts, ops.round_seat_timings TO archive_ingest;
GRANT SELECT ON ops.round_service_facts, ops.round_seat_timings TO archive_reader, archive_quality;

-- Stored facts are trusted only while they match their inputs: any change to a
-- round's projection or to one of its messages removes that round's facts in
-- the same statement. Reports then compute the round live and the processor
-- (or backfill) stores fresh facts. Rewrites with identical values (upserts of
-- unchanged rows) and customer re-parenting do not invalidate anything.
CREATE FUNCTION ops.invalidate_facts_for_rounds() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        DELETE FROM ops.round_service_facts f USING old_rows o JOIN new_rows n USING (round_id)
        WHERE f.round_id = o.round_id
            AND (o.data, o.started_at) IS DISTINCT FROM (n.data, n.started_at);
    ELSE
        DELETE FROM ops.round_service_facts f USING old_rows o WHERE f.round_id = o.round_id;
    END IF;
    RETURN NULL;
END
$function$;
CREATE FUNCTION ops.invalidate_facts_for_messages() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
BEGIN
    IF TG_OP = 'INSERT' THEN
        DELETE FROM ops.round_service_facts f USING new_rows n WHERE f.round_id = n.round_id;
    ELSIF TG_OP = 'DELETE' THEN
        DELETE FROM ops.round_service_facts f USING old_rows o WHERE f.round_id = o.round_id;
    ELSE
        DELETE FROM ops.round_service_facts f USING (
            SELECT o.round_id AS old_round, n.round_id AS new_round
            FROM old_rows o JOIN new_rows n USING (source_key, account_id, message_id)
            WHERE (o.round_id, o.sent_at, o.sender_id, o.sender_name, o.sender_role, o.direction, o.is_private,
                   o.content, o.is_deleted, o.data, o.original_snapshot_id)
                IS DISTINCT FROM (n.round_id, n.sent_at, n.sender_id, n.sender_name, n.sender_role, n.direction,
                   n.is_private, n.content, n.is_deleted, n.data, n.original_snapshot_id)
        ) changed WHERE f.round_id IN (changed.old_round, changed.new_round);
    END IF;
    RETURN NULL;
END
$function$;
REVOKE ALL ON FUNCTION ops.invalidate_facts_for_rounds(), ops.invalidate_facts_for_messages() FROM PUBLIC;
CREATE TRIGGER round_index_facts_update AFTER UPDATE ON ops.round_index
    REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION ops.invalidate_facts_for_rounds();
CREATE TRIGGER round_index_facts_delete AFTER DELETE ON ops.round_index
    REFERENCING OLD TABLE AS old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION ops.invalidate_facts_for_rounds();
CREATE TRIGGER message_index_facts_insert AFTER INSERT ON ops.message_index
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION ops.invalidate_facts_for_messages();
CREATE TRIGGER message_index_facts_update AFTER UPDATE ON ops.message_index
    REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION ops.invalidate_facts_for_messages();
CREATE TRIGGER message_index_facts_delete AFTER DELETE ON ops.message_index
    REFERENCING OLD TABLE AS old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION ops.invalidate_facts_for_messages();

ANALYZE ops.round_index;
ANALYZE ops.message_index;
ANALYZE ops.customer_index;
ANALYZE ops.conversation_index;
ANALYZE ops.rating_index;
ANALYZE ops.event_jobs;

INSERT INTO ops.schema_migrations (version, checksum_sha256, description)
VALUES ('011_query_scaling', :'migration_checksum',
    'Index-backed time windows for archive queries, statistics reports and status counts');
\endif
COMMIT;
