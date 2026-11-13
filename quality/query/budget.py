"""归档读取预算。"""

from quality.processing.budget import BudgetExceeded, MaintenanceBudget

ARCHIVE_BUDGET_SECONDS = 20


class ArchiveBudgetExceeded(BudgetExceeded):
    """归档数据库读取阶段已用完预算。"""


def archive_budget(seconds=ARCHIVE_BUDGET_SECONDS, *, abort_event=None, **kwargs):
    return MaintenanceBudget(seconds, statement_seconds=3, connect_seconds=1,
                             abort_event=abort_event, error_type=ArchiveBudgetExceeded, **kwargs)


def read_only_snapshot(connection):
    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
