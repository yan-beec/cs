# 模块职责：实现 PostgreSQL 连接池、FAQ 数据访问、Redis 快照和答案缓存。

import json
import logging
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from ics_agent.app.logging_config import log_event

try:
    from .models import FAQSnapshot
except ImportError:
    from models import FAQSnapshot

PROJECT_ROOT = Path(__file__).resolve().parents[2]

try:
    from ics_agent.app.config import get_settings as get_app_settings
    from ics_agent.faq.config import get_faq_settings
except ModuleNotFoundError:
    import sys

    sys.path.insert(0, str(PROJECT_ROOT))
    from ics_agent.app.config import get_settings as get_app_settings
    from ics_agent.faq.config import get_faq_settings


logger = logging.getLogger(__name__)


class PostgresPoolExhausted(RuntimeError):
    """Raised when no PostgreSQL pool connection is available immediately."""


class PostgresDependencyError(RuntimeError):
    """Raised when the PostgreSQL driver is not installed."""


def _postgres_kwargs() -> dict[str, Any]:
    settings = get_app_settings()
    kwargs: dict[str, Any] = {
        "host": settings.postgres_host,
        "port": settings.postgres_port,
        "dbname": settings.postgres_database,
        "user": settings.postgres_user,
        "password": settings.postgres_password,
        "connect_timeout": int(settings.postgres_connection_timeout),
        "sslmode": settings.postgres_sslmode,
    }
    return kwargs


class RedisCircuitOpen(redis.RedisError):
    """Raised when Redis is temporarily bypassed after a connection failure."""


class RedisFailureCircuit:
    def __init__(self, cooldown_seconds: float | None = None) -> None:
        settings = get_app_settings()
        self._cooldown_seconds = (
            settings.redis_failure_cooldown_seconds
            if cooldown_seconds is None
            else cooldown_seconds
        )
        self._open_until = 0.0
        self._lock = threading.Lock()

    def before_call(self) -> None:
        with self._lock:
            remaining = self._open_until - time.monotonic()
            if remaining > 0:
                log_event(
                    logger,
                    logging.DEBUG,
                    "redis.circuit.skip",
                    cooldown_remaining_ms=int(remaining * 1000),
                )
                raise RedisCircuitOpen(
                    "Redis circuit is open; skipping Redis operation."
                )

    def record_failure(self, exc: redis.RedisError) -> None:
        unavailable_errors = (
            redis.ConnectionError,
            redis.TimeoutError,
            TimeoutError,
        )
        if not isinstance(exc, unavailable_errors):
            return
        with self._lock:
            self._open_until = time.monotonic() + self._cooldown_seconds
        log_event(
            logger,
            logging.WARNING,
            "redis.circuit.open",
            error_type=type(exc).__name__,
            cooldown_seconds=self._cooldown_seconds,
        )

    def record_success(self) -> None:
        with self._lock:
            self._open_until = 0.0

    def execute(self, operation: Callable[[], Any]) -> Any:
        self.before_call()
        try:
            result = operation()
        except redis.RedisError as exc:
            self.record_failure(exc)
            raise
        else:
            self.record_success()
            return result


class _PooledPostgresConnection:
    """Return a psycopg connection to the pool when ``close`` is called."""

    def __init__(self, pool: Any, connection: Any) -> None:
        self._pool = pool
        self._connection = connection
        self._returned = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    def close(self) -> None:
        if self._returned:
            return
        self._returned = True
        broken = bool(getattr(self._connection, "broken", False))
        if not broken and not bool(getattr(self._connection, "closed", False)):
            try:
                # Read-only psycopg operations still open a transaction. Reset
                # it before returning the connection to avoid pool warnings
                # and leaking transaction state into the next request.
                self._connection.rollback()
            except Exception:
                broken = True
        if broken:
            self._connection.close()
            return
        self._pool.putconn(self._connection)


