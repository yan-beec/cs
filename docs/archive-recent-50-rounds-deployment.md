# 本次归档优化上线步骤：SSH ics，从 Git test 分支发布

本手册沿用你的现有方式：**本地把此次改动提交到 `test` 并推送，服务器 `/opt/cs-src` 拉取远端 `test`，再同步到运行目录 `/opt/cs`。**

每个编号单独执行。确认本步成功，再继续下一步；出现错误就停在当前编号。不要把整章命令一次粘贴执行。本文把同步、迁移、权限验证、构建、三个应用容器更新分开执行，便于定位问题。

## 本次改动和操作约定

- 日常重算同来源、同账号客户的最近 50 轮，旧聊天、消息版本、历史轮次和评价继续保留并可查询。
- 首次建立索引、旧消息修复和显式重建分批执行，保存可恢复进度。
- 新增迁移 `010_round_replay_windows.sql`；20000 事件、64 MiB 用于单个解析批次，过大时自动缩小本批轮数，单轮本身超限仍触发保护。
- “轮”沿用已有规则：已解决后的新客户消息，或客户消息间隔达到原有空闲阈值时开启新轮，不是每一问一答就算一轮。

| 项目 | 本手册采用的值 |
| --- | --- |
| 本地目录 | `D:\project\CS` |
| 当前开发分支 | `try`；本次改动目前尚未提交 |
| 实际发布分支 | `test` |
| 本地 Git 远端 | `chatwoot`，已核对 |
| SSH 登录 | `ssh ics` |
| 服务器 Git 源码目录 | `/opt/cs-src` |
| 服务器运行目录 | `/opt/cs` |
| 服务器实际配置 | `/opt/cs/.env` |
| Docker Compose 项目 | `cs` |
| 共享应用镜像 | `ics-agent:py310` |

本文针对已经运行归档服务、已有数据库的服务器。沿用原 Webhook、密码、模型、渠道和数据卷。下面的 `tar` 只用于服务器本地备份。

Windows 命令在 **本机 PowerShell** 执行；登录后命令在 **服务器 Bash** 执行。文档修订没有执行提交、推送或生产发布。

## 1. 本地：把本次改动放到 test 并推送

如果你已经通过平时的 Git 操作把本次改动提交、合入并推送到 `test`，直接从 **1.12 核对发布版本**开始。

### 1.1 进入项目目录

```powershell
Set-Location D:\project\CS
```

### 1.2 查看当前分支

```powershell
git branch --show-current
```

当前应为 `try`。本节先在当前开发分支提交，再把这次提交放到 `test`。

### 1.3 查看待提交文件

```powershell
git status --short
```

核对都是本次归档优化和文档。新增文件应包含 `replay.py`、`windows.py`、010 迁移及对应测试。不要提交实际 `.env`、密码、模型和临时测试数据。

### 1.4 检查差异格式

```powershell
git diff --check
```

预期没有差异错误。不要批量转换或重新保存已经应用的 001—009 SQL，文件字节改变会导致迁移校验失败。

### 1.5 暂存本次涉及的目录和配置

```powershell
git add -- quality docs .gitattributes .env.example docker-compose.archive.yml deploy/archive/archive.env.example deploy/server/bot.env.example
```

这是一次暂存操作。下一步检查实际清单，不把目录中的无关改动一并发布。

### 1.6 核对暂存清单

```powershell
git diff --cached --name-only
```

只应包含准备发布的代码、配置示例、迁移、测试和文档。有无关文件时先从暂存区移除，不继续提交。

### 1.7 提交本次优化

```powershell
git commit -m "feat: replay recent 50 archive rounds"
```

必须提交成功。若提示冲突、没有暂存文件或提交失败，停在此处核实。

### 1.8 记录本次开发提交号

```powershell
$round50ChangeCommit = (git rev-parse HEAD).Trim()
Write-Output $round50ChangeCommit
```

保存这个 SHA。同一 PowerShell 窗口会保留该变量；换窗口后需将变量重新设为这次提交的完整 SHA。

