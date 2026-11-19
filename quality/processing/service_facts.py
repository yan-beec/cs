"""Per-round statistics facts: the single definition shared by statistics reports and archive filters.

One round's facts depend only on that round's projection row and messages, so the
processor derives them while it projects the conversation (same transaction) and
both the reports and the archive only read them. The SQL here is the only place
where per-round statistics rules are written down; any round whose stored facts
are missing, were computed under an older FACT_VERSION, or contain messages at or
after the caller's "observed" moment is computed live with the identical SQL
(see live_facts()), so results never depend on backfill state or on when a
report/archive view was opened.

Stored per round (ops.round_service_facts, "L1") and per round and hour
(ops.round_reply_hours, "L2", migration 015).

Rules (FACT_VERSION 7, 2026-11-10):
- Transfer clock: ops.round_handoff_at (confirmed ICS assignment, else request,
  else inherited takeover).
- Human reply (one predicate everywhere): public, outgoing, sender role agent,
  known sender, not a rating card, not deleted, non-blank text or attachment.
- Valid customer message: public incoming contact message, not deleted,
  non-blank text or attachment.
- Post-transfer messages: later than the transfer, or in the same second but
  after the request message itself (same-second ties resolved with the ICS
  request parser on original content, see request_markers()).
- Assigned seat: the confirmed ICS assignment (responsibility_basis
  'ics_assignment'). It is the only seat any round is ever credited to; rounds
  without one count for no seat (reports show them as a separate total).
- Assisted round: an ICS-assigned round in which any other seat sent a human
  reply at or after the transfer. The whole round is then assessed for nobody
  (assessed seat responsible_agent_id = NULL); it keeps only the assignment
  count.
- Assessed seat (responsible_agent_id): the assigned seat unless assisted.
- Response timings (R3): every wait, the first included, runs from the last
  valid customer message of a burst after the transfer to the next human
  reply. The customer must speak after the transfer: the request itself does
  not start a wait and a seat speaking first (greeting) is no response.
  Consecutive replies and an unanswered tail add nothing; waits over
  MAX_RESPONSE_SECONDS are excluded without renumbering; only rounds whose
  single post-transfer replying seat is the assessed seat get timings. Each
  round stores its number of waits and their total seconds; reports weight by
  waits: sum of totals / sum of numbers.
- Consultation segments (R11): the transfer, valid customer messages and human
  replies of any seat strictly after it, and the resolution, cut at gaps over
  CONSULTATION_IDLE_SECONDS; each segment from its first to its last point.
  Credited to the ICS-assigned seat; assisted rounds and rounds without any
  human reply (lost) have none. Segments may end after the last reply.
- Reply hours (L2): one row per round and hour with a human reply, with the
  previous such hour of the round (prev_hour_at): a round is counted once in
  [A, B) on its row with hour in [A, B) and prev_hour_at NULL or before A.
- Lost: a transfer was observed and the round has no human reply.
- Report flags: busy refusal (the ICS notice below), human reply, AI reply,
  "handoff" (human reception, refusal or reply) and "AI only" (AI reception
  without refusal or human reply).
- Rating: the linked 1-5 score of a rated round, else NULL.
"""

from __future__ import annotations

import logging
import time

from .budget import MaintenanceBudget
from quality.query.service_metrics import MAX_RESPONSE_SECONDS

logger = logging.getLogger(__name__)

# Historical ICS notice: only this observed message proves a busy refusal.
# Do not infer refusal from an unanswered/pending handoff or a muted contact.
BUSY_NOTICE = ("当前人工坐席繁忙，您可以先继续描述问题，我会尽力帮您解答。"
               "如仍需人工客服，请稍后再次尝试「转人工」。")
assert "'" not in BUSY_NOTICE  # inlined as an SQL literal below

# Bump when any rule above changes: reports and archive then recompute old rows
# live, the processor backfills them and day rollups are rebuilt.
# 2 (2026-11-09): rounds with more than one replying seat add no response timing.
# 3 (2026-11-09): every seat metric belongs to the round's responsible seat only.
# 4 (2026-11-09): rounds a non-responsible seat replied to are assessed for nobody.
# 5 (2026-11-10): consultation time from active segments (migration 014).
# 6 (2026-11-10): segments start at the first post-transfer message and end at the
#    responsible seat's last reply; assisted rounds keep their responsible seat.
# 7 (2026-11-10): one definition for reports and archive (migration 015): the
#    assessed seat is the ICS assignment only, one human-reply predicate, wide
#    per-round facts and per-hour reply rows.
# 9 (2026-11-11, migration 018): waits start at the last message of a customer
#    burst after the transfer, reports weight averages by waits from the stored
#    response_seconds_total (R3); consultation runs from the transfer to the
#    resolution, split by any 600+ second gap, assisted rounds excluded (R11).
# 10 (2026-11-12, migration 020): reply hours carry the round's previous reply
#    hour (prev_hour_at) so any period counts a round once from an index range;
#    rounds without a human reply (lost) have no consultation segments. The
#    migration converts stored version 9 rows in place.
# 11（2026-11-12）：排队通知撤销本次请求，排队本身不产生转人工和流单。
FACT_VERSION = 11

