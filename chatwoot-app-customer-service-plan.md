# 智能客服与 Chatwoot 集成开发计划

## 1. 当前目标

当前不先做 APP H5 页面，也不先做 Website Widget 的正式接入。

开发顺序调整为：

```text
第一阶段：完成智能客服系统本身
    ->
第二阶段：验证智能客服与 Chatwoot 的端到端链路
    ->
第三阶段：将已验证的 Chatwoot 会话能力接入 APP H5 页面
```

第二阶段必须完成以下闭环：

```text
用户在 Chatwoot 会话中发消息
    ->
Chatwoot Webhook 投递到智能客服服务
    ->
智能客服优先判断并自动回复
    ->
用户命中转人工规则
    ->
智能客服停止继续回复
    ->
Chatwoot 会话进入人工处理状态
    ->
坐席可以正常接管并持续回复
```

只有第二阶段端到端验证通过后，才开始第三阶段的 APP、H5 和登录身份对接。

## 2. 总体架构

当前采用外部智能客服服务接入 Chatwoot Webhook 的方式，不修改 Chatwoot 核心代码，也不把 Chatwoot 管理端 Token 暴露给前端。

```text
Chatwoot 会话
    ->
Chatwoot message_created Webhook
    ->
ics_agent FastAPI
    ->
Webhook 签名校验
    ->
事件解析、入站消息过滤、幂等登记
    ->
后台 Webhook 队列
    ->
会话状态判断
    ->
转人工策略判断
    ->
FAQ-first 查询
    ->
LangGraph RAG
    ->
Chatwoot Application API
    ->
自动回复 / 转人工提示 / 私有备注 / 标签 / 分配
    ->
Chatwoot 坐席工作台
```

APP H5 页面在第三阶段才加入：

```text
APP
    ->
H5 客服页面
    ->
Chatwoot Website Widget
    ->
Chatwoot Conversation
    ->
复用第二阶段已经验证的 Webhook、自动回复和转人工链路
```

## 3. 当前项目实现基础

智能客服端已经具备以下基础能力：

- FastAPI 常驻服务。
- `POST /webhooks/chatwoot` Chatwoot Webhook 接收入口。
- `X-Chatwoot-Timestamp` 和 `X-Chatwoot-Signature` 签名校验。
- 仅处理带文本的 `incoming` 消息，避免处理坐席和机器人发出的消息。
- PostgreSQL Webhook 幂等登记，避免重复入队和重复处理。
- 异步 Webhook 队列、处理超时、指数退避和失败重试。
- FAQ-first 查询路径。
- FAQ 未命中时进入 LangGraph RAG 流程。
- 意图分类、向量召回、Cross-Encoder 重排和云端大模型回复。
- 基于 PostgreSQL checkpoint 的会话记忆。
- Chatwoot Application API 自动发送公开文本消息。
- 本地 `/rag/query` 测试接口和管理员任务状态查询接口。

对应的主要代码位置：

| 能力 | 当前代码 |
| --- | --- |
| FastAPI 应用和 Webhook 入口 | `ics_agent/app/main.py` |
| Chatwoot API 客户端 | `ics_agent/app/chatwoot_client.py` |
| Webhook 事件模型和签名校验 | `ics_agent/app/models.py`、`ics_agent/app/webhook.py` |
| Webhook 队列和重试 | `ics_agent/app/webhook_queue.py` |
| Webhook 幂等状态 | `ics_agent/app/webhook_idempotency.py` |
| 应用配置 | `ics_agent/app/config.py` |
| FAQ 查询 | `ics_agent/faq/` |
| RAG 查询编排 | `ics_agent/rag/main.py` |
| LangGraph RAG | `ics_agent/rag/graph/` |
| 对话记忆 | `ics_agent/rag/memory/` |
| H5 承载页原型 | `support-chat.html` |

当前尚未完成的关键能力：

- 显式转人工关键词和业务规则判断。
- 会话级人工接管状态。
- 人工接管后停止 AI 自动回复。
- 人工接管状态按 Chatwoot 会话轮次保存；坐席将会话标记为 `resolved` 或
  `pending` 后，下一条用户消息重新进入 AI。
- Chatwoot 会话标签、私有备注、团队或坐席分配。
- Chatwoot 与智能客服的完整端到端验收测试。

## 4. 第一阶段：完成智能客服系统

### 4.1 阶段目标

