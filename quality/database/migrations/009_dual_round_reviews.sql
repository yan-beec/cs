-- Independently review the AI and human portions while retaining every revision.
BEGIN;
SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(175607, 1);
SELECT EXISTS (SELECT 1 FROM ops.schema_migrations WHERE version = '009_dual_round_reviews') AS already_applied \gset
\if :already_applied
    SELECT checksum_sha256 = :'migration_checksum' AS checksum_matches
    FROM ops.schema_migrations WHERE version = '009_dual_round_reviews' \gset
    \if :checksum_matches
        \echo '009_dual_round_reviews already applied; unchanged migration skipped.'
    \else
        \echo 'ERROR: applied migration checksum differs; restore it and add a new migration.'
        \quit 3
    \endif
\else
SET LOCAL ROLE archive_owner;
ALTER TABLE quality.round_reviews
    ADD COLUMN ai_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (ai_status IN ('passed', 'failed', 'pending')),
    ADD COLUMN human_status TEXT CHECK (human_status IN ('passed', 'failed', 'pending'));
-- Legacy manual verdicts become human verdicts; remarks and revision IDs stay intact.
UPDATE quality.round_reviews SET human_status = status;
ALTER TABLE quality.round_reviews ALTER COLUMN human_status SET NOT NULL,
    ALTER COLUMN human_status SET DEFAULT 'pending';
CREATE OR REPLACE VIEW quality.current_round_reviews AS
    SELECT DISTINCT ON (round_id) round_id, status, remark, reviewer, reviewed_at,
        id::text AS revision, ai_status, human_status
    FROM quality.round_reviews ORDER BY round_id, id DESC;

CREATE FUNCTION quality.save_round_review(
    p_round_id TEXT, p_ai_status TEXT, p_human_status TEXT,
    p_remark TEXT, p_reviewer TEXT, p_revision BIGINT
) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE
    latest_revision BIGINT;
    saved quality.round_reviews%ROWTYPE;
BEGIN
    IF p_ai_status IS NULL OR p_ai_status NOT IN ('passed', 'failed', 'pending')
        OR p_human_status IS NULL OR p_human_status NOT IN ('passed', 'failed', 'pending')
        OR p_remark IS NULL OR char_length(p_remark) > 5000
        OR p_reviewer IS NULL OR char_length(p_reviewer) NOT BETWEEN 1 AND 64
        OR p_revision IS NULL OR p_revision < 0 THEN
        RAISE EXCEPTION 'Invalid round review' USING ERRCODE = '22023';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('quality-review:' || p_round_id, 0));
    SELECT id INTO latest_revision FROM quality.round_reviews
        WHERE round_id = p_round_id ORDER BY id DESC LIMIT 1;
    IF COALESCE(latest_revision, 0) <> p_revision THEN
        RAISE EXCEPTION 'Round review changed' USING ERRCODE = '40001';
    END IF;
    INSERT INTO quality.round_reviews (round_id, source_key, account_id, conversation_id,
        status, ai_status, human_status, remark, reviewer)
    SELECT r.round_id, r.source_key, r.account_id, r.conversation_id,
        p_human_status, p_ai_status, p_human_status, p_remark, p_reviewer
    FROM ops.round_index r WHERE r.round_id = p_round_id
    RETURNING * INTO saved;
    IF NOT FOUND THEN RETURN NULL; END IF;
    RETURN jsonb_build_object('round_id', saved.round_id,
        'ai_status', saved.ai_status, 'human_status', saved.human_status,
        'remark', saved.remark, 'reviewer', saved.reviewer,
        'reviewed_at', saved.reviewed_at, 'revision', saved.id::text);
END
$function$;
REVOKE ALL ON FUNCTION quality.save_round_review(TEXT,TEXT,TEXT,TEXT,TEXT,BIGINT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION quality.save_round_review(TEXT,TEXT,TEXT,TEXT,TEXT,BIGINT) TO archive_reader;
-- Stale applications must refresh rather than silently overwrite two verdicts with one.
REVOKE ALL ON FUNCTION quality.save_round_review(TEXT,TEXT,TEXT,TEXT,BIGINT) FROM archive_reader;
COMMENT ON COLUMN quality.round_reviews.status IS
    'Legacy manual verdict; mirrors human_status on new revisions. Use ai_status and human_status.';
INSERT INTO ops.schema_migrations(version,checksum_sha256,description)
VALUES('009_dual_round_reviews',:'migration_checksum','Independent AI and human round verdicts; preserve legacy manual reviews');
\endif
COMMIT;
