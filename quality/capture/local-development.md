# Windows 本地试验

适用当前开发机：Windows PowerShell、Docker Desktop；ICS 为 `D:\project\CS`，Chatwoot 为 `D:\project\chatwoot\services\chatwoot`。归档库已运行且已建表。本说明只启用本地采集服务与专用账户 Webhook，保留原有机器人对接。

| 使用方 | 地址 |
| --- | --- |
| 本机 PowerShell 调用 Chatwoot API | `http://localhost:3000` |
| 本机检查接收服务 | `http://127.0.0.1:3010/health` |
| Chatwoot 容器投递归档事件 | `http://host.docker.internal:3010/quality/webhooks/chatwoot` |

`host.docker.internal` 是当前 Windows Docker Desktop 中容器访问宿主机的地址。已从本地 Sidekiq 容器访问过本机 `127.0.0.1` 上的临时入口，返回 200。此地址用于本地开发，生产仍按服务器实际内网 IP 配置。

## 1. 通过 API 创建本地专用 Webhook

先从 Chatwoot 个人资料页获取账户管理员的个人 API Access Token，并核对浏览器地址 `/app/accounts/数字ID/...` 中的账号 ID。下面 `1` 按实际修改。

在 PowerShell 中执行。Token 隐藏输入；查询、创建结果留在内存，不自动输出 Secret。

```powershell
Set-Location D:\project\CS
$cwBase = 'http://localhost:3000'
$cwAccountId = 1
$archiveUrl = 'http://host.docker.internal:3010/quality/webhooks/chatwoot'
$cwToken = Read-Host 'Chatwoot 账户管理员个人 API Token' -AsSecureString
$cwHeaders = @{ api_access_token = ([System.Net.NetworkCredential]::new('', $cwToken)).Password }
$cwApi = "$cwBase/api/v1/accounts/$cwAccountId/webhooks"

$existing = Invoke-RestMethod -Method Get -Uri $cwApi -Headers $cwHeaders
$matching = @($existing.payload.webhooks | Where-Object { $_.url -eq $archiveUrl })
if ($matching.Count -gt 1) { throw '检测到多条同地址 Webhook，请先核对，未作修改。' }
$requestBody = @{
    webhook = @{
        name = 'ICS local conversation archive'
        url = $archiveUrl
        subscriptions = @(
            'message_created'
            'message_updated'
            'conversation_created'
            'conversation_updated'
            'conversation_status_changed'
        )
    }
} | ConvertTo-Json -Depth 5

if ($matching.Count -eq 1) {
    $result = Invoke-RestMethod -Method Patch -Uri "$cwApi/$($matching[0].id)" -Headers $cwHeaders -ContentType 'application/json' -Body $requestBody
} else {
    $result = Invoke-RestMethod -Method Post -Uri $cwApi -Headers $cwHeaders -ContentType 'application/json' -Body $requestBody
}
$archiveWebhook = $result.payload.webhook
if (-not $archiveWebhook.id -or -not $archiveWebhook.secret) { throw '响应缺少 Webhook ID 或 Secret，停止并核对版本。' }
$archiveWebhook | Select-Object id, url, subscriptions
Remove-Variable cwToken, cwHeaders -ErrorAction SilentlyContinue
```

重复执行会更新同一目标地址的记录，不反复新建。账户级订阅涵盖该账户下所有收件箱，测试请使用专用测试会话。

## 2. 填写本地 ICS 的 .env

在刚才的同一个 PowerShell 窗口，本机查看专用签名值：

```powershell
$archiveWebhook.secret
```

将该值填入 `D:\project\CS\.env`，补充或修改以下五项；保留现有数据库密码。`ARCHIVE_ALLOWED_ACCOUNT_IDS` 填第 1 节的实际账号 ID。

```dotenv
ARCHIVE_SOURCE_KEY=chatwoot-local
ARCHIVE_ALLOWED_ACCOUNT_IDS=1
ARCHIVE_WEBHOOK_SECRET=填写刚才返回的secret
ARCHIVE_RECEIVER_HOST=127.0.0.1
ARCHIVE_RECEIVER_PORT=3010
```

`ARCHIVE_SOURCE_KEY` 是本地 Chatwoot 实例的稳定标识，开始采集后保持不变。Secret 只在本机配置，不发到聊天或提交到仓库。创建 Webhook 到接收服务启动之间不算正式采集，先完成配置再发送测试消息。

## 3. 启动接收服务

当前本地已有 `ics-agent:py310` 镜像、运行中的 `archive-postgres` 和建好的归档表。附加的 `quality/deploy/compose.local.yml` 将最新 `quality/` 及共享 `ics_agent/` 代码挂载进去并自动重载，因此此次不必重建机器人镜像。

```powershell
Set-Location D:\project\CS
docker compose --env-file .env -f docker-compose.yml -f docker-compose.archive.yml -f quality/deploy/compose.local.yml --profile quality-capture up -d --no-deps --wait --wait-timeout 60 quality-receiver
Invoke-RestMethod -Uri 'http://127.0.0.1:3010/health'
```

