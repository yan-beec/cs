# ics_agent

基于 FastAPI 的智能客服服务，面向 Chatwoot 提供 FAQ 优先、RAG 兜底的自动问答能力。

一次查询的处理顺序如下：

```text
用户问题
  -> 进程内 FAQ / BM25 匹配
  -> FAQ 未命中
  -> 意图识别
  -> ChromaDB + BGE-M3 向量召回
  -> Cross-Encoder 精排
  -> 云端 LLM 生成回复
```

服务还包含：

- Chatwoot Webhook 接收、签名校验、幂等处理和后台队列。
- PostgreSQL 对话记忆和 FAQ 数据存储。
- 进程内 FAQ BM25 索引，命中后直接返回内存答案。
- 管理后台，用于维护知识库、FAQ Excel 和查看日志。
- 本地文档解析、OCR、向量库重建和意图分类模型训练工具。

## 快速开始

### 1. 准备环境

项目使用 Python 3.10。先安装依赖并创建环境文件：

```powershell
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

运行前需要准备以下本地模型目录：

```text
models/
├── bge-m3/
├── bge-reranker-v2-m3/
├── google-bert--bert-base-chinese/
│   └── snapshots/master/
└── intent_classifier_bert/
```

其中 `intent_classifier_bert` 是项目运行时使用的意图分类模型，可以通过后文的训练命令生成。模型文件不包含在 Git 仓库中。

### 2. 启动 PostgreSQL

如果使用项目自带的 Compose：

```powershell
docker compose up -d postgres
```

未通过 `.env` 覆盖时的默认映射端口：

| 服务 | 地址 |
| --- | --- |
| PostgreSQL | `127.0.0.1:5432` |

复制 `.env.example` 后，`POSTGRES_HOST_PORT` 会覆盖 Compose 默认值为 `5433`。请根据实际启动方式统一修改 `POSTGRES_PORT` 和 `POSTGRES_HOST_PORT`。

### 3. 启动服务

```powershell
python -m ics_agent.app.serve --host 0.0.0.0 --port 3001 --reload
```

启动器会将 Uvicorn 输出写入 `log/`，并自动轮转日志文件。

常用地址：

```text
管理后台：http://127.0.0.1:3001/admin
健康检查：http://127.0.0.1:3001/health
客服入口：http://127.0.0.1:3001/support-chat
```

远端部署时，建议通过 Nginx、Caddy 或云负载均衡将 `3001` 代理到公网域名，
然后在 `.env` 中配置浏览器能够访问的 Chatwoot 地址：

```dotenv
CHATWOOT_PUBLIC_BASE_URL=https://chat.example.com
```

此时客服入口为：

```text
https://cs.example.com/support-chat
```

`CHATWOOT_BASE_URL` 是 `ics-agent` 容器访问 Chatwoot API 的地址，
`CHATWOOT_PUBLIC_BASE_URL` 是浏览器加载 Website Widget 时使用的地址，两者
可以不同。若页面放在独立域名下，还需要把该页面域名加入
`CHATWOOT_PROFILE_ALLOWED_ORIGINS`。

当前页面允许没有用户标识时以匿名访客进入。正式接入 APP 登录态时，不要把
`userId` 作为唯一可信身份来源；应由业务后端根据登录令牌生成
`identifier` 和 `identifier_hash`，并将：

```dotenv
CHATWOOT_PROFILE_ALLOW_QUERY_IDENTIFIERS=false
```

作为生产配置。

也可以直接使用应用 Compose：

```powershell
docker compose up -d --build
```

Compose 项目名称固定为 `cs`，容器和数据卷会自动使用 `cs` 命名空间，
例如 `cs-postgres-1` 和 `cs_postgres_data`，避免与服务器上其他
Compose 项目使用相同名称。

默认的 `docker-compose.override.yml` 会额外挂载 `ics_agent/` 源码目录，并启用热重载。
首次启动或修改依赖、Dockerfile 后执行：

```powershell
docker compose up -d --build ics-agent
```

只修改 Python 代码时通常会自动重载；如果需要手动重启：

```powershell
docker compose restart ics-agent
```

Compose 会挂载 `ics_agent/`、`models/`、`database/`、知识库解析结果、向量库和日志目录。
如果要按生产镜像方式运行（不挂载本地源码、不启用热重载），请显式停用覆盖文件：

```powershell
docker compose -f docker-compose.yml up -d --build ics-agent
```

## 配置

所有运行时配置从项目根目录的 `.env` 读取。建议至少修改：

```dotenv
APP_ENV=production
ADMIN_INITIAL_PASSWORD=替换为管理员初始密码
ADMIN_JWT_SECRET=替换为随机长密钥
LLM_MODEL=deepseek-flash
LLM_MODEL_PROVIDER=openai
LLM_API_KEY=你的模型服务密钥
LLM_BASE_URL=https://api.deepseek.com
```

主要配置分组：

| 配置 | 作用 |
| --- | --- |
| `POSTGRES_*` / `POSTGRES_DSN` | PostgreSQL 连接 |
| `LLM_MODEL`、`LLM_MODEL_PROVIDER`、`LLM_API_KEY`、`LLM_BASE_URL` | 云端 LLM |
| `RAG_WARM_UP_ON_STARTUP`、`RAG_INTENT_DATASET_PATH` | 是否在启动时加载本地模型、意图分类 CSV 路径 |
| `CHATWOOT_*` | Chatwoot Webhook 和自动回复 |
| `ADMIN_*` | 管理员认证 |
| `KNOWLEDGE_BASE_*` | 文档解析、Embedding 和向量库 |

生产环境必须替换 `.env.example` 中的默认密码和密钥。管理员初始密码只在首次创建管理员记录时生效，之后修改环境变量不会覆盖数据库密码。

## 查询接口

### 健康检查

```http
GET /health
```

### RAG 查询

```http
POST /rag/query
Content-Type: application/json

