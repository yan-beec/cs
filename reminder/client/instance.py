"""同一 Windows 会话单实例；重复启动时唤起已有客户端。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import threading


class SingleInstance:
    def __init__(self, *, demo: bool = False, name: str = "ICSReminder") -> None:
        self.mutex = self.event = None
        self.acquired = True
        self.closed = threading.Event()
        self.thread: threading.Thread | None = None
        if os.name != "nt":
            return
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateEventW.restype = wintypes.HANDLE
        self.kernel.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.OpenEventW.restype = wintypes.HANDLE
        self.kernel.SetEvent.argtypes = [wintypes.HANDLE]
        self.kernel.SetEvent.restype = wintypes.BOOL
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        prefix = "Local\\" + name + ("-Demo" if demo else "")
        # 先创建唤醒事件，避免第二个实例在首个实例初始化期间找不到它。
        self.event = self.kernel.CreateEventW(None, False, False, prefix + "-Wake")
        if not self.event:
            raise ctypes.WinError(ctypes.get_last_error())
        self.mutex = self.kernel.CreateMutexW(None, False, prefix + "-Mutex")
        if not self.mutex:
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())
        self.acquired = ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS
        if not self.acquired:
            self.kernel.SetEvent(self.event)
            self.close()

    def watch(self, callback) -> None:
        if not self.event or not self.acquired:
            return

        def run():
            while not self.closed.is_set():
                if self.kernel.WaitForSingleObject(self.event, 500) == 0 and not self.closed.is_set():
                    callback()

        self.thread = threading.Thread(target=run, name="ics-reminder-instance", daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.closed.set()
        if self.event:
            self.kernel.SetEvent(self.event)
        if self.thread:
            self.thread.join(timeout=1)
        for handle in (self.event, self.mutex):
            if handle:
                self.kernel.CloseHandle(handle)
        self.event = self.mutex = None
