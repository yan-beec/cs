"""ICS 客服桌面提醒客户端。

客户端复用 ICS 的现有提醒接口，界面使用 CustomTkinter，运行时驻留
Windows 系统托盘。关闭窗口只会隐藏界面，只有托盘菜单中的“退出程序”
才会结束轮询。
"""

from __future__ import annotations

import argparse
import json
import queue
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

import customtkinter as ctk
import pystray
from PIL import ImageTk

from reminder.protocol import validate_base_url
from .browser import BrowserOpenError, open_conversation_url
from .icons import reminder_icon
from .instance import SingleInstance
from .state import ReminderState
from .storage import clear_config, load_config, save_config


ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")
ctk.ThemeManager.theme["CTkFont"]["family"] = "Microsoft YaHei UI"

BG = "#0f172a"
PANEL = "#17233b"
PANEL_2 = "#1e2e4a"
TEXT = "#f8fafc"
MUTED = "#9fb0c9"
BLUE = "#3b82f6"
BLUE_HOVER = "#2563eb"
GREEN = "#22c55e"
ORANGE = "#f59e0b"
RED = "#ef4444"


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def request(base: str, path: str, *, token: str = "", payload: dict | None = None) -> dict:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    query = Request(validate_base_url(base) + path, data=data, headers=headers)
    try:
        with urlopen(query, timeout=30) as response:
            return json.load(response)
    except HTTPError as exc:
        try:
            detail = json.load(exc).get("detail")
        except (ValueError, AttributeError):
            detail = None
        raise ApiError(exc.code, detail if isinstance(detail, str) else f"接口返回 {exc.code}。") from None
    except (URLError, TimeoutError) as exc:
        raise ApiError(0, "无法连接 ICS，请检查地址、网络和服务状态。") from exc


