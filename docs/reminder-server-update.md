# ICS 强弹窗提醒功能：服务器更新与客户端升级操作手册

本文用于已经运行 ICS 和 Chatwoot 的服务器，发布 `reminder` 功能及 Windows 客户端 `1.0.3`。本次新增真实会话提醒的收件箱名称显示，并保留工作台按钮和逐条确认行为。本次按直接覆盖源码、不做备份的方式更新：代码发布、源码同步、更新配置、构建、验证，再升级客服电脑。第 3 节备份与第 12 节基于备份的回滚仅供选择该方式时参考，正常更新不依赖备份记录文件。

服务器提供提醒接口、设备与席位绑定、待确认通知和收件箱信息；客服电脑的客户端和 Chrome/Edge 插件提供弹窗、提示音、复用浏览器页面。**服务器部署和客服电脑升级都需要完成**：服务端更新负责保存和补齐收件箱名称，客户端 `1.0.3` 负责在会话标签和详情中显示；按钮名称、打开后保留弹窗、字体调整也属于客户端行为，只更新服务器不会改变已安装的软件。新版客户端兼容旧服务端和缺少收件箱字段的数据，但需两端均更新才能显示名称。

本手册依据 [服务器首次部署说明](server-first-deployment.md)、[日常同步更新说明](server-sync-update.md) 和 [提醒模块 README](../reminder/README.md) 核对。文中的命令未在远端服务器实际执行。

## 0. 确认本次发布约定

| 项目 | 本次使用的值 |
| --- | --- |
| 开发电脑 | Windows，项目目录 `D:\project\CS` |
| 本机 Python | `C:\Users\lenovo\Anaconda3\envs\cs\python.exe` |
| 服务器登录 | Windows PowerShell 执行 `ssh ics`，沿用已配置的 SSH config |
| Git 地址 | `https://code-sayu.xingyongshe.com/backend/chatwoot.git` |
| 服务器发布分支 | `test` |
| 服务器 Git 源码目录 | `/opt/cs-src` |
| 服务器实际运行目录 | `/opt/cs` |
| Compose 服务 / 镜像 | `ics-agent` / `ics-agent:py310` |
| ICS 内网地址 | 现有部署文档使用 `http://172.20.48.12:3001` |
| Chatwoot 浏览器地址 | 现有部署文档使用 `http://172.20.48.12:3000` |
| Windows 新版安装包 | `D:\project\CS\reminder\dist\browser-plugin\ICSReminderSetup.exe` |

地址和目录沿用已有文档，登录后先与实际部署核对。如果已经更换域名、IP、端口或目录，请将下文对应值统一替换。本手册不要求重新配置 SSH 别名。

完整顺序：

1. 将功能代码合入并推送远端 `test`，记录提交 SHA。
2. `ssh ics`，检查现有服务；本次不备份，跳过第 3 节。
3. 在 `/opt/cs-src` 拉取 `test`，核对 SHA。
4. 将代码同步到 `/opt/cs`，包含新增的 `reminder` 目录。
5. 在服务器原 `.env` 中合并提醒参数和正确的 Chatwoot 公开地址。
6. 构建新镜像，首次启用时配置提醒专用密码与签名密钥。
7. 重建 `ics-agent`，验证启动和提醒表。
8. 如使用反向代理，更新实际 ICS 站点的 `/reminder/` 转发。
9. 升级客服电脑客户端与浏览器插件，验证试验提醒及真实转人工。
10. 记录发布版本。第 12 节的恢复命令仅适用于有对应备份的发布。

选择业务低峰更新，等待已接收的机器人任务处理完成。当前 Webhook 队列保存在应用内存中，重启不能自动重放尚未处理完成的任务。

## 1. 在 Windows 发布正确的 `test` 分支

### 1.1 检查本机代码

以下命令在 **Windows PowerShell** 执行：

```powershell
Set-Location D:\project\CS
git branch --show-current
git status --short
git log -1 --oneline
git diff --stat
```

编写本文时，本机分支是 `yanbicheng`，最近功能提交是 `2ad8463`（“加入强提醒客户端插件”）。这些是编写时的记录，实际发布以你准备发布的最新提交为准。**不能在功能分支上只执行 `git push ... test` 就认为当前改动已发布**；必须先将功能合入本机 `test`。

如本机还有未提交改动，先检查并提交需要发布的文件。以下命令仅在确实有待提交改动时执行：

```powershell
git add -A
git diff --cached --stat
git diff --cached --name-only
git diff --cached --check
```

核对没有 `.env`、真实密码、Token、模型权重、运行数据库、临时文件或无关改动后：

```powershell
git commit -m "feat: publish reminder client and browser integration"
if ($LASTEXITCODE -ne 0) { throw '提交失败，停止发布。' }
```

### 1.2 将当前功能合入 `test`

确认工作区干净，再执行。代码块会记录你当前的分支，所以在功能分支上开始执行：

```powershell
$reminderRepoUrl = 'https://code-sayu.xingyongshe.com/backend/chatwoot.git'
$reminderSourceBranch = (git branch --show-current).Trim()
if (-not $reminderSourceBranch) { throw '当前是 detached HEAD，请先确认功能所在分支。' }
if (git status --porcelain) { throw '工作区还有未提交改动，先处理后再切分支。' }

git fetch $reminderRepoUrl test
if ($LASTEXITCODE -ne 0) { throw '读取远端 test 失败，停止发布。' }

git show-ref --verify --quiet refs/heads/test
if ($LASTEXITCODE -eq 0) {
    git switch test
} else {
    git switch -c test FETCH_HEAD
}
if ($LASTEXITCODE -ne 0) { throw '切换 test 失败，停止发布。' }

git merge --ff-only FETCH_HEAD
if ($LASTEXITCODE -ne 0) { throw '本机 test 与远端已分叉，请处理后再继续。' }

if ($reminderSourceBranch -ne 'test') {
    git merge --no-edit $reminderSourceBranch
    if ($LASTEXITCODE -ne 0) { throw '功能合并出现冲突，解决并提交后再继续。' }
}
git log -3 --oneline
git status --short
```