### 1.9 确认工作区已干净

```powershell
git status --short
```

预期没有输出。若还有未处理修改，先保存、提交或按你原有 Git 流程处理，不带着未处理修改切分支。

### 1.10 切到发布分支 test

```powershell
git switch test
```

预期切换成功或显示已在 `test`。不要使用强制切换。

### 1.11 更新 test，再移入这次提交

先单独执行：

```powershell
git pull --ff-only chatwoot test
```

成功后，再单独执行：

```powershell
git cherry-pick $round50ChangeCommit
```

当前 `try` 还包含本次优化之外的提交，所以这里仅复制刚创建的归档优化提交。若你已通过其他方式把这次改动合入 `test`，不要重复 cherry-pick。

若出现冲突，停在本步逐个解决，再用 `git cherry-pick --continue` 完成。尤其核对 `.gitattributes` 中 010 迁移的 `text eol=lf` 规则；不要用自动覆盖一方、强制重置等方式略过冲突。

### 1.12 核对实际发布文件

```powershell
Get-Item quality/processing/replay.py, quality/processing/windows.py, quality/database/migrations/010_round_replay_windows.sql
```

三个文件都应存在。然后单独确认分支：

```powershell
git branch --show-current
```

必须是 `test`。

### 1.13 在发布分支运行归档回归

```powershell
& 'C:\Users\lenovo\Anaconda3\envs\cs\python.exe' -m pytest quality/tests -q --basetemp ("tmp/pytest-round50-" + (Get-Date -Format yyyyMMddHHmmss))
```

应为零个 failed、零个 error。数据库专用条件未启用时会有 skipped，不等于再次完成数据库联调。不要将开发数据库测试指向生产库。

此前已在一次性测试库验证了累计 21060 条事件、超过 64 MiB 仍可继续归档；服务器无需制造大量测试事件。

### 1.14 推送 test

```powershell
git push chatwoot test
```

必须推送成功，不使用强制推送。

### 1.15 记录服务器应部署的 SHA

```powershell
git rev-parse HEAD
```

保存这里的完整 SHA，服务器第 4.7 步要逐字核对。**使用推送后的 `test` SHA；它可能与 1.8 的 `try` 提交 SHA 不同。**

## 2. 登录服务器并核对原服务

### 2.1 在本机 PowerShell 登录

```powershell
ssh ics
```

从下一步起，全部在服务器 Bash 执行。

### 2.2 进入运行目录

```bash
cd /opt/cs
```

### 2.3 定义本次命令缩写

```bash
c50() {
  docker compose --project-directory /opt/cs --env-file /opt/cs/.env \
    -f /opt/cs/docker-compose.yml -f /opt/cs/docker-compose.archive.yml \
    --profile quality-capture --profile quality-query "$@"
}
```

后面的 `c50` 就是以上完整 Compose 命令。它固定使用服务器两份 Compose，不加载本地开发用的 override。**重新登录 SSH 后需再次定义此函数。**

### 2.4 查看现有服务

```bash
c50 ps -a
```

应有 `postgres`、`archive-postgres`、`ics-agent`、`quality-receiver`、`quality-worker`。原服务应正常运行，双库 healthy。若原来就异常，先处理，不继续更新。

### 2.5 查看空间

```bash
df -h /opt/cs "$HOME"
```

确认足够容纳数据库备份、旧代码和新镜像。

### 2.6 核对当前配置能被读取

```bash
c50 config --quiet && echo 'PASS: Compose配置有效'
```

必须显示 PASS。不直接输出整份配置，以免密码出现在终端记录中。

### 2.7 检查当前 ICS

```bash
c50 exec -T ics-agent python -c 'import os,urllib.request; print(urllib.request.urlopen("http://127.0.0.1:"+os.environ.get("PORT","3001")+"/health",timeout=10).status)'
```

预期 `200`。

### 2.8 检查当前接收器

```bash
c50 exec -T quality-receiver python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:3010/health",timeout=10).status)'
```

