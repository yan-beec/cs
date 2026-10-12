# ICS 转人工桌面提醒插件

`reminder` 是现有 ICS 项目中的一个功能目录。它复用 ICS 的 FastAPI、PostgreSQL 连接池、Chatwoot 客户端和原有转人工流程，在 Chatwoot 会话成功分配给客服后，通过客服电脑上的 Windows 客户端显示置顶弹窗并播放提示音。

本插件不修改 Chatwoot 源码，也不新增一套独立后端。服务端提醒接口随 ICS 一起运行，桌面客户端负责登录、轮询提醒、强弹窗和确认收到；Chrome/Edge 浏览器插件负责核验 Chatwoot 登录状态、复用标签页并打开会话。

## 一、功能和运行方式

- ICS 完成转人工、分配坐席并打开人工会话后，创建该坐席的提醒。
- 客服客户端每隔几秒查询自己的提醒，弹出置顶窗口并播放提示音。
- 提醒弹窗默认 `500×360`，最小 `400×300`，可以拖动窗口边框调整大小；本次运行内后续提醒沿用调整后的尺寸，重启后恢复默认大小。
- 会话标签使用 12 号字体，摘要内容使用 10 号字体，便于快速选择会话并查看摘要。
- 所有提醒的浏览器按钮统一显示“打开 Chatwoot 工作台”。试验提醒进入当前账户的工作台，真实提醒进入对应客户会话；浏览器插件优先复用同站点、同账户的已登录标签页，并恢复浏览器窗口；没有工作台标签时，在已授权且已连接的浏览器配置文件中打开目标页面。
- 点击“打开 Chatwoot 工作台”不会确认提醒，也不会收起提醒弹窗；每条提醒都需要单独点击“确认收到”，有多条提醒时确认一条后保留其他提醒，全部确认后才收起弹窗。登录过期、插件未连接或检测失败时，客户端提示重试，不通过默认浏览器补开页面。
- 提醒弹窗右上角的关闭按钮不会结束未确认提醒；请通过“确认收到”逐条结束提醒。
- Chatwoot 会话被解决或改派后，提醒会同步撤销或转给新的坐席。
- 同一台电脑可以在授权席位之间切换，不需要重复登录。
- 客户端主窗口关闭后继续驻留 Windows 系统托盘；选择“退出程序”才会停止后台轮询。

提醒客户端只接收当前席位的提醒，不改变 Chatwoot 的在线状态、浏览器登录状态或会话分配结果。“打开 Chatwoot 工作台”需要安装并授权浏览器插件，提醒账号登录不能替代 Chatwoot 网页登录。

## 二、部署前准备

部署机器需要具备：

1. 已能正常运行的 ICS 项目和 PostgreSQL。
2. 可访问的 Chatwoot 服务。
3. Chatwoot 用户级 API Token，并具有读取坐席、读取会话状态和读取会话分配的权限。
4. Chatwoot 账户 ID，即 `CHATWOOT_ACCOUNT_ID`。
5. Windows 客服电脑；客户端支持 Windows 10/11。打开会话需要 Chrome 或 Edge，并在实际使用的浏览器配置文件中安装、授权本插件。

Chatwoot 相关配置沿用 ICS 原配置，至少确认以下配置正确：

```dotenv
CHATWOOT_BASE_URL=http://127.0.0.1:3000
CHATWOOT_PUBLIC_BASE_URL=https://chat.example.com
CHATWOOT_API_TOKEN=填写Chatwoot用户级API Token
CHATWOOT_CONTACT_API_TOKEN=填写专用用户级Token；留空时复用 CHATWOOT_API_TOKEN
CHATWOOT_ACCOUNT_ID=填写Chatwoot账户ID
```

`CHATWOOT_BASE_URL` 是 ICS 服务端访问 Chatwoot 的地址；`CHATWOOT_PUBLIC_BASE_URL` 是客服浏览器访问 Chatwoot 的地址。两者可以相同，也可以不同。客户端的“打开 Chatwoot 工作台”使用公开地址：试验提醒打开账户工作台，真实提醒打开对应会话。