class ReminderClient:
    def __init__(self, root: tk.Tk, *, demo: bool = False) -> None:
        self.root, self.demo = root, demo
        self.root.title("ICS 客服提醒")
        self.root.geometry("820x720")
        self.root.minsize(740, 560)
        self.root.configure(fg_color=BG)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
        self.root.bind("<Unmap>", self._on_minimize, add="+")

        self.state = ReminderState()
        self.visible: list[dict] = []
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.browser_executor = ThreadPoolExecutor(max_workers=1)
        self.browser_busy = False
        self.results: queue.SimpleQueue = queue.SimpleQueue()
        self.ui_actions: queue.SimpleQueue = queue.SimpleQueue()
        self.tray_ready = threading.Event()
        self.closing = False
        self._tick_job: str | None = None
        self._tray_signature: tuple | None = None
        self.token = ""
        self.plugin_token = ""
        self.login_server = ""
        self.device_id = ""
        self.current_agent_id = ""
        self.active_agent_name = "尚未选择席位"
        self.binding_version = 1
        self.switchable = False
        self.states: dict[str, ReminderState] = {}
        self.requested_agent: dict | None = None
        self.switching = False
        self.next_agents_refresh = 0.0
        self.agent_values: list[dict] = []
        self.agent_display_values: list[str] = []
        self.busy = False
        self.next_poll = 0.0
        self.next_ring = 0.0
        self.poll_seconds, self.repeat_seconds = 2, 30
        self.tray_icon: pystray.Icon | None = None
        self.tray_thread: threading.Thread | None = None
        self.stop_network = threading.Event()

        self.server = tk.StringVar(value="http://127.0.0.1:3001")
        self.client_server = tk.StringVar()
        self.username = tk.StringVar(value="reminder")
        self.password = tk.StringVar()
        self.remember = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="使用提醒插件账号登录，之后可直接切换当前客服席位。")
        self.agent_name = tk.StringVar(value="尚未绑定客服坐席")
        self.browser_status = tk.StringVar(value="拖动窗口边框可调整大小")

        self._build_main_window()
        self._build_popup()
        self._start_tray()
        self.root.after(350, self._set_window_icons)

        if demo:
            self._prepare_demo()
        else:
            try:
                config = load_config()
                if config:
                    self.connected(config)
            except Exception:
                self.status.set("本机凭据无法读取，请重新登录提醒插件。")
        self._tick_job = self.root.after(200, self.tick)

    def _set_window_icons(self) -> None:
        self.window_icon = ImageTk.PhotoImage(reminder_icon(size=64), master=self.root)
        self.root.iconphoto(True, self.window_icon)
        self.popup.iconphoto(False, self.window_icon)

    def _build_main_window(self) -> None:
        outer = ctk.CTkFrame(self.root, fg_color=BG, corner_radius=0)
        outer.pack(fill="both", expand=True)

        header = ctk.CTkFrame(outer, fg_color=BG, corner_radius=0)
        header.pack(fill="x", padx=30, pady=(25, 10))
        logo = ctk.CTkFrame(header, width=48, height=48, corner_radius=14, fg_color=BLUE)
        logo.pack(side="left")
        logo.pack_propagate(False)
        ctk.CTkLabel(logo, text="↗", font=ctk.CTkFont(size=27, weight="bold"), text_color="white").pack(expand=True)
        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.pack(side="left", padx=14)
        ctk.CTkLabel(title_box, text="ICS 客服提醒", font=ctk.CTkFont(size=23, weight="bold"), text_color=TEXT).pack(anchor="w")
        ctk.CTkLabel(title_box, text="转人工会话 · 桌面置顶提醒", font=ctk.CTkFont(size=12), text_color=MUTED).pack(anchor="w", pady=(3, 0))
        ctk.CTkButton(header, text="—", width=38, height=32, fg_color="transparent", hover_color=PANEL_2, text_color=MUTED, command=self.hide_window).pack(side="right")

        self.status_card = ctk.CTkFrame(outer, fg_color=PANEL, corner_radius=14)
        self.status_card.pack(fill="x", padx=30, pady=(5, 16))
        status_left = ctk.CTkFrame(self.status_card, fg_color="transparent")
        status_left.pack(side="left", padx=18, pady=14)
        self.status_dot = ctk.CTkLabel(status_left, text="●", width=18, font=ctk.CTkFont(size=16), text_color=MUTED)
        self.status_dot.pack(side="left")
        status_text = ctk.CTkFrame(status_left, fg_color="transparent")
        status_text.pack(side="left", padx=(8, 0))
        ctk.CTkLabel(status_text, textvariable=self.agent_name, font=ctk.CTkFont(size=13, weight="bold"), text_color=TEXT).pack(anchor="w")
        ctk.CTkLabel(status_text, textvariable=self.status, font=ctk.CTkFont(size=11), text_color=MUTED, wraplength=570, justify="left").pack(anchor="w", pady=(3, 0))

        footer = ctk.CTkFrame(outer, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=30, pady=(0, 18))
        ctk.CTkLabel(footer, text="关闭窗口后程序会继续在系统托盘运行", text_color=MUTED, font=ctk.CTkFont(size=11)).pack(side="left")
        ctk.CTkButton(footer, text="退出程序", width=90, height=30, fg_color="transparent", border_width=1, border_color="#475569", hover_color="#334155", text_color=MUTED, command=self.quit).pack(side="right")
        self.content = ctk.CTkFrame(outer, fg_color="transparent")
        self.content.pack(fill="both", expand=True, padx=30, pady=(0, 18))
        self.setup = ctk.CTkScrollableFrame(self.content, fg_color=PANEL, corner_radius=14)
        self.setup.pack(fill="both", expand=True)
        self._build_setup()
        self.dashboard = ctk.CTkFrame(self.content, fg_color=PANEL, corner_radius=14)
        self._build_dashboard()

    def _build_setup(self) -> None:
        ctk.CTkLabel(self.setup, text="登录提醒插件", font=ctk.CTkFont(size=18, weight="bold"), text_color=TEXT).pack(anchor="w", padx=24, pady=(22, 3))
        ctk.CTkLabel(self.setup, text="使用插件专用密码。共享电脑交接时，在主界面切换席位即可。", text_color=MUTED, font=ctk.CTkFont(size=12)).pack(anchor="w", padx=24, pady=(0, 15))
        form = ctk.CTkFrame(self.setup, fg_color="transparent")
        form.pack(fill="x", padx=24)
        self._field(form, "ICS 地址", self.server)
        self._field(form, "提醒地址（可选）", self.client_server)
        self._field(form, "账号", self.username)
        self._field(form, "密码", self.password, show="•")
        self.login_button = ctk.CTkButton(self.setup, text="登录插件并读取授权席位", height=40, corner_radius=9, fg_color=BLUE, hover_color=BLUE_HOVER, font=ctk.CTkFont(size=13, weight="bold"), command=self.load_agents)
        self.login_button.pack(fill="x", padx=24, pady=(17, 10))
        ctk.CTkLabel(self.setup, text="坐席选择", text_color=MUTED, font=ctk.CTkFont(size=12)).pack(anchor="w", padx=24, pady=(3, 4))
        self.agents_box = ctk.CTkComboBox(self.setup, values=["请先登录 ICS"], height=38, state="readonly", fg_color=PANEL_2, border_color="#334b70", button_color=BLUE, button_hover_color=BLUE_HOVER)
        self.agents_box.pack(fill="x", padx=24)
        self.agents_box.set("请先登录 ICS")
        ctk.CTkCheckBox(self.setup, text="在本机记住设备（Windows 用户加密保存）", variable=self.remember, text_color=MUTED, checkbox_width=19, checkbox_height=19).pack(anchor="w", padx=24, pady=(13, 5))
        self.bind_button = ctk.CTkButton(self.setup, text="使用所选席位并发送试验提醒", height=38, corner_radius=9, fg_color="#28507f", hover_color=BLUE_HOVER, state="disabled", command=self.bind)
        self.bind_button.pack(fill="x", padx=24, pady=(5, 20))

    def _field(self, parent: ctk.CTkFrame, label: str, variable: tk.StringVar, *, show: str = "") -> None:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=4)
        ctk.CTkLabel(row, text=label, width=145, anchor="w", text_color=MUTED).pack(side="left")
        entry = ctk.CTkEntry(row, textvariable=variable, height=36, fg_color="#111c31", border_color="#334b70", show=show)
        entry.pack(side="left", fill="x", expand=True)

    def _build_dashboard(self) -> None:
        ctk.CTkLabel(self.dashboard, text="提醒服务已就绪", font=ctk.CTkFont(size=19, weight="bold"), text_color=TEXT).pack(anchor="w", padx=24, pady=(25, 3))
        ctk.CTkLabel(self.dashboard, text="转人工后，提醒会自动置顶显示并播放提示音。", text_color=MUTED).pack(anchor="w", padx=24, pady=(0, 22))
        seats = ctk.CTkFrame(self.dashboard, fg_color=PANEL_2, corner_radius=10)
        seats.pack(fill="x", padx=24, pady=(0, 16))
        ctk.CTkLabel(seats, text="当前值班席位", text_color=MUTED).pack(anchor="w", padx=16, pady=(14, 8))
        row = ctk.CTkFrame(seats, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 12))
        self.switch_box = ctk.CTkComboBox(row, values=["正在读取席位…"], state="readonly", height=38, fg_color=PANEL, border_color="#334b70", button_color=BLUE)
        self.switch_box.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.switch_box.set("正在读取席位…")
        self.switch_button = ctk.CTkButton(row, text="切换席位", width=100, height=38, fg_color=BLUE, hover_color=BLUE_HOVER, state="disabled", command=self.switch_agent)
        self.switch_button.pack(side="right")
        ctk.CTkButton(seats, text="刷新席位列表", width=120, height=28, fg_color="transparent", hover_color=PANEL, text_color=MUTED, command=self.refresh_agents).pack(anchor="w", padx=16, pady=(0, 10))
        card = ctk.CTkFrame(self.dashboard, fg_color=PANEL_2, corner_radius=10)
        card.pack(fill="x", padx=24, pady=(0, 18))
        ctk.CTkLabel(card, text="运行状态", text_color=MUTED, font=ctk.CTkFont(size=11)).pack(anchor="w", padx=16, pady=(13, 2))
        ctk.CTkLabel(card, textvariable=self.status, text_color=TEXT, wraplength=590, justify="left").pack(anchor="w", padx=16, pady=(0, 14))
        controls = ctk.CTkFrame(self.dashboard, fg_color="transparent")
        controls.pack(fill="x", padx=24, pady=(0, 24))
        ctk.CTkButton(controls, text="查看当前提醒", width=140, height=38, fg_color=BLUE, hover_color=BLUE_HOVER, command=self.show_popup).pack(side="left")
        ctk.CTkButton(controls, text="退出插件登录", width=130, height=38, fg_color="transparent", border_width=1, border_color="#5275a5", hover_color=PANEL_2, command=self.rebind).pack(side="left", padx=10)
        ctk.CTkButton(controls, text="测试提醒", width=100, height=38, fg_color=PANEL_2, hover_color="#334b70", command=self.test_reminder).pack(side="left")

    def _build_popup(self) -> None:
        self.popup = ctk.CTkToplevel(self.root)
        self.popup.title("ICS · 转人工提醒")
        self.popup.geometry("500x360")
        self.popup.minsize(400, 300)
        self.popup.resizable(True, True)
        self.popup.configure(fg_color=BG)
        self.popup.attributes("-topmost", True)
        self.popup.protocol("WM_DELETE_WINDOW", self.popup.withdraw)
        self.popup.withdraw()
        banner = ctk.CTkFrame(self.popup, fg_color="#7f1d1d", corner_radius=0, height=66)
        banner.pack(fill="x")
        banner.pack_propagate(False)
        ctk.CTkLabel(banner, text="●  有新的客户请求人工", font=ctk.CTkFont(size=18, weight="bold"), text_color="white").pack(anchor="w", padx=16, pady=(8, 0))
        self.count = tk.StringVar()
        ctk.CTkLabel(banner, textvariable=self.count, text_color="#fecaca", font=ctk.CTkFont(size=11)).pack(anchor="w", padx=18)
        panel = ctk.CTkFrame(self.popup, fg_color=BG, corner_radius=0)
        panel.pack(fill="both", expand=True, padx=14, pady=10)
        actions = ctk.CTkFrame(panel, fg_color="transparent")
        actions.pack(side="bottom", fill="x", pady=(8, 0))
        ctk.CTkLabel(actions, textvariable=self.browser_status, text_color=MUTED, font=ctk.CTkFont(size=10), wraplength=360, justify="left").pack(fill="x", pady=(0, 4))
        buttons = ctk.CTkFrame(actions, fg_color="transparent")
        buttons.pack(fill="x")
        self.open_button = ctk.CTkButton(buttons, text="打开 Chatwoot 会话", width=170, height=32, fg_color=BLUE, hover_color=BLUE_HOVER, command=self.open_conversation)
        self.open_button.pack(side="left")
        ctk.CTkButton(buttons, text="确认收到", width=110, height=32, fg_color=GREEN, hover_color="#16a34a", command=self.confirm).pack(side="right")
        self.listbox = tk.Listbox(panel, height=3, exportselection=False, bg="#17233b", fg=TEXT, selectbackground=BLUE, selectforeground="white", relief="flat", highlightthickness=1, highlightbackground="#334b70", font=("Microsoft YaHei UI", 10), activestyle="none")
        self.listbox.pack(fill="x", pady=(0, 6))
        self.listbox.bind("<<ListboxSelect>>", self.show_detail)
        self.detail = ctk.CTkTextbox(panel, height=60, fg_color=PANEL, text_color="#dbeafe", font=ctk.CTkFont(size=12), corner_radius=9, wrap="word", state="disabled")
        self.detail.pack(fill="both", expand=True, pady=3)

    def _start_tray(self) -> None:
        menu = pystray.Menu(
            pystray.MenuItem("打开 ICS 客服提醒", lambda: self.ui_actions.put("show"), default=True),
            pystray.MenuItem("查看当前提醒", lambda: self.ui_actions.put("reminders")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出程序", lambda: self.ui_actions.put("quit")),
        )

        def setup(icon):
            icon.visible = True
            self.tray_ready.set()

        def run():
            try:
                self.tray_icon.run(setup=setup)
            except Exception:
                self.ui_actions.put("tray_error")
            finally:
                self.tray_ready.clear()

        self.tray_icon = pystray.Icon("ics-reminder", reminder_icon(size=64), "ICS 客服提醒", menu)
        self.tray_thread = threading.Thread(target=run, name="ics-reminder-tray", daemon=True)
        self.tray_thread.start()

    def _update_tray(self) -> None:
        signature = (len(self.state.visible()), self.agent_name.get())
        if signature == self._tray_signature or not self.tray_ready.is_set():
            return
        self._tray_signature = signature
        self.tray_icon.title = f"ICS 客服提醒 · {signature[1]} · 待确认 {signature[0]} 条"[:127]
        self.tray_icon.icon = reminder_icon(pending=bool(signature[0]), size=64)

    def _on_minimize(self, event) -> None:
        if event.widget is self.root and not self.closing and self.root.state() == "iconic":
            if self.tray_ready.is_set():
                self.root.after(0, self.hide_window)

    def _prepare_demo(self) -> None:
        self.setup.pack_forget()
        self.dashboard.pack(fill="both", expand=True)
        self.agent_name.set("本机弹窗演示")
        self.status.set("演示模式不会连接 ICS。点击下方按钮生成一条置顶提醒。")
        self.status_dot.configure(text_color=ORANGE)
        ctk.CTkButton(self.dashboard, text="产生新试验提醒", height=38, fg_color=ORANGE, hover_color="#d97706", command=self.demo_item).pack(anchor="w", padx=24, pady=(0, 24))
        self.root.after(800, self.demo_item)

    def submit(self, kind: str, operation) -> None:
        if self.busy:
            return
        self.busy = True
        future = self.executor.submit(operation)

        def done(completed):
            try:
                self.results.put((kind, completed.result(), None))
            except Exception as exc:
                self.results.put((kind, None, exc))

        future.add_done_callback(done)

    def load_agents(self) -> None:
        if self.busy:
            return
        try:
            server = validate_base_url(self.server.get())
        except ValueError as exc:
            self.status.set(str(exc))
            return
        username, password = self.username.get(), self.password.get()
        if not username.strip() or not password:
            self.status.set("请填写账号和密码。")
            return
        self.plugin_token = ""
        self.bind_button.configure(state="disabled")
        self.status.set("正在登录并读取 Chatwoot 坐席…")
        self.login_button.configure(state="disabled")

        def operation():
            token = request(server, "/reminder/plugin/login", payload={"username": username, "password": password})["access_token"]
            agents = request(server, "/reminder/plugin/agents", token=token)["agents"]
            return {"token": token, "agents": agents, "server": server}

        self.submit("agents", operation)

    def bind(self) -> None:
        if self.busy:
            return
        selected_label = self.agents_box.get()
        try:
            selected = self.agent_display_values.index(selected_label)
        except ValueError:
            selected = -1
        if selected < 0 or not self.plugin_token:
            return
        if self.server.get().rstrip("/") != self.login_server:
            self.status.set("ICS 地址已变化，请重新登录并同步坐席。")
            return
        agent_id = self.agent_values[selected]["id"]
        server, token = self.login_server, self.plugin_token
        try:
            client_server = validate_base_url(self.client_server.get() or server)
        except ValueError as exc:
            self.status.set(str(exc))
            return
        self.status.set("正在绑定设备…")

        def operation():
            result = request(server, "/reminder/plugin/devices", token=token, payload={"agent_id": agent_id})
            result["server"] = client_server
            try:
                request(client_server, "/reminder/device/test", token=result["token"], payload={})
            except ApiError:
                result["test_failed"] = True
            return result

        self.submit("bind", operation)

    def connected(self, config: dict) -> None:
        self.server.set(validate_base_url(config["server"]))
        self.token = config["token"]
        self.device_id = config.get("device_id", "")
        self.plugin_token = ""
        self.remember.set(config.get("remember", self.remember.get()))
        self._activate_agent(config)
        self.password.set("")
        self.agent_name.set("当前坐席：" + config["agent_name"])
        self.setup.pack_forget()
        self.dashboard.pack(fill="both", expand=True)
        self.status.set("已绑定，正在连接 ICS。关闭窗口会缩小到系统托盘，提醒仍会继续接收。")
        self.status_dot.configure(text_color=GREEN)
        self.next_poll = 0
        self.next_agents_refresh = 0

    def _activate_agent(self, config: dict) -> None:
        agent_id = str(config.get("agent_id", self.current_agent_id))
        version = config.get("binding_version", self.binding_version)
        changed = agent_id != self.current_agent_id or version != self.binding_version
        if changed:
            self.switching = bool(self.token)
            self.states[self.current_agent_id] = self.state
            self.current_agent_id = agent_id
            self.binding_version = version
            self.state = self.states.setdefault(agent_id, ReminderState())
            self.popup.withdraw()
            self.render()
            self.next_ring = 0
        self.switchable = config.get("switchable", self.switchable)
        self.active_agent_name = config.get("agent_name", self.active_agent_name)
        self.agent_name.set("当前坐席：" + self.active_agent_name)
        self.switch_button.configure(state="normal" if self.switchable and self.agent_values else "disabled")
        if not self.switchable:
            self.switch_box.set("旧版固定席位；重新登录插件后可切换")
        if changed and self.token and self.switchable:
            self._save_device()

    def _save_device(self) -> None:
        if self.remember.get():
            try:
                save_config({"server": self.server.get(), "token": self.token,
                             "device_id": self.device_id, "agent_id": self.current_agent_id,
                             "agent_name": self.active_agent_name, "binding_version": self.binding_version,
                             "switchable": self.switchable, "remember": True})
            except Exception:
                self.status.set("当前登录有效，但本机凭据未能保存，下次需重新登录。")
        else:
            clear_config()

    def refresh_agents(self) -> None:
        if self.demo or not self.switchable or not self.token or self.busy:
            return
        server, token = self.server.get(), self.token
        self.next_agents_refresh = time.monotonic() + 60
        self.submit("refresh_agents", lambda: request(server, "/reminder/device/agents", token=token))

    def switch_agent(self) -> None:
        if self.demo or not self.switchable:
            return
        try:
            index = self.agent_display_values.index(self.switch_box.get())
        except ValueError:
            self.status.set("请先选择需要切换的席位。")
            return
        target = self.agent_values[index]
        if target["id"] == self.current_agent_id:
            self.status.set("已经在使用所选席位。")
            return
        self.requested_agent = dict(target)
        self.status.set("正在切换到：" + target["name"])
        if not self.busy:
            self._perform_switch()

    def _perform_switch(self) -> None:
        target, self.requested_agent = self.requested_agent, None
        self.switching = True
        self.popup.withdraw()
        self.switch_button.configure(state="disabled")
        server, token, version = self.server.get(), self.token, self.binding_version
        self.submit("switch", lambda: request(server, "/reminder/device/switch", token=token,
                                              payload={"agent_id": target["id"], "binding_version": version}))

    def test_reminder(self) -> None:
        if self.demo:
            self.demo_item()
        elif self.token and self.switchable and not self.busy:
            server, token = self.server.get(), self.token
            self.submit("test", lambda: request(server, "/reminder/device/test", token=token, payload={}))

    def _set_agent_choices(self, agents: list[dict], *, initial: bool = False) -> None:
        self.agent_values = agents
        self.agent_display_values = [f'{x["name"]} · ID {x["id"]} · {x["availability_status"]}' for x in agents]
        self.switch_box.configure(values=self.agent_display_values or ["没有授权席位"])
        active = next((value for item, value in zip(agents, self.agent_display_values)
                       if item["id"] == self.current_agent_id), None)
        self.switch_box.set(active or (self.agent_display_values[0] if agents else "没有授权席位"))
        self.switch_button.configure(state="normal" if self.switchable and agents else "disabled")
        if initial:
            self.agents_box.configure(values=self.agent_display_values or ["没有授权席位"])
            self.agents_box.set(self.agent_display_values[0] if agents else "没有授权席位")
            self.bind_button.configure(state="normal" if agents else "disabled")

    def poll(self) -> None:
        server, token, binding_version = self.server.get(), self.token, self.binding_version
        confirmations = list(self.state.confirmations)

        def operation():
            for identity, version in confirmations:
                if self.stop_network.is_set():
                    return {}
                try:
                    request(server, f"/reminder/notifications/{identity}/ack", token=token,
                            payload={"version": version, "binding_version": binding_version})
                except ApiError as exc:
                    if exc.status != 409:
                        raise
            return {} if self.stop_network.is_set() else request(server, "/reminder/notifications", token=token)

        self.submit("poll", operation)

    def tick(self) -> None:
        if self.closing:
            return
        try:
            while True:
                action = self.ui_actions.get_nowait()
                if action == "show":
                    self.show_window()
                elif action == "reminders":
                    self.show_window()
                    self.show_popup()
                elif action == "quit":
                    self.quit()
                    return
                elif action == "tray_error":
                    self.show_window()
                    self.status.set("系统托盘不可用，最小化后会保留在任务栏。")
        except queue.Empty:
            pass
        try:
            while True:
                kind, result, error = self.results.get_nowait()
                if kind == "browser":
                    self.browser_busy = False
                    if error:
                        self.browser_status.set(str(error) if isinstance(error, BrowserOpenError) else "打开浏览器失败，请重试。")
                    else:
                        self.browser_status.set("已在原 Chatwoot 标签页打开会话。" if result else "未找到已登录工作台，已用默认浏览器打开。")
                        self.popup.withdraw()
                        self.next_ring = time.monotonic() + self.repeat_seconds
                    self.show_detail()
                    continue
                self.busy = False
                if error:
                    self.login_button.configure(state="normal")
                    self.status.set(str(error) if isinstance(error, (ApiError, ValueError)) else "操作失败，请检查 ICS 服务或重新登录。")
                    self.status_dot.configure(text_color=RED)
                    if kind == "logout":
                        self._reset_login()
                        self.status.set("已退出本机登录；网络不通，服务器设备未能吊销。")
                    if kind in {"poll", "refresh_agents", "switch", "test"} and isinstance(error, ApiError) and error.status == 401:
                        self._reset_login()
                        self.status.set(str(error))
                        self.show_window()
                    if kind == "bind" and isinstance(error, ApiError) and error.status == 401:
                        self.plugin_token = ""
                        self.bind_button.configure(state="disabled")
                    self.next_poll = 0 if kind == "switch" else time.monotonic() + 5
                elif kind == "agents":
                    self.plugin_token, self.login_server = result["token"], result["server"]
                    self._set_agent_choices(result["agents"], initial=True)
                    self.login_button.configure(state="normal")
                    self.password.set("")
                    self.status.set("请选择当前客服席位；登录后可随时切换。")
                    self.status_dot.configure(text_color=BLUE)
                elif kind == "bind":
                    self.connected(result)
                    self._save_device()
                    if result.get("test_failed"):
                        self.status.set("绑定成功，试验提醒发送失败；可继续测试真实转人工。")
                elif kind == "refresh_agents":
                    self._activate_agent({**result, "switchable": True})
                    self._set_agent_choices(result["agents"])
                elif kind == "switch":
                    self._activate_agent(result)
                    self._set_agent_choices(self.agent_values)
                    self.status.set("席位已切换，正在读取新席位的提醒。")
                    self.next_poll = 0
                elif kind == "test":
                    self.status.set("试验提醒已发送，正在等待接收。")
                    self.next_poll = 0
                elif kind == "logout":
                    self._reset_login()
                elif kind == "poll":
                    previous_agent, previous_binding = self.current_agent_id, self.binding_version
                    self._activate_agent(result)
                    if (self.current_agent_id, self.binding_version) != (previous_agent, previous_binding):
                        self._set_agent_choices(self.agent_values)
                    new = self.state.replace(result["notifications"])
                    self.switching = False
                    self.switch_button.configure(state="normal" if self.switchable and self.agent_values else "disabled")
                    self.poll_seconds = max(1, result.get("poll_seconds", 2))
                    self.repeat_seconds = max(5, result.get("repeat_seconds", 30))
                    self.render()
                    self.status.set(f"ICS 已连接 · 待确认 {len(self.state.visible())} 条 · 确认待同步 {len(self.state.confirmations)} 条")
                    self.status_dot.configure(text_color=GREEN)
                    self.next_poll = time.monotonic() + self.poll_seconds
                    if new and self.state.visible() and not self.requested_agent:
                        self.ring()
        except queue.Empty:
            pass
        now = time.monotonic()
        if self.token and not self.busy:
            if self.requested_agent:
                self._perform_switch()
            elif now >= self.next_poll:
                self.poll()
            elif self.switchable and now >= self.next_agents_refresh and not self.switching:
                self.refresh_agents()
        if self.state.visible() and not self.switching and now >= self.next_ring:
            self.ring()
        self._update_tray()
        self._tick_job = self.root.after(200, self.tick)

    def render(self) -> None:
        previous = self.selected()
        self.visible = self.state.visible()
        self.count.set(f"当前有 {len(self.visible)} 条提醒等待确认")
        self.listbox.delete(0, tk.END)
        for item in self.visible:
            title = "试验提醒" if item["is_test"] else "会话 " + item["conversation_id"]
            self.listbox.insert(tk.END, title + "  ·  " + (item["customer_name"] or "客户"))
        if self.visible:
            selected = next((index for index, item in enumerate(self.visible) if previous and item["id"] == previous["id"]), 0)
            self.listbox.selection_set(selected)
            self.show_detail()
        else:
            self._set_detail("")
            self.popup.withdraw()

    def selected(self) -> dict | None:
        indexes = self.listbox.curselection()
        return self.visible[indexes[0]] if indexes else None

    def show_detail(self, _event=None) -> None:
        item = self.selected()
        if not item:
            return
        created = datetime.fromisoformat(item["created_at"])
        minutes = max(0, int((datetime.now(timezone.utc) - created).total_seconds() / 60))
        self._set_detail(f'等待 {minutes} 分钟\n\n{item["message"] or "客户请求人工协助，请进入会话查看。"}')
        self.open_button.configure(state="normal" if item.get("conversation_url") and not self.browser_busy else "disabled")

    def _set_detail(self, text: str) -> None:
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text)
        self.detail.configure(state="disabled")

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()

    def hide_window(self) -> None:
        if self.closing:
            return
        if self.tray_ready.is_set():
            self.root.withdraw()
        else:
            self.root.iconify()

    def show_popup(self) -> None:
        if self.switching and not self.demo:
            self.status.set("正在同步当前席位，请稍候。")
            return
        if not self.state.visible():
            self.status.set("当前没有待确认提醒。")
            return
        self.render()
        self.popup.deiconify()
        self.popup.lift()

    def ring(self) -> None:
        self.show_popup()
        self.next_ring = time.monotonic() + self.repeat_seconds
        try:
            import winsound
            winsound.PlaySound("SystemExclamation", winsound.SND_ALIAS | winsound.SND_ASYNC)
        except (ImportError, RuntimeError):
            self.root.bell()

    def confirm(self) -> None:
        if self.switching or self.requested_agent:
            self.status.set("请等待席位切换完成后再确认提醒。")
            return
        item = self.selected()
        if item:
            self.state.confirm(item)
            self.render()
            self.next_poll = 0
            self.status.set("确认已记录，等待同步 ICS。" if not self.demo else "演示提醒已确认。")

    def open_conversation(self) -> None:
        if self.browser_busy:
            return
        item = self.selected()
        if item and item.get("conversation_url"):
            try:
                url = validate_base_url(item["conversation_url"])
            except ValueError:
                self.browser_status.set("会话地址无效，请检查 ICS 的 Chatwoot 公网地址配置。")
                return
            self.browser_busy = True
            self.browser_status.set("正在查找已登录的 Chatwoot 标签页…")
            self.open_button.configure(state="disabled")
            future = self.browser_executor.submit(open_conversation_url, url)

            def done(completed):
                try:
                    self.results.put(("browser", completed.result(), None))
                except Exception as exc:
                    self.results.put(("browser", None, exc))

            future.add_done_callback(done)

    def demo_item(self) -> None:
        value = {"id": uuid4().hex, "version": 1, "is_test": True, "conversation_id": "demo", "customer_name": "试验客户", "message": "本机演示：请切到其他软件，检查置顶窗口和提示音。", "created_at": datetime.now(timezone.utc).isoformat(), "conversation_url": ""}
        self.state.replace(list(self.state.items.values()) + [value])
        self.ring()

    def rebind(self) -> None:
        if self.demo:
            self.status.set("本机演示不会修改坐席绑定，请正常启动程序进行绑定。")
            return
        if self.busy:
            self.status.set("请等待当前请求结束后退出登录。")
            return
        if self.token:
            server, token = self.server.get(), self.token
            self.status.set("正在退出插件登录…")
            self.submit("logout", lambda: request(server, "/reminder/device/logout", token=token, payload={}))
        else:
            self._reset_login()

    def _reset_login(self) -> None:
        self.token = self.plugin_token = ""
        self.device_id = self.current_agent_id = ""
        self.binding_version = 1
        self.active_agent_name = "尚未选择席位"
        self.switchable = self.switching = False
        self.requested_agent = None
        clear_config()
        self.state = ReminderState()
        self.states = {}
        self.render()
        self.dashboard.pack_forget()
        self.setup.pack(fill="both", expand=True)
        self.agent_name.set("尚未绑定客服坐席")
        self.status_dot.configure(text_color=MUTED)
        self.bind_button.configure(state="disabled")
        self.agents_box.set("请先登录 ICS")
        self.agent_values = []
        self.agent_display_values = []
        self.switch_button.configure(state="disabled")
        self.status.set("请登录提醒插件并选择客服席位。")

    def quit(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.stop_network.set()
        if self._tick_job:
            self.root.after_cancel(self._tick_job)
        if self.tray_icon is not None:
            self.tray_icon.stop()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.browser_executor.shutdown(wait=False, cancel_futures=True)
        self.popup.destroy()
        self.root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description="ICS Windows 客服桌面提醒客户端")
    parser.add_argument("--demo", action="store_true", help="只演示本机弹窗，不读取凭据、不连接 ICS")
    parser.add_argument("--background", action="store_true", help="已有绑定时直接在托盘后台运行")
    args = parser.parse_args()
    instance = SingleInstance(demo=args.demo)
    if not instance.acquired:
        return
    try:
        root = ctk.CTk()
        client = ReminderClient(root, demo=args.demo)
        instance.watch(lambda: client.ui_actions.put("show"))
        if args.background and client.token:
            root.after(700, client.hide_window)
        root.mainloop()
    finally:
        instance.close()


if __name__ == "__main__":
    main()
