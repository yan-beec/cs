# 同一 Chatwoot 会话按咨询轮次发送评价

同一个 `conversation_id` 可以反复打开、解决。启用后，每轮真正结束时，ICS 随原有会话状态保存本轮的待评价快照，再由独立后台转存邀约任务并创建一张 `input_csat` 原生评价卡。关闭会话的流程不访问评价任务表或 Chatwoot 评价接口。用户在 Website Widget 内提交评分；Chatwoot 按卡片消息 ID 保存评分，满意度报告和 CSV 会分别显示多轮评价。

本功能默认关闭。关闭时不创建邀约表、不启动发送任务，原有问答、上下文轮次、转人工、坐席轮询及静音流程保持原行为。只在真实 `resolved` 边界创建邀约，`pending`、`open`、普通会话活动、六小时上下文切换和重复/过期状态事件均不会额外邀约。没有已开始咨询的历史会话也不会补发。

## 启用

1. 把 `deploy/chatwoot/ics_csat_rounds.rb` 随本版本部署到 **Chatwoot 所在主机**。该补丁只影响带 `ics_round_id` 的新评价消息，不改变其他消息或现有评价的处理。
2. 让 Chatwoot 容器读取这个补丁文件，这一步称为“挂载”。Chatwoot 启动时会自动加载 `/app/config/initializers/` 内的补丁，因此不需要修改 Chatwoot 源码或重新构建它的镜像。

   **本机 Windows 部署示例：**打开 `D:\project\chatwoot\services\chatwoot\docker-compose.yaml`，在文件最上方的 `x-base` 中，保留原有存储配置，再增加补丁挂载，使这一段变成：

   ```yaml
   x-base: &base
       image: chatwoot/chatwoot:latest
       env_file: .env
       volumes:
         - storage_data:/app/storage
         - type: bind
           source: D:/project/CS/deploy/chatwoot/ics_csat_rounds.rb
           target: /app/config/initializers/ics_csat_rounds.rb
           read_only: true
   ```

   - `source` 是电脑上已有的补丁文件，必须是文件，不能是文件夹。
   - `target` 是容器内加载这个文件的位置。
   - `read_only` 表示容器只能读取这个文件。

   现有 `rails`、`sidekiq` 都通过 `<<: *base` 共用这段配置，所以这里只加一次，两者就都能读取补丁。保存后，在 PowerShell 中执行：

   ```powershell
   Set-Location 'D:\project\chatwoot\services\chatwoot'
   docker compose -f docker-compose.yaml up -d --no-deps --force-recreate rails sidekiq
   ```

   这会重新创建两个应用容器并加载补丁，已有数据库和文件存储卷会保留。只修改配置而不重新创建容器，新增挂载不会生效。

   **部署到 Linux 服务器时：**先把补丁文件放到运行 Chatwoot 的服务器，例如 `/opt/cs/deploy/chatwoot/ics_csat_rounds.rb`，再把上面的 `source` 改成这个服务器绝对路径；`target` 不变。Windows 本机路径不能直接用于另一台服务器。如果你的 Compose 没有共用 `x-base.volumes`，需要分别在 Rails 和 Sidekiq 的 `volumes` 下增加同样挂载。已有独立挂载配置时，也可合并仓库提供的 `deploy/chatwoot/docker-compose.csat.yml`，用 `ICS_CSAT_PATCH_PATH` 指定补丁的主机绝对路径。
3. 在需要使用本功能的 Chatwoot Inbox 中关闭自带的自动满意度邀约，避免首次结束时双发。保留现有 AgentBot 和状态 Webhook 配置。
4. 在 **ICS** 的 `.env` 中增加：

   ```dotenv
   CHATWOOT_CSAT_ENABLED=true
   CHATWOOT_CSAT_MESSAGE=本次服务已结束，请评价本次服务。
   CHATWOOT_CSAT_DISPLAY_TYPE=emoji
   CHATWOOT_CSAT_API_TIMEOUT=5
   ```

   `CHATWOOT_CSAT_DISPLAY_TYPE` 可为 `emoji` 或 `star`。沿用现有 Chatwoot 地址、用户级 API Token，以及 AgentBot Token/ID，不增加公开接口或前端凭据。用户级 Token 用于核对已创建的卡片。

5. 在 ICS 现有部署目录重新构建并创建应用：

   ```bash
   docker compose -f docker-compose.yml up -d --build --no-deps --force-recreate ics-agent
   ```

ICS 初始化会话表时增加 `csat_pending_rounds` 字段，用于保存未转存的逐轮快照，不修改 Chatwoot 数据库结构。独立后台自动创建 `chatwoot_csat_invitations`；创建失败不阻止问答和 Webhook 服务启动，每 5 秒重试。后台每 5 秒检查待发送项，发送失败后按 60 秒起、最长 1 小时的间隔重试；重启会继续处理未完成任务。开启前未捕获的历史已结束轮次不会自动回补。

## 故障隔离与恢复