class PostgreSQLConnectionPool:
    _shared_pool: Any | None = None
    _pool_lock = threading.Lock()

    def __init__(self) -> None:
        # Delay opening the pool until a PostgreSQL operation is actually
        # needed. This lets FAQ startup use Redis when PostgreSQL is down.
        self._pool: Any | None = None

    def acquire(self):
        pool = self.__class__._shared_pool
        if pool is None:
            with self.__class__._pool_lock:
                pool = self.__class__._shared_pool
                if pool is None:
                    try:
                        from psycopg_pool import ConnectionPool
                    except ImportError as exc:
                        raise PostgresDependencyError(
                            "psycopg[binary,pool] is required for PostgreSQL access."
                        ) from exc

                    settings = get_app_settings()
                    log_event(
                        logger,
                        logging.INFO,
                        "postgres.pool.create.start",
                        pool_name=settings.postgres_pool_name,
                        pool_size=settings.postgres_pool_size,
                    )
                    try:
                        pool_kwargs = _postgres_kwargs()
                        if settings.postgres_dsn:
                            pool = ConnectionPool(
                                conninfo=settings.postgres_dsn,
                                kwargs={
                                    "connect_timeout": int(
                                        settings.postgres_connection_timeout
                                    ),
                                },
                                min_size=0,
                                max_size=settings.postgres_pool_size,
                                timeout=settings.postgres_pool_acquire_timeout,
                                open=False,
                            )
                        else:
                            pool = ConnectionPool(
                                kwargs=pool_kwargs,
                                min_size=0,
                                max_size=settings.postgres_pool_size,
                                timeout=settings.postgres_pool_acquire_timeout,
                                open=False,
                            )
                        pool.open(wait=False)
                    except Exception as exc:
                        log_event(
                            logger,
                            logging.ERROR,
                            "postgres.pool.create.failed",
                            error_type=type(exc).__name__,
                        )
                        raise
                    self.__class__._shared_pool = pool
                    log_event(
                        logger,
                        logging.INFO,
                        "postgres.pool.create.success",
                        pool_name=settings.postgres_pool_name,
                        pool_size=settings.postgres_pool_size,
                    )
        self._pool = pool
        try:
            settings = get_app_settings()
            connection = pool.getconn(
                timeout=settings.postgres_pool_acquire_timeout
            )
            return _PooledPostgresConnection(pool, connection)
        except Exception as exc:
            try:
                from psycopg_pool import PoolTimeout
            except ImportError:
                PoolTimeout = ()  # type: ignore[assignment]
            if isinstance(exc, PoolTimeout):
                log_event(
                    logger,
                    logging.WARNING,
                    "postgres.pool.exhausted",
                    error_type=type(exc).__name__,
                )
                raise PostgresPoolExhausted(
                    "PostgreSQL connection pool is exhausted."
                ) from exc
            log_event(
                logger,
                logging.ERROR,
                "postgres.pool.acquire.failed",
                error_type=type(exc).__name__,
            )
            raise


