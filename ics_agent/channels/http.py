"""管理与更新接口共用的渠道参数校验。"""

from fastapi import HTTPException, Query

from .config import DEFAULT_CHANNEL, get_channel


def channel_query(channel_key: str = Query(default=DEFAULT_CHANNEL)) -> str:
    try:
        return get_channel(channel_key).key
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