ICS 配置的 `CHATWOOT_PUBLIC_BASE_URL`、浏览器插件保存的 Chatwoot 地址，以及浏览器实际登录 Chatwoot 的地址，必须使用相同的协议、主机、端口和部署路径。在这台开发电脑上，如果浏览器打开的是 `http://localhost:3000/app/accounts/1/dashboard`，公开地址与插件地址应填写 `http://localhost:3000`。`localhost`、`127.0.0.1` 和局域网 IP 是不同的站点地址，即使指向同一服务，也不能混用来复用登录页面。

`localhost` 只适用于服务就在当前电脑上的本地测试。其他客服电脑应统一使用能访问 Chatwoot 服务的局域网地址或正式公开地址，例如 `http://192.168.110.16:3000` 或 `https://chat.example.com`；不要把开发电脑的 `localhost` 配置直接用于所有客服电脑。这里要求一致的是 Chatwoot 公开地址，不是客户端登录提醒账号时填写的 ICS 地址。

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

# 每个席位最多同时有效的插件客户端数量；第4台绑定/切换时才吊销最旧设备
REMINDER_PLUGIN_MAX_DEVICES_PER_AGENT=3

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

### 使用本次已构建的安装包

本次新版安装包位于：

```text
D:\project\CS\reminder\dist\browser-plugin\ICSReminderSetup.exe
```

本次更新版本为 `1.0.2`，统一保留“打开 Chatwoot 工作台”按钮，打开页面后继续显示提醒，全部逐条确认后才收起，并调整会话标签与摘要字号。需要安装新版客户端后这些改动才会生效。

本次使用单独输出目录，避免覆盖仍在运行的旧版 `reminder/dist/ICSReminder.exe`。请使用上面的新版安装包；关闭旧客户端窗口只会隐藏到托盘，需要从托盘选择“退出程序”才会退出。

安装操作：

1. 将 `ICSReminderSetup.exe` 复制到客服电脑；只需分发这一个文件。
2. 如已有旧版，先从系统托盘退出提醒程序，并关闭连接插件的浏览器。
3. 双击安装包，确认“安装目录”。默认是 `%LOCALAPPDATA%\Programs\ICSReminder`；也可以点击“选择安装目录”或直接输入路径，例如 `D:\ICSReminder`。请选择当前 Windows 用户有写入权限的独立程序目录，再点击“安装到本机”。安装不需要 Python 或 Conda；使用有写入权限的目录时无需管理员权限。
4. 安装包自动安装桌面程序、本机浏览器连接组件、插件文件和安装说明，并注册 Chrome/Edge 通信、开始菜单入口和 Windows 卸载项。程序不包含项目 `.env`、ICS 管理员密码或 Chatwoot Token。
5. 点击“查看插件安装说明”，按下一节完成浏览器插件加载、站点授权和登录。安装后可点击“启动提醒程序”，也可从开始菜单打开“ICS 客服提醒”。

当前浏览器插件尚未上架 Chrome Web Store / Edge Add-ons，安装包会准备插件文件，但浏览器中的加载和首次站点授权仍需手动完成。详细说明见[浏览器插件与统一安装包](docs/browser-extension-install.md)。

### 在当前环境重新构建

使用项目指定的 `cs` Python 环境：

```powershell
Set-Location D:\project\CS
& 'C:\Users\lenovo\Anaconda3\envs\cs\python.exe' -m pip install -r reminder\requirements-build.txt
& 'C:\Users\lenovo\Anaconda3\envs\cs\python.exe' reminder\packaging\build.py --installer
```

上述命令默认输出到 `reminder/dist/`，生成 `ICSReminderSetup.exe`、`ICSReminder.exe`、`ICSBrowserHost.exe` 和 `ICSChatwootExtension.zip`。安装包已内置桌面程序、连接组件、插件和说明，客服电脑只需接收 `ICSReminderSetup.exe`。构建缓存位于 `reminder/build/`，不需要复制。

要沿用本次的独立输出目录，使用：

