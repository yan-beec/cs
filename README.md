# ics_agent

独立运行的智能客服服务。

当前边界：

- Chatwoot Webhook 接收、签名校验和事件解析。
- FAQ/MySQL 读取。
- Redis 缓存。
- BM25 相似问题召回。
- Chatwoot API 自动回复。
- 转人工策略和会话状态。

FAQ 数据同步脚本位于 `ics_agent/faq/sync_questions_to_redis.py`。

当前版本先完成服务骨架和 Webhook 接收，不会自动回复 Chatwoot 用户消息。

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

## 云端 LLM 工厂

`ics_agent/llm_factory` 基于 LangChain 的 `init_chat_model` 创建云端聊天模型，
模型配置统一维护在根目录 `.env` 的 `LLM_*` 配置中。默认模型为
`openai:gpt-4o-mini`，调用时不会在代码中绑定具体厂商模型名称。

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

LLM 并发控制也统一放在 `.env`：

```env
LLM_MAX_CONCURRENCY=5
LLM_CONCURRENCY_WAIT_TIMEOUT=5
```

`LLM_MAX_CONCURRENCY` 是单个 Python 进程内该云端模型允许的最大在途请求数，
普通调用和结构化调用共用同一个异步 semaphore。两个调用函数都是异步函数，
内部使用 LangChain 的 `ainvoke()`，业务侧需要使用 `await` 调用。
`LLM_CONCURRENCY_WAIT_TIMEOUT`
表示请求等待 semaphore 的最长时间；超时会抛出
`LLMConcurrencyLimitError`，上游可以将其转为稍后重试或 RAG/兜底路由。

这个限制在每个 Python 进程的事件循环内独立计算。如果启动 4 个 Web 服务进程，
`LLM_MAX_CONCURRENCY=5` 的实际总并发上限约为 20；多进程或多实例部署时，
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

解析配置统一在 `.env` 的 `KNOWLEDGE_BASE_*` 中维护：

```env
KNOWLEDGE_BASE_INPUT_DIR=database/knowledge_base
KNOWLEDGE_BASE_OUTPUT_PATH=knowledge_base_in/output/chunks.jsonl
KNOWLEDGE_BASE_CHUNK_SIZE_TOKENS=256
KNOWLEDGE_BASE_CHUNK_OVERLAP_TOKENS=32
KNOWLEDGE_BASE_TOKENIZER_DIR=models/google-bert--bert-base-chinese/snapshots/master
KNOWLEDGE_BASE_INCLUDE_HEADING_CONTEXT=true
KNOWLEDGE_BASE_DROP_HEADING_ONLY_CHUNKS=true
KNOWLEDGE_BASE_OCR_ENABLED=true
KNOWLEDGE_BASE_OCR_TEXT_SCORE=0.5
```

安装依赖后，从项目根目录执行：

```powershell
C:\Users\lenovo\Anaconda3\envs\cs\python.exe `
  knowledge_base_in\parse_documents.py
```

默认输出：

```text
knowledge_base_in/output/chunks.jsonl
knowledge_base_in/output/chunks.manifest.json
```

每行 JSON 包含稳定的块 ID、文本、token 数以及来源、文件类型、页码和块序号等
元数据。命令行参数可以临时覆盖输入目录、输出路径、块大小和重叠大小。

启动前从 Excel 更新 FAQ 数据库，并刷新 Redis 快照：

```powershell
C:\Users\lenovo\Anaconda3\envs\cs\python.exe `
  ics_agent\faq\faq_base_in\sync_qna_to_mysql.py
```

脚本默认读取根目录 `.env` 中 `FAQ_SOURCE_XLSX_PATH` 指定的
`database/最新问答对.xlsx`，以 Excel 的 `id` 为主键执行幂等更新，并删除
Excel 中已不存在的旧记录。MySQL 更新在单个事务中完成；Redis 不可用时只记录
告警，不影响本次 MySQL 更新。若只需要更新 MySQL，可追加
`--skip-redis-sync`；若不希望删除 Excel 中不存在的数据库记录，可追加
`--keep-extra`。默认在最新 FAQ 快照刷新成功后，只清空 FAQ 专属的
`query-answer` 缓存和访问索引，不会执行 `FLUSHDB`，也不会影响 Redis 中其他
业务数据。可用 `--keep-answer-cache` 临时跳过缓存清理。

也可以单独从 MySQL 同步 FAQ 快照到 Redis：

```powershell
C:\Users\lenovo\Anaconda3\envs\cs\python.exe ics_agent\faq\sync_questions_to_redis.py
```

从 Redis 中的分词问题列表初始化 BM25：

```powershell
C:\Users\lenovo\Anaconda3\envs\cs\python.exe ics_agent\faq\init_bm25.py
```

脚本默认读取 `${REDIS_KEY_PREFIX}faq:question_tokens`，也可以通过 `--redis-key` 指定其他键。

使用 BM25 检索 FAQ，并将命中的 `用户问题 -> answer` 写入 Redis 的 FAQ Hash：

