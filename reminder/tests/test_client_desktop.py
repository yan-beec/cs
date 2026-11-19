"""桌面运行行为回归；GUI 部分由 ICS_REMINDER_GUI_TESTS=1 显式启用。"""

import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest

from reminder.client.instance import SingleInstance


def headless_reminder_client(item, *, demo=False):
    """只验证按钮与状态逻辑，不创建窗口或托盘。"""
    from reminder.client.main import ReminderClient

    client = ReminderClient.__new__(ReminderClient)
    client.demo = demo
    client.closing = False
    client.browser_busy = False
    client.selected = Mock(return_value=item)
    client.open_button = Mock()
    client.browser_status = Mock()
    client.browser_executor = Mock()
    client.browser_bridge = Mock()
    client.ui_actions = Mock()
    client._set_detail = Mock()
    return client


@pytest.mark.parametrize("is_test, path, label", [
    (True, "dashboard", "打开 Chatwoot 工作台"),
    (False, "conversations/56", "打开 Chatwoot 工作台"),
])
def test_server_trial_and_real_reminder_enable_the_correct_browser_action(is_test, path, label):
    from reminder.client.main import open_conversation_url

    url = "http://localhost:3000/app/accounts/1/" + path
    client = headless_reminder_client({
        "is_test": is_test, "message": "测试提醒", "created_at": "2026-10-10T00:00:00+00:00",
        "conversation_url": url,
    })
    client.show_detail()
    client.open_button.configure.assert_called_with(text=label, state="normal")
    client.open_conversation()
    client.browser_executor.submit.assert_called_once_with(open_conversation_url, url, bridge=client.browser_bridge)
    assert client.browser_busy


def test_dashboard_url_keeps_workbench_label_even_if_old_server_flag_is_missing():
    item = {"is_test": False, "message": "测试提醒", "created_at": "2026-10-10T00:00:00+00:00",
            "conversation_url": "http://localhost:3000/app/accounts/1/dashboard"}
    client = headless_reminder_client(item)
    client.show_detail()
    client.open_button.configure.assert_called_with(text="打开 Chatwoot 工作台", state="normal")


@pytest.mark.parametrize("metadata, label, detail", [
    ({"inbox_id": "7", "inbox_name": "官网咨询"}, "会话 56  ·  客户甲  ·  官网咨询", "收件箱：官网咨询"),
    ({"inbox_id": "7", "inbox_name": ""}, "会话 56  ·  客户甲  ·  收件箱 7", "收件箱 7"),
    ({}, "会话 56  ·  客户甲", None),
    ({"inbox_id": None, "inbox_name": None}, "会话 56  ·  客户甲", None),
    ({"is_test": True, "inbox_id": "7", "inbox_name": "官网咨询"}, "试验提醒  ·  客户甲", None),
])
def test_reminder_displays_inbox_and_accepts_older_server_data(metadata, label, detail):
    from reminder.client.state import ReminderState

    item = {"id": "inbox-test", "version": 1, "is_test": False, "conversation_id": "56",
            "customer_name": "客户甲", "message": "需要人工帮助", "created_at": "2026-10-10T00:00:00+00:00",
            "conversation_url": "http://localhost:3000/app/accounts/1/conversations/56", **metadata}
    client = headless_reminder_client(item)
    client.state = ReminderState()
    client.state.replace([item])
    client.count = Mock()
    client.listbox = Mock()
    client.popup = Mock()
    client.render()
    client.listbox.insert.assert_called_once_with("end", label)
    content = client._set_detail.call_args.args[0]
    assert "需要人工帮助" in content
    if detail:
        assert detail in content
    else:
        assert "收件箱：" not in content
    client.open_button.configure.assert_called_with(text="打开 Chatwoot 工作台", state="normal")
    assert client.state.confirmations == set()
    client.popup.withdraw.assert_not_called()


