"""AI review aggregates over current verdicts and archived round evidence."""


def percentage(failed, total):
    return round(failed / total * 100, 2) if total else None


def ai_quality(connection, period, where, values, now, busy_notice):
    row = dict(connection.execute(f"""
        WITH selected AS MATERIALIZED (
            SELECT r.round_id,r.reception_type FROM ops.round_index r
            WHERE {where} AND r.started_at >= %s AND r.started_at < %s
        ), flags AS (
            SELECT s.*,review.ai_status,
                EXISTS(SELECT 1 FROM ops.message_index m WHERE m.round_id=s.round_id
                    AND m.sender_role='bot' AND m.direction='outgoing' AND NOT m.is_private
                    AND m.content=%s) AS refused,
                EXISTS(SELECT 1 FROM ops.message_index m WHERE m.round_id=s.round_id
                    AND m.sender_role='agent' AND m.direction='outgoing' AND NOT m.is_private
                    AND COALESCE(m.data->>'is_rating','false') <> 'true') AS replied,
                EXISTS(SELECT 1 FROM ops.message_index m WHERE m.round_id=s.round_id
                    AND m.sender_role='bot' AND m.direction='outgoing' AND NOT m.is_private
                    AND NOT m.is_deleted AND COALESCE(m.data->>'is_rating','false') <> 'true'
                    AND (COALESCE(m.content,'') ~ '[^[:space:]]'
                        OR jsonb_array_length(COALESCE(m.data->'attachments','[]'::jsonb)) > 0)) AS ai_replied
            FROM selected s LEFT JOIN quality.current_round_reviews review USING(round_id)
        ) SELECT count(*) AS total_rounds,
            count(*) FILTER(WHERE reception_type='human' OR refused OR replied) AS handoff,
            count(*) FILTER(WHERE reception_type='ai_only' AND ai_replied AND NOT refused AND NOT replied) AS ai_only,
            count(*) FILTER(WHERE refused) AS handoff_failed,
            count(*) FILTER(WHERE ai_status='failed') AS failed_rounds
        FROM flags
    """, [*values, period.start, min(period.end, now), busy_notice]).fetchone())
    row['failure_rate'] = percentage(row['failed_rounds'], row['total_rounds'])
    return row
