# 离线对话留档实现计划

> 使用 executing-plans 按任务实现；用户已批准设计并要求实施，无需再次确认。

**目标：** 大批量完整导出为带本地图片视频、离线筛选的 HTML ZIP。

**架构：** 只读快照流式提取消息，持久后台任务下载附件并生成 ZIP64。离线查看器按消息分块读取和筛选；下载文件不经过浏览器 Blob 缓冲。

**技术栈：** Python 3.10+、FastAPI、PostgreSQL、JavaScript、标准库 ZIP/HTTP/SQLite。

**规格：** ../specs/2026-11-05-offline-archive-design.md

## 文件结构

- quality/offline/config.py、repository.py：独立配置及快照选择。
- quality/offline/media.py、bundle.py：附件下载、预算与 ZIP。
- quality/offline/jobs.py、routes.py：持久后台任务、状态、取消、鉴权下载。
- quality/offline/web/viewer.*：无网络依赖的分块筛选页面。
- quality/offline/web/admin.js：管理后台任务面板。
- quality/tests/test_offline_*.py：端到端组件及安全回归。
- ics_agent/app/main.py、admin.html：生命周期和入口；配置模板及文档：部署和使用说明。

## 全局约束

完整导出，不静默截断；附件失效不阻止对话导出。默认单文件 2 GiB、总输入 20 GiB、24 小时时限、2 GiB 最小剩余空间、服务端产物保留 7 天。Excel 行为兼容。

## 审查重点

1. 大数据全量与跨分块筛选：验证超过 Excel 消息上限后消息仍齐全。
2. 不可信消息/文件名/URL：验证脚本不执行、无法路径穿越、跳转不越过来源白名单。
3. 附件过期/中断/重复：验证失败标记、去重和无部分文件假成功。
4. 重启/取消/磁盘用尽：验证任务可恢复、预算失败清理、不会报告完整成功。
5. 未授权下载/大文件下载：验证管理员与签名票据、流式传输及过期处理。

## 任务

### 1. 留档构建器与查看器

- [x] 写 test_offline_bundle.py：全量消息、分页块、图片视频及失效附件、转义、预算、受信任下载。
- [x] 运行测试确认功能缺失，按规格实现 config/media/bundle 与离线 HTML/JS/CSS。
- [x] 运行测试及 Node 查看器筛选行为检查。

### 2. 后台任务与数据库接口

- [x] 写 test_offline_jobs.py/test_offline_api.py：状态持久化、取消/恢复、管理员鉴权、下载票据与内容。
- [x] 运行确认失败，实现 SQLite 任务存储及单线程后台消费；复用 ExportRepository._selection，单快照命名游标提取所有数据。
- [x] 运行测试并确认任务生成不持有数据库连接下载附件。

### 3. 界面集成与部署说明

- [x] 增加管理后台 HTML 留档入口、月份/范围选择、进度、刷新恢复及下载/取消。
- [x] 生命周期接入、默认目录忽略、模板和 README 说明；Excel 超限提示改为实际计数及配置项并回归验证。
- [x] 合成大批消息和可控媒体实际生成离线包，验证包内容及网页离线筛选。

### 4. 验证与独立审查

- [x] 运行归档/报表/管理员相关回归，说明跳过的数据库测试。
- [x] 独立审查代码和规格，修复影响完整性、安全或可用性的问题。
- [x] 更新本文件的完成状态，报告部署方法与容量边界。

## 执行记录

- Task 1: complete — 初次运行缺少模块失败，完成构建器、流式复制、去重与离线查看器后通过。额外 HTTP 服务测试验证下载、跳转、失效、超限及截断。
- Task 2: complete — 任务与 API 测试先因功能缺失失败，实现后通过。重启恢复、单线程隔离、进程锁、取消与发布互斥、鉴权及票据均覆盖。
- Task 3: complete — 管理脚本实际执行测试覆盖月份、全量、当前筛选、直接下载及退出后的响应隔离；现有后台页面回归通过。
- Task 4: complete — `C:/Users/lenovo/Anaconda3/envs/cs/python.exe -m pytest quality/tests tests/test_admin_routes.py -q --basetemp tmp/pytest-offline-final-verified`：689 passed、114 skipped、81 subtests passed；跳过项要求独立可写测试库，不在现有归档库制造数据。
- 真实只读归档库验证：全量 363 条、2026-10 月 351 条，SQL 计数与新提取器一致，消息身份无重复。
- 真实 Chrome `file://`、网络离线验证：2,101 条合成消息、100 条分页、末块关键词筛选、本地 PNG 与 WebM 加载通过，0 次 HTTP/HTTPS 请求。5,101 条消息包的逐条完整性另有组件测试。
- 静态检查：Python 编译、两份 JavaScript 语法、所改现有文件的 Git 空白检查通过。
- Final review: 独立全新上下文审查发现两个 Important（源端大小未核对、逐文件全目录扫描）和一个 Minor（Excel 配置变量未展示）。无范围搁置项。
- Final: fixed 附件不完整误成功 — 源端长度与响应长度不一致、缺少长度头、206 部分响应三种复现 RED→GREEN，失败文件清理。
- Final: fixed 附件量大时平方级扫描 — 75 个附件时完整目录扫描次数从 84 次改为初始化 1 次，测试 RED→GREEN；磁盘预算继续覆盖旧产物和 ZIP 重复空间。
- Excel 提示的配置变量属于用户明确要求的上限可调整说明，一并补齐；无遗留审查项。

## 实现裁决

- Ruling: Git 元数据不可写导致独立 worktree 创建失败 — 在现有非主分支工作区中仅修改本功能文件，不提交、推送或覆盖其他改动；保留用户已有删除及其他会话变更。
- Ruling: 月度范围按消息发送时间精确选择，包含未归轮消息 — 避免按客户完整历史扩大月份或按轮次遗漏当月消息；当前筛选仍保持已有 Excel 选择语义。
- Ruling: 输入预算与目录实际占用预算分别检查 — 旧包、双份消息、附件及 ZIP 同时占用磁盘，不能只检查源文件大小。
- Ruling: 附件元数据去重放入 SQLite，本地查看按块加载普通脚本 — 保持大量附件时内存有界，并兼容无需服务器直接打开 HTML。
- Ruling: 保留当前工作区成果，由既有发布流程更新 ICS — 本次没有更新运行服务或修改实际生产容量配置，部署方式与预算见 `quality/offline/README.md`。

## 运行环境修复：Windows 共享目录（2026-11-05）

- 用户反馈创建任意留档均显示通用请求失败。实际日志为 `sqlite3.OperationalError: unable to open database file`；容器读取留档目录文件、创建临时 SQLite 均报 PermissionError，上级 database 目录可写。Windows ACL 显示该目录只授权 SYSTEM、管理员及所有者。
- 根因为 Windows 下 `mkdir(mode=0700)` 创建的专用 ACL 与 Docker Desktop 共享目录不兼容。Windows 改为继承项目目录权限，Linux 保持 0700；已存在的本地留档目录使用 `icacls /reset /T /C` 恢复继承，未删除数据。
- 新增 Windows 目录权限模式回归及五个管理/下载接口的存储故障测试，先复现 6 项失败，修复后通过。存储错误现在返回明确的 503 提示。
- 当前运行服务实测正常登录、创建月度任务、后台完成、签名下载、ZIP CRC 及逐条消息计数：2026-10 月 351 条消息、2 个本地附件、0 个失效附件，下载 ZIP 140,914 字节。
