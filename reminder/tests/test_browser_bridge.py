import io
import json
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client
import os
import struct
import subprocess
import sys
import threading
from unittest.mock import Mock

import pytest

from reminder.client.bridge import BrowserBridge, BridgeError, EXTENSION_ORIGIN, MAX_MESSAGE, receive_json, send_json
from reminder.client.native_host import read_message, write_message

URL = "https://support.example/app/accounts/1/conversations/56"


def test_native_message_unicode_roundtrip_and_no_protocol_noise():
    stream = io.BytesIO()
    value = {"type": "reply", "result": {"status": "客户会话"}}
    write_message(stream, value)
    assert struct.unpack("<I", stream.getvalue()[:4])[0] == len(stream.getvalue()) - 4
    stream.seek(0)
    assert read_message(stream) == value
    assert read_message(stream) is None


@pytest.mark.parametrize("payload", [b"x", struct.pack("<I", MAX_MESSAGE + 1), struct.pack("<I", 5) + b"{}", struct.pack("<I", 2) + b"[]"])
def test_invalid_native_frames_are_rejected(payload):
    with pytest.raises(ValueError):
        read_message(io.BytesIO(payload))


def bridge_with_peers(peers):
    bridge = BrowserBridge.__new__(BrowserBridge)
    bridge.peers = peers
    bridge.lock = threading.Lock()
    bridge.open_lock = threading.Lock()
    return bridge


def peer(probe, result="reused"):
    value = Mock()
    value.closed.is_set.return_value = False
    value.request.side_effect = lambda action, **kwargs: probe if action == "probe" else {"status": result}
    return value


def test_two_profiles_only_navigate_the_one_with_the_exact_conversation():
    first = peer({"status": "candidate", "tabId": 2, "exact": False, "active": True})
    second = peer({"status": "candidate", "tabId": 8, "exact": True, "active": False})
    assert bridge_with_peers([first, second]).open(URL)
    assert [call.args[0] for call in first.request.call_args_list] == ["probe"]
    second.request.assert_any_call("open", url=URL, tabId=8)


@pytest.mark.parametrize("status", ["error", "login_required", "no_permission", "site_mismatch"])
def test_failure_does_not_navigate_any_profile(status):
    browser = peer({"status": status})
    with pytest.raises(BridgeError):
        bridge_with_peers([browser]).open(URL)
    assert [call.args[0] for call in browser.request.call_args_list] == ["probe"]


def test_site_mismatch_explains_the_actual_reminder_origin():
    browser = peer({"status": "site_mismatch"})
    with pytest.raises(BridgeError, match="地址不一致.*http://localhost:3000"):
        bridge_with_peers([browser]).open("http://localhost:3000/app/accounts/1/dashboard")
    assert [call.args[0] for call in browser.request.call_args_list] == ["probe"]


def test_matching_workbench_profile_is_used_despite_other_profile_site_mismatch():
    mismatch = peer({"status": "site_mismatch"})
    logged_in = peer({"status": "candidate", "tabId": 7})
    dashboard = "http://localhost:3000/app/accounts/1/dashboard"
    assert bridge_with_peers([mismatch, logged_in]).open(dashboard)
    logged_in.request.assert_any_call("open", url=dashboard, tabId=7)
    assert [call.args[0] for call in mismatch.request.call_args_list] == ["probe"]


def test_uncertain_other_profile_prevents_creating_duplicate_tab():
    first, second = peer({"status": "no_tab"}, "opened"), peer({"status": "error"})
    with pytest.raises(BridgeError):
        bridge_with_peers([first, second]).open(URL)
    assert all(call.args[0] == "probe" for call in first.request.call_args_list)


def test_no_tab_opens_inside_the_connected_browser():
    browser = peer({"status": "no_tab"}, "opened")
    assert bridge_with_peers([browser]).open(URL) is False
    browser.request.assert_any_call("open", url=URL, tabId=None)


@pytest.mark.skipif(os.name != "nt", reason="Windows named pipe + DPAPI")
def test_native_host_real_pipe_and_browser_framing_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    bridge = BrowserBridge()
    host = os.environ.get("ICS_REMINDER_HOST_TEST_EXE")
    process = subprocess.Popen(
        [host, EXTENSION_ORIGIN] if host else [sys.executable, "-m", "reminder.client.native_host", EXTENSION_ORIGIN],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    ready, messages = threading.Event(), []

    def browser():
        try:
            while True:
                message = read_message(process.stdout)
                if not message:
                    return
                if message["type"] == "ready":
                    ready.set()
                elif message["type"] == "command":
                    messages.append(message)
                    result = {"status": "candidate", "tabId": 7} if message["action"] == "probe" else {"status": "reused"}
                    write_message(process.stdin, {"type": "reply", "requestId": message["requestId"], "result": result})
                elif message["type"] == "offline":
                    return
        except (OSError, ValueError):
            pass

    thread = threading.Thread(target=browser, daemon=True)
    thread.start()
    try:
        assert ready.wait(5), "Host did not connect to the desktop bridge"
        assert bridge.connected_count == 1
        assert bridge.open(URL)
        assert [message["action"] for message in messages] == ["probe", "open"]
        assert messages[-1]["tabId"] == 7
        assert all("token" not in json.dumps(message) for message in messages)
    finally:
        process.stdin.close()
        process.wait(timeout=5)
        assert process.returncode == 0
        bridge.close()
        thread.join(timeout=2)
        process.stdout.close()
        assert not process.stderr.read(), "Native host must not emit diagnostics in normal operation"
        process.stderr.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows named pipe")
def test_wrong_origin_and_wrong_secret_do_not_disable_the_bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from reminder.client.bridge import bridge_key
    bridge = BrowserBridge()
    try:
        with pytest.raises(AuthenticationError):
            Client(bridge.address, family="AF_PIPE", authkey=b"wrong-key")
        connection = Client(bridge.address, family="AF_PIPE", authkey=bridge_key())
        send_json(connection, {"type": "hello", "origin": "chrome-extension://wrong/"})
        assert connection.poll(3)
        with pytest.raises(EOFError):
            receive_json(connection)
        connection.close()
        assert bridge.connected_count == 0
        connection = Client(bridge.address, family="AF_PIPE", authkey=bridge_key())
        send_json(connection, {"type": "hello", "origin": EXTENSION_ORIGIN})
        assert connection.poll(3)
        assert receive_json(connection) == {"type": "ready"}
        connection.close()
    finally:
        bridge.close()