```powershell
& 'C:\Users\lenovo\Anaconda3\envs\cs\python.exe' reminder\packaging\build.py --installer --output-dir reminder\dist\browser-plugin
```

单独构建桌面程序时可省略 `--installer`，但这不会生成浏览器连接组件和安装包；首次使用完整功能应运行统一安装包。

首次测试桌面弹窗而不连接 ICS：

```powershell
& 'C:\Users\lenovo\Anaconda3\envs\cs\python.exe' -m reminder.client.main --demo
```

演示模式只测试置顶窗口和提示音，不读取服务器、Chatwoot 或本机设备凭据。

### 升级和卸载

升级时先从托盘退出桌面客户端，并关闭插件所在的浏览器，再运行新版安装包。安装向导会读取已有安装目录，按原目录升级，保留 `%APPDATA%\ICSReminder` 下的设备登录和席位配置。本地插件更新后，在扩展管理页点击“重新加载”或重启浏览器，再确认连接恢复。

如需更换已经安装的程序目录，请先卸载旧版，再运行安装包选择新目录重装。卸载会保留设备配置，重装后在浏览器扩展管理页移除原插件，再加载新安装目录下的 `extension` 文件夹并完成站点授权。不要直接移动已安装的文件夹，否则本机连接登记、快捷方式和浏览器插件路径可能失效。

卸载时同样先退出客户端和浏览器，在 Windows 设置的应用列表中找到“ICS 客服提醒”并卸载。卸载向导固定使用该程序的安装目录，不提供目录修改。卸载会移除程序、本机通信登记、快捷方式和卸载项，保留 `%APPDATA%\ICSReminder` 下的设备配置。浏览器插件需要在 Chrome/Edge 扩展管理页单独移除。

## 六、首次使用客户端

### 登录提醒账号并绑定席位

1. 从安装向导点击“启动提醒程序”，或从开始菜单打开“ICS 客服提醒”。
2. 在登录页填写 ICS 地址，例如 `http://127.0.0.1:3001` 或实际 HTTPS 地址。
3. 填写“账号”和“密码”。这里使用的是 `REMINDER_PLUGIN_USERNAME` 与 `REMINDER_PLUGIN_PASSWORD`，不是 ICS 管理员账号密码。
4. 点击“登录插件并读取授权席位”。客户端会从 Chatwoot 同步授权坐席列表。
5. 选择当前值班客服，点击“使用所选席位并发送试验提醒”。
6. 客户端收到试验提醒后，确认置顶效果、提示音、调整窗口大小和“确认收到”按钮。试验提醒提供“打开 Chatwoot 工作台”，目标是当前账户的工作台，例如 `/app/accounts/1/dashboard`，无需先创建客户会话。
7. 完成以下浏览器插件设置并在该浏览器登录 Chatwoot，再发送一次试验提醒，点击“打开 Chatwoot 工作台”检查页面复用，确认提醒弹窗仍显示，再点击“确认收到”。随后用独立测试会话检查真实提醒的同名按钮能进入对应会话。验证通过后，可以关闭客户端主窗口；程序会继续在系统托盘中运行。

勾选“在本机记住设备”后，设备凭据会使用 Windows DPAPI 加密保存到：

```text
%APPDATA%\ICSReminder\device.json
```

客户端不会保存插件密码或临时登录令牌。取消勾选后，退出客户端不会保留设备凭据。

### 加载浏览器插件并连接 Chatwoot