def _build_faq_schema_sql() -> tuple[str, str, str]:
    """Build the FAQ table and index DDL from the configured table name."""
    table_name = get_faq_settings().faq_table_name
    enabled_index_name = f"{table_name}_enabled_idx"
    priority_index_name = f"{table_name}_priority_idx"
    table_sql = f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            id BIGINT PRIMARY KEY,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            category TEXT,
            tags JSONB,
            similar_questions JSONB,
            need_human SMALLINT NOT NULL DEFAULT 0
                CHECK (need_human IN (0, 1)),
            enabled SMALLINT NOT NULL DEFAULT 1
                CHECK (enabled IN (0, 1)),
            priority INTEGER NOT NULL DEFAULT 0,
            answer_type TEXT NOT NULL DEFAULT 'text',
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """.strip()
    enabled_index_sql = f"""
        CREATE INDEX IF NOT EXISTS {enabled_index_name}
        ON {table_name} (enabled)
    """.strip()
    priority_index_sql = f"""
        CREATE INDEX IF NOT EXISTS {priority_index_name}
        ON {table_name} (priority DESC, id)
    """.strip()
    return table_sql, enabled_index_sql, priority_index_sql


def ensure_faq_table(
    connections: PostgreSQLConnectionPool | None = None,
) -> bool:
    """Create the configured FAQ table only when it does not exist.

    Returns ``True`` when this call created the table and ``False`` when the
    table was already present. Existing tables are intentionally not altered.
    """
    table_name = get_faq_settings().faq_table_name
    pool = connections or PostgreSQLConnectionPool()
    connection = pool.acquire()
    created = False
    try:
        cursor = connection.cursor()
        try:
            with connection.transaction():
                cursor.execute(
                    "SELECT to_regclass(%s)",
                    (table_name,),
                )
                exists = cursor.fetchone()[0] is not None
                if not exists:
                    table_sql, enabled_index_sql, priority_index_sql = (
                        _build_faq_schema_sql()
                    )
                    cursor.execute(table_sql)
                    cursor.execute(enabled_index_sql)
                    cursor.execute(priority_index_sql)
                    created = True
        finally:
            cursor.close()
    finally:
        connection.close()

    log_event(
        logger,
        logging.INFO,
        (
            "postgres.faq.table.created"
            if created
            else "postgres.faq.table.exists"
        ),
        table=table_name,
    )
    return created


class FAQRepository:
    def __init__(
        self,
        connections: PostgreSQLConnectionPool | None = None,
    ) -> None:
        self._connections = connections or PostgreSQLConnectionPool()

    def fetch_questions(
        self,
        include_disabled: bool = False,
    ) -> list[tuple[int, str]]:
        settings = get_faq_settings()
        where = "" if include_disabled else "WHERE enabled = 1"
        sql = f"""
            SELECT id, question
            FROM {settings.faq_table_name}
            {where}
            ORDER BY id
        """

        connection = self._connections.acquire()
        try:
            cursor = connection.cursor()
            try:
                cursor.execute(sql)
                rows = [
                    (int(row[0]), str(row[1]).strip())
                    for row in cursor.fetchall()
                ]
                log_event(
                    logger,
                    logging.INFO,
                    "postgres.faq.questions.loaded",
                    count=len(rows),
                    include_disabled=include_disabled,
                )
                return rows
            finally:
                cursor.close()
        finally:
            connection.close()

    def fetch_answer(self, question_id: int) -> tuple[str, str]:
        settings = get_faq_settings()
        sql = f"""
            SELECT question, answer
            FROM {settings.faq_table_name}
            WHERE id = %s AND enabled = 1
            LIMIT 1
        """.strip()

        connection = self._connections.acquire()
        try:
            cursor = connection.cursor()
            try:
                cursor.execute(sql, (question_id,))
                row = cursor.fetchone()
            finally:
                cursor.close()
        finally:
            connection.close()

        if row is None:
            raise LookupError(
                f"Enabled FAQ answer was not found for question id {question_id}."
            )
        log_event(
            logger,
            logging.DEBUG,
            "postgres.faq.answer.loaded",
            question_id=question_id,
        )
        return str(row[0]), str(row[1])

    def fetch_answers(self, question_ids: list[int]) -> dict[int, str]:
        """Load all enabled answers needed by one FAQ runtime snapshot."""
        if not question_ids:
            return {}

        settings = get_faq_settings()
        placeholders = ", ".join(["%s"] * len(question_ids))
        sql = f"""
            SELECT id, answer
            FROM {settings.faq_table_name}
            WHERE id IN ({placeholders}) AND enabled = 1
        """.strip()

        connection = self._connections.acquire()
        try:
            cursor = connection.cursor()
            try:
                cursor.execute(sql, tuple(question_ids))
                answers = {
                    int(row[0]): str(row[1])
                    for row in cursor.fetchall()
                }
            finally:
                cursor.close()
        finally:
            connection.close()

        log_event(
            logger,
            logging.INFO,
            "postgres.faq.answers.loaded",
            requested_count=len(question_ids),
            loaded_count=len(answers),
        )
        return answers

    def answer_sql(self) -> str:
        settings = get_faq_settings()
        return f"""
            SELECT question, answer
            FROM {settings.faq_table_name}
            WHERE id = %s AND enabled = 1
            LIMIT 1
        """.strip()


def _json_value(client: redis.Redis, key: str) -> Any:
    raw_value = client.get(key)
    if raw_value is None:
        raise KeyError(f"Redis key does not exist: {key}")
    try:
        return json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Redis key does not contain valid JSON: {key}") from exc


class RedisFAQSnapshotRepository:
    def __init__(
        self,
        client: redis.Redis | None = None,
        circuit: RedisFailureCircuit | None = None,
    ) -> None:
        app_settings = get_app_settings()
        self.circuit = circuit or RedisFailureCircuit()
        self.client = client or redis.from_url(
            app_settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=app_settings.redis_connect_timeout,
            socket_timeout=app_settings.redis_socket_timeout,
            retry=Retry(NoBackoff(), retries=0),
        )
        self._prefix = app_settings.redis_key_prefix

    def _key(self, suffix: str) -> str:
        return f"{self._prefix}{suffix}"

    def load(self) -> FAQSnapshot:
        settings = get_faq_settings()
        log_event(logger, logging.DEBUG, "redis.snapshot.load.start")
        question_ids = self.circuit.execute(
            lambda: _json_value(
                self.client,
                self._key(settings.faq_question_ids_key),
            )
        )
        questions = self.circuit.execute(
            lambda: _json_value(
                self.client,
                self._key(settings.faq_questions_key),
            )
        )
        tokenized_questions = self.circuit.execute(
            lambda: _json_value(
                self.client,
                self._key(settings.faq_question_tokens_key),
            )
        )
        if not isinstance(question_ids, list) or not all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in question_ids
        ):
            raise ValueError("Redis question ids must be a JSON list of integers.")
        if not isinstance(questions, list) or not all(
            isinstance(value, str) for value in questions
        ):
            raise ValueError("Redis questions must be a JSON list of strings.")
        if not isinstance(tokenized_questions, list) or not all(
            isinstance(value, list) for value in tokenized_questions
        ):
            raise ValueError(
                "Redis question tokens must be a JSON list of token lists."
            )
        snapshot = FAQSnapshot.from_lists(
            question_ids,
            questions,
            tokenized_questions,
        )
        log_event(
            logger,
            logging.INFO,
            "redis.snapshot.load.success",
            count=len(snapshot.documents),
        )
        return snapshot

    def save(self, snapshot: FAQSnapshot) -> dict[str, str]:
        settings = get_faq_settings()
        keys = {
            "ids": self._key(settings.faq_question_ids_key),
            "questions": self._key(settings.faq_questions_key),
            "tokens": self._key(settings.faq_question_tokens_key),
            "meta": self._key(settings.faq_questions_meta_key),
        }
        meta = {
            "count": len(snapshot.documents),
            "terms": list(settings.faq_terms_list),
            "source_table": settings.faq_table_name,
        }
        def save_snapshot() -> None:
            pipe = self.client.pipeline()
            pipe.set(
                keys["ids"],
                json.dumps(snapshot.question_ids, ensure_ascii=False),
            )
            pipe.set(
                keys["questions"],
                json.dumps(snapshot.questions, ensure_ascii=False),
            )
            pipe.set(
                keys["tokens"],
                json.dumps(snapshot.tokenized_questions, ensure_ascii=False),
            )
            pipe.set(keys["meta"], json.dumps(meta, ensure_ascii=False))
            pipe.execute()

        self.circuit.execute(save_snapshot)
        log_event(
            logger,
            logging.INFO,
            "redis.snapshot.save.success",
            count=len(snapshot.documents),
        )
        return keys


_CACHE_GET_SCRIPT = """
local cache_key = KEYS[1]
local index_key = KEYS[2]
local field = ARGV[1]
local now = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local last_access = redis.call("ZSCORE", index_key, field)
if not last_access then
    local legacy_value = redis.call("HGET", cache_key, field)
    if not legacy_value then return false end
    redis.call("ZADD", index_key, now, field)
    return legacy_value
