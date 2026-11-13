-- 咨询状态总时长异常定位（只读，不改任何数据）。在归档库执行，例如：
--   docker compose -f docker-compose.yml -f docker-compose.archive.yml exec -T archive-postgres \
--     psql -U archive_admin -d ics_quality \
--       -v starts='2026-11-06 00:00:00+08' -v ends='2026-11-07 00:00:00+08' -v agent='%小陈%' \
--       -f - < quality/database/diagnose-consultation-seconds.sql
--
-- 参数（都可省略，省略时用下面的默认值）：
--   starts / ends  与统计报表相同的筛选区间，北京时间，结束不含；结束晚于当前时刻时按当前时刻截止
--   agent          坐席名称匹配（ILIKE），'%' 表示全部坐席
--   round_id       指定要看明细的轮次；省略时自动取第 2 步中计入时长最长的那一轮
--
-- 第 1、2 段按【旧口径】计算（2026-11-10 之前上线的版本）：每个坐席在每一轮里从自己第一条
-- 公开回复到最后一条公开回复，截到筛选区间内，同一坐席多轮重叠部分只算一次——用来定位旧报表的异常数字。
-- 第 1b 段按【新口径】（活跃段，statistics/agent_quality.py consultation_ctes）：只取转人工成功后的
-- 有效消息（客户或坐席公开消息），相邻两条间隔超过 600 秒切段，每段从段内第一条消息到该坐席在段内
-- 最后一条回复。为便于对照（且可在未迁移 011 的库上运行），这里按实际发送人归属、未观察到转人工时
-- 取整轮消息，不套用“主责坐席”判定，所以协助轮次里帮忙的同事也会显示时长。

\if :{?starts}
\else
\set starts '2026-11-06 00:00:00+08'
\endif
\if :{?ends}
\else
\set ends '2026-11-07 00:00:00+08'
\endif
\if :{?agent}
\else
\set agent '%'
\endif
\if :{?round_id}
\else
\set round_id ''
\endif

\pset null '—'
BEGIN READ ONLY;
SET LOCAL TIME ZONE 'Asia/Shanghai';

-- 1. 坐席总览：先确认是哪个坐席偏高，以及这个“坐席”是不是真人。
--    total_counted = 报表里的咨询状态总时长；sum_of_round_spans 是不去重叠的简单相加。
--    ai_phase_rounds > 0 表示这个发送者在“纯 AI 接待”轮次里也发过“人工”消息，
--    多半是 AI 回复用了 CHATWOOT_API_TOKEN 对应的用户账号而不是 AgentBot。
\echo '== 1. 坐席总览 =='
WITH bounds AS (
    SELECT :'starts'::timestamptz AS starts_at,
           least(:'ends'::timestamptz, now()) AS ends_at, now() AS observed_at
), spans AS (
    SELECT m.source_key, m.account_id, m.sender_id, max(m.sender_name) AS sender_name, m.round_id,
           max(r.reception_type) AS reception_type,
           greatest(min(m.sent_at), b.starts_at) AS starts_at,
           least(max(m.sent_at), b.ends_at) AS ends_at
    FROM ops.round_index r JOIN ops.message_index m USING (round_id) CROSS JOIN bounds b
    WHERE r.started_at < b.ends_at AND r.last_message_at >= b.starts_at
      AND m.sender_role = 'agent' AND m.direction = 'outgoing' AND NOT m.is_private
      AND COALESCE(m.data->>'is_rating', 'false') <> 'true' AND m.sender_id IS NOT NULL
      AND (m.sent_at IS NULL OR m.sent_at < b.observed_at)
    GROUP BY m.source_key, m.account_id, m.sender_id, m.round_id, b.starts_at, b.ends_at
    HAVING NOT bool_or(m.sent_at IS NULL)
), seats AS (
    SELECT source_key, account_id, sender_id, max(sender_name) AS sender_name,
           count(*) AS rounds,
           count(*) FILTER (WHERE reception_type = 'ai_only') AS ai_phase_rounds,
           sum(greatest(ends_at - starts_at, interval '0')) AS sum_of_round_spans,
           range_agg(tstzrange(starts_at, ends_at, '[)')) FILTER (WHERE starts_at < ends_at) AS spans
    FROM spans GROUP BY 1, 2, 3
)
SELECT s.sender_id, s.sender_name, s.rounds, s.ai_phase_rounds,
       justify_interval(COALESCE((SELECT sum(upper(x) - lower(x)) FROM unnest(s.spans) AS x), interval '0')) AS total_counted,
       justify_interval(s.sum_of_round_spans) AS sum_of_round_spans
FROM seats s
WHERE s.sender_name ILIKE :'agent'
ORDER BY total_counted DESC;

