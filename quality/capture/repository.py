"""Commit immutable evidence and durable processing work in one transaction."""

from dataclasses import dataclass

from psycopg_pool import ConnectionPool

from .config import CaptureSettings
from .envelope import CaptureEnvelope


@dataclass(frozen=True)
class Receipt:
    event_id: int
    duplicate: bool


class CaptureRepository:
    def __init__(self, settings: CaptureSettings):
        self.source_key = settings.source_key
        self.pool = ConnectionPool(
            kwargs=settings.connection_kwargs, min_size=1, max_size=settings.postgres_pool_size,
            timeout=1, open=False,
        )

    def open(self):
        try:
            self.pool.open(wait=True, timeout=10)
            with self.pool.connection() as connection:
                row = connection.execute("""
                    SELECT r.rolsuper, pg_has_role(current_user, 'archive_owner', 'MEMBER'),
                           has_table_privilege(current_user, 'archive.events', 'SELECT'),
                           has_table_privilege(current_user, 'archive.events', 'INSERT'),
                           has_table_privilege(current_user, 'archive.events', 'UPDATE,DELETE,TRUNCATE'),
                           has_table_privilege(current_user, 'ops.event_jobs', 'INSERT')
                    FROM pg_roles r WHERE r.rolname = current_user
                """).fetchone()
                if not row or row[0] or row[1] or not row[2] or not row[3] or row[4] or not row[5]:
                    raise RuntimeError("Receiver requires the restricted archive ingestion role and migrated tables")
        except BaseException:
            self.pool.close()
            raise

    def close(self):
        self.pool.close()

    def ping(self):
        with self.pool.connection() as connection:
            connection.execute("SELECT 1").fetchone()

    def receive(self, envelope: CaptureEnvelope) -> Receipt:
        with self.pool.connection() as connection:
            row = connection.execute("""
                INSERT INTO archive.events
                    (source_key, account_id, event_key, event_type, ingestion_kind,
                     conversation_id, message_id, raw_body, payload)
                VALUES (%s, %s, %s, %s, 'webhook', %s, %s, %s, convert_from(%s, 'UTF8')::jsonb)
                ON CONFLICT (source_key, account_id, event_key) DO NOTHING RETURNING id
            """, (self.source_key, envelope.account_id, envelope.event_key, envelope.event_type,
                  envelope.conversation_id, envelope.message_id, envelope.raw_body, envelope.raw_body)).fetchone()
            duplicate = row is None
            if row is None:
                row = connection.execute("""
                    SELECT id FROM archive.events WHERE source_key = %s AND account_id = %s AND event_key = %s
                """, (self.source_key, envelope.account_id, envelope.event_key)).fetchone()
            if row is None:
                raise RuntimeError("An acknowledged duplicate must already have durable evidence")
            event_id = row[0]
            connection.execute("""
                INSERT INTO ops.event_jobs (event_id) VALUES (%s) ON CONFLICT (event_id) DO NOTHING
            """, (event_id,))
        # The pool connection context commits on exit. Never acknowledge earlier.
        return Receipt(event_id=event_id, duplicate=duplicate)