合并冲突时先解决冲突，再测试；不要强制推送覆盖远端 `test`。如果团队要求通过合并请求发布，可用合并请求将功能分支合入 `test`，随后取得该分支的最终提交 SHA，代替本节本地合并操作。

### 1.3 在最终发布代码上测试

```powershell
& 'C:\Users\lenovo\Anaconda3\envs\cs\python.exe' -m pytest reminder/tests -q
if ($LASTEXITCODE -ne 0) { throw '提醒模块测试失败，停止发布。' }
node --test reminder/extension/tests/core.test.mjs
if ($LASTEXITCODE -ne 0) { throw '浏览器插件测试失败，停止发布。' }
git diff --check
if ($LASTEXITCODE -ne 0) { throw '文件检查失败，停止发布。' }
```

本机需要已有测试依赖和 Node.js。桌面窗口测试默认跳过；要在 Windows 另行验证弹窗行为，可执行：

```powershell
$env:ICS_REMINDER_GUI_TESTS = '1'
try {
    & 'C:\Users\lenovo\Anaconda3\envs\cs\python.exe' -m pytest reminder/tests/test_client_desktop.py -q
    if ($LASTEXITCODE -ne 0) { throw '桌面窗口测试失败，停止发布。' }
} finally {
    Remove-Item Env:ICS_REMINDER_GUI_TESTS -ErrorAction SilentlyContinue
}
```

测试使用隔离数据与模拟浏览器，实际服务器和客服浏览器的验收见第 10 节。

### 1.4 推送并记录版本

```powershell
git push 'https://code-sayu.xingyongshe.com/backend/chatwoot.git' test
if ($LASTEXITCODE -ne 0) { throw '推送失败，服务器暂不能拉取本次版本。' }
$reminderReleaseCommit = (git rev-parse HEAD).Trim()
"本次服务器目标 SHA：$reminderReleaseCommit"
```

保存完整 SHA，服务器会用它核对版本。HTTPS 认证使用 Git 平台账号及具有相应权限的访问令牌，按现有凭据方式输入；不要把令牌写进仓库 URL。`ssh ics` 的登录配置与 Git 仓库认证是两个独立配置。

## 2. 登录服务器并检查现有运行环境

**Windows PowerShell：**

```powershell
ssh ics
```

以下第 2～8 节命令都在进入服务器后的 **Ubuntu Bash 终端** 执行。

```bash
cd /opt/cs
pwd
docker compose version
docker compose -f docker-compose.yml ps -a
docker compose -f docker-compose.yml port ics-agent 3001
curl -i --max-time 20 http://172.20.48.12:3001/health
df -h /opt/cs
free -h
git -C /opt/cs-src branch --show-current
git -C /opt/cs-src status --short
```

预期：ICS 和 PostgreSQL 正常运行，健康接口返回 HTTP 200，源码工作区没有未提交修改，源码分支是 `test`。先记录原有异常，不要把原有故障与本次发布混淆。

如果实际端口绑定是 `127.0.0.1:3001`，本机健康检查改用 `http://127.0.0.1:3001`。客服电脑需走既有反向代理或内网入口，不能直接访问服务器自己的 `127.0.0.1`。

如果 `/opt/cs-src` 不存在，请参考首次部署文档建立 Git checkout，保留已经运行的 `/opt/cs` 和全部数据；不要重新初始化现有部署。服务器分支不同或工作区有改动时，先确认并保留修改，不执行 `reset --hard`。

长时间构建可在已有 `tmux` 中执行：`tmux new -s reminder-update`；断线后重新 `ssh ics`，再 `tmux attach -t reminder-update`。后续始终显式使用 `-f docker-compose.yml`，避免自动加载开发热重载配置。

## 3. 可选：备份运行镜像、配置、源码和数据库

本次选择不备份时跳过整节，直接执行第 4、5 节。选择备份时在 Ubuntu 执行以下代码块，任一步失败都会停止本块：

```bash
(
  set -e
  umask 077
  cd /opt/cs
  mkdir -p /opt/cs/deployment-records
  chmod 700 /opt/cs/deployment-records
  reminder_stamp=$(date +%Y%m%d-%H%M%S)
  reminder_backup_dir="/opt/cs/deployment-records/reminder-$reminder_stamp"
  mkdir -m 700 "$reminder_backup_dir"

  git -C /opt/cs-src rev-parse HEAD > "$reminder_backup_dir/source-before.txt"
  cp -p .env "$reminder_backup_dir/env.before"
  cp -p docker-compose.yml "$reminder_backup_dir/compose.before.yml"

  reminder_container=$(docker compose -f docker-compose.yml ps -q ics-agent)
  test -n "$reminder_container"
  reminder_image=$(docker inspect "$reminder_container" --format '{{.Image}}')
  printf '%s\n' "$reminder_image" > "$reminder_backup_dir/image-before.txt"
  reminder_rollback_tag="ics-agent:reminder-rollback-$reminder_stamp"
  docker image tag "$reminder_image" "$reminder_rollback_tag"
  printf '%s\n' "$reminder_rollback_tag" > "$reminder_backup_dir/rollback-image-tag.txt"

  reminder_code_paths=(ics_agent knowledge_base_in deploy docs Dockerfile docker-compose.yml requirements.txt .dockerignore .env.example)
  if test -d reminder; then reminder_code_paths+=(reminder); fi
  tar --exclude='knowledge_base_in/output' --exclude='knowledge_base_in/vector_store' \
      --exclude='reminder/build' --exclude='reminder/dist' \
      --exclude='__pycache__' --exclude='*.pyc' \
      -czf "$reminder_backup_dir/runtime-code.before.tar.gz" "${reminder_code_paths[@]}"

  docker compose -f docker-compose.yml exec -T postgres \
    sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' \
    > "$reminder_backup_dir/ics-postgres.dump"
  test -s "$reminder_backup_dir/ics-postgres.dump"
  docker compose -f docker-compose.yml exec -T postgres pg_restore --list \
    < "$reminder_backup_dir/ics-postgres.dump" > "$reminder_backup_dir/ics-postgres-list.txt"

  printf '%s\n' "$reminder_backup_dir" > /opt/cs/deployment-records/last-reminder-release.txt
  printf '备份完成：%s\n' "$reminder_backup_dir"
)
```