-- 1b. 新口径（活跃段）总览：与第 1 段的 total_counted 对照，空档造成的虚高会消失。
\echo '== 1b. 新口径（10 分钟活跃段）坐席总览 =='
WITH bounds AS (
    SELECT :'starts'::timestamptz AS starts_at,
           least(:'ends'::timestamptz, now()) AS ends_at, now() AS observed_at
), handoffs AS (
    SELECT r.round_id,
           COALESCE(
               CASE WHEN r.data->>'responsibility_basis' = 'ics_assignment'
                   AND NULLIF(r.data->>'responsible_assigned_at', '')::timestamptz >= r.started_at
                   THEN NULLIF(r.data->>'responsible_assigned_at', '')::timestamptz END,
               CASE WHEN r.data->>'responsibility_basis' IS DISTINCT FROM 'ics_assignment'
                   AND NULLIF(r.data->>'handoff_requested_at', '')::timestamptz >= r.started_at
                   THEN NULLIF(r.data->>'handoff_requested_at', '')::timestamptz END,
               CASE WHEN r.data->>'takeover_inherited' = 'true' THEN r.started_at END) AS handoff_at
    FROM ops.round_index r CROSS JOIN bounds b
    WHERE r.started_at < b.ends_at AND r.last_message_at >= b.starts_at
), events AS (
    SELECT m.round_id, m.sent_at, m.sender_role = 'agent' AS seat_reply,
           m.source_key, m.account_id, m.sender_id, m.sender_name
    FROM handoffs h JOIN ops.message_index m USING (round_id) CROSS JOIN bounds b
    WHERE NOT m.is_private AND COALESCE(m.data->>'is_rating', 'false') <> 'true'
      AND ((m.sender_role = 'agent' AND m.direction = 'outgoing' AND m.sender_id IS NOT NULL)
           OR (m.sender_role = 'contact' AND m.direction = 'incoming'))
      AND m.sent_at < b.observed_at AND (h.handoff_at IS NULL OR m.sent_at > h.handoff_at)
      AND NOT m.is_deleted AND (COALESCE(m.content, '') ~ '[^[:space:]]'
          OR jsonb_array_length(CASE WHEN jsonb_typeof(m.data->'attachments') = 'array'
              THEN m.data->'attachments' ELSE '[]'::jsonb END) > 0)
), breaks AS (
    SELECT e.*, CASE WHEN e.sent_at - lag(e.sent_at) OVER (PARTITION BY e.round_id ORDER BY e.sent_at)
               <= interval '600 seconds' THEN 0 ELSE 1 END AS starts_segment
    FROM events e
), numbered AS (
    SELECT b.*, sum(b.starts_segment) OVER (PARTITION BY b.round_id ORDER BY b.sent_at
               ROWS UNBOUNDED PRECEDING) AS segment
    FROM breaks b
), segment_starts AS (
    SELECT round_id, segment, min(sent_at) AS first_at FROM numbered GROUP BY 1, 2
), segments AS (
    -- 每段从段内第一条消息（不论谁发）到该坐席在段内的最后一条回复。
    SELECT n.source_key, n.account_id, n.sender_id, max(n.sender_name) AS sender_name,
           tstzrange(greatest(st.first_at, bd.starts_at), least(max(n.sent_at), bd.ends_at), '[)') AS span
    FROM numbered n JOIN segment_starts st USING (round_id, segment) CROSS JOIN bounds bd
    WHERE n.seat_reply
    GROUP BY n.round_id, n.segment, st.first_at, n.source_key, n.account_id, n.sender_id, bd.starts_at, bd.ends_at
    HAVING greatest(st.first_at, bd.starts_at) < least(max(n.sent_at), bd.ends_at)
)
SELECT sender_id, max(sender_name) AS sender_name, count(*) AS segments,
       justify_interval(COALESCE((SELECT sum(upper(x) - lower(x)) FROM unnest(range_agg(span)) AS x), interval '0')) AS total_counted_new
FROM segments
WHERE sender_name ILIKE :'agent'
GROUP BY source_key, account_id, sender_id
ORDER BY total_counted_new DESC;

