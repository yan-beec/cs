# 本次发布：归档库 010 → 016、统计/质检新口径、黑名单、快捷回复、离线 HTML 留档

适用：生产归档库当前为 `010_round_replay_windows`，ICS 代码为此前发布版本。本次包含：

- 归档库迁移 011（查询索引 + 统计事实表 + 触发器）、012（黑名单记录）、013（主责坐席列）、014（咨询活跃段列）、015（报表与归档同一口径：每轮统计事实宽表 + 每轮×小时回复表，删除 round_seat_timings）、016（日/月/年汇总表 + 脏日标记触发器）。
- 统计报表“坐席细化 / 质检统计”新查询（按每轮预计算结果汇总）和新口径：每轮只归主责坐席；同一轮有主责以外坐席公开回复的“协助轮次”整轮不计任何考核，仅保留发配量；咨询状态总时长改为“活跃段”（只计转人工成功后的消息，相邻消息间隔超过10分钟切段，空档不计；每段从第一条消息到主责坐席最后一条回复，协助轮次也归主责坐席）。
- ICS：坐席黑名单（拉黑后 AI 继续接待、只拒绝转人工）、快捷回复同步、离线 HTML 留档、归档查询“用户第 N 次咨询”等。
- 坐席客户端（desktop-client）需要重新打包分发。

原则：**先备份 → 停解析进程 → 迁移 → 再上新代码 → 最后启动新解析进程**。新代码离不开 011–016（缺列时统计报“暂不可用”，解析进程整理会话也会失败）；旧代码在新表结构上可以继续运行，所以迁移可以先做、代码可以回退。

---

## 一、开发端（Windows）

1. 看真实改动量（忽略换行差异）：

   ~~~powershell
   Set-Location D:\project\CS
   git status --short
   git diff HEAD --ignore-cr-at-eol --stat
   ~~~

   `git status` 里大量文件只是 CRLF/LF 差异；新增目录（`ics_agent/app/blacklist/`、`ics_agent/app/quick_replies/`、`quality/blacklist/`、`quality/offline/`、`quality/processing/service_facts.py`、`quality/database/migrations/011~014`、`quality/deploy/migrate-local.ps1`）是未跟踪文件，必须 `git add`，否则服务器拿不到。

2. 测试（按 `docs/server-sync-update.md` 第二节）。已知 `statistics/tests/test_statistics_database.py::test_lost_transfers_use_confirmed_responsibility_and_include_seats_without_replies` 在本次改动前就失败，需决定是先修还是记录后放行。

3. 迁移文件和部署脚本必须是 LF（当前已核对无 CRLF）。**不要批量转换 `quality/database/migrations/` 的换行**，已执行的 001–010 字节变化会让迁移脚本因校验和不一致而停止。

4. 当前分支是 `yanbicheng`，发布分支是 `test`：提交后合并到 `test` 并推送，记下提交号。

## 二、服务器发布前检查（不改任何东西）

~~~bash
cd /opt/cs
C="docker compose --env-file .env -f docker-compose.yml -f docker-compose.archive.yml"
cat deployment-records/source-commit.txt          # 当前生产版本，回退用
echo "SELECT version FROM ops.schema_migrations ORDER BY 1;" | $C exec -T archive-postgres sh -c 'psql -X -At -U $POSTGRES_USER -d $POSTGRES_DB'
~~~

最后一行应为 `010_round_replay_windows`。再看数据量，用来估算 011 建索引耗时：

~~~bash
cat <<'SQL' | $C exec -T archive-postgres sh -c 'psql -X -U $POSTGRES_USER -d $POSTGRES_DB'
SELECT relname, n_live_tup, pg_size_pretty(pg_total_relation_size(relid)) AS size
FROM pg_stat_user_tables
WHERE schemaname='ops' AND relname IN ('round_index','message_index','event_jobs','customer_index','conversation_index','rating_index')
ORDER BY pg_total_relation_size(relid) DESC;
SELECT status, count(*) FROM ops.event_jobs GROUP BY 1;
SQL
df -h /opt/cs
~~~

- `event_jobs` 不应有大量 `queued/retry` 积压；有积压先等解析进程处理完。
- 磁盘：离线 HTML 默认预算 50 GiB（`ARCHIVE_OFFLINE_DISK_BUDGET_BYTES`），放在 `/opt/cs/database/archive-exports`，空间不够就在 `.env` 调小。

## 三、备份（必须）

~~~bash
mkdir -p /opt/cs/backup
$C exec -T archive-postgres sh -c 'pg_dump -Fc -U $POSTGRES_USER -d $POSTGRES_DB' > /opt/cs/backup/archive-$(date +%F-%H%M).dump
$C exec -T postgres sh -c 'pg_dump -Fc -U $POSTGRES_USER -d $POSTGRES_DB' > /opt/cs/backup/ics-$(date +%F-%H%M).dump
ls -lh /opt/cs/backup
~~~

