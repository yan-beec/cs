# 多渠道 FAQ、知识库和提示词使用说明

四个渠道共用原有 FAQ 优先、意图识别、检索、可选精排、回复、会话记忆和人工接管流程。渠道只决定使用哪份资源。

| 渠道标识 | 名称 | PostgreSQL FAQ 表 | 知识库逻辑集合 | 提示词文件 |
| --- | --- | --- | --- | --- |
| `xingliao` | 兴聊 | `faq_channel_xingliao` | `kb_channel_xingliao` | `prompt_xingliao.md` |
| `yanliao` | 言聊 | `faq_channel_yanliao` | `kb_channel_yanliao` | `prompt_yanliao.md` |
| `xingyingtong` | 星盈通 | `faq_channel_xingyingtong` | `kb_channel_xingyingtong` | `prompt_xingyingtong.md` |
| `wechat` | 微信 | `faq_channel_wechat` | `kb_channel_wechat` | `prompt_wechat.md` |

## 文件放置位置

目录名称沿用项目实际的 `database`。兴聊已移动的文件保持原位，其他渠道使用相同结构：

```text
database/
  channels.json
  channel_xingliao/
    最新问答对.xlsx
    intent_dataset_4000.csv
    prompt_xingliao.md
    knowledge_base/                   # 兴聊原有知识文档
  channel_yanliao/
    最新问答对.xlsx                    # 资料准备好后放入
    prompt_yanliao.md
    knowledge_base/                   # 资料准备好后放入
  channel_xingyingtong/
    最新问答对.xlsx                    # 资料准备好后放入
    prompt_xingyingtong.md
    knowledge_base/
  channel_wechat/
    最新问答对.xlsx                    # 资料准备好后放入
    prompt_wechat.md
    knowledge_base/
```

新渠道未放入示例业务答案，也不会读取兴聊的数据。意图模型和意图 CSV 暂时共用兴聊的原有版本；CSV 搬迁后继续严格校验模型记录的 SHA256。

`database/channels.json` 集中设置各渠道的 FAQ 工作簿、知识文档目录、分块输出位置、FAQ 表、知识库逻辑集合和提示词路径。需要改 Excel 文件名或文档目录时，修改对应渠道的 `faq_workbook` 或 `knowledge_dir`，然后重启应用。其他渠道仍可使用自己的路径。

## 导入 Excel 快速问答

每个工作簿的首个工作表必须包含以下列，列顺序可调整：

| question | answer | need_human | enabled |
| --- | --- | --- | --- |
| 问题文本 | 本渠道的答案文本 | 0 或 1 | 0 或 1 |

问题与答案不能为空，同一文件内的问题不能重复；空白行会忽略。只有 `enabled=1` 且 `need_human=0` 的记录参与自动 FAQ 回复。额外列会忽略，ID 由数据库生成。

在“零新智能客服”管理后台左侧点击对应渠道，再上传 Excel，最后点击“更新 FAQ 数据库”。上传可以使用任意 `.xlsx` 文件名，后台会保存到该渠道配置的工作簿路径。文件上传和数据更新是两个独立操作；更新会全量替换该渠道 FAQ，并只刷新该渠道的索引和答案缓存。

四个渠道分别有独立的内容管理页面，可使用 `/admin#channel/yanliao` 直接打开言聊渠道；其他渠道替换末尾渠道标识即可。左侧“系统日志”进入 `/admin#logs`，集中展示全部渠道的运行日志。日志页每 2 秒自动刷新，离开页面后暂停自动刷新。

直接放入文件后，也可在项目根目录执行：

```powershell
& 'C:\Users\lenovo\Anaconda3\envs\cs\python.exe' -m ics_agent.faq sync-excel --channel yanliao --refresh-runtime
```

命令行的运行时缓存属于当前命令行进程；常驻服务需要重启后读取该次数据库更新，或直接使用后台/HTTP 更新来立即刷新常驻服务缓存。

HTTP 更新沿用 `POST /faq/update?channel_key=yanliao`，以 multipart 的 `file` 字段提交工作簿，需要管理员 Bearer Token。返回的任务通过 `GET /faq/update/{job_id}?channel_key=yanliao` 查询。该上传接口保留原有临时文件行为；长期保存源文件请使用后台文件上传功能。

## 导入知识文档

将本渠道文档放入对应 `knowledge_base/` 目录，可使用子目录。文件格式、递归解析、分块、OCR 和向量重建逻辑沿用兴聊现有实现。不要把目录说明或提示词放在 `knowledge_base/` 中，避免被当成业务知识索引；`.gitkeep` 不参与解析。

管理后台选择渠道后，上传文档并点击“更新知识库”。更新只发布该渠道的向量版本；其他渠道继续使用各自当前的集合。新渠道分别使用 `knowledge_base_in/output/channel_<渠道>/chunks.jsonl`，兴聊保留原有分块输出位置以兼容现有部署挂载。

