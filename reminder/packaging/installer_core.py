"""当前 Windows 用户安装：文件、Native Messaging 注册、卸载登记。"""

from __future__ import annotations

import html
import json
import os
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4
import zipfile

from reminder.client.bridge import EXTENSION_ORIGIN, HOST_NAME

VERSION = "1.0.3"
PRODUCT = "ICSReminder"
HOST_REGISTRY = [
    rf"Software\Google\Chrome\NativeMessagingHosts\{HOST_NAME}",
    rf"Software\Microsoft\Edge\NativeMessagingHosts\{HOST_NAME}",
]
UNINSTALL_REGISTRY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{PRODUCT}"


def default_install_dir() -> Path:
    return Path(os.environ["LOCALAPPDATA"]) / "Programs" / PRODUCT


def registered_install_dir() -> Path | None:
    """只使用仍存在且属于本程序的当前用户安装登记。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_REGISTRY) as key:
            value = winreg.QueryValueEx(key, "InstallLocation")[0]
        root = Path(value)
        if not root.is_absolute():
            return None
        root = root.resolve()
        marker = json.loads((root / "install.json").read_text(encoding="utf-8"))
        if isinstance(marker, dict) and marker.get("product") == PRODUCT:
            return root
    except (ImportError, OSError, ValueError, TypeError):
        pass
    return None


def preferred_install_dir() -> Path:
    return registered_install_dir() or default_install_dir()


def validate_install_dir(value: str | Path) -> Path:
    """在写入前核验用户选择的目录，保护其他文件和设备配置。"""
    text = os.path.expandvars(str(value).strip())
    if not text:
        raise ValueError("请选择安装目录。")
    root = Path(text).expanduser()
    if not root.is_absolute():
        raise ValueError("请填写完整的安装目录，例如 D:\\ICSReminder。")
    root = root.resolve()
    if root == Path(root.anchor):
        raise ValueError("不能直接安装在磁盘根目录，请选择独立的程序文件夹。")
    if os.environ.get("APPDATA"):
        device_dir = (Path(os.environ["APPDATA"]) / PRODUCT).resolve()
        if root.is_relative_to(device_dir) or device_dir.is_relative_to(root):
            raise ValueError("安装目录不能与设备配置目录重叠，请选择其他目录。")
    if root.exists():
        if not root.is_dir():
            raise ValueError("所选位置不是文件夹，请选择其他目录。")
        marker = root / "install.json"
        if marker.is_file():
            try:
                metadata = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise ValueError("安装目录标记无效，请选择其他目录。") from None
            if not isinstance(metadata, dict) or metadata.get("product") != PRODUCT:
                raise ValueError("安装目录不是本程序目录，请选择其他目录。")
        elif any(root.iterdir()):
            raise ValueError("所选目录已有其他文件，请选择空文件夹或新的程序目录。")
    previous = registered_install_dir()
    if previous is not None and root != previous:
        raise ValueError(f"本机已安装在 {previous}。如需更换目录，请先卸载旧版再重新安装。")
    return root


def shortcut_path() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/ICS 客服提醒.lnk"


def write_registry(root: Path) -> None:
    import winreg
    for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
        for name in HOST_REGISTRY:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, name, 0, winreg.KEY_WRITE | view) as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(root / "native-host.json"))
    values = {
        "DisplayName": "ICS 客服提醒", "DisplayVersion": VERSION, "Publisher": "ICS",
        "InstallLocation": str(root), "DisplayIcon": str(root / "ICSReminder.exe"),
        "UninstallString": f'"{root / "Uninstall.exe"}" --uninstall',
    }
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, UNINSTALL_REGISTRY, 0, winreg.KEY_WRITE) as key:
        for name, value in values.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        for name in ("NoModify", "NoRepair"):
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, 1)


def remove_registry(root: Path) -> None:
    import winreg
    # 只移除仍指向本安装目录的登记，不影响其他位置的新版本。
    for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
        for name in HOST_REGISTRY:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, name, 0, winreg.KEY_READ | view) as key:
                    value = winreg.QueryValueEx(key, "")[0]
                if Path(value).resolve() == (root / "native-host.json").resolve():
                    winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER, name, view)
            except FileNotFoundError:
                pass
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_REGISTRY) as key:
            value = winreg.QueryValueEx(key, "InstallLocation")[0]
        if Path(value).resolve() == root.resolve():
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_REGISTRY)
    except FileNotFoundError:
        pass


def create_shortcut(root: Path) -> None:
    # 路径通过标准输入传递，避免将安装目录拼进脚本。
    command = r"""
        $ErrorActionPreference = 'Stop'
        [Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
        $config = [Console]::In.ReadToEnd() | ConvertFrom-Json
        $shell = New-Object -ComObject WScript.Shell
        $link = $shell.CreateShortcut($config.shortcut)
        $link.TargetPath = $config.executable
        $link.WorkingDirectory = $config.directory
        $link.IconLocation = $config.executable
        $link.Save()
    """
    subprocess.run(
        [str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
         "-NoProfile", "-NonInteractive", "-Command", command],
        input=json.dumps({"shortcut": str(shortcut_path()), "executable": str(root / "ICSReminder.exe"), "directory": str(root)}),
        text=True, encoding="utf-8", capture_output=True, check=True, timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def extract_payload(payload: Path, destination: Path) -> None:
    with zipfile.ZipFile(payload) as archive:
        for info in archive.infolist():
            target = (destination / info.filename).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise ValueError("安装包文件路径无效。")
        archive.extractall(destination)
    for name in ("ICSReminder.exe", "ICSBrowserHost.exe", "extension/manifest.json", "安装说明.html"):
        if not (destination / name).is_file():
            raise ValueError("安装包不完整，请重新生成安装包。")


def install(payload: Path, root: Path, setup_exe: Path) -> None:
    root = validate_install_dir(root)
    previous_install = (root / "install.json").is_file()
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = root.with_name(f"{root.name}-install-{uuid4().hex}")
    backup = root.with_name(f"{root.name}-previous-{uuid4().hex}")
    staging.mkdir()
    previous = False
    try:
        extract_payload(payload, staging)
        host = {"name": HOST_NAME, "description": "ICS Reminder browser connection", "path": str(root / "ICSBrowserHost.exe"),
                "type": "stdio", "allowed_origins": [EXTENSION_ORIGIN]}
        (staging / "native-host.json").write_text(json.dumps(host, ensure_ascii=False, indent=2), encoding="utf-8")
        (staging / "install.json").write_text(json.dumps({"product": PRODUCT, "version": VERSION}), encoding="utf-8")
        guide = staging / "安装说明.html"
        guide.write_text(guide.read_text(encoding="utf-8").replace("{{EXTENSION_DIR}}", html.escape(str(root / "extension"))), encoding="utf-8")
        shutil.copy2(setup_exe, staging / "Uninstall.exe")
        if root.exists():
            root.rename(backup)
            previous = True
        staging.rename(root)
        try:
            write_registry(root)
            create_shortcut(root)
        except Exception:
            # 文件/注册回滚；已有配置与设备凭据不被覆盖。
            remove_registry(root)
            shutil.rmtree(root)
            if previous:
                backup.rename(root)
                if previous_install:
                    write_registry(root)
                    create_shortcut(root)
                previous = False
            raise
        if previous:
            # 新版已安装；占用中的旧文件保留在备份目录，避免损坏已完成安装。
            shutil.rmtree(backup, ignore_errors=True)
            previous = False
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if previous and backup.exists() and not root.exists():
            backup.rename(root)


def uninstall(root: Path) -> bool:
    """由安装目录之外的临时卸载程序调用，设备配置保留。"""
    root = root.resolve()
    marker = json.loads((root / "install.json").read_text(encoding="utf-8"))
    if marker.get("product") != PRODUCT or root == Path(root.anchor) or root.parent == root:
        raise ValueError("不是 ICS 客户端安装目录。")
    for path in sorted(root.rglob("*"), key=lambda value: len(value.parts), reverse=True):
        if path == root / "install.json":
            continue
        path.rmdir() if path.is_dir() else path.unlink()
    remove_registry(root)
    shortcut_path().unlink(missing_ok=True)
    (root / "install.json").unlink()
    root.rmdir()
    return False


def browser_executable(browser: str) -> Path | None:
    parts = "Google/Chrome/Application/chrome.exe" if browser == "chrome" else "Microsoft/Edge/Application/msedge.exe"
    for base in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
        if os.environ.get(base):
            candidate = Path(os.environ[base]) / parts
            if candidate.is_file():
                return candidate
    return None