end
if now - tonumber(last_access) >= ttl then
    redis.call("HDEL", cache_key, field)
    redis.call("ZREM", index_key, field)
    return false
end
local value = redis.call("HGET", cache_key, field)
if not value then
    redis.call("ZREM", index_key, field)
    return false
end
redis.call("ZADD", index_key, now, field)
return value
"""

_CACHE_SET_SCRIPT = """
local cache_key = KEYS[1]
local index_key = KEYS[2]
local field = ARGV[1]
local value = ARGV[2]
local now = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
local max_entries = tonumber(ARGV[5])
local expire_before = now - ttl
redis.call("HSET", cache_key, field, value)
redis.call("ZADD", index_key, now, field)
local expired_fields = redis.call("ZRANGEBYSCORE", index_key, "-inf", expire_before)
for _, expired_field in ipairs(expired_fields) do
    redis.call("HDEL", cache_key, expired_field)
    redis.call("ZREM", index_key, expired_field)
end
local count = redis.call("ZCARD", index_key)
if count > max_entries then
    local overflow = count - max_entries
    local oldest_fields = redis.call("ZRANGE", index_key, 0, overflow - 1)
    for _, oldest_field in ipairs(oldest_fields) do
        redis.call("HDEL", cache_key, oldest_field)
        redis.call("ZREM", index_key, oldest_field)
    end