预期 `200`。本次沿用已有 Webhook 和签名，不重新创建。

## 3. 保存回退资料并暂停旧 worker

本阶段只暂停 worker，ICS 和接收器继续运行，新事件先保存到队列。备份期间暂停后台资料导入、FAQ 和渠道资料编辑。

### 3.1 设置本次备份目录

```bash
ROUND50_STAMP=$(date +%Y%m%d-%H%M%S)
ROUND50_BACKUP="$HOME/cs-backups/round50-$ROUND50_STAMP"
```

### 3.2 创建私有备份目录

```bash
umask 077
mkdir -p "$ROUND50_BACKUP" /opt/cs/deployment-records
```

### 3.3 保存路径，供断线后继续

```bash
printf 'ROUND50_STAMP=%q\nROUND50_BACKUP=%q\n' "$ROUND50_STAMP" "$ROUND50_BACKUP" > /opt/cs/deployment-records/round50-release.env
```

重连 SSH 后：先重新定义 2.3 的 `c50`，再执行 `source /opt/cs/deployment-records/round50-release.env`，从中断处继续。不要重新创建一套备份路径覆盖本次记录，也不要 `source /opt/cs/.env`。

### 3.4 备份服务器配置

```bash
cp -p /opt/cs/.env "$ROUND50_BACKUP/env.before"
```

### 3.5 保存旧 ICS 镜像标签

```bash
docker image tag "$(docker inspect "$(c50 ps -q ics-agent)" --format '{{.Image}}')" "ics-agent:round50-before-ics-$ROUND50_STAMP"
```

### 3.6 保存旧接收器镜像标签

```bash
docker image tag "$(docker inspect "$(c50 ps -q quality-receiver)" --format '{{.Image}}')" "ics-agent:round50-before-receiver-$ROUND50_STAMP"
```

### 3.7 保存旧 worker 镜像标签

```bash
docker image tag "$(docker inspect "$(c50 ps -q quality-worker)" --format '{{.Image}}')" "ics-agent:round50-before-worker-$ROUND50_STAMP"
```

以上三个 tag 命令成功通常没有输出；如有报错，停在对应编号。

### 3.8 保存双库容器及数据卷标识

```bash
docker inspect "$(c50 ps -q postgres)" "$(c50 ps -q archive-postgres)" --format '{{.Name}} {{.Id}} {{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}} {{.Source}}{{end}}{{end}}' > "$ROUND50_BACKUP/databases.before.txt"
```

### 3.9 备份运行代码

```bash
tar --exclude='__pycache__' --exclude='*.pyc' --exclude='knowledge_base_in/output' --exclude='knowledge_base_in/vector_store' --exclude='reminder/build' --exclude='reminder/dist' \
  -czf "$ROUND50_BACKUP/code.before.tar.gz" -C /opt/cs ics_agent quality statistics knowledge_base_in reminder deploy docs Dockerfile docker-compose.yml docker-compose.archive.yml .dockerignore requirements.txt .env.example
```

这是一次服务器本地备份操作，不用于上传发布。若目录缺失、文件变化、无权限或空间不足，停在本步处理，不忽略归档错误。

### 3.10 检查代码备份可列出

```bash
tar -tzf "$ROUND50_BACKUP/code.before.tar.gz" >/dev/null && echo 'PASS: 代码备份可读取'
```

### 3.11 备份渠道和 FAQ 等原始资料

```bash
tar -czf "$ROUND50_BACKUP/database-files.before.tar.gz" -C /opt/cs database
```

模型、知识库生成目录和数据库卷不属于代码同步的替换目标，仍按你原有运行资料备份流程保留。

### 3.12 停止旧解析进程

```bash
c50 stop -t 120 quality-worker
```

预期 worker 停止；接收器、ICS、双库继续运行。

### 3.13 导出归档数据库

```bash
c50 exec -T archive-postgres sh -c 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' > "$ROUND50_BACKUP/archive.before.dump"
```

