-- Day rollups ("L3") of the statistics metrics (2026-11-10).
-- One row per Beijing day and seat / channel, computed by the processor from the
-- per-round facts (migration 015) with the metric registry
-- quality/processing/stat_metrics.py, so they are the same numbers the reports
-- would compute live. Metrics counted once per round per period (reply clock)
-- are stored in three variants: d = the day itself, m = rounds whose first reply
-- of the month falls on this day, y = the same for the year; a month or year is
-- then the sum of its days. Reports use a day only while it is built under the
-- current rule versions and not marked dirty; any change to a built day's facts
-- or reviews marks it dirty in the same transaction, and the processor rebuilds it.
BEGIN;
SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(175607, 1);
SET LOCAL ROLE archive_owner;
SELECT EXISTS (SELECT 1 FROM ops.schema_migrations WHERE version = '016_stat_day_rollups') AS already_applied \gset
\if :already_applied
    SELECT checksum_sha256 = :'migration_checksum' AS checksum_matches
    FROM ops.schema_migrations WHERE version = '016_stat_day_rollups' \gset
    \if :checksum_matches
        \echo '016_stat_day_rollups already applied; unchanged migration skipped.'
    \else
        \echo 'ERROR: applied migration checksum differs; restore it and add a new migration.'
        \quit 3
    \endif
\else