预期返回 `status: ready`。本命令只启动或更新接收服务；`--no-deps` 要求归档库已经运行。若数据库还没启动、表未创建，先按[数据库说明](../README.md)完成准备。

确认后，从 Chatwoot 任务容器检查入口。以下通过标准输入传入 Ruby 代码，兼容部分 PowerShell 版本会丢失原生程序参数中双引号的情况；请整段复制，`@'` 与结束的 `'@` 按示例放在单独的行首：

```powershell
Set-Location D:\project\chatwoot\services\chatwoot
@'
u = URI("http://host.docker.internal:3010/health")
r = Net::HTTP.start(u.host, u.port, open_timeout: 3, read_timeout: 5) { |h| h.get(u.request_uri) }
puts r.code
'@ | docker compose -f docker-compose.yaml exec -T sidekiq ruby -rnet/http
```

预期 200。当前本地 Compose 已为 Rails 和 Sidekiq 设置 `SAFE_FETCH_ALLOW_PRIVATE_NETWORK=true`；若更换本地配置，需确保此项在这两个 Chatwoot 容器中生效。

修改 Python 源码会自动重载。修改 `.env` 后则需要重新执行启动命令以重建环境；普通 `restart` 不会刷新已有容器环境变量。

## 4. 发送消息并核对

在本地 Chatwoot 的测试会话中按顺序操作：

1. 客户发送 `archive-local-test-001`。
2. 观察机器人回复，再由人工回复一条测试消息。
3. 编辑一条测试消息，再删除该测试消息，记录它的消息 ID。

然后查询归档元数据与任务状态：

```powershell
Set-Location D:\project\CS
docker compose --env-file .env -f docker-compose.yml -f docker-compose.archive.yml exec -T archive-postgres psql -U archive_reader -d ics_quality -c "SELECT e.id,e.event_type,e.account_id,e.conversation_id,e.message_id,e.received_at,j.status FROM archive.events e JOIN ops.event_jobs j ON j.event_id=e.id WHERE e.source_key='chatwoot-local' ORDER BY e.id DESC LIMIT 20;"
```

数据库名如不是 `ics_quality`，替换为实际值。预期有客户、机器人、人工的 `message_created`，以及编辑/删除产生的 `message_updated`。仅启用捕获时任务为 `queued`；按[归档查询说明](../query/README.md)应用 `002_query` 并启用 `quality-worker` 后，任务应逐步变成 `completed`。解析进程停止期间捕获可继续，原始事件落库后排队。

要检查原文与修改/删除版本，在下面 SQL 中将 `123` 换成测试消息的实际 ID：

```powershell
docker compose --env-file .env -f docker-compose.yml -f docker-compose.archive.yml exec -T archive-postgres psql -U archive_reader -d ics_quality -c "SELECT id,event_type,payload->>'content' AS content,received_at FROM archive.events WHERE source_key='chatwoot-local' AND account_id=1 AND message_id=123 ORDER BY id;"
```

账号 ID 也按实际修改。应能看到最初内容仍在，后续版本另行追加。具体事件数量取决于 Chatwoot 实际发送的通知；删除观察只有源端发出后才会出现。附件目前只归档链接，不复制文件。启用查询后可在本机 `http://127.0.0.1:3001/admin` 的“对话归档”查看用户历史、接待轮次、坐席、收件箱、版本并导出 Excel。

如果没有入库，先看接收服务日志中的签名、账号、数据库等错误，并在 Chatwoot Sidekiq 日志核对实际投递。不要把完整客户消息或 Secret 贴到日志输出中。

```powershell
docker compose --env-file .env -f docker-compose.yml -f docker-compose.archive.yml -f quality/deploy/compose.local.yml --profile quality-capture logs --tail 50 quality-receiver
```

当前 Chatwoot 账户级 Webhook 失败不会自动重试；先确认服务 ready 和容器侧连通，再开始测试。

## 5. 暂停试验

当前窗口执行以下 API 请求会删除刚才的本地专用订阅。重新提示输入管理员 Token，不修改原有 ICS AgentBot；归档数据保留。

```powershell
$cwToken = Read-Host 'Chatwoot 账户管理员个人 API Token' -AsSecureString
$cwHeaders = @{ api_access_token = ([System.Net.NetworkCredential]::new('', $cwToken)).Password }
Invoke-RestMethod -Method Delete -Uri "$cwApi/$($archiveWebhook.id)" -Headers $cwHeaders
Remove-Variable cwToken, cwHeaders -ErrorAction SilentlyContinue
Set-Location D:\project\CS
docker compose --env-file .env -f docker-compose.yml -f docker-compose.archive.yml -f quality/deploy/compose.local.yml --profile quality-capture stop quality-receiver
```

停止后原有 ICS、Chatwoot 和归档数据库继续运行；不执行 `down -v`，不删除数据库卷。
