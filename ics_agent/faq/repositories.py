# 模块职责：实现 MySQL 连接池、FAQ 数据访问、Redis 快照和答案缓存。

import json
import logging
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable

import redis
from mysql.connector.errors import PoolError
from mysql.connector.pooling import MySQLConnectionPool as ConnectorPool
from redis.backoff import NoBackoff
from redis.retry import Retry

from ics_agent.app.logging_config import log_event

try:
    from .models import FAQSnapshot
except ImportError:
    from models import FAQSnapshot

PROJECT_ROOT = Path(__file__).resolve().parents[2]

try:
    from ics_agent.app.config import get_settings
except ModuleNotFoundError:
    import sys

    sys.path.insert(0, str(PROJECT_ROOT))
    from ics_agent.app.config import get_settings


logger = logging.getLogger(__name__)


def _mysql_config() -> dict:
    settings = get_settings()
    return {
        "host": settings.mysql_host,
        "port": settings.mysql_port,
        "database": settings.mysql_database,
        "user": settings.mysql_user,
        "password": settings.mysql_password,
        "charset": "utf8mb4",
        "use_unicode": True,
        "connection_timeout": int(settings.mysql_connection_timeout),
    }


class RedisCircuitOpen(redis.RedisError):
    """Raised when Redis is temporarily bypassed after a connection failure."""


class RedisFailureCircuit:
    def __init__(self, cooldown_seconds: float | None = None) -> None:
        settings = get_settings()
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


class MySQLConnectionPool:
    _shared_pool: ConnectorPool | None = None
    _pool_lock = threading.Lock()

    def __init__(self) -> None:
        # Delay opening the pool until a MySQL operation is actually needed.
        # This lets FAQ startup use the Redis snapshot when MySQL is down.
        self._pool: ConnectorPool | None = None

    def acquire(self):
        pool = self.__class__._shared_pool
        if pool is None:
            with self.__class__._pool_lock:
                pool = self.__class__._shared_pool
                if pool is None:
                    settings = get_settings()
                    log_event(
                        logger,
                        logging.INFO,
                        "mysql.pool.create.start",
                        pool_name=settings.mysql_pool_name,
                        pool_size=settings.mysql_pool_size,
                    )
                    try:
                        pool = ConnectorPool(
                            pool_name=settings.mysql_pool_name,
                            pool_size=settings.mysql_pool_size,
                            pool_reset_session=True,
                            **_mysql_config(),
                        )
                    except Exception as exc:
                        log_event(
                            logger,
                            logging.ERROR,
                            "mysql.pool.create.failed",
                            error_type=type(exc).__name__,
                        )
                        raise
                    self.__class__._shared_pool = pool
                    log_event(
                        logger,
                        logging.INFO,
                        "mysql.pool.create.success",
                        pool_name=settings.mysql_pool_name,
                        pool_size=settings.mysql_pool_size,
                    )
        self._pool = pool
        try:
            return pool.get_connection()
        except PoolError as exc:
            log_event(
                logger,
                logging.WARNING,
                "mysql.pool.exhausted",
                error_type=type(exc).__name__,
            )
            raise


class FAQRepository:
    def __init__(self, connections: MySQLConnectionPool | None = None) -> None:
        self._connections = connections or MySQLConnectionPool()

    def fetch_questions(
        self,
        include_disabled: bool = False,
    ) -> list[tuple[int, str]]:
        settings = get_settings()
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
                    "mysql.faq.questions.loaded",
                    count=len(rows),
                    include_disabled=include_disabled,
                )
                return rows
            finally:
                cursor.close()
        finally:
            connection.close()

    def fetch_answer(self, question_id: int) -> tuple[str, str]:
        settings = get_settings()
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
            "mysql.faq.answer.loaded",
            question_id=question_id,
        )
        return str(row[0]), str(row[1])

    def answer_sql(self) -> str:
        settings = get_settings()
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
        settings = get_settings()
        self.circuit = circuit or RedisFailureCircuit()
        self.client = client or redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=settings.redis_connect_timeout,
            socket_timeout=settings.redis_socket_timeout,
            retry=Retry(NoBackoff(), retries=0),
        )
        self._prefix = settings.redis_key_prefix

    def _key(self, suffix: str) -> str:
        return f"{self._prefix}{suffix}"

    def load(self) -> FAQSnapshot:
        settings = get_settings()
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
        settings = get_settings()
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
    ) -> None:
        settings = get_settings()
        self.circuit = circuit or RedisFailureCircuit()
        self.client = client or redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=settings.redis_connect_timeout,
            socket_timeout=settings.redis_socket_timeout,
            retry=Retry(NoBackoff(), retries=0),
        )
        self._cache_key = (
            f"{settings.redis_key_prefix}{settings.faq_query_answers_key}"
        )
        self._index_key = (
            f"{settings.redis_key_prefix}{settings.faq_cache_index_key}"
        )

    def get(self, query: str, ttl_seconds: int | None = None) -> str | None:
        normalized_query = normalize_query(query)
        if not normalized_query:
            return None
        settings = get_settings()
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
        settings = get_settings()
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