-- 2. 按轮次列出该坐席计入时长最长的轮次，异常轮次一般排在最前面。
--    看点：max_gap 很大（同一轮中间停了很久又回复）、replies_before_handoff > 0
--    （转人工之前就有“人工”消息）、invalid_replies > 0（已删除/空白消息撑开了时段）。
--    用 customer_name + conversation_id + round_no 可以在归档页面里找到这一轮。
\echo '== 2. 计入时长最长的轮次（前 30） =='
WITH bounds AS (
    SELECT :'starts'::timestamptz AS starts_at,
           least(:'ends'::timestamptz, now()) AS ends_at, now() AS observed_at
), replies AS (
    SELECT m.round_id, m.sender_id, m.sender_name, m.message_id, m.sent_at,
           NOT m.is_deleted AND (COALESCE(m.content, '') ~ '[^[:space:]]'
               OR jsonb_array_length(CASE WHEN jsonb_typeof(m.data->'attachments') = 'array'
                   THEN m.data->'attachments' ELSE '[]'::jsonb END) > 0) AS valid,
           m.sent_at - lag(m.sent_at) OVER (PARTITION BY m.round_id, m.sender_id
               ORDER BY m.sent_at, m.message_id) AS gap
    FROM ops.round_index r JOIN ops.message_index m USING (round_id) CROSS JOIN bounds b
    WHERE r.started_at < b.ends_at AND r.last_message_at >= b.starts_at
      AND m.sender_role = 'agent' AND m.direction = 'outgoing' AND NOT m.is_private
      AND COALESCE(m.data->>'is_rating', 'false') <> 'true' AND m.sender_id IS NOT NULL
      AND (m.sent_at IS NULL OR m.sent_at < b.observed_at)
      AND m.sender_name ILIKE :'agent'
), per_round AS (
    SELECT round_id, sender_id, max(sender_name) AS sender_name, count(*) AS replies,
           count(*) FILTER (WHERE NOT valid) AS invalid_replies,
           min(sent_at) AS first_reply, max(sent_at) AS last_reply, max(gap) AS max_gap
    FROM replies GROUP BY 1, 2 HAVING NOT bool_or(sent_at IS NULL)
), handoff AS (
    SELECT p.*, r.conversation_id, r.round_no, r.customer_id, r.started_at, r.status, r.reception_type,
           NULLIF(r.data->>'resolved_at', '')::timestamptz AS resolved_at,
           COALESCE(
               CASE WHEN r.data->>'responsibility_basis' = 'ics_assignment'
                   AND NULLIF(r.data->>'responsible_assigned_at', '')::timestamptz >= r.started_at
                   THEN NULLIF(r.data->>'responsible_assigned_at', '')::timestamptz END,
               CASE WHEN r.data->>'responsibility_basis' IS DISTINCT FROM 'ics_assignment'
                   AND NULLIF(r.data->>'handoff_requested_at', '')::timestamptz >= r.started_at
                   THEN NULLIF(r.data->>'handoff_requested_at', '')::timestamptz END,
               CASE WHEN r.data->>'takeover_inherited' = 'true' THEN r.started_at END) AS handoff_at
    FROM per_round p JOIN ops.round_index r USING (round_id)
)
SELECT h.sender_name, c.display_name AS customer_name, h.conversation_id, h.round_no,
       justify_interval(greatest(least(h.last_reply, b.ends_at) - greatest(h.first_reply, b.starts_at), interval '0')) AS counted,
       h.replies, h.max_gap, h.invalid_replies,
       (SELECT count(*) FROM replies x WHERE x.round_id = h.round_id AND x.sender_id = h.sender_id
            AND h.handoff_at IS NOT NULL AND x.sent_at < h.handoff_at) AS replies_before_handoff,
       (SELECT count(*) FROM replies x WHERE x.round_id = h.round_id AND x.sender_id = h.sender_id
            AND h.resolved_at IS NOT NULL AND x.sent_at > h.resolved_at) AS replies_after_resolved,
       to_char(h.started_at, 'MM-DD HH24:MI:SS') AS round_started,
       to_char(h.handoff_at, 'MM-DD HH24:MI:SS') AS handoff_at,
       to_char(h.first_reply, 'MM-DD HH24:MI:SS') AS first_reply,
       to_char(h.last_reply, 'MM-DD HH24:MI:SS') AS last_reply,
       to_char(h.resolved_at, 'MM-DD HH24:MI:SS') AS resolved_at,
       h.status, h.reception_type, h.round_id
FROM handoff h CROSS JOIN bounds b
    LEFT JOIN ops.customer_index c ON c.customer_id = h.customer_id
ORDER BY counted DESC, h.max_gap DESC NULLS LAST
LIMIT 30;

-- 自动取上一步最长的一轮（若命令行已指定 round_id 则用指定的）。
\if :{?top_round_id}
\else
\set top_round_id ''
\endif
SELECT m.round_id AS top_round_id
FROM ops.round_index r JOIN ops.message_index m USING (round_id)
WHERE r.started_at < least(:'ends'::timestamptz, now()) AND r.last_message_at >= :'starts'::timestamptz
  AND m.sender_role = 'agent' AND m.direction = 'outgoing' AND NOT m.is_private
  AND COALESCE(m.data->>'is_rating', 'false') <> 'true' AND m.sender_id IS NOT NULL
  AND m.sent_at < now() AND m.sender_name ILIKE :'agent'
