# ICS assignment evidence delivery

ICS saves its selected agent only after the assignment API succeeds and the local
success mark commits. The mark verifies the business round and selected agent,
so a late response from a previous round cannot record ownership for a new round.
The journal key is `(account_id, conversation_id, business_round_id)`; retries
never overwrite the first snapshot. Chatwoot changes and reply senders do not
change this evidence.

`chatwoot_assignment_outbox` is created by the existing ICS state initialization.
Startup may snapshot a currently active, successful assignment with a valid
trigger and session. These upgrade snapshots use
`evidence_kind=current_control_backfill`, `assigned_at=null`, and an explicit
`recorded_at`; ended historical assignments are not inferred.
The persisted `handoff_round_id` must equal the current business round. Processing
timestamps cannot establish this relationship. Older controls without that proof
remain unknown, and an idle rollover retains the original handoff round, so older
handoff flags cannot create a new assignment snapshot.
If the initial source-schema initialization fails, a temporary background worker
retries with its own pool of at most one connection and bounded timeouts. Existing
webhook workers start normally and retry failed source transactions. Recovery
releases its pool and thread after success; a normal startup allocates neither.

`ARCHIVE_ASSIGNMENT_SYNC_ENABLED` defaults to false. When enabled, configure a
stable `ARCHIVE_SOURCE_KEY`, explicit `ARCHIVE_ALLOWED_ACCOUNT_IDS`,
`ARCHIVE_ASSIGNMENT_USER` (default `archive_assignment`),
`ARCHIVE_ASSIGNMENT_PASSWORD`, and the existing `ARCHIVE_POSTGRES_*` settings.
The source connection reuses the ICS application's existing database settings,
through a separate pool. No ICS database credential is added to archive workers.
The synchronization credential must have the restricted permissions provided by
the assignment migration; it must not own the archive or modify projections.
The ingestion password is not used by this module.

One background thread polls every five seconds, with bounded failure backoff.
Source and target each have a separate pool with at most one connection and one
waiter. Connection attempts have a three-second timeout, statements two seconds,
and lock acquisition one second. A pass delivers at most twenty records, checking
shutdown between records. Configuration errors and archive failures do not block
ICS startup or handoff.

Delivery appends a deterministic `ics_assignment_recorded` backfill event and its
processing job in one archive transaction. Only after that commit does the source
receive `delivered_at`. A crash or failed source acknowledgement retries the same
event key and raw JSON. All serialized assignment timestamps are normalized to
UTC, including the source timestamp, so a database session timezone change does
not alter the bytes on retry. Unknown historical assignment times remain null.
Original conversation messages are not rewritten.

## Isolated evidence and recovery

Only an explicitly invalid source observation or different immutable evidence
under the same event key is isolated. The source row receives
`delivery_error_code=invalid_evidence` or `evidence_conflict` and
`delivery_failed_at`; its original observation fields and `delivered_at=null`
remain unchanged. Existing archive evidence is never overwritten. Later valid
rows continue in the same bounded pass, and subsequent passes skip isolated rows
without increasing the twenty-record limit or using an expanding offset. The
isolation metadata survives process restarts. Connection errors, timeouts,
transaction failures and failed source metadata writes are retried with backoff;
they are not classified as bad evidence.

The status endpoint reports `assignment_sync_status=degraded` whenever isolated
rows exist. It includes `assignment_sync_pending_count`,
`assignment_sync_blocked_count` and `assignment_sync_counts_sampled_at` (UTC).
Counts are sampled from the configured accounts at most once per sixty seconds
and adjusted for this worker's committed deliveries and isolations between
samples. Newly arriving source records and changes made by another worker appear
at the next sample. A database failure reports `unavailable`, retaining the last
known counts; archive queries can still remain available independently. No
credentials, source content or exception detail is written into error metadata.

An operator must review both the original source observation and the existing
archive event, using the source account, conversation and business round to find
the exact row. Retain that evidence and establish whether the problem was a
validation defect, configuration error, serialization difference or a real
assignment conflict before retrying. Repair the processing or configuration
cause first. Do not edit the archived raw bytes or the source observation fields
to make conflicting evidence agree. A real unresolved conflict remains isolated
for explicit reconciliation.

Once that review proves a retry can safely succeed, clear only
`delivery_error_code` and `delivery_failed_at` for the specific still-undelivered
source row in a controlled source-database transaction. This requeues the same
original observation; it does not acknowledge it or create a replacement
observation. Verify the following source acknowledgement and status counts. A
remaining validation error or evidence conflict will isolate the row again.


## 排队决定证据（2026-11）

排队仓库启动时创建 `ics_handoff_queue_event_outbox`；联系人不能为空，
账号/会话/触发消息构成主键。该表保存独立决定，不保存聊天正文、随机种子或人数历史。
源记录永久保留最小幂等凭据；清理排队状态不删除决定。

同消息先读取旧决定，再解析联系人、执行排队及分配。旧决定直接返回排队结果，
不会重算人数、兑现、分配、成功计次或将后来的人工接待退回 AI，也不重发旧提示。
新决定先提交源证据，再保持 AI 并尝试提示。源读取或保存失败时告警并进入原转人工检查。

归档新增 `ics_handoff_queue_decided`，沿用 `backfill` 和受限 writer。
顶层 account/conversation 保留归档身份；queue_decision 包含 schema_version=1、
origin=ics、outcome=queued、字符串 trigger_message_id/contact_id 和 UTC trigger_at/decided_at。
不要求或制造 business_round_id。重放应按触发消息锚定原请求。
目标事件和作业同事务提交后才确认源；重试保持相同键、原时间和相同字节。
冲突/非法数据隔离而不删除源。

投递复用现有一个执行器及源/目标小池，保持 5 秒轮询和原退避。每轮总计至多 20，
两类交替取数，各至少有 10 条配额，剩余配额供另一类使用，下一轮交换起始类别。
新表未创建时记录降级，仍投递成功分配记录。
状态接口新增 queue_decision_sync_pending_count 和 queue_decision_sync_blocked_count，
原 assignment_sync_pending_count/blocked_count 仍只表示成功分配源。缺新表时排队计数为 null，
同步状态 degraded；计数沿用一分钟采样及批内完成/隔离更新。

现有账号/会话处理锁为进程内锁，当前 serve 不传 workers；部署须保持单个 ICS 消费进程。
不扩大为跨会话兑现预占。多进程部署需要先增加覆盖整段处理的分布式保护。
