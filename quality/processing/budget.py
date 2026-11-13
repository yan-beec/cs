"""数据库阶段的截止时间、逐语句检查和连接取消生命周期。"""

from contextlib import contextmanager
from itertools import islice
import threading
import time


class BudgetExceeded(TimeoutError):
    """维护任务已用完本次预算。"""


class MaintenanceBudget:
    """预算约束取连接和数据库语句；回滚与取消线程清理时间另计。"""

    def __init__(self, seconds, *, statement_seconds=60, error_type=BudgetExceeded,
                 clock=time.monotonic, cancel_interval=1, connect_seconds=None,
                 abort_event: threading.Event | None = None, check_interval=.25):
        self.clock = clock
        self.deadline = clock() + seconds
        self.statement_seconds = statement_seconds
        self.error_type = error_type
        self.cancel_interval = cancel_interval
        self.connect_seconds = connect_seconds
        self.abort_event = abort_event
        self.check_interval = check_interval
        self.last_capped = False
        self.expired = threading.Event()
        self.thread = None

    @property
    def reason(self):
        """仅用于异常分类；语句时限被压短不等于预算已经耗尽。"""
        if self.abort_event is not None and self.abort_event.is_set():
            return 'aborted'
        if self.expired.is_set() or self.clock() >= self.deadline:
            return 'expired'
        return None

    def check(self):
        if self.abort_event is not None and self.abort_event.is_set():
            raise self.error_type('数据库任务已取消')
        remaining = self.deadline - self.clock()
        if self.expired.is_set() or remaining <= 0:
            self.expired.set()
            raise self.error_type('维护任务超过总预算')
        return remaining

    @contextmanager
    def connection(self, pool, setup=None):
        # 获取连接也消耗同一份预算；线程只在实际持有连接时运行。
        remaining = self.check()
        timeout = min(self.connect_seconds, remaining) if self.connect_seconds is not None else remaining
        with pool.connection(timeout=timeout) as raw:
            self.check()
            stopped = threading.Event()

            def cancel():
                while not stopped.is_set():
                    if self.abort_event is not None and self.abort_event.is_set():
                        break
                    remaining = self.deadline - self.clock()
                    if remaining <= 0:
                        self.expired.set()
                        break
                    if stopped.wait(min(self.check_interval, remaining)):
                        return
                while not stopped.is_set():
                    try:
                        raw.cancel_safe(timeout=0.5)
                    except Exception:
                        # 取消通道故障不终止看门狗，下一周期继续尝试。
                        pass
                    if stopped.wait(self.cancel_interval):
                        break

            self.thread = threading.Thread(target=cancel, name='maintenance-budget', daemon=True)
            self.thread.start()
            try:
                with raw.transaction():
                    try:
                        # 事务隔离级别必须先于包装连接的 set_config 语句设置。
                        if setup is not None:
                            setup(raw)
                        self.check()
                        yield BudgetConnection(raw, self)
                        self.check()
                    except BaseException:
                        # 失败后清理另计预算，先停止取消，避免重复取消打断回滚。
                        stopped.set()
                        self.thread.join()
                        raise
            finally:
                # 必须先结束取消任务，再将连接归还连接池，避免误取消下一位借用者。
                stopped.set()
                self.thread.join()


class BudgetConnection:
    """统一约束 helper 内部语句和游标批量写入，无需调用方逐处检查。"""

    def __init__(self, raw, budget):
        self.raw, self.budget = raw, budget

    def _prepare(self):
        remaining = self.budget.check()
        self.budget.last_capped = remaining < self.budget.statement_seconds
        milliseconds = max(1, int(min(self.budget.statement_seconds, remaining) * 1000))
        self.raw.execute("SELECT set_config('statement_timeout', %s, true)", (str(milliseconds),))
        self.budget.check()

    def execute(self, query, params=None, **kwargs):
        self._prepare()
        return self.raw.execute(query, params, **kwargs)

    @contextmanager
    def cursor(self, *args, **kwargs):
        self.budget.check()
        with self.raw.cursor(*args, **kwargs) as cursor:
            yield BudgetCursor(cursor, self)


class BudgetCursor:
    """每次写入都检查预算，防止批量语句绕过截止时间。"""

    def __init__(self, raw, connection):
        self.raw, self.connection = raw, connection

    def execute(self, query, params=None, **kwargs):
        self.connection._prepare()
        return self.raw.execute(query, params, **kwargs)

    def fetchmany(self, size):
        self.connection._prepare()
        result = self.raw.fetchmany(size)
        self.connection.budget.check()
        return result

    def executemany(self, query, params_seq):
        def checked(params):
            for item in params:
                self.connection.budget.check()
                yield item

        pending = iter(checked(params_seq))
        while batch := list(islice(pending, 200)):
            self.connection._prepare()
            # 保留驱动原生批量发送；消费参数时也逐条检查，阻止超时后继续排队。
            self.raw.executemany(query, checked(batch))
            self.connection.budget.check()


def classify(budget, error):
    """区分主动取消、总预算与单条语句超时。"""
    from psycopg.errors import QueryCanceled
    if budget.reason == 'aborted':
        return 'aborted'
    if isinstance(error, BudgetExceeded) or budget.reason == 'expired':
        return 'expired'
    if isinstance(error, QueryCanceled):
        return 'expired' if budget.last_capped else 'statement'
    return None
