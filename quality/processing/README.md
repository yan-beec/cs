# 独立归档投影工作进程

工作进程只连接归档 PostgreSQL，以 `archive_ingest` 消费已经落库的
`ops.event_jobs`。它不初始化客服应用，不访问机器人数据库，不调用模型，
也不执行附件下载。附件元数据与下载任务会追加保存，文件复制仍是后续阶段。

显式运行：

```sh
python -m quality.processing.worker
python -m quality.processing.worker --once
python -m quality.processing.worker --rebuild --source-key chatwoot-main
python -m quality.processing.worker --rebuild --source-key chatwoot-main --account-id 1 --conversation-id 123
```

Windows 开发使用 `C:\Users\lenovo\Anaconda3\envs\cs\python.exe`。
执行前必须按现有迁移流程应用新增的 `002_query.sql`；没有自动启动或自动改库。
`--rebuild` 会从不可变事件重建选定来源的可变查询投影，并补充缺失快照，
不会覆盖、删除原始事件或历史快照。正在由其他工作进程处理的会话会跳过。
显式重建成功会完成该会话已读取的 failed 任务；其他工作进程持有的 processing
任务仍由其租约管理。

配置读取现有 `ARCHIVE_POSTGRES_HOST/PORT/DB/SSLMODE` 与
`ARCHIVE_INGEST_USER/PASSWORD`，独立配置包括：

| 变量 | 默认值 |
| --- | --- |
| `ARCHIVE_PROCESSOR_POOL_SIZE` | 2 |
| `ARCHIVE_PROCESSOR_LEASE_SECONDS` | 120 |
| `ARCHIVE_PROCESSOR_MAX_ATTEMPTS` | 8 |
| `ARCHIVE_PROCESSOR_POLL_SECONDS` | 2 |
| `ARCHIVE_PROCESSOR_MAX_CONVERSATION_EVENTS` | 20000 |
| `ARCHIVE_PROCESSOR_MAX_CONVERSATION_BYTES` | 67108864 |
| `CHATWOOT_SESSION_IDLE_TIMEOUT_SECONDS` | 21600 |

渠道直接复用 `database/channels.json` 中明确的 account/inbox 映射，未知渠道为
`null`。启动时读取该文件；变更映射后重启并按需要重建投影。

每个持久化任务都有唯一领取令牌和租约；会话级事务锁避免多个进程同时
重建同一会话。成功时，快照追加、查询投影替换、当前任务及已读取范围内
queued/retry 任务的完成在同一事务提交。其他工作进程持有的有效租约不被覆盖。
进程中断后过期租约可恢复；失败按指数退避重试，达到上限标记 failed。
错误记录仅保存异常类型，日志不输出对话正文或凭据。

轮次从具有真实创建时间的公开客户新消息开始；解决后下一条客户消息，
或距上一条客户消息达到空闲阈值，开启新轮。编辑、私密备注、分配、转接
申请、CSAT 卡和活动消息不拆轮。空闲结束记录 `boundary_at`；只有真实
解决事件记录 `closed_at/resolved_at`。缺少创建时间或仅观察到编辑的消息
保留证据并标记未知，不用接收时间冒充发送时间。
状态事件缺少源端更新时间时同样保留证据，不用接收时间关闭当前轮；
评价或会话快照的源时间未知则保存为 `null`，另保留观察时间。
源时间精度无法区分同一时刻的先后时，约定消息按 Chatwoot 消息 ID 顺序
处理，随后处理该时刻的状态观察；不会据此推测额外轮次。

合法新轮建立后立即默认 `reception_type=ai_only`，无需等待 AI 回复或轮次结束。
客户明确要求转人工，或观察到 `handoff_requested/handoff_required=true`，
该轮立即改为 `human`，后续 AI 回复不回退分类。申请仅记
`handoff_result=requested`；明确确认的接管标记或公开人工回复才证明实际接管，
并记 `completed`。单纯分配和私密备注不触发人工分类，也不证明实际接管。
空闲边界只继承已确认的实际接管，申请本身不继承；显式解决后新轮重置。
`classification_finalized` 仅兼容表示轮次已结束，与接待分类无关。
接待类型与回复构成分别保存；系统消息、私密备注和评价卡不计入公开服务回复数。
实际 Chatwoot 未提供确认标记时，以人工公开回复作为可观察的实际接管证据。
首次只观察到更新、但消息创建时间和发送身份明确的消息，可以在已有轮内
按逻辑消息统计一次，并记录 `observed_only_message_count`；不会据此开启或
拆分轮，也不会把编辑后的正文声明为原始发送正文。后续补到创建事件仍只计一次。

每轮责任坐席固定取本轮最早的有效 ICS 原始分配记录；后续 Chatwoot 改派、
其他客服代答、私密备注、机器人和 CSAT 不覆盖责任。只接受独立同步器追加的
`event_type=ics_assignment_recorded/ingestion_kind=backfill` 证据，要求
`assignment.schema_version=1/origin=ics`、匹配的账号和会话、相等且非空的
`assignment_key/business_round_id`、有效坐席和触发消息 ID，以及带明确时区
的 ISO 时间。普通 `assignment_applied` 证据必须提供真实 `assigned_at`。
升级补录 `current_control_backfill` 可以只有 `recorded_at`，此时分配时间保持
`null`；记录时间只用于稳定排序，不冒充历史分配时间。

