-- Consultation (busy) segments per round for the seat "咨询状态总时长" report.
-- A round is cut wherever two consecutive valid messages (customer messages or
-- seat replies) are more than 10 minutes apart; each segment runs from its first
-- to its last public seat reply. Rebuildable: quality/processing/service_facts.py
-- (FACT_VERSION 5) recomputes old rows, reports compute missing rows live.
BEGIN;
SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(175607, 1);
SET LOCAL ROLE archive_owner;
SELECT EXISTS (SELECT 1 FROM ops.schema_migrations WHERE version = '014_consultation_segments') AS already_applied \gset
\if :already_applied
    SELECT checksum_sha256 = :'migration_checksum' AS checksum_matches
    FROM ops.schema_migrations WHERE version = '014_consultation_segments' \gset
    \if :checksum_matches
        \echo '014_consultation_segments already applied; unchanged migration skipped.'
    \else
        \echo 'ERROR: applied migration checksum differs; restore it and add a new migration.'
        \quit 3
    \endif
\else

-- Nullable column without default: a catalog-only change, no table rewrite.
ALTER TABLE ops.round_service_facts
    ADD COLUMN consultation_spans TSTZMULTIRANGE;

COMMENT ON COLUMN ops.round_service_facts.consultation_spans IS
    '本轮咨询活跃段（相邻有效消息间隔超过10分钟即切段，每段从坐席首条到末条公开回复）；空表示无可计时段。';

INSERT INTO ops.schema_migrations (version, checksum_sha256, description)
VALUES ('014_consultation_segments', :'migration_checksum', 'Consultation busy segments per round in stored statistics facts');
\endif
COMMIT;