1. 打开客服实际登录 Chatwoot 的 Chrome 或 Edge 配置文件。Chrome 访问 `chrome://extensions`，Edge 访问 `edge://extensions`；安装向导也提供打开扩展页面的按钮。
2. 开启“开发者模式”，选择“加载已解压的扩展程序”或“加载解压缩的扩展”，选择“所选安装目录\extension”文件夹。例如安装到 `D:\ICSReminder` 时，选择 `D:\ICSReminder\extension`；使用默认目录时，选择 `%LOCALAPPDATA%\Programs\ICSReminder\extension`。安装说明页会显示本次实际插件路径。请选择文件夹，不是构建目录中的 zip 文件。
3. 安装后会打开插件设置页；之后也可以点击插件图标打开“ICS 浏览器连接设置”。
4. 填写客服浏览器访问的 Chatwoot 地址，例如 `https://chat.example.com`。该地址的协议、主机、端口和部署路径必须与 ICS 的 `CHATWOOT_PUBLIC_BASE_URL` 及实际登录页面一致。本机开发浏览器使用 `http://localhost:3000` 时，插件也填写此地址；其他客服电脑使用能访问服务的统一局域网或正式公开地址。
5. 点击“保存地址并授权此站点”，按浏览器提示完成站点访问授权。
6. 保持桌面提醒程序运行，确认插件设置页显示“已连接桌面提醒程序”，客户端主窗口显示“浏览器插件已连接”及配置文件数量。未连接时先启动桌面程序，再点“重新连接”；插件也会每分钟自动尝试重连。
7. 在此浏览器配置文件中登录 Chatwoot，确认可以访问提醒所属的账户。先发送试验提醒，点击“打开 Chatwoot 工作台”检查原标签页复用和最小化窗口恢复；真实会话提醒使用同名按钮进入对应客户会话。打开页面后提醒弹窗继续显示，“确认收到”仍需逐条单独点击。

每个浏览器配置文件需要单独加载插件、保存地址、站点授权和登录 Chatwoot。多个 Chrome/Edge 配置文件同时连接时，客户端会同时检测，选择符合站点和账户要求的已登录标签，最终只导航一个标签。当前不支持无痕模式。

登录检测适配当前 Chatwoot 的 `cw_d_session_info` 和 `/auth/validate_token`，支持页面与 API 同源部署；独立 API 域名或不同认证方式需要另行适配。没有工作台标签时，插件会在自己的浏览器配置文件中打开会话，该页面仍可能要求登录。

## 七、客服日常使用

### 接收转人工提醒

当客户触发转人工且 ICS 成功分配坐席后：

1. 当前被分配坐席的客户端在下一次轮询显示置顶提醒。
2. 点击“打开 Chatwoot 工作台”，插件核验同站点、同账户的登录状态，在原标签页进入对应会话并恢复浏览器窗口；无工作台时在已连接的配置文件中打开会话。提醒弹窗继续显示，提醒仍未确认。
3. 处理客户后点击当前提醒的“确认收到”，停止该条提醒的本地重复提示。有多条提醒时，选择下一条继续处理并确认；全部提醒确认后，弹窗才收起。

其他坐席不会收到这条提醒。会话被解决后，提醒自动撤销；会话被手工改派后，原坐席提醒撤销，新坐席收到新提醒。

### 共享电脑切换席位

多人共用一台客服电脑时：

1. 打开客户端主界面。
2. 在“当前值班席位”下拉框中选择新的客服。
3. 点击“切换席位”。
4. 等待状态显示切换成功后，新的席位开始接收提醒。

席位列表每分钟自动刷新，也可以手动点击“刷新席位列表”。切换只改变当前电脑接收谁的提醒，不会改变 Chatwoot 中的会话分配。

每个席位默认允许最多 3 台插件客户端同时有效。前 3 台都会收到该席位的提醒；第 4 台绑定或切换到该席位时，服务端按最后心跳和创建时间吊销最旧的一台插件设备。数量由 `REMINDER_PLUGIN_MAX_DEVICES_PER_AGENT` 配置。

### 托盘和退出

- 点击主窗口关闭按钮：隐藏到系统托盘，提醒继续运行。
- 点击提醒弹窗右上角关闭按钮：不会结束未确认提醒，仍需逐条点击“确认收到”。
- 托盘菜单“打开 ICS 客服提醒”：恢复主界面。
- 托盘菜单“查看当前提醒”：恢复提醒窗口。
- 托盘菜单“退出程序”：停止轮询并退出客户端。
- 重复启动程序：唤起已有客户端，不会创建多个提醒进程。

退出插件登录会吊销服务器上的当前设备，并删除本机保存的设备凭据。网络中断时，本机会先清除凭据；服务器侧吊销需要恢复网络后由管理员处理。