预期：最后显示备份路径，数据库备份非空且能够列出内容。镜像备份标签来自**实际运行容器**的镜像，不会因为最近构建过其他版本而记录错镜像。

此处备份的是 ICS 的 PostgreSQL，不是已有 Chatwoot 的数据库。模型、FAQ 数据、向量库和 Chatwoot 附件沿用已有备份体系，本次不重新生成或覆盖。包含凭据的备份保留在受限目录；正常回滚用旧镜像和配置，不直接用数据库备份覆盖发布后新增的业务数据。

## 4. 拉取服务器 `test` 分支并核对 SHA

```bash
git -C /opt/cs-src status --short
git -C /opt/cs-src branch --show-current
```

确认工作区干净且分支是 `test`，再执行指定地址：

```bash
git -C /opt/cs-src pull --ff-only https://code-sayu.xingyongshe.com/backend/chatwoot.git test
git -C /opt/cs-src log -1 --oneline
```

拉取失败、出现分支分叉或认证问题时停止，不继续复制和构建。该命令不要求修改服务器的 `origin` 地址。

将第 1.4 节记录的完整 SHA 替换到下方：

```bash
(
  set -e
  reminder_expected_commit='替换为Windows记录的完整提交SHA'
  reminder_actual_commit=$(git -C /opt/cs-src rev-parse HEAD)
  test "$reminder_actual_commit" = "$reminder_expected_commit"
  test -f /opt/cs-src/reminder/backend/routes.py
  test -f /opt/cs-src/reminder/backend/auth.py
  test -f /opt/cs-src/reminder/client/bridge.py
  test -f /opt/cs-src/reminder/extension/manifest.json
  grep -q '^COPY reminder ./reminder$' /opt/cs-src/deploy/server/Dockerfile.backend-only
  printf '发布 SHA 与提醒源码检查通过：%s\n' "$reminder_actual_commit"
)
```

如果远端 `test` 已有更新提交，可以先重新核对并确定新的发布 SHA；不要忽略不一致直接上线。

## 5. 同步到实际运行目录，必须包含 `reminder`

第 4 节源码检查通过后执行；此代码块不读取备份目录或发布记录文件：

```bash
(
  set -e
  test -f /opt/cs-src/deploy/server/Dockerfile.backend-only
  test -d /opt/cs-src/reminder/backend
  mkdir -p /opt/cs/ics_agent /opt/cs/reminder /opt/cs/knowledge_base_in /opt/cs/deploy /opt/cs/docs

  rsync -a --delete --exclude='__pycache__/' --exclude='*.pyc' \
    /opt/cs-src/ics_agent/ /opt/cs/ics_agent/
  rsync -a --delete --exclude='/build/' --exclude='/dist/' \
    --exclude='__pycache__/' --exclude='*.pyc' \
    /opt/cs-src/reminder/ /opt/cs/reminder/
  rsync -a --delete --exclude='/output/' --exclude='/vector_store/' \
    --exclude='__pycache__/' --exclude='*.pyc' \
    /opt/cs-src/knowledge_base_in/ /opt/cs/knowledge_base_in/
  rsync -a /opt/cs-src/deploy/ /opt/cs/deploy/
  rsync -a /opt/cs-src/docs/ /opt/cs/docs/
  cp /opt/cs-src/docker-compose.yml /opt/cs-src/.dockerignore \
     /opt/cs-src/requirements.txt /opt/cs-src/.env.example /opt/cs/
  cp /opt/cs-src/deploy/server/Dockerfile.backend-only /opt/cs/Dockerfile

  cmp -s /opt/cs/reminder/backend/routes.py /opt/cs-src/reminder/backend/routes.py
  cmp -s /opt/cs/Dockerfile /opt/cs-src/deploy/server/Dockerfile.backend-only
  echo '运行目录同步通过，包含 reminder。'
)
```

这段同步保留 `/opt/cs/.env`、`models`、`database`、`log`、知识库生成文件、向量库和 PostgreSQL 数据卷。`--delete` 仅用于指定代码目录；不要将目标改成整个 `/opt/cs`，也不要加 `--delete-excluded`。

已有服务器可能为根 Compose 做过端口或资源调整。同步会更新根 `docker-compose.yml`，请核对服务器仍需使用的设置，将确有需要的服务器专用设置合并回去。端口等已支持环境变量的设置优先留在服务器 `.env`。

Linux 构建使用 `deploy/server/Dockerfile.backend-only`，无需 Windows GUI 依赖或独立客服前端文件。`reminder/build`、`reminder/dist` 是 Windows 打包产物，不作为服务器源码同步。

### 5.1 早期同步命令提示 `last-reminder-release.txt` 不存在

旧命令到末尾读取发布记录时才报错，前面的 `rsync`、`cp`、`cmp` 已完成，源码已覆盖。没有执行 build 或 recreate，因此无需为了这条报错重新同步。

本次不备份，直接跳过以下旧记录语句，不创建该文件：

```bash
reminder_backup_dir=$(cat /opt/cs/deployment-records/last-reminder-release.txt)
git -C /opt/cs-src rev-parse HEAD > "$reminder_backup_dir/source-pending.txt"
```

接着执行第 6 节配置、第 7 节构建和容器重建即可。今后同步使用上方不依赖备份记录的新版代码块。

## 6. 合并服务器 `.env` 配置

### 6.1 添加提醒参数

```bash
cd /opt/cs
nano .env
```

在原文件中增加缺失项，已有同名项直接修改原行，避免重复定义。以下是首次启用的配置结构，密码和密钥暂留空，下一节用工具写入：