# A gap longer than this between two consecutive valid messages of a round ends
# a consultation segment; exactly 600 seconds continues it.
CONSULTATION_IDLE_SECONDS = 600

VALID_CONTENT = """NOT m.is_deleted AND (COALESCE(m.content,'') ~ '[^[:space:]]'
    OR jsonb_array_length(CASE WHEN jsonb_typeof(m.data->'attachments')='array'
        THEN m.data->'attachments' ELSE '[]'::jsonb END) > 0)"""

# The one human-reply predicate (message alias m). Archive filters, reports,
# lost rounds and flags all use it.
HUMAN_REPLY = ("m.sender_role='agent' AND m.direction='outgoing' AND NOT m.is_private"
               " AND COALESCE(m.data->>'is_rating','false') <> 'true' AND m.sender_id IS NOT NULL AND "
               + VALID_CONTENT)

ASSIGNED_AGENT_ID = ("CASE WHEN {r}.data->>'responsibility_basis'='ics_assignment'"
                     " THEN NULLIF({r}.data->>'responsible_agent_id','') END")
ASSIGNED_AGENT_NAME = ("CASE WHEN {r}.data->>'responsibility_basis'='ics_assignment'"
                       " AND NULLIF({r}.data->>'responsible_agent_id','') IS NOT NULL"
                       " THEN {r}.data->>'responsible_agent_name' END")
ASSIGNED_AT = ("CASE WHEN {r}.data->>'responsibility_basis'='ics_assignment'"
               " AND NULLIF({r}.data->>'responsible_agent_id','') IS NOT NULL"
               " THEN ops.archive_ts({r}.data->>'responsible_assigned_at') END")

# Column order and types of one fact row; used for the table insert, the live
# JSON transport and every reader's relation.
FACT_COLUMNS = (
    ("round_id", "text"), ("source_key", "text"), ("account_id", "bigint"), ("channel_key", "text"),
    ("inbox_id", "bigint"), ("started_at", "timestamptz"), ("reception_type", "text"),
    ("handoff_at", "timestamptz"), ("assigned_agent_id", "text"), ("assigned_agent_name", "text"),
    ("assigned_at", "timestamptz"), ("responsible_agent_id", "text"), ("responsible_agent_name", "text"),
    ("assisted", "boolean"), ("responding_seats", "integer"), ("first_reply_at", "timestamptz"),
    ("last_reply_at", "timestamptz"), ("human_replied", "boolean"), ("busy_refused", "boolean"),
    ("ai_replied", "boolean"), ("is_lost", "boolean"), ("is_handoff", "boolean"), ("is_ai_only", "boolean"),
    ("visitor_messages", "integer"), ("rating", "smallint"), ("first_response_seconds", "double precision"),
    ("average_response_seconds", "double precision"), ("response_samples", "integer"),
    ("consultation_spans", "tstzmultirange"), ("messages_until", "timestamptz"),
    ("response_seconds_total", "double precision"),
)
HOUR_COLUMNS = (("round_id", "text"), ("hour_at", "timestamptz"), ("any_replies", "integer"),
                ("assessed_replies", "integer"), ("prev_hour_at", "timestamptz"))
FACT_NAMES = tuple(name for name, _ in FACT_COLUMNS)
HOUR_NAMES = tuple(name for name, _ in HOUR_COLUMNS)


def fact_is_current(fact: str, observed_at: str) -> str:
    """Stored facts apply when computed under this rule version and no message of
    the round is at or after the caller's "now" (live rules only see earlier ones)."""
    return (f"{fact}.fact_version={FACT_VERSION}"
            f" AND ({fact}.messages_until IS NULL OR {fact}.messages_until < {observed_at})")