## 八、真实功能验收

建议按以下顺序验收：

1. 用 `--demo` 验证 Windows 置顶弹窗、声音和托盘；拖动边框调整大小，确认本次运行内后续弹窗沿用尺寸。
2. 运行统一安装包，分别检查默认目录和自选的有写入权限目录；在实际使用的 Chrome/Edge 配置文件中加载对应安装目录的插件并完成站点授权，确认插件和桌面程序已连接。
3. 用提醒账号登录，确认能读取真实 Chatwoot 坐席；发送一次试验提醒，确认客户端能收到，点击“打开 Chatwoot 工作台”复用已登录页面且提醒弹窗仍显示，再单独点击“确认收到”收起弹窗。该步骤无需客户会话；`--demo` 仍只验证本地弹窗与声音，不能验证登录或浏览器连接。
4. 使用独立测试会话从客户入口触发“转人工”，确认目标坐席收到提醒。
5. 保持 Chatwoot 已登录并最小化浏览器，点击“打开 Chatwoot 工作台”，确认复用原标签并进入对应客户会话、恢复窗口且提醒弹窗仍显示；同时保留多条提醒，确认一条后其他提醒继续显示，确认最后一条后弹窗收起。
6. 检查 Chrome/Edge 同时打开、多个配置文件、无工作台、登录过期、未授权和连接中断等情况。检测失败时应提示重试，不通过默认浏览器补开页面；重复点击不应创建多个会话标签。
7. 在 Chatwoot 中解决会话，确认提醒撤销；手工改派会话，确认原坐席提醒撤销、新坐席收到提醒。
8. 在共享电脑上执行 A → B → A 席位切换，确认每个席位的提醒和确认状态互不混淆。
9. 重启 ICS、客户端和浏览器，确认未确认的有效提醒可以恢复、插件可以重新连接；检查升级时自动沿用原安装目录且设置保留，卸载后浏览器插件可单独移除；需更换目录时，按先卸载、再重装的流程检查连接恢复。

本次 `1.0.2` 已通过完整 Python 回归（包括桌面 GUI 测试）及 12 项浏览器插件测试；补充异步打开与席位切换保护后，15 项桌面回归测试再次全部通过。安装包内的客户端代码已核对与当前源码一致。此前用户已确认试验提醒能正常打开工作台；新版弹窗行为仍需安装后按以上步骤实际验收，真实客户会话、Chrome/Edge 双开、多配置文件及其他异常场景需分别检查。

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

### 能收到提醒但打不开工作台或会话

检查 `CHATWOOT_PUBLIC_BASE_URL`。该地址必须能被客服浏览器访问，不能填写仅容器内部可访问的地址。确认客户端显示“浏览器插件已连接”，插件配置的 Chatwoot 地址、该公开地址与浏览器实际登录页面的协议、主机、端口和部署路径一致，并且已经完成站点授权。登录过期时先在 Chatwoot 登录，并确认有目标账户的访问权限，再重试。

提示 Chatwoot 地址不一致时，先核对这三处地址。例如服务端公开地址为 `http://192.168.110.16:3000`，插件和浏览器却使用 `http://localhost:3000`，即使插件已连接也无法匹配。仅在本开发电脑上测试时，可以统一为 `http://localhost:3000`；正式部署和其他客服电脑应统一为可访问服务的局域网或公开地址。修改服务端 `.env` 后重启或重新创建 ICS 服务，插件保存正确地址并完成授权，再在相同地址登录 Chatwoot 后重试。

没有已连接且已授权的插件，或者已有页面无法通过登录核验时，客户端会提示失败，不会通过默认浏览器补开页面。请在实际使用的浏览器配置文件中检查设置。

### 试验提醒的打开按钮不可点击

试验提醒和真实会话提醒都应显示“打开 Chatwoot 工作台”：前者打开账户工作台，后者进入对应客户会话。旧版试验提醒未提供工作台地址，会导致打开按钮禁用；仍显示“打开 Chatwoot 会话”时，请先从托盘退出旧程序，再安装并启动 `1.0.2` 或更新版本。请更新服务端与客户端、重新加载浏览器插件后，重新发送一次试验提醒。`--demo` 是纯本地演示，不提供浏览器打开入口，也不会登录 ICS 或 Chatwoot。

