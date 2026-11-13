-- 按保存时的接待分类校验结论，纯 AI 的人工结论为空，保留全部历史版本。
BEGIN;
SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(175607, 1);
SELECT EXISTS (SELECT 1 FROM ops.schema_migrations WHERE version = '019_scene_round_reviews') AS already_applied \gset
\if :already_applied
    SELECT checksum_sha256 = :'migration_checksum' AS checksum_matches
    FROM ops.schema_migrations WHERE version = '019_scene_round_reviews' \gset
    \if :checksum_matches
        \echo '019_scene_round_reviews already applied; unchanged migration skipped.'
    \else
        \echo 'ERROR: applied migration checksum differs; restore it and add a new migration.'
        \quit 3
    \endif
\else
SET LOCAL ROLE archive_owner;
ALTER TABLE quality.round_reviews ALTER COLUMN human_status DROP NOT NULL,
    ALTER COLUMN human_status DROP DEFAULT;
CREATE OR REPLACE FUNCTION quality.save_round_review(
    p_round_id TEXT, p_ai_status TEXT, p_human_status TEXT,
    p_remark TEXT, p_reviewer TEXT, p_revision BIGINT
) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE
    latest_revision BIGINT;
    current_round ops.round_index%ROWTYPE;
    saved quality.round_reviews%ROWTYPE;
BEGIN
    IF p_ai_status IS NULL OR p_ai_status NOT IN ('passed', 'failed', 'pending')
        OR (p_human_status IS NOT NULL AND p_human_status NOT IN ('passed', 'failed', 'pending'))
        OR p_remark IS NULL OR char_length(p_remark) > 5000
        OR p_reviewer IS NULL OR char_length(p_reviewer) NOT BETWEEN 1 AND 64
        OR p_revision IS NULL OR p_revision < 0 THEN
        RAISE EXCEPTION 'Invalid round review' USING ERRCODE = '22023';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('quality-review:' || p_round_id, 0));
    -- 与投影更新互斥，分类校验和写入使用同一轮次快照。
    SELECT * INTO current_round FROM ops.round_index WHERE round_id = p_round_id FOR SHARE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    IF (current_round.reception_type = 'ai_only' AND p_human_status IS NOT NULL)
        OR (current_round.reception_type = 'human' AND p_human_status IS NULL) THEN
        RAISE EXCEPTION 'Review does not match current reception type'
            USING ERRCODE = '22023', DETAIL = current_round.reception_type;
    END IF;
    SELECT id INTO latest_revision FROM quality.round_reviews
        WHERE round_id = p_round_id ORDER BY id DESC LIMIT 1;
    IF COALESCE(latest_revision, 0) <> p_revision THEN
        RAISE EXCEPTION 'Round review changed' USING ERRCODE = '40001';
    END IF;
    INSERT INTO quality.round_reviews (round_id, source_key, account_id, conversation_id,
        status, ai_status, human_status, remark, reviewer)
    VALUES (current_round.round_id, current_round.source_key, current_round.account_id,
        current_round.conversation_id, COALESCE(p_human_status, 'pending'),
        p_ai_status, p_human_status, p_remark, p_reviewer)
    RETURNING * INTO saved;
    RETURN jsonb_build_object('round_id', saved.round_id, 'reception_type', current_round.reception_type,
        'ai_status', saved.ai_status, 'human_status', saved.human_status,
        'remark', saved.remark, 'reviewer', saved.reviewer,
        'reviewed_at', saved.reviewed_at, 'revision', saved.id::text);
END
$function$;
REVOKE ALL ON FUNCTION quality.save_round_review(TEXT,TEXT,TEXT,TEXT,TEXT,BIGINT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION quality.save_round_review(TEXT,TEXT,TEXT,TEXT,TEXT,BIGINT) TO archive_reader;
COMMENT ON COLUMN quality.round_reviews.human_status IS '人工结论；纯 AI 接待保存 NULL（不适用）。历史结论不回写。';
INSERT INTO ops.schema_migrations(version,checksum_sha256,description)
VALUES('019_scene_round_reviews',:'migration_checksum','Validate current reception scene and allow null human verdicts for AI-only rounds');
\endif
COMMIT;
