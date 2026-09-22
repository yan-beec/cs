# ics_agent

独立运行的智能客服服务。

## 管理后台

管理员控制台默认访问地址：

```text
http://127.0.0.1:3001/admin
```

访问地址和端口可在 `.env` 中调整：

```dotenv
ADMIN_WEB_HOST=127.0.0.1
ADMIN_WEB_PORT=3001
```

管理后台仅允许唯一管理员账号 `Denpans` 登录。登录后可以查看、上传和删除
`database/knowledge_base` 下的文件，覆盖 `database/最新问答对.xlsx`，并提交知识库
和 FAQ 数据库更新任务。知识库文件支持在线预览和下载，FAQ Excel 支持按工作表在线浏览
和下载。知识库和 FAQ 更新按钮会在执行前要求确认全量覆盖，并由后端再次校验确认参数。
系统日志页面会同步当前服务进程保留的日志缓冲。

当前边界：

- Chatwoot Webhook 接收、签名校验和事件解析。
- FastAPI 常驻进程调用 RAG MVP，并提供 `/rag/query` 测试接口。
- FAQ/PostgreSQL 读取。
- Redis 缓存。
- BM25 相似问题召回。
- Chatwoot API 自动回复。
- 转人工策略和会话状态。

FAQ 的数据库、Redis 快照和命令行入口已经收敛到 `ics_agent/faq/sync.py`
和 `ics_agent/faq/cli.py`。

当前版本的 Chatwoot Webhook 会对带文本的入站消息执行 RAG MVP，并将答案返回
给 Webhook 调用方；暂未调用 Chatwoot API 将答案写回会话。

## 本地运行

在项目根目录 `D:\project\CS` 执行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
uvicorn ics_agent.app.main:app --host 0.0.0.0 --port 3001 --reload
```

项目根目录的 `requirements.txt` 是整个项目唯一的 Python 依赖清单。

## PostgreSQL 和 Redis 容器

当前 Compose 需要以下镜像：

```text
postgres:16.15-alpine3.23
redis:7.4.11-alpine3.21
redis/redisinsight:3.8.0
```

ChromaDB 使用本地持久化目录，不需要单独的 ChromaDB 镜像；RAG 模型也从项目根目录
的 `models` 目录加载。若构建 `ics_agent` 应用容器，还需要从
`python:3.10.21-slim-bookworm` 构建本地应用镜像，与项目当前的 Python 3.10
运行环境保持一致。

PostgreSQL 只负责提供数据库服务。应用启动时会检查
`faq_qna_optimized` 表：如果表已存在则不修改；如果表不存在，则自动创建
FAQ 所需的字段和索引，然后再执行 FAQ 初始化。FAQ 表包含：
`id`、`question`、`answer`、`category`、`tags`、`similar_questions`、
`need_human`、`enabled`、`priority`、`answer_type`、`updated_at`。
首次创建表后，仍需要执行一次 Excel 同步脚本写入 FAQ 数据。
`.env` 中的 `POSTGRES_DSN` 留空时，FAQ 会使用拆分的连接配置；后续接入
LangGraph PostgreSQL checkpointer 时，可复用 `Settings.postgres_connection_string`。

## 云端 LLM 工厂

`ics_agent/llm_factory` 基于 LangChain 的 `init_chat_model` 创建云端聊天模型。
模型提供商、模型名、API 密钥和 Base URL 等连接初始化配置维护在根目录
`.env`；调用超时、重试、输出长度和进程内并发限制维护在
`ics_agent/app/config.py`。当前默认模型名为 `deepseek-flash`，调用时不会在
代码中绑定具体厂商模型名称。

普通调用：

```python
from ics_agent.llm_factory import call_cloud_llm

response = await call_cloud_llm(
    "请用一句话说明什么是退款审核。",
    system_prompt="你是智能客服助手。",
)
print(response.content)
```

结构化调用：

```python
from pydantic import BaseModel

from ics_agent.llm_factory import call_structured_llm


class IntentResult(BaseModel):
    intent: str
    confidence: float