```dotenv
REMINDER_ENABLED=true
REMINDER_POLL_SECONDS=2
REMINDER_REPEAT_SECONDS=30
REMINDER_PLUGIN_USERNAME=reminder
REMINDER_PLUGIN_PASSWORD=
REMINDER_PLUGIN_PASSWORD_HASH=
REMINDER_PLUGIN_SECRET=
REMINDER_PLUGIN_ALLOWED_AGENT_IDS=
REMINDER_PLUGIN_MAX_DEVICES_PER_AGENT=3
REMINDER_PLUGIN_SESSION_EXPIRE_SECONDS=3600
```

参数含义：

| 参数 | 含义与操作 |
| --- | --- |
| `REMINDER_ENABLED` | 设为 `true` 才启用提醒及启动建表 |
| `REMINDER_POLL_SECONDS` | 默认 2 秒轮询一次；可用范围 1～30 秒 |
| `REMINDER_REPEAT_SECONDS` | 未确认提醒默认每 30 秒重复提示；可用范围 5～300 秒 |
| `REMINDER_PLUGIN_USERNAME` | 客户端登录用的提醒专用账号，与 ICS 管理员账号不同 |
| `REMINDER_PLUGIN_PASSWORD` | 提醒专用密码，不使用 Chatwoot Token 或管理员密码 |
| `REMINDER_PLUGIN_PASSWORD_HASH` | 已有非空哈希时优先使用哈希；首次用工具设置密码时会清空此项 |
| `REMINDER_PLUGIN_SECRET` | 插件令牌签名密钥；首次生成，之后普通更新保持原值 |
| `REMINDER_PLUGIN_ALLOWED_AGENT_IDS` | 空值允许当前账户所有席位；可填写实际坐席 ID，例如 `11,12` |
| `REMINDER_PLUGIN_MAX_DEVICES_PER_AGENT` | 默认每席位最多 3 台有效插件客户端；第 4 台加入时吊销最旧设备 |
| `REMINDER_PLUGIN_SESSION_EXPIRE_SECONDS` | 首次登录临时令牌有效期，默认 3600 秒；不是设备每小时必须重新登录 |

如果服务器已启用提醒，**不要把已有密码、哈希和密钥清空**，也不要为普通更新重新生成。改账号、密码、密钥、授权席位或 Chatwoot 账户后，旧插件凭据可能失效，需要客服重新登录。

### 6.2 核对 Chatwoot 和转人工配置

沿用原服务器的有效 Token、账户及转人工配置，核对：

```dotenv
# ICS 服务端可访问的 Chatwoot 地址；已正常工作时保留现有值
CHATWOOT_BASE_URL=http://172.20.48.12:3000

# 客服浏览器访问地址；可以不同于上面的内部地址
CHATWOOT_PUBLIC_BASE_URL=http://172.20.48.12:3000

# 保留原有实际值，不要粘贴空值覆盖
# CHATWOOT_ACCOUNT_ID=实际账户ID
# CHATWOOT_API_TOKEN=原有用户级API Token
# CHATWOOT_CONTACT_API_TOKEN=原有专用用户Token，或留空复用上项
CHATWOOT_HANDOFF_ENABLED=true
```

读取席位和收件箱需要用户级 API Token，不能换成 AgentBot Token 或 Website Token；该 Token 应具有目标账户的会话和收件箱读取权限。原有 AgentBot 绑定、Webhook Secret、LLM 配置和模型路径继续沿用现有部署，本次无需重新创建 Chatwoot 机器人或重建前端。

**这三处必须一致：** `CHATWOOT_PUBLIC_BASE_URL`、浏览器插件保存的 Chatwoot 地址、客服浏览器实际登录页面的站点地址。协议、主机、端口和部署路径都要一致，配置只填写站点基地址，不加 `/app/accounts/...`。

例如客服实际使用 `http://172.20.48.12:3000/app/accounts/1/dashboard`，上述公开地址和插件设置均填 `http://172.20.48.12:3000`。如果实际使用域名，则两处均填该域名。不要把开发环境的 `http://localhost:3000` 复制到客服部署：客服电脑的 `localhost` 指向客服电脑自己。

### 6.3 核对 ICS 对客服电脑的访问入口

客户端的“ICS 地址”与 Chatwoot 地址不同：

| 客服电脑中的位置 | 当前内网示例 |
| --- | --- |
| 桌面提醒客户端登录页的 ICS 地址 | `http://172.20.48.12:3001` |
| 浏览器插件设置页的 Chatwoot 地址 | `http://172.20.48.12:3000` |
| 浏览器 Chatwoot 工作台页面 | `http://172.20.48.12:3000/app/accounts/1/dashboard`，账户 ID 按实际值 |

如果现有 ICS 是私网直连模式，服务器 `.env` 的 `ADMIN_WEB_HOST` 应为服务器实际私网 IP，例如 `172.20.48.12`，端口为 `3001`，并确保客服网段可访问。若现有 ICS 通过 HTTPS 反向代理访问，保留原端口绑定，客户端填写已有 ICS 域名，第 8 节补齐代理转发。

保存后：

```bash
chmod 600 /opt/cs/.env
cd /opt/cs
docker compose -f docker-compose.yml config --quiet
```

预期无报错。`.env.example` 和服务器示例模板不会自动修改实际 `.env`；不要用模板整文件覆盖现有密码和运行参数。

## 7. 构建镜像、配置专用登录并更新应用

### 7.1 先构建新镜像

```bash
cd /opt/cs
docker compose --progress plain -f docker-compose.yml build ics-agent
```

看到构建成功再继续。构建失败时旧容器通常仍在运行，先查明错误；不要执行后续替换命令。服务器只安装根 `requirements.txt` 中的后端依赖，不需要 Anaconda、`requirements-client.txt` 或 `requirements-build.txt`。

### 7.2 首次启用时设置提醒密码与密钥

如果原服务器已有有效提醒密码和密钥，本步骤跳过。首次启用时，执行下面的交互式工具，输入两次你准备分发给客服的提醒专用密码：

```bash
cd /opt/cs
docker compose -f docker-compose.yml run --rm --no-deps \
  --user "$(id -u):$(id -g)" --volume /opt/cs:/app --entrypoint python \
  ics-agent -m reminder.backend.configure_login
chmod 600 /opt/cs/.env
```