### 3.14 检查归档库备份

```bash
c50 exec -T archive-postgres pg_restore --list < "$ROUND50_BACKUP/archive.before.dump" >/dev/null && echo 'PASS: 归档库备份目录可读取'
```

### 3.15 导出 ICS 主数据库

```bash
c50 exec -T postgres sh -c 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' > "$ROUND50_BACKUP/business.before.dump"
```

### 3.16 检查主库备份

```bash
c50 exec -T postgres pg_restore --list < "$ROUND50_BACKUP/business.before.dump" >/dev/null && echo 'PASS: 主库备份目录可读取'
```

### 3.17 标记备份完成

仅在上面所有备份命令成功后执行：

```bash
touch "$ROUND50_BACKUP/BACKUP_COMPLETE"
```

数据库导出为一致性快照；备份后新收到的事件仍在在线库中。常规应用回退不覆盖恢复这份数据库快照，避免丢失后续数据。

若备份失败、且尚未执行后面的同步或配置修改，可用 `c50 start quality-worker` 恢复旧解析进程，再解决备份问题。

## 4. 服务器：从 Git 拉取 test

### 4.1 进入 Git 源码目录

```bash
cd /opt/cs-src
```

### 4.2 确认源码工作区干净

```bash
git status --short
```

预期无输出。有修改时先保存和核实，不用 `reset --hard` 或强制清理绕过。

### 4.3 查看服务器远端名称

```bash
git remote
```

本地远端为 `chatwoot`；服务器远端以本步实际输出为准。

### 4.4 设置本次使用的远端

```bash
ROUND50_REMOTE=chatwoot
```

若服务器实际远端叫 `origin`，只把这一行改成 `ROUND50_REMOTE=origin`，后续命令保持不变。

### 4.5 切到服务器 test 分支

```bash
git switch test
```

必须成功切换或显示已在 `test`。分支不存在或工作区阻止切换时，先核实服务器原有仓库状态。

### 4.6 拉取 test

```bash
git pull --ff-only "$ROUND50_REMOTE" test
```

应成功更新或显示 Already up to date。若提示分叉或无法 fast-forward，停在本步处理，不强制覆盖。

### 4.7 核对本次发布 SHA

```bash
git rev-parse HEAD
```

必须与本地 **1.15 推送后的 test SHA** 一致。不一致时不继续同步。

### 4.8 核对必要文件

```bash
ls quality/processing/replay.py quality/processing/windows.py quality/database/migrations/010_round_replay_windows.sql
```

三个文件都必须存在。最后再单独检查工作区：

```bash
git status --short
```

仍应无输出。

## 5. 同步到运行目录并补配置

拉取只改变 `/opt/cs-src`，不会自动改变 `/opt/cs` 中的运行代码。

### 5.1 同步代码

```bash
bash /opt/cs-src/deploy/server/sync-code.sh /opt/cs-src /opt/cs
```

预期显示 `ICS code and quality migrations synchronized; runtime configuration and data preserved.`。该脚本同步所需代码、`statistics/`、部署文件和服务器 Dockerfile，保留 `.env`、渠道资料、模型、日志及运行数据。

### 5.2 核对运行目录已收到迁移

```bash
ls /opt/cs/quality/database/migrations/010_round_replay_windows.sql
```

### 5.3 编辑服务器原配置

```bash
nano /opt/cs/.env
```

找到下面六项；已有的修改数值，没有的才新增，避免重复定义。**下面是文件内容，不是 Bash 命令：**

```dotenv
ARCHIVE_PROCESSOR_RECENT_ROUNDS=50
ARCHIVE_PROCESSOR_INDEX_BATCH_SIZE=500
ARCHIVE_PROCESSOR_INDEX_BATCH_BYTES=8388608
ARCHIVE_PROCESSOR_POOL_SIZE=1
ARCHIVE_PROCESSOR_MAX_CONVERSATION_EVENTS=20000
ARCHIVE_PROCESSOR_MAX_CONVERSATION_BYTES=67108864
```

