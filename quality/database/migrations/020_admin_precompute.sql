-- Admin statistics and archive filters read precomputed fields (R2, 2026-11-12).
-- See docs/R2-admin加载性能隐患排查-2026-11-12.md. Everything here is derived
-- data maintained by triggers or by the processor; dropping it loses nothing.
--
-- 1. quality.round_review_current: latest verdict per round (trigger on
--    round_reviews); quality.current_round_reviews now reads it.
-- 2. ops.round_fact_pending: rounds without stored facts. Every round has a
--    fact row or a pending row, so finding rounds to compute live no longer
--    anti-joins all rounds.
-- 3. FACT_VERSION 10: round_reply_hours.prev_hour_at (previous reply hour of the
--    round) lets any period count a round once without grouping all its hours;
--    rounds without a human reply have no consultation segments. Stored version
--    9 facts are converted in place (same rule, no recomputation).
-- 4. Day rollups: reply metrics are stored once per day plus "repeats" (rounds
--    counted on a day that had replied earlier), which any period subtracts;
--    the month/year variants are gone. R13 user rollups per day.
-- 5. ops.customer_index message_total / round_total / message_since and
--    ops.customer_round_seq ("the customer's N-th consultation").
-- 6. ops.presence_minutes / ops.presence_agent_days: presence samples per
--    scope-minute and per seat-day, mirrored by triggers.
-- 7. ops.contact_block_state: latest blacklist event per contact.
--
-- Takes table locks that block projection while it runs: stop the processor
-- first and run at a quiet time (see the release notes).
BEGIN;
SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(175607, 1);
SET LOCAL ROLE archive_owner;
SELECT EXISTS (SELECT 1 FROM ops.schema_migrations WHERE version = '020_admin_precompute') AS already_applied \gset
\if :already_applied
    SELECT checksum_sha256 = :'migration_checksum' AS checksum_matches
    FROM ops.schema_migrations WHERE version = '020_admin_precompute' \gset
    \if :checksum_matches
        \echo '020_admin_precompute already applied; unchanged migration skipped.'
    \else
        \echo 'ERROR: applied migration checksum differs; restore it and add a new migration.'
        \quit 3
    \endif
\else

-- Backfills below must see every row their triggers will later maintain.
LOCK TABLE quality.round_reviews, ops.round_index, ops.message_index, ops.round_service_facts,
    ops.round_reply_hours, ops.customer_index, archive.agent_presence_samples, archive.contact_block_events
    IN SHARE ROW EXCLUSIVE MODE;

-- ------------------------------------------------------------------ 1. current review
CREATE TABLE quality.round_review_current (
    round_id TEXT PRIMARY KEY,
    review_id BIGINT NOT NULL,
    status TEXT NOT NULL,
    remark TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    reviewed_at TIMESTAMPTZ NOT NULL,
    ai_status TEXT NOT NULL,
    human_status TEXT
);
CREATE INDEX round_review_current_ai_idx ON quality.round_review_current (ai_status);
CREATE INDEX round_review_current_human_idx ON quality.round_review_current (human_status);
INSERT INTO quality.round_review_current
SELECT DISTINCT ON (round_id) round_id, id, status, remark, reviewer, reviewed_at, ai_status, human_status
FROM quality.round_reviews ORDER BY round_id, id DESC;

CREATE FUNCTION quality.sync_round_review_current() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
BEGIN
    INSERT INTO quality.round_review_current AS c
        (round_id, review_id, status, remark, reviewer, reviewed_at, ai_status, human_status)
    SELECT DISTINCT ON (n.round_id) n.round_id, n.id, n.status, n.remark, n.reviewer, n.reviewed_at,
        n.ai_status, n.human_status
    FROM new_rows n ORDER BY n.round_id, n.id DESC
    ON CONFLICT (round_id) DO UPDATE SET review_id = EXCLUDED.review_id, status = EXCLUDED.status,
        remark = EXCLUDED.remark, reviewer = EXCLUDED.reviewer, reviewed_at = EXCLUDED.reviewed_at,
        ai_status = EXCLUDED.ai_status, human_status = EXCLUDED.human_status
    WHERE c.review_id < EXCLUDED.review_id;
    RETURN NULL;