{
  "query": "怎么充值？",
  "thread_id": "local:test-session"
}
```

`thread_id` 可选。传入相同的 ID 时，服务会复用 PostgreSQL 中的对话记忆。
Chatwoot Webhook 默认按同一个 `conversation_id` 生成稳定 `thread_id`；
`RAG_MEMORY_RETENTION_DAYS=0` 时长期保留该记忆，不会因时间自动清理。

返回结果包含：

- 最终路由：`faq` 或 `rag`。
- FAQ 命中信息和匹配度。
- 意图分类结果。
- 召回和精排数量。
- 最终答案及精排文档摘要。

### Chatwoot Webhook

```http
POST /webhooks/chatwoot
```

Chatwoot 需要发送以下请求头：

```text
X-Chatwoot-Timestamp
X-Chatwoot-Signature
```

当前推荐通过 AgentBot Webhook 接入，使用 `CHATWOOT_AGENT_BOT_SECRET` 校验签名。
`CHATWOOT_WEBHOOK_SECRET` 仅用于普通 Account Webhook；不使用普通 Webhook 时保持为空即可。

Webhook 只处理带文本的入站消息：

- 相同事件只会入队一次。
- 任务在后台执行，接口先返回 `202`。
- 失败后最多重试 3 次，并使用指数退避。
- 只包含 Emoji 的消息不会进入 RAG，会返回默认提示。
- 默认只记录答案，不主动回复 Chatwoot。
- 普通 AI 处理期间会将 Chatwoot 会话保持为 `pending`，不会以 `open` 状态出现在人工座席待接管队列。
- 用户明确输入“人工”或“转人工”时，会先关闭该会话的 AI 回复开关，再执行 Chatwoot 人工接管。
- 转人工前会优先读取当前会话 Inbox 的坐席可用状态；如果配置了 Team/Agent，
  还会按对应范围过滤。仅当目标范围中存在 `availability_status=online` 的坐席
  时才转人工，不再按固定 09:00-22:00 时间段判断。`busy`、`offline` 或无法读取
  坐席状态时不会打开人工会话，
  会向用户提示当前暂无在线空闲坐席并保持 AI 会话可继续承接。
- 人工接管会打开会话、添加 `handoff_required` 标签、写入私有备注，并按配置分配 Team/Agent。
- 人工接管后，当前 Chatwoot 对话在坐席结束本轮处理前不会再自动回复。
- 坐席在 Chatwoot 中点击“已解决”后，下一条用户消息会重新进入 AI。
- 同一个 Chatwoot 对话会继续复用长期历史，不需要新建窗口或切换 `thread_id`。
- 会话状态类 Webhook 的幂等键包含请求体哈希，避免同一会话第二次标记
  `resolved`、`pending` 或 `open` 时被旧事件记录误判为重复。

配置以下变量后才会发送自动回复：

```dotenv
CHATWOOT_BASE_URL=http://127.0.0.1:3000
CHATWOOT_API_TOKEN=你的 Chatwoot API Token
CHATWOOT_ACCOUNT_ID=你的 Account ID
CHATWOOT_HMAC_TOKEN=Website Inbox 的 HMAC token
CHATWOOT_WEBHOOK_SECRET=
# 当前推荐使用 AgentBot Webhook；普通 Account Webhook 不使用时保持为空。
# AgentBot 设置中的 Secret；使用 AgentBot Webhook 时必须配置
CHATWOOT_AGENT_BOT_SECRET=Chatwoot AgentBot Secret
# AgentBot 的 Access Token 和 ID，让自动回复以 AgentBot 身份写回
CHATWOOT_AGENT_BOT_ACCESS_TOKEN=Chatwoot AgentBot Access Token
CHATWOOT_AGENT_BOT_ID=Chatwoot AgentBot ID
CHATWOOT_AUTO_REPLY_ENABLED=true
CHATWOOT_HANDOFF_ENABLED=true
CHATWOOT_HANDOFF_LABEL=handoff_required
CHATWOOT_HANDOFF_NOTICE_ENABLED=true
# 转人工和坐席轮询都使用 Webhook 事件中的当前 Inbox；服务不执行跨 Inbox 迁移。
# 可选；配置后转人工前只检查该 Team 下的在线坐席。
CHATWOOT_HANDOFF_TEAM_ID=
# 可选；配置后只在该坐席在线时转人工。
CHATWOOT_HANDOFF_ASSIGNEE_ID=
# 每位在线坐席最多同时承接的 open 人工会话数，默认 5。
CHATWOOT_HANDOFF_MAX_ACTIVE_CONVERSATIONS=5
# 读取坐席在线状态、open 会话数，以及更新状态/分配都需要用户级 API Token，不能只配置 AgentBot Token。
# CHATWOOT_CONTACT_API_TOKEN 未配置时回退使用 CHATWOOT_API_TOKEN。
# Chatwoot 静音联系人默认 7 天后自动解除；单位为秒。
CHATWOOT_BLOCK_DURATION_SECONDS=604800
CHATWOOT_BLOCK_REAPER_INTERVAL_SECONDS=60
CHATWOOT_BLOCK_REAPER_BATCH_SIZE=100
```

### 人工接管与同 Inbox 轮询

当前版本不会通过 API 把 Website 会话迁移到另一个 Inbox，也不会在人工结束后把会话回流到 AI Inbox。转人工时，服务只读取 Webhook 事件中当前 Inbox 的在线坐席，并按轮询和每人最多 5 个 open 会话的限制分配；配置 `CHATWOOT_HANDOFF_TEAM_ID` 时会再取 Team 与当前 Inbox 的交集。

如果要使用 ics-agent 的自动轮询，请把人工坐席加入 Website Widget 所在的 Inbox，并保持该 Inbox 的 AgentBot 配置不变。人工结束并将会话标记为 `resolved` 后，服务只释放本地人工接管状态，会话仍留在原 Inbox；下一次用户消息是否重新进入 AI 流程由该 Inbox 的 AgentBot/Webhook 配置决定。

如果必须让 AI Inbox 完全不包含人工坐席，再把会话交给独立的人工 Inbox，需要先在实际 Chatwoot 版本中验证该渠道支持的迁移方式和 Widget 连续性，再启用独立迁移流程；不要仅凭 `PATCH .../conversations/:id` 的成功响应判断迁移完成。

转人工由同一个 `POST /webhooks/chatwoot` 入口触发，不需要把
Chatwoot API Token 暴露给前端。服务会调用 Chatwoot API 完成会话状态更新、标签、
私有备注、分配和一次性公开提示；Webhook
重试时这些动作按本地 PostgreSQL 会话状态逐项幂等处理。服务需要接收
Chatwoot 的会话状态变更 Webhook，至少覆盖坐席将会话标记为
`resolved` 的事件；如果没有状态事件，服务会尝试读取当前状态，
同时普通用户消息触发的 Chatwoot `open` 会在 AI 处理开始和回复完成后尽力同步回
`pending`；状态同步失败不会阻断 RAG 或已生成的 AI 回复，只有明确转人工路径会保持
`open`。如果历史状态中残留
`handoff_required=true` 但最后已知状态已经是 `resolved`，服务启动
和下一条用户消息都会自愈释放该人工轮次。

Chatwoot 的 Mute 会将联系人标记为 `blocked`，但不会阻止外部 AgentBot
主动调用会话打开接口。因此，`ics-agent` 会在转人工前检查联系人静音状态；
已静音联系人不会被转入人工队列，并会收到“非常抱歉，当前人工座席繁忙，请稍后再试。”
的提示。座席通过 Chatwoot 静音联系人后，服务默认将该状态记录 7 天，
后台任务到期后调用 Chatwoot 联系人 API 自动设置 `blocked=false`。
可通过 `CHATWOOT_BLOCK_DURATION_SECONDS` 调整时长；设置为 `604800` 即 7 天。
联系人 API 必须使用用户 API Token，不能使用 AgentBot Token；可配置
`CHATWOOT_CONTACT_API_TOKEN`，未配置时回退到 `CHATWOOT_API_TOKEN`。

### Website Widget 用户身份和历史会话

`support-chat.html` 默认调用：

```http
GET /chatwoot/profile?identifier=10001&name=张三
```

服务端使用 `CHATWOOT_HMAC_TOKEN` 生成 `identifier_hash`，H5 再调用
Chatwoot `setUser`。这样即使 `file://` 页面重开、浏览器无法持久化
`cw_conversation` cookie，Chatwoot 仍能把新入口归到同一个 Contact，并检索
该用户已验证的历史会话。

