# Chatwoot 按消息保留至少 60 天

本方案用于已经由 ICS 完整归档的 Chatwoot 账号：ICS 归档库长期保留已有文字和原始事件，Chatwoot 分批清理过期消息及所属附件。经常咨询的用户也会清理旧消息，最近消息和会话本身继续保留。无需修改 ICS 运行配置，不修改 Chatwoot Compose，不重建或重启服务。

## 规则与影响

- 按 `messages.created_at` 计算保留期，同时要求 `updated_at` 也早于截止点。最近 60 天编辑过的旧消息会暂缓清理；它携带的更新内容也获得至少 60 天。
- 如果所属附件在最近 60 天创建或更新，整条消息暂缓清理，防止清掉新上传的文件。附件的保留起点不按容器文件的 mtime 判断。
- 清理不依赖会话是否已解决或最近是否活跃。客户、客服、机器人、私密备注、系统活动和评价卡等过期消息都在范围内。
- 使用 `Message#destroy!` 物理删除消息，通过关联回调清理 Chatwoot 附件、通知和评价响应。附件文件由 Active Storage 的后台任务删除，Sidekiq 必须运行。
- 不使用普通“删除消息”API；该 API 将正文改为删除占位文字，并触发 `message_updated`，可能使 ICS 将旧消息标记为撤回。维护脚本不主动发送该更新；ICS 原始事件和历史快照不参与清理。
- 保留会话壳、联系人、现有状态和分配，不清理其他会话级记录。不保证 Chatwoot 自带报表、评价卡和引用旧消息的展示维持原样，历史查询以 ICS 为准；会话清空后需要验证原前端仍能发送新消息。
- 当前脚本不支持启用了外部全文检索的实例，正式模式检测到后停止，以免搜索索引继续保存已删除正文。需另外适配该索引后再执行，不能通过关闭检测绕过。
- 外部渠道或远程网站托管的媒体不由此脚本删除；本地 `chatwoot_storage_data` 中的共享文件只有在不再被其他记录引用时才会释放。备份、邮件源内容、外部集成及其他内容副本有各自的保留策略。
- 每次固定 UTC 截止点，严格早于它才处理；默认预览，必须显式设置 `CHATWOOT_RETENTION_DRY_RUN=0` 才删除。单次默认 500 条，上限 5000 条，每条开始前检查 120 秒循环预算，当前条及其回调完成后才会停止；数据库锁避免同一数据库的本脚本重叠运行，锁住或发生新修改的消息留待下次。
- 分批限额、定时周期、故障和附件任务积压会让物理保留超过 60 天。该方案是“至少 60 天，过期后持续清理”，并非第 60 天当秒删除。

已核对 Chatwoot v4.17.1 官方源码及本地 4.18.0 源码的关联行为，但尚未在服务器安装版本上执行。脚本会检查关键关联的删除方式；第一次正式运行前须完成下方预览和小批验收。服务器源码、定制模块和配置仍以实际容器为准。