def consultation_segment_ctes(rounds: str, events: str, prefix: str) -> str:
    """Consultation segments (R11) of the rounds in CTE `rounds`.

    `rounds(round_id,transfer_at,resolved_at)` gives each round's successful
    transfer and its resolution (NULL while unresolved or not yet seen);
    `events(round_id,sent_at,human_reply)` holds the round's valid public
    customer messages and human replies of any seat (sent_at may be NULL).

    The timeline is the transfer, every such message strictly after it and the
    resolution. Neighbouring points more than CONSULTATION_IDLE_SECONDS apart
    split it (exactly 600 seconds continues); each segment runs from its first
    point to its last, so a long gap before the resolution ends the last segment
    at the message before the gap. Without an observed transfer the timeline
    starts at the first human reply. A message of unknown time cannot be placed,
    so its round has no segments. Produces `{prefix}_segments(round_id,first_at,last_at)`:
    one row per segment of positive length.
    """
    return f"""
        {prefix}_stats AS (
            -- Per-round aggregates joined by hash (no correlated scans of the events).
            SELECT e.round_id,min(e.sent_at) FILTER(WHERE e.human_reply) AS first_reply_at,
                bool_or(e.sent_at IS NULL) AS unknown_time
            FROM {events} e GROUP BY e.round_id
        ), {prefix}_scoped AS (
            SELECT r.round_id,r.resolved_at,COALESCE(r.transfer_at,st.first_reply_at) AS begins_at,
                COALESCE(st.unknown_time,false) AS unknown_time
            FROM {rounds} r LEFT JOIN {prefix}_stats st USING(round_id)
        ), {prefix}_points AS (
            SELECT s.round_id,s.begins_at AS at FROM {prefix}_scoped s
            WHERE NOT s.unknown_time AND s.begins_at IS NOT NULL
            UNION ALL
            SELECT s.round_id,e.sent_at FROM {prefix}_scoped s JOIN {events} e USING(round_id)
            WHERE NOT s.unknown_time AND e.sent_at > s.begins_at
            UNION ALL
            SELECT s.round_id,s.resolved_at FROM {prefix}_scoped s
            WHERE NOT s.unknown_time AND s.resolved_at > s.begins_at
        ), {prefix}_breaks AS (
            SELECT p.round_id,p.at,
                CASE WHEN p.at - lag(p.at) OVER(PARTITION BY p.round_id ORDER BY p.at)
                    <= interval '{CONSULTATION_IDLE_SECONDS} seconds' THEN 0 ELSE 1 END AS starts_segment
            FROM {prefix}_points p
        ), {prefix}_numbered AS (
            SELECT b.*,sum(b.starts_segment) OVER(PARTITION BY b.round_id ORDER BY b.at
                ROWS UNBOUNDED PRECEDING) AS segment
            FROM {prefix}_breaks b
        ), {prefix}_segments AS (
            SELECT round_id,min(at) AS first_at,max(at) AS last_at
            FROM {prefix}_numbered
            GROUP BY round_id,segment HAVING min(at) < max(at)
        )"""


