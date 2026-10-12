"""无需管理员权限的单文件安装向导。"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from tkinter import filedialog
import webbrowser
from uuid import uuid4

import customtkinter as ctk

from reminder.packaging.installer_core import browser_executable, install, preferred_install_dir, uninstall, validate_install_dir


def main() -> None:
    if "--uninstall" in sys.argv and "--target" not in sys.argv:
        # 从临时目录运行卸载向导，便于无管理员权限删除安装目录内的程序。
        helper = Path(tempfile.gettempdir()) / f"ICSReminder-Uninstall-{uuid4().hex}.exe"
        shutil.copy2(sys.executable, helper)
        subprocess.Popen([str(helper), "--uninstall", "--target", str(Path(sys.executable).parent)])
        return
    ctk.set_appearance_mode("dark")
    root = ctk.CTk()
    root.title("ICS 客服提醒安装")
    root.geometry("700x580")
    root.minsize(660, 550)
    target = Path(sys.argv[sys.argv.index("--target") + 1]).resolve() if "--uninstall" in sys.argv else preferred_install_dir()
    outer = ctk.CTkFrame(root, fg_color="transparent")
    outer.pack(fill="both", expand=True, padx=28, pady=24)
    removing = "--uninstall" in sys.argv
    ctk.CTkLabel(outer, text="卸载 ICS 客服提醒" if removing else "安装 ICS 客服提醒", font=ctk.CTkFont(size=24, weight="bold")).pack(anchor="w", pady=(0, 12))
    ctk.CTkLabel(outer, text="桌面强提醒 · Chrome / Edge 页面连接", text_color="#b7c4d7").pack(anchor="w")
    ctk.CTkLabel(outer, text="请先从托盘退出旧版提醒程序，并关闭正在连接插件的浏览器。\n设备登录与席位设置会保留。", justify="left", text_color="#b7c4d7").pack(anchor="w", pady=16)
    ctk.CTkLabel(outer, text="安装目录", text_color="#b7c4d7").pack(anchor="w")
    directory = ctk.CTkFrame(outer, fg_color="transparent")
    directory.pack(fill="x", pady=(6, 8))
    path = ctk.CTkEntry(directory, height=36)
    path.pack(side="left", fill="x", expand=True)
    path.insert(0, str(target))

    def choose_directory():
        current = Path(path.get().strip()).expanduser()
        initial = current if current.is_dir() else current.parent
        selected = filedialog.askdirectory(parent=root, title="选择安装目录", initialdir=str(initial), mustexist=False)
        if selected:
            path.delete(0, "end")
            path.insert(0, selected)

    browse = None
    if removing:
        path.configure(state="readonly")
    else:
        browse = ctk.CTkButton(directory, text="选择安装目录", width=130, height=36, command=choose_directory)
        browse.pack(side="left", padx=(10, 0))
        ctk.CTkLabel(outer, text="可输入完整路径或选择空文件夹，需有写入权限。升级沿用原目录；更换位置请先卸载。",
                     wraplength=620, justify="left", text_color="#b7c4d7").pack(anchor="w", pady=(0, 12))
    status = ctk.StringVar(value="卸载后浏览器插件需在扩展管理页移除。" if removing else "安装后，在客服使用的浏览器中完成一次插件加载和站点授权。")
    ctk.CTkLabel(outer, textvariable=status, wraplength=570, justify="left", text_color="#b7c4d7").pack(anchor="w", pady=(0, 18))
    finishing = []

    def operation():
        try:
            if removing:
                delayed = uninstall(target)
                finishing.append((True, "已卸载。部分占用文件将在重启后清除。" if delayed else "已卸载。"))
            else:
                payload = Path(sys._MEIPASS) / "reminder/packaging/payload.zip" if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1] / "build/payload.zip"
                install(payload, target, Path(sys.executable))
                finishing.append((True, "安装完成。请按说明加载浏览器插件，然后启动提醒程序。"))
        except Exception as exc:
            finishing.append((False, "无法写入所选目录。请选择有写入权限的位置，并确认旧客户端和浏览器已退出。" if isinstance(exc, PermissionError) else str(exc)))

    def start():
        nonlocal target
        if not removing:
            try:
                target = validate_install_dir(path.get())
            except (OSError, ValueError) as exc:
                status.set(str(exc))
                return
            path.delete(0, "end")
            path.insert(0, str(target))
            path.configure(state="disabled")
            browse.configure(state="disabled")
        action.configure(state="disabled")
        root.protocol("WM_DELETE_WINDOW", lambda: None)
        status.set("正在卸载…" if removing else "正在安装并配置浏览器连接…")
        threading.Thread(target=operation, daemon=True).start()
        root.after(100, finish)

    def finish():
        if not finishing:
            root.after(100, finish)
            return
        ok, message = finishing.pop()
        root.protocol("WM_DELETE_WINDOW", root.destroy)
        status.set(message)
        if not ok:
            action.configure(state="normal")
            if not removing:
                path.configure(state="normal")
                browse.configure(state="normal")
        else:
            action.configure(text="关闭", state="normal", command=root.destroy)
            if not removing:
                actions.pack(fill="x", pady=(18, 0))

    action = ctk.CTkButton(outer, text="确认卸载" if removing else "安装到本机", height=38, command=start)
    action.pack(anchor="w")
    actions = ctk.CTkFrame(outer, fg_color="transparent")
    ctk.CTkButton(actions, text="启动提醒程序", command=lambda: subprocess.Popen([str(target / "ICSReminder.exe")], cwd=target)).pack(side="left", padx=(0, 12))
    ctk.CTkButton(actions, text="查看插件安装说明", command=lambda: webbrowser.open((target / "安装说明.html").as_uri())).pack(side="left")
    browsers = ctk.CTkFrame(outer, fg_color="transparent")
    browsers.pack(fill="x", side="bottom", pady=(14, 0))
    for browser, label in (("chrome", "Chrome 扩展页面"), ("edge", "Edge 扩展页面")):
        executable = browser_executable(browser)
        if executable:
            ctk.CTkButton(browsers, text=label, fg_color="#334155", command=lambda browser=browser, executable=executable: subprocess.Popen([str(executable), f"{browser}://extensions"])).pack(side="left", padx=(0, 12))
    root.mainloop()


if __name__ == "__main__":
    main()