预期显示“插件登录配置已保存到项目 .env”。密码输入不回显，工具会在缺少签名密钥时生成随机密钥；账号缺失时使用 `reminder`。这次目录挂载使保存位置实际落在宿主机 `/opt/cs/.env`；`--user` 使用当前部署用户，避免工具原子替换后文件归 root、当前用户无法读取。

不要直接对运行容器 `exec ... configure_login`：Compose 的 `env_file` 只注入环境变量，并未把宿主 `.env` 挂到容器里。也不要将上述目录挂载换成单文件 `.env` 挂载，配置工具需要原子替换文件，单文件挂载可能无法替换。此一次性工具结束后，正常应用仍使用 Compose 原来的挂载配置。

如希望仅补齐缺失的随机密码和密钥，可在同一命令末尾加 `--generate`；生成密码在服务器 `.env` 中保存，不在终端输出，需在私有配置文件中取得。该工具不会自动设置 `REMINDER_ENABLED=true`，第 6 节仍需完成。

### 7.3 重建应用容器

确认 PostgreSQL 已正常运行，然后：

```bash
cd /opt/cs
docker compose -f docker-compose.yml config --quiet
docker compose -f docker-compose.yml up -d --no-deps --force-recreate ics-agent
docker compose -f docker-compose.yml ps -a
```

`--no-deps` 使本次只更新应用，前提是第 2 节已确认 PostgreSQL 正常。本次不启动第二个提醒服务，也不新增提醒服务器端口。修改环境变量后必须 recreate，单纯 `restart` 不会重新注入 `.env`。

### 7.4 检查当前启动日志和健康状态

```bash
docker compose -f docker-compose.yml logs --tail 100 ics-agent
tail -n 160 /opt/cs/log/uvicorn-admin.stderr.log
curl -i --max-time 20 http://172.20.48.12:3001/health
```

模型预热可能需要一段时间，容器显示 Up 不等于应用已就绪。预期当前这次启动日志包含 `event=应用.reminder.就绪`（源码事件为 `app.reminder.ready`，输出时会翻译），随后出现 `Application startup complete`，健康检查 HTTP 200。不要只凭历史日志里的成功行判断这次发布成功。

`REMINDER_ENABLED=true` 时，应用启动会自动创建/升级 `reminder_devices`、`reminder_notifications`，增加设备认证和绑定版本字段，并将旧的单设备唯一索引替换为普通索引。`1.0.3` 还会自动增加 `reminder_notifications.inbox_id`、`inbox_name`，保留现有提醒，不需要手工执行建表或改表 SQL。数据库用户需有相应建表、改表和索引权限；提醒迁移失败会阻止应用启动，应查看这次启动异常；未备份的发布先修复权限或迁移错误再重建，有对应备份时可按第 12 节恢复。

新提醒优先保存 Chatwoot Webhook 中的收件箱 ID 和名称；已有待确认提醒会随会话状态同步回填，缺少名称时通过 Chatwoot 收件箱接口查询。名称查询失败不会影响提醒接收、打开工作台或确认收到；试验提醒没有真实收件箱，不能用它验证收件箱显示。

### 7.5 核对实际容器配置和数据库结构

仅打印非敏感配置与凭据是否存在：

```bash
docker compose -f docker-compose.yml exec -T ics-agent python - <<'PY'
from ics_agent.app.core.config import get_settings
from reminder.protocol import workbench_url
s = get_settings()
print('reminder_enabled =', s.reminder_enabled)
print('poll_seconds =', s.reminder_poll_seconds)
print('repeat_seconds =', s.reminder_repeat_seconds)
print('plugin_username =', s.reminder_plugin_username)
print('plugin_password_configured =', bool(s.reminder_plugin_password or s.reminder_plugin_password_hash))
print('plugin_secret_configured =', bool(s.reminder_plugin_secret))
print('allowed_agent_ids =', s.reminder_plugin_allowed_agent_ids or '(当前账户全部坐席)')
print('max_devices_per_agent =', s.reminder_plugin_max_devices_per_agent)
print('chatwoot_public_base_url =', s.chatwoot_public_base_url)
print('chatwoot_account_id =', s.chatwoot_account_id)
print('test_workbench_url =', workbench_url(s.chatwoot_public_base_url, s.chatwoot_account_id))
assert s.reminder_enabled
assert s.reminder_plugin_password or s.reminder_plugin_password_hash
assert s.reminder_plugin_secret
assert s.chatwoot_account_id.strip() and s.chatwoot_public_base_url.strip()
PY
```

预期启用和凭据存在均为 `True`，工作台 URL 为客服能访问的真实地址。然后检查表和新增字段：

```bash
docker compose -f docker-compose.yml exec -T postgres \
  sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1' <<'SQL'
SELECT to_regclass('public.reminder_devices') AS devices_table,
       to_regclass('public.reminder_notifications') AS notifications_table;
SELECT column_name FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = 'reminder_devices'
  AND column_name IN ('auth_kind', 'auth_version', 'binding_version')
ORDER BY column_name;
SELECT column_name FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = 'reminder_notifications'
  AND column_name IN ('inbox_id', 'inbox_name')
ORDER BY column_name;
SQL
```

默认 PostgreSQL schema 下预期返回两张表名、三个设备字段和两个收件箱字段。若你明确采用了自定义 schema，请把查询里的 `public` 替换为实际 schema；不要因为查询为空就删除数据库重建。

检查未携带设备凭据的接口：

```bash
curl -i --max-time 20 http://172.20.48.12:3001/reminder/notifications
```

启用后预期 **HTTP 401**，表示提醒路由可访问且需要设备认证。503 且提示“提醒功能未启用”表示开关未生效；404 表示代码或访问入口仍旧；502/503 的其他原因需要看响应说明及应用日志。

## 8. 如有反向代理，更新实际 ICS 站点

私网直接访问 `http://172.20.48.12:3001`、没有经过代理时，可跳过本节，直接检查客服电脑网络连通。