def fact_ctes(targets: str, markers: str, observed_at: str) -> str:
    """CTE fragments over `targets(round_id)` and `markers(round_id,message_id)`.

    Produces fact_rows (one row per target round, columns FACT_COLUMNS) and
    fact_hour_rows (columns HOUR_COLUMNS). observed_at is an SQL expression for
    "now": messages at or after it are not seen.

    Shape matters for scale: the only joins are rounds->message_index (index
    lookups) and rounds->markers / rounds->per-round aggregates (hash on
    round_id). Later steps are window functions or GROUP BY over one CTE.
    """
    busy = BUSY_NOTICE
    seen = f"(m.sent_at IS NULL OR m.sent_at < {observed_at})"
    return f"""
        fact_rounds AS MATERIALIZED (
            SELECT r.round_id,r.source_key,r.account_id,r.channel_key,r.inbox_id,r.started_at,r.reception_type,
                CASE WHEN r.rating_status='rated' AND r.rating BETWEEN 1 AND 5 THEN r.rating END AS rating,
                ops.round_handoff_at(r.data,r.started_at) AS handoff_at,
                CASE WHEN r.data->>'responsibility_basis'='ics_assignment'
                    THEN ops.round_handoff_at(r.data,r.started_at)
                    ELSE NULLIF(r.data->>'handoff_requested_at','')::timestamptz END AS requested_at,
                COALESCE(marker.message_id, CASE WHEN r.data->>'responsibility_trigger_message_id' ~ '^[0-9]+$'
                    THEN (r.data->>'responsibility_trigger_message_id')::bigint END) AS known_request_message_id,
                {ASSIGNED_AGENT_ID.format(r='r')} AS assigned_agent_id,
                {ASSIGNED_AGENT_NAME.format(r='r')} AS assigned_agent_name,
                {ASSIGNED_AT.format(r='r')} AS assigned_at,
                EXISTS(SELECT 1 FROM ops.message_index m WHERE m.round_id=r.round_id AND {seen}
                    AND m.sender_role='bot' AND m.direction='outgoing' AND NOT m.is_private
                    AND m.content='{busy}') AS busy_refused,
                EXISTS(SELECT 1 FROM ops.message_index m WHERE m.round_id=r.round_id AND {seen}
                    AND m.sender_role='bot' AND m.direction='outgoing' AND NOT m.is_private
                    AND COALESCE(m.data->>'is_rating','false') <> 'true' AND {VALID_CONTENT}) AS ai_replied,
                CASE WHEN r.status='resolved' AND ops.archive_ts(COALESCE(r.data->>'resolved_at',
                    r.data->>'closed_at')) < {observed_at}
                    THEN ops.archive_ts(COALESCE(r.data->>'resolved_at',r.data->>'closed_at')) END AS resolved_at,
                -- The resolution is part of the consultation timeline: a fact computed
                -- after it is not valid for an earlier "now".
                GREATEST((SELECT max(m.sent_at) FROM ops.message_index m WHERE m.round_id=r.round_id),
                    CASE WHEN r.status='resolved' THEN ops.archive_ts(COALESCE(r.data->>'resolved_at',
                        r.data->>'closed_at')) END) AS messages_until
            FROM ops.round_index r LEFT JOIN {markers} marker USING(round_id)
            WHERE r.round_id IN (SELECT round_id FROM {targets})
        ), fact_messages AS MATERIALIZED (
            SELECT m.round_id,m.sent_at,m.message_id,m.sender_id,m.sender_name,
                r.source_key,r.account_id,r.handoff_at,r.requested_at,r.known_request_message_id,
                r.assigned_agent_id,
                m.sender_role='contact' AND m.direction='incoming' AS customer,
                m.sender_role='agent' AND m.direction='outgoing' AS agent,
                {VALID_CONTENT} AS valid_content,
                COALESCE({HUMAN_REPLY},false) AS human_reply
            FROM fact_rounds r JOIN ops.message_index m USING(round_id)
            WHERE NOT m.is_private AND COALESCE(m.data->>'is_rating','false') <> 'true'
                AND ((m.sender_role='contact' AND m.direction='incoming')
                    OR (m.sender_role='agent' AND m.direction='outgoing'))
                AND {seen}
        ), fact_phased AS (
            SELECT m.*,
                bool_or(m.sent_at IS NULL AND m.valid_content) OVER round_messages AS unknown_time,
                COALESCE(m.known_request_message_id, min(m.message_id) FILTER(
                    WHERE m.customer AND m.sent_at=m.handoff_at) OVER round_messages) AS request_message_id,
                -- Human replies at or after the transfer (any time without one).
                (m.human_reply AND m.sent_at IS NOT NULL
                    AND (m.handoff_at IS NULL OR m.sent_at >= m.handoff_at)) AS post_reply
            FROM fact_messages m WINDOW round_messages AS (PARTITION BY m.round_id)
        ), fact_flagged AS MATERIALIZED (
            SELECT p.*,(p.sent_at > p.handoff_at OR (p.sent_at=p.handoff_at
                AND (p.requested_at IS DISTINCT FROM p.handoff_at OR p.message_id > p.request_message_id)))
                AS post_transfer,
                -- Another seat than the ICS-assigned one replied: nobody is assessed.
                p.assigned_agent_id IS NOT NULL AND bool_or(p.post_reply AND p.sender_id <> p.assigned_agent_id)
                    OVER(PARTITION BY p.round_id) AS assisted
            FROM fact_phased p
        ), fact_summary AS (
            SELECT round_id,(count(*) FILTER(WHERE customer AND valid_content AND post_transfer))::integer AS visitor_messages,
                bool_or(assisted) AS assisted,
                (count(DISTINCT sender_id) FILTER(WHERE post_reply))::integer AS responding_seats,
                bool_or(human_reply) AS human_replied,
                min(sent_at) FILTER(WHERE human_reply) AS first_reply_at,
                max(sent_at) FILTER(WHERE human_reply) AS last_reply_at
            FROM fact_flagged GROUP BY round_id
        ), fact_ordered AS (
            SELECT f.*,row_number() OVER(PARTITION BY f.round_id ORDER BY f.sent_at,f.message_id) AS ordinal
            FROM fact_flagged f
            WHERE NOT f.unknown_time AND f.valid_content AND f.handoff_at IS NOT NULL AND f.post_transfer
        ), fact_sequenced AS (
            SELECT o.*,max(ordinal) FILTER(WHERE agent) OVER(PARTITION BY round_id ORDER BY ordinal
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS previous_agent
            FROM fact_ordered o
        ), fact_cycles AS (
            SELECT round_id,previous_agent,
                min(sent_at) FILTER(WHERE customer) AS first_customer_at,
                max(sent_at) FILTER(WHERE customer) AS last_customer_at,
                min(sent_at) FILTER(WHERE agent) AS replied_at,
                max(sender_id) FILTER(WHERE agent) AS agent_id
            FROM fact_sequenced GROUP BY round_id,previous_agent
        ), fact_responses AS (
            -- Only waits that follow a customer message after the transfer: a seat
            -- speaking first (greeting) is no response, and the first wait is the
            -- first one the customer started.
            SELECT c.*,row_number() OVER(PARTITION BY round_id
                ORDER BY replied_at,previous_agent NULLS FIRST) AS response_no
            FROM fact_cycles c WHERE first_customer_at IS NOT NULL AND replied_at IS NOT NULL
        ), fact_completed AS (
            SELECT c.*,
                extract(epoch FROM replied_at - last_customer_at)::double precision AS response_seconds
            FROM fact_responses c
        ), fact_seat_parts AS (
            -- Seats with a valid public reply at or after the transfer form the
            -- transfer cohort even when no wait completed ...
            SELECT round_id,sender_id AS agent_id,NULL::double precision AS response_seconds,
                NULL::bigint AS response_no,TRUE AS cohort,assigned_agent_id
            FROM fact_flagged
            WHERE agent AND valid_content AND sender_id IS NOT NULL AND handoff_at IS NOT NULL AND sent_at >= handoff_at
            UNION ALL
            -- ... and capped completed waits keep their original numbers: excluding
            -- a slow first reply must not promote a later reply to first response.
            SELECT round_id,agent_id,response_seconds,response_no,FALSE,NULL
            FROM fact_completed WHERE agent_id IS NOT NULL AND response_seconds BETWEEN 0 AND {MAX_RESPONSE_SECONDS}
        ), fact_seat_groups AS (
            SELECT round_id,agent_id,
                max(response_seconds) FILTER(WHERE response_no=1) AS first_response_seconds,
                avg(response_seconds) AS average_response_seconds,
                count(response_seconds)::integer AS response_samples,
                sum(response_seconds) AS response_seconds_total,
                max(assigned_agent_id) AS assigned_agent_id,
                count(*) OVER(PARTITION BY round_id) AS round_seats
            FROM fact_seat_parts GROUP BY round_id,agent_id HAVING bool_or(cohort)
        ), fact_timings AS (
            -- Only the assigned seat, and only when it answered the round alone
            -- after the transfer (an assisted round has no timing at all).
            SELECT round_id,first_response_seconds,average_response_seconds,response_samples,response_seconds_total
            FROM fact_seat_groups WHERE agent_id=assigned_agent_id AND round_seats=1
        ), fact_consultation_rounds AS (
            -- Only rounds the ICS-assigned seat answered alone: assisted rounds (mostly
            -- shift handovers, whose times would spill across shifts) count for nobody,
            -- and a round nobody answered (lost) has no consultation time.
            SELECT r.round_id,r.handoff_at AS transfer_at,r.resolved_at
            FROM fact_rounds r LEFT JOIN fact_summary s USING(round_id)
            WHERE r.assigned_agent_id IS NOT NULL AND NOT COALESCE(s.assisted,false)
                AND COALESCE(s.human_replied,false)
        ), fact_consultation_events AS (
            SELECT f.round_id,f.sent_at,f.human_reply
            FROM fact_flagged f JOIN fact_consultation_rounds USING(round_id)
            WHERE f.valid_content AND (f.customer OR f.human_reply)
        ), {consultation_segment_ctes('fact_consultation_rounds', 'fact_consultation_events', 'fact_consultation')}, fact_consultation AS (
            SELECT round_id,range_agg(tstzrange(first_at,last_at,'[)')) AS spans
            FROM fact_consultation_segments GROUP BY round_id
        ), fact_rows AS (
            SELECT r.round_id,r.source_key,r.account_id,r.channel_key,r.inbox_id,r.started_at,r.reception_type,
                r.handoff_at,r.assigned_agent_id,r.assigned_agent_name,r.assigned_at,
                CASE WHEN NOT COALESCE(s.assisted,false) THEN r.assigned_agent_id END AS responsible_agent_id,
                CASE WHEN NOT COALESCE(s.assisted,false) THEN r.assigned_agent_name END AS responsible_agent_name,
                COALESCE(s.assisted,false) AS assisted,COALESCE(s.responding_seats,0) AS responding_seats,
                s.first_reply_at,s.last_reply_at,COALESCE(s.human_replied,false) AS human_replied,
                r.busy_refused,r.ai_replied,
                r.handoff_at IS NOT NULL AND NOT COALESCE(s.human_replied,false) AS is_lost,
                (r.reception_type='human' OR r.busy_refused OR COALESCE(s.human_replied,false)) AS is_handoff,
                (r.reception_type='ai_only' AND NOT r.busy_refused AND NOT COALESCE(s.human_replied,false)) AS is_ai_only,
                COALESCE(s.visitor_messages,0) AS visitor_messages,r.rating,
                t.first_response_seconds,t.average_response_seconds,t.response_samples,
                t.response_seconds_total,COALESCE(c.spans,'{{}}'::tstzmultirange) AS consultation_spans,r.messages_until
            FROM fact_rounds r LEFT JOIN fact_summary s USING(round_id) LEFT JOIN fact_timings t USING(round_id)
                LEFT JOIN fact_consultation c USING(round_id)
        ), fact_hour_groups AS (
            SELECT round_id,date_trunc('hour',sent_at) AS hour_at,count(*)::integer AS any_replies,
                (count(*) FILTER(WHERE NOT assisted AND sender_id=assigned_agent_id))::integer AS assessed_replies
            FROM fact_flagged WHERE human_reply AND sent_at IS NOT NULL
            GROUP BY round_id,date_trunc('hour',sent_at)
        ), fact_hour_rows AS (
            SELECT g.*,lag(g.hour_at) OVER(PARTITION BY g.round_id ORDER BY g.hour_at) AS prev_hour_at
            FROM fact_hour_groups g
        )"""