建议（数据量大时尤其）：把归档备份恢复到一个临时 PostgreSQL 容器，在上面跑一遍迁移计时，就能知道正式迁移要停多久。

## 四、发布（选低峰时段）

**为什么要低峰：** 011 在一个事务里建十几个索引（非 CONCURRENTLY），期间 `round_index / message_index / event_jobs` 等表只能读不能写。采集器（quality-receiver）写入的锁等待上限是 1 秒，超时会给 Chatwoot 返回错误，而 Chatwoot 一般不会重发失败的 webhook，这段时间的归档事件可能丢失（不影响智能客服本身和 Chatwoot 里的聊天记录）。数据量在几十万到几百万行时通常是几十秒级，具体以第三步的演练为准。

1. 拉代码：

   ~~~bash
   git -C /opt/cs-src status --short        # 应无输出
   git -C /opt/cs-src switch test
   git -C /opt/cs-src pull --ff-only <远端名> test
   git -C /opt/cs-src rev-parse HEAD        # 与开发端推送的提交号一致
   ~~~

2. 停解析进程（避免迁移时它持有锁导致迁移 10 秒锁超时失败，也避免它在半新半旧状态下处理消息）：

   ~~~bash
   cd /opt/cs && $C --profile quality-query stop quality-worker
   ~~~

3. 同步 + 迁移 + 构建 + 更新 ICS（统一发布脚本；之前带 `--with-receiver` 启动过采集器的，继续带上）：

   ~~~bash
   bash /opt/cs-src/deploy/server/release.sh --source /opt/cs-src --target /opt/cs --with-receiver
   ~~~

   脚本顺序：同步代码 → 迁移 011、012、013、014、015、016 并校验权限 → 构建镜像 →（采集器）→ 更新 ics-agent。任一步失败即停止；迁移失败时不会构建和更新 ICS。若迁移报 `lock_timeout`，确认 worker 已停后直接重跑。

4. 用新镜像启动解析进程（`release.sh` 不管它，必须手动）：

   ~~~bash
   cd /opt/cs && $C --profile quality-query up -d quality-worker
   ~~~

## 五、发布后验证

~~~bash
echo "SELECT version FROM ops.schema_migrations ORDER BY 1;" | $C exec -T archive-postgres sh -c 'psql -X -At -U $POSTGRES_USER -d $POSTGRES_DB'   # 末尾 011、012、013、014、015、016
$C logs --since 10m ics-agent | grep -Ei "UndefinedColumn|unavailable|error" | tail
$C logs --since 10m quality-worker | grep -Ei "UndefinedColumn|failed" | tail
~~~

- 等模型预热后按首次部署手册第 9 节检查 `/health`、管理员登录、FAQ、RAG、Chatwoot 消息往返。
- 后台“对话归档”能打开，“黑名单”筛选不再提示迁移；“统计报表”日视图的坐席细化、质检统计正常。
- 统计事实回填进度（新 worker 空闲时自动从最新往前补）：

  ~~~bash
  cat <<'SQL' | $C exec -T archive-postgres sh -c 'psql -X -U $POSTGRES_USER -d $POSTGRES_DB'
  SELECT count(*) AS rounds, count(f.round_id) FILTER (WHERE f.fact_version=6) AS facts_ready
  FROM ops.round_index r LEFT JOIN ops.round_service_facts f USING(round_id);
  SQL
  ~~~

  **回填完成前**，没有预计算结果的轮次在查询时现场计算，结果是对的但慢：月/年等长区间可能提示“查询超时”，这是预期现象，等 `facts_ready` 接近 `rounds` 后再看长区间。日志出现 `Archive service facts backfill complete` 表示完成。

- 若迁移前后有消息因缺表处理失败，迁移后重新排队：

  ~~~sql
  UPDATE ops.event_jobs SET status='retry', attempts=0, available_at=CURRENT_TIMESTAMP, last_error=NULL
  WHERE status='failed' AND last_error='UndefinedColumn';
  ~~~

## 六、坐席客户端

服务器先发布，客户端后升级。旧客户端继续可用（只是没有黑名单、快捷回复同步）。在 `D:\project\chatwoot\services\chatwoot\desktop-client` 重新 `npm run build:reminder` 并打包 1.4.0 分发给坐席；注意 CS 里的 reminder 打包会覆盖客户端里的提醒组件，以 desktop-client 为准。

## 七、需要提前告知业务方的变化