`CHATWOOT_HMAC_TOKEN` 只能放在 `ics_agent` 后端环境变量中，不能放进 H5。
本地联调可以设置：

```dotenv
CHATWOOT_PROFILE_ALLOW_QUERY_IDENTIFIERS=true
```

正式环境应把 `PROFILE_ENDPOINT` 替换成业务后端接口，由登录态返回
`identifier`、`identifier_hash`、`name` 等资料，并保持
`CHATWOOT_PROFILE_ALLOW_QUERY_IDENTIFIERS=false`。

如果 Website Widget 在启用 HMAC 前已经产生过带明确 `identifier` 的历史入口，
需要在 Chatwoot PostgreSQL 中执行一次迁移，让这些历史入口也能被安全查询。
只迁移已有明确身份的 Contact，不合并匿名访客：

```sql
UPDATE contact_inboxes ci
SET hmac_verified = TRUE
FROM contacts c
WHERE c.id = ci.contact_id
  AND ci.inbox_id = <Website Inbox ID>
  AND c.identifier IS NOT NULL
  AND ci.hmac_verified = FALSE;
```

查询 Webhook 后台任务：

```http
GET /admin/webhook-jobs/{event_key}
Authorization: Bearer <access_token>
```

## 管理后台

打开 `http://127.0.0.1:3001/admin`，使用 `.env` 中的管理员账号登录。后台支持：