def ambiguous_sql(targets_cte: str) -> str:
    """Rounds whose request second holds several customer messages, with original content."""
    return f"""{targets_cte}
        SELECT r.round_id,jsonb_agg(jsonb_build_object('id',m.message_id,'content',o.content)
            ORDER BY m.message_id) AS candidates
        FROM ops.round_index r JOIN ops.message_index m USING(round_id)
            JOIN archive.message_snapshots o ON o.id=m.original_snapshot_id
        WHERE r.round_id IN (SELECT round_id FROM fact_targets)
            AND r.data->>'handoff_reason'='explicit_request'
            AND NULLIF(r.data->>'handoff_requested_at','')::timestamptz=ops.round_handoff_at(r.data,r.started_at)
            AND m.sent_at=ops.round_handoff_at(r.data,r.started_at)
            AND m.sender_role='contact' AND m.direction='incoming'
            AND NOT m.is_private AND COALESCE(m.data->>'is_rating','false') <> 'true'
        GROUP BY r.round_id HAVING count(*) > 1"""


def request_markers(connection, targets_cte: str, params) -> dict:
    """Same-second ties: the ICS request parser on original content picks the request."""
    from ics_agent.app.chatwoot.payload import is_explicit_handoff_request

    markers = {}
    for row in connection.execute(ambiguous_sql(targets_cte), params).fetchall():
        for message in row["candidates"]:
            if is_explicit_handoff_request(message["content"]):
                markers[row["round_id"]] = message["id"]
                break
    return markers