end
return true
"""


def normalize_query(query: str) -> str:
    normalized = unicodedata.normalize("NFKC", query).casefold()
    normalized = "".join(
        " " if unicodedata.category(char).startswith(("P", "Z")) else char
        for char in normalized
    )
    return " ".join(normalized.split())


class FAQAnswerCache:
    def __init__(
        self,
        client: redis.Redis | None = None,
        circuit: RedisFailureCircuit | None = None,
        namespace: str | None = None,
    ) -> None:
        app_settings = get_app_settings()
        faq_settings = get_faq_settings()
        self.circuit = circuit or RedisFailureCircuit()
        self.client = client or redis.from_url(
            app_settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=app_settings.redis_connect_timeout,
            socket_timeout=app_settings.redis_socket_timeout,
            retry=Retry(NoBackoff(), retries=0),
        )
        namespace_suffix = f":{namespace}" if namespace else ""
        self._cache_key = (
            f"{app_settings.redis_key_prefix}{faq_settings.faq_query_answers_key}"
            f"{namespace_suffix}"
        )
        self._index_key = (
            f"{app_settings.redis_key_prefix}{faq_settings.faq_cache_index_key}"
            f"{namespace_suffix}"
        )

    def get(self, query: str, ttl_seconds: int | None = None) -> str | None:
        normalized_query = normalize_query(query)
        if not normalized_query:
            return None
        settings = get_faq_settings()
        ttl = (
            settings.faq_cache_ttl_seconds
            if ttl_seconds is None
            else ttl_seconds
        )
        value = self.circuit.execute(
            lambda: self.client.eval(
                _CACHE_GET_SCRIPT,
                2,
                self._cache_key,
                self._index_key,
                normalized_query,
                str(time.time()),
                str(ttl),
            )
        )
        log_event(
            logger,
            logging.DEBUG,
            "redis.answer_cache.read",
            result="hit" if value is not None else "miss",
        )
        return value if value is not None else None

    def set(
        self,
        query: str,
        answer: str,
        ttl_seconds: int | None = None,
        max_entries: int | None = None,
    ) -> None:
        normalized_query = normalize_query(query)
        if not normalized_query:
            return
        settings = get_faq_settings()
        ttl = (
            settings.faq_cache_ttl_seconds
            if ttl_seconds is None
            else ttl_seconds
        )
        limit = (
            settings.faq_cache_max_entries
            if max_entries is None
            else max_entries
        )
        self.circuit.execute(
            lambda: self.client.eval(
                _CACHE_SET_SCRIPT,
                2,
                self._cache_key,
                self._index_key,
                normalized_query,
                answer,
                str(time.time()),
                str(ttl),
                str(limit),
            )
        )
        log_event(logger, logging.DEBUG, "redis.answer_cache.write.success")

    def clear(self) -> int:
        """Remove only the FAQ query-answer cache and its access index."""
        removed_keys = self.circuit.execute(
            lambda: self.client.unlink(self._cache_key, self._index_key)
        )
        removed_count = int(removed_keys or 0)
        log_event(
            logger,
            logging.INFO,
            "redis.answer_cache.clear.success",
            removed_keys=removed_count,
        )
        return removed_count

    @property
    def key(self) -> str:
        return self._cache_key


def postgres_config() -> dict[str, Any]:
    """Return the psycopg keyword arguments for the configured database."""
    return _postgres_kwargs()


def get_postgres_connection():
    """Acquire one pooled PostgreSQL connection."""
    return PostgreSQLConnectionPool().acquire()


def get_cached_answer(
    client: redis.Redis,
    query: str,
    ttl_seconds: int | None = None,
) -> str | None:
    """Read one answer from the FAQ cache."""
    return FAQAnswerCache(client).get(query, ttl_seconds=ttl_seconds)


def set_cached_answer(
    client: redis.Redis,
    query: str,
    answer: str,
    ttl_seconds: int | None = None,
    max_entries: int | None = None,
) -> bool:
    """Write one answer to the FAQ cache."""
    FAQAnswerCache(client).set(
        query,
        answer,
        ttl_seconds=ttl_seconds,
        max_entries=max_entries,
    )
    return True


def cache_key() -> str:
    """Return the configured Redis key for FAQ answer cache entries."""
    app_settings = get_app_settings()
    faq_settings = get_faq_settings()
    return (
        f"{app_settings.redis_key_prefix}"
        f"{faq_settings.faq_query_answers_key}"
    )


def cache_index_key() -> str:
    """Return the configured Redis key for FAQ cache access timestamps."""
    app_settings = get_app_settings()
    faq_settings = get_faq_settings()
    return (
        f"{app_settings.redis_key_prefix}"
        f"{faq_settings.faq_cache_index_key}"
    )
