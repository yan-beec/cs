# 转人工排队运维说明

入口：管理后台 → 系统管理 → 排队设置。首次初始化总开关为关闭，配置保存在 ICS 主库；重启、升级不覆盖已有配置，不新增环境变量。

成功分配到人工后才计次。总开关关闭时仍积累成功次数，转人工功能本身关闭、繁忙、拉黑、排队提示均不计次。每日档位按最近 24 小时成功次数加 1 判断；周档位按最近一个北京时间周五 09:00 以来成功次数加 1 判断。两个计数互不影响。

日档位和周附加人数相加，周附加只取最高命中档。后台可增删档位、指定固定人数或随机区间；初始值只用于建库。已有排队固定保存初始人数、时间、种子和递减间隔，改配置不改变其递减节奏。排到零后保留兑现机会，繁忙、拉黑不消耗它，成功分配才删除排队状态。

用户统计排行榜和 Excel 中的“近24小时计数”“当前周次计数”直接读取本模块的日、周计数，展示已成功的次数，不加本次请求的 1 次。两列独立于报表日期及渠道筛选，跨会话、跨渠道累计；无记录和旧周归零，不可用显示“—”，不追补部署前的历史。

## 数据与接口

ICS 启动后通过独立小连接池初始化三张表：

- `ics_handoff_success_counter`：每个账号、联系人一行，保存近期成功时间与周计数。
- `ics_handoff_queue_state`：每个账号、联系人最多一行，跨会话保留排队或兑现机会。
- `ics_handoff_queue_config`：配置版本与追加修改历史；并发保存旧版本返回 409。

管理员接口：

- `GET /admin/api/handoff-queue/config`
- `PUT /admin/api/handoff-queue/config`，请求为 `{expected_version, config}`。
- `GET /admin/api/handoff-queue/history?limit=20`

`config` 字段为 `enabled`、`daily_tiers`、`weekly_tiers`、`step_min_s`、`step_max_s`、`estimate_per_person_s`。档位元素为 `{start, min_count, max_count}`；上下限相同表示固定人数。人数范围 0–200，间隔及每人预计秒数范围 5–600。保存实例立即使用新版本，其他实例最多 30 秒刷新。配置读取失败沿用最后成功配置，启动时未成功读取则保持默认关闭。

## 核对与故障

以下 SQL 只在 ICS 主库使用：

```sql
SELECT version, updated_by, updated_at, config
FROM ics_handoff_queue_config ORDER BY version DESC LIMIT 20;

SELECT account_id, contact_id, week_count, week_anchor,
       cardinality(success_times) AS stored_successes, updated_at
FROM ics_handoff_success_counter ORDER BY updated_at DESC LIMIT 20;

SELECT account_id, contact_id, initial_count, entered_at, redeem_ready, config_version
FROM ics_handoff_queue_state LIMIT 20;
```

`stored_successes` 是保存的数组长度，实际日计数必须过滤严格晚于当前时刻减 24 小时的时间；不能直接把数组长度当作实时次数。

每 10 分钟清理无日/周记录的计数行，将保证已归零的排队压缩为兑现标记。清理延迟不会延长排队或改变计数；请求时现算真实剩余人数。清理使用数据库锁避免多实例重复执行。

关注日志：`webhook.handoff.queue_runtime_failed`、`queue_config_failed`、`queue_evaluate_failed`、`queue_record_failed`、`queue_contact_failed`、`queue_notice_failed`（均以前述 `webhook.handoff.` 为前缀）。数据库或联系人异常时门槛放行，按原流程转人工；成功计次失败只告警，不追加补计。

排队提示必须保留前缀 `当前咨询人数较多，您前面还有`，归档用它识别排队拦截。发送失败时归档可能只看到转人工关键词，须结合 `queue_notice_failed` 核对。改变此前缀必须同时更新归档识别与事实版本。

联合上线步骤见 [评分复核与排队上线说明](2026-11-12-评分复核与排队-上线说明.md)。