TARGETS_BY_IDS = "WITH fact_targets AS (SELECT unnest(%s::text[]) AS round_id)"


def _with_facts(connection, round_ids, observed_at=None):
    """WITH clause producing fact_rows/fact_hour_rows; observed_at None = the current time."""
    markers = request_markers(connection, TARGETS_BY_IDS, [round_ids])
    marker_ids = list(markers)
    params = [round_ids, observed_at, marker_ids, [markers[key] for key in marker_ids]]
    sql = f"""{TARGETS_BY_IDS},
        fact_now AS MATERIALIZED (SELECT COALESCE(%s::timestamptz, CURRENT_TIMESTAMP) AS at),
        fact_markers AS (SELECT * FROM unnest(%s::text[],%s::bigint[]) AS t(round_id,message_id)),
        {fact_ctes('fact_targets', 'fact_markers', '(SELECT at FROM fact_now)')}"""
    return sql, params


def store(connection, round_ids) -> int:
    """Recompute and replace stored facts for these rounds inside the caller's transaction."""
    round_ids = sorted({str(value) for value in round_ids if value})
    if not round_ids:
        return 0
    connection.execute("DELETE FROM ops.round_service_facts WHERE round_id=ANY(%s)", (round_ids,))
    with_facts, params = _with_facts(connection, round_ids)
    names = ",".join(FACT_NAMES)
    inserted = connection.execute(f"""{with_facts}
        INSERT INTO ops.round_service_facts({names},fact_version)
        SELECT {names},{FACT_VERSION} FROM fact_rows
    """, params).rowcount
    connection.execute(f"""{with_facts}
        INSERT INTO ops.round_reply_hours({",".join(HOUR_NAMES)})
        SELECT {",".join(HOUR_NAMES)} FROM fact_hour_rows
    """, params)
    return inserted


LIVE_BATCH = 5000


def live_facts(connection, round_ids, observed_at) -> tuple[str, str]:
    """Facts of these rounds as seen at observed_at, by the stored rule, as JSON text.

    Returns (rows_json, hours_json) for FactSource; nothing is written, so the
    read-only report/archive connections can use it.
    """
    rows, hours = [], []
    round_ids = sorted({str(value) for value in round_ids if value})
    for offset in range(0, len(round_ids), LIVE_BATCH):
        batch = round_ids[offset:offset + LIVE_BATCH]
        with_facts, params = _with_facts(connection, batch, observed_at)
        row = connection.execute(f"""{with_facts}
            SELECT (SELECT COALESCE(jsonb_agg(to_jsonb(x)),'[]'::jsonb) FROM fact_rows x)::text AS rows,
                (SELECT COALESCE(jsonb_agg(to_jsonb(x)),'[]'::jsonb) FROM fact_hour_rows x)::text AS hours
        """, params).fetchone()
        rows.append(row["rows"])
        hours.append(row["hours"])
    if not rows:
        return "[]", "[]"
    if len(rows) == 1:
        return rows[0], hours[0]
    join = lambda parts: "[" + ",".join(part[1:-1] for part in parts if part != "[]") + "]"
    return join(rows), join(hours)