参考：[Chatwoot 消息模型](https://github.com/chatwoot/chatwoot/blob/v4.17.1/app/models/message.rb)、[消息删除接口](https://github.com/chatwoot/chatwoot/blob/v4.17.1/app/controllers/api/v1/accounts/conversations/messages_controller.rb)、[Rails Active Storage 文件清理](https://github.com/rails/rails/blob/v7.2.3/activestorage/app/models/active_storage/attachment.rb)。

## 1. 上传维护文件

先停用之前的整会话清理定时任务，避免两套策略同时运行。保留已验证的 Chatwoot PostgreSQL 和附件卷备份，确认候选消息已经归档至 ICS。

在 Windows PowerShell 执行，SSH 别名沿用 `ics`：

```powershell
ssh ics 'mkdir -p "$HOME/chatwoot-maintenance"'
scp D:\project\CS\deploy\chatwoot\message_retention.rb D:\project\CS\deploy\chatwoot\run-message-retention.sh ics:chatwoot-maintenance/
ssh ics
```

后续命令都在 **Ubuntu 服务器 Bash** 中执行：

```bash
cd "$HOME/chatwoot-maintenance"
bash -n run-message-retention.sh
docker exec -i chatwoot-rails-1 ruby -c < message_retention.rb
docker exec chatwoot-rails-1 bundle exec rails runner 'puts "Chatwoot account IDs: #{Account.order(:id).pluck(:id).join(",")}"'
```

`ruby -c` 预期显示 `Syntax OK`。最后一条仅列账号 ID，不打印密码或客户正文。

## 2. 预览

在同一个 Bash 窗口设置**实际已被 ICS 完整归档的账号 ID**，不要将示例中的 1 当成确认值；多个账号写成 `1,2`，不带空格：

```bash
read -r -p '输入已完整归档的 Chatwoot Account ID（多个用逗号）：' CHATWOOT_RETENTION_ACCOUNT_IDS
export CHATWOOT_RETENTION_ACCOUNT_IDS
CHATWOOT_RETENTION_DRY_RUN=1 CHATWOOT_RETENTION_BATCH_SIZE=10 \
  bash "$HOME/chatwoot-maintenance/run-message-retention.sh"
```

输出含账号、UTC 截止点、符合条件的消息总数、选中消息和附件数，以及最多 10 个消息 ID。`conversation_db_id` 是数据库内部 ID，不一定等于 Chatwoot 界面的会话编号。预览不输出正文，也不执行删除。

`eligible_messages=0` 表示没有达到本策略清理条件的消息，属于正常结果。不要为了制造候选记录而调短生产环境的 60 天保留期。

## 3. 正式处理 10 条

候选范围已核对、备份已验证后，在同一个窗口执行：

```bash
CHATWOOT_RETENTION_DRY_RUN=0 CHATWOOT_RETENTION_BATCH_SIZE=10 \
  bash "$HOME/chatwoot-maintenance/run-message-retention.sh"
docker logs --tail 100 chatwoot-sidekiq-1
```

核对这些旧消息在 Chatwoot 中消失、同会话的近期消息仍在、近期附件能打开、新消息能继续收发；在 ICS 检查旧文字、原始版本和质检记录仍在。旧媒体来源链接失效是预期结果。若这 10 条覆盖了一个会话的全部消息，也检查该空会话和用户后续咨询能否正常工作。

脚本按消息逐条提交，出错停止时之前的删除不会回滚。先排查报错类型和 Sidekiq 日志，不连续扩大批次。脚本不打印可能包含客户正文的数据库异常详情。

## 4. 持续清理

小批验收正常后可改为每次 500 条，观察输出的 `remaining_eligible`。积压较多时，可在前一轮结束且 Sidekiq 正常的情况下再次运行；单次最多 5000 条。不能仅凭“脚本退出”判定文件已经从磁盘删除。

保存定时任务使用的非密钥配置，保留当前窗口中已经确认的账号 ID：

```bash
printf 'CHATWOOT_RETENTION_ACCOUNT_IDS=%s\nCHATWOOT_RETENTION_BATCH_SIZE=500\n' \
  "$CHATWOOT_RETENTION_ACCOUNT_IDS" > "$HOME/chatwoot-maintenance/retention.conf"
chmod 600 "$HOME/chatwoot-maintenance/retention.conf"
```

执行 `crontab -e`，新增以下一行，其他已有任务保留。每小时第 15 分钟处理一批，避免每日批量上限太低造成持续积压：

```cron
15 * * * * /bin/bash -c 'set -a; source "$HOME/chatwoot-maintenance/retention.conf"; set +a; CHATWOOT_RETENTION_DRY_RUN=0 /bin/bash "$HOME/chatwoot-maintenance/run-message-retention.sh"' 2>&1 | /usr/bin/logger -t chatwoot-message-retention
```

定时任务由具有现有 Docker 访问权限的同一 Ubuntu 用户安装。可用下列命令检查；如果服务器未向当前用户开放系统日志读取权限，由服务器管理员查阅该标签日志，不修改数据库或文件权限来解决。

```bash
crontab -l
journalctl -t chatwoot-message-retention --since today --no-pager
docker logs --tail 100 chatwoot-sidekiq-1
docker system df -v
```

持续出现 `remaining_eligible` 很大且不下降时，结合服务器负载与 Sidekiq 队列增加批次或频率；不能保证默认 500 条适合所有消息量。停止定时清理只需移除新增的 cron 行，已经删除的数据只能从备份恢复。PostgreSQL 删除后会复用内部空闲空间，数据库卷大小不一定立即下降，不需要为此在线执行 `VACUUM FULL`。
