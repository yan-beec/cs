# Chatwoot 消息接收

本阶段接收并保存原始 Webhook，生成数据库待处理任务。代码在 `quality/capture/`，复用 ICS 的 FastAPI、psycopg 和 `ics_agent.app.chatwoot.payload.verify_signature`；与机器人共用应用镜像，单独使用采集账号和数据库连接池。

接收地址：`POST /quality/webhooks/chatwoot`，默认端口 3010。`GET /health` 检查数据库可用性。

## 接收保证

- 强制校验账户级 Webhook 的 `X-Chatwoot-Signature` 和 `X-Chatwoot-Timestamp`，不接受未签名请求；账号必须在配置范围内。
- 不筛掉 outgoing、人工、机器人或私密消息；正文、附件链接等整个原始载荷以字节保存。
- 原始事件与 `ops.event_jobs` 在同一数据库事务中写入，提交后才返回成功。新事件返回 202，相同请求重投返回 200 和相同 event_id。
- 去重键由事件类型及完整请求字节的 SHA256 构成。同一消息的修改、删除载荷另行追加，不覆盖原文。字节不同但语义相同的投递可能各自保留一条证据。
- 数据库或队列写入失败返回 503，不返回假成功；重投不会生成重复任务。签名无效返回 401，账号不符返回 403，载荷无效返回 400，超限返回 413。
- 原始 JSON 的数据库解析直接使用原始字节，避免 Python 浮点转换改变归档数值。请求上限默认 2 MiB。

当前只写 `archive.events` 和 `ops.event_jobs`。消息快照、会话查询索引及附件下载由下一阶段持久化队列工作进程处理；目前任务保持 queued。附件链接已保存在原始载荷中，文件本身尚未独立复制。模型质检不在接收链路执行。

## 1. 配置账户级 Webhook

在 Chatwoot「设置 → 集成 → Webhooks」新建用于归档的账户级 Webhook，订阅 `message_created`、`message_updated`，以及所需的 `conversation_created`、`conversation_updated`、`conversation_status_changed`。不要替换现有 AgentBot 地址。

将新 Webhook 的 Secret 填入服务器已有 `/opt/cs/.env`，并设置：

```dotenv
ARCHIVE_SOURCE_KEY=chatwoot-main
ARCHIVE_ALLOWED_ACCOUNT_IDS=1
ARCHIVE_WEBHOOK_SECRET=填写该账户级Webhook的实际Secret
ARCHIVE_RECEIVER_HOST=127.0.0.1
ARCHIVE_RECEIVER_PORT=3010
```

账号 ID 使用你的实际值，多个账号用逗号分隔。此入口只接受使用同一配置密钥签名的账号；如果不同账号各有不同密钥，需要分别配置接收实例，不能把不同密钥合并填写。`ARCHIVE_SOURCE_KEY` 标识 Chatwoot 实例，启用后保持稳定。独立采集数据库密码继续使用已有 `ARCHIVE_INGEST_PASSWORD`，不轮换密码。

建议先创建 Webhook 获取 Secret、填配置并启动接收服务，确认地址可达后再完成 URL/订阅启用。创建到配置完成期间不声明已开始归档。

## 2. 发布并启动

本机提交、推送，服务器拉取后执行：

```bash
bash /opt/cs-src/deploy/server/release.sh --source /opt/cs-src --target /opt/cs --with-receiver
```

该参数在数据库迁移及镜像构建成功后启动/更新 `quality-receiver`，等待健康检查通过，再更新机器人。后续发布继续带上 `--with-receiver`，以便两个进程使用同版代码。接收服务为可选 Compose profile `quality-capture`，不带此参数的原发布流程不启动或更新它。

接收进程显式仅获取采集库凭据和接收配置，不读取整份 `.env` 作为容器环境，也不获得机器人、维护账号或模型 API 密钥。机器人环境会清空新增的签名密钥。

只启动接收服务时，在已迁移且已构建镜像的运行目录执行：

```bash
cd /opt/cs
docker compose --env-file .env -f docker-compose.yml -f docker-compose.archive.yml --profile quality-capture up -d --no-deps --wait --wait-timeout 60 quality-receiver
```

## 3. 让 Chatwoot 访问入口

同一 Docker 网络可使用 `http://quality-receiver:3010/quality/webhooks/chatwoot`。不同网络时，可通过现有 Nginx 将该精确路径转发到 `http://127.0.0.1:3010`，或把接收服务绑定到 Chatwoot 可访问的服务器内网地址。默认宿主机 127.0.0.1 不能直接被另一个 Docker 网络的 Sidekiq 容器访问。

现有 Nginx 的合适 `server` 块内可加入：

```nginx
location = /quality/webhooks/chatwoot {
    client_max_body_size 2m;
    proxy_pass http://127.0.0.1:3010;
    proxy_connect_timeout 3s;
    proxy_read_timeout 10s;
}
```

Nginx 配置需由服务器现有部署流程应用。本次代码不会更改已启用的 Nginx 配置或自动创建远端 Chatwoot Webhook。

## 4. 验收

在测试会话分别发送客户消息、机器人回复、人工回复，再编辑/删除一条测试消息。通过接收服务日志和只读查询核对每次事件，特别检查原文仍可查到。

```bash
cd /opt/cs
docker compose --env-file .env -f docker-compose.yml -f docker-compose.archive.yml ps quality-receiver
docker compose --env-file .env -f docker-compose.yml -f docker-compose.archive.yml exec -T archive-postgres psql -U archive_reader -d ics_quality -c "SELECT id, event_type, account_id, conversation_id, message_id, received_at FROM archive.events ORDER BY id DESC LIMIT 20;"
```

实际数据库名不为 `ics_quality` 时替换命令中的库名。不要将完整原始载荷或密码贴入日志。

已核对当前本地 Chatwoot 的 `lib/webhooks/trigger.rb`：账户级 Webhook 投递错误会被记录并吞掉，不会像部分 AgentBot 错误一样触发重试。因此接收端 503 只表示未确认接收，不能假定源端会重投。要保证服务中断期间也不漏收，还需要源端持久化投递重试，或可恢复的补录机制；源端在首次成功接收前已删除的原文无法靠接收表恢复。

## 测试

```bash
python -m unittest quality.tests.test_capture
```

数据库集成测试必须显式启用，并仅允许 `quality_capture_test_` 开头的临时数据库和 `capture-test-` 开头的来源。它通过真实 HTTP 检查并发去重、修改/删除证据保留、采集权限及队列写入失败时回滚，不用于生产库。
