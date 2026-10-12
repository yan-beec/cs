# ICS 转人工桌面提醒插件

`reminder` 是现有 ICS 项目中的一个功能目录。它复用 ICS 的 FastAPI、PostgreSQL 连接池、Chatwoot 客户端和原有转人工流程，在 Chatwoot 会话成功分配给客服后，通过客服电脑上的 Windows 客户端显示置顶弹窗并播放提示音。

本插件不修改 Chatwoot 源码，也不新增一套独立后端。服务端提醒接口随 ICS 一起运行，桌面客户端只负责登录、轮询提醒、弹窗、打开会话和确认收到。

## 一、功能和运行方式

- ICS 完成转人工、分配坐席并打开人工会话后，创建该坐席的提醒。
- 客服客户端每隔几秒查询自己的提醒，弹出置顶窗口并播放提示音。
- 客服可以直接打开对应的 Chatwoot 会话，也可以点击“确认收到”。
- Chatwoot 会话被解决或改派后，提醒会同步撤销或转给新的坐席。
- 同一台电脑可以在授权席位之间切换，不需要重复登录。
- 客户端关闭窗口后继续驻留 Windows 系统托盘；选择“退出程序”才会停止后台轮询。

提醒客户端只接收当前席位的提醒，不改变 Chatwoot 的在线状态、浏览器登录状态或会话分配结果。

## 二、部署前准备

部署机器需要具备：

1. 已能正常运行的 ICS 项目和 PostgreSQL。
2. 可访问的 Chatwoot 服务。
3. Chatwoot 用户级 API Token，并具有读取坐席、读取会话状态和读取会话分配的权限。
4. Chatwoot 账户 ID，即 `CHATWOOT_ACCOUNT_ID`。
5. Windows 客服电脑；客户端支持 Windows 10/11。

Chatwoot 相关配置沿用 ICS 原配置，至少确认以下配置正确：

```dotenv
CHATWOOT_BASE_URL=http://127.0.0.1:3000
CHATWOOT_PUBLIC_BASE_URL=https://chat.example.com
CHATWOOT_API_TOKEN=填写Chatwoot用户级API Token
CHATWOOT_CONTACT_API_TOKEN=填写专用用户级Token；留空时复用 CHATWOOT_API_TOKEN
CHATWOOT_ACCOUNT_ID=填写Chatwoot账户ID
```

`CHATWOOT_BASE_URL` 是 ICS 服务端访问 Chatwoot 的地址；`CHATWOOT_PUBLIC_BASE_URL` 是客服浏览器访问 Chatwoot 的地址。两者可以相同，也可以不同。客户端的“打开 Chatwoot 会话”使用公开地址。

## 三、配置 ICS 提醒功能

在项目根目录 `.env` 中加入或修改以下配置：

```dotenv
# 启用提醒功能
REMINDER_ENABLED=true
REMINDER_POLL_SECONDS=2
REMINDER_REPEAT_SECONDS=30

# 插件独立登录，不使用 ICS 管理员账号和密码
REMINDER_PLUGIN_USERNAME=reminder
REMINDER_PLUGIN_PASSWORD=填写插件专用密码
REMINDER_PLUGIN_PASSWORD_HASH=
REMINDER_PLUGIN_SECRET=填写至少32字符的随机签名密钥

# 留空表示允许当前 Chatwoot 账户的全部坐席；也可填写 11,12
REMINDER_PLUGIN_ALLOWED_AGENT_IDS=

# 仅用于首次登录和注册设备，单位为秒
REMINDER_PLUGIN_SESSION_EXPIRE_SECONDS=3600
```

插件密码和签名密钥不要提交到 Git，也不要写入客户端程序。推荐使用配置工具生成随机值：

```powershell
Set-Location D:\project\CS
& 'C:\Users\lenovo\Anaconda3\envs\cs\python.exe' -m reminder.backend.configure_login --generate
```

该命令只补齐缺失的插件账号、随机密码和签名密钥，不会覆盖已有配置。生成的插件密码保存在项目根目录 `.env` 的 `REMINDER_PLUGIN_PASSWORD` 中。

需要手动设置密码时，省略 `--generate`：

```powershell
& 'C:\Users\lenovo\Anaconda3\envs\cs\python.exe' -m reminder.backend.configure_login
```

