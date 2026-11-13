-- 离线 HTML 留档查询耗时评估（只读）。在归档库执行，例如：
--   docker compose -f docker-compose.yml -f docker-compose.archive.yml exec -T archive-postgres \
--     psql -U archive_admin -d ics_quality -f - < quality/database/diagnose-offline-export.sql
-- 留档查询的 120 秒超时只看数据库侧的“扫描 + 排序”，与图片下载、HTML 生成时间无关。

\timing on

-- 1. 消息表规模：总条数、表 + TOAST 总大小、平均每行字节数
SELECT count(*) AS messages,
       pg_size_pretty(pg_total_relation_size('ops.message_index')) AS total_size,
       pg_total_relation_size('ops.message_index') / greatest(count(*), 1) AS bytes_per_message
FROM ops.message_index;

-- 2. 每月消息量（北京时间），看最大的月份有多少
SELECT to_char(sent_at AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM') AS month, count(*) AS messages
FROM ops.message_index GROUP BY 1 ORDER BY 1;

-- 3. 模拟留档的单核整表扫描（与留档一样关闭并行）。按月留档会扫描两次，
--    再加排序和关联，总耗时大约是这里 Execution Time 的 2.5～4 倍。
BEGIN READ ONLY;
SET LOCAL max_parallel_workers_per_gather = 0;
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*) FROM ops.message_index
WHERE sent_at >= date_trunc('month', now() AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai';
ROLLBACK;