`Ctrl+O` 保存，回车确认，`Ctrl+X` 退出。不替换整份 `.env`，不修改密码、Webhook 和 `CHATWOOT_SESSION_IDLE_TIMEOUT_SECONDS`。

### 5.4 查看这六项数值

```bash
grep -nE '^ARCHIVE_PROCESSOR_(RECENT_ROUNDS|INDEX_BATCH_SIZE|INDEX_BATCH_BYTES|POOL_SIZE|MAX_CONVERSATION_EVENTS|MAX_CONVERSATION_BYTES)=' /opt/cs/.env
```

每项应只出现一次，值与上面一致。512 MiB、0.5 CPU 的 worker 容器保护仍保留。

### 5.5 再验证 Compose 配置

```bash
c50 config --quiet && echo 'PASS: 更新后的配置有效'
```

必须看到 PASS。

## 6. 数据库：迁移和权限验证分开执行

旧 worker 保持停止。此阶段不要先启动新 worker。

### 6.1 确认归档数据库就绪

```bash
c50 up -d --no-deps --wait --wait-timeout 120 archive-postgres
```

预期归档库 healthy。此命令不更新 ICS 或 worker。

### 6.2 在数据库容器里创建迁移临时目录

```bash
ROUND50_MIGRATE_DIR=$(c50 exec -T archive-postgres mktemp -d /tmp/ics-round50.XXXXXX)
```

单独查看结果：

```bash
printf '%s\n' "$ROUND50_MIGRATE_DIR"
```

必须显示 `/tmp/ics-round50.` 开头的目录。空值或报错时停在此处。

### 6.3 复制本版数据库文件

```bash
c50 cp /opt/cs/quality/database/. "archive-postgres:$ROUND50_MIGRATE_DIR"
```

### 6.4 执行迁移

```bash
c50 exec -T archive-postgres sh "$ROUND50_MIGRATE_DIR/apply-migrations.sh"
```

预期 001—009 校验后跳过，010 首次显示 `Applying 010_round_replay_windows.sql`；已应用的 010 会校验后跳过。

如有 `checksum differs`，停在本步，从更新前备份核对已应用 SQL 原始字节。不要改迁移记录、清库或批量转换 SQL 换行。

### 6.5 单独执行结构和权限验证

```bash
c50 exec -T archive-postgres sh "$ROUND50_MIGRATE_DIR/verify-schema.sh"
```

必须看到验证 PASS，包括原始证据的追加保护、定位表权限和运行账号登录。样例数据会在事务中回滚。

若迁移成功、验证失败，只修复验证错误后重跑本步，不删除已应用的 010。

### 6.6 核对 010 记录

```bash
c50 exec -T archive-postgres sh -c 'exec psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1' <<'SQL'
SELECT version,applied_at FROM ops.schema_migrations WHERE version='010_round_replay_windows';
SQL
```

必须返回一行。至此数据库阶段通过，才继续构建。

## 7. 分别构建和更新三个应用容器

### 7.1 构建共享镜像

```bash
c50 build ics-agent
```

必须构建成功。只构建 `ics-agent` 即可，接收器和 worker 使用同一个镜像。

### 7.2 先更新接收器

```bash
c50 up -d --no-deps --force-recreate --wait --wait-timeout 90 quality-receiver
```

预期接收器 healthy。失败时先单独看日志：

```bash
c50 logs --tail 100 quality-receiver
```

解决后只重做 7.2，不继续更新 ICS。

### 7.3 核对接收器健康

```bash
c50 exec -T quality-receiver python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:3010/health",timeout=10).status)'
```

必须为 `200`。

### 7.4 更新 ICS

```bash
c50 up -d --no-deps --force-recreate ics-agent
```

### 7.5 等待 ICS 就绪

```bash
c50 exec -T ics-agent python -c 'import os,urllib.request; print(urllib.request.urlopen("http://127.0.0.1:"+os.environ.get("PORT","3001")+"/health",timeout=10).status)'
```