@pytest.mark.parametrize("result, error, pending, switching, requested_agent, visible", [
    (True, None, True, False, None, True),
    (False, None, True, False, None, True),
    (None, RuntimeError("failed"), True, False, None, True),
    (True, None, False, False, None, False),
    (True, None, True, True, None, False),
    (True, None, True, False, "12", False),
])
def test_open_completion_keeps_popup_visible_without_confirming(result, error, pending, switching, requested_agent, visible):
    from reminder.client.main import ReminderClient

    client = headless_reminder_client({
        "is_test": True, "message": "测试提醒", "created_at": "2026-10-10T00:00:00+00:00",
        "conversation_url": "http://localhost:3000/app/accounts/1/dashboard",
    })
    client.state = Mock()
    client.state.visible.return_value = [{"id": "pending", "version": 1}] if pending else []
    client.popup = Mock()
    client.results = Mock()
    client.next_ring = time.monotonic() + 30
    client.repeat_seconds = 30
    client.browser_status = Mock()
    client.show_detail = Mock()
    client.tick = ReminderClient.tick.__get__(client)
    client.results.get_nowait.side_effect = [("browser", result, error), __import__("queue").Empty]
    client.ui_actions.get_nowait.side_effect = __import__("queue").Empty
    client.token = ""
    client.switching = switching
    client.requested_agent = requested_agent
    client.browser_bridge = None
    client.browser_connection = Mock()
    client.browser_connection.get.return_value = ""
    client.root = Mock()
    client.root.after = Mock()
    client._update_tray = Mock()
    client.tick()
    if visible:
        client.popup.deiconify.assert_called_once()
        client.popup.attributes.assert_any_call("-topmost", True)
    else:
        client.popup.deiconify.assert_not_called()
    client.popup.withdraw.assert_not_called()
    client.state.confirm.assert_not_called()


@pytest.mark.parametrize("demo, explanation", [(True, "本机演示未连接 ICS"), (False, "未收到 Chatwoot 地址")])
def test_missing_browser_url_explains_disabled_action(demo, explanation):
    client = headless_reminder_client({
        "is_test": True, "message": "测试提醒", "created_at": "2026-10-10T00:00:00+00:00",
        "conversation_url": "",
    }, demo=demo)
    client.show_detail()
    client.open_button.configure.assert_called_with(text="打开 Chatwoot 工作台", state="disabled")
    assert explanation in client._set_detail.call_args.args[0]
    assert explanation in client.browser_status.set.call_args.args[0]
    client.open_conversation()
    assert explanation in client.browser_status.set.call_args.args[0]
    client.browser_executor.submit.assert_not_called()
    assert not client.browser_busy


def isolated_gui_case(name: str) -> bool:
    """Conda Tcl 的每个 GUI 用例用独立进程，模拟真实单 Tk 程序的生命周期。"""
    if os.environ.get("ICS_REMINDER_GUI_CHILD") == "1":
        return False
    environment = os.environ.copy()
    environment["ICS_REMINDER_GUI_CHILD"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(Path(__file__).resolve()) + "::" + name, "-q"],
        env=environment, capture_output=True, text=True, timeout=25,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return True


@pytest.mark.skipif(os.name != "nt", reason="Windows named event and mutex")
def test_second_instance_wakes_owner_and_releases_lock():
    name = "ICSReminder-Test-" + uuid4().hex
    first = SingleInstance(name=name)
    wake = threading.Event()
    try:
        assert first.acquired
        first.watch(wake.set)
        second = SingleInstance(name=name)
        assert not second.acquired
        assert wake.wait(2)
        second.close()
    finally:
        first.close()
    third = SingleInstance(name=name)
    try:
        assert third.acquired
    finally:
        third.close()