- 结束会话只在原有会话状态 UPDATE 中追加本轮快照，不写评价任务表。评价表缺失、无权限或被锁住时，结束会话、释放人工接管和下一轮接待仍按原流程执行。
- 每轮快照包含轮次 ID、结束时间、原坐席、Inbox 和最后一条用户消息 ID。重开不会覆盖历史待处理快照；同一轮的重复/过期状态不会追加第二份。
- 后台把快照转存为邀约任务，转存与移除该快照在独立事务中一同提交。失败时全部回滚，快照保留；恢复或重启后继续转存。确认快照时只移除对应轮次，不清空新轮次，也不修改业务的 `updated_at`。
- 评价使用独立数据库连接池和单个专用工作线程，不占用问答的连接池、工作线程或消息队列。评价事务的数据库锁等待上限为 250 毫秒，单条语句上限为 2 秒；访问评价表时不锁定会话行。
- `CHATWOOT_CSAT_API_TIMEOUT` 默认 5 秒，只作用于评价 HTTP 请求；消息查询的多次翻页共用这个时间预算。普通消息发送、转人工等接口的超时配置不变。发送失败或回执丢失后，继续使用轮次 ID 核对、防重和重试。
- 关闭评价开关后停止捕获和发送，待处理快照和邀约任务保留，重新开启后继续处理。

隔离范围是评价专用表、后台任务和接口的故障。会话主表及其快照字段需要完成本版本的初始化；共用 PostgreSQL 整体不可用、会话主表无法写入、磁盘或进程故障仍会影响原有业务，不能通过评价隔离消除。

## 防重与坐席归属

- 本地以 `account_id + conversation_id + active_thread_id` 唯一记录邀约。快照转存和重复状态通知不会生成第二项任务。
- 发送前分页核对本轮已有卡片。Chatwoot 补丁在会话行锁内按 `ics_round_id` 查找/创建，因此请求超时、断线或多进程重试也会返回同一张卡。
- 邀约保存本轮结束时的坐席；Webhook 提供的坐席优先于 ICS 本地分配记录。释放人工接管不会丢失该快照。Chatwoot 补丁创建评分记录时使用快照，因此延迟提交旧轮次的评价也归属于旧轮次坐席。无人工坐席时报告显示无坐席。
- 发送记录的 `message_id` 是邀约卡 ID。评分和反馈仍存入 Chatwoot 原有评价表，报告按评分记录显示；未评分卡只计入邀约发送量。统计受报告当前日期和筛选条件影响。

必须部署 Chatwoot 补丁，才能保证超时重试的原子防重及历史坐席归属。只部署 ICS 时，普通 Widget 多卡评分仍可保存，但这两项保证不成立。

本次只支持在聊天窗口内评价。原生 `/survey/responses/{uuid}` 独立评价页仍按会话读取第一张卡，请勿把这个会话级链接用于逐轮评价。会话详情原有单条评价关联也未更改；查看全部结果请使用满意度报告。团队/Inbox 筛选仍遵循 Chatwoot 原有按当前会话关联的口径。

## 验收

1. 同一用户发起咨询，转人工并解决，确认出现一张评价卡。
2. 提交评分后继续在同一窗口咨询，再次转人工并解决，确认 `conversation_id` 未变，出现另一张评价卡。
3. 两张卡分别提交不同评分，后台满意度报告和 CSV 应有两条独立记录。
4. 重投同一次 `resolved` 通知，确认不增加卡片；停止、启动 ICS 后也不补发已成功卡片。
5. 第一轮先不评分，第二轮换坐席后再评价第一张卡，确认报告仍显示第一轮坐席。
6. 关闭 `CHATWOOT_CSAT_ENABLED` 并重建 ICS 后，停止新增及发送邀约，已有评分和聊天记录保留。需要恢复 Chatwoot 自带邀约时，再单独开启 Inbox 对应开关。
7. 在隔离测试库中让评价表不可用，连续结束并重开两轮；确认会话正常结束、人工接管释放、两轮快照均保留。恢复评价表并重启后台后，应生成两条不同轮次的邀约。
8. 在隔离测试库中锁住评价表或会话确认行，转存应在短超时后失败并保留快照；释放锁后重试只生成一项任务。

## 查看发送状态

ICS 日志中的 `csat.invitation.sent` 表示创建或找回成功；`csat.invitation.retry` 表示将在稍后重试。可在 ICS PostgreSQL 中只读检查：

```sql
SELECT account_id, conversation_id, round_id, assigned_agent_id,
       message_id, attempts, last_error, closed_at, sent_at, next_attempt_at
FROM chatwoot_csat_invitations
ORDER BY closed_at DESC
LIMIT 50;
```

评价表尚未恢复时，可直接查看保存在会话主表中的快照积压：

```sql
SELECT account_id, conversation_id, jsonb_array_length(csat_pending_rounds) AS pending_rounds
FROM chatwoot_conversation_control
WHERE csat_pending_rounds <> '[]'::jsonb;
```