预期 `200`。模型预热可能需要几分钟；刚启动未就绪时，等 10—20 秒后单独重复本步，不必从头发布。约 10 分钟仍未就绪时查看 `c50 logs --tail 100 ics-agent` 及 `/opt/cs/log/` 中的应用日志，停在这里排查。

### 7.6 最后更新 worker

```bash
c50 up -d --no-deps --force-recreate quality-worker
```

本步必须执行。只重启 ICS、只构建镜像或只编辑 `.env`，都不能替代 worker 的重新创建。

### 7.7 核对 worker 实际参数

```bash
c50 exec -T quality-worker python -c 'from quality.processing.config import ProcessingSettings; s=ProcessingSettings(); print(s.processor_recent_rounds,s.processor_max_conversation_events,s.processor_max_conversation_bytes)'
```

预期输出：

```text
50 20000 67108864
```

依次为最近轮数、单批事件预算、单批字节预算。

### 7.8 查看 worker 日志

```bash
c50 logs --tail 100 quality-worker
```

不应持续出现导入、权限、数据库连接或解析错误。没有新任务时日志可能很少，不能仅靠日志行数判断是否运行。

## 8. 上线验收和发布记录

### 8.1 查看所有服务

```bash
c50 ps -a
```

三个应用均应 running，接收器及双库 healthy，无反复重启。

### 8.2 取得新共享镜像 ID

```bash
ROUND50_NEW_IMAGE=$(docker image inspect ics-agent:py310 --format '{{.Id}}')
```

### 8.3 单独检查 ICS 镜像

```bash
test "$(docker inspect "$(c50 ps -q ics-agent)" --format '{{.Image}}')" = "$ROUND50_NEW_IMAGE" && echo 'PASS: ICS使用新镜像'
```

### 8.4 单独检查接收器镜像

```bash
test "$(docker inspect "$(c50 ps -q quality-receiver)" --format '{{.Image}}')" = "$ROUND50_NEW_IMAGE" && echo 'PASS: 接收器使用新镜像'
```

### 8.5 单独检查 worker 镜像

```bash
test "$(docker inspect "$(c50 ps -q quality-worker)" --format '{{.Image}}')" = "$ROUND50_NEW_IMAGE" && echo 'PASS: worker使用新镜像'
```

三个检查都必须显示各自的 PASS。

### 8.6 保存更新后的双库标识

```bash
docker inspect "$(c50 ps -q postgres)" "$(c50 ps -q archive-postgres)" --format '{{.Name}} {{.Id}} {{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}} {{.Source}}{{end}}{{end}}' > "$ROUND50_BACKUP/databases.after.txt"
```

### 8.7 对比双库标识

```bash
diff -u "$ROUND50_BACKUP/databases.before.txt" "$ROUND50_BACKUP/databases.after.txt"
```

本次不改变双库配置，预期没有差异。若有差异，先核对是否仅容器被重新创建；数据卷名称和路径必须与发布前一致。不要在数据卷不同的情况下继续验收，也不要删除任何数据卷。

### 8.8 查看任务推进

```bash
c50 exec -T archive-postgres sh -c 'exec psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1' <<'SQL'
SELECT status,count(*) AS jobs,max(updated_at) AS last_update FROM ops.event_jobs GROUP BY status ORDER BY status;
SQL
```

在自有测试会话发一组消息，再重复本查询：应看到任务完成时间推进，新消息可在后台查到。持续有新消息时，队列不要求始终为零。

### 8.9 查看正在进行的分批计划

```bash
c50 exec -T archive-postgres sh -c 'exec psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1' <<'SQL'
SELECT source_key,account_id,conversation_id,plan->>'indexed' AS indexing_complete,
       plan->>'index_cursor' AS indexed_event_id,plan->>'cursor' AS next_round_no,updated_at
FROM ops.conversation_replay_state WHERE plan<>'{}'::jsonb ORDER BY updated_at DESC LIMIT 20;
SQL
```