在不依赖 Chatwoot 的情况下，先确保智能客服本身可以稳定回答、正确使用知识库、保留必要的会话上下文，并能明确区分可自动回答和必须人工处理的问题。

### 4.2 FAQ-first + RAG 处理方式

当前项目不采用单一大模型直接回答，而是采用以下固定顺序：

```text
用户问题
    ->
FAQ 服务查询
    ->
命中 FAQ：直接返回 FAQ 答案
    ->
未命中 FAQ：进入 LangGraph RAG
    ->
意图分类
    ->
知识库向量召回
    ->
Cross-Encoder 重排
    ->
根据检索内容调用大模型生成最终回复
    ->
更新会话记忆
```

FAQ 数据继续使用当前项目的 `question`、`answer`、`need_human`、`enabled` 字段。

其中：

- `enabled=0` 的记录不参与正常查询。
- `need_human=1` 的记录不应作为普通自动回答直接返回。
- `need_human=1` 应作为人工处理信号，后续由转人工策略统一处理。
- FAQ 命中后仍然可以写入 RAG 会话记忆，保持多轮上下文连续性。

### 4.3 第一阶段需要完成的工作

1. 完成 FAQ 数据清洗和同步流程。
2. 确认知识库文档、切分结果、Chroma 向量库和重排模型可稳定加载。
3. 完成意图分类模型的训练、评估和本地加载。
4. 固定 RAG 的回答边界，禁止模型编造产品规则、处理结果和人工已完成的操作。
5. 增加无法回答、敏感问题和需要账号核验问题的人工引导策略。
6. 增加 FAQ、RAG、意图分类、检索失败和模型失败的测试样例。
7. 验证 PostgreSQL 不可用、FAQ 初始化失败、模型加载失败时的降级行为。
8. 验证会话记忆不会串会话，`thread_id` 按 Chatwoot 对话稳定生成，保证同一窗口历史持续。
9. 为转人工策略预留统一的结果字段，例如：

```text
answer
should_handoff
handoff_reason
handoff_source
intent_label
intent_confidence
faq_route
match_degree
```

### 4.4 第一阶段验收标准

- FAQ 命中问题可以返回稳定答案。
- FAQ 未命中问题可以进入 RAG。
- RAG 只能基于检索内容回答。
- 检索内容不足时可以明确说明无法确认，并给出人工处理建议。
- 同一个会话的多轮问题可以使用历史上下文。
- 不同会话之间不存在记忆串线。
- `need_human=1` 的问题不会被当作普通 FAQ 自动处理。
- 关键异常有日志，并且不会导致服务进程直接退出。
- `/rag/query` 可以用于独立验证查询结果和转人工判断结果。

## 5. 第二阶段：Chatwoot 与智能客服端到端连接

### 5.1 阶段目标

先使用 Chatwoot 自带的测试入口或 Website Inbox 测试会话验证后端链路，不接 APP H5，不接 APP 登录态。

本阶段只关注：

- Chatwoot 能否把用户消息投递给智能客服。
- 智能客服能否优先自动回复。
- 用户要求人工时能否停止 AI。
- 坐席能否在 Chatwoot 中顺利接管。

### 5.2 Chatwoot 侧配置

在 Chatwoot 中准备一个专用测试 Account、Inbox、Team 和 Agent：

1. 创建测试 Inbox。
2. 配置至少一个人工 Team 和一个坐席账号。
3. 配置自动分配规则，确保转人工后会话能进入人工处理范围。
4. 创建并确认以下标签：

```text
ai_handled
handoff_required
```

5. 配置 Webhook，至少监听用户消息创建相关事件。
6. 将 Webhook URL 指向：

```text
https://你的智能客服域名/webhooks/chatwoot
```

7. 设置 Webhook Secret，并写入智能客服端的：

```dotenv
CHATWOOT_WEBHOOK_SECRET=...
```

8. 创建供智能客服服务使用的 Chatwoot Application API Token。
9. 确认该 Token 可以：

- 发送公开消息；
- 发送私有备注；
- 更新会话状态；
- 给会话添加或移除标签；
- 分配 Team 或 Agent。

### 5.3 智能客服端配置

智能客服端使用项目现有的环境变量：