def _typed(columns) -> str:
    return ",".join(f"{name} {kind}" for name, kind in columns)


class FactSource:
    """Facts of the rounds a reader needs, as one relation: stored current rows
    plus live rows for rounds without current stored facts at `observed_at`.

    Readers never see whether a round came from the table or was computed live.
    """

    def __init__(self, observed_at, rows_json: str = "[]", hours_json: str = "[]", live_ids=()):
        self.observed_at = observed_at
        self.rows_json = rows_json
        self.hours_json = hours_json
        self.live_ids = tuple(live_ids)

    @classmethod
    def load(cls, connection, observed_at, *, start=None, end=None, scope: str = "TRUE", scope_params=(),
             round_filter: str | None = None, round_params=()):
        """Find rounds lacking current facts (overlapping [start,end) and/or a
        round_index condition on alias r) and compute them live."""
        clauses, params = [scope], list(scope_params)
        if end is not None:
            clauses.append("r.started_at < %s")
            params.append(end)
        if start is not None:
            clauses.append("(r.last_message_at >= %s OR ops.round_handoff_at(r.data,r.started_at) >= %s"
                           " OR ops.archive_ts(r.data->>'responsible_assigned_at') >= %s)")
            params.extend([start, start, start])
        if round_filter:
            clauses.append(round_filter)
            params.extend(round_params)
        # Rounds without current facts at observed_at, found from small sets only
        # (migration 020): rounds with no fact row are queued in round_fact_pending,
        # plus rows of another rule version and rows with messages at/after observed_at.
        missing = [row["round_id"] for row in connection.execute(f"""
            SELECT r.round_id FROM ops.round_index r
            WHERE {' AND '.join(clauses)} AND r.round_id IN (
                SELECT p.round_id FROM ops.round_fact_pending p
                UNION ALL SELECT f.round_id FROM ops.round_service_facts f
                    WHERE f.fact_version < %s OR f.fact_version > %s
                UNION ALL SELECT f.round_id FROM ops.round_service_facts f WHERE f.messages_until >= %s::timestamptz)
        """, [*params, FACT_VERSION, FACT_VERSION, observed_at]).fetchall()]
        if not missing:
            return cls(observed_at)
        rows, hours = live_facts(connection, missing, observed_at)
        return cls(observed_at, rows, hours, missing)

    @property
    def has_live(self) -> bool:
        return bool(self.live_ids)

    def facts(self, where: str = "TRUE", params=(), alias: str = "f") -> tuple[str, list]:
        """(subquery, params): fact rows matching `where` (written against alias f)."""
        names = ",".join(f"f.{name}" for name in FACT_NAMES)
        sql = (f"SELECT {names} FROM ops.round_service_facts f WHERE "
               f"{fact_is_current('f', 'COALESCE(%s::timestamptz, CURRENT_TIMESTAMP)')} AND ({where})")
        values = [self.observed_at, *params]
        if self.has_live:
            sql += (f" UNION ALL SELECT {names} FROM jsonb_to_recordset(%s::jsonb) AS f({_typed(FACT_COLUMNS)})"
                    f" WHERE ({where})")
            values.extend([self.rows_json, *params])
        return f"({sql}) {alias}", values

    def hours(self, where: str = "TRUE", params=(), alias: str = "h") -> tuple[str, list]:
        """(subquery, params): reply-hour rows of current facts matching `where` (alias h)."""
        names = ",".join(f"h.{name}" for name in HOUR_NAMES)
        sql = (f"SELECT {names} FROM ops.round_reply_hours h JOIN ops.round_service_facts cf"
               f" ON cf.round_id=h.round_id AND {fact_is_current('cf', 'COALESCE(%s::timestamptz, CURRENT_TIMESTAMP)')}"
               f" WHERE ({where})")
        values = [self.observed_at, *params]
        if self.has_live:
            sql += (f" UNION ALL SELECT {names} FROM jsonb_to_recordset(%s::jsonb) AS h({_typed(HOUR_COLUMNS)})"
                    f" WHERE ({where})")
            values.extend([self.hours_json, *params])
        return f"({sql}) {alias}", values


def delete(connection, round_ids) -> None:
    round_ids = [str(value) for value in round_ids if value]
    if round_ids:
        connection.execute("DELETE FROM ops.round_service_facts WHERE round_id=ANY(%s)", (round_ids,))