result = await call_structured_llm(
    "用户说：账号被限制流量了怎么办？",
    IntentResult,
    system_prompt="请识别用户意图并输出结构化结果。",
)
print(result.intent, result.confidence)
```

需要更换模型时，修改 `.env` 中的 `LLM_MODEL`；切换到其他 LangChain
provider 时，还需要在 `requirements.txt` 中加入对应的 provider integration package。

LLM 并发控制统一在 `ics_agent/app/config.py` 的 `AppSettings` 中维护：

```python
llm_max_concurrency = 5
llm_concurrency_wait_timeout = 5
```

`llm_max_concurrency` 是单个 Python 进程内该云端模型允许的最大在途请求数，
普通调用和结构化调用共用同一个异步 semaphore。两个调用函数都是异步函数，
内部使用 LangChain 的 `ainvoke()`，业务侧需要使用 `await` 调用。
`llm_concurrency_wait_timeout`
表示请求等待 semaphore 的最长时间；超时会抛出
`LLMConcurrencyLimitError`，上游可以将其转为稍后重试或 RAG/兜底路由。

这个限制在每个 Python 进程的事件循环内独立计算。如果启动 4 个 Web 服务进程，
`llm_max_concurrency=5` 的实际总并发上限约为 20；多进程或多实例部署时，
应按实例数分摊供应商允许的总并发。semaphore 只控制并发数，不能替代供应商
的 RPM/TPM 限流器；如果仍遇到 429，还需要增加按请求数或 token 速率的限流。

## 知识库文档解析

`knowledge_base_in/parse_documents.py` 会递归解析
`database/knowledge_base` 下的文档。文本类文件主要使用 LangChain loader，
图片文件使用 RapidOCR；Markdown 和其他文本会使用中文优先的
`RecursiveCharacterTextSplitter`，按本地 BERT tokenizer 的真实 token 数限制为
每块 256 token，默认重叠 32 token。Markdown 会先识别标题层级，再在章节正文
内部切分；每个 chunk 会把当前章节的标题路径直接并入 `text`，并同步写入
`heading_path`、`heading_levels` 和 `heading_context` 元数据。只有标题、空白或
分隔线的内容不会生成 chunk，避免把标题孤块作为检索结果。

解析、分块、OCR、BGE-M3、ChromaDB 和 dense 召回配置统一在
`ics_agent/rag/config.py` 的 `RAGSettings` 中维护。根目录 `.env` 只保留
应用环境、连接信息、密钥和 `RAG_WARM_UP_ON_STARTUP` 等启动初始化项。

安装依赖后，从项目根目录执行：

```powershell
C:\Users\lenovo\Anaconda3\envs\cs\python.exe `
  knowledge_base_in\parse_documents.py
```

该命令会递归读取 `database/knowledge_base`，先生成可直接查看的：

```text
knowledge_base_in/output/chunks.jsonl
knowledge_base_in/output/chunks.manifest.json
```

输出文件写入完成后，脚本会通过异步线程自动执行 ChromaDB 向量库全量重建，
默认删除旧的 `knowledge_base` collection 后重新写入所有 chunk。命令完成时会
同时输出解析 manifest 和向量库 manifest。

每行 JSON 包含稳定的块 ID、文本、token 数以及来源、文件类型、页码和块序号等
元数据。命令行参数可以临时覆盖输入目录、输出路径、块大小和重叠大小。若只想
执行解析、不更新向量库，可以追加 `--skip-vector-store`。

### 后台触发知识库全量更新

FastAPI 已提供后台更新接口，后台管理页面的“更新知识库”按钮可以直接调用：

```text
POST /knowledge-base/update
```

接口会固定递归处理 `RAGSettings.knowledge_base_input_dir` 指定的
`database/knowledge_base` 目录，完成文档解析、chunk 输出和 ChromaDB
collection 全量重建。接口会立即返回任务编号，不等待 BGE-M3 向量计算完成。

查询任务状态：

```text
GET /knowledge-base/update/{job_id}
```

建议后台按钮按以下状态处理：

- `submitted` / `running`：按钮置为“更新中”，继续轮询状态接口；
- `completed`：显示本次解析 chunk 数和向量库记录数；
- `failed`：显示错误信息，允许重新点击；
- `already_applied`：表示知识库目录内容没有变化，无需重复重建；
- HTTP `409`：已有另一份知识库更新任务运行中。

如果需要强制重新生成，即使源目录内容没有变化，也可以调用：

```text
POST /knowledge-base/update?force=true
```

这个任务管理器在当前进程内保证单任务执行和重复提交幂等；跨多个
FastAPI worker 时，建议将该按钮路由固定到单独 worker，或后续把任务锁和
状态迁移到 Redis/PostgreSQL。

### 后台管理员登录

FAQ 更新和知识库更新接口现在要求系统管理员登录。应用首次启动时会在
PostgreSQL 中创建 `system_users` 表，并且只初始化一条 `id=1` 的管理员记录：