```powershell
C:\Users\lenovo\Anaconda3\envs\cs\python.exe ics_agent\faq\search_faq.py "怎么充值"
```

默认匹配度阈值由根目录 `.env` 中的 `FAQ_MATCH_THRESHOLD` 设置，也可以通过
`--threshold` 临时覆盖。BM25 参数、FAQ 表名、分词词典和 Redis 键后缀也统一在
`.env` 的 `FAQ_*` 配置中维护。
应用端口、MySQL/Redis/RedisInsight 端口和其他连接参数也在 `.env` 中维护。
启动同步时是否自动清理 FAQ 答案缓存由
`FAQ_CLEAR_ANSWER_CACHE_ON_SYNC` 控制，默认值为 `true`。

后台上传 Excel 时应调用
`ics_agent.faq.faq_base_in.refresh_faq_runtime_from_excel()`。该入口会按顺序
完成 MySQL 更新、Redis FAQ 快照刷新、FAQ 答案缓存清理和当前进程 BM25
重建；上传接口不需要分别调用多个命令行脚本。多进程部署时，其他进程需要通过
进程间通知或重启后重新初始化 BM25。

启动 FAQ 常驻执行逻辑并处理 query：

```powershell
C:\Users\lenovo\Anaconda3\envs\cs\python.exe ics_agent\faq\faq_runtime.py "怎么充值"
```

`faq_runtime.py` 会在首次初始化时加载 FAQ 快照并构建一次内存 BM25；后续 query
先查 Redis FAQ Hash，命中直接返回，未命中再做 BM25。未达到阈值时返回
`rag_query`，供二级 RAG 链路使用。`FAQ_SYNC_ON_STARTUP=true` 时启动会从 MySQL
刷新 FAQ 快照到 Redis。

MySQL 使用连接池，默认最多 20 个连接。FAQ query 缓存会进行 query 规范化，
默认使用 86400 秒滑动 TTL，并最多保留 10000 条记录，对应配置为
`MYSQL_POOL_SIZE`、`MYSQL_CONNECTION_TIMEOUT`、`REDIS_CONNECT_TIMEOUT`、
`REDIS_SOCKET_TIMEOUT`、`REDIS_FAILURE_COOLDOWN_SECONDS`、`FAQ_CACHE_TTL_SECONDS` 和
`FAQ_CACHE_MAX_ENTRIES`。

FAQ 的故障降级策略如下：

- MySQL 启动同步失败时，优先加载 Redis 中最近一次可用的 FAQ 快照。
- Redis 缓存读取失败时，继续执行 BM25。
- Redis 缓存写入失败时，仍返回 MySQL 查询到的答案。
- Redis 连接失败后，在熔断窗口内跳过 Redis，避免每次请求重复等待超时。
- MySQL 查询失败或连接池耗尽时，返回 `rag` 路由结果。
- MySQL 和 Redis 都不可用且没有可用快照时，直接返回 `rag`，不让请求崩溃。

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
faq.service.initialize.mysql_sync_failed
faq.service.initialize.redis_snapshot_fallback
faq.service.initialize.failed
faq.snapshot.sync.start
faq.snapshot.sync.success
mysql.pool.create.start
mysql.pool.create.success
mysql.pool.exhausted
mysql.faq.questions.loaded
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
tokenizer.py              Jieba 分词器
repositories.py           MySQL 连接池、FAQ 数据仓库、Redis 快照和缓存
index.py                  BM25 索引与匹配度计算
service.py                FAQ 同步、初始化、缓存查询和 FAQ/RAG 路由
sync_questions_to_redis.py  FAQ 快照同步 CLI
init_bm25.py              BM25 初始化 CLI
search_faq.py             单次 FAQ 检索 CLI
faq_runtime.py            常驻 FAQ 服务 CLI 和调用入口
faq_base_in/sync_qna_to_mysql.py  Excel 问答对到 MySQL 的启动同步 CLI
```

应用目录的模块职责：

```text
config.py                 环境配置与配置校验
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

预训练模型从根目录 `.env` 的 `RAG_PRETRAINED_MODEL_DIR` 加载，默认使用
`models/google-bert--bert-base-chinese/snapshots/master`，不会联网下载。
微调后的模型默认写入 `RAG_OUTPUT_DIR` 指定的
`models/intent_classifier_bert`，模型目录已加入 `.gitignore`。

训练配置统一在根目录 `.env` 的 `RAG_*` 配置中维护。
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
`text`、`label` 和 `label_id` 三列。默认测试集路径由 `.env` 中的
`RAG_EVAL_DATASET_PATH` 配置，当前为 `database/eval.csv`。

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

Webhook：

```text
POST http://127.0.0.1:3001/webhooks/chatwoot
```

## 独立 Compose 连接地址

因为 Chatwoot 和 CS 暂时保持两个独立 Compose 项目，宿主机运行的 `ics_agent` 使用：

```env
CHATWOOT_BASE_URL=http://127.0.0.1:3000
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
REDIS_URL=redis://127.0.0.1:6380/0
```

如果将来把 `ics_agent` 容器加入共享 Docker 网络，再改成对应的容器服务名。