```dotenv
CHATWOOT_BASE_URL=http://可被ics-agent访问的Chatwoot地址
CHATWOOT_API_TOKEN=Chatwoot Application API Token
CHATWOOT_ACCOUNT_ID=Chatwoot Account ID
CHATWOOT_WEBHOOK_SECRET=Webhook Secret
CHATWOOT_AUTO_REPLY_ENABLED=true
CHATWOOT_API_TIMEOUT=15
```

部署连接需要注意：

- 如果 `ics_agent` 在 Docker 容器中，容器内的 `127.0.0.1` 指向容器自身，不一定是 Chatwoot。
- Chatwoot 在宿主机时，Docker Desktop 通常使用 `host.docker.internal`。
- 两套 Compose 使用共享 Docker 网络时，使用 Chatwoot 的服务名。
- 在开启自动回复前，先确认智能客服容器可以访问 Chatwoot API。

### 5.4 端到端消息流程

正式流程如下：

```text
用户发送消息
    ->
Chatwoot 产生 message_created
    ->
POST /webhooks/chatwoot
    ->
验证签名
    ->
解析 account、conversation、message_type、content
    ->
仅接受 incoming 文本消息
    ->
根据 account_id + event_id 做幂等登记
    ->
放入 WebhookJobQueue
    ->
读取会话控制状态
    ->
判断是否已经人工接管
    ->
判断本条消息是否需要转人工
    ->
需要人工：执行 handoff
    ->
不需要人工：FAQ-first，再进入 RAG
    ->
调用 Chatwoot messages API 发公开回复
```

当前项目已经实现签名校验、入站过滤、幂等、队列和自动回复；本阶段需要在此基础上补齐会话控制和转人工动作。

### 5.5 转人工策略

转人工判断必须发生在 FAQ 和 RAG 之前，避免用户明确要求人工时仍然被 AI 继续回答。

第一版规则：

- 用户明确输入“人工”“转人工”“人工客服”“客服”“投诉”等表达。
- 用户提出退款、支付失败、订单纠纷等需要人工核验的问题。
- 用户提出账号安全、封禁、实名、隐私或身份核验问题。
- FAQ 记录标记为 `need_human=1`。
- 意图分类结果属于需要人工处理的类别。
- 意图置信度低于配置阈值。
- 检索不到足够内容，或重排结果不足以支撑可靠回答。
- 用户连续否定回答，或连续多轮无法解决问题。

规则执行顺序：

```text
检查会话是否已人工接管
    ->
检查用户是否明确要求人工
    ->
检查 FAQ need_human
    ->
检查敏感业务和人工意图
    ->
检查置信度与检索质量
    ->
决定自动回复或转人工
```

### 5.6 转人工动作

转人工不能只回复一句“请发送转人工”，而要真正修改会话处理状态：

1. 在智能客服自己的 PostgreSQL 中将会话标记为人工接管。
2. 给 Chatwoot 会话添加 `handoff_required` 标签。
3. 给 Chatwoot 会话添加一条 private note，至少包含：

```text
转人工原因
用户原始问题
最近一次 AI 回复或 FAQ 命中情况
意图和置信度信息
```

4. 按配置将会话分配给指定 Team 或 Agent。
5. 仅在明确转人工时将会话切换为 `open`，确保坐席工作台可以接管；普通 AI 处理期间保持 `pending`，避免误出现在人工待接管队列。
6. 给用户发送一次公开提示：

```text
我已为你转接人工客服，请稍等，人工客服会继续为你处理。
```

7. 后续用户消息仍然可以通过 Webhook 到达智能客服，但只做记录和状态更新，不再调用 FAQ、RAG 或自动回复。
8. 坐席发送的 outgoing 消息不得再次触发 AI 回复。

建议新增会话控制表：

```text
chatwoot_conversation_control
--------------------------------
account_id
conversation_id
active_thread_id
last_chatwoot_status
ai_enabled
handoff_required
handoff_reason
handoff_at
assigned_team_id
assigned_agent_id
last_user_message_id
last_ai_message_id
last_handoff_message_id
created_at
updated_at
```