@pytest.mark.skipif(os.environ.get("ICS_REMINDER_GUI_TESTS") != "1", reason="Explicit desktop test run")
def test_background_poll_popup_tray_restore_and_shutdown():
    if isolated_gui_case("test_background_poll_popup_tray_restore_and_shutdown"):
        return
    import customtkinter as ctk
    from reminder.client.main import ApiError, ReminderClient

    root = ctk.CTk()
    errors = []
    root.report_callback_exception = lambda *args: errors.append(args)
    with (patch("reminder.client.main.load_config", return_value=None),
          patch("reminder.client.main.save_config"), patch("reminder.client.main.clear_config"),
          patch("reminder.client.main.request") as request, patch("winsound.PlaySound")):
        client = ReminderClient(root)
        try:
            deadline = time.monotonic() + 3
            while not client.tray_ready.is_set() and time.monotonic() < deadline:
                root.update()
            assert client.tray_ready.is_set(), "Tray icon did not become available"
            root.update()
            assert client.bind_button.winfo_ismapped()

            item = {"id": "gui-test", "version": 1, "is_test": True,
                    "conversation_id": "demo", "customer_name": "试验客户",
                    "message": "桌面测试" * 400,
                    "created_at": "2026-10-10T00:00:00+00:00", "conversation_url": ""}
            request.return_value = {"notifications": [item], "poll_seconds": 2, "repeat_seconds": 30}
            client.connected({"server": "http://127.0.0.1:3001", "token": "test-device", "agent_name": "测试坐席"})
            client.hide_window()
            root.update()
            assert root.state() == "withdrawn"
            client.tick()
            deadline = time.monotonic() + 3
            while not client.state.visible() and time.monotonic() < deadline:
                root.update()
            root.update()
            assert root.state() == "withdrawn", "Background poll must not restore the main window"
            assert client.popup.state() == "normal"
            assert client.popup.attributes("-topmost")
            client.close_popup()
            root.update()
            assert client.popup.state() == "normal"
            assert client.state.confirmations == set()
            assert client.open_button.winfo_ismapped(), "Long messages must not hide action buttons"
            client.popup.geometry("400x300")
            root.update()
            assert client.open_button.winfo_y() + client.open_button.winfo_height() <= client.open_button.master.winfo_height()
            popup_size = (client.popup.winfo_width(), client.popup.winfo_height())
            client.popup.withdraw()
            client.show_popup()
            root.update()
            assert (client.popup.winfo_width(), client.popup.winfo_height()) == popup_size
            assert client.open_button.winfo_ismapped()
            assert request.call_count >= 1

            client.ui_actions.put("show")
            client.tick()
            root.update()
            assert root.state() == "normal"
            second = {**item, "id": "gui-test-2", "message": "第二条提醒"}
            request.return_value = {"notifications": [item, second], "poll_seconds": 2, "repeat_seconds": 30}
            client.state.replace([item, second])
            client.show_popup()
            root.update()
            client.confirm()
            root.update()
            assert [entry["id"] for entry in client.state.visible()] == ["gui-test-2"]
            assert client.popup.state() == "normal"
            client.confirm()
            root.update()
            assert client.state.visible() == []
            assert client.popup.state() == "withdrawn"
            assert {("gui-test", 1), ("gui-test-2", 1)} <= client.state.confirmations

            root.iconify()
            root.update()
            root.update()
            assert root.state() == "withdrawn", "Native minimize should hide to tray"

            # 登录失败也应允许重试，不要永久禁用登录按钮。
            client.results.put(("agents", None, ApiError(401, "登录失败")))
            client.tick()
            assert client.login_button.cget("state") == "normal"
            assert client.token == "test-device", "Admin login failure must not revoke device identity"
            assert not errors
        finally:
            client.quit()
            client.tray_thread.join(timeout=3)
        assert not client.tray_thread.is_alive()


@pytest.mark.skipif(os.environ.get("ICS_REMINDER_GUI_TESTS") != "1", reason="Explicit desktop test run")
def test_browser_lookup_is_async_and_keeps_reminder_unconfirmed():
    if isolated_gui_case("test_browser_lookup_is_async_and_keeps_reminder_unconfirmed"):
        return
    import customtkinter as ctk
    from reminder.client.main import ReminderClient

    root = ctk.CTk()
    release = threading.Event()
    started = threading.Event()

    def open_url(url, **kwargs):
        started.set()
        assert release.wait(3)
        return True

    with (patch("reminder.client.main.load_config", return_value=None),
          patch("reminder.client.main.open_conversation_url", side_effect=open_url) as opened,
          patch("winsound.PlaySound")):
        client = ReminderClient(root, demo=True)
        try:
            item = {"id": "browser-test", "version": 1, "is_test": False,
                    "conversation_id": "56", "customer_name": "测试客户", "message": "转人工",
                    "inbox_id": "7", "inbox_name": "官网咨询收件箱名称" * 12,
                    "created_at": "2026-10-10T00:00:00+00:00",
                    "conversation_url": "https://support.example/app/accounts/1/conversations/56"}
            client.state.replace([item])
            client.show_popup()
            root.update()
            assert item["inbox_name"] in client.listbox.get(0)
            assert "收件箱：" + item["inbox_name"] in client.detail.get("1.0", "end")
            assert client.open_button.winfo_ismapped()
            client.open_conversation()
            assert started.wait(1)
            client.open_conversation()
            assert opened.call_count == 1
            assert client.browser_busy
            assert not client.busy, "Browser detection must not occupy the ICS worker"
            assert client.state.confirmations == set()
            release.set()
            deadline = time.monotonic() + 3
            while client.browser_busy and time.monotonic() < deadline:
                root.update()
            assert not client.browser_busy
            assert client.popup.state() == "normal"
            assert client.state.visible() == [item]
            assert client.state.confirmations == set()
        finally:
            release.set()
            client.quit()
            client.tray_thread.join(timeout=3)