如果客服使用 ICS 域名，旧版 Nginx 可能只放行 `/health` 和 `/webhooks/chatwoot`。拉取 `deploy/server/nginx-bot.conf.example` 不会更新 `/etc/nginx/` 的实际站点文件，需要手工补齐。

1. 找到实际代理 ICS 的站点配置，确认是 ICS 站点。
2. 编辑实际 ICS 站点配置文件。
3. 在该站点的 `server` 块内新增以下段落；已存在时核对，无需重复添加。

```nginx
location /reminder/ {
    limit_except GET POST { deny all; }
    proxy_pass http://127.0.0.1:3001;
}
```

这里的上游必须与实际 ICS 监听地址一致：如果第 2 节显示端口仅绑定 `172.20.48.12:3001`，改为 `proxy_pass http://172.20.48.12:3001;`。`proxy_pass` 末尾不加 `/`，保留请求的 `/reminder/` 路径。不要覆盖已有 Chatwoot 站点，也不用为客服开放 ICS `/admin`、`/auth` 或 `/rag/query`。

编辑示例，先将路径替换为实际文件，再执行：

```bash
(
  set -e
  reminder_nginx_site='/etc/nginx/sites-available/替换为实际ICS站点文件'
  test -f "$reminder_nginx_site"
  sudo nano "$reminder_nginx_site"
)
```

检查通过后才重载：

```bash
sudo nginx -t && sudo systemctl reload nginx
```

从客服电脑验证 ICS 域名下的 `/health` 返回 200，`/reminder/notifications` 未认证返回 401。失败时先对照代理路径和上游地址；应用地址成功不代表客户端所用域名也成功。

浏览器 Native Messaging 连接只发生在客服电脑本机，服务器不用开放额外的浏览器连接端口。

## 9. 构建并分发 Windows 客户端 `1.0.3`

### 9.1 从同一发布代码生成安装包

回到 **Windows PowerShell**，在已发布的 `test` 代码上执行：

```powershell
Set-Location D:\project\CS
git rev-parse HEAD
& 'C:\Users\lenovo\Anaconda3\envs\cs\python.exe' reminder\packaging\build.py --installer --output-dir reminder\dist\browser-plugin
if ($LASTEXITCODE -ne 0) { throw '客户端构建失败，不分发旧安装包。' }
Get-Item reminder\dist\browser-plugin\ICSReminderSetup.exe | Select-Object Name, Length, LastWriteTime
Get-FileHash reminder\dist\browser-plugin\ICSReminderSetup.exe -Algorithm SHA256
```

如缺构建依赖，先使用同一个 `cs` 环境安装 `reminder\requirements-build.txt`。安装版本和扩展版本在本次代码中为 `1.0.3`；继续发布新版时同步更新版本号。

生成文件：`D:\project\CS\reminder\dist\browser-plugin\ICSReminderSetup.exe`。分发这一个安装包即可，内置客户端、本机连接组件、浏览器插件和安装说明。`dist` 被 Git 忽略，服务器 `git pull` 不会得到安装包；Ubuntu 服务器也不运行或构建 Windows exe。

保留本次安装包 SHA256、对应 Git SHA 和旧版安装包。可以直接将安装包交付给客服；若需要服务器暂存分发，使用已配置的 SSH 别名：

```powershell
ssh ics 'mkdir -p ~/reminder-release/1.0.3'
if ($LASTEXITCODE -ne 0) { throw '创建服务器暂存目录失败。' }
scp -o ServerAliveInterval=30 D:\project\CS\reminder\dist\browser-plugin\ICSReminderSetup.exe ics:~/reminder-release/1.0.3/
if ($LASTEXITCODE -ne 0) { throw '安装包上传失败。' }
ssh ics 'sha256sum ~/reminder-release/1.0.3/ICSReminderSetup.exe'
```

两端 SHA256 必须一致。上传只用于保存安装包，不会自动在客服电脑安装，也不会自动开设下载网站。

### 9.2 每台客服电脑升级

1. 在 Windows 系统托盘右键“ICS 客服提醒”，选择“退出程序”。仅关闭主窗口会隐藏到托盘，旧程序仍在运行。
2. 退出加载本插件的 Chrome/Edge，结束旧本机连接组件。
3. 运行新版 `ICSReminderSetup.exe`。已有安装时沿用原安装目录升级；你当前的目录为 `D:\software\reminder`。首次安装可自主选择有写入权限的目录。
4. 安装后从开始菜单启动“ICS 客服提醒”。设备登录和席位设置保存在 `%APPDATA%\ICSReminder`，正常升级会保留；服务器首次启用、设备未记住或凭据已变化时重新登录。
5. 打开实际使用的 Chrome/Edge 配置文件，进入 `chrome://extensions` 或 `edge://extensions`，核对“ICS Chatwoot 页面连接”的插件路径和版本 `1.0.3`，点击“重新加载”，或确认重启浏览器后已加载新文件。
6. 首次使用插件时开启“开发者模式”，选择“加载已解压的扩展程序”，选择实际安装目录下的 `extension` 文件夹，例如 `D:\software\reminder\extension`。
7. 打开插件设置，Chatwoot 地址填写第 6 节确认的公开地址，例如 `http://172.20.48.12:3000`；点击“保存地址并授权此站点”，完成授权。
8. 在这个浏览器配置文件里登录 Chatwoot，确认可访问目标账户。客户端应显示“浏览器插件已连接”及配置文件数量。
9. 桌面客户端的 ICS 地址填写实际 ICS 入口，例如 `http://172.20.48.12:3001`，使用提醒专用账号及密码登录，选择当前值班席位。

如果要更换已有安装目录，先卸载再重装，随后在扩展管理页移除旧路径插件并加载新目录下的 `extension`；不要直接移动已安装文件夹。插件目前尚未上架扩展商店，首次加载和站点授权仍需在浏览器手工完成。

## 10. 逐项验收

### 10.1 在客服电脑验证服务器入口

**Windows PowerShell**，按实际 ICS 入口替换示例地址：

