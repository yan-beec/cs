"""Durable, bounded recent-round replay. Every mutation belongs to the caller's transaction."""

import uuid

from psycopg import sql
from psycopg.types.json import Jsonb

from .projector import is_rating, obj, stable_id
from .windows import event_locator, message_locators, round_locators


SCOPE = "source_key=%s AND account_id=%s AND conversation_id=%s"


def related(left, right):
    """Trusted SQL aliases only; link keys never cross a conversation scope."""
    return f"""({left}.event_id={right}.event_id OR
        ({left}.data->>'assignment_key' IS NOT NULL AND
         {left}.data->>'assignment_key'={right}.data->>'assignment_key') OR
        ({left}.data->>'rating'='true' AND {right}.data->>'rating'='true' AND (
            {left}.anchor_message_id={right}.anchor_message_id OR
            {left}.data->>'rating_external'={right}.data->>'rating_external')))"""


class RoundReplay:
    def __init__(self, repository):
        self.repository = repository
        self.settings = repository.settings

    def save_plan(self, connection, scope, plan):
        from .repository import json_safe
        connection.execute("""INSERT INTO ops.conversation_replay_state
            (source_key,account_id,conversation_id,plan) VALUES(%s,%s,%s,%s)
            ON CONFLICT(source_key,account_id,conversation_id) DO UPDATE
            SET plan=EXCLUDED.plan,updated_at=CURRENT_TIMESTAMP""", (*scope, Jsonb(json_safe(plan))))

    def step(self, connection, scope, claim=None, *, rebuild=False):
        stored = connection.execute(f"SELECT plan FROM ops.conversation_replay_state WHERE {SCOPE}", scope).fetchone()
        plan = stored["plan"] if stored else {}
        if rebuild or not plan:
            high_water = connection.execute(f"SELECT max(id) AS id FROM archive.events WHERE {SCOPE}", scope).fetchone()["id"]
            plan = {"high_water": high_water, "indexed": False, "index_cursor": 0,
                    "cursor": None, "seed": {}, "rebuild": rebuild}
        if not plan["indexed"]:
            if not self.index_batch(connection, scope, plan):
                self.save_plan(connection, scope, plan)
                return False
            self.refresh_locators(connection, scope, plan["high_water"], plan["rebuild"])
            plan["indexed"] = True
            plan["cursor"], plan["seed"] = self.start_cursor(connection, scope, plan)
        done = self.project_window(connection, scope, plan)
        if done:
            self.complete(connection, scope, plan, claim)
            self.save_plan(connection, scope, {})
        else:
            self.save_plan(connection, scope, plan)
        return done

    def index_batch(self, connection, scope, plan):
        # Select IDs/byte lengths first. A page of 500 maximum-sized payloads
        # must not be materialized before its byte budget has been checked.
        rows = connection.execute("""
            WITH candidates AS (
                SELECT e.id,octet_length(e.payload::text) AS bytes FROM archive.events e
                WHERE e.source_key=%s AND e.account_id=%s AND e.conversation_id=%s AND e.id<=%s
                  AND ((%s AND e.id>%s) OR (NOT %s AND
                    NOT EXISTS(SELECT 1 FROM ops.event_replay_index x WHERE x.event_id=e.id)))
                ORDER BY e.id LIMIT %s
            ), bounded AS (
                SELECT id,row_number() OVER(ORDER BY id) AS n,sum(bytes) OVER(ORDER BY id) AS bytes
                FROM candidates
            )
            SELECT e.id,e.source_key,e.account_id,e.conversation_id,e.message_id,e.event_type,
                e.ingestion_kind,e.source_time,e.received_at,e.payload,
                octet_length(e.payload::text) AS payload_bytes
            FROM bounded b JOIN archive.events e ON e.id=b.id
            WHERE b.bytes<=%s OR b.n=1 ORDER BY e.id
        """, (*scope, plan["high_water"], plan["rebuild"], plan["index_cursor"], plan["rebuild"], self.settings.processor_index_batch_size,
                self.settings.processor_index_batch_bytes)).fetchall()
        self.bulk_upsert(connection, "ops.event_replay_index", (event_locator(row) for row in rows), ("event_id",))
        if rows:
            plan["index_cursor"] = rows[-1]["id"]
        return not connection.execute("""SELECT EXISTS(
            SELECT 1 FROM archive.events e WHERE e.source_key=%s AND e.account_id=%s
              AND e.conversation_id=%s AND e.id<=%s
              AND ((%s AND e.id>%s) OR (NOT %s AND
                NOT EXISTS(SELECT 1 FROM ops.event_replay_index x WHERE x.event_id=e.id)))
        ) AS remaining""", (*scope, plan["high_water"], plan["rebuild"], plan["index_cursor"], plan["rebuild"])).fetchone()["remaining"]

    @staticmethod
    def bulk_upsert(connection, table, rows, conflict):
        rows = iter(rows)
        first = next(rows, None)
        if first is None:
            return
        columns = list(first)
        query = sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT ({}) DO UPDATE SET {}").format(
            sql.Identifier(*table.split(".")), sql.SQL(",").join(map(sql.Identifier, columns)),
            sql.SQL(",").join(sql.Placeholder() for _ in columns), sql.SQL(",").join(map(sql.Identifier, conflict)),
            sql.SQL(",").join(sql.SQL("{}=EXCLUDED.{}").format(sql.Identifier(key), sql.Identifier(key))
                              for key in columns if key not in conflict))
        from itertools import chain, islice
        from .repository import ProcessingRepository
        with connection.cursor() as cursor:
            pending = chain((first,), rows)
            while batch := list(islice(pending, 500)):
                cursor.executemany(query, [ProcessingRepository._values(row) for row in batch])

    def refresh_locators(self, connection, scope, high_water, rebuild):
        # Only changed logical messages need their version metadata replayed.
        # The server cursor and grouping keep even a heavily edited message bounded.
        with connection.cursor(name="replay_messages_" + uuid.uuid4().hex) as cursor:
            cursor.execute("""SELECT x.* FROM ops.event_replay_index x
                WHERE x.source_key=%s AND x.account_id=%s AND x.conversation_id=%s
                  AND x.event_id<=%s AND x.message_id IS NOT NULL AND (%s OR NOT EXISTS(
                    SELECT 1 FROM ops.replay_message_index old WHERE old.source_key=x.source_key
                      AND old.account_id=x.account_id AND old.conversation_id=x.conversation_id
                      AND old.message_id=x.message_id) OR EXISTS(
                    SELECT 1 FROM ops.event_replay_index p JOIN ops.event_jobs j ON j.event_id=p.event_id
                    WHERE p.source_key=x.source_key AND p.account_id=x.account_id
                      AND p.conversation_id=x.conversation_id AND p.message_id=x.message_id
                      AND p.event_id<=%s AND j.status<>'completed'))
                ORDER BY x.message_id,x.event_at,x.event_id
            """, (*scope, high_water, rebuild, high_water))
            def observations():
                while batch := cursor.fetchmany(500):
                    yield from batch
            rows = ({"source_key": scope[0], "account_id": scope[1], "conversation_id": scope[2], **row}
                    for row in message_locators(observations()))
            self.bulk_upsert(connection, "ops.replay_message_index", rows,
                             ("source_key", "account_id", "conversation_id", "message_id"))
        contact = connection.execute(f"""SELECT contact_id FROM ops.event_replay_index
            WHERE {SCOPE} AND event_id<=%s AND contact_id IS NOT NULL
            ORDER BY event_at DESC,event_id DESC LIMIT 1""", (*scope, high_water)).fetchone()
        customer_id = (stable_id("customer:", scope[0], scope[1], "contact", contact["contact_id"]) if contact
                       else stable_id("customer:", scope[0], scope[1], "conversation", scope[2]))
        connection.execute(f"DELETE FROM ops.replay_round_index WHERE {SCOPE}", scope)
        with connection.cursor(name="replay_rounds_" + uuid.uuid4().hex) as cursor:
            cursor.execute("""SELECT creation_at AS at,'customer' AS kind,message_id,0 AS tie
                FROM ops.replay_message_index WHERE source_key=%s AND account_id=%s AND conversation_id=%s
                  AND is_customer_creation
                UNION ALL
                SELECT state_at,'resolution',event_id,1 FROM ops.event_replay_index
                WHERE source_key=%s AND account_id=%s AND conversation_id=%s
                  AND event_id<=%s AND is_resolution
                ORDER BY at,tie,message_id
            """, (*scope, *scope, high_water))
            def actions():
                while batch := cursor.fetchmany(500):
                    yield from batch
            self.bulk_upsert(connection, "ops.replay_round_index",
                             round_locators(actions(), scope, customer_id, self.settings.idle_timeout_seconds),
                             ("source_key", "account_id", "conversation_id", "round_id"))
        # Keep immutable round IDs, and update their display ordinals atomically.
        # This is metadata-only work; old message bodies are never loaded here.
        connection.execute("""UPDATE ops.round_index r SET round_no=a.round_no
            FROM ops.replay_round_index a WHERE a.source_key=%s AND a.account_id=%s AND a.conversation_id=%s
              AND r.round_id=a.round_id AND r.round_no<>a.round_no""", scope)
        obsolete = connection.execute("""SELECT r.round_id FROM ops.round_index r
            WHERE r.source_key=%s AND r.account_id=%s AND r.conversation_id=%s
              AND NOT EXISTS(SELECT 1 FROM ops.replay_round_index a
                  WHERE a.source_key=r.source_key AND a.account_id=r.account_id
                    AND a.conversation_id=r.conversation_id AND a.round_id=r.round_id)
        """, scope).fetchall()
        ids = [row["round_id"] for row in obsolete]
        if ids:
            # A late creation can merge/split old rounds. Keep their messages and
            # ratings visible while the affected suffix is repaired in batches.
            connection.execute("UPDATE ops.message_index SET round_id=NULL WHERE round_id=ANY(%s)", (ids,))
            connection.execute("UPDATE ops.rating_index SET round_id=NULL,status='unlinked' WHERE round_id=ANY(%s)", (ids,))
            connection.execute("DELETE FROM ops.round_agent_index WHERE round_id=ANY(%s)", (ids,))
            connection.execute("DELETE FROM ops.round_index WHERE round_id=ANY(%s)", (ids,))

    def start_cursor(self, connection, scope, plan):
        if plan["rebuild"]:
            return 1, {}
        # A customer's window can span several Chatwoot conversations. Each
        # conversation retains its own state machine and ordinal sequence.
        recent = connection.execute("""WITH customer AS (
                SELECT customer_id FROM ops.replay_round_index
                WHERE source_key=%s AND account_id=%s AND conversation_id=%s LIMIT 1
            ), recent AS (
                SELECT round_id,started_at,conversation_id FROM ops.replay_round_index
                WHERE source_key=%s AND account_id=%s AND conversation_id=%s
                UNION ALL
                SELECT round_id,started_at,conversation_id FROM ops.round_index
                WHERE customer_id=(SELECT customer_id FROM customer) AND conversation_id<>%s
            ), selected AS (
                SELECT * FROM recent ORDER BY started_at DESC,round_id DESC LIMIT %s
            )
            SELECT min(a.round_no) AS n FROM ops.replay_round_index a JOIN selected s ON s.round_id=a.round_id
            WHERE a.source_key=%s AND a.account_id=%s AND a.conversation_id=%s
        """, (*scope, *scope, scope[2], self.settings.processor_recent_rounds, *scope)).fetchone()["n"]
        facts, params = self.facts(scope, plan["high_water"])
        dirty = connection.execute(facts + f"""SELECT min(LEAST(x.event_at,x.state_at,x.message_at,
                x.link_at,x.assignment_started_at,x.assignment_at,
                CASE WHEN x.data->>'assignment_key' IS NOT NULL OR x.data->>'rating'='true' THEN
                    (SELECT min(LEAST(p.link_at,p.assignment_started_at,p.assignment_at))
                     FROM facts p WHERE {related('p', 'x')}) END)) AS at
            FROM facts x WHERE x.job_status<>'completed'
        """, params).fetchone()["at"]
        affected = connection.execute(f"""SELECT round_no FROM ops.replay_round_index
            WHERE {SCOPE} AND started_at<=%s ORDER BY round_no DESC LIMIT 1""", (*scope, dirty)).fetchone() if dirty else None
        first = connection.execute(f"SELECT min(started_at) AS at FROM ops.replay_round_index WHERE {SCOPE}", scope).fetchone()["at"]
        target = min(recent or 2**31-1, affected["round_no"] if affected else 1)
        if dirty and first and dirty < first:
            target = 1
        checkpoint = connection.execute("""SELECT a.round_no,r.data->'replay_seed' AS seed
            FROM ops.replay_round_index a JOIN ops.round_index r ON r.round_id=a.round_id
            WHERE a.source_key=%s AND a.account_id=%s AND a.conversation_id=%s AND a.round_no<=%s
              AND r.data->'replay_seed'->>'schema_version'='1'
            ORDER BY a.round_no DESC LIMIT 1""", (*scope, target)).fetchone()
        return (checkpoint["round_no"], checkpoint["seed"]) if checkpoint else (1, {})

    @staticmethod
    def facts(scope, high_water):
        query = """WITH raw_facts AS (
            SELECT x.*,j.status AS job_status,m.sent_at AS message_at,am.sent_at AS anchor_at,
                a.started_at AS explicit_round_at,(x.data->>'assignment_at')::timestamptz AS assignment_at
            FROM ops.event_replay_index x JOIN ops.event_jobs j ON j.event_id=x.event_id
            LEFT JOIN ops.replay_message_index m ON m.source_key=x.source_key AND m.account_id=x.account_id
                AND m.conversation_id=x.conversation_id AND m.message_id=x.message_id
            LEFT JOIN ops.replay_message_index am ON am.source_key=x.source_key AND am.account_id=x.account_id
                AND am.conversation_id=x.conversation_id AND am.message_id=x.anchor_message_id
            LEFT JOIN ops.replay_round_index a ON a.source_key=x.source_key AND a.account_id=x.account_id
                AND a.conversation_id=x.conversation_id AND a.round_id=x.data->>'rating_external'
                AND EXISTS(SELECT 1 FROM ops.event_replay_index resolution
                    WHERE resolution.source_key=a.source_key AND resolution.account_id=a.account_id
                      AND resolution.conversation_id=a.conversation_id AND resolution.is_resolution
                      AND resolution.event_id<=%s AND resolution.state_at>=a.started_at
                      AND NOT EXISTS(SELECT 1 FROM ops.replay_round_index later
                          WHERE later.source_key=a.source_key AND later.account_id=a.account_id
                            AND later.conversation_id=a.conversation_id AND later.round_no>a.round_no
                            AND later.started_at<=resolution.state_at))
            WHERE x.source_key=%s AND x.account_id=%s AND x.conversation_id=%s AND x.event_id<=%s
        ), facts AS (
            SELECT x.*,COALESCE(x.explicit_round_at,x.closed_at,x.anchor_at,
                CASE WHEN x.data->>'rating'='true' THEN
                    (SELECT min(COALESCE(p.explicit_round_at,p.closed_at,p.anchor_at))
                     FROM raw_facts p WHERE """ + related('p', 'x') + """ ) END,
                x.assignment_started_at,x.event_at) AS link_at FROM raw_facts x
        ) """
        return query, (high_water, *scope, high_water)

    @classmethod
    def window_selection(cls, scope, high_water, start, end):
        # Component flags prevent an old message embedded in a fresh state
        # observation from being counted in the wrong replay window.
        query, params = cls.facts(scope, high_water)
        bounds = []
        def inside(column):
            bounds.extend((start, start, end, end))
            return f"((%s::timestamptz IS NULL OR {column}>=%s) AND (%s::timestamptz IS NULL OR {column}<%s))"
        message = f"(x.message_at IS NOT NULL AND {inside('x.message_at')}) OR (x.message_at IS NULL AND {inside('x.event_at')})"
        state = inside('x.state_at')
        link = inside('p.link_at')
        session = inside('p.assignment_started_at')
        assigned = inside('p.assignment_at')
        fallback = inside('x.link_at')
        query += f""", located AS (
            SELECT x.*,
                {message} AS message_in_window,{state} AS state_in_window,
                CASE WHEN x.data->>'assignment_key' IS NOT NULL OR x.data->>'rating'='true' THEN
                    EXISTS(SELECT 1 FROM facts p WHERE {related('p', 'x')} AND
                        ({link} OR (x.data->>'assignment_key' IS NOT NULL AND ({session} OR {assigned}))))
                    ELSE {fallback} END AS rating_in_window
            FROM facts x
        ), selected AS (
            SELECT * FROM located WHERE message_in_window OR state_in_window OR rating_in_window
        ) """
        return query, (*params, *bounds)

    def rating_context(self, connection, scope, high_water, events):
        """Global proof sets are tiny (two matches suffice to prove a conflict)."""
        locators = [event_locator({**event, "payload_bytes": 0}) for event in events
                    if event["_rating_in_window"] and is_rating({**obj(event["payload"].get("message")), **event["payload"]})]
        invitations = [row["anchor_message_id"] for row in locators if row["data"]["rating"] and row["anchor_message_id"]]
        aliases = [row["data"]["rating_external"] for row in locators if row["data"]["rating_external"]]
        context = {"invitations": {}, "aliases": {}, "unique": {}, "matched": set()}
        selected_ids = {row["event_id"] for row in locators}
        if not selected_ids:
            return context
        allowed = {"invitations": set(invitations), "aliases": set(aliases)}
        # Boundaries come from canonical metadata, including rounds not yet
        # projected during bootstrap. No historical bodies are read here.
        with connection.cursor(name="rating_proofs_" + uuid.uuid4().hex) as cursor:
            cursor.execute("""WITH ordered AS (
                SELECT round_id,started_at,lead(started_at) OVER(ORDER BY round_no) AS following
                FROM ops.replay_round_index WHERE source_key=%s AND account_id=%s AND conversation_id=%s
            ), resolved AS (
                SELECT a.round_id,(SELECT min(x.state_at) FROM ops.event_replay_index x
                    WHERE x.source_key=%s AND x.account_id=%s AND x.conversation_id=%s
                      AND x.event_id<=%s AND x.is_resolution AND x.state_at>=a.started_at
                      AND (a.following IS NULL OR x.state_at<a.following)) AS closed_at FROM ordered a
            ) SELECT x.event_id,x.anchor_message_id,x.data->>'rating_external' AS alias,
                    min(r.round_id) AS round_id,count(*) AS match_count
                FROM ops.event_replay_index x JOIN resolved r ON r.closed_at IS NOT NULL AND (
                    r.round_id=x.data->>'rating_external' OR
                    (x.closed_at BETWEEN r.closed_at-INTERVAL '1 millisecond' AND r.closed_at+INTERVAL '1 millisecond'
                     AND NOT EXISTS(SELECT 1 FROM resolved direct WHERE direct.round_id=x.data->>'rating_external'
                                    AND direct.closed_at IS NOT NULL)))
                WHERE x.source_key=%s AND x.account_id=%s AND x.conversation_id=%s AND x.event_id<=%s
                  AND x.data->>'rating'='true' AND
                    (x.anchor_message_id=ANY(%s) OR x.data->>'rating_external'=ANY(%s) OR x.event_id=ANY(%s))
                GROUP BY x.event_id,x.anchor_message_id,x.data->>'rating_external'
            """, (*scope, *scope, high_water, *scope, high_water, invitations, aliases, list(selected_ids)))
            while rows := cursor.fetchmany(500):
                for row in rows:
                    if row["event_id"] in selected_ids:
                        context["matched"].add(row["event_id"])
                        if row["match_count"] == 1:
                            context["unique"][row["event_id"]] = row["round_id"]
                    if row["match_count"] != 1:
                        continue
                    for group, key in (("invitations", row["anchor_message_id"]), ("aliases", row["alias"])):
                        if key in allowed[group]:
                            matches = context[group].setdefault(key, set())
                            if len(matches) < 2:
                                matches.add(row["round_id"])
        return context

    def assignment_context(self, connection, scope, high_water, events):
        from .projector import _assignment
        evidence = [item for event in events if (item := _assignment(event)) is not None]
        context = {"rounds": [], "messages": [], "names": {}}
        if not evidence:
            return context
        triggers = list({item["trigger_message_id"] for item in evidence})
        identity = ",".join(f"""(SELECT (x.data->>'{key}'){cast} FROM ops.event_replay_index x
            WHERE x.source_key=m.source_key AND x.account_id=m.account_id AND x.conversation_id=m.conversation_id
                AND x.message_id=m.message_id AND x.event_id<=%s AND x.data->>'{key}' IS NOT NULL
                AND x.data->>'{key}'<>'unknown' ORDER BY x.event_at DESC,x.event_id DESC LIMIT 1) AS {key}"""
            for key, cast in (("role", ""), ("direction", ""), ("private", "::boolean"), ("rating", "::boolean")))
        rows = connection.execute(f"""WITH ordered AS (
            SELECT *,lead(started_at) OVER(ORDER BY round_no) AS following
            FROM ops.replay_round_index WHERE source_key=%s AND account_id=%s AND conversation_id=%s
        ) SELECT m.*,a.round_id,a.started_at,a.following,
            (SELECT min(x.state_at) FROM ops.event_replay_index x
                WHERE x.source_key=m.source_key AND x.account_id=m.account_id AND x.conversation_id=m.conversation_id
                  AND x.event_id<=%s AND x.is_resolution AND x.state_at>=a.started_at
                  AND (a.following IS NULL OR x.state_at<a.following)) AS closed_at,
            identity.role,identity.direction,identity.private,identity.rating
        FROM ops.replay_message_index m LEFT JOIN LATERAL (
            SELECT * FROM ordered a WHERE a.started_at<=COALESCE(m.creation_at,m.sent_at)
            ORDER BY a.round_no DESC LIMIT 1
        ) a ON TRUE LEFT JOIN LATERAL (
            SELECT {identity}
        ) identity ON TRUE
        WHERE m.source_key=%s AND m.account_id=%s AND m.conversation_id=%s AND m.message_id=ANY(%s)
        """, (*scope, high_water, *((high_water,) * 4), *scope, triggers)).fetchall()
        for row in rows:
            public_customer = row["is_customer_creation"] or (row["creation_event_id"] is None
                and row["role"] == "contact" and row["direction"] == "incoming"
                and not row["private"] and not row["rating"])
            context["messages"].append({"message_id": row["message_id"], "round_id": row["round_id"],
                "creation_event_id": None, "sent_at": row["creation_at"] or row["sent_at"],
                "sender_role": "contact" if public_customer else "unknown", "direction": "incoming",
                "is_private": False, "data": {"is_rating": False}})
            if row["round_id"]:
                context["rounds"].append({"round_id": row["round_id"], "started_at": row["started_at"],
                    "data": {"closed_at": row["closed_at"]}, "_following_started_at": row["following"]})
        agents = list({item["agent_id"] for item in evidence})
        names = connection.execute("""SELECT DISTINCT ON (data->>'sender_id')
                data->>'sender_id' AS id,data->>'sender_name' AS name FROM ops.event_replay_index
            WHERE source_key=%s AND account_id=%s AND conversation_id=%s AND event_id<=%s
                AND data->>'role'='agent' AND data->>'sender_id'=ANY(%s) AND data->>'sender_name' IS NOT NULL
            ORDER BY data->>'sender_id',event_at DESC,event_id DESC
        """, (*scope, high_water, agents)).fetchall()
        context["names"] = {row["id"]: row["name"] for row in names}
        return context

    def project_window(self, connection, scope, plan):
        anchors = connection.execute(f"""SELECT * FROM ops.replay_round_index WHERE {SCOPE} AND round_no>=%s
            ORDER BY round_no LIMIT %s""", (*scope, plan["cursor"], self.settings.processor_recent_rounds + 1)).fetchall()
        size = min(self.settings.processor_recent_rounds, len(anchors))
        while True:
            following = anchors[size] if size < len(anchors) else None
            start = anchors[0]["started_at"] if anchors and plan["cursor"] > 1 else None
            end = following["started_at"] if following else None
            selection, params = self.window_selection(scope, plan["high_water"], start, end)
            budget = connection.execute(selection + "SELECT count(*) AS n,COALESCE(sum(payload_bytes),0) AS bytes FROM selected", params).fetchone()
            if budget["n"] <= self.settings.processor_max_conversation_events and budget["bytes"] <= self.settings.processor_max_conversation_bytes:
                break
            if size <= 1:
                bound = "byte bound" if budget["bytes"] > self.settings.processor_max_conversation_bytes else "event bound"
                raise RuntimeError("Single replay round exceeds configured " + bound)
            size = max(1, size // 2)
        events = connection.execute(selection + """SELECT e.id,e.source_key,e.account_id,e.conversation_id,
                e.message_id,e.event_type,e.ingestion_kind,e.source_time,e.received_at,e.payload,
                s.message_in_window AS _message_in_window,s.state_in_window AS _state_in_window,
                s.rating_in_window AS _rating_in_window
            FROM selected s JOIN archive.events e ON e.id=s.event_id ORDER BY e.id
        """, params).fetchall()
        if events:
            projection = self.repository._write_projection(connection, events, replay_seed=plan["seed"],
                round_no_offset=plan["cursor"] - 1, following_started_at=end, window=(start, end),
                rating_context=self.rating_context(connection, scope, plan["high_water"], events),
                assignment_context=self.assignment_context(connection, scope, plan["high_water"], events))
            expected = [row["round_id"] for row in anchors[:size]]
            if [row["round_id"] for row in projection.rounds] != expected:
                raise RuntimeError("Replay locator and source round boundaries disagree")
            plan["seed"] = projection.continuation
        if following:
            plan["cursor"] = following["round_no"]
            return False
        return True

    @staticmethod
    def complete(connection, scope, plan, claim):
        # Join the locator, not an ID watermark alone: a lower sequence ID may
        # commit late and must keep its durable job until actually indexed.
        connection.execute("""UPDATE ops.event_jobs j SET status='completed',locked_by=NULL,locked_until=NULL,
            completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP,last_error=NULL
            FROM ops.event_replay_index x WHERE x.event_id=j.event_id
              AND x.source_key=%s AND x.account_id=%s AND x.conversation_id=%s AND x.event_id<=%s
              AND (j.status IN ('queued','retry') OR (%s AND j.status='failed') OR
                (j.event_id=%s AND j.status='processing' AND j.locked_by=%s))
        """, (*scope, plan["high_water"], plan["rebuild"],
              claim["event_id"] if claim else None, claim["locked_by"] if claim else None))