`ai_enabled` 是智能客服自己的控制状态，不依赖 Chatwoot 是否存在同名字段。
普通 AI 消息处理开始后，Chatwoot 会话状态同步为 `pending`；Chatwoot 收到用户
消息时可能短暂自动变为 `open`，但不代表人工接管。只有明确转人工动作才设置
为 `open`。AI 主动同步的 `pending` 不会释放人工接管状态，避免和坐席手动点击
`pending` 的结束人工信号混淆。
人工接管状态按 Chatwoot 对话轮次生效：同一轮人工处理中后续消息停止 AI；
坐席将会话标记为 `resolved` 或 `pending` 后，状态记录被释放，下一条用户消息
重新进入 AI，同时继续复用同一个 Chatwoot 对话的长期历史。Chatwoot 的标签、
备注、分配和会话状态用于坐席侧展示与运营，数据库状态用于智能客服判断是否继续处理。
状态类 Webhook 的幂等键需要带请求体哈希，不能只用会话 id 和状态；同一会话
可能多次进入 `open -> resolved/pending -> open` 的新轮次，旧的 `resolved`
记录不能阻止后续轮次释放人工接管。服务启动和新用户消息处理时，也应对
`handoff_required=true` 且最后已知状态为 `resolved` 或 `pending` 的历史脏状态做自愈释放。

### 5.7 Chatwoot API 客户端扩展方向

在当前 `ChatwootClient.send_message()` 的基础上，补充面向会话操作的方法：

```text
send_message(...)
add_conversation_labels(...)
remove_conversation_labels(...)
create_private_note(...)
update_conversation(...)
assign_conversation(...)
```

所有 Chatwoot API 调用统一复用：

- `CHATWOOT_BASE_URL`
- `CHATWOOT_API_TOKEN`
- `CHATWOOT_API_TIMEOUT`
- 当前的 HTTP 错误处理和日志规范

转人工动作需要具备幂等性。相同会话重复收到相同消息或 Chatwoot 重试时，不能重复发送转人工提示、重复添加备注或重复创建分配记录。

### 5.8 第二阶段验收用例

| 用例 | 预期结果 |
| --- | --- |
| 用户发送普通 FAQ 问题 | Webhook 到达，智能客服自动回复 |
| 用户发送知识库可回答问题 | FAQ 未命中时进入 RAG，并在 Chatwoot 中看到回复 |
| 用户发送“转人工” | 不进入 RAG，执行转人工动作 |
| 用户发送退款、封禁、实名等问题 | 按规则转人工 |
| 转人工后用户继续发消息 | Webhook 可以接收，但 AI 不再自动回复 |
| 转人工后坐席回复 | 用户能正常收到坐席消息，智能客服不插话 |
| 智能客服自动回复触发 outgoing Webhook | 被过滤，不产生循环回复 |
| 相同 Webhook 重复投递 | 不重复自动回复或转人工动作 |
| Chatwoot API 短暂失败 | 按现有队列重试，最终记录失败原因 |
| Webhook 签名错误 | 返回 401，不进入队列 |
| Chatwoot 无法访问智能客服 | Chatwoot 仍可由坐席手动处理 |

第二阶段完成的标志是：可以在 Chatwoot 坐席工作台中完整观察“AI 自动回复 -> 用户要求人工 -> 会话进入人工 -> 坐席继续回复”的闭环。

## 6. 第三阶段：H5 页面和 APP 对接

### 6.1 阶段目标

第二阶段端到端链路稳定后，再把 Chatwoot 会话入口接入 APP。

此阶段不重新设计智能客服逻辑，H5 只负责创建或打开 Chatwoot Website Inbox 会话，后续消息继续复用第二阶段已经验证的 Webhook 和转人工链路。

### 6.2 H5 页面职责

当前 `support-chat.html` 可以作为承载页原型，正式接入时负责：

- 加载 Chatwoot Website Widget。
- 打开客服窗口。
- 设置当前用户身份。
- 设置业务自定义属性。
- 处理未读数、窗口打开和消息事件。
- 处理 APP WebView 的加载失败、返回和登录失效。

H5 不负责：

- 保存 Chatwoot Application API Token。
- 判断是否转人工。
- 调用智能客服内部管理接口完成转人工。
- 自己实现一套替代 Chatwoot 的聊天 UI。

### 6.3 用户身份与安全

如果 Website Inbox 使用登录用户身份，需要启用 Identity Validation：

```text
APP 登录态
    ->
H5 携带短期登录凭证
    ->
后端校验 APP 用户
    ->
后端生成 Chatwoot identifier_hash
    ->
H5 调用 setUser
    ->
Chatwoot 复用同一个 Contact 和 Conversation
```

Identity Validation 的 HMAC Secret 只能放在后端，不能写入 H5、APP 或公开 JavaScript。