END
$function$;
REVOKE ALL ON FUNCTION quality.sync_round_review_current() FROM PUBLIC;
CREATE TRIGGER round_reviews_current_sync AFTER INSERT ON quality.round_reviews
    REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION quality.sync_round_review_current();

CREATE OR REPLACE VIEW quality.current_round_reviews AS
    SELECT round_id, status, remark, reviewer, reviewed_at, review_id::text AS revision, ai_status, human_status
    FROM quality.round_review_current;

REVOKE ALL ON quality.round_review_current FROM PUBLIC, archive_ingest, archive_reader, archive_quality;
GRANT SELECT ON quality.round_review_current TO archive_reader, archive_quality, archive_ingest;
COMMENT ON TABLE quality.round_review_current IS '每轮当前质检结论（round_reviews 最新一条），由触发器维护。';

-- ------------------------------------------------------------------ 2. rounds without facts
CREATE TABLE ops.round_fact_pending (
    round_id TEXT PRIMARY KEY,
    queued_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO ops.round_fact_pending (round_id)
SELECT r.round_id FROM ops.round_index r
WHERE NOT EXISTS (SELECT 1 FROM ops.round_service_facts f WHERE f.round_id = r.round_id);

CREATE FUNCTION ops.fact_pending_queue() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
BEGIN
    IF TG_OP = 'TRUNCATE' THEN
        INSERT INTO ops.round_fact_pending (round_id) SELECT round_id FROM ops.round_index
        ON CONFLICT (round_id) DO NOTHING;
    ELSE
        INSERT INTO ops.round_fact_pending (round_id) SELECT round_id FROM changed_rows
        ON CONFLICT (round_id) DO NOTHING;
    END IF;
    RETURN NULL;
END
$function$;
CREATE FUNCTION ops.fact_pending_clear() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
BEGIN
    -- Facts stored, or the round itself deleted (its facts were removed and queued first).
    DELETE FROM ops.round_fact_pending p USING changed_rows c WHERE p.round_id = c.round_id
        AND (TG_TABLE_NAME = 'round_service_facts'
             OR NOT EXISTS (SELECT 1 FROM ops.round_index r WHERE r.round_id = c.round_id));
    RETURN NULL;
END
$function$;
REVOKE ALL ON FUNCTION ops.fact_pending_queue(), ops.fact_pending_clear() FROM PUBLIC;
CREATE TRIGGER round_index_fact_pending AFTER INSERT ON ops.round_index
    REFERENCING NEW TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION ops.fact_pending_queue();
CREATE TRIGGER round_service_facts_pending_delete AFTER DELETE ON ops.round_service_facts
    REFERENCING OLD TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION ops.fact_pending_queue();
CREATE TRIGGER round_service_facts_pending_truncate AFTER TRUNCATE ON ops.round_service_facts
    FOR EACH STATEMENT EXECUTE FUNCTION ops.fact_pending_queue();
CREATE TRIGGER round_service_facts_pending_insert AFTER INSERT ON ops.round_service_facts
    REFERENCING NEW TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION ops.fact_pending_clear();
-- Fires after round_index_facts_delete (trigger names run in order), which queued the deleted rounds.
CREATE TRIGGER round_index_zz_fact_pending_forget AFTER DELETE ON ops.round_index
    REFERENCING OLD TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION ops.fact_pending_clear();

REVOKE ALL ON ops.round_fact_pending FROM PUBLIC, archive_ingest, archive_reader, archive_quality;
GRANT SELECT, DELETE ON ops.round_fact_pending TO archive_ingest;
GRANT SELECT ON ops.round_fact_pending TO archive_reader, archive_quality;
COMMENT ON TABLE ops.round_fact_pending IS
    '尚无已存统计事实的轮次（新轮次、事实失效后待重算）。每个轮次要么有事实行，要么在此表。';

-- Stale stored facts: newer rounds than the reader's "now" and older rule versions.
CREATE INDEX round_service_facts_messages_until_idx ON ops.round_service_facts (messages_until);
CREATE INDEX round_service_facts_version_idx ON ops.round_service_facts (fact_version);

-- ------------------------------------------------------------------ 3. FACT_VERSION 10
ALTER TABLE ops.round_reply_hours ADD COLUMN prev_hour_at TIMESTAMPTZ;
COMMENT ON COLUMN ops.round_reply_hours.prev_hour_at IS
    '本轮上一个有人工回复的小时；区间 [A,B) 内首次计入该轮 = 本行小时在区间内且本列为空或早于 A。';
UPDATE ops.round_reply_hours h SET prev_hour_at = x.prev
FROM (SELECT round_id, hour_at, lag(hour_at) OVER (PARTITION BY round_id ORDER BY hour_at) AS prev
      FROM ops.round_reply_hours) x
WHERE h.round_id = x.round_id AND h.hour_at = x.hour_at AND x.prev IS NOT NULL;
DROP INDEX ops.round_reply_hours_window_idx;
CREATE INDEX round_reply_hours_window_idx ON ops.round_reply_hours (hour_at, round_id)
    INCLUDE (assessed_replies, prev_hour_at);

-- Rounds without a human reply (lost rounds) have no consultation time.
UPDATE ops.round_service_facts SET consultation_spans = '{}'::tstzmultirange
WHERE fact_version = 9 AND NOT human_replied AND consultation_spans <> '{}'::tstzmultirange;
UPDATE ops.round_service_facts SET fact_version = 10 WHERE fact_version = 9;
-- Segments may end after the last reply (customer messages, resolution): index their end.
CREATE INDEX round_service_facts_consultation_end_idx ON ops.round_service_facts (upper(consultation_spans));
COMMENT ON COLUMN ops.round_service_facts.consultation_spans IS
    '本轮咨询时段：转人工成功→解决，含之后任何人的有效消息，相邻间隔超过600秒切开；协助轮次、无 ICS 分配轮次与无人工回复轮次为空。';

-- ------------------------------------------------------------------ 4. day rollups
-- Reply metrics: the day row counts rounds first replied on the day; repeats are
-- those day rows of rounds with an earlier reply (prev_hour_at). A period
-- [A,B) of whole days = sum of day rows - repeats whose prev_hour_at >= A.
DELETE FROM ops.stat_seat_days WHERE variant <> 'd';
CREATE TABLE ops.stat_seat_repeats (
    day DATE NOT NULL,
    prev_hour_at TIMESTAMPTZ NOT NULL,
    source_key TEXT NOT NULL,
    account_id BIGINT NOT NULL,
    agent_id TEXT NOT NULL,
    agent_name TEXT,
    inquiries BIGINT NOT NULL DEFAULT 0,
    dialogues BIGINT NOT NULL DEFAULT 0,
    high_quality_dialogues BIGINT NOT NULL DEFAULT 0,
    rated_inquiries BIGINT NOT NULL DEFAULT 0,
    rating_sum BIGINT NOT NULL DEFAULT 0,
    low_rating BIGINT NOT NULL DEFAULT 0,
    failed_inquiries BIGINT NOT NULL DEFAULT 0,
    unassigned_inquiries BIGINT NOT NULL DEFAULT 0,
    assisted_inquiries BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (day, prev_hour_at, source_key, account_id, agent_id)
);
-- R13: successful ICS assignments per day, user (contact) and customer row.
CREATE TABLE ops.stat_user_days (
    day DATE NOT NULL,
    source_key TEXT NOT NULL,
    account_id BIGINT NOT NULL,
    channel_key TEXT NOT NULL,             -- '' = unmapped channel
    contact_id TEXT NOT NULL,              -- '' = contact unknown
    customer_id TEXT NOT NULL,
    assignments BIGINT NOT NULL,
    conversations BIGINT[] NOT NULL,
    first_assigned_at TIMESTAMPTZ NOT NULL,
    last_assigned_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (day, source_key, account_id, channel_key, contact_id, customer_id)
);
REVOKE ALL ON ops.stat_seat_repeats, ops.stat_user_days FROM PUBLIC, archive_ingest, archive_reader, archive_quality;
GRANT SELECT, INSERT, UPDATE, DELETE ON ops.stat_seat_repeats, ops.stat_user_days TO archive_ingest;
GRANT SELECT ON ops.stat_seat_repeats, ops.stat_user_days TO archive_reader, archive_quality;

-- A round moved to another customer changes the R13 rollup of its assignment day
-- (customer re-parenting does not invalidate facts).
CREATE FUNCTION ops.stat_round_customer_changed() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
BEGIN
    PERFORM ops.stat_mark_days(ARRAY(
        SELECT DISTINCT (ops.archive_ts(n.data->>'responsible_assigned_at') AT TIME ZONE 'Asia/Shanghai')::date
        FROM old_rows o JOIN new_rows n USING (round_id)
        WHERE o.customer_id IS DISTINCT FROM n.customer_id AND n.data->>'responsibility_basis' = 'ics_assignment'));
    RETURN NULL;
END
$function$;
REVOKE ALL ON FUNCTION ops.stat_round_customer_changed() FROM PUBLIC;
CREATE TRIGGER round_index_stat_customer AFTER UPDATE ON ops.round_index
    REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION ops.stat_round_customer_changed();

-- ------------------------------------------------------------------ 5. customer counts and sequence
ALTER TABLE ops.customer_index
    ADD COLUMN message_total INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN round_total INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN message_since TIMESTAMPTZ;
COMMENT ON COLUMN ops.customer_index.message_total IS '该客户在 message_index 中的消息数（触发器维护）。';
COMMENT ON COLUMN ops.customer_index.round_total IS '该客户的轮次数（触发器维护）。';
COMMENT ON COLUMN ops.customer_index.message_since IS
    '该客户最早一条消息的时间；有时间未知的消息时为 1970-01-01（视为任何锁定时刻之前都可见）；无消息为空。';
UPDATE ops.customer_index c SET message_total = m.n, message_since = m.since
FROM (SELECT customer_id, count(*)::integer AS n, min(COALESCE(sent_at, '1970-01-01 00:00:00+00'::timestamptz)) AS since
      FROM ops.message_index GROUP BY customer_id) m
WHERE m.customer_id = c.customer_id;
UPDATE ops.customer_index c SET round_total = r.n
FROM (SELECT customer_id, count(*)::integer AS n FROM ops.round_index GROUP BY customer_id) r
WHERE r.customer_id = c.customer_id;

CREATE TABLE ops.customer_round_seq (
    round_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    seq INTEGER NOT NULL CHECK (seq > 0)
);
CREATE INDEX customer_round_seq_customer_idx ON ops.customer_round_seq (customer_id, seq);
INSERT INTO ops.customer_round_seq (round_id, customer_id, seq)
SELECT round_id, customer_id, row_number() OVER (PARTITION BY customer_id ORDER BY started_at, round_id)
FROM ops.round_index;
COMMENT ON TABLE ops.customer_round_seq IS
    '用户第 N 次咨询：按客户全部轮次的开始时间排序（跨会话），由 round_index 触发器维护。';

-- Messages: counts are adjusted incrementally (projection upserts one message per statement).
CREATE FUNCTION ops.customer_message_since(p_customer_id TEXT) RETURNS TIMESTAMPTZ
LANGUAGE sql STABLE SET search_path = pg_catalog AS $function$
    SELECT CASE WHEN EXISTS (SELECT 1 FROM ops.message_index m WHERE m.customer_id = p_customer_id AND m.sent_at IS NULL)
        THEN '1970-01-01 00:00:00+00'::timestamptz
        ELSE (SELECT min(m.sent_at) FROM ops.message_index m WHERE m.customer_id = p_customer_id) END
$function$;
CREATE FUNCTION ops.customer_message_counts() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
BEGIN
    IF TG_OP = 'TRUNCATE' THEN
        UPDATE ops.customer_index SET message_total = 0, message_since = NULL
        WHERE message_total <> 0 OR message_since IS NOT NULL;
        RETURN NULL;
    END IF;
    IF TG_OP = 'INSERT' THEN
        UPDATE ops.customer_index c SET message_total = c.message_total + d.n,
            message_since = LEAST(c.message_since, d.since)
        FROM (SELECT customer_id, count(*)::integer AS n,
                  min(COALESCE(sent_at, '1970-01-01 00:00:00+00'::timestamptz)) AS since
              FROM new_rows GROUP BY customer_id) d
        WHERE c.customer_id = d.customer_id;
    ELSIF TG_OP = 'DELETE' THEN
        UPDATE ops.customer_index c SET message_total = c.message_total - d.n,
            message_since = ops.customer_message_since(c.customer_id)
        FROM (SELECT customer_id, count(*)::integer AS n FROM old_rows GROUP BY customer_id) d
        WHERE c.customer_id = d.customer_id;
    ELSE
        -- Only re-parented messages or changed times matter.
        UPDATE ops.customer_index c SET message_total = c.message_total + d.n,
            message_since = ops.customer_message_since(c.customer_id)
        FROM (
            SELECT customer_id, sum(n)::integer AS n FROM (
                SELECT n.customer_id, 1 AS n FROM old_rows o JOIN new_rows n USING (source_key, account_id, message_id)
                WHERE (o.customer_id, o.sent_at) IS DISTINCT FROM (n.customer_id, n.sent_at)
                UNION ALL
                SELECT o.customer_id, -1 FROM old_rows o JOIN new_rows n USING (source_key, account_id, message_id)
                WHERE (o.customer_id, o.sent_at) IS DISTINCT FROM (n.customer_id, n.sent_at)
            ) moved GROUP BY customer_id
        ) d
        WHERE c.customer_id = d.customer_id;
    END IF;
    RETURN NULL;
END
$function$;
-- Rounds: recount and renumber the affected customers (few rounds per statement).
CREATE FUNCTION ops.customer_round_refresh(p_customers TEXT[]) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
BEGIN
    IF p_customers IS NULL OR cardinality(p_customers) = 0 THEN
        RETURN;
    END IF;
    DELETE FROM ops.customer_round_seq s WHERE s.customer_id = ANY(p_customers)
        AND NOT EXISTS (SELECT 1 FROM ops.round_index r WHERE r.round_id = s.round_id AND r.customer_id = s.customer_id);
    INSERT INTO ops.customer_round_seq AS s (round_id, customer_id, seq)
    SELECT round_id, customer_id,
        row_number() OVER (PARTITION BY customer_id ORDER BY started_at, round_id)
    FROM ops.round_index WHERE customer_id = ANY(p_customers)
    ON CONFLICT (round_id) DO UPDATE SET customer_id = EXCLUDED.customer_id, seq = EXCLUDED.seq
    WHERE (s.customer_id, s.seq) IS DISTINCT FROM (EXCLUDED.customer_id, EXCLUDED.seq);
    UPDATE ops.customer_index c SET round_total = x.n
    FROM (SELECT k.customer_id,
              (SELECT count(*) FROM ops.round_index r WHERE r.customer_id = k.customer_id)::integer AS n
          FROM unnest(p_customers) AS k(customer_id)) x
    WHERE c.customer_id = x.customer_id AND c.round_total <> x.n;
END
$function$;
CREATE FUNCTION ops.customer_round_counts() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
BEGIN
    IF TG_OP = 'TRUNCATE' THEN
        DELETE FROM ops.customer_round_seq;
        UPDATE ops.customer_index SET round_total = 0 WHERE round_total <> 0;
    ELSIF TG_OP = 'INSERT' THEN
        PERFORM ops.customer_round_refresh(ARRAY(SELECT DISTINCT customer_id FROM new_rows));
    ELSIF TG_OP = 'DELETE' THEN
        PERFORM ops.customer_round_refresh(ARRAY(SELECT DISTINCT customer_id FROM old_rows));
    ELSE
        PERFORM ops.customer_round_refresh(ARRAY(
            SELECT DISTINCT x FROM old_rows o JOIN new_rows n USING (round_id)
                CROSS JOIN LATERAL (VALUES (o.customer_id), (n.customer_id)) AS v(x)
            WHERE (o.customer_id, o.started_at) IS DISTINCT FROM (n.customer_id, n.started_at)));
    END IF;
    RETURN NULL;
END
$function$;
REVOKE ALL ON FUNCTION ops.customer_message_since(TEXT), ops.customer_message_counts(),
    ops.customer_round_refresh(TEXT[]), ops.customer_round_counts() FROM PUBLIC;
CREATE TRIGGER message_index_customer_insert AFTER INSERT ON ops.message_index
    REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION ops.customer_message_counts();
CREATE TRIGGER message_index_customer_update AFTER UPDATE ON ops.message_index
    REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION ops.customer_message_counts();
CREATE TRIGGER message_index_customer_delete AFTER DELETE ON ops.message_index
    REFERENCING OLD TABLE AS old_rows FOR EACH STATEMENT EXECUTE FUNCTION ops.customer_message_counts();
CREATE TRIGGER message_index_customer_truncate AFTER TRUNCATE ON ops.message_index
    FOR EACH STATEMENT EXECUTE FUNCTION ops.customer_message_counts();
CREATE TRIGGER round_index_customer_insert AFTER INSERT ON ops.round_index
    REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION ops.customer_round_counts();
CREATE TRIGGER round_index_customer_update AFTER UPDATE ON ops.round_index
    REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION ops.customer_round_counts();
CREATE TRIGGER round_index_customer_delete AFTER DELETE ON ops.round_index
    REFERENCING OLD TABLE AS old_rows FOR EACH STATEMENT EXECUTE FUNCTION ops.customer_round_counts();
CREATE TRIGGER round_index_customer_truncate AFTER TRUNCATE ON ops.round_index
    FOR EACH STATEMENT EXECUTE FUNCTION ops.customer_round_counts();

REVOKE ALL ON ops.customer_round_seq FROM PUBLIC, archive_ingest, archive_reader, archive_quality;
GRANT SELECT ON ops.customer_round_seq TO archive_ingest, archive_reader, archive_quality;

-- ------------------------------------------------------------------ 6. presence rollups
CREATE TABLE ops.presence_minutes (
    source_key TEXT NOT NULL,
    account_id BIGINT NOT NULL,
    sampled_minute TIMESTAMPTZ NOT NULL,
    online INTEGER NOT NULL DEFAULT 0,
    busy INTEGER NOT NULL DEFAULT 0,
    offline INTEGER NOT NULL DEFAULT 0,
    unknown INTEGER NOT NULL DEFAULT 0,
    samples INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source_key, account_id, sampled_minute)
);
CREATE INDEX presence_minutes_time_idx ON ops.presence_minutes (sampled_minute);
CREATE TABLE ops.presence_agent_days (
    source_key TEXT NOT NULL,
    account_id BIGINT NOT NULL,
    agent_id TEXT NOT NULL,
    day DATE NOT NULL,                     -- Beijing day of sampled_minute
    agent_name TEXT,                       -- max(agent_name) of the day's samples
    online_minutes INTEGER NOT NULL DEFAULT 0,
    busy_minutes INTEGER NOT NULL DEFAULT 0,
    offline_minutes INTEGER NOT NULL DEFAULT 0,
    unknown_minutes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source_key, account_id, agent_id, day)
);
CREATE INDEX presence_agent_days_day_idx ON ops.presence_agent_days (day);
INSERT INTO ops.presence_minutes
SELECT source_key, account_id, sampled_minute,
    count(*) FILTER (WHERE availability_status = 'online'), count(*) FILTER (WHERE availability_status = 'busy'),
    count(*) FILTER (WHERE availability_status = 'offline'), count(*) FILTER (WHERE availability_status = 'unknown'),
    count(*)