有待处理任务时，隔一段时间重复查询，定位事件 ID 或下一批轮号应推进。计划完成后 `plan={}`，会从此列表消失。

首次升级会在有任务的会话上分批建立索引，可能比后续日常处理慢。无新任务的历史会话仍可查已有索引，不会仅因上线而自动重建全库。

### 8.10 浏览器和数据检查

逐项验收：

1. 原 ICS 正常回复、转人工、管理员登录正常。
2. 自有测试会话的新消息已归档，任务最终完成。
3. 长历史客户的早期聊天、消息版本和较早轮次仍可分页查询；轮号不因窗口起点而重新从 1 开始。
4. 旧消息编辑后能更新到原消息；测试评价补评后仍属于原轮次。
5. 旧轮次质检备注、人工接待分类、责任坐席、访客评价和 Excel 导出正常。
6. 已启用的统计、客服状态功能正常。

原始事件数量和累计字节继续增长是正常现象。本次不清理旧历史，不用事件数量下降作为优化验收条件。历史修复期间可短暂出现部分轮次已更新、部分待更新，待该会话任务完成后再判断。

可单独查看 worker 资源：

```bash
docker stats --no-stream "$(c50 ps -q quality-worker)"
```

### 8.11 记录发布 SHA

验收全部通过后，记录：

```bash
git -C /opt/cs-src rev-parse HEAD > /opt/cs/deployment-records/source-commit.txt
```

保留本次备份、旧镜像标签及 `round50-release.env`。以上全部通过，才记录本次上线完成。

## 9. 可选：修复此前已经 failed 的指定会话

新版本不直接把所有旧 failed 任务改成待处理。先查看实际失败范围：

```bash
c50 exec -T archive-postgres sh -c 'exec psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1' <<'SQL'
SELECT e.source_key,e.account_id,e.conversation_id,count(*) AS failed_jobs
FROM ops.event_jobs j JOIN archive.events e ON e.id=j.event_id WHERE j.status='failed'
GROUP BY e.source_key,e.account_id,e.conversation_id ORDER BY failed_jobs DESC LIMIT 20;
SQL
```

只有需要修复某个失败会话时才执行下面步骤，正常上线不要求全库重建。

### 9.1 停止常驻 worker

```bash
c50 stop -t 120 quality-worker
```

### 9.2 重建一个指定会话

以下沿用此前关注的 `chatwoot-main / 账号1 / 会话2081`，执行前替换为失败列表中的真实值：

```bash
c50 run --rm --no-deps quality-worker python -m quality.processing.worker --rebuild --source-key chatwoot-main --account-id 1 --conversation-id 2081
```

有匹配数据且完成时应看到 `Rebuilt conversations=1`。此动作分批重建该会话完整历史，并完成实际处理范围内的 failed 任务；不改写或删除原始证据。

### 9.3 成功后恢复常驻 worker

```bash
c50 up -d --no-deps quality-worker
```

若 9.2 失败，停在该步排查，接收器仍可继续采集。单轮本身超过保护预算时仍可能失败，不通过删除事件或伪造任务完成状态解决。

## 10. 故障定位和回退

| 出错位置 | 先处理什么 |
| --- | --- |
| 本地 cherry-pick 冲突 | 停在 1.11，解决文件冲突后继续，不强制覆盖 |
| 服务器拉取失败或 SHA 不符 | 停在 4.6—4.7，核对 test 是否推送成功及服务器远端 |
| 同步报错 | 停在 5.1，检查目录、权限、脚本 LF 和完整源码 |
| 迁移 checksum 不符 | 停在 6.4，核对已应用 SQL 原始字节，不修改迁移记录 |
| 迁移锁超时 | 确认旧 worker 停稳，待影响迁移的长查询结束后重试 |
| 权限验证失败 | 停在 6.5，核对数据库实际账号及服务器配置，不改用主库凭据 |
| 镜像构建失败 | 停在 7.1，修复构建错误，数据库已提交的010保留 |
| 接收器不健康 | 停在 7.2—7.3，查看该容器日志，不继续更新 ICS |
| worker 参数不正确 | 停在 7.7，核对 .env 和强制重新创建是否执行 |
| 持续 RuntimeError、重启或 OOM | 暂停 worker，核对对应会话和单轮预算，不仅凭累计历史大小判断 |