CREATE TABLE ops.stat_rollup_days (
    day DATE PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('building', 'built')),
    fact_version SMALLINT NOT NULL,
    rollup_version SMALLINT NOT NULL,
    -- Snapshot time of the build; reports evaluated earlier than this compute live.
    built_at TIMESTAMPTZ
);
CREATE TABLE ops.stat_dirty_days (
    day DATE PRIMARY KEY,
    dirtied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE ops.stat_seat_days (
    day DATE NOT NULL,
    variant CHAR(1) NOT NULL CHECK (variant IN ('d', 'm', 'y')),
    source_key TEXT NOT NULL,
    account_id BIGINT NOT NULL,
    agent_id TEXT NOT NULL,                -- '' = rounds without an assessed / assigned seat
    agent_name TEXT,
    -- reply clock, per variant
    inquiries BIGINT NOT NULL DEFAULT 0,
    dialogues BIGINT NOT NULL DEFAULT 0,
    high_quality_dialogues BIGINT NOT NULL DEFAULT 0,
    rated_inquiries BIGINT NOT NULL DEFAULT 0,
    rating_sum BIGINT NOT NULL DEFAULT 0,
    low_rating BIGINT NOT NULL DEFAULT 0,
    failed_inquiries BIGINT NOT NULL DEFAULT 0,
    unassigned_inquiries BIGINT NOT NULL DEFAULT 0,
    assisted_inquiries BIGINT NOT NULL DEFAULT 0,
    -- additive clocks, only on variant d
    replies BIGINT NOT NULL DEFAULT 0,
    assigned_inquiries BIGINT NOT NULL DEFAULT 0,
    lost_inquiries BIGINT NOT NULL DEFAULT 0,
    first_response_count BIGINT NOT NULL DEFAULT 0,
    first_response_sum DOUBLE PRECISION NOT NULL DEFAULT 0,
    average_response_count BIGINT NOT NULL DEFAULT 0,
    average_response_sum DOUBLE PRECISION NOT NULL DEFAULT 0,
    response_rounds BIGINT NOT NULL DEFAULT 0,
    response_samples BIGINT NOT NULL DEFAULT 0,
    consultation_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
    PRIMARY KEY (day, variant, source_key, account_id, agent_id)
);
CREATE TABLE ops.stat_round_days (
    day DATE NOT NULL,
    source_key TEXT NOT NULL,
    account_id BIGINT NOT NULL,
    channel_key TEXT NOT NULL,             -- '' = unmapped channel
    incoming BIGINT NOT NULL DEFAULT 0,
    ai_only BIGINT NOT NULL DEFAULT 0,
    handoff BIGINT NOT NULL DEFAULT 0,
    rejected BIGINT NOT NULL DEFAULT 0,
    effective_human BIGINT NOT NULL DEFAULT 0,
    low_rating_rounds BIGINT NOT NULL DEFAULT 0,
    ai_replied_only BIGINT NOT NULL DEFAULT 0,
    ai_failed BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (day, source_key, account_id, channel_key)
);
REVOKE ALL ON ops.stat_rollup_days, ops.stat_dirty_days, ops.stat_seat_days, ops.stat_round_days
    FROM PUBLIC, archive_ingest, archive_reader, archive_quality;
GRANT SELECT, INSERT, UPDATE, DELETE ON ops.stat_rollup_days, ops.stat_dirty_days, ops.stat_seat_days,
    ops.stat_round_days TO archive_ingest;
GRANT SELECT ON ops.stat_rollup_days, ops.stat_dirty_days, ops.stat_seat_days, ops.stat_round_days
    TO archive_reader, archive_quality;

-- The rollup builder (processor) reads the latest review verdicts.
GRANT USAGE ON SCHEMA quality TO archive_ingest;
GRANT SELECT ON quality.round_reviews TO archive_ingest;

-- Mark built days dirty. A shared advisory lock per day is held until the
-- writer commits; the builder takes the exclusive lock while it registers a day
-- as "building", so a change is either visible to the build or marks the day.
CREATE FUNCTION ops.stat_mark_days(days DATE[]) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
DECLARE
    d DATE;
BEGIN
    FOR d IN SELECT DISTINCT x FROM unnest(days) AS x WHERE x IS NOT NULL ORDER BY x LOOP
        PERFORM pg_advisory_xact_lock_shared(175608, d - DATE '2000-01-01');
    END LOOP;
    INSERT INTO ops.stat_dirty_days (day)
    SELECT DISTINCT x FROM unnest(days) AS x
    WHERE x IS NOT NULL AND EXISTS (SELECT 1 FROM ops.stat_rollup_days r WHERE r.day = x)
    ON CONFLICT (day) DO UPDATE SET dirtied_at = clock_timestamp();
END
$function$;

-- Every Beijing day a fact row contributes to under any clock.
CREATE FUNCTION ops.stat_fact_days(f ops.round_service_facts) RETURNS DATE[]
LANGUAGE sql STABLE SET search_path = pg_catalog AS $function$
    SELECT array_agg(DISTINCT x) FROM (
        SELECT (f.started_at AT TIME ZONE 'Asia/Shanghai')::date
        UNION ALL SELECT (f.handoff_at AT TIME ZONE 'Asia/Shanghai')::date
        UNION ALL SELECT (f.assigned_at AT TIME ZONE 'Asia/Shanghai')::date
        UNION ALL SELECT generate_series((f.first_reply_at AT TIME ZONE 'Asia/Shanghai')::date,
            (f.last_reply_at AT TIME ZONE 'Asia/Shanghai')::date, interval '1 day')::date
        UNION ALL SELECT generate_series((lower(s) AT TIME ZONE 'Asia/Shanghai')::date,
            (upper(s) AT TIME ZONE 'Asia/Shanghai')::date, interval '1 day')::date
            FROM unnest(COALESCE(f.consultation_spans, '{}'::tstzmultirange)) AS s
    ) days(x) WHERE x IS NOT NULL
$function$;

CREATE FUNCTION ops.stat_facts_changed() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
BEGIN
    PERFORM ops.stat_mark_days(ARRAY(SELECT DISTINCT unnest(ops.stat_fact_days(c)) FROM changed_rows c));
    RETURN NULL;
END
$function$;
CREATE FUNCTION ops.stat_reviews_changed() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
BEGIN
    PERFORM ops.stat_mark_days(ARRAY(SELECT DISTINCT unnest(ops.stat_fact_days(f))
        FROM ops.round_service_facts f WHERE f.round_id IN (SELECT round_id FROM changed_rows)));
    RETURN NULL;
END
$function$;
REVOKE ALL ON FUNCTION ops.stat_mark_days(DATE[]), ops.stat_fact_days(ops.round_service_facts),
    ops.stat_facts_changed(), ops.stat_reviews_changed() FROM PUBLIC;

CREATE TRIGGER round_service_facts_stat_insert AFTER INSERT ON ops.round_service_facts
    REFERENCING NEW TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION ops.stat_facts_changed();
CREATE TRIGGER round_service_facts_stat_delete AFTER DELETE ON ops.round_service_facts
    REFERENCING OLD TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION ops.stat_facts_changed();
CREATE TRIGGER round_reviews_stat_insert AFTER INSERT ON quality.round_reviews
    REFERENCING NEW TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION ops.stat_reviews_changed();

INSERT INTO ops.schema_migrations (version, checksum_sha256, description)
VALUES ('016_stat_day_rollups', :'migration_checksum',
    'Day rollups of the statistics metrics with dirty-day tracking');
\endif
COMMIT;
