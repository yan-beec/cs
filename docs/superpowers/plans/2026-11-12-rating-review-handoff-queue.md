# 评分复核与转人工排队合并实施计划

> **面向 AI 代理的工作者：** 按任务实现，独立文件域可并行处理；使用失败用例、实现、验证循环。

**目标：** 同批交付评分有效性复核和 R1 动态排队，统一统计版本及上线顺序。

**架构：** 评分复核以归档库追加历史和当前状态保存；排队使用 ICS 主库独立模块。原始评分保留，统计查询读取当前复核状态；排队通知在归档投影中撤销该次转人工请求。

**技术栈：** Python、FastAPI、PostgreSQL、现有管理后台与离线 HTML。

**规格：** `docs/superpowers/specs/2026-11-12-rating-review-design.md`；`docs/2026-11-12-R1-转人工排队-执行方案.md`；R1 产品口径见 `docs/product/2026-11-客服系统优化-产品文档-V1.2.md` 第二章。

## 全局约束与裁定

- 使用 `C:\Users\lenovo\Anaconda3\envs\CS\python.exe`；新注释、文档字符串使用中文。
- 基于当前未提交实现原地增量开发，不覆盖已有改动，不提交无关改动、不部署生产。
- 评分新增 `021_rating_reviews.sql`，不编辑历史迁移；排队三张表在 ICS 主库初始化。
- 统一 `FACT_VERSION = 11`、`ROLLUP_VERSION = 3`，升级后按既有预算恢复历史统计。
- 两份规格及合并开发已获用户授权，不重复请求计划批准。
- 独立文件域通过并行任务处理；共享文件有唯一负责人，接口先固定，集成串行验证。

## 审查重点

1. 复核保存与投影、汇总构建并发，不能丢标记或使用旧缓存。
2. 复核后筛选集合变化、详情保留及刷新失败，不能覆盖质检草稿或展示旧总数为最新。
3. 排队期间成功计次幂等、数据库异常放行，不阻塞原转人工链路。
4. 先繁忙后排队、已人工回复、连续媒体请求，不能误改归档责任及流单统计。
5. 迁移后首次查询预算、跨日评分、权限和导出保持一致。

## 任务 1：排队模块与业务链路

文件：新增 `ics_agent/app/handoff_queue/{__init__,policy,repository,service,runtime,routes}.py`；修改 `ics_agent/app/chatwoot/routes.py`、`ics_agent/app/main.py`；新增 `tests/test_handoff_queue*.py`。

- [x] 先建立周五 09:00、24h 边界、动态配置、确定性递减、并发计次和故障放行用例，确认缺失行为。
- [x] 实现模块、配置历史与管理员接口、生命周期及成功分配计数，接入排队分支。
- [x] 验证现有 handoff、blacklist 与新增用例。

接口：`GET/PUT /admin/api/handoff-queue/config`，PUT `{expected_version, config}`；GET `{version, config}`；历史 `GET /admin/api/handoff-queue/history?limit=20` 返回 `{items}`。具体配置字段由任务 1 固定后通知页面负责人。

## 任务 2：评分持久化、查询、统计与导出数据

文件：新增迁移与 `quality/query/rating_reviews.py`（如需）；修改 `quality/query/{repository,routes,runtime}.py`、`quality/processing/{stat_metrics,rollups}.py`、`quality/offline/{repository,bundle}.py`、`statistics/glossary.py`；新增评分 API 和数据库测试。

- [x] 先补 1/3/5 分改判、普通筛选与 metric 明细差异、版本冲突与权限用例，确认失败。
- [x] 实现两张复核表、受限原子保存函数及日期失效、完整内容版本与同快照读取。
- [x] 所有评分聚合、明细、概况、导出数据采用一致规则，列表批量携带复核状态，升级汇总版本。
- [x] 验证评分、查询、统计与导出测试；真实数据库验证由集成阶段统一执行。

接口：规格中的 `GET/PUT /admin/api/quality/rounds/{round_id}/rating-review`；PUT `{status, revision, content_revision}`；GET/PUT 返回完整评分内容与复核状态。轮次的 `rating_review` 字段保存同样复核数据，版本为字符串。

## 任务 3：管理后台及离线页面

文件：`ics_agent/app/admin.html`、`quality/offline/web/{viewer.js,viewer.css}`；按需 `statistics/web/reports.js`；新增行为验证测试，不修改上述两个后端任务文件。

- [x] 增加评分复核交互用例和排队设置入口测试，先确认失败。
- [x] 实现有效/无效按钮、版本整体刷新、防旧响应、切换轮次保护、metric 列表刷新与详情保留。
- [x] 实现系统管理下排队设置及配置历史，使用后端确定的配置协议。
- [x] 离线 HTML 展示快照复核状态，普通评分筛选保留原始分数。
- [x] 验证页面脚本语法及行为用例。

## 任务 4：排队归档语义与统一集成

文件：`quality/processing/{projector,service_facts}.py`、`quality/tests/test_processing.py`；部署说明与本计划。

- [x] 先补排队、繁忙后排队、人工回复后排队等投影回归，确认失败。
- [x] 保存请求前状态，识别固定排队前缀恢复原状态，保留历史拒接与人工证据；升级事实版本。
- [x] 运行单元、页面和隔离 PostgreSQL 的迁移、查询、统计全量相关回归。
- [x] 独立审查修改并修复实质问题；记录测试证据和剩余限制。
- [x] 补上线清单：停 worker → 迁移 → 更新所有统计/ICS 进程（排队关闭）→ 验证重建 → 开放复核、确认排队参数后开启。

## 接口与冲突检查

| 任务 | 边界检查 | 裁定 |
|---|---|---|
| 1 / 3 | 排队配置 API 与表单 | 任务 1 固定 schema 后发给任务 3；页面由任务 3 独占 |
| 2 / 3 | 评分完整内容与版本 | 按评分规格固定；任务 2 提供一致响应 |
| 1 / 4 | 排队消息前缀 | 共用固定文本 `当前咨询人数较多，您前面还有` |
| 2 / 4 | 两类统计版本 | 任务 2 修改汇总版本，任务 4 修改事实版本 |
| 1 | 成功计数与排队删除 | 同一事务；配置关闭仍计数 |
| 2 | 原始评分与统计有效性 | 不改原始评分，只在评分表达式与 metric 明细排除 |
| 3 | 刷新与草稿 | 按轮次、请求代次更新，保留已打开详情与质检草稿 |
| 4 | 历史重建 | 只按最终版本运行；不向生产执行迁移 |

## 完成记录

最终相关集合 279 项及 76 个子用例通过；统计数据库补充 41 项、两个大样本隔离重跑通过；两次空库/重复迁移和权限验证通过。独立审查发现已修复并复核关闭。详见 `docs/2026-11-12-评分复核与排队-实施记录.md`。未提交 Git、未部署生产。