```powershell
curl.exe -i --max-time 20 http://172.20.48.12:3001/health
curl.exe -i --max-time 20 http://172.20.48.12:3001/reminder/notifications
```

预期分别为 HTTP 200 和 401。服务器本机能访问、客服电脑不能访问时，先处理内网路由、入口绑定、现有防火墙或代理配置，再测客户端。

### 10.2 试验提醒：不涉及客户会话

1. 客户端登录、绑定席位后点击“测试提醒”。确认收到置顶弹窗和提示音。
2. 按钮应统一显示“打开 Chatwoot 工作台”，会话标签较大、摘要较小；窗口可拖动调整大小。试验提醒没有真实收件箱，不应显示虚构的收件箱名称。
3. 先在浏览器登录目标 Chatwoot 账户，将浏览器最小化，再点击“打开 Chatwoot 工作台”。
4. 预期复用匹配的已登录标签页、恢复浏览器窗口并进入该账户工作台；提醒弹窗继续显示，未被自动确认。
5. 点击弹窗右上角关闭时，未确认提醒仍保留，并提示逐条确认。
6. 明确点击“确认收到”后，这条试验提醒才结束；没有其他提醒时弹窗收起。

`--demo` 只演示本机弹窗和声音，不提供服务端工作台链接，不能代替上述测试。

### 10.3 多条提醒：必须全部确认才收起

1. 在客户端连续发送两次“测试提醒”，等两条都出现在列表。
2. 选择第一条并打开工作台，确认列表仍有两条待确认提醒。
3. 点击第一条的“确认收到”，确认弹窗保留并显示剩余一条。
4. 对剩余提醒点击“确认收到”，确认弹窗收起。
5. 等待后续轮询，确认已确认的试验提醒不会重新出现。

试验提醒会在创建 15 分钟后自动失效，上述测试应在有效期内完成。

### 10.4 真实转人工及原有机器人回归

使用专门测试会话，先确认客户端当前席位与 Chatwoot 的目标坐席一致：

1. 从已有客服前端发送普通问题，确认原有机器人仍能正常回复。
2. 在测试会话触发“转人工”，确认会话成功分配并打开，目标坐席客户端收到提醒。会话标签应显示会话序号、客户姓名和正确的收件箱名称，例如“会话 56 · 客户甲 · 官网咨询”；选中后详情区显示完整名称。使用多个收件箱时分别创建测试会话核对，使用长名称时确认详情可完整查看。
3. 点击“打开 Chatwoot 工作台”，实际进入这条客户会话，页面复用且提醒弹窗继续保留。
4. 点击“确认收到”，确认该条提醒结束。
5. 再产生一条未确认测试提醒，在 Chatwoot 解决会话，确认提醒撤销。
6. 测试改派：原坐席的未确认提醒撤销，新坐席客户端收到对应提醒。
7. 测试共享电脑切换席位，确认只显示当前席位的提醒；在浏览器操作尚未完成时切换席位，旧操作结果不能弹回旧席位提醒。
8. 重启客户端后重新确认插件连接、设备记忆和待确认提醒恢复。服务器升级前已有的待确认提醒应在会话同步后补齐收件箱信息，名称与 Chatwoot 会话所属收件箱一致。

如日常使用多个浏览器或多个配置文件，再分别检查每个配置文件的插件加载、站点授权与 Chatwoot 登录。登录过期或插件断开时，应显示明确错误、保留未确认提醒，恢复连接后可以重试。真实验收结果与本地自动化测试结果分别记录。

## 11. 记录成功发布

全部验收通过后，可在 Ubuntu 保存本次版本信息。版本记录不包含配置、数据库或镜像备份，也不依赖 `last-reminder-release.txt`：

```bash
(
  set -e
  umask 077
  cd /opt/cs
  reminder_record_dir="/opt/cs/deployment-records/reminder-release-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$reminder_record_dir"
  git -C /opt/cs-src rev-parse HEAD > "$reminder_record_dir/source-released.txt"
  reminder_container=$(docker compose -f docker-compose.yml ps -q ics-agent)
  test -n "$reminder_container"
  docker inspect "$reminder_container" --format '{{.Image}}' > "$reminder_record_dir/image-released.txt"
  date -Is > "$reminder_record_dir/accepted-at.txt"
  printf '本次版本记录已保存：%s\n' "$reminder_record_dir"
)
```

另记客户端安装包版本、SHA256、已升级客服电脑及插件版本。

## 12. 失败时恢复旧服务

本节的镜像、配置恢复命令仅适用于事先生成了对应备份的发布。本次未备份时，不执行读取 `last-reminder-release.txt` 或恢复备份文件的命令。

### 12.1 构建失败且尚未重建应用

检查旧容器 `/health`。旧容器仍正常时，停止后续步骤，修复源码、配置或构建问题后重新发布。`build` 本身不会自动替换正在运行的容器。

### 12.2 已替换容器，恢复旧镜像和配置

在 Ubuntu 执行。先确认备份目录是本次发布前生成的目录：

```bash
cat /opt/cs/deployment-records/last-reminder-release.txt
```

第 3 节完整备份中应有 `compose.before.yml`、`env.before` 和旧镜像标签；有这些对应文件时再执行下方恢复代码块。

然后：

```bash
(
  set -e
  cd /opt/cs
  reminder_backup_dir=$(cat /opt/cs/deployment-records/last-reminder-release.txt)
  test -f "$reminder_backup_dir/env.before"
  test -f "$reminder_backup_dir/compose.before.yml"
  reminder_rollback_tag=$(cat "$reminder_backup_dir/rollback-image-tag.txt")
  docker image inspect "$reminder_rollback_tag" > /dev/null

  cp -p "$reminder_backup_dir/env.before" /opt/cs/.env
  chmod 600 /opt/cs/.env
  cp -p "$reminder_backup_dir/compose.before.yml" /opt/cs/docker-compose.yml
  docker image tag "$reminder_rollback_tag" ics-agent:py310
  docker compose -f docker-compose.yml config --quiet
  docker compose -f docker-compose.yml up -d --no-deps --force-recreate ics-agent
)
```