```text
username: Denpans
```

密码来自根目录 `.env` 的 `ADMIN_INITIAL_PASSWORD`，仅在管理员记录首次创建时
生效，后续重启不会覆盖数据库中的密码。密码在 PostgreSQL 中只保存 PBKDF2
哈希。登录接口：

```text
POST /auth/login
Content-Type: application/json

{
  "username": "Denpans",
  "password": "你的管理员密码"
}
```

登录成功后将返回 JWT，后台请求需要携带：

```text
Authorization: Bearer <access_token>
```

受保护接口：

- `POST /faq/update`
- `GET /faq/update/{job_id}`
- `POST /knowledge-base/update`
- `GET /knowledge-base/update/{job_id}`

`ADMIN_INITIAL_PASSWORD` 和 `ADMIN_JWT_SECRET` 仅用于本地初始配置，部署前必须
替换为真实密钥。数据库表结构也提供在 `database/system_users.sql` 中。

## ChromaDB 向量入库

`knowledge_base_in/build_chromadb.py` 会读取已经生成的
`knowledge_base_in/output/chunks.jsonl`，使用项目根目录
`models/bge-m3` 中的本地 BGE-M3 模型生成归一化 dense embedding，并写入持久化
ChromaDB collection。chunk 的完整 `text` 会作为向量库 document，来源、标题路径、
标题上下文和 token 数等信息会写入 metadata。

首次安装或环境更新后执行：

```powershell
C:\Users\lenovo\Anaconda3\envs\cs\python.exe -m pip install -r requirements.txt
```

从项目根目录构建向量库：

```powershell
C:\Users\lenovo\Anaconda3\envs\cs\python.exe `
  knowledge_base_in\build_chromadb.py
```

脚本默认重建 `knowledge_base` collection，防止已经从源文档删除的旧 chunk
继续留在向量库中。向量库默认写入
`knowledge_base_in/vector_store/chroma`，该目录已加入 `.gitignore`。
如果只想对现有 collection 执行 upsert，可以追加 `--no-reset`。

模型加载和向量计算支持自动 GPU 检测：检测到 CUDA 时使用 GPU，否则使用 CPU。
可以通过 `RAGSettings.knowledge_base_embedding_batch_size` 调整显存占用，
通过 `RAGSettings.knowledge_base_chroma_upsert_batch_size` 调整 ChromaDB
批量写入大小。

当前项目使用 ChromaDB `0.6.x` 的本地 dense collection 接口。BGE-M3 只生成
dense embedding，向量和原始 chunk 文本、metadata 一起写入 ChromaDB，不生成
任何 sparse 旁路索引，也不参与 FAQ/BM25 快速匹配层。

构建 dense 检索索引：

```powershell
C:\Users\lenovo\Anaconda3\envs\cs\python.exe `
  knowledge_base_in\build_chromadb.py
```

执行 dense 检索：

```powershell
C:\Users\lenovo\Anaconda3\envs\cs\python.exe `
  ics_agent\rag\retrieval\dense_search.py "实名认证一直审核不成功"
```

入库和检索前需要安装 `FlagEmbedding`，用于加载本地 BGE-M3 并生成 dense
embedding。查询结果中的 `rank` 和 `distance` 可用于观察 ChromaDB 的 dense
召回结果。

在生产服务中不要在每个用户请求里重新创建 `DenseRetriever`。通过
`get_dense_retriever()` 获取进程级单例，embedding 模型会在该进程内复用；
应用启动时可以用 `get_dense_retriever(warm_up=True)` 提前加载模型，避免第一
个用户请求承担模型加载耗时。多进程部署时，每个 worker 会各自保留一份模型实例。

## RAG 检索模块

RAG 链路可以直接使用 `ics_agent.rag.retrieval`，默认从
`ics_agent/rag/config.py` 的 `RAGSettings.knowledge_base_retrieval_top_k`
读取召回数量。异步链路建议使用
`aretrieve_documents()`：

```python
from ics_agent.rag.retrieval import aretrieve_documents

documents = await aretrieve_documents(user_query)
for document in documents:
    print(document.chunk_id, document.text, document.metadata)
```

应用启动时可以提前预热：

```python
from ics_agent.rag.retrieval import get_retrieval_service