FROM archive.agent_presence_samples GROUP BY source_key, account_id, sampled_minute;
INSERT INTO ops.presence_agent_days
SELECT source_key, account_id, agent_id, (sampled_minute AT TIME ZONE 'Asia/Shanghai')::date, max(agent_name),
    count(*) FILTER (WHERE availability_status = 'online'), count(*) FILTER (WHERE availability_status = 'busy'),
    count(*) FILTER (WHERE availability_status = 'offline'), count(*) FILTER (WHERE availability_status = 'unknown')
FROM archive.agent_presence_samples GROUP BY 1, 2, 3, 4;

CREATE FUNCTION ops.presence_rollup_sync() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
BEGIN
    IF TG_OP = 'TRUNCATE' THEN
        DELETE FROM ops.presence_minutes;
        DELETE FROM ops.presence_agent_days;
    ELSIF TG_OP = 'INSERT' THEN
        INSERT INTO ops.presence_minutes AS p
        SELECT source_key, account_id, sampled_minute,
            count(*) FILTER (WHERE availability_status = 'online'), count(*) FILTER (WHERE availability_status = 'busy'),
            count(*) FILTER (WHERE availability_status = 'offline'), count(*) FILTER (WHERE availability_status = 'unknown'),
            count(*)
        FROM new_rows GROUP BY source_key, account_id, sampled_minute
        ON CONFLICT (source_key, account_id, sampled_minute) DO UPDATE SET online = p.online + EXCLUDED.online,
            busy = p.busy + EXCLUDED.busy, offline = p.offline + EXCLUDED.offline,
            unknown = p.unknown + EXCLUDED.unknown, samples = p.samples + EXCLUDED.samples;
        INSERT INTO ops.presence_agent_days AS p
        SELECT source_key, account_id, agent_id, (sampled_minute AT TIME ZONE 'Asia/Shanghai')::date, max(agent_name),
            count(*) FILTER (WHERE availability_status = 'online'), count(*) FILTER (WHERE availability_status = 'busy'),
            count(*) FILTER (WHERE availability_status = 'offline'), count(*) FILTER (WHERE availability_status = 'unknown')
        FROM new_rows GROUP BY 1, 2, 3, 4
        ON CONFLICT (source_key, account_id, agent_id, day) DO UPDATE SET
            agent_name = GREATEST(p.agent_name, EXCLUDED.agent_name),
            online_minutes = p.online_minutes + EXCLUDED.online_minutes,
            busy_minutes = p.busy_minutes + EXCLUDED.busy_minutes,
            offline_minutes = p.offline_minutes + EXCLUDED.offline_minutes,
            unknown_minutes = p.unknown_minutes + EXCLUDED.unknown_minutes;
    ELSE
        UPDATE ops.presence_minutes p SET online = p.online - d.online, busy = p.busy - d.busy,
            offline = p.offline - d.offline, unknown = p.unknown - d.unknown, samples = p.samples - d.samples
        FROM (SELECT source_key, account_id, sampled_minute,
                  count(*) FILTER (WHERE availability_status = 'online') AS online,
                  count(*) FILTER (WHERE availability_status = 'busy') AS busy,
                  count(*) FILTER (WHERE availability_status = 'offline') AS offline,
                  count(*) FILTER (WHERE availability_status = 'unknown') AS unknown, count(*) AS samples
              FROM old_rows GROUP BY 1, 2, 3) d
        WHERE p.source_key = d.source_key AND p.account_id = d.account_id AND p.sampled_minute = d.sampled_minute;
        DELETE FROM ops.presence_minutes p USING (SELECT DISTINCT source_key, account_id, sampled_minute FROM old_rows) d
        WHERE p.source_key = d.source_key AND p.account_id = d.account_id AND p.sampled_minute = d.sampled_minute
            AND p.samples <= 0;
        UPDATE ops.presence_agent_days p SET online_minutes = p.online_minutes - d.online,
            busy_minutes = p.busy_minutes - d.busy, offline_minutes = p.offline_minutes - d.offline,
            unknown_minutes = p.unknown_minutes - d.unknown
        FROM (SELECT source_key, account_id, agent_id, (sampled_minute AT TIME ZONE 'Asia/Shanghai')::date AS day,
                  count(*) FILTER (WHERE availability_status = 'online') AS online,
                  count(*) FILTER (WHERE availability_status = 'busy') AS busy,
                  count(*) FILTER (WHERE availability_status = 'offline') AS offline,
                  count(*) FILTER (WHERE availability_status = 'unknown') AS unknown
              FROM old_rows GROUP BY 1, 2, 3, 4) d
        WHERE p.source_key = d.source_key AND p.account_id = d.account_id AND p.agent_id = d.agent_id AND p.day = d.day;
        DELETE FROM ops.presence_agent_days p
        WHERE (p.source_key, p.account_id, p.agent_id, p.day) IN (
                SELECT source_key, account_id, agent_id, (sampled_minute AT TIME ZONE 'Asia/Shanghai')::date FROM old_rows)
            AND p.online_minutes + p.busy_minutes + p.offline_minutes + p.unknown_minutes <= 0;
    END IF;
    RETURN NULL;
