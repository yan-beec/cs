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


ROOT = Path(__file__).resolve().parents[2]
FEATURE = ROOT / "reminder"


def main() -> None:
    parser = argparse.ArgumentParser(description="构建 ICS Windows 提醒客户端")
    parser.add_argument("--output-dir", type=Path, default=FEATURE / "dist")
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
        "--add-data", str(FEATURE / "client" / "browser_windows.ps1") + ";reminder/client",
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


if __name__ == "__main__":
    main()
