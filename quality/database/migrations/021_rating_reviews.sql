-- 评分复核保留原始内容，独立保存只追加历史与当前结论。
BEGIN;
SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(175607, 1);
SET LOCAL ROLE archive_owner;
SELECT EXISTS (SELECT 1 FROM ops.schema_migrations WHERE version='021_rating_reviews') AS already_applied \gset
\if :already_applied
    SELECT checksum_sha256=:'migration_checksum' AS checksum_matches
    FROM ops.schema_migrations WHERE version='021_rating_reviews' \gset
    \if :checksum_matches
        \echo '021_rating_reviews already applied; unchanged migration skipped.'
    \else
        \echo 'ERROR: applied migration checksum differs.'
        \quit 3
    \endif
\else
CREATE TABLE quality.round_rating_reviews (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    round_id TEXT NOT NULL, source_key TEXT NOT NULL, account_id BIGINT NOT NULL,
    conversation_id BIGINT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('valid','invalid')),
    reviewer TEXT NOT NULL, reviewed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    content JSONB NOT NULL
);
CREATE INDEX round_rating_reviews_round_idx ON quality.round_rating_reviews(round_id,id DESC);
CREATE TABLE quality.round_rating_review_current (
    round_id TEXT PRIMARY KEY, review_id BIGINT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('valid','invalid')),
    reviewer TEXT NOT NULL, reviewed_at TIMESTAMPTZ NOT NULL
);
-- 不关联投影外键，重建及重复同步不会删除已保存结论。
CREATE FUNCTION quality.rating_review_content(r ops.round_index) RETURNS JSONB
LANGUAGE sql STABLE SET search_path=pg_catalog AS $function$
    SELECT jsonb_build_object('round_id',r.round_id,'rating_status',r.rating_status,
        'rating',r.rating,'rating_feedback',r.data->>'rating_feedback',
        'selected_rating',jsonb_build_object(
            'invitation_message_id',r.data->'invitation_message_id',
            'external_round_id',r.data->'external_round_id',
            'rating_key',CASE WHEN NULLIF(r.data->>'invitation_message_id','') IS NOT NULL
                THEN 'message:' || (r.data->>'invitation_message_id')
                WHEN NULLIF(r.data->>'external_round_id','') IS NOT NULL
                THEN 'round:' || (r.data->>'external_round_id')
                ELSE (SELECT ri.rating_key FROM ops.rating_index ri
                    WHERE ri.round_id=r.round_id AND ri.source_key=r.source_key
                        AND ri.account_id=r.account_id AND ri.conversation_id=r.conversation_id
                        AND ri.invitation_message_id IS NULL AND ri.external_round_id IS NULL
                        AND ri.rating IS NOT DISTINCT FROM r.rating
                        AND ri.feedback IS NOT DISTINCT FROM (r.data->>'rating_feedback')
                        AND ops.archive_ts(ri.data->>'source_time') IS NOT DISTINCT FROM
                            ops.archive_ts(r.data->>'rating_at')
                    ORDER BY ri.snapshot_id DESC LIMIT 1) END))
$function$;
CREATE FUNCTION quality.get_rating_review(p_round_id TEXT) RETURNS JSONB
LANGUAGE sql STABLE SET search_path=pg_catalog AS $function$
    SELECT quality.rating_review_content(r) || jsonb_build_object(
        'content_revision',md5(quality.rating_review_content(r)::text),
        'status',COALESCE(c.status,'unmarked'),'revision',COALESCE(c.review_id,0)::text,
        'reviewer',c.reviewer,'reviewed_at',c.reviewed_at)
    FROM ops.round_index r LEFT JOIN quality.round_rating_review_current c USING(round_id)
    WHERE r.round_id=p_round_id
$function$;
CREATE FUNCTION quality.save_rating_review(p_round_id TEXT,p_status TEXT,p_reviewer TEXT,
    p_revision BIGINT,p_content_revision TEXT) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $function$
DECLARE
    r ops.round_index%ROWTYPE;
    c quality.round_rating_review_current%ROWTYPE;
    saved quality.round_rating_reviews%ROWTYPE;
    content JSONB;
BEGIN
    IF p_status IS NULL OR p_status NOT IN ('valid','invalid') OR p_revision IS NULL OR p_revision<0
        OR p_reviewer IS NULL OR char_length(p_reviewer) NOT BETWEEN 1 AND 64
        OR p_content_revision IS NULL THEN
        RAISE EXCEPTION '评分复核参数无效' USING ERRCODE='22023';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('rating-review:' || p_round_id,0));
    -- 投影更新也必须等待此锁；校验的内容与响应来自同一份已锁定轮次。
    SELECT * INTO r FROM ops.round_index WHERE round_id=p_round_id FOR SHARE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    content := quality.rating_review_content(r);
    SELECT * INTO c FROM quality.round_rating_review_current WHERE round_id=p_round_id;
    IF COALESCE(c.review_id,0)<>p_revision OR md5(content::text)<>p_content_revision THEN
        RAISE EXCEPTION '评分内容或复核状态已变化，请重新读取' USING ERRCODE='40001';
    END IF;
    IF r.rating_status<>'rated' OR r.rating IS NULL OR r.rating NOT BETWEEN 1 AND 5 THEN
        RAISE EXCEPTION '当前轮次尚无已关联评分' USING ERRCODE='22023';
    END IF;
    IF c.status IS DISTINCT FROM p_status THEN
        INSERT INTO quality.round_rating_reviews(round_id,source_key,account_id,conversation_id,status,reviewer,content)
        VALUES(r.round_id,r.source_key,r.account_id,r.conversation_id,p_status,p_reviewer,content)
        RETURNING * INTO saved;
        INSERT INTO quality.round_rating_review_current(round_id,review_id,status,reviewer,reviewed_at)
        VALUES(r.round_id,saved.id,saved.status,saved.reviewer,saved.reviewed_at)
        ON CONFLICT(round_id) DO UPDATE SET review_id=EXCLUDED.review_id,status=EXCLUDED.status,
            reviewer=EXCLUDED.reviewer,reviewed_at=EXCLUDED.reviewed_at;
        -- 与汇总构建复用日期锁，覆盖起始日与全部坐席回复日期。
        PERFORM ops.stat_mark_days(ARRAY(SELECT DISTINCT unnest(ops.stat_fact_days(f))
            FROM ops.round_service_facts f WHERE f.round_id=p_round_id));
    END IF;
    RETURN quality.get_rating_review(p_round_id);
END
$function$;
REVOKE ALL ON quality.round_rating_reviews,quality.round_rating_review_current FROM PUBLIC,archive_reader,archive_ingest,archive_quality;
GRANT SELECT ON quality.round_rating_reviews,quality.round_rating_review_current TO archive_reader,archive_ingest,archive_quality;
REVOKE ALL ON FUNCTION quality.rating_review_content(ops.round_index),quality.get_rating_review(TEXT),
    quality.save_rating_review(TEXT,TEXT,TEXT,BIGINT,TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION quality.rating_review_content(ops.round_index),quality.get_rating_review(TEXT)
    TO archive_reader,archive_ingest,archive_quality;
GRANT EXECUTE ON FUNCTION quality.save_rating_review(TEXT,TEXT,TEXT,BIGINT,TEXT) TO archive_reader;
INSERT INTO ops.schema_migrations(version,checksum_sha256,description)
VALUES('021_rating_reviews',:'migration_checksum','评分有效性复核、版本冲突及统计日期原子失效');
\endif
COMMIT;