恢复时不加 `--build`，否则会重新构建尚未回退的源码。重新检查健康接口、启动日志和测试会话。

如果回退到旧提醒版本后因设备索引或多设备数据导致启动失败，可先在 `.env` 设置 `REMINDER_ENABLED=false`，再 recreate 恢复原有机器人主服务，保留提醒表和数据，之后在测试环境评估兼容方案。不要通过删设备、删提醒表或删数据库卷解决迁移问题。

### 12.3 恢复本次修改过的 Nginx

仅在第 8 节有实际代理修改及对应备份时执行：

```bash
(
  set -e
  reminder_backup_dir=$(cat /opt/cs/deployment-records/last-reminder-release.txt)
  reminder_nginx_site=$(cat "$reminder_backup_dir/nginx-site-path.txt")
  sudo cp -p "$reminder_backup_dir/nginx-site.before" "$reminder_nginx_site"
  sudo nginx -t
  sudo systemctl reload nginx
)
```

### 12.4 源码与客户端后续处理

镜像回退不会回退 `/opt/cs-src` 的 Git 分支或 `/opt/cs` 的构建源码。保留发布记录并明确当前“源码版本”与“运行镜像版本”的差异；下一次构建前先处理远端 `test` 的回退或修复提交，不要直接再次构建失败版本。需要恢复运行目录文件时，可从第 3 节源码归档恢复指定代码目录，确认不覆盖运行数据。

服务端回滚也不会自动降级客服电脑。必要时使用发布前保留的旧安装包，从托盘退出新版、关闭浏览器后安装兼容客户端；服务端关闭提醒期间先按原 Chatwoot 工作流程处理客户。

普通代码回滚不恢复 PostgreSQL 备份，避免丢失更新后产生的客户数据。仅在明确的数据损坏恢复方案中使用数据库备份，先保留当前数据并按已有恢复文档操作。本次更新不执行 `docker compose down -v`、删除数据库目录或整目录带删除同步。

## 13. 常见故障对照

| 现象 | 检查与处理 |
| --- | --- |
| 同步时提示 `last-reminder-release.txt` 不存在 | 早期同步块仅在记录阶段失败，前面的源码已同步；按第 5.1 节直接继续配置与构建，新同步块不依赖备份文件 |
| 拉取后还是旧功能 | 核对远端 `test` 是否包含功能；Windows/服务器 SHA 是否一致；是否同步 `reminder`、build 并 recreate |
| 构建报 `COPY reminder` 找不到 | 检查 `/opt/cs/reminder`；重新执行第 5 节完整同步 |
| 改 `.env` 没生效 | 检查宿主 `/opt/cs/.env`；执行 recreate，使用第 7.5 节核对实际配置 |
| 配置工具提示文件替换失败 | 按第 7.2 节使用目录挂载，确认当前部署用户可写 `/opt/cs/.env`；不要使用单文件 bind 挂载 |
| 提醒接口返回 404 | 核对新镜像、接口路径；若直连成功而域名失败，补齐实际代理 `/reminder/` |
| 提醒接口返回 503 | 阅读响应；检查 `REMINDER_ENABLED`、插件密码/密钥、账户 ID 或 Chatwoot 可用性 |
| 客户端读不到席位 | 检查用户级 API Token、账户 ID、授权席位列表及 ICS 到 Chatwoot 的网络 |
| 客户端登录后又要求登录 | 核对是否改过密码、密钥、授权范围或账户；超过设备上限时最旧设备会被吊销 |
| 打开按钮仍是“打开 Chatwoot 会话” | 安装包或运行进程仍旧；退出托盘旧程序，用新版包升级并从开始菜单启动 |
| 打开工作台仍关闭弹窗 | 同样核对客服电脑是否真正运行 `1.0.3`，仅更新服务端不改变旧 exe |
| 会话提示没有收件箱名称 | 先使用真实会话提醒；核对服务端代码及客户端 `1.0.3` 均已更新，启动自动新增 `inbox_id`、`inbox_name`；已有提醒等待同步，仍缺名称时检查 Chatwoot 连接及用户级 Token 的收件箱读取权限；名称查询失败不影响处理提醒 |
| 插件显示未连接 | 启动桌面客户端；确认实际浏览器配置文件加载安装目录的插件，重新加载或重启浏览器 |
| 插件已连接但打不开页面 | 三处 Chatwoot 地址必须一致，完成站点授权并在该配置文件登录目标账户；不要混用 localhost、IP 和域名 |
| 客服电脑访问 ICS 失败 | 从客服电脑测 `/health`；核对私网/VPN、端口绑定、已有防火墙与代理入口 |
| 没有真实转人工提醒 | 确认已启用、会话分配成功、目标席位匹配；试验提醒成功仅证明接收链路，不证明转人工链路 |

## 14. 完成检查表

- [ ] 功能已经进入指定仓库的 `test` 分支，Windows 和服务器目标 SHA 一致。
- [ ] 本次更新方式已确认；不备份时跳过备份及基于备份的恢复步骤。
- [ ] `/opt/cs/reminder` 已同步，使用后端 Dockerfile 构建成功。
- [ ] 服务器提醒开关、账号/密码/密钥、授权席位与 Chatwoot 公开地址已核对。
- [ ] 新应用当前启动成功，健康接口 200，两张提醒表、新增设备字段及 `inbox_id`、`inbox_name` 存在。
- [ ] 客服实际入口的提醒接口未认证返回 401，必要的代理转发已生效。
- [ ] 客服电脑已安装新版，实际浏览器插件已加载、授权、连接并登录 Chatwoot。
- [ ] 打开工作台保留弹窗，多条提醒全部逐条确认后才收起。
- [ ] 真实提醒显示正确收件箱名称，长名称详情可完整查看，已有待确认提醒同步后补齐；试验提醒没有虚构收件箱。
- [ ] 普通机器人回复、真实转人工、解决/改派与席位切换验收通过。
- [ ] 安装包哈希、发布版本、升级电脑和验收时间已记录，旧版本仍可回退。