- **统计口径变了**：坐席细化/质检统计每轮只算主责坐席；交班等“协助轮次”整轮不计入任何坐席（只保留发配量和主责坐席的咨询时长），合计也不含。咨询状态总时长改为活跃段：只计转人工成功后的消息，同一轮相邻两条有效消息（客户或坐席）间隔超过10分钟就切段，空档不算忙碌；每段从段内第一条消息到主责坐席最后一条回复（原来整轮首末回复整段计入，未解决的会话会出现单日十几小时）。发布后数字会和以前不同，属正常。
- **黑名单行为**：拉黑后客户继续由 AI 接待，只是转人工时回复与“坐席繁忙”相同的话术，客户无感知。
- 离线 HTML 留档在独立低优先级子进程中生成，首次完整下载后服务器只保留 2 小时，未下载 24 小时后删除。

## 八、回退

- 数据库**不回退**：011–014 都是新增表/列/索引/触发器，旧版代码可以在新结构上运行。注意 015 删除了 `ops.round_seat_timings`，**015 之后不能回退到 014 及以前的代码**（旧代码读这张表会报错）；如需回退代码，只能回退到本次发布的代码版本。
- 代码回退：`/opt/cs-src` 切回第二步记录的旧提交，重新执行 `release.sh`（迁移会显示已执行并跳过），再重启 quality-worker。
- 回退后注意：旧版 ICS 不认识黑名单的 `pinned` 标记，客户端设置的永久/72 小时拉黑可能在下一次 Chatwoot 回调时被改回默认 24 小时。
- 只有数据损坏时才用第三步的备份恢复。


## 九、015/016 补充（2026-11-10，报表与归档同一口径）

- **口径变化（需提前告知）**：考核坐席统一为 ICS 已确认分配的坐席，不再用“转人工后第一个回复的坐席”兜底。没有 ICS 分配记录的轮次（Chatwoot 手动分配、沿用上一轮人工接待、分配冲突）不再计入任何坐席，坐席细化概况单列“无ICS分配轮次”。因此部分坐席的询盘数、对话量、评分等会比旧版少，这部分轮次挪到了合计里。
- **迁移 015**：只加可空列、建一张新表和几个新列上的索引（新列此时为空，建索引很快），并删除 `ops.round_seat_timings`。`FACT_VERSION` 升为 7，旧事实全部失效，处理程序空闲时自动回填；**回填完成前长区间统计会走现场计算、明显变慢**，按年统计可能超时，回填完成后恢复。
- **迁移 016**：汇总表、脏日表、触发器，并给 `archive_ingest` 授予 `quality.round_reviews` 只读（汇总程序要读质检结论）。日汇总在事实回填完成后自动构建（每次空闲构建一天），构建前报表自动用现场计算，数字相同。
- 发布后验证：`SELECT count(*) FROM ops.round_service_facts WHERE fact_version=7;` 应逐步增长到与 `ops.round_index` 行数相同；`SELECT count(*) FROM ops.stat_rollup_days WHERE state='built';` 之后逐步增长。

## 十、FACT_VERSION 9 / 迁移 018 补充（2026-11-11，R3 响应时长与 R11 咨询时长）

- **迁移 018_response_seconds_total**：`ops.round_service_facts` 加一列可空 `response_seconds_total`（每轮已完成响应总秒数），不重写表、瞬间完成。**须先迁移再上线新代码**（新代码写入和读取该列）。
- `FACT_VERSION` 7 → 9：旧事实全部失效，报表对未回填轮次现场按新规则计算，处理程序空闲时自动回填，日汇总随后自动重建；回填完成前长区间统计会变慢，数字一致。
- **口径变化（需提前告知坐席与运营）**：
  - 首响与每段响应都从客户**最后一条**有效消息起算（分段发送以最后一段为准）。转人工后客户必须先发言：转人工指令本身不作起点，客户发言前坐席主动发的欢迎语等不算首响。
  - 报表平均响应改为**按响应次数加权**：Σ各轮响应总秒数 ÷ Σ响应次数（原为先按对话平均再等权），直接读预存的 `response_seconds_total`。
  - 咨询状态总时长改为**转人工成功 → 解决**：时间线含转人工、之后任何人的有效消息和解决，相邻间隔超过 600 秒切开、空档不计；未解决轮次截至最后一条消息。终点不再是主责最后一条回复。
  - **协助轮次（有 ICS 分配坐席以外的坐席回复）不计咨询状态总时长**，整轮只保留发配量（原来咨询时长仍计入主责坐席）。
  - 考核坐席保持“只认 ICS 分配”，没有分配记录的轮次仍单列，不计入任何坐席。
- 发布后验证：`SELECT count(*) FROM ops.round_service_facts WHERE fact_version=9;` 逐步增长到与 `ops.round_index` 行数相同。
