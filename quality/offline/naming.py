"""留档标题与下载文件名：按月为“聊天记录2026年10月”，全部历史带导出日期，筛选导出精确到分钟（同一天多次筛选导出不重名）。"""
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))


def archive_title(scope, at):
    """at 可为 datetime 或 Unix 时间戳；日期统一按北京时间。"""
    scope = scope or {}
    moment = at.astimezone(BEIJING) if isinstance(at, datetime) else datetime.fromtimestamp(at, BEIJING)
    mode = scope.get("mode")
    if mode == "month" and scope.get("month"):
        year, month = scope["month"].split("-")
        return f"聊天记录{int(year)}年{int(month)}月"
    day = f"{moment:%Y%m%d}"
    if mode == "all":
        return f"聊天记录全部历史-{day}"
    # 同一天会按不同条件多次筛选导出，精确到分钟避免下载文件名重复。
    return f"聊天记录筛选导出-{moment:%Y%m%d-%H%M}"


def media_folder(scope, at):
    """附件包内的顶层文件夹，也是 HTML 引用附件的相对目录。

    用纯 ASCII：Windows 自带解压对 ZIP 内中文文件名支持不一，乱码会让页面找不到附件。
    与标题一一对应，多个月的留档放在同一文件夹里也不会互相覆盖。
    """
    scope = scope or {}
    moment = at.astimezone(BEIJING) if isinstance(at, datetime) else datetime.fromtimestamp(at, BEIJING)
    mode = scope.get("mode")
    if mode == "month" and scope.get("month"):
        year, month = scope["month"].split("-")
        return f"ics-attachments-{int(year)}-{int(month):02d}"
    if mode == "all":
        return f"ics-attachments-all-{moment:%Y%m%d}"
    return f"ics-attachments-filter-{moment:%Y%m%d-%H%M}"


def download_name(scope, at, suffix=".html"):
    return archive_title(scope, at) + suffix