命令会隐藏密码输入。若配置了非空的 `REMINDER_PLUGIN_PASSWORD_HASH`，服务端优先使用 PBKDF2 哈希校验，并忽略明文密码。

## 四、启动或更新 ICS 服务

### 方式 A：Docker Compose

本地 Compose 使用现有的 `ics-agent` 服务，不需要启动第二个提醒服务。修改 `.env` 后，必须重新创建容器，才能让新环境变量生效：

```powershell
Set-Location D:\project\CS
docker compose up -d --no-deps --force-recreate ics-agent
```

查看服务状态：

```powershell
docker compose ps
```

如果代码也发生变化，先按项目原有方式重新构建，再启动：

```powershell
docker compose build ics-agent
docker compose up -d ics-agent
```

本地开发覆盖配置会把 `ics_agent` 和 `reminder` 挂载到容器，并自动重载 Python 代码；但是 `.env` 变化仍需要重新创建容器。

### 方式 B：直接运行 ICS

在项目根目录执行：

```powershell
Set-Location D:\project\CS
& 'C:\Users\lenovo\Anaconda3\envs\cs\python.exe' -m ics_agent.app.serve --host 127.0.0.1 --port 3001
```

如果 ICS 已经占用 3001 端口，不要重复启动；修改配置后重启原来的 ICS 进程即可。

提醒功能首次启动时会在现有 PostgreSQL 中创建或升级提醒相关表，包括 `reminder_notifications` 和 `reminder_devices`，不需要手工执行建表 SQL。

## 五、安装 Windows 客户端

### 直接使用已构建程序

发布文件位于：

```text
D:\project\CS\reminder\dist\ICSReminder.exe
```

将这个 exe 复制到客服电脑即可。程序不包含项目 `.env`、ICS 管理员密码或 Chatwoot Token。

### 在当前环境重新构建

使用项目指定的 `cs` Python 环境：

```powershell
Set-Location D:\project\CS
& 'C:\Users\lenovo\Anaconda3\envs\cs\python.exe' -m pip install -r reminder\requirements-build.txt
& 'C:\Users\lenovo\Anaconda3\envs\cs\python.exe' reminder\packaging\build.py
```

构建结果仍为 `reminder/dist/ICSReminder.exe`。构建缓存会生成在 `reminder/build/`，该目录不需要复制到客服电脑。

首次测试桌面弹窗而不连接 ICS：

```powershell
& 'C:\Users\lenovo\Anaconda3\envs\cs\python.exe' -m reminder.client.main --demo
```

演示模式只测试置顶窗口和提示音，不读取服务器、Chatwoot 或本机设备凭据。

## 六、首次使用客户端

1. 双击 `ICSReminder.exe`。
2. 在登录页填写 ICS 地址，例如 `http://127.0.0.1:3001` 或实际 HTTPS 地址。
3. 填写“账号”和“密码”。这里使用的是 `REMINDER_PLUGIN_USERNAME` 与 `REMINDER_PLUGIN_PASSWORD`，不是 ICS 管理员账号密码。
4. 点击“登录插件并读取授权席位”。客户端会从 Chatwoot 同步授权坐席列表。
5. 选择当前值班客服，点击“使用所选席位并发送试验提醒”。
6. 客户端收到试验提醒后，确认置顶效果、提示音、会话链接和“确认收到”按钮。
7. 试验通过后，客户端可以关闭窗口；程序会继续在系统托盘中运行。

勾选“在本机记住设备”后，设备凭据会使用 Windows DPAPI 加密保存到：

```text
%APPDATA%\ICSReminder\device.json
```

客户端不会保存插件密码或临时登录令牌。取消勾选后，退出客户端不会保留设备凭据。

## 七、客服日常使用

### 接收转人工提醒

当客户触发转人工且 ICS 成功分配坐席后：

1. 当前被分配坐席的客户端在下一次轮询显示置顶提醒。
2. 点击“打开 Chatwoot 会话”进入对应会话。
3. 处理客户后点击“确认收到”，停止本地重复提示。

其他坐席不会收到这条提醒。会话被解决后，提醒自动撤销；会话被手工改派后，原坐席提醒撤销，新坐席收到新提醒。

### 共享电脑切换席位

多人共用一台客服电脑时：

1. 打开客户端主界面。
2. 在“当前值班席位”下拉框中选择新的客服。
3. 点击“切换席位”。
4. 等待状态显示切换成功后，新的席位开始接收提醒。

