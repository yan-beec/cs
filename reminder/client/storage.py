"""用 Windows DPAPI 保存本机设备凭据，不保存管理员密码。"""

from __future__ import annotations

import base64
import ctypes
import json
import os
from pathlib import Path
from ctypes import wintypes


class DataBlob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]


def crypt(value: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("自动保存凭据仅支持 Windows；可取消记住设备后使用。")
    buffer = ctypes.create_string_buffer(value)
    source = DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    output = DataBlob()
    function = (ctypes.windll.crypt32.CryptUnprotectData if decrypt
                else ctypes.windll.crypt32.CryptProtectData)
    function.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                         wintypes.DWORD, ctypes.POINTER(DataBlob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(ctypes.cast(output.data, ctypes.c_void_p))


def config_path() -> Path:
    if not os.environ.get("APPDATA"):
        raise RuntimeError("找不到 Windows 用户配置目录。")
    return Path(os.environ["APPDATA"]) / "ICSReminder" / "device.json"


def save_config(value: dict) -> None:
    path = config_path()
    encoded = base64.b64encode(crypt(json.dumps(value).encode())).decode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(encoded, encoding="ascii")
    temporary.replace(path)


def load_config() -> dict | None:
    if os.name != "nt":
        return None
    path = config_path()
    if not path.exists():
        return None
    return json.loads(crypt(base64.b64decode(path.read_text(encoding="ascii")), decrypt=True))


def clear_config() -> None:
    if os.name == "nt":
        config_path().unlink(missing_ok=True)
