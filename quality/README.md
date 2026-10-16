# ICS 对话归档与质检模块

`quality/` 是现有 ICS 项目中的功能模块。后续采集、归档、查询与每日模型质检代码统一在此维护，公共能力继续与 ICS 共享，不建立第二套账号、渠道配置、Chatwoot 客户端或模型调用框架。

当前交付包含数据库迁移、权限、服务器发布集成、原始 Webhook 接收，以及归档后的用户历史、轮次、坐席、评价和消息版本查询。接收代码位于 `quality/capture/`，配置见[消息接收说明](capture/README.md)；新增独立工作进程位于 `quality/processing/`，后台复用 ICS 的 `/admin`。启用和使用步骤见[归档查询首版说明](query/README.md)，解析口径见[处理器说明](processing/README.md)。附件文件复制、历史 API 补录和模型评分为后续步骤。

## 共享边界

- 复用 `ics_agent.app.core.auth` 的管理员认证，后台接口接入现有 FastAPI 应用。
- 复用 `ics_agent.channels` 的渠道配置；未识别来源需要显式标记，不能误归入默认渠道。
- 复用 Chatwoot 客户端和适用的签名校验，单独适配完整双向消息，不沿用机器人只处理入站消息的筛选。
- 复用 psycopg 等已有依赖；归档库使用 `ARCHIVE_*` 配置和单独连接池，不复用现有指向机器人数据库的全局池。
- 模型客户端可以共享，但每日质检有单独的运行窗口、并发、预算与版本。
- 独立的是数据存储和任务执行边界，代码、认证、渠道和通用能力与 ICS 共享。

## 首版表结构

| 分区与表 | 用途 |
| --- | --- |
| `archive.events` | 完整事件载荷、原始字节、摘要、来源、去重键和接收时间 |
| `archive.message_snapshots` | 每次观察到的消息正文及发送人快照，包含创建、更新、删除标记和历史补录 |
| `archive.conversation_snapshots` | 会话属性与状态事件快照 |
| `archive.attachments` | 消息附件的源信息 |
| `archive.attachment_copies` | 已独立保存文件的位置、大小与校验值 |
| `ops.event_jobs` | 已持久化事件的处理、重试和租约状态 |
| `ops.attachment_jobs` | 附件下载、重试和租约状态 |
| `ops.conversation_index` | 可重建的会话查询索引，便于后台按渠道、用户、客服和时间查阅 |
| `ops.customer_index`、`ops.identity_map` | 来源和账号内的客户身份索引，不按同名或手机号自动合并 |
| `ops.round_index`、`ops.round_agent_index` | 会话内的咨询轮次、接待分类、实际回复坐席和分配记录 |
| `ops.message_index` | 逻辑消息查询视图，关联不可变原始版本和所属轮次 |
| `archive.rating_snapshots`、`ops.rating_index` | 不可变评价观察和可重算关联；无法证明归属的评价独立展示 |
| `ops.backfill_jobs` | 历史及遗漏补录范围、进度和重试 |
| `ops.schema_migrations` | 仅维护账号可读写的迁移版本与校验值 |

目前不创建每日模型评分表，后续通过新版本迁移加入已有 `quality` 数据库分区。

## 账号权限

| 账号 | 原始证据 `archive.*` | 当前采集任务与查询索引 | 迁移历史 |
| --- | --- | --- | --- |
| `archive_ingest` | 查询、追加 | 查询、新增、更新；轮次等派生投影允许删除以便事务重建 | 无权限 |
| `archive_reader` | 只读 | 只读 | 无权限 |
| `archive_assignment` | 仅查询、追加事件 | 仅查询、新增事件任务 | 无权限 |
| `archive_quality` | 只读 | 只读 | 无权限 |
| `archive_owner` | 对象所有者，不可直接登录 | 维护 | 维护 |
| `archive_admin` | 受控管理账号 | 维护 | 维护 |

运行账号不能修改、删除、清空原始证据表，也不能建表或提权为对象所有者。质检账号不能更改采集队列；未来质检任务表另行授权。最高权限维护账号仍能维护数据库，不将其凭据交给日常应用。