### 6.4 第三阶段验收标准

- APP 可以打开 H5 客服页面。
- 用户身份可以稳定映射到 Chatwoot Contact。
- 用户发消息后，第二阶段的智能客服链路仍能收到消息并自动回复。
- 用户在 H5 中发送“转人工”后，坐席工作台可以正常接管。
- 坐席回复能在 H5 中正常展示。
- APP 刷新、重新进入和网络恢复不会错误创建重复会话。

## 7. 部署和环境

当前项目使用独立 Docker Compose 运行 PostgreSQL 和 `ics-agent`。Chatwoot 可以保持独立 Compose 项目，但必须解决两个 Compose 之间的网络访问。

推荐先使用以下拓扑完成第二阶段：

```text
Chatwoot Compose
    <- HTTP API
    <- Webhook HTTP
ics-agent Compose
```

至少确认：

1. Chatwoot 能访问智能客服的公开 Webhook 地址。
2. `ics-agent` 能访问 Chatwoot API 地址。
3. Webhook 签名密钥两端一致。
4. `CHATWOOT_ACCOUNT_ID` 与测试 Account 一致。
5. Chatwoot API Token 没有过期且权限足够。
6. PostgreSQL 的容器内地址与宿主机地址没有混用。
7. 端口、防火墙和反向代理不会拦截 Webhook。

## 8. 测试和观测

### 8.1 临时验证脚本

测试脚本按需在临时目录或临时文件中创建，验证完成后删除，不在项目根目录长期维护 `tests/` 测试目录。

需要覆盖：

- Webhook 签名校验。
- Webhook 事件解析。
- incoming、outgoing、private note 消息过滤。
- Webhook 幂等登记。
- 转人工关键词识别。
- 敏感业务和 `need_human` 判断。
- 会话人工状态判断。
- Chatwoot API 错误和超时。
- 转人工动作重复执行。

### 8.2 集成测试

使用测试 Chatwoot 实例验证：

- Webhook 可以到达 `/webhooks/chatwoot`。
- 普通消息可以自动回复。
- 自动回复不会形成循环。
- 转人工动作可以添加标签、备注和分配。
- 人工接管后 AI 不再回复。
- 坐席回复不会被智能客服拦截。

### 8.3 日志和指标

至少记录：

- Webhook event id、account id、conversation id。
- Webhook 排队、处理、重试和最终状态。
- FAQ 路由或 RAG 路由。
- 意图、置信度和转人工原因。
- Chatwoot API 调用结果。
- 人工接管时间。
- 坐席首次响应时间。

后续可统计：

- AI 自动解决率。
- 转人工率。
- Chatwoot API 失败率。
- Webhook 重试率。
- 人工首次响应时间。

## 9. 开发阶段总览

### 阶段一：智能客服本体

目标：FAQ、RAG、记忆、边界、异常降级和转人工判断结果稳定。

完成条件：不依赖 Chatwoot 也可以通过 `/rag/query` 验证查询和路由结果。

### 阶段二：Chatwoot 端到端链路

目标：消息接收、智能客服优先自动回复、转人工、坐席接管全部跑通。

完成条件：在 Chatwoot 坐席工作台完成完整验收用例。

### 阶段三：H5 和 APP 对接

目标：将已验证的 Chatwoot 会话入口接入 APP，并打通用户身份。

完成条件：APP 内可以完成 AI 回复、转人工和人工坐席回复全流程。

## 10. 当前下一步

按当前路线，下一步不做 H5 页面和 APP 对接，先完成：

1. 定义统一的 `should_handoff` 和 `handoff_reason` 结果。
2. 实现转人工关键词、敏感业务、FAQ `need_human` 和低置信度规则。
3. 增加智能客服自己的会话控制状态。
4. 扩展 `ChatwootClient`，支持标签、私有备注、会话更新和分配。
5. 在 Webhook 队列中接入“已人工接管则停止 AI”的判断。
6. 补充转人工动作的幂等处理。
7. 准备 Chatwoot 测试 Account、Inbox、Team、Agent、Webhook 和 API Token。
8. 先完成普通自动回复的端到端验证。
9. 再完成“转人工 -> 坐席接管 -> AI 停止插话”的端到端验证。
10. 第二阶段全部通过后，再开始 H5 页面和 APP 登录身份对接。