class FactBackfill:
    """Fill facts for rounds projected before facts existed, newest first, in small idle-time batches.

    Keyset pagination over round_index_started_idx keeps each batch bounded no
    matter how much has already been filled; the cursor lives in memory, so a
    restart rescans quickly through rows that already have current facts.
    Each conversation is locked exactly like projection, so a concurrent replay
    can never be overwritten by an older backfill read.
    """

    def __init__(self, pool, scope_lock, batch_size=200, pause_seconds=0.2, batch_budget_seconds=90):
        self.pool = pool
        self.scope_lock = scope_lock
        self.batch_size = batch_size
        self.pause_seconds = pause_seconds
        self.batch_budget_seconds = batch_budget_seconds
        self.cursor = None
        self.done = False
        # Conversations locked by a live replay during their batch; retried at the end.
        self.skipped = {}

    def step(self) -> bool:
        """整批共用预算，失败时保留游标和待重试会话，避免跳过未完成数据。"""
        if self.done:
            return False
        budget = MaintenanceBudget(self.batch_budget_seconds, statement_seconds=30)
        cursor, done = self.cursor, self.done
        skipped = {scope: list(ids) for scope, ids in self.skipped.items()}
        try:
            return self._step(budget)
        except BaseException:
            self.cursor, self.done, self.skipped = cursor, done, skipped
            raise

    def _step(self, budget):
        with budget.connection(self.pool) as connection:
            # 根据开始时间倒序扫描缺失事实或事实版本过期的轮次。
            rows = connection.execute("""
                SELECT r.round_id,r.source_key,r.account_id,r.conversation_id,r.started_at
                FROM ops.round_index r
                WHERE r.round_id IN (SELECT p.round_id FROM ops.round_fact_pending p
                        UNION ALL SELECT f.round_id FROM ops.round_service_facts f
                        WHERE f.fact_version < %(version)s OR f.fact_version > %(version)s)
                    AND (%(at)s::timestamptz IS NULL OR (r.started_at,r.round_id) < (%(at)s::timestamptz,%(id)s::text))
                ORDER BY r.started_at DESC,r.round_id DESC LIMIT %(limit)s
            """, {"at": self.cursor[0] if self.cursor else None, "id": self.cursor[1] if self.cursor else None,
                  "limit": self.batch_size, "version": FACT_VERSION}).fetchall()
        if not rows:
            if not self.skipped:
                with budget.connection(self.pool) as connection:
                    # 清理已被重放替换或删除的轮次队列项，成功提交后再标记完成。
                    connection.execute("""DELETE FROM ops.round_fact_pending p
                        WHERE NOT EXISTS (SELECT 1 FROM ops.round_index r WHERE r.round_id=p.round_id)""")
                self.done = True
                logger.info("Archive service facts backfill complete")
                return False
            pending, remaining, capacity = {}, {}, self.batch_size
            for scope, ids in self.skipped.items():
                count = min(capacity, len(ids))
                if count:
                    pending[scope] = ids[:count]
                    capacity -= count
                if count < len(ids):
                    remaining[scope] = ids[count:]
            # 事务成功后才移除已提交的部分；仍锁忙的会话移到队尾轮转。
            blocked = self._store(pending, retry=False, budget=budget)
            for scope, ids in blocked.items():
                remaining[scope] = list(dict.fromkeys([*remaining.pop(scope, []), *ids]))
            self.skipped = remaining
            return True
        pending = {}
        for row in rows:
            pending.setdefault((row["source_key"], row["account_id"], row["conversation_id"]), []).append(row["round_id"])
        self._store(pending, retry=True, budget=budget)
        budget.check()
        self.cursor = (rows[-1]["started_at"], rows[-1]["round_id"])
        if pending and self.pause_seconds:
            time.sleep(min(self.pause_seconds, budget.check()))
            budget.check()
        return True

    def _store(self, pending, *, retry, budget=None):
        blocked = {}
        if pending:
            budget = budget or MaintenanceBudget(self.batch_budget_seconds, statement_seconds=30)
            with budget.connection(self.pool) as connection:
                locked = []
                for scope, round_ids in pending.items():
                    # 锁忙的会话始终保留待重试，不能在最后一轮直接丢弃。
                    if connection.execute("SELECT pg_try_advisory_xact_lock(%s) AS locked",
                                          (self.scope_lock(*scope),)).fetchone()["locked"]:
                        locked.extend(round_ids)
                    else:
                        blocked[scope] = list(round_ids)
                if locked:
                    # 轮次可能已在批次读取后被重放替换。
                    store(connection, [row["round_id"] for row in connection.execute(
                        "SELECT round_id FROM ops.round_index WHERE round_id=ANY(%s)", (locked,)).fetchall()])
            if retry:
                for scope, ids in blocked.items():
                    self.skipped[scope] = list(dict.fromkeys([*self.skipped.get(scope, []), *ids]))
        return blocked
