"""为用户排行补充排队实时计数；不使用报表日期或渠道裁剪排队窗口。"""
import logging
from datetime import datetime, timedelta, timezone

from ics_agent.app.handoff_queue.policy import week_anchor
from ics_agent.app.handoff_queue.runtime import get_handoff_queue_runtime
from quality.assignment.config import AssignmentSyncSettings

logger = logging.getLogger(__name__)


def attach_queue_counts(report, budget):
    """只补充当前页或导出前百名；读取异常时保留排行并明确显示缺失。"""
    users = report["users"]
    for user in users:
        user.update(daily_count=None, weekly_count=None)
    now = datetime.now(timezone.utc)
    metadata = {"available": False, "as_of": now.isoformat(),
                "daily_start": (now - timedelta(days=1)).isoformat(), "week_start": week_anchor(now).isoformat()}
    report["queue_counts"] = metadata
    budget.check()
    try:
        runtime = get_handoff_queue_runtime()
        if not runtime.ready or runtime.repository is None:
            return report
        source_key = AssignmentSyncSettings().source_key
        if not source_key:
            return report
        # 排队按账号和联系人计数，仅为本实例来源关联，避免其他归档来源的同号用户串数。
        matched = [user for user in users if user["source_key"] == source_key]
        keys = list(dict.fromkeys((str(user["account_id"]), str(user["contact_id"])) for user in matched))
        snapshot = runtime.repository.counter_snapshot(keys, budget=budget)
        budget.check()
        now = snapshot["as_of"]
        metadata.update(available=True, as_of=now.isoformat(),
                        daily_start=(now - timedelta(days=1)).isoformat(), week_start=snapshot["week_start"].isoformat())
        for user in matched:
            user["daily_count"], user["weekly_count"] = snapshot["counts"][(str(user["account_id"]), str(user["contact_id"]))]
    except Exception as error:
        # 取消或总预算耗尽继续交给上层处理；普通计数故障不能变成虚假的零。
        budget.check()
        logger.warning("Statistics queue counts unavailable: %s", type(error).__name__)
    return report
