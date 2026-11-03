# 对话归档与客服状态服务器更新手册

本文用于已有 ICS 服务器增加或更新“对话归档”和“客服状态”。沿用 [服务器同步更新操作手册](server-sync-update.md) 的 Windows 开发端 → Ubuntu 服务器流程，补齐独立归档库、数据库迁移、账户 Webhook、接收器、解析进程、后台页面及状态保留期配置。

**按第一节至第十一节顺序执行；第二节和第四节的源码来源选择对应的 A 或 B，不需要执行两套。** 第3.2节为备份流程；本次已明确决定跳过备份时，改执行3.3，不把失败的备份写成成功。已有归档数据库与首次增加归档数据库使用同一迁移入口，但初始化处理不同，见第八节。日后更新可使用第十二节清单；异常与回退见第十三、十四节。

本手册提供发布命令和验收标准，不代表已经在目标服务器执行或通过验收。发布会暂停 ICS、归档接收和解析进程；按本次确认的范围，**接受维护期间漏收，不进行历史补录，也不宣称恢复后能补齐维护窗口。** 不清空数据库、不删除 Docker 数据卷、不覆盖原服务器 `.env`、模型或渠道资料。

### 本次首次上线的执行路线

本次按“生产环境原有ICS正常运行，尚未部署quality服务、角色或归档数据库；Chatwoot为4.18.0”准备。**首次上线要从空归档卷初始化，并执行完整001–009，不能只执行009。** 本地库已迁移不代表服务器库已迁移；不上传本地数据库或本地 `.env`。

| 顺序 | 在哪里操作 | 执行内容 | 继续条件 |
| --- | --- | --- | --- |
| 1 | Windows PowerShell | 第二节：测试；A打包上传或B推送Git，选一种 | 测试无失败；记下包SHA256或提交SHA |
| 2 | 服务器Bash | 3.1：核对工具、实际地址、现有服务和Chatwoot能力 | 原业务健康，参数与实际部署一致 |
| 3 | 服务器Bash | 3.2备份；本次决定跳过备份则执行3.3，确认ICS停止并记录主库标识 | 3.2输出“备份完成”，或3.3输出对应PASS；跳过不等于备份完成 |
| 4 | 服务器Bash | 第四节执行相同的A或B；第五节同步代码 | 007文件存在；原配置、渠道、模型仍在 |
| 5 | 服务器编辑器、Bash | 第六节补配置；第七节新建专用账户Webhook并填回Secret | 配置验证通过；URL、账号、签名独立于AgentBot |
| 6 | 服务器Bash | 第八节启动独立归档库、初始化、迁移和权限验证 | 001–009齐全；`hidden_at`字段和受限权限正确 |
| 7 | 服务器Bash | 第九节构建镜像，启动接收器、ICS和worker | 接收器健康；ICS预热后健康；三个进程同版 |
| 8 | 服务器、浏览器 | 第十、十一节验证消息、历史、导出、状态和原业务 | 真实事件完成解析；状态采样时间推进 |

每个代码块整段复制；`(...)`中的变量只在该代码块有效。每段运行后立即检查结果，有错误就停在该节。服务器参数文件由3.1创建；重连SSH后按该节重新加载。标为“已有归档库”“后续更新”“可选”的分支，首次上线不必全部执行。

## 一、部署结构与本次边界

### 1.1 目录与服务约定

| 项目 | 约定 |
| --- | --- |
| Windows 开发目录 | `D:\project\CS` |
| Windows Python 环境 | `C:\Users\lenovo\Anaconda3\envs\cs` |
| Windows SSH 别名 | `ics` |
| Git 发布分支 | `test`；发布前先按现有流程整理好该分支 |
| Git 源码目录 | `/opt/cs-src` |
| 实际运行目录 | `/opt/cs` |
| 实际服务器配置 | `/opt/cs/.env`，始终保留并人工补充 |
| Docker Compose 项目 | `cs`，沿用现有主 Compose 的项目名 |
| 共享后端镜像 | `ics-agent:py310` |
| 原 ICS / 管理后台 | `ics-agent`，示例 `http://172.20.48.12:3001` |
| 独立归档数据库 | `archive-postgres`，库名默认 `ics_quality` |
| 原业务数据库 | `postgres`，库名默认 `cs`，本次不迁移到归档库 |
| 归档接收器 | `quality-receiver`，示例 `http://172.20.48.12:3010` |
| 归档解析进程 | `quality-worker`，无对外 HTTP 端口 |
| Chatwoot | 示例 `http://172.20.48.12:3000`，沿用现有独立部署 |

`172.20.48.12` 和账号 `1` 都是现有文档的示例，执行前替换为服务器和 Chatwoot 的实际值。Ubuntu 服务器继续通过 Docker 运行，不需要安装 Windows 的 Anaconda 环境。

### 1.2 数据流与隔离

1. Chatwoot **新增一个专用账户级 Webhook**，向 3010 端口投递五类事件。
2. 接收器校验专用签名，将原始请求字节和持久化解析任务写入独立归档数据库；事务提交后才确认接收。
3. `quality-worker` 处理已接收任务，追加消息、会话和评价观察记录，维护可重建的查询索引。
4. 现有 ICS FastAPI 和 `/admin` 登录提供查询、导出和客服状态页面；各归档功能使用独立连接池与受限账号。
5. ICS 的坐席状态采集后台每 60 秒观察一次 Chatwoot 当前账号坐席，独立保存状态历史；按第十节的规则清理过期状态记录。

原 ICS AgentBot 继续使用原来的 3001 Webhook、绑定关系和签名配置。归档签名 `ARCHIVE_WEBHOOK_SECRET` 与原 `CHATWOOT_AGENT_BOT_SECRET` 等业务配置独立，不能互相替换。

| 使用方 | 主要归档身份 | 范围 |
| --- | --- | --- |
| 建库、迁移、备份 | `archive_admin` / 对象所有者 `archive_owner` | 运维操作；不提供给机器人 |
| 接收器、解析进程 | `archive_ingest` | 原始聊天证据追加、解析任务和派生索引 |
| ICS 查询和导出 | `archive_reader` | 读取归档记录；查询和导出连接独立 |
| ICS 分配记录同步 | `archive_assignment` | 追加可证明 ICS 成功分配的事件 |
| ICS 状态采集与受限清理 | `archive_presence` | 状态样本及指定清理函数；不能读写原始聊天证据 |
| 预留质检结果账号 | `archive_quality` | 本版不执行模型评分 |

聊天原始证据不设本次自动清理。45 天保留期只针对坐席状态样本及过期、已关闭的监管区间。附件保存元数据和来源链接，文件本身尚未独立复制；Excel 导出不能恢复从未接收到的消息版本。

### 1.3 本次需要发布的文件

共享同步脚本已覆盖 `ics_agent/`、`quality/`、`knowledge_base_in/`、`reminder/`、`deploy/`、`docs/`，以及两份服务器 Compose、依赖文件和后端 Dockerfile。本次不能只复制 `ics_agent/`；镜像和三个应用进程必须包含同版 `quality/`。

同步保留服务器 `.env`、`database/`、`models/`、`log/`、知识库输出和向量库。`database/channels.json` 继续使用服务器现有映射，本次不使用本地文件覆盖。原主库卷默认 `cs_postgres_data`；新归档库卷默认 `cs_archive_postgres_data`。已有部署若使用外部卷或自定义项目名，先核对并保留原设置，不能顺手切换到新空卷。

## 二、在 Windows 准备发布版本（二选一）

### 2.1 先检查和测试

在 **Windows PowerShell** 执行：

```powershell
Set-Location D:\project\CS
git branch --show-current
git status --short
git diff --stat
git diff --check
if ($LASTEXITCODE -ne 0) { throw '差异检查失败，停止发布。' }

$archiveTestStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
& 'C:\Users\lenovo\Anaconda3\envs\cs\python.exe' -m pytest tests quality/tests -q -p no:cacheprovider --basetemp "tmp/pytest-archive-release-$archiveTestStamp"
if ($LASTEXITCODE -ne 0) { throw 'ICS 或归档模块测试失败，停止发布。' }

Get-ChildItem quality\database\migrations\*.sql | Select-Object Name
```

数据库集成测试有专用测试库和显式开关要求；未启用时出现 `skipped`，不能计为数据库联调通过。不要把测试库配置改成生产库后运行测试。应确认发布文件包含 `001_capture.sql` 至 `010_round_replay_windows.sql`，并记录这次实际执行的测试结果。

Windows 到 Linux 发布还要检查 Shell 脚本换行。当前 `.gitattributes` 没有统一 Shell/SQL 的 LF 规则，显式用 `bash` 调用也不能修复脚本正文中的 CRLF。下面只检查，不改写文件：

```powershell
& 'C:\Users\lenovo\Anaconda3\envs\cs\python.exe' -c "from pathlib import Path; paths=[*Path('deploy/server').glob('*.sh'),*Path('deploy/archive/init').glob('*.sh'),*Path('quality').rglob('*.sh')]; bad=[str(p) for p in paths if b'\r' in p.read_bytes()]; print('Shell CRLF files:',bad); raise SystemExit(bool(bad))"
if ($LASTEXITCODE -ne 0) { throw '发布 Shell 脚本存在 CRLF，请仅核对这些 .sh 文件的 LF 保存格式。' }
```

如需修正，先在开发端只处理对应Shell文件并重新验证。**不要对 `quality/database/migrations/` 批量执行 `dos2unix`，也不要重新保存任何已应用的001–009 SQL**；换行变化同样会改变SHA256。上传包按原字节打包、传输并校验总包哈希；已有SQL以实际已应用文件的字节为准。

测试应显示零个 `failed`、零个 `error`。`skipped`表示需要专用测试条件，不能当作数据库联调已通过。有失败就先修复重跑，不通过排除失败用例继续发布。打包后若再次修改代码，必须重新测试、打包并记录哈希。

### 2.2 方式 A：直接上传当前本地版本

适用于直接部署当前工作区，不要求先提交 Git。更新包只包含后端代码和部署文件，不包含本机 `.env`、业务数据、模型、日志、前端独立项目或开发热重载 Compose。

```powershell
Set-Location D:\project\CS
New-Item -ItemType Directory -Force .\tmp | Out-Null
$archivePackage = Join-Path $PWD 'tmp\cs-archive-update.tar.gz'

tar.exe -czf $archivePackage `
  --exclude=__pycache__ --exclude=*.pyc `
  --exclude=knowledge_base_in/output --exclude=knowledge_base_in/vector_store `
  --exclude=reminder/build --exclude=reminder/dist --exclude=.pytest_cache `
  ics_agent quality knowledge_base_in reminder deploy docs `
  docker-compose.yml docker-compose.archive.yml `
  .dockerignore requirements.txt .env.example
if ($LASTEXITCODE -ne 0) { throw '归档更新包打包失败。' }

Get-FileHash $archivePackage -Algorithm SHA256
scp -o ServerAliveInterval=30 $archivePackage ics:~/cs-archive-update.tar.gz
if ($LASTEXITCODE -ne 0) { throw '更新包上传失败。' }
```

保存输出的 SHA256，第 4.1 节在服务器核对。采用此方式，跳过 2.3 和 4.2。

### 2.3 方式 B：沿用 Git 发布

先按现有项目流程将本次准备发布内容整理到 `test`，并确认工作区没有未处理的修改。不要直接把包含无关改动的工作区切换、重置或全部提交。

```powershell
Set-Location D:\project\CS
git branch --show-current
git status --short
git remote
git log -1 --oneline test
```

确认当前发布分支为 `test`、内容已提交并完成检查后推送。当前项目的远端名为 `chatwoot`，不是旧文档中假定的 `origin`；其他开发机先按上面输出核对实际远端名。

```powershell
if (git status --porcelain) { throw '工作区仍有未提交修改，停止发布。' }
git push chatwoot test
if ($LASTEXITCODE -ne 0) { throw '推送失败，停止发布。' }
$archiveReleaseCommit = (git rev-parse test).Trim()
"本次发布完整 SHA：$archiveReleaseCommit"
```

服务器只能拉取已推送的提交；方式 A 打包当前工作区的内容不自动等于远端 `test` 内容。

## 三、服务器检查、暂停与双库备份

### 3.1 登录、准备参数并检查现有服务

在Windows PowerShell执行 `ssh ics`。以下命令均在**Ubuntu服务器Bash**执行。`/opt/cs`应属于当前发布用户；用户应能运行Docker。先检查原运行目录和工具：

```bash
(
set -euo pipefail
cd /opt/cs
test -f .env
test -f docker-compose.yml
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
docker compose --project-directory /opt/cs --env-file .env -f docker-compose.yml ps -a
df -h /opt/cs "$HOME"
free -h
docker compose version
for archive_tool in rsync flock python3 curl openssl git tar nano; do
  command -v "$archive_tool" || { printf '缺少工具：%s\n' "$archive_tool"; exit 1; }
done
docker compose up --help | grep -- --wait-timeout
docker compose config --help | grep -- --format
curl --help all | grep -- --fail-with-body
timedatectl status
)
```

