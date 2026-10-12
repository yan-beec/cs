"""PyInstaller 入口，只引入提醒客户端，不引入 ICS 服务端或配置。"""

from reminder.client.main import main


if __name__ == "__main__":
    main()
