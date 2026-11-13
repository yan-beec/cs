-- Stored response totals for wait-weighted averages (2026-11-11, FACT_VERSION 9).
-- R3: the report's average response = sum of every round's completed-wait
-- seconds / sum of their number. Each round now stores the total seconds of its
-- counted waits next to response_samples, so reports and day rollups only add
-- stored numbers instead of recomputing per round. Nullable column, no rewrite;
-- facts of older versions are refilled by the processor in idle time.
BEGIN;
SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(175607, 1);
SET LOCAL ROLE archive_owner;
SELECT EXISTS (SELECT 1 FROM ops.schema_migrations WHERE version = '018_response_seconds_total') AS already_applied \gset
\if :already_applied
    SELECT checksum_sha256 = :'migration_checksum' AS checksum_matches
    FROM ops.schema_migrations WHERE version = '018_response_seconds_total' \gset
    \if :checksum_matches
        \echo '018_response_seconds_total already applied; unchanged migration skipped.'
    \else
        \echo 'ERROR: applied migration checksum differs; restore it and add a new migration.'
        \quit 3
    \endif
\else

ALTER TABLE ops.round_service_facts ADD COLUMN response_seconds_total DOUBLE PRECISION;

COMMENT ON COLUMN ops.round_service_facts.response_seconds_total IS
    '本轮考核坐席已完成响应（含首响，剔除超过600秒的段）的总秒数；报表平均响应 = Σ本列 ÷ Σresponse_samples。';
COMMENT ON COLUMN ops.round_service_facts.average_response_seconds IS
    '本轮平均响应（response_seconds_total ÷ response_samples），仅供归档卡片、筛选和离线 HTML，不参与报表汇总。';
COMMENT ON COLUMN ops.round_service_facts.consultation_spans IS
    '本轮咨询时段：转人工成功→解决，含之后任何人的有效消息，相邻间隔超过600秒切开；协助轮次与无 ICS 分配轮次为空。';

INSERT INTO ops.schema_migrations (version, checksum_sha256, description)
VALUES ('018_response_seconds_total', :'migration_checksum',
    'Stored per-round response totals for wait-weighted report averages');
\endif
COMMIT;
