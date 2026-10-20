-- Human reviews outlive rebuildable round projections. Every save is retained.
BEGIN;
SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(175607, 1);
SELECT EXISTS (SELECT 1 FROM ops.schema_migrations WHERE version = '008_round_reviews') AS already_applied \gset
\if :already_applied
    SELECT checksum_sha256 = :'migration_checksum' AS checksum_matches
    FROM ops.schema_migrations WHERE version = '008_round_reviews' \gset
    \if :checksum_matches
        \echo '008_round_reviews already applied; unchanged migration skipped.'
    \else
        \echo 'ERROR: applied migration checksum differs; restore it and add a new migration.'
        \quit 3
    \endif
\else
SET LOCAL ROLE archive_owner;
CREATE TABLE quality.round_reviews (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    round_id TEXT NOT NULL,
    source_key TEXT NOT NULL,
    account_id BIGINT NOT NULL,
    conversation_id BIGINT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('passed', 'failed', 'pending')),
    remark TEXT NOT NULL CHECK (char_length(remark) <= 5000),
    reviewer TEXT NOT NULL CHECK (char_length(reviewer) BETWEEN 1 AND 64),
    reviewed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX round_reviews_latest_idx ON quality.round_reviews (round_id, id DESC);
-- No foreign key to ops.round_index: processing replaces those rows on replay.
CREATE VIEW quality.current_round_reviews AS
    SELECT DISTINCT ON (round_id) round_id, status, remark, reviewer, reviewed_at,
        id::text AS revision
    FROM quality.round_reviews ORDER BY round_id, id DESC;
REVOKE ALL ON quality.round_reviews FROM PUBLIC, archive_ingest, archive_reader, archive_quality;
REVOKE ALL ON SEQUENCE quality.round_reviews_id_seq FROM PUBLIC, archive_ingest, archive_reader, archive_quality;
REVOKE ALL ON quality.current_round_reviews FROM PUBLIC, archive_ingest, archive_reader, archive_quality;
GRANT SELECT ON quality.round_reviews, quality.current_round_reviews TO archive_reader, archive_quality;

CREATE FUNCTION quality.save_round_review(
    p_round_id TEXT, p_status TEXT, p_remark TEXT, p_reviewer TEXT, p_revision BIGINT
) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE
    latest_revision BIGINT;
    saved quality.round_reviews%ROWTYPE;
BEGIN
    IF p_status IS NULL OR p_status NOT IN ('passed', 'failed', 'pending')
        OR p_remark IS NULL OR char_length(p_remark) > 5000
        OR p_reviewer IS NULL OR char_length(p_reviewer) NOT BETWEEN 1 AND 64
        OR p_revision IS NULL OR p_revision < 0 THEN
        RAISE EXCEPTION 'Invalid round review' USING ERRCODE = '22023';
    END IF;
    -- Serialize saves for one round and reject stale browser edits.
    PERFORM pg_advisory_xact_lock(hashtextextended('quality-review:' || p_round_id, 0));
    SELECT id INTO latest_revision FROM quality.round_reviews
        WHERE round_id = p_round_id ORDER BY id DESC LIMIT 1;
    IF COALESCE(latest_revision, 0) <> p_revision THEN
        RAISE EXCEPTION 'Round review changed' USING ERRCODE = '40001';
    END IF;
    INSERT INTO quality.round_reviews (round_id, source_key, account_id, conversation_id,
        status, remark, reviewer)
    SELECT r.round_id, r.source_key, r.account_id, r.conversation_id,
        p_status, p_remark, p_reviewer
    FROM ops.round_index r WHERE r.round_id = p_round_id
    RETURNING * INTO saved;
    IF NOT FOUND THEN RETURN NULL; END IF;
    RETURN jsonb_build_object('round_id', saved.round_id, 'status', saved.status,
        'remark', saved.remark, 'reviewer', saved.reviewer,
        'reviewed_at', saved.reviewed_at, 'revision', saved.id::text);
END
$function$;
REVOKE ALL ON FUNCTION quality.save_round_review(TEXT,TEXT,TEXT,TEXT,BIGINT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION quality.save_round_review(TEXT,TEXT,TEXT,TEXT,BIGINT) TO archive_reader;
COMMENT ON TABLE quality.round_reviews IS
    'Permanent human round reviews. New revisions append; projection rebuilds and presence retention never delete them.';
INSERT INTO ops.schema_migrations(version,checksum_sha256,description)
VALUES('008_round_reviews',:'migration_checksum','Persistent per-round human verdicts and remarks with append-only revisions');
\endif
COMMIT;