也可在项目根目录执行：

```powershell
& 'C:\Users\lenovo\Anaconda3\envs\cs\python.exe' -m knowledge_base_in.parse_documents --channel yanliao
```

HTTP 更新沿用 `POST /knowledge-base/update?channel_key=yanliao`，进度通过 `GET /knowledge-base/update/{job_id}?channel_key=yanliao` 查询，需要管理员认证。

空目录不会发布一次覆盖更新；空知识库查询返回空参考资料，回复遵守本渠道提示词，不会借用其他渠道知识。

## 调整提示词

编辑对应渠道根目录的 `prompt_<渠道>.md`。它是该渠道调用主模型和备用模型时使用的 system prompt，下一轮生成回复即可读取新内容。公共回答边界、日期时间、历史记忆和文档拼接继续使用现有逻辑。

兴聊保留原有提示词和 few-shot 示例；新渠道暂不引用含兴聊业务事实的示例。FAQ 命中仍直接返回表中答案，提示词不会重写 FAQ 原文。

## 绑定 Chatwoot 收件箱和 H5

`database/channels.json` 已配置以下收件箱绑定，部署时同步该文件并重启应用即可加载：

| 渠道 | account_id | inbox_id |
| --- | --- | --- |
| 兴聊 | 1 | 1 |
| 言聊 | 1 | 2 |
| 星盈通 | 1 | 3 |
| 微信 | 1 | 4 |

每个 `(account_id, inbox_id)` 只能绑定一个渠道。后端以 Webhook 中的收件箱来源确定渠道，不采用前端联系人属性中的 `channel` 值。未绑定的收件箱继续使用兴聊，以兼容现有入口；后续调整绑定后需要重启服务。

本地开发的自动重载只监听 Python 源码，修改 `channels.json` 不会自动生效。使用 Docker 的本地环境修改绑定后执行 `docker compose restart ics-agent`。启动日志会列出每个渠道已加载的收件箱绑定；处理消息时的 `webhook.channel.resolved` 日志记录 `account_id`、`inbox_id` 和 `channel_key`，可在后台“系统日志”核对实际选择的渠道。

各 Inbox 绑定现有机器人或 Webhook 地址 `POST /webhooks/chatwoot`。如果使用现有 H5，可访问：

```text
/support-chat?channel_key=yanliao&websiteToken=该收件箱的WebsiteToken
```

也可把 Website Token 填入该渠道配置的 `website_token` 字段，使用 `/support-chat?channel_key=yanliao`。它是浏览器使用的公开 Widget 标识。新渠道未配置 token 时会显示缺少配置，不会自动使用兴聊 token。

如使用本项目 `/chatwoot/profile` 签发身份签名，在 `.env` 中配置各 Inbox 的身份校验密钥：

```dotenv
CHATWOOT_CHANNEL_HMAC_TOKENS={"yanliao":"言聊Inbox密钥","xingyingtong":"星盈通Inbox密钥","wechat":"微信Inbox密钥"}
```

兴聊继续使用原来的 `CHATWOOT_HMAC_TOKEN`。密钥不放入 `channels.json`，也不返回浏览器。新渠道没有对应密钥时，身份接口返回未配置；使用业务后端身份接口时，由业务后端按对应 Inbox 签名。

## 本地验证与升级兼容

`POST /rag/query` 请求体可指定渠道，例如：

```json
{"query":"如何联系客服？","channel_key":"yanliao","thread_id":"test-user-1"}
```

省略渠道时仍使用兴聊。非兴聊渠道在记忆存储层增加渠道命名空间，兴聊原有会话 ID 保持兼容。删除非兴聊记忆时，调用 `DELETE /admin/memory?channel_key=yanliao&thread_id=test-user-1`。

应用启动时自动初始化四个渠道的表和知识库；手动初始化可执行：

```powershell
& 'C:\Users\lenovo\Anaconda3\envs\cs\python.exe' -m ics_agent.channels initialize
```

首次升级会将旧 `faq_qna_optimized` 数据复制到空的 `faq_channel_xingliao`，保留旧表，并记录一次迁移标记。日后清空或更新新表不会重新导入旧数据。

兴聊 `kb_channel_xingliao` 使用现有 active 指针机制引用旧的 `knowledge_base` 物理集合，保留原向量和检索行为；下一次正常重建将发布新名称下的版本集合。其他渠道初始创建独立空集合。不要绕过 active 指针直接操作某个旧的物理集合。

旧的默认环境变量路径和资源名称会兼容映射到新位置；其他自定义覆盖仍然支持。服务器代码更新流程通常会保留 `database/`，因此部署此次改造时，需要同时添加 `channels.json`、各渠道提示词与目录，并将兴聊文件调整到新位置。修改后的解析脚本也需要随镜像或后端代码包更新。