`003_assignment_writer` 为 ICS 固定责任记录增加独立的 `archive_assignment` 账号，不授予消息快照、派生索引或任务更新权限。启用 `ARCHIVE_ASSIGNMENT_SYNC_ENABLED=true` 前配置独立随机 `ARCHIVE_ASSIGNMENT_PASSWORD` 并执行迁移；迁移入口从环境安全设置该账号密码。ICS 分配成功后先在自己的数据库持久保存按轮唯一的快照，再用受限账号异步追加到归档库，归档失败不阻断客服分配。详见[责任口径与配置](query/README.md)。

## 服务器自动迁移

共享的 ICS 发布入口为 `deploy/server/release.sh`，质检库专用步骤位于 `quality/deploy/migrate.sh`。从源码仓库发布：

```bash
bash /opt/cs-src/deploy/server/release.sh --source /opt/cs-src --target /opt/cs
```

发布会同步代码，等待归档库 healthy，执行迁移和权限验证，再构建、更新 ICS 容器。迁移或验证失败会停止后续步骤；`.env`、业务数据和模型不会被同步覆盖。每次迁移使用独立临时目录，避免混入旧 SQL。完整部署及压缩包更新方式见[统一发布说明](../docs/server-release.md)。

Linux 上只更新归档数据库，可在 ICS 项目根目录执行 `bash quality/deploy/migrate.sh`，不会构建或更新机器人容器。仅拉取代码或直接重启应用不会自动迁移，服务器更新需使用上述发布入口。

## 手动建表和验证

Windows 或 Linux 均从 ICS 项目根目录执行。生产环境先备份已有归档库，并确认 `.env` 对应目标环境。

```sh
docker compose --env-file .env -f docker-compose.yml -f docker-compose.archive.yml cp quality/database/. archive-postgres:/tmp/ics-quality-database
docker compose --env-file .env -f docker-compose.yml -f docker-compose.archive.yml exec -T archive-postgres sh /tmp/ics-quality-database/apply-migrations.sh
docker compose --env-file .env -f docker-compose.yml -f docker-compose.archive.yml exec -T archive-postgres sh /tmp/ics-quality-database/verify-schema.sh
```

迁移在事务内以 `archive_owner` 身份建表，使用锁避免并发执行。重复执行相同迁移会跳过；已执行 SQL 文件发生变化时，校验失败并停止。后续修改通过新增编号 SQL 完成，不能编辑已应用迁移。仓库属性固定 SQL 和 Shell 为 LF，确保不同环境校验一致。

验证在事务中写入样本并回滚，检查事件去重、消息版本追加、来源隔离、实际权限拒绝、任务更新、文件记录和只读查询，不保留测试记录。身份序列可能因验证产生空号，这是 PostgreSQL 的正常行为。

此步骤对已初始化的归档库执行，不依赖清空数据卷，也不重复执行首次账号初始化脚本。

## 后续采集代码约定

1. 原始事件与 `ops.event_jobs` 在同一事务中插入，提交后再确认接收。任务进程从持久化队列领取，不以进程内队列作为唯一任务来源。
2. 事件键包含来源、账号、事件类型以及事件标识、版本或内容摘要。不能仅按消息 ID 去重，否则更新会被误丢弃。
3. 消息身份是“实例来源 + 账号 ID + Chatwoot 消息 ID”。同一消息可以有多个不可变快照；重复同一事件使用 `ON CONFLICT DO NOTHING`，不能使用覆盖更新。
4. 创建事件、修改事件和补录分别保留来源。首次观察已经是修改后内容时，不声明它就是原始发送内容。后台需要按逻辑消息分组展示，不能把各快照误计为多条消息。
5. 附件复制成功并校验实际字节后才追加 `attachment_copies`，下载进度只更新 `ops.attachment_jobs`。不依赖源端链接作为长期文件存储。
6. 任务领取时设置 `processing`、`locked_by`、`locked_until`；完成时清除租约并设置 `completed_at`。租约超时后可以恢复，状态条件由表约束检查。
7. 删除或撤回只追加观察证据。数据库不删除原文；首次接收前已在源端消失的内容仍需源端可靠投递保护，不能由表结构恢复。
