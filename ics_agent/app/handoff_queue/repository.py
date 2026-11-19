"""独立小连接池及排队三张表的原子读写。"""
from datetime import datetime, timedelta, timezone
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg.errors import UniqueViolation
from psycopg_pool import ConnectionPool
from .policy import DEFAULT_CONFIG, week_anchor, success_counts

class ConfigConflict(RuntimeError):
    pass

class HandoffQueueRepository:

    def __init__(self, settings, pool=None):
        if pool is None:
            common = {
                "connect_timeout": 3, "application_name": "ics-handoff-queue",
                "options": "-c statement_timeout=3000 -c lock_timeout=2000", "row_factory": dict_row,
            }
            source = {"conninfo": settings.postgres_dsn, "kwargs": common} if settings.postgres_dsn else {
                "kwargs": {
                    **common, "host": settings.postgres_host, "port": settings.postgres_port,
                    "dbname": settings.postgres_database, "user": settings.postgres_user,
                    "password": settings.postgres_password, "sslmode": settings.postgres_sslmode,
                },
            }
            pool = ConnectionPool(**source, min_size=0, max_size=2, timeout=3, max_waiting=8, open=False)
        self.pool = pool

    def open(self):
        self.pool.open(wait=False)

    def close(self):
        self.pool.close()

    def ensure_tables(self):
        with self.pool.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('ics-handoff-queue-schema'))")
            conn.execute("""CREATE TABLE IF NOT EXISTS ics_handoff_queue_event_outbox (
                account_id TEXT NOT NULL, conversation_id TEXT NOT NULL, trigger_message_id TEXT NOT NULL,
                contact_id TEXT NOT NULL, trigger_at TIMESTAMPTZ NOT NULL, decided_at TIMESTAMPTZ NOT NULL,
                event_key TEXT NOT NULL UNIQUE, payload_version INTEGER NOT NULL CHECK(payload_version=1),
                outcome TEXT NOT NULL CHECK(outcome='queued'), delivered_at TIMESTAMPTZ,
                delivery_error_code TEXT, delivery_failed_at TIMESTAMPTZ,
                PRIMARY KEY(account_id,conversation_id,trigger_message_id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS ics_handoff_success_counter (
                account_id TEXT NOT NULL, contact_id TEXT NOT NULL,
                success_times TIMESTAMPTZ[] NOT NULL, week_count INT NOT NULL,
                week_anchor TIMESTAMPTZ NOT NULL, last_message_id TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL, PRIMARY KEY(account_id,contact_id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS ics_handoff_queue_state (
                account_id TEXT NOT NULL, contact_id TEXT NOT NULL,
                initial_count INT, entered_at TIMESTAMPTZ, seed BIGINT,
                step_min_s INT, step_max_s INT, redeem_ready BOOLEAN NOT NULL DEFAULT FALSE,
                config_version BIGINT NOT NULL, PRIMARY KEY(account_id,contact_id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS ics_handoff_queue_config (
                version BIGINT PRIMARY KEY, config JSONB NOT NULL, updated_by TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, previous_config JSONB)""")
            conn.execute("""INSERT INTO ics_handoff_queue_config(version,config,updated_by)
                VALUES(1,%s,'system') ON CONFLICT DO NOTHING""", (Jsonb(DEFAULT_CONFIG),))

    def current_config(self):
        with self.pool.connection() as conn:
            return conn.execute('SELECT version,config FROM ics_handoff_queue_config ORDER BY version DESC LIMIT 1').fetchone()

    def save_config(self, expected_version, config, operator):
        try:
            with self.pool.connection() as conn:
                previous = conn.execute('SELECT version,config FROM ics_handoff_queue_config ORDER BY version DESC LIMIT 1').fetchone()
                if not previous or previous['version'] != expected_version:
                    raise ConfigConflict()
                return conn.execute("""INSERT INTO ics_handoff_queue_config(version,config,updated_by,previous_config)
                    VALUES(%s,%s,%s,%s) RETURNING version,config""", (expected_version + 1, Jsonb(config), operator, Jsonb(previous['config']))).fetchone()
        except UniqueViolation:
            raise ConfigConflict() from None

    def history(self, limit):
        with self.pool.connection() as conn:
            return conn.execute('SELECT * FROM ics_handoff_queue_config ORDER BY version DESC LIMIT %s', (limit,)).fetchall()

    def get_state(self, account, contact):
        with self.pool.connection() as conn:
            return conn.execute('SELECT * FROM ics_handoff_queue_state WHERE account_id=%s AND contact_id=%s', (account, contact)).fetchone()

    def get_counter(self, account, contact):
        with self.pool.connection() as conn:
            return conn.execute('SELECT * FROM ics_handoff_success_counter WHERE account_id=%s AND contact_id=%s', (account, contact)).fetchone()

    def counter_snapshot(self, keys, *, now=None, budget=None):
        """批量读取同一时刻的日、周计数，直接复用排队判断口径。"""
        keys = list(dict.fromkeys((str(account), str(contact)) for account, contact in keys))
        rows = []
        if keys:
            accounts, contacts = map(list, zip(*keys))
            context = budget.connection(self.pool) if budget is not None else self.pool.connection()
            with context as conn:
                rows = conn.execute("""SELECT c.account_id,c.contact_id,c.success_times,c.week_anchor,c.week_count
                    FROM unnest(%s::text[],%s::text[]) AS k(account_id,contact_id)
                    JOIN ics_handoff_success_counter c USING(account_id,contact_id)""", (accounts, contacts)).fetchall()
        now = now or datetime.now(timezone.utc)
        counts = dict.fromkeys(keys, (0, 0))
        counts.update({(row['account_id'], row['contact_id']): success_counts(row, now) for row in rows})
        return {'as_of': now, 'week_start': week_anchor(now), 'counts': counts}

    def create_state(self, account, contact, state):
        with self.pool.connection() as conn:
            conn.execute("""INSERT INTO ics_handoff_queue_state(account_id,contact_id,initial_count,entered_at,seed,step_min_s,step_max_s,config_version)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""", (account, contact, state['initial_count'], state['entered_at'], state['seed'], state['step_min_s'], state['step_max_s'], state['config_version']))
            return conn.execute('SELECT * FROM ics_handoff_queue_state WHERE account_id=%s AND contact_id=%s', (account, contact)).fetchone()

    def mark_redeem(self, account, contact, state):
        """只压缩本次读取的排队，迟到操作不能修改成功后新建的排队。"""
        with self.pool.connection() as conn:
            conn.execute("""UPDATE ics_handoff_queue_state SET redeem_ready=TRUE,
                initial_count=NULL,entered_at=NULL,seed=NULL,step_min_s=NULL,step_max_s=NULL
                WHERE account_id=%s AND contact_id=%s AND NOT redeem_ready
                    AND entered_at=%s AND seed=%s AND config_version=%s""",
                (account, contact, state['entered_at'], state['seed'], state['config_version']))

    def record_success(self, account, contact, message_id, now):
        with self.pool.connection() as conn:
            row = conn.execute("""INSERT INTO ics_handoff_success_counter AS c
                (account_id,contact_id,success_times,week_count,week_anchor,last_message_id,updated_at)
                VALUES(%s,%s,ARRAY[%s]::timestamptz[],1,%s,%s,%s)
                ON CONFLICT(account_id,contact_id) DO UPDATE SET
                success_times=ARRAY(SELECT t FROM unnest(c.success_times) t WHERE t>%s) || EXCLUDED.success_times,
                week_count=CASE WHEN c.week_anchor=EXCLUDED.week_anchor THEN c.week_count+1
                    WHEN c.week_anchor<EXCLUDED.week_anchor THEN 1 ELSE c.week_count END,
                week_anchor=GREATEST(c.week_anchor,EXCLUDED.week_anchor),
                last_message_id=EXCLUDED.last_message_id,updated_at=GREATEST(c.updated_at,EXCLUDED.updated_at)
                WHERE c.last_message_id<>EXCLUDED.last_message_id RETURNING account_id""", (account, contact, now, week_anchor(now), message_id, now, now - timedelta(days=1))).fetchone()
            if row:
                conn.execute('DELETE FROM ics_handoff_queue_state WHERE account_id=%s AND contact_id=%s', (account, contact))
            return bool(row)

    def cleanup(self, now):
        with self.pool.connection() as conn:
            if not conn.execute("SELECT pg_try_advisory_xact_lock(hashtext('ics-handoff-queue-cleanup')) AS acquired").fetchone()['acquired']:
                return
            conn.execute("""UPDATE ics_handoff_queue_state SET redeem_ready=TRUE,
                initial_count=NULL,entered_at=NULL,seed=NULL,step_min_s=NULL,step_max_s=NULL
                WHERE NOT redeem_ready AND entered_at+initial_count*step_max_s*interval '1 second'<=%s""", (now,))
            conn.execute("""DELETE FROM ics_handoff_success_counter
                WHERE week_anchor<%s AND NOT EXISTS(SELECT 1 FROM unnest(success_times) t WHERE t>%s)""", (week_anchor(now), now - timedelta(days=1)))

    def saved_decision(self, account, conversation, message):
        with self.pool.connection() as conn:
            return conn.execute("""SELECT * FROM ics_handoff_queue_event_outbox
                WHERE account_id=%s AND conversation_id=%s AND trigger_message_id=%s""",
                (account, conversation, message)).fetchone()

    def save_decision(self, row):
        with self.pool.connection() as conn:
            inserted = conn.execute("""INSERT INTO ics_handoff_queue_event_outbox
                (account_id,conversation_id,trigger_message_id,contact_id,trigger_at,decided_at,event_key,payload_version,outcome)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING *""",
                tuple(row[k] for k in ('account_id','conversation_id','trigger_message_id','contact_id',
                    'trigger_at','decided_at','event_key','payload_version','outcome'))).fetchone()
            if inserted:
                return inserted, True
            existing = conn.execute("""SELECT * FROM ics_handoff_queue_event_outbox
                WHERE account_id=%s AND conversation_id=%s AND trigger_message_id=%s""",
                (row['account_id'],row['conversation_id'],row['trigger_message_id'])).fetchone()
            if existing is None:
                raise RuntimeError('Queue evidence conflict cannot be recovered')
            return existing, False