END
$function$;
REVOKE ALL ON FUNCTION ops.presence_rollup_sync() FROM PUBLIC;
CREATE TRIGGER agent_presence_samples_rollup_insert AFTER INSERT ON archive.agent_presence_samples
    REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION ops.presence_rollup_sync();
CREATE TRIGGER agent_presence_samples_rollup_delete AFTER DELETE ON archive.agent_presence_samples
    REFERENCING OLD TABLE AS old_rows FOR EACH STATEMENT EXECUTE FUNCTION ops.presence_rollup_sync();
CREATE TRIGGER agent_presence_samples_rollup_truncate AFTER TRUNCATE ON archive.agent_presence_samples
    FOR EACH STATEMENT EXECUTE FUNCTION ops.presence_rollup_sync();
REVOKE ALL ON ops.presence_minutes, ops.presence_agent_days
    FROM PUBLIC, archive_ingest, archive_reader, archive_quality, archive_presence;
GRANT SELECT ON ops.presence_minutes, ops.presence_agent_days TO archive_reader, archive_quality, archive_presence;

-- ------------------------------------------------------------------ 7. blacklist state
CREATE TABLE ops.contact_block_state (
    source_key TEXT NOT NULL,
    account_id BIGINT NOT NULL,
    contact_id TEXT NOT NULL,
    source_seq BIGINT NOT NULL,
    action TEXT NOT NULL,
    permanent BOOLEAN NOT NULL,
    blocked_until TIMESTAMPTZ,
    PRIMARY KEY (source_key, account_id, contact_id)
);
INSERT INTO ops.contact_block_state
SELECT DISTINCT ON (source_key, account_id, contact_id) source_key, account_id, contact_id, source_seq, action,
    permanent, blocked_until