GROUP BY m.round_id, m.sender_id
ORDER BY least(max(m.sent_at), least(:'ends'::timestamptz, now()))
       - greatest(min(m.sent_at), :'starts'::timestamptz) DESC
LIMIT 1 \gset

-- 3. 这一轮的完整时间线（不含正文），gap_from_prev 很大的位置就是空档。
--    counts_for_seat = 是 表示这条消息参与了该坐席的起止时间计算。
\echo '== 3. 轮次时间线 =='
SELECT to_char(m.sent_at, 'MM-DD HH24:MI:SS') AS sent_at,
       m.sent_at - lag(m.sent_at) OVER (ORDER BY m.sent_at NULLS LAST, m.message_id) AS gap_from_prev,
       m.sender_role, m.direction, m.sender_name, m.sender_id, m.is_private, m.is_deleted,
       COALESCE(m.data->>'is_rating', 'false') = 'true' AS is_rating,
       CASE WHEN m.sender_role = 'agent' AND m.direction = 'outgoing' AND NOT m.is_private
                 AND COALESCE(m.data->>'is_rating', 'false') <> 'true' AND m.sender_id IS NOT NULL
            THEN '是' END AS counts_for_seat,
       left(regexp_replace(COALESCE(m.content, ''), '\s+', ' ', 'g'), 30) AS preview,
       m.message_id
FROM ops.message_index m
WHERE m.round_id = COALESCE(NULLIF(:'round_id', ''), :'top_round_id')
ORDER BY m.sent_at NULLS LAST, m.message_id
LIMIT 500;

-- 4. 这一轮期间该会话的状态变化事件（解决/重新打开）。
--    Chatwoot 已经自动解决，但这里没有 resolved 事件 → 归档没收到解决事件（Webhook 未订阅/投递失败）；
--    有 resolved 事件但 recognized_as_resolution 不是 t → 收到了但没被识别为解决；
--    解决被识别了、而第 3 段里解决之后还有坐席消息且中间没有客户消息 → 解决后的回复仍归在旧轮次。
\echo '== 4. 轮次期间的会话状态事件 =='
SELECT to_char(COALESCE(e.source_time, e.received_at), 'MM-DD HH24:MI:SS') AS event_time,
       to_char(e.received_at, 'MM-DD HH24:MI:SS') AS received_at,
       e.event_type, e.ingestion_kind,
       COALESCE(e.payload->>'status', e.payload->'conversation'->>'status') AS status,
       e.payload ? 'updated_at' AS has_updated_at,
       x.is_resolution AS recognized_as_resolution,
       j.status AS job_status, left(j.last_error, 60) AS job_error, e.id AS event_id
FROM ops.round_index r
    JOIN archive.events e ON e.source_key = r.source_key AND e.account_id = r.account_id
        AND e.conversation_id = r.conversation_id
    LEFT JOIN ops.event_replay_index x ON x.event_id = e.id
    LEFT JOIN ops.event_jobs j ON j.event_id = e.id
WHERE r.round_id = COALESCE(NULLIF(:'round_id', ''), :'top_round_id')
  AND COALESCE(e.source_time, e.received_at) >= r.started_at - interval '1 hour'
  AND COALESCE(e.source_time, e.received_at) <= r.last_message_at + interval '1 hour'
  AND (e.event_type = 'conversation_status_changed'
       OR EXISTS (SELECT 1 FROM jsonb_array_elements(CASE WHEN jsonb_typeof(e.payload->'changed_attributes') = 'array'
                      THEN e.payload->'changed_attributes' ELSE '[]'::jsonb END) c WHERE c ? 'status'))
ORDER BY COALESCE(e.source_time, e.received_at), e.id
LIMIT 200;

-- 5. 全库：最近 7 天每天收到的“已解决”事件数与被识别数，看是否整体缺失。
\echo '== 5. 最近 7 天解决事件接收/识别情况 =='
SELECT to_char(e.received_at, 'YYYY-MM-DD') AS day,
       count(*) FILTER (WHERE COALESCE(e.payload->>'status', e.payload->'conversation'->>'status') = 'resolved') AS resolved_events,
       count(*) FILTER (WHERE x.is_resolution) AS recognized,
       count(*) FILTER (WHERE x.event_id IS NULL) AS not_indexed
FROM archive.events e LEFT JOIN ops.event_replay_index x ON x.event_id = e.id
WHERE e.received_at >= now() - interval '7 days'
  AND e.event_type IN ('conversation_status_changed', 'conversation_updated')
GROUP BY 1 ORDER BY 1;

ROLLBACK;