- 浏览、上传、预览、下载和删除 `database/knowledge_base` 下的文件。
- 上传和预览 `database/最新问答对.xlsx`。
- 提交 FAQ 全量更新任务。
- 提交知识库解析和向量库重建任务。
- 查看服务进程内的日志缓冲。

登录接口：

```http
POST /auth/login
Content-Type: application/json

{
  "username": "Denpans",
  "password": "管理员密码"
}
```

登录成功后，将返回 JWT。受保护接口使用：

```text
Authorization: Bearer <access_token>
```

## FAQ 数据

FAQ 源文件默认是：

```text
database/最新问答对.xlsx
```

FAQ 数据会同步到 PostgreSQL 的 `faq_qna_optimized` 表，并在服务进程内生成 BM25 索引和答案字典。达到匹配阈值时直接返回内存中的 FAQ 答案，否则进入 RAG。

命令行同步：

```powershell
# Excel -> PostgreSQL
python -m ics_agent.faq sync-excel

# 查询 FAQ；未命中时返回 rag 路由
python -m ics_agent.faq query "怎么充值"
```

FAQ Excel 至少应包含以下字段：

```text
question
answer
need_human
enabled
```

管理员后台更新和命令行同步都会执行全量替换。FAQ 运行时初始化失败时，服务会降级到 RAG。