分配证据在真实消息建轮后通过触发客户消息关联，不按到达时间或当前轮关联。
会话开始时间和已知分配时间必须与触发消息所在轮的时间边界一致；开始时间允许
1 毫秒序列化精度差，已知分配不得早于触发消息或越过真实解决/下一轮边界。
同一分配键的坐席、触发消息、会话开始时间或轮次发生冲突，以及缺少唯一可信
锚点时，相关轮记 `responsibility_basis=assignment_conflict`，责任字段留空。
非法来源、版本、结构或账号/会话不匹配的证据不参与责任计算，原始事件仍保留。
有效证据按真实分配时间排序，同一时刻按归档事件 ID 排序；升级补录无历史时间
时使用记录时间排序。同轮存在多个独立有效分配键时只取最早记录，责任不跨轮继承。

`round.data` 保存 `responsible_agent_id/responsible_agent_name`、
`responsible_assigned_at`、`responsibility_event_id`、
`responsibility_external_round_id`、`responsibility_trigger_message_id`，
依据为 `responsibility_basis=ics_assignment`。姓名只能从归档内相同人类
sender ID 的消息补齐，不能用改派快照替换身份。没有有效 ICS 分配证据时为
`assignment_unobserved`，不使用 Chatwoot `current_assignee_id`、实际回复人
或旧 CSAT `ics_assigned_agent_id` 兜底。

最后实际人工公开 outgoing 服务回复独立保留为 `last_public_agent_id`、
`last_public_agent_name`、`last_public_reply_at`、`last_public_message_id`，
按真实 `sent_at`、同一时刻按 Chatwoot 消息 ID 排序。首次仅观察到更新的真实
人工回复仍可在已有轮内计入一次，编辑时间不当作回复时间；补到较早消息不覆盖
后发回复。缺少有效 sender ID 时只保留实际观察到的姓名、时间和消息 ID；无
有效公开人工回复时四个字段均为 `null`。接待分类和已确认接管继承规则保持独立。
重建只更新查询投影，不改写原始消息、归档证据或 ICS 原有评价邀约与分配快照。

首版客户合并只使用同来源、同账号的明确 contact ID。姓名、手机号、未验证
identifier 不作为合并键；明确验证的业务标识可展示，但本阶段不自动跨
contact 合并，避免错误拼接客户历史。没有 contact ID 时按会话隔离并标记未知。

ICS 的随机 session UUID 与本模块稳定 round ID 分开保存。CSAT 通过邀请
message ID、已证明的业务 round ID 别名和 `ics_closed_at` 关联已解决轮。
业务 `ics_round_id` 不等于归档 round ID；结束时间允许不超过 1 毫秒的
序列化精度差，但必须唯一匹配已解决轮。多个候选、冲突的邀请或别名证据
保留为 `ops.rating_index.status=unlinked`，不会猜测属于最新轮；后续旧卡
评分继续使用已证明的邀请/别名归属。
评分兼容顶层响应、`content_attributes.csat_survey_response`，以及 Chatwoot
原生 `content_attributes.submitted_values.csat_survey_response`。最新评分和
反馈同时更新查询投影及轮次的 `rating_feedback/rating_at`；明确清空反馈
不会恢复旧值。没有评分变化的卡片生命周期更新不改变评分时间，缺少评分
源时间时 `rating_at=null`，不使用邀请创建时间代替。
原始邀请和评分观察均追加到 `archive.rating_snapshots`。重建时重新解析原始
事件更新可变查询投影，不修改已有快照，即使旧快照曾未解析出评分。评价卡
的创建、更新不计入公开服务回复，也不会开启或拆分咨询轮次。

首版每次批处理重放整个会话，保证乱序和重复投递不会累加计数或倒退最新
消息版本。重放不加载原始字节副本，先检查事件数和 JSON 文本字节预算，
只读取已检查的高水位以内事件。`MAX_CONVERSATION_EVENTS`、
`MAX_CONVERSATION_BYTES`、SQL 超时和独立连接池限制资源。
超长会话超过上限会进入重试/失败，需后续增量检查点优化；失败不会丢失原始事件。
达到最大尝试次数后仍反复过期的工作租约会标记 failed，避免进程持续崩溃时无上限重领。

测试：

```sh
python -m unittest quality.tests.test_processing
```

数据库集成测试 `quality.tests.test_query_database` 仅在
`PROCESSING_TEST_DATABASE=1` 时启用，并限制数据库名为
`quality_query_test_` 或 `quality_capture_test_` 前缀。先在一次性测试库
应用 001/002，再设置 `PROCESSING_TEST_ADMIN_PASSWORD` 和独立连接参数；
绝不能指向现有生产或本机业务归档库。
