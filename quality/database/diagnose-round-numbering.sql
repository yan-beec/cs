-- 轮次编号排查（只读）。用 archive_reader 或 archive_admin 执行：
--   docker compose --env-file .env -f docker-compose.yml -f docker-compose.archive.yml \
--     exec -T archive-postgres psql -U archive_admin -d ics_quality \
--     -v customer_id="'<customer_id>'" -f - < quality/database/diagnose-round-numbering.sql
-- customer_id 必须传入（可在 ops.customer_index 中按姓名/联系人 ID 查到）；
-- 只看全库情况时可传 -v customer_id="''"，第①、②段返回空结果。
\set ON_ERROR_STOP on
\pset pager off
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;

\echo '① 该客户的全部轮次：会话 ID 各不相同且都是 round_no=1，说明每次咨询都是新的 Chatwoot 会话'
SELECT conversation_id, round_no AS conversation_round_no,
    row_number() OVER (PARTITION BY customer_id ORDER BY started_at, round_id) AS customer_round_seq,
    started_at, status, data->>'start_reason' AS start_reason
FROM ops.round_index WHERE customer_id = :customer_id ORDER BY started_at, round_id;

\echo '② 该客户各会话的投影任务状态（failed 持续存在说明会话投影被卡住）'
SELECT e.conversation_id, j.status, j.last_error, count(*) AS jobs
FROM ops.event_jobs j JOIN archive.events e ON e.id = j.event_id
WHERE e.conversation_id IN (SELECT DISTINCT conversation_id FROM ops.round_index WHERE customer_id = :customer_id)
GROUP BY 1, 2, 3 ORDER BY 1, 2;

\echo '③ 全库：仍有未完成重放计划的会话（应为空或很快清空）'
SELECT source_key, account_id, conversation_id, updated_at,
    plan->>'cursor' AS cursor, plan->>'high_water' AS high_water
FROM ops.conversation_replay_state WHERE plan <> '{}'::jsonb ORDER BY updated_at LIMIT 50;

\echo '④ 全库：投影任务状态分布'
SELECT status, last_error, count(*) FROM ops.event_jobs GROUP BY 1, 2 ORDER BY 3 DESC;

\echo '⑤ 最近的“已解决”状态事件是否被识别（has_updated_at=false 或 is_resolution=false 需关注）'
SELECT e.id, e.conversation_id, e.payload ? 'updated_at' AS has_updated_at,
    e.payload->>'status' AS status, x.is_resolution
FROM archive.events e LEFT JOIN ops.event_replay_index x ON x.event_id = e.id
WHERE e.event_type = 'conversation_status_changed' ORDER BY e.id DESC LIMIT 20;

\echo '⑥ 全库：多会话客户中“每个会话只有 1 轮”的人数（量化按会话编号的影响面）'
SELECT count(*) AS customers_all_round_one, sum(conversations) AS conversations
FROM (
    SELECT customer_id, count(DISTINCT conversation_id) AS conversations, max(round_no) AS max_round_no
    FROM ops.round_index GROUP BY customer_id
) t WHERE conversations > 1 AND max_round_no = 1;

ROLLBACK;