get_retrieval_service(warm_up=True)
```

## RAG Cross-Encoder 精排

`ics_agent.rag.reranker` 使用本地
`models/bge-reranker-v2-m3` 对 dense 检索召回的 Top-20 文档进行 cross-encoder
精排，默认返回 Top-5。模型会在进程内保持单例，并自动检测 GPU；异步推理通过
线程池执行，不阻塞事件循环。

重排模型、Top-K、批大小、最大长度、FP16 和并发配置统一在
`ics_agent/rag/config.py` 的 `RAGSettings` 中维护。

应用启动时预热：

```python
from ics_agent.rag.reranker import get_reranker_service

get_reranker_service(warm_up=True)
```

完整执行召回和精排：

```python
from ics_agent.rag.reranker import retrieve_and_rerank

documents = await retrieve_and_rerank(user_query)
```

如果已经有 dense 召回结果：

```python
from ics_agent.rag.reranker import arerank_documents

reranked_documents = await arerank_documents(
    user_query,
    retrieved_documents,
)
```

## RAG MVP 图

当前 MVP 图位于 `ics_agent/rag/graph`，固定执行以下顺序：

```text
用户 query
  -> 意图识别
  -> dense retrieval Top-20
  -> cross-encoder reranker Top-5
  -> 云端 LLM 回复
```

当前意图识别结果只写入状态，不执行条件分支；后续可以基于
`intent_label`、`intent_confidence` 和 `route` 增加 FAQ、闲聊或 RAG 的条件边。

异步执行：

```python
from ics_agent.rag.graph import run_rag_graph

state = await run_rag_graph(user_query)
answer = state["answer"]
```

应用启动时预热整条本地模型链路：

```python
from ics_agent.rag.graph import get_rag_graph

get_rag_graph(warm_up=True)
```

启动前从 Excel 更新 FAQ 数据库，并刷新 Redis 快照：

```powershell
C:\Users\lenovo\Anaconda3\envs\cs\python.exe `
  -m ics_agent.faq sync-excel
```

脚本默认读取 `ics_agent/faq/config.py` 中 `FAQSettings.faq_source_xlsx_path`
指定的 `database/最新问答对.xlsx`，以 Excel 的 `id` 为主键执行幂等更新，并删除
Excel 中已不存在的旧记录。PostgreSQL 更新在单个事务中完成；Redis 不可用时只记录
告警，不影响本次 PostgreSQL 更新。若只需要更新 PostgreSQL，可追加
`--skip-redis-sync`；若不希望删除 Excel 中不存在的数据库记录，可追加
`--keep-extra`。默认在最新 FAQ 快照刷新成功后，只清空 FAQ 专属的
`query-answer` 缓存和访问索引，不会执行 `FLUSHDB`，也不会影响 Redis 中其他
业务数据。可用 `--keep-answer-cache` 临时跳过缓存清理。

也可以单独从 PostgreSQL 同步 FAQ 快照到 Redis：

```powershell
C:\Users\lenovo\Anaconda3\envs\cs\python.exe -m ics_agent.faq sync-redis
```

从 Redis 中的分词问题列表初始化 BM25：

```powershell
C:\Users\lenovo\Anaconda3\envs\cs\python.exe -m ics_agent.faq build-index
```

脚本默认读取 `${REDIS_KEY_PREFIX}faq:question_tokens`，也可以通过 `--redis-key` 指定其他键。

使用 BM25 检索 FAQ，并将命中的 `用户问题 -> answer` 写入 Redis 的 FAQ Hash：

```powershell
C:\Users\lenovo\Anaconda3\envs\cs\python.exe -m ics_agent.faq query "怎么充值"
```

默认匹配度阈值由 `ics_agent/faq/config.py` 中的
`FAQSettings.faq_match_threshold` 设置，也可以通过 `--threshold` 临时覆盖。
BM25 参数、FAQ 表名、分词词典、缓存 TTL、最大容量和 Redis 键后缀也统一在
`FAQSettings` 中维护。
应用端口、PostgreSQL/Redis/RedisInsight 端口和其他连接参数也在 `.env` 中维护。
启动同步时是否自动清理 FAQ 答案缓存由
`FAQSettings.faq_clear_answer_cache_on_sync` 控制，默认值为 `true`。

后台上传 Excel 时应调用
`ics_agent.faq.sync.refresh_faq_runtime_from_excel()`。该入口会按顺序
完成 PostgreSQL 更新、Redis FAQ 快照刷新、FAQ 答案缓存清理和当前进程 BM25
重建；上传接口不需要分别调用多个命令行脚本。多进程部署时，其他进程需要通过
进程间通知或重启后重新初始化 BM25。

