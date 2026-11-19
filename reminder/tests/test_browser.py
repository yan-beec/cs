"""浏览器复用回归测试，不操作实际浏览器。"""

from unittest.mock import Mock, patch

import pytest

from reminder.client.browser import BrowserOpenError, open_conversation_url
from reminder.client.bridge import BridgeError


URL = "https://support.example/app/accounts/1/conversations/56"


@pytest.mark.parametrize("reused", [True, False])
def test_conversation_is_opened_by_the_connected_plugin(reused):
    bridge = Mock()
    bridge.open.return_value = reused
    assert open_conversation_url(URL, bridge=bridge) is reused
    bridge.open.assert_called_once_with(URL)


def test_disconnected_plugin_never_opens_default_browser():
    with patch("webbrowser.open") as opened:
        with pytest.raises(BrowserOpenError, match="未连接"):
            open_conversation_url(URL)
    opened.assert_not_called()


def test_bridge_error_never_opens_default_browser():
    bridge = Mock()
    bridge.open.side_effect = BridgeError("检测未完成")
    with patch("webbrowser.open") as opened:
        with pytest.raises(BrowserOpenError, match="检测未完成"):
            open_conversation_url(URL, bridge=bridge)
    opened.assert_not_called()


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://user:pass@support.example/app/accounts/1"])
def test_invalid_url_never_reaches_browser(url):
    bridge = Mock()
    with pytest.raises(ValueError):
        open_conversation_url(url, bridge=bridge)
    bridge.open.assert_not_called()