席位列表每分钟自动刷新，也可以手动点击“刷新席位列表”。切换只改变当前电脑接收谁的提醒，不会改变 Chatwoot 中的会话分配。

每个席位同时只允许一台有效设备。若切换到已被其他电脑占用的席位，另一台电脑的插件设备凭据会失效。

### 托盘和退出

- 点击窗口关闭按钮：隐藏到系统托盘，提醒继续运行。
- 托盘菜单“打开 ICS 客服提醒”：恢复主界面。
- 托盘菜单“查看当前提醒”：恢复提醒窗口。
- 托盘菜单“退出程序”：停止轮询并退出客户端。
- 重复启动程序：唤起已有客户端，不会创建多个提醒进程。

退出插件登录会吊销服务器上的当前设备，并删除本机保存的设备凭据。网络中断时，本机会先清除凭据；服务器侧吊销需要恢复网络后由管理员处理。

## 八、真实功能验收

建议按以下顺序验收：

1. 用 `--demo` 验证 Windows 置顶弹窗、声音和托盘。
2. 用插件账号登录，确认能读取真实 Chatwoot 坐席。
3. 发送一次试验提醒，确认客户端能收到并确认。
4. 从真实客户入口触发“转人工”，确认目标坐席收到提醒。
5. 在 Chatwoot 中解决会话，确认提醒撤销。
6. 手工改派会话，确认原坐席提醒撤销、新坐席收到提醒。
7. 在共享电脑上执行 A → B → A 席位切换，确认每个席位的提醒和确认状态互不混淆。
8. 重启 ICS 和客户端，确认未确认的有效提醒可以恢复。

## 九、常见问题

### 登录返回“提醒功能未启用”

检查 `.env`：

```dotenv
REMINDER_ENABLED=true
```

修改后必须重启或重新创建 `ics-agent` 容器。

### 登录返回“请先配置提醒插件的独立登录密码”

执行配置工具生成密码，并确认 ICS 服务重新加载了 `.env`：

```powershell
& 'C:\Users\lenovo\Anaconda3\envs\cs\python.exe' -m reminder.backend.configure_login --generate
docker compose up -d --no-deps --force-recreate ics-agent
```

### 能登录但读取不到坐席

检查 `CHATWOOT_ACCOUNT_ID`、`CHATWOOT_BASE_URL`、`CHATWOOT_API_TOKEN` 或 `CHATWOOT_CONTACT_API_TOKEN`。坐席查询必须使用用户级 API Token，不能使用 AgentBot Token。

### 能收到提醒但打不开会话

检查 `CHATWOOT_PUBLIC_BASE_URL`。该地址必须能被客服浏览器访问，不能填写仅容器内部可访问的地址。

### 修改了密码但旧客户端仍能运行

修改插件密码、签名密钥、账户或授权范围后，重启 ICS 会使已有插件令牌和设备凭据失效。客户端需要重新登录；如旧程序仍在托盘运行，请先选择“退出程序”，再启动新版 exe。

### 客户端没有弹窗

确认客户端没有被退出，只是隐藏到托盘；确认当前席位与 Chatwoot 实际分配席位一致；再检查 Windows 音量、通知设置以及客户端到 ICS 地址的网络连接。

## 十、安全边界

- 插件账号独立于 `system_users` 和 ICS 管理员账号，只能访问提醒相关接口。
- 插件临时登录令牌不能访问 ICS 管理后台。
- 客户端不保存 Chatwoot API Token、ICS 管理员密码或插件密码。
- 设备凭据服务端只保存摘要，客户端使用当前 Windows 用户的 DPAPI 加密保存。
- 生产环境建议使用 HTTPS，并限制 `REMINDER_PLUGIN_ALLOWED_AGENT_IDS` 到实际需要的席位。
- `.env`、数据库连接密码、Chatwoot Token 和插件签名密钥不得提交到 Git 或打包进 exe。

## 十一、相关文档和测试

- [开发计划](docs/development-plan.md)
- [插件登录与席位切换](docs/plugin-login-and-seats.md)
- [验收记录](docs/acceptance.md)

运行提醒模块测试：

```powershell
Set-Location D:\project\CS
& 'C:\Users\lenovo\Anaconda3\envs\cs\python.exe' -m pytest reminder/tests -q
```

测试使用隔离数据和模拟 Chatwoot 响应，不会向真实客户发送消息。