@pytest.mark.skipif(os.environ.get("ICS_REMINDER_GUI_TESTS") != "1", reason="Explicit desktop test run")
def test_shared_seat_switch_keeps_confirmation_in_its_own_seat():
    if isolated_gui_case("test_shared_seat_switch_keeps_confirmation_in_its_own_seat"):
        return
    import customtkinter as ctk
    from reminder.client.main import ReminderClient

    root = ctk.CTk()
    errors = []
    root.report_callback_exception = lambda *args: errors.append(args)
    a = {"id": "pending-a", "version": 1, "is_test": True, "conversation_id": "demo-a",
         "customer_name": "A 的客户", "message": "请转人工",
         "created_at": "2026-10-10T00:00:00+00:00", "conversation_url": ""}
    b = {**a, "id": "pending-b", "customer_name": "B 的客户"}
    remote = {"agent_id": "11", "agent_name": "张三", "binding_version": 1, "switchable": True}
    agents = [{"id": "11", "name": "张三", "availability_status": "online"},
              {"id": "12", "name": "李四", "availability_status": "offline"}]

    def response(server, path, *, token="", payload=None):
        if path == "/reminder/device/switch":
            remote.update(agent_id=payload["agent_id"], agent_name="李四" if payload["agent_id"] == "12" else "张三",
                          binding_version=remote["binding_version"] + 1)
            return dict(remote)
        if path == "/reminder/device/agents":
            return {**remote, "agents": agents}
        return {**remote, "notifications": [a] if remote["agent_id"] == "11" else [b]}

    with (patch("reminder.client.main.load_config", return_value=None),
          patch("reminder.client.main.save_config") as saved,
          patch("reminder.client.main.clear_config"), patch("winsound.PlaySound"),
          patch("reminder.client.main.request", side_effect=response) as request):
        client = ReminderClient(root)
        try:
            client.connected({"server": "http://127.0.0.1:3001", "token": "plugin-device",
                              "device_id": "shared-pc", **remote})
            client._set_agent_choices(agents)
            client.state.replace([a])
            client.state.confirm(a)
            client.next_poll = time.monotonic() + 100
            client.switch_box.set(client.agent_display_values[1])
            client.switch_agent()
            deadline = time.monotonic() + 3
            while (client.current_agent_id != "12" or client.switching) and time.monotonic() < deadline:
                root.update()
            assert client.current_agent_id == "12"
            assert [item["id"] for item in client.state.visible()] == ["pending-b"]
            assert ("pending-a", 1) in client.states["11"].confirmations
            assert client.state.confirmations == set()
            assert not any(call.args[1].endswith("pending-a/ack") for call in request.call_args_list)
            assert saved.call_args.args[0]["agent_id"] == "12"
            assert "password" not in saved.call_args.args[0]
            assert "plugin_token" not in saved.call_args.args[0]

            client.next_poll = time.monotonic() + 100
            client.switch_box.set(client.agent_display_values[0])
            client.switch_agent()
            deadline = time.monotonic() + 3
            while (client.current_agent_id != "11" or client.switching) and time.monotonic() < deadline:
                root.update()
            assert client.current_agent_id == "11"
            assert client.state.visible() == []
            ack = next(call for call in request.call_args_list if call.args[1].endswith("pending-a/ack"))
            assert ack.kwargs["payload"]["binding_version"] == 3
            assert not errors
        finally:
            client.quit()
            client.tray_thread.join(timeout=3)
