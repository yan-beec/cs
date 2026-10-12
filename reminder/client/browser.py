"""通过浏览器插件复用 Chatwoot 标签页，支持 Chrome/Edge 多个配置文件。"""
from reminder.protocol import validate_base_url
from .bridge import BrowserBridge, BridgeError


class BrowserOpenError(RuntimeError):
    pass


def open_conversation_url(url: str, *, bridge: BrowserBridge | None = None) -> bool:
    url = validate_base_url(url)
    if bridge is None:
        raise BrowserOpenError("浏览器插件未连接，请先完成插件安装并启动提醒客户端。")
    try:
        return bridge.open(url)
    except BridgeError as exc:
        raise BrowserOpenError(str(exc)) from exc