## 知识库和向量库

知识库源文件位于：

```text
database/knowledge_base/
```

解析流程：

1. 递归读取文本、Markdown、PDF、Word、图片等文件。
2. 对图片执行 OCR，对文档按 Token 切分。
3. 将分块写入 `knowledge_base_in/output/chunks.jsonl`。
4. 使用本地 BGE-M3 生成 dense embedding。
5. 将结果写入 `knowledge_base_in/vector_store/chroma`。

手动执行：

```powershell
# 解析文档并重建向量库
python knowledge_base_in/parse_documents.py

# 只解析，不写入向量库
python knowledge_base_in/parse_documents.py --skip-vector-store

# 单独重建现有 chunks.jsonl 对应的向量库
python knowledge_base_in/build_chromadb.py

# 测试 dense 检索
python ics_agent/rag/retrieval/dense_search.py "实名认证一直审核不成功"
```

应用内的知识库更新接口会在后台执行完整的解析和向量库重建：

```http
POST /knowledge-base/update
Authorization: Bearer <access_token>
```

查询任务：

```http
GET /knowledge-base/update/{job_id}
Authorization: Bearer <access_token>
```

知识库更新会保留现有 Chroma collection 对象，并清理旧记录后写入新内容，避免运行中的检索器继续引用已删除的 collection。

## 意图分类模型

训练数据默认由 `RAG_INTENT_DATASET_PATH` 指定，默认值是：

```text
database/intent_dataset_4000.csv
```

CSV 至少包含 `text`、`label` 和 `label_id` 三列。`label` 和 `label_id`
是分类器唯一的类别定义，训练器会从 CSV 动态生成 `num_labels`、
`label2id`、`id2label` 和模型标签元数据，代码和配置不再固定业务类别或类别数量。
每个 `label_id` 必须从 0 开始连续编号，并且每个类别至少需要两条样本。
训练使用本地中文 BERT，默认按类别进行 8:2 分层切分，模型输出到：

```text
models/intent_classifier_bert/
```

训练：

```powershell
python -m ics_agent.rag.intent_classifier.train
```

指定数据集：

```powershell
python -m ics_agent.rag.intent_classifier.train `
  --dataset database\intent_dataset_4000.csv `
  --eval-ratio 0.2 `
  --seed 42
```

新增或调整意图时，只需准备符合上述格式的新 CSV，并重新执行训练命令。
训练产物会保存该 CSV 的路径、标签映射和文件指纹；应用启动时优先使用模型
元数据记录的 CSV，并校验它是否仍与模型一致，避免继续使用旧分类模型。
重新训练后重启应用即可加载新的类别。

使用独立标注集评估：

```powershell
python -m ics_agent.rag.intent_classifier.test `
  --dataset database\eval.csv
```

## 本地命令行查询

根目录的 `main.py` 提供交互式查询和单次查询：

```powershell
# 进入交互模式
python main.py

# 单次查询
python main.py --query "我刚才咨询的充值问题是什么？"

# 使用固定会话 ID 复用对话记忆
python main.py `
  --thread-id local:test-session `
  --query "我刚才说的充值问题还没解决"
```

## 项目结构

```text
ics_agent/
├── app/                    FastAPI、认证、Webhook、管理后台
├── faq/                    FAQ 同步、内存 BM25 查询
├── rag/
│   ├── graph/              LangGraph 查询流程
│   ├── retrieval/          BGE-M3 + ChromaDB 检索
│   ├── reranker/           Cross-Encoder 精排
│   ├── memory/             PostgreSQL 对话记忆
│   └── intent_classifier/  意图分类模型训练和评估
└── llm_factory/            云端 LLM 初始化和调用

knowledge_base_in/          文档解析和向量库构建工具
database/                   FAQ、知识库和训练数据
```

## 许可证

当前仓库未声明开源许可证。
