# Chatwoot 消息接收

本阶段接收并保存原始 Webhook，生成数据库待处理任务。代码在 `quality/capture/`，复用 ICS 的 FastAPI、psycopg 和 `ics_agent.app.chatwoot.payload.verify_signature`；与机器人共用应用镜像，单独使用采集账号和数据库连接池。

接收地址：`POST /quality/webhooks/chatwoot`，默认端口 3010。`GET /health` 检查数据库可用性。正式部署与 ICS、Chatwoot 位于同一服务器，通过服务器内网 IP 直接访问，网络设置见第 3 节。

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
ARCHIVE_RECEIVER_HOST=172.20.48.12
ARCHIVE_RECEIVER_PORT=3010
```

`172.20.48.12` 沿用现有 ICS 文档中的内网地址示例，请替换为服务器实际内网 IP。账号 ID 使用你的实际值，多个账号用逗号分隔。此入口只接受使用同一配置密钥签名的账号；如果不同账号各有不同密钥，需要分别配置接收实例，不能把不同密钥合并填写。`ARCHIVE_SOURCE_KEY` 标识 Chatwoot 实例，启用后保持稳定。独立采集数据库密码继续使用已有 `ARCHIVE_INGEST_PASSWORD`，不轮换密码。

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

## 3. 同一服务器内网直连

本部署与 ICS 使用相同的服务器内网 IP，接收服务使用独立端口 3010。Chatwoot、ICS 与采集服务均在同一服务器，通过主机内网地址互通，无需域名、证书或新增 Nginx 转发。

现有 ICS 部署文档使用 `172.20.48.12` 作为内网地址示例。以下地址必须换成服务器实际持有的内网 IP，并与原有 ICS 的访问方式保持一致。

| 服务 | 同机内网地址示例 |
| --- | --- |
| Chatwoot | `http://172.20.48.12:3000` |
| 原有 ICS | `http://172.20.48.12:3001` |
| 消息接收 Webhook | `http://172.20.48.12:3010/quality/webhooks/chatwoot` |

在服务器 `/opt/cs/.env` 设置 `ARCHIVE_RECEIVER_HOST=172.20.48.12`、`ARCHIVE_RECEIVER_PORT=3010`，再按第 2 节发布或更新接收容器。该值用于绑定宿主机网卡；容器内部进程监听 `0.0.0.0:3010`，宿主机仅映射到配置的内网 IP。归档 PostgreSQL 继续只在 ICS 的 Docker 网络内访问，不发布宿主机端口。

模板保留 `127.0.0.1` 用于初始本机检查，正式内网直连时必须改为实际内网 IP。Chatwoot 容器中的 `127.0.0.1` 是该容器自身；两个 Compose 项目也不会自动拥有相同 Docker 网络，因此此次统一使用主机内网 IP。`ARCHIVE_RECEIVER_HOST` 填 IP，不带 `http://`、端口或路径。

启动后，先在服务器检查：

```bash
curl --fail --max-time 10 http://172.20.48.12:3010/health
```

再到已有 Chatwoot 部署目录，从实际发送 Webhook 的 Sidekiq 容器检查，服务名按服务器实际值修改：

```bash
docker compose exec -T sidekiq ruby -rnet/http -e 'u=URI("http://172.20.48.12:3010/health"); r=Net::HTTP.start(u.host,u.port,open_timeout:3,read_timeout:5) { |h| h.get(u.request_uri) }; puts r.code'
```

两处预期均为 HTTP 200。若主机成功、Sidekiq 失败，检查容器到主机内网 IP 的路由、端口映射及现有主机防火墙规则。同一台服务器上的访问仍需经过实际容器网络验证。

内网 Webhook 还受 Chatwoot 的 URL 安全检查控制。当前本地部署已为 Rails、Sidekiq 配置 `SAFE_FETCH_ALLOW_PRIVATE_NETWORK=true`；服务器是否配置需单独核对。当前版本若因私网限制拒绝投递，应在 **Chatwoot 侧**配置该项并按原部署流程重建 Rails、Sidekiq；写入 ICS 的 `.env` 不生效。详情沿用[ICS 首次部署手册第 10B 节](../../docs/server-first-deployment.md#10b-无域名的私网测试方式)。

然后把专用账户级 Webhook 的目标 URL 设置为上表中的 3010 地址，保留原有 ICS 机器人 Webhook 的 3001 地址。内网接收仍强制校验专用账户 Webhook 的签名 Secret。

接收端口仅绑定实际内网 IP，沿用已有受控内网访问规则，不添加公网端口映射。上述连通检查和真实投递应在服务器执行；本次代码更新不会修改服务器网络或现有 Chatwoot Webhook。

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