启动 FAQ 常驻执行逻辑并处理 query：

```powershell
C:\Users\lenovo\Anaconda3\envs\cs\python.exe -m ics_agent.faq query "怎么充值"
```

FAQ 运行时会在首次初始化时加载 FAQ 快照并构建一次内存 BM25；后续 query
先查 Redis FAQ Hash，命中直接返回，未命中再做 BM25。未达到阈值时返回
`rag_query`，供二级 RAG 链路使用。`FAQ_SYNC_ON_STARTUP=true` 时启动会从 PostgreSQL
刷新 FAQ 快照到 Redis。

PostgreSQL 使用连接池，默认最多 20 个连接。FAQ query 缓存会进行 query 规范化，
默认使用 864000 秒滑动 TTL，并最多保留 10000 条记录。PostgreSQL、Redis
连接超时和连接池参数由 `ics_agent/app/config.py` 管理；FAQ 缓存 TTL 和最大容量
由 `ics_agent/faq/config.py` 的 `FAQSettings` 管理。

FAQ 的故障降级策略如下：

- PostgreSQL 启动同步失败时，优先加载 Redis 中最近一次可用的 FAQ 快照。
- Redis 缓存读取失败时，继续执行 BM25。
- Redis 缓存写入失败时，仍返回 PostgreSQL 查询到的答案。
- Redis 连接失败后，在熔断窗口内跳过 Redis，避免每次请求重复等待超时。
- PostgreSQL 查询失败或连接池耗尽时，返回 `rag` 路由结果。
- PostgreSQL 和 Redis 都不可用且没有可用快照时，直接返回 `rag`，不让请求崩溃。

## 日志规划

项目使用 Python 标准库 `logging`，统一由
`ics_agent/app/logging_config.py` 配置。日志名称沿用模块路径，例如：

```text
ics_agent.app.main
ics_agent.faq.service
ics_agent.faq.repositories
ics_agent.faq.index
```

业务日志使用 `event=...` 标记，主要节点如下：

```text
app.startup
webhook.signature.invalid
webhook.received
faq.service.initialize.start
faq.service.initialize.success
faq.service.initialize.postgres_sync_failed
faq.service.initialize.redis_snapshot_fallback
faq.service.initialize.failed
faq.snapshot.sync.start
faq.snapshot.sync.success
postgres.pool.create.start
postgres.pool.create.success
postgres.pool.exhausted
postgres.faq.questions.loaded
redis.snapshot.load.success
redis.snapshot.save.success
redis.circuit.open
redis.circuit.skip
redis.answer_cache.read
redis.answer_cache.write.success
redis.answer_cache.clear.success
redis.answer_cache.clear.failed
faq.bm25.initialized
faq.query.start
faq.query.route
```

`faq.query.route` 会记录 `cache`、`faq` 或 `rag` 路由、命中问题 ID、
匹配度和耗时，但不会记录完整用户 query 或 answer。日志级别通过根目录
`.env` 的 `LOG_LEVEL` 控制。

FAQ 目录的模块职责：

```text
models.py                 FAQ 文档、快照、匹配结果和路由结果
config.py                FAQ、数据库、Redis 和匹配参数
index.py                 Jieba 分词、BM25 索引与匹配度计算
repositories.py          PostgreSQL 连接池、FAQ 数据仓库、Redis 快照和缓存
service.py               FAQ 初始化、查询、运行时更新和 FAQ/RAG 路由
sync.py                  Excel 校验、PostgreSQL 替换、Redis 刷新和运行时重建
cli.py                   FAQ 查询、同步和 BM25 构建命令
__main__.py              `python -m ics_agent.faq` 命令入口
```

应用目录的模块职责：

```text
config.py                 应用、基础设施、认证和外部服务配置
logging_config.py         全局日志配置与事件日志辅助函数
models.py                 Chatwoot Webhook 事件模型
webhook.py                Webhook 签名校验与事件解析
main.py                   FastAPI 应用、健康检查和 Webhook 入口
```

## RAG 意图分类微调模块

RAG 目录中的 `intent_classifier` 使用 `transformers.Trainer` 对项目根目录
`database/intent_dataset_4000.csv` 做中文意图分类微调。CSV 至少需要包含
`text`、`label` 和 `label_id` 三列；当前数据集包含 4 个类别，每类 1000 条。
训练前会按 `label_id` 做固定随机种子的 8:2 分层切分。

