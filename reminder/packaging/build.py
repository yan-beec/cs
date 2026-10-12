"""在当前 Python 环境构建 Windows 单文件客户端。

运行：python reminder/packaging/build.py
所有打包缓存和输出都保留在 reminder 下，不把项目 .env 打入客户端。
"""

from __future__ import annotations

import os
import argparse
from pathlib import Path
import subprocess
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[2]
FEATURE = ROOT / "reminder"


def main() -> None:
    parser = argparse.ArgumentParser(description="构建 ICS Windows 提醒客户端")
    parser.add_argument("--output-dir", type=Path, default=FEATURE / "dist")
    parser.add_argument("--installer", action="store_true", help="同时构建浏览器连接组件、插件和统一安装包")
    args = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("Windows 客户端需要在 Windows 上打包。")
    sys.path.insert(0, str(ROOT))
    from reminder.client.icons import reminder_icon

    work = FEATURE / "build"
    work.mkdir(parents=True, exist_ok=True)
    icon = work / "ICSReminder.ico"
    reminder_icon().save(icon, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    environment = os.environ.copy()
    environment["PYINSTALLER_CONFIG_DIR"] = str(work / "cache")
    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", "ICSReminder", "--icon", str(icon),
        "--paths", str(ROOT), "--collect-all", "customtkinter",
        "--hidden-import", "pystray._win32",
        "--exclude-module", "IPython", "--exclude-module", "matplotlib",
        "--exclude-module", "numpy", "--exclude-module", "pytest",
        "--distpath", str(args.output_dir.resolve()),
        "--workpath", str(work / "pyinstaller"),
        "--specpath", str(work),
        str(FEATURE / "packaging" / "entrypoint.py"),
    ]
    subprocess.run(command, cwd=ROOT, env=environment, check=True)
    print(f"Windows 客户端已生成：{args.output_dir.resolve() / 'ICSReminder.exe'}")
    if not args.installer:
        return
    output = args.output_dir.resolve()
    host_command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--console",
        "--name", "ICSBrowserHost", "--paths", str(ROOT),
        "--distpath", str(output), "--workpath", str(work / "native"), "--specpath", str(work),
        str(FEATURE / "packaging/native_entrypoint.py"),
    ]
    subprocess.run(host_command, cwd=ROOT, env=environment, check=True)
    extension = FEATURE / "extension"
    (extension / "icons").mkdir(exist_ok=True)
    for size in (16, 48, 128):
        reminder_icon(size=size).save(extension / "icons" / f"{size}.png")
    extension_files = [path for path in extension.rglob("*") if path.is_file() and "tests" not in path.parts]
    with zipfile.ZipFile(output / "ICSChatwootExtension.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for path in extension_files:
            archive.write(path, path.relative_to(extension))
    payload = work / "payload.zip"
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in ("ICSReminder.exe", "ICSBrowserHost.exe"):
            archive.write(output / name, name)
        for path in extension_files:
            archive.write(path, Path("extension") / path.relative_to(extension))
        archive.write(FEATURE / "packaging/install-guide.html", "安装说明.html")
    setup_command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", "ICSReminderSetup", "--icon", str(icon), "--paths", str(ROOT),
        "--collect-all", "customtkinter", "--add-data", str(payload) + ";reminder/packaging",
        "--exclude-module", "IPython", "--exclude-module", "matplotlib", "--exclude-module", "numpy", "--exclude-module", "pytest",
        "--distpath", str(output), "--workpath", str(work / "installer"), "--specpath", str(work),
        str(FEATURE / "packaging/setup_entrypoint.py"),
    ]
    subprocess.run(setup_command, cwd=ROOT, env=environment, check=True)
    print(f"统一安装包已生成：{output / 'ICSReminderSetup.exe'}")


if __name__ == "__main__":
    main()
