"""优先复用 Windows Chrome/Edge 中已登录的 Chatwoot 工作台标签页。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import webbrowser

from reminder.protocol import validate_base_url


class BrowserOpenError(RuntimeError):
    """检测/导航结果不确定时停止，避免再创建一个重复页面。"""


def open_conversation_url(url: str) -> bool:
    """返回是否复用了标签页；仅明确未找到工作台时交给默认浏览器。"""
    url = validate_base_url(url)
    if os.name == "nt":
        environment = os.environ.copy()
        environment["ICS_REMINDER_BROWSER_SCRIPT"] = str(Path(__file__).with_name("browser_windows.ps1"))
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        try:
            result = subprocess.run(
                [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-STA", "-Command",
                 "& ([scriptblock]::Create([IO.File]::ReadAllText($env:ICS_REMINDER_BROWSER_SCRIPT)))"],
                input=json.dumps({"url": url}), capture_output=True, text=True,
                encoding="utf-8", env=environment, timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            outcome = json.loads(result.stdout.strip())
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise BrowserOpenError("浏览器检测未完成，请重试；也可在已登录的 Chatwoot 中手动进入会话。") from exc
        if not isinstance(outcome, dict):
            raise BrowserOpenError("浏览器返回了未知结果，请重试。")
        if result.returncode != 0 or outcome.get("status") == "error":
            raise BrowserOpenError("无法操作已有浏览器页面，请恢复浏览器窗口后重试。")
        if outcome.get("status") == "reused":
            return True
        if outcome.get("status") != "not_found":
            raise BrowserOpenError("浏览器返回了未知结果，请重试。")
    if not webbrowser.open(url, new=0, autoraise=True):
        raise BrowserOpenError("无法打开浏览器，请检查 Windows 默认浏览器设置。")
    return False