工具缺失时先执行 `sudo apt-get update`，再执行 `sudo apt-get install -y rsync util-linux python3 curl openssl git tar nano`，然后重跑检查。Compose须支持上述两项参数，curl须支持 `--fail-with-body`；不支持时先更新服务器对应工具。Docker权限错误须先按现有运维流程处理，不混用不同发布用户。时间应正确，签名验证依赖正确时钟。

创建**只含非密钥参数**的发布记录：

```bash
cd /opt/cs
umask 077
mkdir -p deployment-records
chmod 700 deployment-records
nano deployment-records/archive-release-params.sh
```

粘贴以下内容，按实际修改IP、端口、账号及两个Chatwoot容器名。容器名从上方 `docker ps` 抄录；可用 `ip -br -4 address` 查看本机内网IP。示例值不能未经核对直接使用：

```bash
export ARCHIVE_RELEASE_IP='172.20.48.12'
export ARCHIVE_RELEASE_ICS_URL="http://${ARCHIVE_RELEASE_IP}:3001"
export ARCHIVE_RELEASE_RECEIVER_URL="http://${ARCHIVE_RELEASE_IP}:3010"
export CW_QUALITY_BASE="http://${ARCHIVE_RELEASE_IP}:3000"
export CW_QUALITY_ACCOUNT_ID='1'
export CW_QUALITY_RAILS_CONTAINER='chatwoot-rails-1'
export CW_QUALITY_SIDEKIQ_CONTAINER='chatwoot-sidekiq-1'
```

`nano`保存操作：`Ctrl+O`、回车，再按 `Ctrl+X`退出；后文“编辑保存”均同此操作。此文件不放Token或密码。加载并检查：

```bash
source /opt/cs/deployment-records/archive-release-params.sh
(
set -euo pipefail
curl --fail --max-time 20 "$ARCHIVE_RELEASE_ICS_URL/health"
archive_existing_db=$(docker compose --project-directory /opt/cs --env-file /opt/cs/.env \
  -f /opt/cs/docker-compose.yml ps -q postgres)
test -n "$archive_existing_db"
docker inspect "$archive_existing_db" --format '{{.Name}} {{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}'
test "$(docker inspect "$archive_existing_db" --format '{{.State.Health.Status}}')" = healthy
docker inspect "$CW_QUALITY_RAILS_CONTAINER" --format '{{.Config.Image}}'
docker inspect "$CW_QUALITY_SIDEKIQ_CONTAINER" --format '{{.Config.Image}}'
printf 'PASS: 原ICS、主库及两个Chatwoot容器检查完成。\n'
)
```

ICS应HTTP200；主库应显示 `running healthy`，最终输出 `PASS`。现有ICS/主库的Compose项目标签必须为 `cs`，3.2还会自动检查。实际不是 `cs` 时先停止，调整整套部署约定；不能用下面固定的项目标签误建另一套空数据库。

**生产Chatwoot为4.18.0仍须检查定制能力及私网配置。** 在停止ICS前运行下列只读检查，只读取源码和指定的私网开关，不输出密钥：

```bash
(
set -euo pipefail
for archive_cw_container in "$CW_QUALITY_RAILS_CONTAINER" "$CW_QUALITY_SIDEKIQ_CONTAINER"; do
  docker exec -i "$archive_cw_container" ruby <<'RUBY'
require 'json'
trigger = File.read('/app/lib/webhooks/trigger.rb')
controller = File.read('/app/app/controllers/api/v1/accounts/webhooks_controller.rb')
model = File.read('/app/app/models/webhook.rb')
checks = {
  signature_header: trigger.include?('X-Chatwoot-Signature'),
  timestamp_header: trigger.include?('X-Chatwoot-Timestamp'),
  signing_secret: trigger.include?('@secret'),
  webhook_api: controller.include?('def create') && controller.include?('def update'),
  message_updated: model.include?('message_updated'),
  private_network_enabled: ENV['SAFE_FETCH_ALLOW_PRIVATE_NETWORK'] == 'true'
}
puts JSON.generate(checks)
abort('Chatwoot preflight failed') unless checks.values.all?
RUBY
done
)
```

