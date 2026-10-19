-- Database-clock retention and durable daily cleanup limits. Never edit 005.
BEGIN;
SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(175607, 1);
SELECT EXISTS (SELECT 1 FROM ops.schema_migrations WHERE version = '006_presence_retention_state') AS already_applied \gset
\if :already_applied
    SELECT checksum_sha256 = :'migration_checksum' AS checksum_matches
    FROM ops.schema_migrations WHERE version = '006_presence_retention_state' \gset
    \if :checksum_matches
        \echo '006_presence_retention_state already applied; unchanged migration skipped.'
    \else
        \echo 'ERROR: applied migration checksum differs; restore it and add a new migration.'
        \quit 3
    \endif
\else
SET LOCAL ROLE archive_owner;

CREATE TABLE ops.agent_presence_cleanup_state (
    source_key TEXT NOT NULL CHECK (length(source_key) BETWEEN 1 AND 128 AND source_key=btrim(source_key)),
    account_id BIGINT NOT NULL CHECK (account_id>0),
    cleanup_day DATE NOT NULL,
    cutoff_at TIMESTAMPTZ NOT NULL,
    batches INTEGER NOT NULL DEFAULT 0 CHECK (batches BETWEEN 0 AND 200),
    deleted_samples INTEGER NOT NULL DEFAULT 0 CHECK (deleted_samples BETWEEN 0 AND 400000),
    deleted_periods INTEGER NOT NULL DEFAULT 0 CHECK (deleted_periods BETWEEN 0 AND 400000),
    status TEXT NOT NULL CHECK (status IN ('pending','completed','limited')),
    last_success_at TIMESTAMPTZ,
    PRIMARY KEY (source_key,account_id)
);
REVOKE ALL ON ops.agent_presence_cleanup_state
    FROM PUBLIC,archive_ingest,archive_reader,archive_quality,archive_assignment,archive_presence;
GRANT SELECT ON ops.agent_presence_cleanup_state TO archive_reader;

CREATE OR REPLACE FUNCTION archive.reject_presence_sample_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF TG_OP = 'DELETE' AND current_user = 'archive_owner'
        AND OLD.sampled_at < CURRENT_TIMESTAMP - INTERVAL '1080 hours' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'Presence samples are append-only until their actual observation is over 45 days old'
        USING ERRCODE = '55000';
END $$;

CREATE FUNCTION ops.run_presence_cleanup(p_source_key TEXT, p_account_id BIGINT)
RETURNS TABLE(deleted_samples INTEGER, deleted_periods INTEGER, cleanup_status TEXT,
    cleanup_day DATE, cutoff_at TIMESTAMPTZ, batches INTEGER,
    total_deleted_samples INTEGER, total_deleted_periods INTEGER, last_success_at TIMESTAMPTZ)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    local_now TIMESTAMP := CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai';
    today DATE := local_now::date;
    retention_cutoff TIMESTAMPTZ :=
        ((local_now::date + TIME '04:00') - INTERVAL '45 days') AT TIME ZONE 'Asia/Shanghai';
    saved ops.agent_presence_cleanup_state%ROWTYPE;
    sample_count INTEGER := 0;
    period_count INTEGER := 0;
