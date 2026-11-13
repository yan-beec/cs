"""黑名单固定选项：时长只允许四档（2 小时 / 24 小时 / 72 小时 / 永久）；拉黑原因由坐席人工填写（必填）。"""

from __future__ import annotations

from datetime import datetime, timedelta

# 顺序即客户端显示顺序；2 小时用于临时刷屏、情绪激动等短暂冷却。
DURATIONS: dict[str, dict] = {
    "2h": {"label": "2 小时", "seconds": 2 * 60 * 60},
    "24h": {"label": "24 小时", "seconds": 24 * 60 * 60},
    "72h": {"label": "72 小时", "seconds": 72 * 60 * 60},
    "permanent": {"label": "永久", "seconds": None},
}

# 旧版预设原因，只用于显示 2026-11-11 之前的历史记录；新记录不再写 reason_code，
# 原因文字保存在 remark 字段（拉黑=原因，修改时长=修改原因，解除=解除备注）。
LEGACY_REASONS: dict[str, str] = {
    "abuse": "辱骂/骚扰",
    "ads": "广告引流",
    "fraud": "诈骗",
    "spam": "恶意刷屏",
    "other": "其他",
}

ACTION_LABELS: dict[str, str] = {
    "block": "拉黑",
    "change": "修改时长",
    "unblock": "解除拉黑",
    "expire": "到期自动解除",
}

SOURCE_LABELS: dict[str, str] = {
    "client": "坐席客户端",
    "chatwoot": "Chatwoot 网页端",
    "system": "系统",
}

REMARK_MAX_LENGTH = 500
REASON_MAX_LENGTH = REMARK_MAX_LENGTH
CLIENT_TOO_OLD = "当前坐席客户端版本过旧，拉黑原因需要手动填写，请升级坐席客户端后再操作。"
WEB_BLOCK_REMARK = "未填写（Chatwoot 网页端直接拉黑）"
WEB_UNBLOCK_REMARK = "未填写（Chatwoot 网页端直接解除）"
EXPIRE_REMARK = "到期自动解除"


def blocked_until_for(duration: str, now: datetime) -> datetime | None:
    """返回到期时间；永久拉黑返回 None（数据库中记为 infinity）。"""
    if duration not in DURATIONS:
        raise ValueError("拉黑时长只能是 2 小时、24 小时、72 小时或永久。")
    seconds = DURATIONS[duration]["seconds"]
    return None if seconds is None else now + timedelta(seconds=seconds)


def options_payload() -> dict:
    return {
        "durations": [{"code": code, "label": item["label"]} for code, item in DURATIONS.items()],
        "default_duration": "24h",
        # 保留空列表：旧版客户端读不到预设原因时无法提交，提示升级。
        "reasons": [],
        "reason_mode": "text",
        "reason_max_length": REASON_MAX_LENGTH,
        "remark_max_length": REMARK_MAX_LENGTH,
    }
