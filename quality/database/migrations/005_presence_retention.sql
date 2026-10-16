-- Expire only presence history; original chat evidence remains append-only.
BEGIN;
SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(175607, 1);
SELECT EXISTS (SELECT 1 FROM ops.schema_migrations WHERE version = '005_presence_retention') AS already_applied \gset
\if :already_applied
    SELECT checksum_sha256 = :'migration_checksum' AS checksum_matches
    FROM ops.schema_migrations WHERE version = '005_presence_retention' \gset
    \if :checksum_matches
        \echo '005_presence_retention already applied; unchanged migration skipped.'
    \else
        \echo 'ERROR: applied migration checksum differs; restore it and add a new migration.'
        \quit 3
    \endif
\else
SET LOCAL ROLE archive_owner;

-- A normal presence connection still cannot DELETE, UPDATE or TRUNCATE samples.
-- The owner may expire old rows, including when called through the restricted
-- maintenance function below. No caller-controlled setting enables this path.
CREATE OR REPLACE FUNCTION archive.reject_presence_sample_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF TG_OP = 'DELETE' AND current_user = 'archive_owner' THEN
        IF OLD.sampled_minute <
            (((CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date - 44)::timestamp AT TIME ZONE 'Asia/Shanghai') THEN
            RETURN OLD;
        END IF;
    END IF;
    RAISE EXCEPTION 'Presence samples are append-only within their 45-day retention window'
        USING ERRCODE = '55000';
END $$;

CREATE INDEX agent_presence_periods_retention_idx
    ON ops.agent_presence_periods(source_key,account_id,ended_minute,agent_id,started_minute)
    WHERE ended_minute IS NOT NULL;

CREATE FUNCTION ops.cleanup_presence_history(p_source_key TEXT, p_account_id BIGINT)
RETURNS TABLE(deleted_samples INTEGER, deleted_periods INTEGER)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    retention_cutoff TIMESTAMPTZ :=
        (((CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date - 44)::timestamp AT TIME ZONE 'Asia/Shanghai');
BEGIN
    IF p_source_key IS NULL OR length(p_source_key) NOT BETWEEN 1 AND 128
        OR p_source_key <> btrim(p_source_key) OR p_account_id IS NULL OR p_account_id <= 0 THEN
        RAISE EXCEPTION 'Presence cleanup requires a valid source and account' USING ERRCODE = '22023';
    END IF;
    deleted_samples := 0;
    deleted_periods := 0;
    -- A colliding hash can only postpone cleanup; it never broadens its scope.
    IF NOT pg_try_advisory_xact_lock(175608, hashtext(p_source_key || ':' || p_account_id::text)) THEN
        RAISE EXCEPTION 'Presence cleanup is already running for this scope' USING ERRCODE = '55P03';
    END IF;

    WITH expired AS (
        SELECT s.source_key,s.account_id,s.agent_id,s.sampled_minute
        FROM archive.agent_presence_samples s
        WHERE s.source_key=p_source_key AND s.account_id=p_account_id
            AND s.sampled_minute<retention_cutoff
        ORDER BY s.sampled_minute,s.agent_id
        LIMIT 2000 FOR UPDATE
    )
    DELETE FROM archive.agent_presence_samples s USING expired e
    WHERE s.source_key=e.source_key AND s.account_id=e.account_id
        AND s.agent_id=e.agent_id AND s.sampled_minute=e.sampled_minute;
    GET DIAGNOSTICS deleted_samples = ROW_COUNT;

    -- Intervals are [start,end): end=cutoff cannot describe retained minutes.
    -- Open intervals and intervals spanning the boundary remain intact.
    WITH expired AS (
        SELECT p.source_key,p.account_id,p.agent_id,p.started_minute
        FROM ops.agent_presence_periods p
        WHERE p.source_key=p_source_key AND p.account_id=p_account_id
            AND p.ended_minute<=retention_cutoff
        ORDER BY p.ended_minute,p.agent_id,p.started_minute
        LIMIT 2000 FOR UPDATE
    )
    DELETE FROM ops.agent_presence_periods p USING expired e
    WHERE p.source_key=e.source_key AND p.account_id=e.account_id
        AND p.agent_id=e.agent_id AND p.started_minute=e.started_minute;
    GET DIAGNOSTICS deleted_periods = ROW_COUNT;
    RETURN NEXT;
END $$;
REVOKE ALL ON FUNCTION ops.cleanup_presence_history(TEXT,BIGINT)
    FROM PUBLIC,archive_ingest,archive_reader,archive_quality,archive_assignment,archive_presence;
GRANT EXECUTE ON FUNCTION ops.cleanup_presence_history(TEXT,BIGINT) TO archive_presence;

COMMENT ON FUNCTION ops.cleanup_presence_history(TEXT,BIGINT) IS
    'Bounded expiration of presence history only: retain today and previous 44 Beijing dates, at most 2000 samples and 2000 closed periods per call.';
COMMENT ON TABLE archive.agent_presence_samples IS
    'Successful Chatwoot agent observations, immutable until controlled expiration beyond 45 Beijing calendar dates; absence is not offline.';
INSERT INTO ops.schema_migrations(version,checksum_sha256,description)
VALUES('005_presence_retention',:'migration_checksum','Bounded 45-Beijing-calendar-day presence retention without runtime table deletion privileges');
\endif
COMMIT;