BEGIN
    IF p_source_key IS NULL OR length(p_source_key) NOT BETWEEN 1 AND 128
        OR p_source_key <> btrim(p_source_key) OR p_account_id IS NULL OR p_account_id <= 0 THEN
        RAISE EXCEPTION 'Presence cleanup requires a valid source and account' USING ERRCODE = '22023';
    END IF;
    -- The database decides when cleanup is allowed, even for direct calls.
    IF local_now::time < TIME '04:00' THEN
        RETURN QUERY SELECT 0,0,'waiting'::text,today,retention_cutoff,0,0,0,NULL::timestamptz;
        RETURN;
    END IF;
    IF NOT pg_try_advisory_xact_lock(175608, hashtext(p_source_key || ':' || p_account_id::text)) THEN
        RAISE EXCEPTION 'Presence cleanup is already running for this scope' USING ERRCODE = '55P03';
    END IF;
    INSERT INTO ops.agent_presence_cleanup_state AS s
        (source_key,account_id,cleanup_day,cutoff_at,status)
    VALUES(p_source_key,p_account_id,today,retention_cutoff,'pending')
    ON CONFLICT (source_key,account_id) DO UPDATE SET
        cleanup_day=EXCLUDED.cleanup_day,cutoff_at=EXCLUDED.cutoff_at,batches=0,
        deleted_samples=0,deleted_periods=0,status='pending'
    WHERE s.cleanup_day < EXCLUDED.cleanup_day;
    SELECT s.* INTO STRICT saved FROM ops.agent_presence_cleanup_state s
        WHERE s.source_key=p_source_key AND s.account_id=p_account_id FOR UPDATE;

    -- A restart or second process cannot reset the committed daily budget.
    IF saved.cleanup_day=today AND saved.status='pending' AND saved.batches<200 THEN
        WITH expired AS (
            SELECT s.source_key,s.account_id,s.agent_id,s.sampled_minute
            FROM archive.agent_presence_samples s
            WHERE s.source_key=p_source_key AND s.account_id=p_account_id
                AND s.sampled_minute<saved.cutoff_at AND s.sampled_at<saved.cutoff_at
            ORDER BY s.sampled_minute,s.agent_id LIMIT 2000 FOR UPDATE
        )
        DELETE FROM archive.agent_presence_samples s USING expired e
        WHERE s.source_key=e.source_key AND s.account_id=e.account_id
            AND s.agent_id=e.agent_id AND s.sampled_minute=e.sampled_minute;
        GET DIAGNOSTICS sample_count = ROW_COUNT;

        WITH expired AS (
            SELECT p.source_key,p.account_id,p.agent_id,p.started_minute
            FROM ops.agent_presence_periods p
            WHERE p.source_key=p_source_key AND p.account_id=p_account_id
                AND p.ended_minute<=saved.cutoff_at
            ORDER BY p.ended_minute,p.agent_id,p.started_minute LIMIT 2000 FOR UPDATE
        )
        DELETE FROM ops.agent_presence_periods p USING expired e
        WHERE p.source_key=e.source_key AND p.account_id=e.account_id
            AND p.agent_id=e.agent_id AND p.started_minute=e.started_minute;
        GET DIAGNOSTICS period_count = ROW_COUNT;

        UPDATE ops.agent_presence_cleanup_state AS s SET
            batches=s.batches+1,deleted_samples=s.deleted_samples+sample_count,
            deleted_periods=s.deleted_periods+period_count,last_success_at=CURRENT_TIMESTAMP,
            status=CASE WHEN sample_count<2000 AND period_count<2000 THEN 'completed'
                        WHEN s.batches+1>=200 THEN 'limited' ELSE 'pending' END
        WHERE s.source_key=p_source_key AND s.account_id=p_account_id RETURNING s.* INTO saved;
    END IF;
    RETURN QUERY SELECT sample_count,period_count,saved.status,saved.cleanup_day,saved.cutoff_at,
        saved.batches,saved.deleted_samples,saved.deleted_periods,saved.last_success_at;
END $$;
REVOKE ALL ON FUNCTION ops.run_presence_cleanup(TEXT,BIGINT)
    FROM PUBLIC,archive_ingest,archive_reader,archive_quality,archive_assignment,archive_presence;
GRANT EXECUTE ON FUNCTION ops.run_presence_cleanup(TEXT,BIGINT) TO archive_presence;

-- Keep the old signature for older applications, without a budget bypass.
CREATE OR REPLACE FUNCTION ops.cleanup_presence_history(p_source_key TEXT, p_account_id BIGINT)
RETURNS TABLE(deleted_samples INTEGER, deleted_periods INTEGER)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    RETURN QUERY SELECT r.deleted_samples,r.deleted_periods
        FROM ops.run_presence_cleanup(p_source_key,p_account_id) r;
END $$;
COMMENT ON TABLE ops.agent_presence_cleanup_state IS
    'One durable cleanup ledger per source/account: database Beijing day, 04:00 minus 45 days cutoff, at most 200 committed batches per day.';
COMMENT ON FUNCTION ops.run_presence_cleanup(TEXT,BIGINT) IS
    'After database-clock 04:00, expire all observations older than that day 04:00 minus 45 days; 2000 rows per table per batch, durable daily cap 200, atomic progress and deletion.';
COMMENT ON FUNCTION ops.cleanup_presence_history(TEXT,BIGINT) IS
    'Compatibility wrapper for run_presence_cleanup; uses the same durable daily budget.';
COMMENT ON TABLE archive.agent_presence_samples IS
    'Successful Chatwoot agent observations, immutable for at least 45 elapsed days from sampled_at; controlled daily expiration only, absence is not offline.';
INSERT INTO ops.schema_migrations(version,checksum_sha256,description)
VALUES('006_presence_retention_state',:'migration_checksum','Actual-observation 45-day retention with database-clock 04:00 scheduling and durable bounded daily cleanup');
\endif
COMMIT;
