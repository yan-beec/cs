"""配置插件专用登录密码，不修改 system_users。

python -m reminder.backend.configure_login          # 交互式设置新密码
python -m reminder.backend.configure_login --generate  # 只为缺失项生成随机值
密码通过隐藏输入获取，不通过命令参数或日志输出。
"""

from __future__ import annotations

import argparse
from getpass import getpass
import secrets

from dotenv import dotenv_values, set_key

from ics_agent.app.core.config import PROJECT_ROOT


def main() -> None:
    parser = argparse.ArgumentParser(description="配置 ICS 提醒插件独立登录")
    parser.add_argument("--generate", action="store_true", help="只补齐缺失项，生成随机密码与密钥")
    args = parser.parse_args()
    path = PROJECT_ROOT / ".env"
    values = dotenv_values(path, encoding="utf-8")
    updates = {}
    if not values.get("REMINDER_PLUGIN_USERNAME"):
        updates["REMINDER_PLUGIN_USERNAME"] = "reminder"
    if args.generate:
        if not (values.get("REMINDER_PLUGIN_PASSWORD") or values.get("REMINDER_PLUGIN_PASSWORD_HASH")):
            updates["REMINDER_PLUGIN_PASSWORD"] = secrets.token_urlsafe(15)
    else:
        password = getpass("插件新密码：")
        repeat = getpass("再次输入：")
        if not password or password != repeat:
            raise SystemExit("密码为空或两次输入不同，未修改配置。")
        updates["REMINDER_PLUGIN_PASSWORD"] = password
        updates["REMINDER_PLUGIN_PASSWORD_HASH"] = ""
    if not values.get("REMINDER_PLUGIN_SECRET"):
        updates["REMINDER_PLUGIN_SECRET"] = secrets.token_urlsafe(48)
    for key in ("REMINDER_PLUGIN_ALLOWED_AGENT_IDS", "REMINDER_PLUGIN_MAX_DEVICES_PER_AGENT",
                "REMINDER_PLUGIN_SESSION_EXPIRE_SECONDS"):
        if key not in values:
            updates[key] = "" if key.endswith("IDS") else ("3" if key.endswith("PER_AGENT") else "3600")
    for key, value in updates.items():
        set_key(path, key, value, quote_mode="always", encoding="utf-8")
    print("插件登录配置已保存到项目 .env；密码不显示在终端。重启原 ICS 后生效。")


if __name__ == "__main__":
    main()
