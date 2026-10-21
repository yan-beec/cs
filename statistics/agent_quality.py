"""Per-seat quality from completed reply waits and observed ICS assignments."""

from ics_agent.app.chatwoot.payload import is_explicit_handoff_request


def handoff_sql(alias):
    # Projection timestamps are ISO timestamps produced by quality's projector.
    # A request can be refused one or more times before ICS records the actual
    # assignment.  The assignment timestamp is the successful transfer clock;
    # using the earlier request timestamp would include the failed wait.
    return f"""COALESCE(
        CASE WHEN {alias}.data->>'responsibility_basis'='ics_assignment'
            AND NULLIF({alias}.data->>'responsible_assigned_at','')::timestamptz >= {alias}.started_at
            THEN NULLIF({alias}.data->>'responsible_assigned_at','')::timestamptz END,
        CASE WHEN {alias}.data->>'responsibility_basis' IS DISTINCT FROM 'ics_assignment'
            AND NULLIF({alias}.data->>'handoff_requested_at','')::timestamptz >= {alias}.started_at
            THEN NULLIF({alias}.data->>'handoff_requested_at','')::timestamptz END,
        CASE WHEN {alias}.data->>'takeover_inherited'='true' THEN {alias}.started_at END)"""


def handoff_ctes(where):
    """The caller supplies bounds and scoped parameters inside one read snapshot."""
    return f"""
        handoffs AS MATERIALIZED (
            SELECT r.round_id,r.source_key,r.account_id,{handoff_sql('r')} AS handoff_at,
                CASE WHEN r.data->>'responsibility_basis'='ics_assignment'
                    THEN NULLIF(r.data->>'responsible_agent_id','') END AS agent_id,
                r.data->>'responsible_agent_name' AS agent_name,
                NULLIF(r.data->>'responsible_assigned_at','')::timestamptz AS assigned_at
            FROM ops.round_index r CROSS JOIN bounds
            WHERE {where} AND r.started_at < bounds.ends_at
        ), assignment_totals AS (
            SELECT source_key,account_id,agent_id,max(agent_name) AS agent_name,
                count(*) AS assigned_inquiries
            FROM handoffs CROSS JOIN bounds
            WHERE agent_id IS NOT NULL AND assigned_at >= bounds.starts_at AND assigned_at < bounds.ends_at
            GROUP BY 1,2,3
        ), lost AS MATERIALIZED (
            SELECT r.* FROM handoffs r CROSS JOIN bounds
            WHERE r.handoff_at >= bounds.starts_at AND r.handoff_at < bounds.ends_at
                AND NOT EXISTS(SELECT 1 FROM ops.message_index m WHERE m.round_id=r.round_id
                    AND m.sender_role='agent' AND m.direction='outgoing' AND NOT m.is_private
                    AND COALESCE(m.data->>'is_rating','false') <> 'true'
                    AND (m.sent_at IS NULL OR m.sent_at < bounds.observed_at))
        ), loss_totals AS (
            SELECT source_key,account_id,agent_id,max(agent_name) AS agent_name,
                count(*) AS lost_inquiries FROM lost WHERE agent_id IS NOT NULL GROUP BY 1,2,3
        )
    """


def consultation_ctes(where):
    """Union each seat's first-to-last reply spans, clipped to the selected window.

    Include spans crossing the window even when neither endpoint is inside it.
    PostgreSQL's multirange union also handles nested and touching intervals.
    """
    return f"""
        consultation_spans AS (
            SELECT m.source_key,m.account_id,m.sender_id AS agent_id,max(m.sender_name) AS agent_name,
                greatest(min(m.sent_at),bounds.starts_at) AS starts_at,
                least(max(m.sent_at),bounds.ends_at) AS ends_at
            FROM ops.round_index r JOIN ops.message_index m USING(round_id) CROSS JOIN bounds
            WHERE {where} AND r.started_at < bounds.ends_at AND r.last_message_at >= bounds.starts_at
                AND m.sender_role='agent' AND m.direction='outgoing' AND NOT m.is_private
                AND COALESCE(m.data->>'is_rating','false') <> 'true' AND m.sender_id IS NOT NULL
                AND (m.sent_at IS NULL OR m.sent_at < bounds.observed_at)
            GROUP BY m.source_key,m.account_id,m.sender_id,m.round_id,bounds.starts_at,bounds.ends_at
            HAVING NOT bool_or(m.sent_at IS NULL)
        ), consultation_ranges AS (
            SELECT source_key,account_id,agent_id,max(agent_name) AS agent_name,
                range_agg(tstzrange(starts_at,ends_at,'[)')) AS spans
            FROM consultation_spans WHERE starts_at < ends_at GROUP BY 1,2,3
        ), consultation_totals AS (
            SELECT source_key,account_id,agent_id,max(agent_name) AS agent_name,
                sum(extract(epoch FROM upper(span)-lower(span)))::double precision AS consultation_seconds
            FROM consultation_ranges CROSS JOIN LATERAL unnest(spans) AS span GROUP BY 1,2,3
        )
    """