### 10.1 仅解析异常：先暂停 worker

```bash
c50 stop -t 120 quality-worker
```

保持 ICS、接收器和数据库运行，新事件先入库排队。解决问题后从对应失败步骤重试，再执行 worker 更新和验收。

### 10.2 需要恢复旧应用：确认备份

先恢复本次路径：

```bash
source /opt/cs/deployment-records/round50-release.env
```

再检查：

```bash
test -f "$ROUND50_BACKUP/BACKUP_COMPLETE" && echo 'PASS: 本次备份已完成'
```

必须显示 PASS。下面是应用回退，保留 010 和在线数据库中的新事件，不执行数据库覆盖恢复。

### 10.3 停止三个应用

```bash
c50 stop -t 120 quality-worker quality-receiver ics-agent
```

双库继续运行。

### 10.4 创建代码恢复目录

```bash
mkdir -m 700 "$ROUND50_BACKUP/code-restore"
```

如已存在，先核实是否为此前中断的回退目录，从中断处继续，不删除备份重来。

### 10.5 解出旧代码

```bash
tar -xzf "$ROUND50_BACKUP/code.before.tar.gz" -C "$ROUND50_BACKUP/code-restore"
```

### 10.6 同步回旧运行代码

```bash
bash "$ROUND50_BACKUP/code-restore/deploy/server/sync-code.sh" "$ROUND50_BACKUP/code-restore" /opt/cs
```

### 10.7 恢复旧服务器配置

```bash
cp -p "$ROUND50_BACKUP/env.before" /opt/cs/.env
```

### 10.8 创建旧镜像选择文件

先让 Compose 能读取本次时间戳：

```bash
export ROUND50_STAMP
```

再打开文件：

```bash
nano "$ROUND50_BACKUP/rollback-images.yml"
```

写入下面的 **YAML 文件内容**，保存退出；这里的 `${ROUND50_STAMP}` 保留原样，由 Compose 读取刚导出的变量：

```yaml
services:
  ics-agent:
    image: ics-agent:round50-before-ics-${ROUND50_STAMP}
    pull_policy: never
  quality-receiver:
    image: ics-agent:round50-before-receiver-${ROUND50_STAMP}
    pull_policy: never
  quality-worker:
    image: ics-agent:round50-before-worker-${ROUND50_STAMP}
    pull_policy: never
```

### 10.9 验证回退配置

```bash
c50 -f "$ROUND50_BACKUP/rollback-images.yml" config --quiet && echo 'PASS: 回退配置有效'
```

### 10.10 恢复旧接收器

```bash
c50 -f "$ROUND50_BACKUP/rollback-images.yml" up -d --no-deps --no-build --force-recreate --wait --wait-timeout 90 quality-receiver
```

### 10.11 恢复旧 ICS

```bash
c50 -f "$ROUND50_BACKUP/rollback-images.yml" up -d --no-deps --no-build --force-recreate ics-agent
```

重复 7.5 的健康检查，等待 ICS 为 200。

### 10.12 恢复旧 worker

```bash
c50 -f "$ROUND50_BACKUP/rollback-images.yml" up -d --no-deps --no-build --force-recreate quality-worker
```

检查服务和日志，确认原业务恢复。旧 worker 的原有长会话限制也会恢复，因此此回退用于应急恢复，不代表已经解决本次优化问题。

本次应急回退只恢复运行目录和镜像，`/opt/cs-src` 仍是拉取后的 `test`。后续修复和正常发布仍通过远端 `test` 完成。回退期间操作容器继续携带 `rollback-images.yml`，避免普通 `up` 又切回共享标签上的新镜像；不要运行旧版数据库降级、删除新表或篡改迁移记录。
