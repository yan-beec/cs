"""桌面端与浏览器 Native Messaging host 的本机通信。"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import ctypes
import hashlib
import json
from multiprocessing import AuthenticationError
from multiprocessing.connection import Listener
import os
from pathlib import Path
import secrets
import threading
from urllib.parse import urlsplit
from uuid import uuid4

from .storage import crypt

HOST_NAME = "com.ics.reminder"
EXTENSION_ID = "gfbmcpfhhmiibndnkpcmpeecplfffnac"
EXTENSION_ORIGIN = f"chrome-extension://{EXTENSION_ID}/"
MAX_MESSAGE = 65536


class BridgeError(RuntimeError):
    pass


def bridge_key(*, create: bool = False) -> bytes:
    path = Path(os.environ["APPDATA"]) / "ICSReminder" / "browser.key"
    if not path.exists() and create:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"browser-{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(base64.b64encode(crypt(secrets.token_bytes(32))))
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    value = crypt(base64.b64decode(path.read_bytes()), decrypt=True)
    if len(value) != 32:
        raise BridgeError("浏览器连接配置无效，请重新启动提醒客户端。")
    return value


def pipe_address() -> str:
    session = ctypes.c_ulong()
    ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session))
    user = hashlib.sha256(os.environ["APPDATA"].casefold().encode()).hexdigest()[:16]
    return rf"\\.\pipe\ICSReminderBrowser-{user}-{session.value}"


def send_json(connection, value: dict) -> None:
    encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_MESSAGE:
        raise ValueError("Browser message is too large")
    connection.send_bytes(encoded)


def receive_json(connection) -> dict:
    value = json.loads(connection.recv_bytes(MAX_MESSAGE).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Browser message must be an object")
    return value


class BrowserPeer:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.closed = threading.Event()
        self.lock = threading.Lock()
        self.pending: dict[str, list] = {}

    def read(self) -> None:
        try:
            send_json(self.connection, {"type": "ready"})
            while not self.closed.is_set():
                response = receive_json(self.connection)
                with self.lock:
                    pending = self.pending.get(response.get("requestId"))
                    if response.get("type") == "reply" and pending:
                        pending[1] = response.get("result")
                        pending[0].set()
        except (EOFError, OSError, ValueError):
            pass
        finally:
            self.close()

    def request(self, action: str, *, timeout: float = 6, **payload) -> dict:
        identity, pending = uuid4().hex, [threading.Event(), None]
        try:
            with self.lock:
                if self.closed.is_set():
                    raise BridgeError("浏览器插件连接已断开，请在插件设置页重新连接。")
                self.pending[identity] = pending
                send_json(self.connection, {"type": "command", "requestId": identity, "action": action, **payload})
            if not pending[0].wait(timeout) or not isinstance(pending[1], dict):
                raise BridgeError("浏览器插件未及时响应，请重试。")
            return pending[1]
        except (OSError, EOFError) as exc:
            raise BridgeError("浏览器插件连接已断开，请重试。") from exc
        finally:
            with self.lock:
                self.pending.pop(identity, None)

    def close(self) -> None:
        with self.lock:
            if self.closed.is_set():
                return
            self.closed.set()
            for pending in self.pending.values():
                pending[0].set()
            self.connection.close()


class BrowserBridge:
    def __init__(self, *, address=None, authkey: bytes | None = None, family: str = "AF_PIPE") -> None:
        self.closed = threading.Event()
        self.lock = threading.Lock()
        self.open_lock = threading.Lock()
        self.peers: list[BrowserPeer] = []
        self.listener = Listener(address or pipe_address(), family=family, authkey=authkey or bridge_key(create=True))
        self.address = self.listener.address
        self.thread = threading.Thread(target=self._accept, name="ics-browser-bridge", daemon=True)
        self.thread.start()

    def _accept(self) -> None:
        while not self.closed.is_set():
            try:
                connection = self.listener.accept()
            except AuthenticationError:
                continue
            except (OSError, EOFError, AttributeError):
                break

            def register(connection=connection):
                peer = None
                try:
                    if not connection.poll(3):
                        return
                    hello = receive_json(connection)
                    if hello.get("type") != "hello" or hello.get("origin") != EXTENSION_ORIGIN:
                        return
                    peer = BrowserPeer(connection)
                    with self.lock:
                        if self.closed.is_set():
                            return
                        self.peers.append(peer)
                    try:
                        peer.read()
                    finally:
                        with self.lock:
                            self.peers.remove(peer)
                except (OSError, EOFError, ValueError):
                    pass
                finally:
                    peer.close() if peer else connection.close()

            threading.Thread(target=register, name="ics-browser-peer", daemon=True).start()

    @property
    def connected_count(self) -> int:
        with self.lock:
            return sum(not peer.closed.is_set() for peer in self.peers)

    def open(self, url: str) -> bool:
        # 两阶段：所有浏览器只检测，最终只给一个配置文件发送一次导航命令。
        with self.open_lock:
            with self.lock:
                peers = [peer for peer in self.peers if not peer.closed.is_set()]
            if not peers:
                raise BridgeError("浏览器插件未连接，请点击主界面的“浏览器插件安装说明”。")

            def probe(peer):
                try:
                    return peer, peer.request("probe", url=url)
                except BridgeError:
                    return peer, {"status": "error"}

            with ThreadPoolExecutor(max_workers=min(8, len(peers))) as executor:
                answers = list(executor.map(probe, peers))
            candidates = [(peer, value) for peer, value in answers if value.get("status") == "candidate"]
            if candidates:
                peer, value = max(candidates, key=lambda item: (bool(item[1].get("exact")), bool(item[1].get("active"))))
            else:
                statuses = {value.get("status") for _, value in answers}
                if statuses - {"no_tab", "no_permission", "login_required", "site_mismatch"}:
                    raise BridgeError("浏览器页面检测未完成，请重试。")
                available = [(peer, value) for peer, value in answers if value.get("status") == "no_tab"]
                if not available:
                    if "login_required" in statuses:
                        raise BridgeError("Chatwoot 登录已过期，请先在浏览器登录后重试。")
                    if "site_mismatch" in statuses:
                        target = urlsplit(url)
                        raise BridgeError(f"Chatwoot 地址不一致，提醒使用 {target.scheme}://{target.netloc}。请统一服务端公开地址、插件设置和浏览器登录地址。")
                    raise BridgeError("请在浏览器插件设置页填写 Chatwoot 地址并授权。")
                peer, value = available[0]
            result = peer.request("open", url=url, tabId=value.get("tabId"))
            if result.get("status") not in {"reused", "opened"}:
                raise BridgeError("浏览器页面未能打开，请检查登录状态后重试。")
            return result["status"] == "reused"

    def close(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        self.listener.close()
        with self.lock:
            peers = list(self.peers)
        for peer in peers:
            peer.close()
        self.thread.join(timeout=0.5)