def timing_replies_cte(where):
    """Find seats in the transfer cohort, including replies after the window."""
    return f"""
        timing_replies AS MATERIALIZED (
            SELECT DISTINCT r.source_key,r.account_id,m.sender_id AS agent_id,m.sender_name,m.round_id
            FROM ops.round_index r JOIN ops.message_index m USING(round_id) CROSS JOIN bounds
            WHERE {where} AND {handoff_sql('r')} >= bounds.starts_at
                AND {handoff_sql('r')} < bounds.ends_at
                AND m.sent_at >= {handoff_sql('r')} AND m.sent_at < bounds.observed_at
                AND m.sender_role='agent' AND m.direction='outgoing' AND NOT m.is_private
                AND COALESCE(m.data->>'is_rating','false') <> 'true' AND m.sender_id IS NOT NULL
                AND NOT m.is_deleted AND (COALESCE(m.content,'') ~ '[^[:space:]]'
                    OR jsonb_array_length(CASE WHEN jsonb_typeof(m.data->'attachments')='array'
                        THEN m.data->'attachments' ELSE '[]'::jsonb END) > 0)
        )
    """


def reply_quality(connection, period, agents, now):
    """Keep reply-based ratings/dialogues and transfer-based response timings.

    First waits start at the first valid post-transfer customer message. Later
    waits start at the last message of a customer burst. Each completed wait is
    credited to its replying seat, then averaged per round before per-seat means.
    Timing rounds are selected by handoff time; their full history through now
    can include replies after the selected window. Unanswered tails add no wait.
    """
    if not agents:
        return {}
    placeholders = ','.join(['(%s::text,%s::bigint,%s::text)'] * len(agents))
    identities = [a[key] for a in agents for key in ('source_key', 'account_id', 'agent_id')]
    timing_scope = """EXISTS(SELECT 1 FROM selected s WHERE s.source_key=r.source_key
        AND s.account_id=r.account_id AND s.agent_id=m.sender_id)"""
    cohort = f"""
        WITH selected(source_key,account_id,agent_id) AS (VALUES {placeholders}),
        bounds AS (SELECT %s::timestamptz AS starts_at,%s::timestamptz AS ends_at,
            %s::timestamptz AS observed_at),
        selected_replies AS MATERIALIZED (
            SELECT DISTINCT m.source_key,m.account_id,m.sender_id AS agent_id,m.round_id
            FROM ops.message_index m JOIN selected s
                ON m.source_key=s.source_key AND m.account_id=s.account_id AND m.sender_id=s.agent_id
                CROSS JOIN bounds
            WHERE m.sent_at >= bounds.starts_at AND m.sent_at < bounds.ends_at
                AND m.sender_role='agent' AND m.direction='outgoing' AND NOT m.is_private
                AND COALESCE(m.data->>'is_rating','false') <> 'true' AND m.round_id IS NOT NULL
        ), {timing_replies_cte(timing_scope)}, rounds AS MATERIALIZED (
            SELECT r.round_id,r.rating,r.rating_status,{handoff_sql('r')} AS handoff_at,
                NULLIF(r.data->>'handoff_requested_at','')::timestamptz AS raw_requested_at,
                CASE WHEN r.data->>'responsibility_basis'='ics_assignment'
                    THEN {handoff_sql('r')}
                    ELSE NULLIF(r.data->>'handoff_requested_at','')::timestamptz END AS requested_at,
                CASE WHEN r.data->>'responsibility_trigger_message_id' ~ '^[0-9]+$'
                    THEN (r.data->>'responsibility_trigger_message_id')::bigint END AS assignment_trigger_message_id,
                r.data->>'handoff_reason' AS handoff_reason
            FROM ops.round_index r JOIN (
                SELECT round_id FROM selected_replies UNION SELECT round_id FROM timing_replies
            ) p USING(round_id)
        )
    """
    params = [*identities, period.start, min(period.end, now), now]
    # With second-precision source times, a greeting and the request may share a
    # timestamp. Use the same request parser as collection, on original content,
    # rather than assuming that the first message at that timestamp requested it.
    ambiguous = connection.execute(f"""{cohort}
        SELECT r.round_id,jsonb_agg(jsonb_build_object('id',m.message_id,'content',o.content)
            ORDER BY m.message_id) AS candidates
        FROM rounds r JOIN ops.message_index m USING(round_id)
            JOIN archive.message_snapshots o ON o.id=m.original_snapshot_id
        WHERE r.handoff_reason='explicit_request' AND r.raw_requested_at=r.handoff_at
            AND m.sent_at=r.handoff_at AND m.sender_role='contact' AND m.direction='incoming'
            AND NOT m.is_private AND COALESCE(m.data->>'is_rating','false') <> 'true'
        GROUP BY r.round_id HAVING count(*) > 1
    """, params).fetchall()
    markers = {}
    for row in ambiguous:
        for message in row['candidates']:
            if is_explicit_handoff_request(message['content']):
                markers[row['round_id']] = message['id']
                break
    rows = connection.execute(f"""{cohort},
        request_markers AS (SELECT * FROM unnest(%s::text[],%s::bigint[]) AS t(round_id,message_id)),
        messages AS MATERIALIZED (
            SELECT m.round_id,m.sent_at,m.message_id,m.sender_id,
                m.sender_role='contact' AND m.direction='incoming' AS customer,
                m.sender_role='agent' AND m.direction='outgoing' AS agent,
                NOT m.is_deleted AND (COALESCE(m.content,'') ~ '[^[:space:]]'
                    OR jsonb_array_length(CASE WHEN jsonb_typeof(m.data->'attachments')='array'
                        THEN m.data->'attachments' ELSE '[]'::jsonb END) > 0) AS valid_content
            FROM ops.message_index m JOIN rounds r USING(round_id) CROSS JOIN bounds
            WHERE NOT m.is_private AND COALESCE(m.data->>'is_rating','false') <> 'true'
                AND ((m.sender_role='contact' AND m.direction='incoming')
                    OR (m.sender_role='agent' AND m.direction='outgoing'))
                AND (m.sent_at IS NULL OR m.sent_at < bounds.observed_at)
        ), phases AS (
            SELECT r.round_id,r.handoff_at,r.requested_at,
                bool_or(m.sent_at IS NULL AND m.valid_content) AS unknown_time,
                COALESCE(max(marker.message_id), max(r.assignment_trigger_message_id),
                    min(m.message_id) FILTER(WHERE m.customer AND m.sent_at=r.handoff_at)) AS request_message_id
            FROM rounds r JOIN messages m USING(round_id) LEFT JOIN request_markers marker USING(round_id)
            GROUP BY r.round_id,r.handoff_at,r.requested_at
        ), visitor_messages AS (
            SELECT p.round_id,count(*) AS messages
            FROM phases p JOIN messages m USING(round_id)
            WHERE m.customer AND m.valid_content AND (m.sent_at > p.handoff_at
                OR (m.sent_at=p.handoff_at AND (p.requested_at IS DISTINCT FROM p.handoff_at
                    OR m.message_id > p.request_message_id)))
            GROUP BY p.round_id
        ), dialogues AS (
            SELECT p.source_key,p.account_id,p.agent_id,count(*) AS dialogues,
                count(*) FILTER(WHERE v.messages > 5) AS high_quality_dialogues
            FROM selected_replies p JOIN visitor_messages v USING(round_id) GROUP BY 1,2,3
        ), ordered AS (
            SELECT m.*,p.handoff_at,
                row_number() OVER(PARTITION BY m.round_id ORDER BY m.sent_at,m.message_id) AS ordinal
            FROM messages m JOIN phases p USING(round_id) CROSS JOIN bounds
            WHERE NOT p.unknown_time AND m.valid_content
                AND p.handoff_at >= bounds.starts_at AND p.handoff_at < bounds.ends_at
                AND (m.sent_at > p.handoff_at OR (m.sent_at=p.handoff_at
                    AND (p.requested_at IS DISTINCT FROM p.handoff_at OR m.message_id > p.request_message_id)))
        ), sequenced AS (
            SELECT o.*,max(ordinal) FILTER(WHERE agent) OVER(PARTITION BY round_id ORDER BY ordinal
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS previous_agent
            FROM ordered o
        ), cycles AS (
            SELECT round_id,previous_agent,
                min(sent_at) FILTER(WHERE customer) AS first_customer_at,
                max(sent_at) FILTER(WHERE customer) AS last_customer_at,
                min(sent_at) FILTER(WHERE agent) AS replied_at,
                max(sender_id) FILTER(WHERE agent) AS agent_id
            FROM sequenced GROUP BY round_id,previous_agent
        ), responses AS (
            SELECT c.*,row_number() OVER(PARTITION BY round_id
                ORDER BY replied_at,previous_agent NULLS FIRST) AS response_no
            FROM cycles c WHERE first_customer_at IS NOT NULL AND replied_at IS NOT NULL
        ), completed AS (
            SELECT c.*,
                extract(epoch FROM replied_at - CASE WHEN response_no=1
                    THEN first_customer_at ELSE last_customer_at END)::double precision AS response_seconds
            FROM responses c
        ), round_timings AS (
            SELECT r.source_key,r.account_id,c.agent_id,c.round_id,
                max(response_seconds) FILTER(WHERE response_no=1) AS first_response_seconds,
                avg(response_seconds) AS average_response_seconds,count(response_seconds) AS response_samples
            FROM completed c JOIN ops.round_index r USING(round_id)
            GROUP BY r.source_key,r.account_id,c.agent_id,c.round_id
        ), timings AS (
            SELECT source_key,account_id,agent_id,
                avg(first_response_seconds) AS first_response_seconds,
                count(first_response_seconds) AS first_response_samples,
                avg(average_response_seconds) AS average_response_seconds,
                sum(response_samples) AS response_samples,count(*) AS response_rounds
            FROM round_timings GROUP BY source_key,account_id,agent_id
        ), ratings AS (
            SELECT p.source_key,p.account_id,p.agent_id,
                avg(r.rating)::double precision AS average_rating,
                count(*) AS rated_inquiries,count(*) FILTER(WHERE r.rating < 4) AS low_rating
            FROM selected_replies p JOIN rounds r USING(round_id)
            WHERE r.rating_status='rated' AND r.rating BETWEEN 1 AND 5 GROUP BY 1,2,3
        ) SELECT s.*,t.first_response_seconds,COALESCE(t.first_response_samples,0) AS first_response_samples,
            t.average_response_seconds,COALESCE(t.response_samples,0) AS response_samples,
            COALESCE(t.response_rounds,0) AS response_rounds,
            r.average_rating,COALESCE(r.rated_inquiries,0) AS rated_inquiries,COALESCE(r.low_rating,0) AS low_rating,
            COALESCE(d.dialogues,0) AS dialogues,COALESCE(d.high_quality_dialogues,0) AS high_quality_dialogues
        FROM selected s LEFT JOIN timings t USING(source_key,account_id,agent_id)
            LEFT JOIN ratings r USING(source_key,account_id,agent_id)
            LEFT JOIN dialogues d USING(source_key,account_id,agent_id)
    """, [*params, list(markers), list(markers.values())]).fetchall()
    return {(r['source_key'], r['account_id'], r['agent_id']): dict(r) for r in rows}