预训练模型从 `ics_agent/rag/config.py` 的
`RAGSettings.rag_pretrained_model_dir` 加载，默认使用
`models/google-bert--bert-base-chinese/snapshots/master`，不会联网下载。
微调后的模型默认写入 `RAGSettings.rag_output_dir` 指定的
`models/intent_classifier_bert`，模型目录已加入 `.gitignore`。

训练配置统一在 `ics_agent/rag/config.py` 的 `RAGSettings` 中维护。
训练启动时会自动检测 CUDA：检测到可用 GPU 时优先使用 `cuda:0`，否则显式
降级为 CPU，并在日志中记录实际设备和 GPU 信息。

安装新增依赖：

```powershell
C:\Users\lenovo\Anaconda3\envs\cs\python.exe -m pip install -r requirements.txt
```

数据集准备好后，可以这样启动训练：

```powershell
C:\Users\lenovo\Anaconda3\envs\cs\python.exe -m ics_agent.rag.intent_classifier.train
```

也可以指定数据集、验证集比例和随机种子：

```powershell
C:\Users\lenovo\Anaconda3\envs\cs\python.exe -m ics_agent.rag.intent_classifier.train `
  --dataset database\intent_dataset_4000.csv `
  --eval-ratio 0.2 `
  --seed 42
```

训练模块会自动完成：

- 加载本地 BERT tokenizer 和分类模型。
- 校验 `label` 与 `label_id` 的一一对应关系。
- 按类别进行确定性分层切分。
- 对文本进行截断和动态 padding。
- 使用 `Trainer` 执行训练、保存 checkpoint 和最终模型。
- 计算 accuracy 和 macro F1。
- 保存 tokenizer、标签映射和最终模型。

训练过程中的 checkpoint 会写入临时目录，训练结束后只保留验证集表现最佳的
最终模型，不会在 `models/intent_classifier_bert` 下保留多个 checkpoint。

训练完成后，可以传入一份独立的、带真实标签的 CSV 测试集进行全量评估。
测试脚本不会切分测试集，CSV 中的每一行都会参与评估；测试集至少需要包含
`text`、`label` 和 `label_id` 三列。默认测试集路径由
`RAGSettings.rag_eval_dataset_path` 配置，当前为 `database/eval.csv`。

```powershell
C:\Users\lenovo\Anaconda3\envs\cs\python.exe ics_agent\rag\intent_classifier\test.py
```

也可以临时覆盖默认测试集：

```powershell
C:\Users\lenovo\Anaconda3\envs\cs\python.exe ics_agent\rag\intent_classifier\test.py `
  --dataset database\another_eval.csv
```

测试结果会输出 accuracy、macro F1、每个类别的 precision/recall/F1 和混淆矩阵，
并记录实际使用的 CPU 或 GPU。

所有意图分类训练相关代码统一位于 `ics_agent.rag.intent_classifier`。

构建智能客服镜像时，也要从项目根目录执行：

```powershell
docker build -t ics-agent .
```

健康检查：

```text
GET http://127.0.0.1:3001/health
```

常驻 FastAPI 进程中测试完整 RAG MVP：

```powershell
$body = @{ query = "怎么充值" } | ConvertTo-Json
Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:3001/rag/query `
  -ContentType "application/json" `
  -Body $body
```

`/rag/query` 会返回意图识别结果、召回数量、精排数量、精排文档摘要和
LLM 最终答案。服务启动时如果 `RAG_WARM_UP_ON_STARTUP=true`，意图模型、
BGE-M3、Reranker 和 LangGraph 会先完成预热，后续请求复用常驻实例。

Webhook：

```text
POST http://127.0.0.1:3001/webhooks/chatwoot
```

签名校验通过后，带有文本内容的入站 Chatwoot 消息会复用同一条 RAG 链路。
当前接口会把 RAG 结果返回给测试客户端，尚未调用 Chatwoot API 将答案发送回
会话；正式接入自动回复时需要再增加 Chatwoot 回复接口和异步任务队列。

## 独立 Compose 连接地址

因为 Chatwoot 和 CS 暂时保持两个独立 Compose 项目，宿主机运行的 `ics_agent` 使用：

```env
CHATWOOT_BASE_URL=http://127.0.0.1:3000
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5433
POSTGRES_DATABASE=cs
POSTGRES_USER=cs
POSTGRES_PASSWORD=cs_password
REDIS_URL=redis://127.0.0.1:6380/0
```

如果将来把 `ics_agent` 容器加入共享 Docker 网络，再改成对应的容器服务名。
