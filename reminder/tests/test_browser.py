"""浏览器复用回归测试，不操作实际浏览器。"""

import json
from pathlib import Path
import re
import subprocess
from unittest.mock import Mock, patch

import pytest

from reminder.client.browser import BrowserOpenError, open_conversation_url


URL = "https://support.example/app/accounts/1/conversations/56"


@pytest.mark.parametrize("status, reused, launches", [("reused", True, 0), ("not_found", False, 1)])
def test_default_browser_only_opens_when_no_logged_in_tab_exists(status, reused, launches):
    completed = Mock(returncode=0, stdout=json.dumps({"status": status}))
    with (patch("reminder.client.browser.subprocess.run", return_value=completed) as run,
          patch("reminder.client.browser.webbrowser.open", return_value=True) as opened):
        assert open_conversation_url(URL) is reused
    assert opened.call_count == launches
    if launches:
        opened.assert_called_once_with(URL, new=0, autoraise=True)
    assert json.loads(run.call_args.kwargs["input"]) == {"url": URL}
    assert URL not in run.call_args.args[0], "URLs must be data, never shell code"


@pytest.mark.parametrize("completed", [
    Mock(returncode=1, stdout='{"status":"error"}'),
    Mock(returncode=0, stdout='{"status":"error"}'),
    Mock(returncode=0, stdout='{"status":"unknown"}'),
    Mock(returncode=0, stdout='bad output'),
    Mock(returncode=0, stdout='null'),
])
def test_uncertain_detection_does_not_open_duplicate_page(completed):
    with (patch("reminder.client.browser.subprocess.run", return_value=completed),
          patch("reminder.client.browser.webbrowser.open") as opened):
        with pytest.raises(BrowserOpenError):
            open_conversation_url(URL)
    opened.assert_not_called()


def test_timeout_does_not_open_duplicate_page():
    with (patch("reminder.client.browser.subprocess.run", side_effect=subprocess.TimeoutExpired("powershell", 15)),
          patch("reminder.client.browser.webbrowser.open") as opened):
        with pytest.raises(BrowserOpenError):
            open_conversation_url(URL)
    opened.assert_not_called()


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://user:pass@support.example/app/accounts/1"])
def test_invalid_url_never_reaches_browser(url):
    with patch("reminder.client.browser.subprocess.run") as run:
        with pytest.raises(ValueError):
            open_conversation_url(url)
    run.assert_not_called()


def test_windows_bridge_compiles_and_matches_site_and_account():
    """编译实际辅助代码，仅调用纯 URL 方法，不检测/操纵浏览器。"""
    helper = Path(__file__).resolve().parents[1] / "client/browser_windows.ps1"
    source = re.search(r"-TypeDefinition @'\n(.*?)\n'@", helper.read_text(encoding="utf-8"), re.S).group(1)
    command = r"""
        $ErrorActionPreference = 'Stop'
        [Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
        Add-Type -ReferencedAssemblies UIAutomationClient,UIAutomationTypes,WindowsBase,System.Windows.Forms -TypeDefinition ([Console]::In.ReadToEnd())
        $method = [ReminderBrowser].GetMethod('Matches', [Reflection.BindingFlags]'NonPublic,Static')
        $target = [Uri]'https://support.example/help/app/accounts/1/conversations/56'
        $cases = @(
            @('https://support.example/help/app/accounts/1/dashboard', $true),
            @('support.example/help/app/accounts/1/conversations/70', $true),
            @('https://SUPPORT.example:443/help/app/accounts/1', $true),
            @('https://support.example/help/app/login', $false),
            @('https://support.example/help/app/accounts/12', $false),
            @('https://support.example/app/accounts/1', $false),
            @('https://other.example/help/app/accounts/1', $false),
            @('https://support.example:444/help/app/accounts/1', $false),
            @('http://support.example/help/app/accounts/1', $false),
            @('https://user:password@support.example/help/app/accounts/1', $false)
        )
        foreach ($case in $cases) {
            if ($method.Invoke($null, @($case[0], $target)) -ne $case[1]) { throw 'URL matching failed' }
        }
        'OK'
    """
    completed = subprocess.run(
        [r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        input=source, capture_output=True, text=True, encoding="utf-8", timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "OK"