两份输出应全部为 `true`。此为能力预检，签名算法和定制编辑/删除载荷还须通过11.3真实消息验证。失败时先处理Chatwoot，不进入停机步骤：缺少定制源码时，按原Chatwoot镜像发布流程同步本地试验已验证的定制镜像；ICS更新包不包含该镜像。仅私网开关缺失时，按[首次部署手册10B节](server-first-deployment.md#10b-无域名的私网测试方式)，在Chatwoot实际Compose中为Rails、Sidekiq显式加入 `SAFE_FETCH_ALLOW_PRIVATE_NETWORK: "true"`，用原来的Compose组合重新创建这两个服务，再重跑本检查。将开关写在ICS的 `.env` 无效。

确认模型、渠道资料和磁盘空间正常。原ICS发布前已异常时先处理。等待已接收请求结束，暂停后台资料导入，再进入维护时间。重连SSH后先运行 `source /opt/cs/deployment-records/archive-release-params.sh`；Bash数组如 `compose=(...)`仍按各节重新定义。

### 3.2 备份并暂停写入进程

下面代码使用 **更新前的主 Compose**，即首次上线尚无 `docker-compose.archive.yml` 也能执行。按 `cs` 项目标签查找已有归档服务，仅停止 ICS、归档接收和解析，不停止主数据库或 Chatwoot。

本节使用当前ubuntu用户备份，**不调用sudo、不要求系统密码、不修改原文件权限或所有者**。当前用户须能够读取代码和运行资料；数据库仍通过已获授权的Docker命令导出。文件不可读时正常报错并停止，不能用 `--ignore-failed-read`把漏备份当作成功。所有备份文件在当前用户私有的新目录中生成，无需再执行chown。若旧版命令正停在sudo密码提示，先按 `Ctrl+C`退出，再重新执行下方整段；会新建备份目录，不复用上次未完成的备份。

```bash
(
  set -euo pipefail
  umask 077
  cd /opt/cs
  test -r .env
  test -d database
  trap 'printf "%s\n" "备份未完成，停止后续部署；若ICS已停止且尚未同步代码，可按本节末尾的start命令恢复原应用。" >&2' ERR
  mkdir -p deployment-records
  chmod 700 deployment-records
  archive_release_tag=$(date +%Y%m%d-%H%M%S)
  archive_backup_root="$HOME/cs-backups"
  mkdir -p "$archive_backup_root"
  chmod 700 "$archive_backup_root"
  archive_backup_dir="$archive_backup_root/archive-$archive_release_tag"
  mkdir -m 700 "$archive_backup_dir"
  base=(docker compose --project-directory /opt/cs --env-file .env -f docker-compose.yml)

  archive_previous_ics=$("${base[@]}" ps -a -q ics-agent)
  test -n "$archive_previous_ics"
  test "$(docker inspect "$archive_previous_ics" --format '{{ index .Config.Labels "com.docker.compose.project" }}')" = cs
  archive_business_db=$("${base[@]}" ps -q postgres)
  test -n "$archive_business_db"
  docker inspect "$archive_business_db" --format '{{.Id}} {{.Config.Image}} {{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}} {{.Source}}{{end}}{{end}}' \
    > "$archive_backup_dir/business-container-before.txt"
  docker inspect "$archive_previous_ics" --format '{{.Image}}' > "$archive_backup_dir/previous-ics-image-id.txt"
  docker image tag "$(cat "$archive_backup_dir/previous-ics-image-id.txt")" "ics-agent:rollback-archive-$archive_release_tag"
  printf '%s\n' "$archive_release_tag" > "$archive_backup_dir/release-tag.txt"
  docker ps --filter label=com.docker.compose.project=cs \
    --format '{{.Label "com.docker.compose.service"}}' > "$archive_backup_dir/running-services-before.txt"
  if [ -d /opt/cs-src/.git ]; then
    git -C /opt/cs-src rev-parse HEAD > "$archive_backup_dir/previous-source-commit.txt"
  fi
  cp -p .env "$archive_backup_dir/env.backup"
  chmod 600 "$archive_backup_dir/env.backup"

  "${base[@]}" stop ics-agent
  for archive_service in quality-receiver quality-worker; do
    archive_service_id=$(docker ps -a --filter label=com.docker.compose.project=cs \
      --filter "label=com.docker.compose.service=$archive_service" --format '{{.ID}}')
    if [ -n "$archive_service_id" ]; then
      test "$(printf '%s\n' "$archive_service_id" | wc -l)" -eq 1
      docker inspect "$archive_service_id" --format '{{.Image}}' > "$archive_backup_dir/previous-$archive_service-image-id.txt"
      docker image tag "$(cat "$archive_backup_dir/previous-$archive_service-image-id.txt")" \
        "ics-agent:rollback-archive-$archive_release_tag-$archive_service"
      docker stop "$archive_service_id"
    fi
  done

  "${base[@]}" exec -T postgres sh -c \
    'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' \
    > "$archive_backup_dir/business-postgres.dump"
  test -s "$archive_backup_dir/business-postgres.dump"
  "${base[@]}" exec -T postgres pg_restore --list \
    < "$archive_backup_dir/business-postgres.dump" > /dev/null
  "${base[@]}" exec -T postgres sh -c \
    'exec pg_dumpall -U "$POSTGRES_USER" --roles-only' > "$archive_backup_dir/business-roles.sql"

  archive_db_id=$(docker ps -a --filter label=com.docker.compose.project=cs \
    --filter label=com.docker.compose.service=archive-postgres --format '{{.ID}}')
  if [ -n "$archive_db_id" ]; then
    test "$(printf '%s\n' "$archive_db_id" | wc -l)" -eq 1
    test "$(docker inspect "$archive_db_id" --format '{{.State.Running}}')" = true
    docker exec "$archive_db_id" sh -c \
      'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' \
      > "$archive_backup_dir/archive-postgres.dump"
    test -s "$archive_backup_dir/archive-postgres.dump"
    docker exec -i "$archive_db_id" pg_restore --list \
      < "$archive_backup_dir/archive-postgres.dump" > /dev/null
    docker exec "$archive_db_id" sh -c \
      'exec pg_dumpall -U "$POSTGRES_USER" --roles-only' > "$archive_backup_dir/archive-roles.sql"
  else
    printf '%s\n' '本次首次增加归档数据库，没有既有归档库可备份。' \
      > "$archive_backup_dir/archive-first-install.txt"
  fi

  test -d database
  archive_data_paths=(database)
  for archive_path in knowledge_base_in/output knowledge_base_in/vector_store; do
    if [ -d "$archive_path" ]; then archive_data_paths+=("$archive_path"); fi
  done
  # 使用自定义运行资料目录时，在此数组追加其真实路径后再执行备份。
  tar -czf "$archive_backup_dir/runtime-data.tar.gz" "${archive_data_paths[@]}"
  archive_code_paths=()
  for archive_path in ics_agent quality knowledge_base_in reminder deploy docs \
      Dockerfile docker-compose.yml docker-compose.archive.yml .dockerignore requirements.txt .env.example; do
    if [ -e "$archive_path" ]; then archive_code_paths+=("$archive_path"); fi
  done
  tar --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='knowledge_base_in/output' --exclude='knowledge_base_in/vector_store' \
    --exclude='reminder/build' --exclude='reminder/dist' \
    -czf "$archive_backup_dir/previous-code.tar.gz" "${archive_code_paths[@]}"
  tar -tzf "$archive_backup_dir/runtime-data.tar.gz" > /dev/null
  tar -tzf "$archive_backup_dir/previous-code.tar.gz" > /dev/null
  chmod 600 "$archive_backup_dir"/*
  cp "$archive_backup_dir/business-container-before.txt" deployment-records/business-container-before.txt
  printf '%s\n' completed > deployment-records/archive-backup-status.txt
  printf '%s\n' "$archive_backup_dir" > deployment-records/last-archive-backup.txt
  printf '备份完成：%s\n' "$archive_backup_dir"
)
```

只有输出“备份完成”才继续。若已有归档库容器停止、备份权限不足或磁盘空间不足，先解决再重跑本节；每次生成新时间戳目录。不要把已有库无法读取误当成首次空库，也不要为备份删除、重建归档卷。

备份位于 Docker 构建目录之外。默认输出目录不存在时不会让备份失败；若服务器的 FAQ、文档、输出或向量库使用自定义路径，执行前须将其加入 `archive_data_paths` 并核对压缩包目录。自定义运行资料若位于同步脚本会删除更新的代码目录内，还需先确认该路径已在同步脚本中排除，不能仅做备份后直接同步。`.env`、聊天数据库及角色备份包含敏感信息，角色 SQL 可能包含密码哈希，不能上传工单或提交 Git。`pg_restore --list` 和压缩包目录检查验证文件可读取，不代替完整恢复演练。模型本次不修改、不重新上传，沿用原模型备份策略；发布前不要清理旧镜像。

若本次备份失败或决定取消维护，且尚未同步代码，可用更新前主Compose恢复旧ICS：

```bash
docker compose --project-directory /opt/cs --env-file /opt/cs/.env \
  -f /opt/cs/docker-compose.yml start ics-agent
```

然后等待原健康接口恢复，并按备份的 `running-services-before.txt`恢复原本运行的归档进程。已开始替换代码或配置后，按第十四节回退。若报 `Permission denied`，先核对具体不可读路径，不改变整个项目的权限、不用提权容器绕过文件权限；没有该路径的读取授权时需由管理员处理后再重跑。

### 3.3 本次已决定跳过备份时

本节适用于本次“尚未部署quality，备份因资料权限失败，已明确决定不继续备份”的发布。停止使用3.2失败的压缩包，执行下方命令确认ICS停止、原主库健康，并记录主库标识供9.3核对；不复制数据库、代码或运行资料，也不需要sudo密码。此前未完成的备份目录保留原样，无需删除。**本次没有完整备份，14.2/14.3依赖完整备份的回退命令不适用。**

```bash
(
set -euo pipefail
umask 077
cd /opt/cs
base=(docker compose --project-directory /opt/cs --env-file .env -f docker-compose.yml)
archive_current_ics=$("${base[@]}" ps -a -q ics-agent)
test -n "$archive_current_ics"
test "$(docker inspect "$archive_current_ics" --format '{{ index .Config.Labels "com.docker.compose.project" }}')" = cs
"${base[@]}" stop ics-agent
test "$(docker inspect "$archive_current_ics" --format '{{.State.Running}}')" = false
archive_business_db=$("${base[@]}" ps -q postgres)
test -n "$archive_business_db"
test "$(docker inspect "$archive_business_db" --format '{{.State.Health.Status}}')" = healthy
mkdir -p deployment-records
chmod 700 deployment-records
docker inspect "$archive_business_db" --format '{{.Id}} {{.Config.Image}} {{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}} {{.Source}}{{end}}{{end}}' \
  > deployment-records/business-container-before.txt
printf '%s\n' skipped > deployment-records/archive-backup-status.txt
printf '%s\n' 'PASS: 本次跳过备份；ICS已停止，主库健康，主库标识已记录。继续第四节。'
)
```

继续第四节选择与第二节一致的A或B，再依次执行第五至十一节。第四至十一节无需执行sudo；第五节同步保留 `database/`及知识库输出/向量库，不因本次跳过备份而修改这些资料权限。第9.3节使用本节的主库标识文件核对，发布记录如实填写 `backup=skipped`；不创建或伪造 `last-archive-backup.txt`来绕过旧的检查。

## 四、在服务器准备新版源码（二选一）

两种方式最终都生成 `/opt/cs/deployment-records/archive-source-dir.txt`。源码目录必须在运行目录 `/opt/cs` **之外**，共享同步脚本会拒绝相互包含的源、目标目录。

### 4.1 对应方式 A：核对并解压上传包

先核对服务器 SHA256 与第 2.2 节 Windows 输出一致：

```bash
sha256sum "$HOME/cs-archive-update.tar.gz"
```

一致后执行：

```bash
(
  set -euo pipefail
  umask 077
  test -s "$HOME/cs-archive-update.tar.gz"
  archive_source_root="$HOME/cs-release"
  mkdir -p "$archive_source_root" /opt/cs/deployment-records
  chmod 700 "$archive_source_root"
  archive_source_dir=$(mktemp -d "$archive_source_root/archive-source.XXXXXX")
  tar -xzf "$HOME/cs-archive-update.tar.gz" -C "$archive_source_dir"
  test -f "$archive_source_dir/deploy/server/sync-code.sh"
  test -f "$archive_source_dir/deploy/server/release.sh"
  test -f "$archive_source_dir/docker-compose.archive.yml"
  test -f "$archive_source_dir/quality/database/migrations/010_round_replay_windows.sql"
  printf '%s\n' "$archive_source_dir" > /opt/cs/deployment-records/archive-source-dir.txt
  sha256sum "$HOME/cs-archive-update.tar.gz" > /opt/cs/deployment-records/archive-package.sha256
  printf '新版源码目录：%s\n' "$archive_source_dir"
)
```

不要直接将更新包解压覆盖整个 `/opt/cs`。采用此方式，跳过 4.2。

### 4.2 对应方式 B：快进拉取 Git 发布分支

```bash
git -C /opt/cs-src status --short
git -C /opt/cs-src branch --show-current
```

必须确认工作区干净、分支为 `test`。发现本地改动或分支不符时，先按现有发布流程处理，不能用 `reset --hard` 掩盖差异。

```bash
git -C /opt/cs-src pull --ff-only https://code-sayu.xingyongshe.com/backend/chatwoot.git test
git -C /opt/cs-src log -1 --oneline
```

粘贴 Windows 记录的完整 SHA 并校验；不要保留下面占位文字：

```bash
expected_archive_commit='粘贴第2.3节记录的完整SHA'
actual_archive_commit=$(git -C /opt/cs-src rev-parse HEAD)
test "$actual_archive_commit" = "$expected_archive_commit" || { echo '源码 SHA 不一致，停止发布。'; exit 1; }
test -f /opt/cs-src/quality/database/migrations/010_round_replay_windows.sql
printf '%s\n' /opt/cs-src > /opt/cs/deployment-records/archive-source-dir.txt
printf '%s\n' "$actual_archive_commit" > /opt/cs/deployment-records/archive-new-source-commit.txt
```

## 五、同步代码并保留运行配置与资料

两种源码来源从这里使用同一套命令：

Git方式的拉取更新 `/opt/cs-src`，运行目录 `/opt/cs`由本节同步。第4.2节末尾的源码SHA核对和写入 `archive-source-dir.txt`也须执行；单独运行 `git pull`不会创建这个记录文件。本节没有打印成功提示时不能视为已同步，以下检查失败会输出具体原因。

```bash
(
  set -euo pipefail
  trap 'printf "%s\n" "第五节同步未完成，请处理上方错误；不要继续配置检查或启动新服务。" >&2' ERR
  if [ ! -s /opt/cs/deployment-records/archive-source-dir.txt ]; then
    printf '%s\n' '缺少源码目录记录：/opt/cs/deployment-records/archive-source-dir.txt；先完成4.1或4.2末尾的源码记录步骤。' >&2
    exit 1
  fi
  archive_source_dir=$(cat /opt/cs/deployment-records/archive-source-dir.txt)
  printf '同步来源：%s\n同步目标：/opt/cs\n' "$archive_source_dir"
  for archive_required in deploy/server/sync-code.sh docker-compose.archive.yml \
      quality/deploy/migrate.sh quality/database/migrations/010_round_replay_windows.sql; do
    if [ ! -f "$archive_source_dir/$archive_required" ]; then
      printf '源码缺少文件：%s/%s；核对实际拉取目录、分支和提交版本。\n' "$archive_source_dir" "$archive_required" >&2
      exit 1
    fi
  done
  bash "$archive_source_dir/deploy/server/sync-code.sh" "$archive_source_dir" /opt/cs
  cmp -s /opt/cs/Dockerfile "$archive_source_dir/deploy/server/Dockerfile.backend-only"
  test -f /opt/cs/docker-compose.archive.yml
  test -f /opt/cs/quality/database/migrations/010_round_replay_windows.sql
  test -f /opt/cs/database/channels.json
  printf '%s\n' '代码同步完成，服务器原配置与数据目录保留。'
)
```

`sync-code.sh` 仅在明确的应用代码目录内使用有边界的同步删除；不能将其改为对整个 `/opt/cs` 使用 `rsync --delete`。后端 Dockerfile 包含 `quality/`，生产不需要本地源码挂载。

同步脚本会将 `deploy/archive/init`设为755、其中的 `01-archive-roles.sh`设为644，供容器内PostgreSQL用户读取。该脚本只包含初始化代码，密码从容器环境读取；不放宽 `.env`、资料目录或部署记录的权限。此前使用 `umask 077`拉取的代码可能是700/600，不能把该权限直接用于容器初始化挂载。

检查 `/opt/cs/database/channels.json` 仍为服务器实际账号、收件箱映射。解析进程将此文件只读挂载；文件不存在时先恢复服务器现有正确配置，不能让 Docker 把缺失文件路径建成目录。归档中的业务渠道依赖这份映射，Chatwoot 收件箱名称与 ICS 业务渠道是两套不同的筛选项。

若本次另有明确的渠道映射变更，worker 在启动时读取配置，修改后需重建该进程；仅重启 ICS 不足以更新它。既有派生索引不会自动按新映射重算，历史投影调整应另按明确的数据整理范围执行，本次不附带重放或补录。

## 六、补充服务器 `.env`

### 6.1 配置原则

```bash
cd /opt/cs
nano .env
```

只在现有文件中新增或核对以下归档项。不要用本机 `.env`、`.env.example` 或模板替换整份服务器配置；保留现有 `POSTGRES_*`、管理员、Chatwoot AgentBot、模型、提醒插件、渠道和业务开关。

首次建库为每个数据库角色生成不同的随机密码，例如在服务器本地运行 `openssl rand -hex 32`，将结果保存到受限配置中，不粘贴到聊天。已有归档库必须沿用其现有实际密码；修改环境变量不等于自动更改既有数据库密码。

本次首次上线需分别生成管理员、ingest、reader、quality、assignment、presence六个不同密码：每次运行 `openssl rand -hex 32`取得一个值，填入下方对应一项，六项不能共用一个值。保存 `.env` 后用 `chmod 600 /opt/cs/.env`限制权限。已有同名项直接修改原行，不在文件末尾重复追加。

### 6.2 独立数据库及功能开关

下面密码都是占位，必须替换。角色名与建库脚本保持一致：

```dotenv
ARCHIVE_POSTGRES_DB=ics_quality
ARCHIVE_POSTGRES_HOST=archive-postgres
ARCHIVE_POSTGRES_PORT=5432
ARCHIVE_POSTGRES_SSLMODE=prefer
ARCHIVE_POSTGRES_ADMIN_PASSWORD=REPLACE_WITH_RANDOM_ARCHIVE_ADMIN_PASSWORD
ARCHIVE_INGEST_USER=archive_ingest
ARCHIVE_INGEST_PASSWORD=REPLACE_WITH_RANDOM_ARCHIVE_INGEST_PASSWORD
ARCHIVE_READER_USER=archive_reader
ARCHIVE_READER_PASSWORD=REPLACE_WITH_RANDOM_ARCHIVE_READER_PASSWORD
ARCHIVE_QUALITY_USER=archive_quality
ARCHIVE_QUALITY_PASSWORD=REPLACE_WITH_RANDOM_ARCHIVE_QUALITY_PASSWORD
ARCHIVE_POSTGRES_POOL_SIZE=5

ARCHIVE_QUERY_ENABLED=true
ARCHIVE_QUERY_POOL_SIZE=2
ARCHIVE_ASSIGNMENT_SYNC_ENABLED=true
ARCHIVE_ASSIGNMENT_USER=archive_assignment
ARCHIVE_ASSIGNMENT_PASSWORD=REPLACE_WITH_RANDOM_ASSIGNMENT_PASSWORD
ARCHIVE_PRESENCE_ENABLED=true
ARCHIVE_PRESENCE_USER=archive_presence
ARCHIVE_PRESENCE_PASSWORD=REPLACE_WITH_RANDOM_PRESENCE_PASSWORD
ARCHIVE_PRESENCE_MAX_AGENTS=100
```

功能开关设为 `true` 后，须完成全部迁移再启动新版 ICS。只需要原始采集时可将三个功能开关保持 `false`，暂不启动 `quality-worker`；本手册后续按全部功能启用验收。

服务器模板默认使用 `archive-postgres:5432` 作为 Docker 内地址。归档数据库不发布宿主机端口，不连接原 ICS 的 `postgres`，也不连接 Chatwoot 自己的数据库。

### 6.3 本次采用的资源参数

以下数值与根目录 `.env.example` 的保守部署值一致，应显式补入服务器配置，不能依赖较大的代码或 Compose 回退默认值：

```dotenv
ARCHIVE_EXPORT_MAX_ENTITIES=1000
ARCHIVE_EXPORT_MAX_MESSAGES=5000
ARCHIVE_EXPORT_MAX_BYTES=8388608
ARCHIVE_EXPORT_TIMEOUT_SECONDS=60

ARCHIVE_PROCESSOR_POOL_SIZE=1
ARCHIVE_PROCESSOR_LEASE_SECONDS=120
ARCHIVE_PROCESSOR_POLL_SECONDS=2
ARCHIVE_PROCESSOR_MAX_ATTEMPTS=8
ARCHIVE_PROCESSOR_RECENT_ROUNDS=50
ARCHIVE_PROCESSOR_INDEX_BATCH_SIZE=500
ARCHIVE_PROCESSOR_INDEX_BATCH_BYTES=8388608
# 单个解析批次的预算；超限时自动缩小轮次批次，旧历史继续保留。
ARCHIVE_PROCESSOR_MAX_CONVERSATION_EVENTS=5000
ARCHIVE_PROCESSOR_MAX_CONVERSATION_BYTES=16777216
```

单接收进程最多 5 个归档连接，交互查询池 2 个，解析进程 1 个；状态采集固定单连接、单执行线程，导出也使用独立单连接和单线程。分配同步另有受限连接。上述数量不是原 ICS `POSTGRES_POOL_SIZE`，本次不要调整原业务池。

`quality-worker` 已在独立容器限制为 `0.5 CPU / 512 MiB`。导出的 8 MiB 是索引 JSON 预算，**不是 ICS 进程内存、临时磁盘或 CPU 的硬上限**；导出仍在 ICS 进程内。其他独立容器的可选硬限制见第十五节，本次主流程不擅自给原 ICS 新增内存上限。

### 6.4 归档接收及复用的 Chatwoot 配置

```dotenv
ARCHIVE_SOURCE_KEY=chatwoot-main
ARCHIVE_ALLOWED_ACCOUNT_IDS=1
ARCHIVE_WEBHOOK_SECRET=稍后填写专用账户Webhook返回的实际Secret
ARCHIVE_RECEIVER_HOST=172.20.48.12
ARCHIVE_RECEIVER_PORT=3010
ARCHIVE_WEBHOOK_TOLERANCE_SECONDS=300
ARCHIVE_RECEIVER_MAX_BODY_BYTES=2097152
```

`ARCHIVE_SOURCE_KEY` 标识 Chatwoot 实例，开始采集后保持稳定；随意修改会形成不同来源的数据范围。账号填实际 ID。多个账号以逗号分隔，但当前接收实例只支持同一签名 Secret；不同账号各自不同的 Secret 不能拼在一起使用。

`ARCHIVE_RECEIVER_HOST` 填服务器实际内网 IP，不带协议、端口或路径。模板的 `127.0.0.1` 只适合主机本地检查，Chatwoot 容器不能通过它访问主机接收器。服务绑定内网 IP 并沿用现有内网访问规则，无需新增公网端口、域名或证书。

此处IP/端口须与3.1参数文件中的 `ARCHIVE_RELEASE_IP`、`ARCHIVE_RELEASE_RECEIVER_URL`一致；`CHATWOOT_BASE_URL`、账号也应与参数文件一致。第六节先暂留签名占位，第七节取得真实Secret后必须回填，不能带占位值启动服务。

客服目录和状态采集复用现有以下配置，按实际值核对，不覆盖现有有效凭据：

```dotenv
CHATWOOT_BASE_URL=http://172.20.48.12:3000
CHATWOOT_ACCOUNT_ID=1
CHATWOOT_CONTACT_API_TOKEN=具备该账户目录权限的个人用户API凭据
```

`CHATWOOT_CONTACT_API_TOKEN` 优先；留空时回退 `CHATWOOT_API_TOKEN`。必须是能访问该账号坐席和收件箱目录的用户凭据，AgentBot Token 不能替代。状态采集使用配置的当前单个账号，与允许归档接收的账号列表不是同一概念。

## 七、创建或更新专用 Chatwoot 账户 Webhook

### 7.1 准备参数、令牌和请求体

本节不修改原AgentBot或其他Webhook。使用Chatwoot账户管理员在个人资料页获取的个人API Access Token。首次上线执行7.2；已有专用归档记录时执行7.3；两者完成后都执行7.4。

在同一个服务器Bash窗口执行。参数文件必须已按3.1创建；本段使用函数保留后续步骤需要的变量，并在缺少文件、参数或令牌时立即返回，不继续生成错误请求：

```bash
archive_prepare_webhook() {
cd /opt/cs || return 1
if [ ! -r deployment-records/archive-release-params.sh ]; then
  printf '%s\n' '缺少 /opt/cs/deployment-records/archive-release-params.sh；先执行3.1的创建参数文件步骤，本次未发起请求。' >&2
  return 1
fi
source deployment-records/archive-release-params.sh || return 1
python3 - <<'PY' || return 1
import os
from urllib.parse import urlsplit
for key in ('ARCHIVE_RELEASE_RECEIVER_URL', 'CW_QUALITY_BASE'):
    value = os.environ.get(key, '')
    try:
        url = urlsplit(value)
        valid = url.scheme in ('http', 'https') and bool(url.hostname) and url.port != 0
    except ValueError:
        valid = False
    if not valid:
        raise SystemExit('地址参数缺失或无效：'+key+'；先修正3.1参数文件，未发起请求。')
account = os.environ.get('CW_QUALITY_ACCOUNT_ID', '')
if not (account.isascii() and account.isdecimal() and int(account) > 0):
    raise SystemExit('CW_QUALITY_ACCOUNT_ID必须为实际账号的正整数ID，未发起请求。')
PY
umask 077
set -o pipefail
read -r -s -p 'Chatwoot账户管理员个人API Token：' CW_QUALITY_TOKEN </dev/tty || return 1
printf '\n'
if [ -z "$CW_QUALITY_TOKEN" ]; then
  printf '%s\n' 'Token为空，未发起请求。' >&2
  return 1
fi
export CW_QUALITY_ARCHIVE_URL="$ARCHIVE_RELEASE_RECEIVER_URL/quality/webhooks/chatwoot"
python3 - <<'PY' || return 1
import json, os
from pathlib import Path
payload = {'webhook': {
    'name': 'ICS conversation archive',
    'url': os.environ['CW_QUALITY_ARCHIVE_URL'],
    'subscriptions': ['message_created', 'message_updated', 'conversation_created',
                      'conversation_updated', 'conversation_status_changed']}}
Path('.env.quality-webhook-request.json').write_text(json.dumps(payload), encoding='utf-8')
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY
curl --fail-with-body --silent --show-error \
  -H "api_access_token: $CW_QUALITY_TOKEN" \
  "$CW_QUALITY_BASE/api/v1/accounts/$CW_QUALITY_ACCOUNT_ID/webhooks" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps([{k:w.get(k) for k in ("id","name","url","subscriptions")} for w in d["payload"]["webhooks"]],ensure_ascii=False,indent=2))'
}
archive_prepare_webhook
```

若提示参数文件不存在，说明3.1的文件创建未完成或文件保存在了其他位置。回到3.1，从 `mkdir -p deployment-records`开始创建并保存该文件，再重新执行本节整段命令；只重新执行 `source`不会自动创建文件。此前错误生成的 `.env.quality-webhook-request.json`会被成功执行的本节覆盖，无需修改数据库或重做备份。

检查请求体中的URL是本次实际3010接收地址，查询结果来自正确账号。已有同URL或明确专用于归档的记录时记下ID，跳过7.2，执行7.3。无专用记录时执行7.2。账户内URL唯一，不反复新建。

### 7.2 首次新建专用归档Webhook

```bash
cd /opt/cs
curl --fail-with-body --silent --show-error \
  -X POST -H "api_access_token: $CW_QUALITY_TOKEN" \
  -H 'Content-Type: application/json' \
  --output .env.quality-webhook-result.json \
  --data-binary @.env.quality-webhook-request.json \
  "$CW_QUALITY_BASE/api/v1/accounts/$CW_QUALITY_ACCOUNT_ID/webhooks"
```

命令应退出码0。失败时用 `nano .env.quality-webhook-result.json`仅在本地查看错误，处理后重试，不把错误响应当作成功。**成功后跳过7.3，直接执行7.4。** 创建到服务就绪之间属于已接受的维护漏收窗口。

### 7.3 已有专用归档Webhook时更新

从7.1结果抄录专用归档ID；不要填AgentBot ID或其他业务Webhook ID：

```bash
cd /opt/cs
read -r -p '专用归档Webhook ID：' CW_QUALITY_WEBHOOK_ID
if [[ ! "$CW_QUALITY_WEBHOOK_ID" =~ ^[1-9][0-9]*$ ]]; then
  printf '%s\n' 'ID格式错误，未执行更新。'
else
  curl --fail-with-body --silent --show-error \
    -X PATCH -H "api_access_token: $CW_QUALITY_TOKEN" \
    -H 'Content-Type: application/json' \
    --output .env.quality-webhook-result.json \
    --data-binary @.env.quality-webhook-request.json \
    "$CW_QUALITY_BASE/api/v1/accounts/$CW_QUALITY_ACCOUNT_ID/webhooks/$CW_QUALITY_WEBHOOK_ID"
fi
```

命令应退出码0。订阅列表整体替换，请求文件必须含全部五项。更新通常保留Secret，但以成功响应为准；无单条GET路由，用列表接口读回。

### 7.4 校验响应、填回签名并确认目录权限

新建或更新成功后，执行：

```bash
cd /opt/cs
python3 - <<'PY'
import json, os
w = json.load(open('.env.quality-webhook-result.json'))['payload']['webhook']
expected = {'message_created','message_updated','conversation_created',
            'conversation_updated','conversation_status_changed'}
assert w.get('id') and w.get('url') == os.environ['CW_QUALITY_ARCHIVE_URL'], 'Webhook ID或URL不符'
assert set(w.get('subscriptions', [])) == expected, '订阅不完整'
assert len(w.get('secret') or '') >= 16, '响应没有有效签名Secret，检查Chatwoot定制版本'
print(json.dumps({k:w.get(k) for k in ('id','url','subscriptions')}, ensure_ascii=False, indent=2))
print('PASS: Webhook配置与Secret存在性检查通过；未打印Secret。')
PY
nano .env.quality-webhook-result.json
```

在服务器编辑器中找到 `payload.webhook.secret`并复制，退出编辑器。再执行 `nano /opt/cs/.env`，将其填入 `ARCHIVE_WEBHOOK_SECRET=`，只保留一个同名配置项；不得替换原AgentBot Secret。接口不接受自定义Secret参数，必须使用Chatwoot返回值。`ARCHIVE_ALLOWED_ACCOUNT_IDS`须包含本次账号；记录Webhook ID、URL、五项订阅，普通发布记录不保存Secret。

保存后读回专用Webhook的非密钥摘要，并验证该用户令牌能读取坐席与收件箱：

```bash
curl --fail-with-body --silent --show-error \
  -H "api_access_token: $CW_QUALITY_TOKEN" \
  "$CW_QUALITY_BASE/api/v1/accounts/$CW_QUALITY_ACCOUNT_ID/webhooks" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps([{k:w.get(k) for k in ("id","url","subscriptions")} for w in d["payload"]["webhooks"]],ensure_ascii=False,indent=2))'
curl --fail-with-body --silent --show-error \
  -H "api_access_token: $CW_QUALITY_TOKEN" \
  "$CW_QUALITY_BASE/api/v1/accounts/$CW_QUALITY_ACCOUNT_ID/agents" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); assert isinstance(d,list); print("Agent directory count:",len(d))'
curl --fail-with-body --silent --show-error \
  -H "api_access_token: $CW_QUALITY_TOKEN" \
  "$CW_QUALITY_BASE/api/v1/accounts/$CW_QUALITY_ACCOUNT_ID/inboxes" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); assert isinstance(d.get("payload"),list); print("Inbox directory count:",len(d["payload"]))'
```

三条均应成功；401/403先处理Token或权限。用于 `CHATWOOT_CONTACT_API_TOKEN` 的令牌也须有同样目录权限；如果该项与本次Token不同，11.5还须通过ICS实际配置验证。前端拒绝内网URL时继续使用本API，不关闭接收签名。

完成后删除临时密钥文件并检查配置：

```bash
unset CW_QUALITY_TOKEN
rm -f -- /opt/cs/.env.quality-webhook-result.json /opt/cs/.env.quality-webhook-request.json
cd /opt/cs
chmod 600 .env
docker compose --project-directory /opt/cs --env-file .env \
  -f docker-compose.yml -f docker-compose.archive.yml config --quiet
```

应无错误退出。`config --quiet`不能检查密码是否真实、数据库表是否存在。不要打印完整Compose配置、容器环境、`.env`、HTTP签名或带Token的调试输出。继续执行7.5，再进入第八节；不能绕过首次初始化与001–009迁移直接启动ICS。

### 7.5 在建库前检查最终配置

以下在受限临时文件中解析Compose，仅输出检查结论及错误项名称，不打印密码。完成后自动删除临时文件：

```bash
(
set -euo pipefail
umask 077
cd /opt/cs
source deployment-records/archive-release-params.sh
trap 'rm -f -- /opt/cs/.env.quality-compose-check.json' EXIT
docker compose --project-directory /opt/cs --env-file .env \
  -f docker-compose.yml -f docker-compose.archive.yml \
  --profile quality-capture --profile quality-query config --format json > .env.quality-compose-check.json
python3 - <<'PY'
import json, os, re
from collections import Counter
from pathlib import Path
cfg = json.loads(Path('.env.quality-compose-check.json').read_text())
services = cfg['services']
db = services['archive-postgres']['environment']
ics = services['ics-agent']['environment']
receiver = services['quality-receiver']['environment']
worker = services['quality-worker']['environment']
errors = []
keys = [m.group(1) for line in Path('.env').read_text().splitlines()
        if (m := re.match(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=', line))]
errors += ['重复配置项:'+k for k,n in Counter(keys).items() if n > 1]
password_keys = ['POSTGRES_PASSWORD','ARCHIVE_INGEST_PASSWORD','ARCHIVE_READER_PASSWORD',
                 'ARCHIVE_QUALITY_PASSWORD','ARCHIVE_ASSIGNMENT_PASSWORD','ARCHIVE_PRESENCE_PASSWORD']
for key in password_keys:
    value = str(db.get(key) or '')
    if not value or value.startswith(('REPLACE_', 'change-me')):
        errors.append('archive-postgres:'+key)
if len({db.get(key) for key in password_keys}) != 6:
    errors.append('六个归档数据库密码须不同')
for key in ['ARCHIVE_QUERY_ENABLED','ARCHIVE_ASSIGNMENT_SYNC_ENABLED','ARCHIVE_PRESENCE_ENABLED']:
    if str(ics.get(key)).lower() != 'true': errors.append('ics-agent:'+key)
for name, env in [('ics-agent',ics),('quality-receiver',receiver),('quality-worker',worker)]:
    if env.get('ARCHIVE_POSTGRES_HOST') != 'archive-postgres': errors.append(name+':ARCHIVE_POSTGRES_HOST')
    if env.get('ARCHIVE_POSTGRES_DB') != db.get('POSTGRES_DB'): errors.append(name+':ARCHIVE_POSTGRES_DB')
for name, env, key in [('ics-agent',ics,'ARCHIVE_READER_PASSWORD'),
                       ('ics-agent',ics,'ARCHIVE_ASSIGNMENT_PASSWORD'),
                       ('ics-agent',ics,'ARCHIVE_PRESENCE_PASSWORD'),
                       ('quality-receiver',receiver,'ARCHIVE_INGEST_PASSWORD'),
                       ('quality-worker',worker,'ARCHIVE_INGEST_PASSWORD')]:
    if env.get(key) != db.get(key): errors.append(name+':'+key)
secret = str(receiver.get('ARCHIVE_WEBHOOK_SECRET') or '')
if len(secret.strip()) < 16 or any(v in secret for v in ('REPLACE_', 'change-me', '稍后', '填写')):
    errors.append('quality-receiver:ARCHIVE_WEBHOOK_SECRET')
if os.environ['CW_QUALITY_ACCOUNT_ID'] not in str(receiver.get('ARCHIVE_ALLOWED_ACCOUNT_IDS','')).split(','):
    errors.append('quality-receiver:ARCHIVE_ALLOWED_ACCOUNT_IDS')
if str(ics.get('CHATWOOT_ACCOUNT_ID')) != os.environ['CW_QUALITY_ACCOUNT_ID']:
    errors.append('ics-agent:CHATWOOT_ACCOUNT_ID')
if not (ics.get('CHATWOOT_CONTACT_API_TOKEN') or ics.get('CHATWOOT_API_TOKEN')):
    errors.append('ics-agent:Chatwoot用户目录Token')
if errors:
    print('FAIL:', ', '.join(errors))
    raise SystemExit(1)
print('PASS: 配置项无重复、归档凭据独立且一致、三个功能启用、账号匹配。')
print('归档数据卷:', cfg.get('volumes',{}).get('archive_postgres_data',{}).get('name'))
PY
)
```

须出现 `PASS`，并确认数据卷名为预期的 `cs_archive_postgres_data`（已有自定义卷时须与原卷一致）。失败就回到 `.env`修正对应项，再执行本节；不要继续建库。此检查不验证Secret与Chatwoot是否匹配，实际签名仍须真实消息验收。

## 八、首次建库与数据库升级

### 8.1 首次空卷初始化做什么

第一次启动 `archive-postgres` 且归档数据卷为空时，PostgreSQL 镜像创建独立数据库和 `archive_admin`，自动执行挂载的 `deploy/archive/init/01-archive-roles.sh`，创建 `archive_owner`、采集/只读/质检角色以及 `archive`、`ops`、`quality` schema。

初始化脚本只创建身份、schema 和默认权限，**不会创建完整业务表**；必须继续运行 001–009 迁移。原 `postgres` 容器和主数据库卷不参与此初始化。

### 8.2 已有归档卷如何处理

已有卷不会重新执行 `docker-entrypoint-initdb.d`。保留原库名、实际密码、数据卷和已应用SQL，不删卷重来，不手工重跑初始化脚本，不修改任何已应用的001–009文件。本次首次上线没有归档卷时，直接执行8.3。

如既有归档库由早期手工建设，先核对角色、schema 和 `ops.schema_migrations` 是否符合项目迁移基线；遇到对象冲突时停止并核实，不能通过清空表或伪造迁移记录跳过。

已有管理员、ingest、reader、quality 密码不会因修改 `.env` 自动轮换。迁移脚本在配置真实值时会显式设置 `archive_assignment` 和 `archive_presence` 的密码；这两项也应与预期服务器密码一致。其他角色的密码轮换需单独安排，不混入本次升级。

### 8.3 统一迁移入口

本次没有归档库时也直接执行下面命令；脚本会创建独立归档容器和卷，空卷初始化角色/schema，再建全部表。**无需先进入原业务库创建quality库，也无需手工运行CREATE DATABASE或逐个粘贴SQL。** 不运行本地测试库导入。

```bash
cd /opt/cs
bash quality/deploy/migrate.sh /opt/cs
if [ "$?" -ne 0 ]; then
  printf '%s\n' '迁移失败，停在第八节，不启动应用。'
else
  printf '%s\n' '迁移命令成功，继续执行8.4核对。'
fi
```

脚本会：

1. 确认初始化文件存在，将初始化目录/脚本分别设为755/644，再校验两份生产 Compose 配置。
2. 只启动 `archive-postgres`，等待数据库健康；首次空卷自动初始化。
3. 将本次完整迁移目录复制到容器内新建的临时目录。
4. 按编号依次执行尚未应用的 SQL，并核对已应用文件 SHA256。
5. 验证 schema、证据只追加权限、任务权限与指定角色登录，再清理临时目录。

首次执行应看到初始化和 `Applying 001_capture.sql`至 `Applying 010_round_replay_windows.sql`，最终出现 `Quality database migration and verification passed.`。再次运行应核对后跳过已应用版本。数据库日志可用 `docker compose --project-directory /opt/cs --env-file .env -f docker-compose.yml -f docker-compose.archive.yml logs --tail 100 archive-postgres`查看；初始化失败时保留当前卷排查，不删卷重跑。

若旧版部署在此处报 `ls: can't open '/docker-entrypoint-initdb.d/': Permission denied`，且主机初始化目录/脚本为700/600，按下方顺序停止归档容器、修正这两处代码权限，再重跑迁移。不需要sudo、不删除卷，不调整原业务数据库：

```bash
(
  set -euo pipefail
  cd /opt/cs
  docker compose --project-directory /opt/cs --env-file .env \
    -f docker-compose.yml -f docker-compose.archive.yml stop archive-postgres
  chmod 644 deploy/archive/init/01-archive-roles.sh
  chmod 755 deploy/archive/init
  bash quality/deploy/migrate.sh /opt/cs
)
```

成功后继续8.4。旧版 `sync-code.sh`再次同步会恢复源码中的700/600；在第九节重新同步前，须先让源码中的初始化目录/脚本也具备755/644权限，或更新到包含本次权限修复的发布脚本。Git方式可执行下列命令；只修改文件权限，不修改已应用SQL：

```bash
(
  set -euo pipefail
  archive_source_dir=$(cat /opt/cs/deployment-records/archive-source-dir.txt)
  test -n "$archive_source_dir"
  test -f "$archive_source_dir/deploy/archive/init/01-archive-roles.sh"
  chmod 644 "$archive_source_dir/deploy/archive/init/01-archive-roles.sh"
  chmod 755 "$archive_source_dir/deploy/archive/init"
)
```

| 迁移 | 内容 |
| --- | --- |
| `001_capture` | 原始事件、消息/会话快照、附件元数据、持久化任务与迁移历史 |
| `002_query` | 用户、身份、轮次、消息、坐席、评价查询索引与评价观察记录 |
| `003_assignment_writer` | ICS 成功分配记录的独立受限追加角色 |
| `004_agent_presence` | 状态样本、坐席登记、监管范围及区间，独立 presence 角色 |
| `005_presence_retention` | 状态历史受限过期清理基础与索引；保留原文件 |
| `006_presence_retention_state` | 实际采样时间至少 45 天、数据库每日 04:00 截止点、持久化每日清理预算 |
| `007_presence_agent_management` | 本地坐席删除标记与受限权限；保留历史，新采集发现现有坐席时自动恢复 |
| `008_round_reviews` | 每轮人工质检结果与文本备注；永久追加修改版本，独立于轮次索引重建，导出最新备注 |
| `009_dual_round_reviews` | 将AI质检和人工质检拆为两个独立结果；原单项结果归入人工质检，AI默认待定，保留原备注及历史版本 |
| `010_round_replay_windows` | 客户最近50轮重算；完整历史保留，分批建立定位索引并保存可恢复进度 |

本次坐席管理更新必须先执行 `007_presence_agent_management`，再启动更新后的 ICS；已运行至 006 的服务器仍需执行本节迁移，不能只替换后台页面。

新版应用须完成截至 `010_round_replay_windows` 的全部迁移。已完成001–009的服务器新增010，再更新 `quality-worker`；首次建立索引会分批处理历史。不重新初始化数据卷，不新增或轮换密码。更新操作见12.1。

已应用且校验值一致的版本显示跳过。出现 `checksum differs` 时恢复原已应用文件并新增后续迁移，不改历史校验值。每个 SQL 自身为事务，但 001–010 整个过程不是一个总事务；中途失败可能已有前面版本提交，修复后重跑会继续核对和执行。

迁移失败时保持应用进程停止，不继续构建启动；保留完整非密钥错误用于排查。迁移验证中的测试记录在事务回滚，不作为真实业务采集记录。脚本验证 ingest、reader、quality，以及已配置的 presence 密码登录；assignment 的真实同步还需第十一节业务验收。

这里的“迁移”是服务器既有归档库的增量表结构和权限升级，不是把 Windows 测试聊天搬到服务器。原 ICS 数据仍留在原主库，服务器已有归档证据和任务保留；首次建库从启用采集后的新事件开始。维护期漏收按已确认范围处理，本次没有历史补录、测试库导入或全量重建步骤。

### 8.4 只读核对迁移记录

```bash
cd /opt/cs
compose=(docker compose --project-directory /opt/cs --env-file .env \
  -f docker-compose.yml -f docker-compose.archive.yml)
"${compose[@]}" exec -T archive-postgres sh -c \
  'exec psql -X -U archive_admin -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 \
    -c "SELECT version,applied_at FROM ops.schema_migrations ORDER BY version;"'
```

应包含完整 001–010。迁移历史用运维账号读取；不要假定 `archive_reader` 有迁移历史权限。每次新终端重新定义 `compose` 数组；下文数组写法仅适用于 Bash。

再检查本次开发环境曾遗漏的007字段和权限。**版本齐全、字段存在、权限正确三项都要通过**：

```bash
"${compose[@]}" exec -T archive-postgres sh -c \
  'exec psql -X -U archive_admin -d "$POSTGRES_DB" -v ON_ERROR_STOP=1' <<'SQL'
DO $check$
BEGIN
  IF (SELECT count(*) FROM ops.schema_migrations WHERE version IN
      ('001_capture','002_query','003_assignment_writer','004_agent_presence',
       '005_presence_retention','006_presence_retention_state','007_presence_agent_management',
       '008_round_reviews','009_dual_round_reviews','010_round_replay_windows')) <> 10 THEN
    RAISE EXCEPTION '001-010 migrations are incomplete';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='ops'
      AND table_name='agent_presence_registry' AND column_name='hidden_at') THEN
    RAISE EXCEPTION '007 hidden_at column is missing';
  END IF;
  IF NOT has_column_privilege('archive_presence','ops.agent_presence_registry','hidden_at','UPDATE')
      OR has_table_privilege('archive_presence','ops.agent_presence_registry','DELETE,TRUNCATE') THEN
    RAISE EXCEPTION '007 roster management permissions are incorrect';
  END IF;
  IF to_regclass('quality.current_round_reviews') IS NULL
      OR NOT has_column_privilege('archive_reader','quality.current_round_reviews','ai_status','SELECT')
      OR NOT has_column_privilege('archive_reader','quality.current_round_reviews','human_status','SELECT')
      OR NOT has_function_privilege('archive_reader','quality.save_round_review(text,text,text,text,text,bigint)','EXECUTE')
      OR has_function_privilege('archive_reader','quality.save_round_review(text,text,text,text,bigint)','EXECUTE')
      OR NOT has_table_privilege('archive_reader','quality.current_round_reviews','SELECT')
      OR has_table_privilege('archive_reader','quality.round_reviews','INSERT,UPDATE,DELETE,TRUNCATE') THEN
    RAISE EXCEPTION '009 dual round review objects or restricted permissions are incorrect';
  END IF;
END
$check$;
SELECT 'PASS: 001-009, roster permissions and persistent round reviews' AS verification;
SQL
```

必须输出 `PASS`；缺007会使每日时长和分钟时间核对无法读取，还会阻止新状态样本保存。失败时回到8.3处理，不能只刷新页面或反复重启ICS。

缺008或009时新版查询不会就绪，质检备注无法保存，聊天Excel也无法读取质检列。迁移入口的自动验证同时核对009双项保存函数的所有者、受限授权及无公开执行权限。

启用分配同步时再验证 assignment 的 TCP 密码认证，密码由容器已有环境引用，不出现在命令文本或输出中：

```bash
"${compose[@]}" exec -T archive-postgres sh -c \
  'PGPASSWORD="$ARCHIVE_ASSIGNMENT_PASSWORD" psql -X -h 127.0.0.1 \
    -U archive_assignment -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 \
    -c "SELECT current_user,current_database();"'
```

应返回 `archive_assignment` 与实际归档库名；失败时停止并核对服务器配置和角色密码，不能将仅有 schema 权限验证当作认证成功。

## 九、构建与启动三个应用进程

### 9.1 使用统一发布脚本

```bash
archive_source_dir=$(cat /opt/cs/deployment-records/archive-source-dir.txt)
bash "$archive_source_dir/deploy/server/release.sh" \
  --source "$archive_source_dir" --target /opt/cs --with-receiver
```

此脚本会串行锁定发布，重新核对并同步完整代码，执行迁移及验证，构建共享镜像，启动/更新接收器并等待健康，再以 `--no-deps`仅更新ICS。原业务库必须已健康运行，脚本不因主Compose变化连带重建它。第八节已应用版本会核对后跳过。Git来源工作区必须干净；上传来源使用第四节解压的完整源码目录。

接收器必须带 `--with-receiver` 才会更新。脚本成功仅说明对应步骤完成；ICS 仍可能需要模型预热，应等待实际健康检查通过。

若源码此前已完整同步且明确需要直接对运行目录发布，可使用 `/opt/cs/deploy/server/release.sh --target /opt/cs --in-place --with-receiver`；`--in-place` 跳过源码同步，不能用于只有部分文件的新目录。

### 9.2 显式更新解析进程

发布脚本**不会更新 `quality-worker`**，每次相关代码发布都执行：

```bash
cd /opt/cs
compose=(docker compose --project-directory /opt/cs --env-file .env \
  -f docker-compose.yml -f docker-compose.archive.yml)
"${compose[@]}" --profile quality-capture --profile quality-query config --quiet
"${compose[@]}" --profile quality-query up -d --no-deps --force-recreate quality-worker
"${compose[@]}" --profile quality-capture --profile quality-query ps -a
```

所有归档服务器命令显式使用两份 `-f`，避免加载本地 `docker-compose.override.yml`。三个应用进程使用本次共享镜像，独立数据库保持运行。

### 9.3 等待健康并检查日志

ICS加载本地模型需要时间，单次curl失败不能直接认定发布失败。使用有截止时间的轮询；不输出每次连接失败，不无限等待：

```bash
(
set -euo pipefail
source /opt/cs/deployment-records/archive-release-params.sh
archive_wait_health() {
  local archive_url=$1 archive_deadline=$((SECONDS + $2))
  while [ "$SECONDS" -lt "$archive_deadline" ]; do
    if curl --fail --silent --max-time 5 "$archive_url"; then printf '\n'; return 0; fi
    sleep 5
  done
  printf '等待健康超时：%s\n' "$archive_url" >&2
  return 1
}
archive_wait_health "$ARCHIVE_RELEASE_RECEIVER_URL/health" 90
archive_wait_health "$ARCHIVE_RELEASE_ICS_URL/health" 600
)
```

两个接口都HTTP200后，再检查容器、镜像与日志：

```bash
(
set -euo pipefail
cd /opt/cs
compose=(docker compose --project-directory /opt/cs --env-file .env \
  -f docker-compose.yml -f docker-compose.archive.yml)
"${compose[@]}" --profile quality-capture --profile quality-query ps -a
archive_shared_image=$(docker image inspect ics-agent:py310 --format '{{.Id}}')
for archive_service in ics-agent quality-receiver quality-worker; do
  archive_container=$("${compose[@]}" ps -q "$archive_service")
  test -n "$archive_container"
  docker inspect "$archive_container" --format '{{.Name}} {{.Image}} {{.State.Status}}'
  test "$(docker inspect "$archive_container" --format '{{.Image}}')" = "$archive_shared_image"
  test "$(docker inspect "$archive_container" --format '{{.State.Status}}')" = running
done
printf 'PASS: 三个应用容器均运行于本次共享镜像。\n'
"${compose[@]}" logs --tail 100 quality-receiver quality-worker
tail -n 100 /opt/cs/log/uvicorn-admin.stderr.log
)
```

接收器应 HTTP 200、正文 `status=ready`，表示能访问归档库。ICS 应恢复原健康响应。worker 无 HTTP 健康接口，应结合运行状态、日志和真实任务由 `queued` 进入 `completed` 验收，不能仅凭容器 `running` 宣称解析正常。

三个应用容器应都为 `running`且镜像ID等于本次共享镜像，不得出现 `Restarting`、`Exited`或混用旧镜像。日志检查迁移缺失、权限、密码、导入异常。健康超时则先排查日志和模型路径，不进入业务验收；完整备份可用时按第十四节回退。本次执行3.3跳过备份时，停在失败步骤排查，不能执行依赖完整备份的回退命令。

核对原业务库未被本次发布替换，以下两行应相同。记录文件由新版3.2或3.3创建，不依赖备份目录：

```bash
(
set -euo pipefail
cd /opt/cs
test -s deployment-records/business-container-before.txt
cat deployment-records/business-container-before.txt
compose=(docker compose --project-directory /opt/cs --env-file .env \
  -f docker-compose.yml -f docker-compose.archive.yml)
archive_business_db=$("${compose[@]}" ps -q postgres)
test -n "$archive_business_db"
archive_business_after=$(docker inspect "$archive_business_db" --format '{{.Id}} {{.Config.Image}} {{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}} {{.Source}}{{end}}{{end}}')
printf '%s\n' "$archive_business_after"
test "$archive_business_after" = "$(cat deployment-records/business-container-before.txt)"
printf '%s\n' 'PASS: 原主库容器、镜像和数据卷标识一致。'
)
```

若日志目录由服务器原配置改名，使用原 `UVICORN_LOG_DIRECTORY`及stderr文件名，不修改现有日志配置来迎合示例路径。

修改 `.env` 后必须重新创建需要该配置的容器；普通 `restart` 不刷新已注入的环境变量。只改归档 Secret/接收绑定时重建接收器，只改导出/查询/状态/分配配置时重建 ICS，只改解析配置时重建 worker；同时修改则更新对应全部进程。

## 十、客服状态保留期及持久化清理

### 10.1 本版规则

- 以 `sampled_at` 的**实际采样时间**保留至少 45 个完整天数，不能按“当天加前 44 个日期”提前删除尚不足 45 天的观察。
- 使用数据库时间和北京时间；每日 04:00 后允许清理，截止点为“当日北京时间 04:00 减 45 天”。严格早于截止点的样本才可进入该日清理；刚好等于截止点的观察保留。
- `ops.agent_presence_cleanup_state` 按来源与账号保存任务日期、截止点、已提交批数、删除累计数和状态。进程重启不重置当日预算。
- 每批最多删除 2000 条过期样本和 2000 条已关闭、完全过期的监管区间；每天每来源/账号最多 200 个已提交批次。开放区间和跨截止点区间保留。
- 采集后台复用现有单连接、单执行线程，正常分钟处理后最多推进一批；数据库停机、采集进程停机或每日限额耗尽可能延后清理，不保证 04:00 瞬间删完。
- 批次通过 `ops.run_presence_cleanup(text,bigint)` 执行，删除和进度在同一事务提交。兼容旧清理入口也受同一持久化预算约束，不通过重启或直接调用绕开。
- 不需要额外 Linux cron；需要 ICS 的 presence 后台启用并运行。`archive_presence` 仅有受限函数执行权限，不能直接 DELETE 状态表，也不能清理聊天原始证据。

例如 10 月 16 日 04:00 的清理截止点为 9 月 1 日 04:00；9 月 1 日 03:59:59 的观察可以进入清理，04:00:00 的观察保留。受每日时点、分批限额及故障影响，物理保留可能超过 45 天，不会为凑足当天清理量删除未到期数据。

### 10.2 检查数据库计划与结果

仅查询，不在生产验收中人为调用清理函数删除数据：

```bash
"${compose[@]}" exec -T archive-postgres sh -c \
  'exec psql -X -U archive_reader -d "$POSTGRES_DB" -v ON_ERROR_STOP=1' <<'SQL'
SELECT source_key,account_id,cleanup_day,cutoff_at,batches,
       deleted_samples,deleted_periods,status,last_success_at
FROM ops.agent_presence_cleanup_state
ORDER BY source_key,account_id;
SQL
```

上线后尚未到当天允许执行时点，或采集后台尚未推进时，表内没有当前账号记录不等于迁移失败。数据库持久状态为 `pending`、`completed`、`limited`；后台接口另会显示 `waiting`、`unavailable` 等执行状态。`limited` 表示当天预算已用完，次日继续；`completed` 表示该日截止点下本轮检查已完成。

04:00前的 `collector.cleanup` 为 `waiting`，显示当天即将执行的目标截止点及零个当日批次；历史接口的 `retention_cutoff_at` 仍使用前一个已经到达的04:00边界，二者此时可能不同。该显示不会清空数据库中前一天的进度，也不会提前删除记录。

后台 `/admin/api/quality/presence` 返回 `collector.cleanup`，包括状态、日期、批数、删除累计、截止点与最近成功时间。检查这些信息并与计划表核对，不通过改服务器日期、伪造旧观察或直接改计数表进行生产验收。

状态历史查询会标记超出可用范围的日期/分钟，不把过期、未监控或缺采样解释为离线。实际最早边界按返回的 `retention_cutoff_at` 等保留期元数据核对；边界日期可能只有部分分钟有效。每日统计只计已结束分钟，实时状态页面有值也不代表该分钟已持久化。

## 十一、逐项验收并恢复业务

### 11.1 验证 Chatwoot 到接收器的实际网络

先在ICS主机确认9.3健康检查成功，再使用3.1已核对的实际Sidekiq容器检查。此命令不依赖当前目录，也不会误用ICS的Compose：

```bash
source /opt/cs/deployment-records/archive-release-params.sh
docker exec -i "$CW_QUALITY_SIDEKIQ_CONTAINER" ruby -rnet/http - "$ARCHIVE_RELEASE_RECEIVER_URL/health" <<'RUBY'
u = URI(ARGV.fetch(0))
r = Net::HTTP.start(u.host, u.port, open_timeout: 3, read_timeout: 5) { |h| h.get(u.request_uri) }
puts r.code
abort('Receiver health is not 200') unless r.code == '200'
RUBY
```

预期输出200且命令成功。主机可达而Sidekiq不可达时，核对容器路由、实际内网IP、3010绑定和现有防火墙规则。容器名若改过，先修正3.1参数文件再加载。

实际 Webhook 还受 Chatwoot 私网 URL 安全检查影响。如果目标服务器版本拒绝私网投递，在 **Chatwoot Rails 与 Sidekiq** 的环境中核对 `SAFE_FETCH_ALLOW_PRIVATE_NETWORK=true`，按原 Chatwoot 部署流程更新；写在 ICS `.env` 无效。本地版本支持该项不代表服务器已经配置，必须以真实消息投递为准。

### 11.2 验证签名与后台登录保护

回到 `/opt/cs`。下面未签名请求应返回 **401**，不会保存业务事件：

```bash
source /opt/cs/deployment-records/archive-release-params.sh
curl --silent --show-error --output /dev/null --write-out '%{http_code}\n' \
  --header 'Content-Type: application/json' --data '{}' \
  "$ARCHIVE_RELEASE_RECEIVER_URL/quality/webhooks/chatwoot"

curl --silent --show-error --output /dev/null --write-out '%{http_code}\n' \
  "$ARCHIVE_RELEASE_ICS_URL/admin/api/quality/status"
```

第二条未登录归档后台请求也应为 401。不要为了通过检查关闭签名或管理员鉴权。登录继续使用原 `/admin` 账号；不新增独立质检登录系统。

接收器其他预期状态：新合法事件 202，完全相同投递重复接收 200 且同一事件 ID；账号不符 403、无效载荷 400、过大 413、Content-Type 不符 415、数据库写入失败 503。本次以实际 Chatwoot 签名投递验收，不在生产制造数据库故障。

### 11.3 验证真实消息与原文保留

使用现有前端的**专用测试客户和Inbox**，不在真实客户对话中修改或删除消息。按顺序完成：

1. 客户发送 `archive-release-customer-001`，确认机器人正常回复。
2. 按原流程转人工，记录Chatwoot的会话ID和实际接待坐席。
3. 人工坐席发送 `archive-release-check-001`，记录**这条人工消息的ID**，然后发送一条私密备注。
4. 等待接收、解析完成后，编辑上述人工消息为 `archive-release-check-edited-001`；待事件处理完成，再删除同一条测试消息。
5. 记录该账号、会话ID及人工消息ID，供下方精确SQL使用。初始文本、编辑和删除应有独立观察版本；不能只看到最终删除状态就认定原文已归档。

首次上线仅归档接收器就绪后的新消息；原Chatwoot多年历史不会自动出现在ICS中，不能用历史会话为空判断安装失败。

```bash
cd /opt/cs
compose=(docker compose --project-directory /opt/cs --env-file .env \
  -f docker-compose.yml -f docker-compose.archive.yml)
"${compose[@]}" exec -T archive-postgres sh -c \
  'exec psql -X -U archive_reader -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 \
    -c "SELECT id,event_type,account_id,conversation_id,message_id,received_at FROM archive.events ORDER BY id DESC LIMIT 20;"'

"${compose[@]}" exec -T archive-postgres sh -c \
  'exec psql -X -U archive_reader -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 \
    -c "SELECT status,count(*) FROM ops.event_jobs GROUP BY status ORDER BY status;"'
```

应有新事件，任务逐步进入 `completed`。接收器先保存事件与任务，查询索引由 worker 随后生成；原始事件存在而页面暂时为空时，先检查 worker 及任务状态。

精确核对时，下方 `source`填 `.env`中真实 `ARCHIVE_SOURCE_KEY`，`account`填实际账号，`conversation`填测试会话ID，`message`填第3步人工消息ID，四项全部改好后执行。输出仅为测试元数据与布尔值，不打印真实正文：

```bash
"${compose[@]}" exec -T archive-postgres sh -c \
  'exec psql -X -U archive_reader -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 \
    -v source=chatwoot-main -v account=1 -v conversation=123 -v message=456' <<'SQL'
SELECT e.id,e.event_type,e.message_id,e.received_at,j.status,
       octet_length(e.raw_body) AS raw_bytes,e.body_sha256
FROM archive.events e JOIN ops.event_jobs j ON j.event_id=e.id
WHERE e.source_key=:'source' AND e.account_id=:account
  AND e.conversation_id=:conversation
ORDER BY e.id DESC LIMIT 30;

SELECT event_id,snapshot_kind,direction,sender_role,
       content='archive-release-check-001' AS original_test_text
FROM archive.message_snapshots
WHERE source_key=:'source' AND account_id=:account AND message_id=:message
ORDER BY id;
SQL
```

编辑、删除后应仍能找到创建快照的 `original_test_text=true`，并有后续观察版本；不会覆盖原始创建事件。未被 Chatwoot 投递或从未成功接收的旧文本不在此保证范围内。

可只读核对原始事件的运行角色权限：

```bash
"${compose[@]}" exec -T archive-postgres sh -c \
  'exec psql -X -U archive_reader -d "$POSTGRES_DB" -v ON_ERROR_STOP=1' <<'SQL'
SELECT role_name,
       has_table_privilege(role_name,'archive.events','SELECT') AS can_read,
       has_table_privilege(role_name,'archive.events','INSERT') AS can_append,
       has_table_privilege(role_name,'archive.events','UPDATE,DELETE,TRUNCATE') AS can_rewrite
FROM unnest(ARRAY['archive_ingest','archive_reader','archive_quality',
                 'archive_assignment','archive_presence']) AS role_name;
SQL
```

所有角色 `can_rewrite` 应为 false；presence 对聊天原始事件的读取和追加也应为 false。本检查不执行真实修改或删除来试探生产权限。

### 11.4 验证后台归档、责任与导出

浏览器打开3.1中 `ARCHIVE_RELEASE_ICS_URL`加 `/admin`，使用原管理员登录，按 `Ctrl+F5`强制刷新，进入左侧“对话归档”（`#quality`）：

1. “归档可查询”状态正常。在右侧输入测试会话ID，点击“查询归档”，客户列表能找到该客户，累计轮次/消息数合理；重置后筛选保持展开且所有筛选为空。
2. 收件箱对应服务器原Inbox映射，客服与收件箱选择能读取真实名称；不要求页面显示已移除的“来源/账号”“ICS业务渠道”等筛选项。目录有值不能替代归档队列验收。
3. 客户、机器人、人工和私密备注保留真实发送角色；消息版本及原始事件下载可查。
4. 本轮首次已确认 ICS 成功分配的坐席成为责任坐席；后续改派或他人回复不修改既定责任。缺少可靠证据时留空，不按最后回复人猜测。
5. 按现有业务流程解决测试咨询并提交 CSAT，核对评分和文字反馈归属正确轮次。完整历史和单轮详情的评价邀请气泡须直接显示星级、分数及反馈；低于4分用红色突出，未提交时显示待评价。保持窗口打开，评分事件解析入库后应在下一次15秒刷新时更新；核对阅读位置、展开的原文版本和未保存的质检备注不受影响。跨轮补评应留在原邀请处，不套用新轮评分。此项直接读取已有评价索引，无新增数据库迁移。若服务器未启用原有 CSAT 能力，不为本次归档验收擅自改变其原业务开关。
6. 筛选、分页、用户完整历史和单轮详情正常；导出弹窗取消不生成文件，确认后下载 Excel，核对聊天范围、时间、发送角色和会话 ID，首行可筛选。超出本次数量/预算限制时明确提示，缩小范围后可导出。

另检查本次完成的页面改版：筛选位于右侧，概况和紧凑客户卡片在左侧；点击客户时右侧打开完整历史，仍可点击其他客户切换；轮次目录可跳转，机器人和人工气泡颜色可区分。客户卡片按最近一轮展示首响、平均人工响应、最后一句是否人工、总时长、评价及人工阶段客户/人工消息计数。首次测试尚未转人工或评价时显示空值/待回复属于正常状态，不要求凭空出现统计。

列表导出涵盖已应用筛选选中的全部结果分页；轮次或用户被选中后导出其完整已归档消息范围，详情导出独立于列表分页。筛选结果、用户完整历史和本轮导出均只生成一个“聊天记录”工作表，包含客户、发送时间（北京时间）、发送人、发送人类型、聊天内容、会话 ID、消息 ID、消息标记、附件、轮次、AI质检、人工质检和质检备注；后三列对应消息所属轮次的最新已保存记录，未质检轮次为待定、空备注，未归轮消息留空；首行支持筛选并冻结，正文自动换行。文件不包含说明、筛选参数或用户/轮次概况。正文为逻辑消息最新观察内容，编辑/删除历史在页面版本及原始事件中查；附件仅显示文件名和原始链接。

人工质检备注另按下列步骤验收：

1. 打开一轮测试对话，顶部“本轮质检”分别将“AI质检”和“人工质检”都选为“不合格”，输入一段备注，点击“保存备注”，须显示已保存时间与管理员。
2. 滚动消息、翻消息页，备注区保持常驻。切到另一轮填写“合格”及另一段备注，两轮内容相互独立；完整历史顶部选择框也可按轮读取备注。
3. 关闭重开、刷新页面，原结果和备注仍存在。继续发送测试消息并等待解析，原轮备注仍存在，不被索引重建覆盖。
4. 分别导出本轮、客户完整历史、按轮次和按客户的筛选结果，均只有一个“聊天记录”Sheet；核对“轮次”“AI质检”“人工质检”“质检备注”及首行筛选。已下载的旧文件不会自动更新，修改备注后重新导出。
5. 在两个窗口打开同一轮：一个先保存，另一个保存旧版本时应提示已被其他窗口更新，并保留输入；点击重新读取后再修改保存。未保存输入时切换或关闭详情应提示，不能显示虚假的保存成功。
6. 筛选栏分别选择AI质检、人工质检的合格或不合格并查询，再选择两项都不合格；仅匹配同一轮最新保存的结果，不能把不同轮次的两个不合格拼接。待定包含尚未保存结果的轮次。修改并保存结果后重新查询，应按新结果匹配；点击重置应清空两项并保持筛选栏展开。核对概况及筛选导出也沿用相同条件。按客户模式入选的是有匹配轮次的客户，其完整历史和导出仍保留全部轮次。
7. 客户卡片按最近一轮显示AI/人工指示灯，合格为绿灯、不合格为红灯、待定不亮灯；两项独立。按轮次列表及完整历史轮次目录也须显示各轮对应的灯。保存后应立即更新，改回待定应熄灭；保存失败保持原灯色。已质检旧轮次后新开一轮，新轮待定时卡片不沿用旧轮灯色，可从轮次目录查看旧结果。

### 11.5 验证客服状态

浏览器左侧点击“客服状态”（`#presence`），选择北京时间今天和一个测试坐席。首次启动需等待**1–2分钟并跨过一个完整分钟**；仅出现当前在线/忙碌卡片，不能算历史采集正常。

1. `collector.last_saved_at` 持续推进，而不仅是当前目录刷新时间变化。
2. 每日统计、单坐席全天分钟时间轴出现已结束分钟的观察；当前/未来时间单独显示。
3. 在测试坐席上切换在线、忙碌、离线，按后续分钟观察核对，而不是要求切换瞬间反映到历史。
4. 缺采样、未监控、未知与离线区分；关闭浏览器后采集继续运行。
5. 按第十节检查数据库清理状态；尚未到每日 04:00 或无过期记录时，不人为造生产旧数据。
6. 打开“每日状态时长 → 坐席管理”，有已离开Chatwoot的测试旧坐席时删除，确认默认名单不再显示；勾选“查看已删除坐席历史”，核对原统计和时间轴。首次建库没有旧坐席时跳过此项，不删除真实Chatwoot账号来造测试数据。
7. 使用测试坐席验证误删：本地删除后等待下一次新的后台分钟采集，确认 Chatwoot 仍在册的坐席自动恢复、原历史分钟不丢失。删除操作不修改 Chatwoot，当前实时坐席仍按 Chatwoot 名单展示；历史继续遵循45天保留期。

页面无数据时可按 `F12`打开浏览器开发者工具，切到“网络/Network”，刷新当前页，依次点开 `/presence`、`/presence/daily`、`/presence/timeline`，在“预览/Preview”核对：当前状态和历史接口均应 `status=ready`；`collector.last_saved_at`应推进；每日 `agents`有坐席，时间轴 `segments`有区间。不要复制或发送请求中的Authorization令牌。若接口为 `unavailable`，先按8.4检查007字段/权限，再查presence密码和应用日志；不是反复刷新即可解决的问题。

只读检查已保存样本：

```bash
cd /opt/cs
compose=(docker compose --project-directory /opt/cs --env-file .env \
  -f docker-compose.yml -f docker-compose.archive.yml)
"${compose[@]}" exec -T archive-postgres sh -c \
  'exec psql -X -U archive_reader -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 \
    -c "SELECT source_key,account_id,agent_id,sampled_minute,sampled_at,availability_status FROM archive.agent_presence_samples ORDER BY sampled_minute DESC LIMIT 20;"'
```

分钟观测不是精确考勤；采样间隙不能证明真实上下线时刻。采集异常不补造离线，也不恢复启用前或停机期间缺失的状态。

### 11.6 验证原 ICS 功能并记录结果

使用原测试渠道复查自动回复、转人工/分配、解决会话、原管理后台、FAQ/知识库查询及已启用的提醒功能；核对原模型、提示词、渠道映射未被替换。与发布前健康基线比较，确认原业务仍可使用后结束维护。

记录发布 SHA 或更新包 SHA256、备份目录（执行3.3时写 `backup=skipped`）、001–009迁移记录、三个进程版本、健康状态、真实消息和页面验收结果、维护开始/结束时间及已接受的漏收窗口。不要保存 Token、Secret、原始客户内容到普通发布记录。

全部通过后才结束维护，并恢复原业务入口/资料导入。至少填写以下发布记录，不填写任何密码：源码SHA或包哈希、8.4 PASS、三容器镜像ID、主库前后标识一致、测试会话/消息ID、客户/机器人/人工/私密消息验证、原文版本保留、单Sheet导出、当前/每日/时间轴验证、坐席误删恢复、原ICS功能验证。任何必验项失败，停在相应步骤修复或按第十四节回退，不将“容器启动”写成“上线验收完成”。

## 十二、后续常规更新与只改配置

后续仍按本手册选 A 上传或 B Git 拉取，备份、同步并保留服务器 `.env`。完整源码准备且服务器归档配置已正确时，核心发布命令为：

```bash
(
set -euo pipefail
source /opt/cs/deployment-records/archive-release-params.sh
archive_source_dir=$(cat /opt/cs/deployment-records/archive-source-dir.txt)
bash "$archive_source_dir/deploy/server/release.sh" \
  --source "$archive_source_dir" --target /opt/cs --with-receiver

cd /opt/cs
compose=(docker compose --project-directory /opt/cs --env-file .env \
  -f docker-compose.yml -f docker-compose.archive.yml)
"${compose[@]}" --profile quality-query up -d --no-deps --force-recreate quality-worker
"${compose[@]}" --profile quality-capture --profile quality-query ps -a
)
```

本段任一步失败会停止后续步骤；成功后仍须执行第9.3节的健康等待、镜像检查和第十一节对应验收，不能跳过模型预热。

只改 `.env` 不需重新构建镜像，但要重建相关容器。例如导出限制修改后：

```bash
cd /opt/cs
compose=(docker compose --project-directory /opt/cs --env-file .env \
  -f docker-compose.yml -f docker-compose.archive.yml)
"${compose[@]}" config --quiet
"${compose[@]}" up -d --no-deps --force-recreate ics-agent
```

若更改 assignment/presence 密码，需要先按实际密码管理流程确认数据库身份与配置一致；迁移入口会为这两个角色设置真实配置值。不要只重建 ICS 后期待数据库自动接受新密码。

关闭后台归档查询可设置 `ARCHIVE_QUERY_ENABLED=false` 并重建 ICS、停止 worker；要同时关闭状态采集和分配同步，还需各自设为 `false`。接收器可独立继续保存原始事件，后续 worker 启动后处理已保存任务。此处处理的是已有持久任务，不代表维护期未接收事件已经补回。

### 12.1 已完成001–007或008后更新双项质检备注

已按前文部署归档库、Webhook和三个应用进程的服务器，本次更新代码并补齐008、009，不重建归档库、不重新创建Webhook，也不更改 `.env`中的既有密码。先在开发端提交并推送这次代码，记录待发布的提交SHA。

服务器Git方式按下列步骤执行；将占位值替换为开发端实际提交SHA：

```bash
(
set -euo pipefail
cd /opt/cs-src
test -z "$(git status --porcelain)"
git pull --ff-only
archive_expected_commit='替换为本次发布提交SHA'
test "$(git rev-parse HEAD)" = "$archive_expected_commit"
test -f quality/database/migrations/010_round_replay_windows.sql
bash deploy/server/release.sh --source /opt/cs-src --target /opt/cs --with-receiver

cd /opt/cs
compose=(docker compose --project-directory /opt/cs --env-file .env \
  -f docker-compose.yml -f docker-compose.archive.yml)
"${compose[@]}" --profile quality-query up -d --no-deps --force-recreate quality-worker
"${compose[@]}" --profile quality-capture --profile quality-query ps -a
)
```

须看到已应用版本的 `unchanged migration skipped`、新增版本的 `Applying`以及 `Quality database migration and verification passed.`。已有001–009时新增010；重复发布时001–010均核对后跳过。脚本会修正初始化代码的755/644权限，保留 `.env`、运行资料、数据库卷及既有聊天记录。

009会将原单项结果保留为“人工质检”，新增“AI质检”为待定，原备注、版本号和保存时间保留。两项均由管理员手动标记，可同时不合格；共用一份本轮文本备注。发布后刷新已打开的后台页面再保存，旧页面的单项请求会被拒绝，避免覆盖双项结果。已应用009的服务器仍需执行010，供最近50轮解析使用。

随后依次执行8.4版本及权限核对、9.3健康与镜像核对、11.4质检备注及Excel验收。第十一节其他已有功能仍须回归检查。服务器无需运行开发测试或导入本地数据库。

## 十三、常见问题与检查顺序

| 现象 | 先检查 |
| --- | --- |
| `config --quiet` 提示缺少 archive 密码 | 运行目录、服务器 `.env`、是否合并归档字段；不要复制本地整份配置 |
| 归档库反复重启，日志提示 `docker-entrypoint-initdb.d`的 `Permission denied` | 初始化目录/脚本是否为700/600；按8.3仅改为755/644并重跑，保留归档卷 |
| 数据库初始化报角色或 schema 已存在 | 是否使用正确归档卷、是否已有手工建设；停止核对，不清库重建 |
| 迁移提示 checksum 不一致 | 发布包是否改了已应用 SQL；恢复历史文件并用新迁移修正 |
| 新版归档查询未就绪、质检备注或导出失败 | 是否已应用008和009；按8.4核对质检视图及受限函数权限，再查看应用日志 |
| 备注保存返回409 | 其他窗口已保存新版本；当前输入保留，重新读取后再修改，不强行覆盖旧版本 |
| DB healthy，但迁移角色登录失败 | 既有密码是否与 `.env` 一致；healthcheck 不验证全部角色密码 |
| receiver 401 | 专用账户 Webhook Secret、签名/时间戳、时钟及是否已重建容器 |
| receiver 403 | 实际 account ID 是否在 `ARCHIVE_ALLOWED_ACCOUNT_IDS` |
| receiver 503 / 不健康 | 独立库、ingest 密码、连接和迁移，不改用主业务库 |
| 主机能访问3010，Chatwoot无事件 | Sidekiq网络、实际URL和订阅、Chatwoot私网限制及其投递日志 |
| Chatwoot API 401/403 | 管理员个人 API Token及该账号权限，不用AgentBot凭据替代 |
| Chatwoot API 404/422 | 账号/版本/API路径、URL、重复URL和五项订阅 |
| 原始事件有，后台没有消息 | worker是否同版运行、任务queued/retry/failed、映射文件及解析日志 |
| `failed` 或持续积压 | 看对应错误、处理预算与日志，保留原始事件，不删任务/证据来消除告警 |
| 目录无姓名、当前状态不可用 | Chatwoot用户级Token、账号、目录接口；与归档库健康分开判断 |
| 当前状态有，每日时长/时间轴为空 | 首先检查007迁移和`hidden_at`字段，再查presence开关/密码、`collector.last_saved_at`是否推进、是否等待完整分钟 |
| 清理显示 limited | 当日200批预算已用完，下一允许日继续；不重置计划表 |
| 清理表暂时无记录 | 006是否应用、当前北京时间、presence后台是否已推进首次清理 |
| 修改配置不生效 | 是否对相应容器执行force-recreate；restart不更新环境 |
| 导出被拒绝或429 | 数量/JSON预算/超时或已有导出任务；缩小范围或等待，不直接扩大资源限制 |

日志只查看必要尾部，不打印容器完整环境、完整 HTTP 签名、原始 Webhook 响应或客户正文。接收器返回失败不能被解释为 Chatwoot 必定会重投，本次不增加补漏步骤。

## 十四、应用回退与数据保护

**本次执行3.3跳过完整备份时，不执行14.2或14.3的备份恢复脚本。** 旧镜像标签、部分dump或未完成的tar不构成本节需要的整套回退材料。

### 14.1 回退原则

优先恢复旧 ICS 镜像、代码和服务器配置；保留原主数据库、归档数据库、数据卷和已应用迁移。**应用回退不自动降级数据库，也不从旧备份覆盖发布后新采集的聊天记录。** 不运行 `docker compose down -v`、不删除 `cs_archive_postgres_data`，不撤销或改写已应用 SQL。

下面回退以先恢复原 ICS 为目标，暂停可选接收器/worker；因此回退窗口也可能漏收，属于已接受边界。若需要恢复升级前的归档接收与解析，确认旧镜像/旧代码支持当前 schema 后再单独恢复，见 14.3。

### 14.2 恢复旧 ICS

确认第 3.2 节备份成功且旧镜像未被清理。以下代码只停止指定应用进程，覆盖已备份代码和 `.env`，不覆盖运行数据、不操作数据库卷：

```bash
(
  set -euo pipefail
  cd /opt/cs
  test "$(cat deployment-records/archive-backup-status.txt 2>/dev/null || true)" != skipped
  test -s deployment-records/last-archive-backup.txt
  archive_backup_dir=$(cat deployment-records/last-archive-backup.txt)
  test -f "$archive_backup_dir/env.backup"
  test -s "$archive_backup_dir/previous-code.tar.gz"
  test -s "$archive_backup_dir/previous-ics-image-id.txt"
  archive_old_image=$(cat "$archive_backup_dir/previous-ics-image-id.txt")
  docker image inspect "$archive_old_image" --format '{{.Id}}'

  for archive_service in ics-agent quality-receiver quality-worker; do
    archive_service_id=$(docker ps -a --filter label=com.docker.compose.project=cs \
      --filter "label=com.docker.compose.service=$archive_service" --format '{{.ID}}')
    if [ -n "$archive_service_id" ]; then
      test "$(printf '%s\n' "$archive_service_id" | wc -l)" -eq 1
      docker stop "$archive_service_id"
    fi
  done

  tar --no-same-owner -xzf "$archive_backup_dir/previous-code.tar.gz" -C /opt/cs
  cp -p "$archive_backup_dir/env.backup" /opt/cs/.env
  chmod 600 /opt/cs/.env
  docker image tag "$archive_old_image" ics-agent:py310
  docker compose --project-directory /opt/cs --env-file .env -f docker-compose.yml config --quiet
  docker compose --project-directory /opt/cs --env-file .env -f docker-compose.yml \
    up -d --no-deps --no-build --force-recreate ics-agent
)
```

然后等待原 ICS 模型预热，检查 `/health`、原客服业务和日志。生产镜像中的代码来自旧镜像；本次回退不通过删除多出来的 `quality/` 等目录影响归档数据，也不改变 `/opt/cs-src` Git 历史。下一次发布前重新选择并记录源码版本，避免把失败版本再次发布。

### 14.3 升级前已有归档进程时

备份中保留了升级前接收器和 worker 的各自镜像 ID，以及原运行服务列表。确认原归档 `.env`、旧两份 Compose、旧镜像与当前数据库向后兼容后，可为它们使用临时镜像覆盖文件，避免误用被回退的 ICS 标签启动不同版本：

```bash
(
  set -euo pipefail
  umask 077
  cd /opt/cs
  test "$(cat deployment-records/archive-backup-status.txt 2>/dev/null || true)" != skipped
  archive_backup_dir=$(cat deployment-records/last-archive-backup.txt)
  archive_release_tag=$(cat "$archive_backup_dir/release-tag.txt")
  test -f docker-compose.archive.yml
  archive_restore_services=()
  archive_rollback_override="$archive_backup_dir/rollback-archive-services.yml"
  printf 'services:\n' > "$archive_rollback_override"
  for archive_service in quality-receiver quality-worker; do
    if ! grep -Fxq "$archive_service" "$archive_backup_dir/running-services-before.txt"; then
      continue
    fi
    test -s "$archive_backup_dir/previous-$archive_service-image-id.txt"
    archive_old_image=$(cat "$archive_backup_dir/previous-$archive_service-image-id.txt")
    archive_old_tag="ics-agent:rollback-archive-$archive_release_tag-$archive_service"
    docker image inspect "$archive_old_image" --format '{{.Id}}'
    docker image tag "$archive_old_image" "$archive_old_tag"
    printf '  %s:\n    image: %s\n' "$archive_service" "$archive_old_tag" >> "$archive_rollback_override"
    archive_restore_services+=("$archive_service")
  done
  test "${#archive_restore_services[@]}" -gt 0
  rollback_compose=(docker compose --project-directory /opt/cs --env-file .env \
    -f docker-compose.yml -f docker-compose.archive.yml -f "$archive_rollback_override")
  "${rollback_compose[@]}" --profile quality-capture --profile quality-query config --quiet
  "${rollback_compose[@]}" --profile quality-capture --profile quality-query \
    up -d --no-deps --no-build --force-recreate "${archive_restore_services[@]}"
)
```

这段仅恢复升级前原本运行且有镜像备份的进程，首次上线没有旧进程时跳过整节。恢复后重新执行健康、队列与真实采集验收。临时覆盖文件只用于这次回退；下一次正常发布仍应统一回到选定的新版本镜像与部署配置。数据库清理兼容入口仍受 006 的每日预算限制，不能因为应用回退而改回或删除 005/006。

### 14.4 数据库备份的使用边界

双库备份用于灾难恢复，不是普通代码回退的一步。若迁移或数据库实际损坏，保留当前卷、日志与新证据，先在独立恢复环境使用对应角色 SQL 和 custom-format dump 验证恢复，再安排停写与正式切换。不要在运行库直接执行 `pg_restore --clean`，也不要把两个库的角色、dump、卷路径混用。

归档库恢复旧备份会失去备份后新增证据；原业务库恢复旧备份会倒退业务状态。此类恢复需单独确定恢复点和数据影响，不能为了尽快显示绿色健康状态覆盖现有数据。

## 十五、可选的独立容器硬资源上限

第六节限制连接、处理量和导出预算，worker 已有容器硬限制。若希望为归档数据库和接收器增加 CPU/内存硬上限，可在容量观测后使用额外 Compose 文件，**不修改原主 Compose 或原 ICS 容器的资源设置**。

例如单独保存为 `/opt/cs/docker-compose.quality-limits.yml`：

```yaml
services:
  archive-postgres:
    cpus: 1.0
    mem_limit: 1g
  quality-receiver:
    cpus: 0.5
    mem_limit: 384m
  quality-worker:
    cpus: 0.5
    mem_limit: 512m
```

这是待按服务器负载验证的起始示例，不代表已在目标服务器测得足够容量。CPU 配额限制对应容器处理速率；达到内存硬上限可能被终止，不会把队列等待变成更大内存保障。主 ICS 导出仍共享其进程，以上文件不提供导出的独立内存硬隔离。

**现有 `migrate.sh` 和 `release.sh` 固定使用两份 Compose，不自动读取此第三份文件。** 因而本手册默认不启用它；如选择使用，应把资源覆盖纳入完整后续发布流程，不能临时加一次就宣称以后永久有效。脚本执行后需重新按三份配置部署相应独立容器，可能重建归档数据库容器，应安排维护并重新验收；不能用不同 Compose 文件交替重建后忽略资源配置变化。

核对合并配置的命令只使用 quiet 模式：

```bash
cd /opt/cs
docker compose --project-directory /opt/cs --env-file .env \
  -f docker-compose.yml -f docker-compose.archive.yml \
  -f docker-compose.quality-limits.yml config --quiet
```

本次主流程先使用已明确的低资源 `.env` 参数和 worker 上限；是否应用这个额外文件，应以目标服务器的内存、CPU、数据库响应和真实采集表现决定，不把它作为未经验证的“零影响”保证。
