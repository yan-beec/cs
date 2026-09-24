# ics_agent

基于 FastAPI 的智能客服服务，面向 Chatwoot 提供 FAQ 优先、RAG 兜底的自动问答能力。

一次查询的处理顺序如下：

```text
用户问题
  -> FAQ 缓存 / BM25 匹配
  -> FAQ 未命中
  -> 意图识别
  -> ChromaDB + BGE-M3 向量召回
  -> Cross-Encoder 精排
  -> 云端 LLM 生成回复
```

服务还包含：

- Chatwoot Webhook 接收、签名校验、幂等处理和后台队列。
- PostgreSQL 对话记忆和 FAQ 数据存储。
- Redis FAQ 快照、查询缓存和运行时 BM25 索引。
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

### 2. 启动 PostgreSQL 和 Redis

如果使用项目自带的 Compose：

```powershell
docker compose up -d postgres redis redisinsight
```

未通过 `.env` 覆盖时的默认映射端口：

| 服务 | 地址 |
| --- | --- |
| PostgreSQL | `127.0.0.1:5432` |
| Redis | `127.0.0.1:6380` |
| RedisInsight | `http://127.0.0.1:5540` |

复制 `.env.example` 后，`POSTGRES_HOST_PORT` 会覆盖 Compose 默认值为 `5433`，Redis 会映射到 `6380`。请根据实际启动方式统一修改 `POSTGRES_PORT`、`POSTGRES_HOST_PORT` 和 `REDIS_URL`。

### 3. 启动服务

```powershell
python -m ics_agent.app.serve --host 0.0.0.0 --port 3001 --reload
```

启动器会将 Uvicorn 输出写入 `log/`，并自动轮转日志文件。

常用地址：

```text
管理后台：http://127.0.0.1:3001/admin
健康检查：http://127.0.0.1:3001/health
```

也可以直接使用应用 Compose：

```powershell
docker compose up -d --build
```

Compose 项目名称固定为 `cs`，容器和数据卷会自动使用 `cs` 命名空间，
例如 `cs-postgres-1`、`cs-redis-1` 和 `cs_postgres_data`，避免与服务器上其他
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
ADMIN_INITIAL_PASSWORD=替换为管理员初始密码
ADMIN_JWT_SECRET=替换为随机长密钥
LLM_API_KEY=你的模型服务密钥
LLM_BASE_URL=https://api.deepseek.com
```

主要配置分组：

| 配置 | 作用 |
| --- | --- |
| `POSTGRES_*` / `POSTGRES_DSN` | PostgreSQL 连接 |
| `REDIS_URL` | Redis 连接 |
| `LLM_MODEL`、`LLM_MODEL_PROVIDER`、`LLM_API_KEY`、`LLM_BASE_URL` | 云端 LLM |
| `RAG_WARM_UP_ON_STARTUP`、`RAG_INTENT_DATASET_PATH` | 是否在启动时加载本地模型、意图分类 CSV 路径 |
| `FAQ_SYNC_ON_STARTUP` | 是否在启动时刷新 FAQ Redis 快照 |
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

当 `CHATWOOT_WEBHOOK_SECRET` 已配置时，服务会按 Chatwoot 的时间戳签名格式校验原始请求体，默认只接受前后 5 分钟内的请求。

Webhook 只处理带文本的入站消息：

- 相同事件只会入队一次。
- 任务在后台执行，接口先返回 `202`。
- 失败后最多重试 3 次，并使用指数退避。
- 只包含 Emoji 的消息不会进入 RAG，会返回默认提示。
- 默认只记录答案，不主动回复 Chatwoot。
- 普通 AI 处理期间会将 Chatwoot 会话保持为 `pending`，不会以 `open` 状态出现在人工座席待接管队列。
- 用户明确输入“人工”或“转人工”时，会先关闭该会话的 AI 回复开关，再执行 Chatwoot 人工接管。
- 人工接管会打开会话、添加 `handoff_required` 标签、写入私有备注，并按配置分配 Team/Agent。
- 人工接管后，当前 Chatwoot 对话在坐席结束本轮处理前不会再自动回复。
- 坐席在 Chatwoot 中点击“已解决”或“待处理”后，下一条用户消息会重新进入 AI。
- 同一个 Chatwoot 对话会继续复用长期历史，不需要新建窗口或切换 `thread_id`。

配置以下变量后才会发送自动回复：

```dotenv
CHATWOOT_BASE_URL=http://127.0.0.1:3000
CHATWOOT_API_TOKEN=你的 Chatwoot API Token
CHATWOOT_ACCOUNT_ID=你的 Account ID
CHATWOOT_HMAC_TOKEN=Website Inbox 的 HMAC token
CHATWOOT_WEBHOOK_SECRET=与 Chatwoot 一致的密钥
CHATWOOT_AUTO_REPLY_ENABLED=true
CHATWOOT_HANDOFF_ENABLED=true
CHATWOOT_HANDOFF_LABEL=handoff_required
CHATWOOT_HANDOFF_NOTICE_ENABLED=true
# 可选；不配置时使用 Chatwoot 自身的自动分配规则。
CHATWOOT_HANDOFF_TEAM_ID=
CHATWOOT_HANDOFF_ASSIGNEE_ID=
```

转人工由同一个 `POST /webhooks/chatwoot` 入口触发，不需要把
Chatwoot Application API Token 暴露给前端。服务会调用 Chatwoot Application
API 完成会话状态更新、标签、私有备注、分配和一次性公开提示；Webhook
重试时这些动作按本地 PostgreSQL 会话状态逐项幂等处理。服务需要接收
Chatwoot 的会话状态变更 Webhook，至少覆盖坐席将会话标记为
`resolved` 或 `pending` 的事件；如果没有状态事件，服务会尝试读取当前状态，
同时普通用户消息触发的 Chatwoot `open` 会在 AI 处理开始和回复完成后同步回
`pending`；只有明确转人工路径会保持 `open`。

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

FAQ 数据会同步到 PostgreSQL 的 `faq_qna_optimized` 表，再生成 Redis 快照和进程内 BM25 索引。达到匹配阈值时直接返回 FAQ 答案，否则进入 RAG。

命令行同步：

```powershell
# Excel -> PostgreSQL -> Redis，并刷新运行时 FAQ
python -m ics_agent.faq sync-excel

# PostgreSQL -> Redis
python -m ics_agent.faq sync-redis

# 从 Redis 快照构建 BM25
python -m ics_agent.faq build-index

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

管理员后台更新和命令行同步都会执行全量替换。FAQ 更新失败时，服务会尽量使用已有 Redis 快照或降级到 RAG。

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
├── faq/                    FAQ 同步、Redis 快照、BM25 查询
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
