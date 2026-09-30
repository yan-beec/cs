# 模块职责：实现 PostgreSQL 连接池和 FAQ 数据访问。

import logging
import threading
from pathlib import Path
from typing import Any

from ics_agent.app.core.logging_config import log_event

PROJECT_ROOT = Path(__file__).resolve().parents[2]

try:
    from ics_agent.app.core.config import get_settings as get_app_settings
    from ics_agent.faq.config import get_faq_settings
except ModuleNotFoundError:
    import sys

    sys.path.insert(0, str(PROJECT_ROOT))
    from ics_agent.app.core.config import get_settings as get_app_settings
    from ics_agent.faq.config import get_faq_settings


logger = logging.getLogger(__name__)


class PostgresPoolExhausted(RuntimeError):
    """无法立即获取 PostgreSQL 连接池连接时抛出。"""


class PostgresDependencyError(RuntimeError):
    """未安装 PostgreSQL 驱动时抛出。"""


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


class _PooledPostgresConnection:
    """调用 ``close`` 时将 psycopg 连接归还连接池。"""

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
        closed = bool(getattr(self._connection, "closed", False))
        if not broken and not closed:
            try:
                # 只读 psycopg 操作仍会开启事务。归还连接前重置事务，
                # 避免连接池警告以及事务状态泄漏到下一个请求。
                self._connection.rollback()
            except Exception:
                broken = True

        if broken and not closed:
            # 先释放损坏连接占用的底层资源，再通过 putconn() 告知连接池
            # 该名额已经归还，以便连接池创建替代连接。
            try:
                self._connection.close()
            except Exception:
                # 即使底层 close 失败，也要继续 putconn() 释放池名额。
                pass

        # 坏连接也必须交还给连接池。psycopg_pool 会在 putconn() 时识别
        # transaction status 为 UNKNOWN 的连接，将其关闭并异步补充新连接；
        # 直接调用 connection.close() 会让池一直认为该名额仍在使用中。
        try:
            self._pool.putconn(self._connection)
        except Exception:
            # putconn() 失败时尽力关闭底层连接，避免泄漏资源。原始异常
            # 仍然向调用方传播，便于定位连接池本身的故障。
            try:
                self._connection.close()
            finally:
                raise


class PostgreSQLConnectionPool:
    _shared_pool: Any | None = None
    _pool_lock = threading.Lock()

    def __init__(self) -> None:
        # 延迟到确实需要执行 PostgreSQL 操作时再打开连接池。
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


def _build_faq_schema_sql() -> tuple[str, str, str, str]:
    """根据配置的表名构建 FAQ 表和索引的 DDL。"""
    table_name = get_faq_settings().faq_table_name
    enabled_index_name = f"{table_name}_enabled_idx"
    priority_index_name = f"{table_name}_priority_idx"
    table_sql = f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            need_human SMALLINT NOT NULL DEFAULT 0
                CHECK (need_human IN (0, 1)),
            enabled SMALLINT NOT NULL DEFAULT 1
                CHECK (enabled IN (0, 1)),
            priority INTEGER NOT NULL DEFAULT 0,
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
    migration_sql = f"""
        ALTER TABLE {table_name}
        ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 0,
        ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL
            DEFAULT CURRENT_TIMESTAMP,
        DROP COLUMN IF EXISTS category,
        DROP COLUMN IF EXISTS tags,
        DROP COLUMN IF EXISTS similar_questions,
        DROP COLUMN IF EXISTS answer_type
    """.strip()
    return (
        table_sql,
        enabled_index_sql,
        priority_index_sql,
        migration_sql,
    )


def _ensure_faq_id_generation(cursor: Any, table_name: str) -> None:
    """为旧表补上数据库生成 ID，避免 Excel 再承担主键职责。"""
    cursor.execute(
        """
        SELECT is_identity, column_default
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = %s
          AND column_name = 'id'
        """.strip(),
        (table_name,),
    )
    column = cursor.fetchone()
    if column is None or str(column[0]).upper() == "YES" or column[1]:
        return

    sequence_name = f"{table_name}_id_seq"
    cursor.execute(f"CREATE SEQUENCE IF NOT EXISTS {sequence_name}")
    cursor.execute(
        f"ALTER SEQUENCE {sequence_name} OWNED BY {table_name}.id"
    )
    cursor.execute(
        f"""
        ALTER TABLE {table_name}
        ALTER COLUMN id SET DEFAULT nextval('{sequence_name}')
        """.strip()
    )
    cursor.execute(
        f"""
        SELECT setval(
            '{sequence_name}',
            COALESCE(MAX(id), 0) + 1,
            false
        )
        FROM {table_name}
        """.strip()
    )


def ensure_faq_table(
    connections: PostgreSQLConnectionPool | None = None,
) -> bool:
    """创建 FAQ 表并移除旧版字段。

    Returns ``True`` when this call created the table and ``False`` when the
    table was already present.
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
                    (
                        table_sql,
                        enabled_index_sql,
                        priority_index_sql,
                        migration_sql,
                    ) = (
                        _build_faq_schema_sql()
                    )
                    cursor.execute(table_sql)
                    created = True
                else:
                    (
                        _,
                        enabled_index_sql,
                        priority_index_sql,
                        migration_sql,
                    ) = (
                        _build_faq_schema_sql()
                    )
                cursor.execute(migration_sql)
                cursor.execute(enabled_index_sql)
                cursor.execute(priority_index_sql)
                if exists:
                    _ensure_faq_id_generation(cursor, table_name)
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
        where = (
            "WHERE need_human = 0"
            if include_disabled
            else "WHERE enabled = 1 AND need_human = 0"
        )
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
            WHERE id = %s AND enabled = 1 AND need_human = 0
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
        """加载一个 FAQ 运行时快照所需的全部启用答案。"""
        if not question_ids:
            return {}

        settings = get_faq_settings()
        placeholders = ", ".join(["%s"] * len(question_ids))
        sql = f"""
            SELECT id, answer
            FROM {settings.faq_table_name}
            WHERE id IN ({placeholders})
              AND enabled = 1
              AND need_human = 0
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
            WHERE id = %s AND enabled = 1 AND need_human = 0
            LIMIT 1
        """.strip()


def postgres_config() -> dict[str, Any]:
    """返回配置数据库使用的 psycopg 关键字参数。"""
    return _postgres_kwargs()


def get_postgres_connection():
    """获取一个连接池中的 PostgreSQL 连接。"""
    return PostgreSQLConnectionPool().acquire()