FROM archive.contact_block_events ORDER BY source_key, account_id, contact_id, source_seq DESC;
CREATE FUNCTION ops.contact_block_state_sync() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
BEGIN
    INSERT INTO ops.contact_block_state AS s
    SELECT DISTINCT ON (source_key, account_id, contact_id) source_key, account_id, contact_id, source_seq, action,
        permanent, blocked_until
    FROM new_rows ORDER BY source_key, account_id, contact_id, source_seq DESC
    ON CONFLICT (source_key, account_id, contact_id) DO UPDATE SET source_seq = EXCLUDED.source_seq,
        action = EXCLUDED.action, permanent = EXCLUDED.permanent, blocked_until = EXCLUDED.blocked_until
    WHERE s.source_seq < EXCLUDED.source_seq;
    RETURN NULL;
END
$function$;
REVOKE ALL ON FUNCTION ops.contact_block_state_sync() FROM PUBLIC;
CREATE TRIGGER contact_block_events_state AFTER INSERT ON archive.contact_block_events
    REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION ops.contact_block_state_sync();
REVOKE ALL ON ops.contact_block_state FROM PUBLIC, archive_ingest, archive_reader, archive_quality;
GRANT SELECT ON ops.contact_block_state TO archive_reader, archive_quality, archive_assignment;
COMMENT ON TABLE ops.contact_block_state IS '每个联系人最新一条拉黑事件（触发器维护）；是否仍在拉黑中按 now() 判断。';

ANALYZE quality.round_review_current;
ANALYZE ops.round_fact_pending;
ANALYZE ops.round_reply_hours;
ANALYZE ops.customer_index;
ANALYZE ops.customer_round_seq;
ANALYZE ops.presence_minutes;
ANALYZE ops.presence_agent_days;
ANALYZE ops.contact_block_state;

INSERT INTO ops.schema_migrations (version, checksum_sha256, description)
VALUES ('020_admin_precompute', :'migration_checksum',
    'Precomputed fields for admin statistics and archive filters (R2)');
\endif
COMMIT;