### 浏览器插件显示未连接

先确认已运行统一安装包，并通过开始菜单启动安装后的客户端。只复制旧版 `ICSReminder.exe` 不能完成本机连接注册。再检查插件是否已启用、是否加载了安装目录的 `extension` 文件夹，并在插件设置页点击“重新连接”。更新后可重新加载插件或重启浏览器。

本机安装和连接登记属于当前 Windows 用户。换用其他 Windows 用户时，需要在该用户下重新安装、加载插件并完成授权。公司浏览器若禁用了开发者模式或本机通信，需要由管理方按公司策略部署插件。

### 更新安装提示文件被占用

在系统托盘选择“退出程序”，并退出连接插件的 Chrome/Edge 后重试。仅关闭客户端主窗口不会结束程序。构建时如需保留正在运行的旧版，可使用前面的 `--output-dir reminder\dist\browser-plugin` 单独输出新版。

### 自选目录无法安装，或希望更换安装目录

请选择当前 Windows 用户有写入权限的独立程序目录，例如 `D:\ICSReminder`，不要直接选择磁盘根目录或存放其他文件的公共目录。目录无写入权限时，改用默认目录或其他可写目录。

已有安装时按原目录升级；如需改到其他位置，先从 Windows 应用列表卸载旧版，再选择新目录安装，并重新加载新目录的浏览器插件。设备登录和席位配置保存在 `%APPDATA%\ICSReminder`，与程序安装位置分开。

### 修改了密码但旧客户端仍能运行

修改插件密码、签名密钥、账户或授权范围后，重启 ICS 会使已有插件令牌和设备凭据失效。客户端需要重新登录；如旧程序仍在托盘运行，请先选择“退出程序”，再启动新版 exe。

### 客户端没有弹窗

确认客户端没有被退出，只是隐藏到托盘；确认当前席位与 Chatwoot 实际分配席位一致；再检查 Windows 音量、通知设置以及客户端到 ICS 地址的网络连接。

## 十、安全边界

- 插件账号独立于 `system_users` 和 ICS 管理员账号，只能访问提醒相关接口。
- 插件临时登录令牌不能访问 ICS 管理后台。
- 客户端不保存 Chatwoot API Token、ICS 管理员密码或插件密码。
- 浏览器插件使用 Chatwoot 页面已有登录环境核验状态；登录凭据留在页面，插件和本机连接消息不传 Cookie、密码、API Token 或客户消息。
- 本机浏览器连接使用当前 Windows 用户、当前登录会话的认证命名管道，不开放 TCP 监听端口；随机认证密钥由 DPAPI 加密保存在 `%APPDATA%\ICSReminder\browser.key`。
- 设备凭据服务端只保存摘要，客户端使用当前 Windows 用户的 DPAPI 加密保存。
- 生产环境建议使用 HTTPS，并限制 `REMINDER_PLUGIN_ALLOWED_AGENT_IDS` 到实际需要的席位。
- `.env`、数据库连接密码、Chatwoot Token 和插件签名密钥不得提交到 Git 或打包进 exe。

## 十一、相关文档和测试

- [开发计划](docs/development-plan.md)
- [插件登录与席位切换](docs/plugin-login-and-seats.md)
- [验收记录](docs/acceptance.md)
- [浏览器插件与统一安装包](docs/browser-extension-install.md)
- [弹窗与浏览器打开改进](docs/client-improvements.md)

运行提醒模块和浏览器插件测试（插件测试需要 Node.js）：

```powershell
Set-Location D:\project\CS
& 'C:\Users\lenovo\Anaconda3\envs\cs\python.exe' -m pytest reminder/tests -q
node --test reminder/extension/tests/core.test.mjs
```

测试使用隔离数据、模拟 Chatwoot 响应和模拟浏览器，不会向真实客户发送消息；真实浏览器联测按“真实功能验收”执行。
