# 基于 Chatwoot 的 APP 客服系统开发方案

## 1. 背景与目标

现有产品已经有成熟 APP，但缺少后端客服系统。计划使用 Chatwoot 作为坐席平台，通过 Website Inbox 的 H5 聊天窗口嵌入 APP WebView，让用户在 APP 内点击“联系客服”后直接进入 Chatwoot 对话。

同时，通过 Chatwoot 的 Webhook 机制接入一个前置智能客服服务，实现：

- 用户消息先经过智能客服判断。
- 常见问题由智能客服自动回复。
- 复杂、低置信度、敏感或用户主动要求人工的问题转交 Chatwoot 坐席。
- Chatwoot 继续作为人工坐席工作台、会话管理和客服运营平台。

## 2. 总体架构

```text
APP
  -> 客服入口
  -> 内嵌 WebView
  -> H5 客服承载页
  -> Chatwoot Website Widget
  -> Chatwoot Conversation
  -> Chatwoot Webhook
  -> 智能客服服务
       -> 知识库 / FAQ / 大模型
       -> Chatwoot API 自动回复
       -> 必要时转人工
  -> Chatwoot 坐席台
```

核心原则：

- APP 不直接持有 Chatwoot 管理端 API Token。
- H5 只负责加载 Chatwoot Widget、设置用户身份、展示聊天窗口。
- 智能客服服务独立部署，不直接修改 Chatwoot 核心代码。
- Chatwoot 作为坐席平台，负责人工会话、客服分配、标签、备注、统计和运营。

## 3. 接入方式选择

本方案使用 Chatwoot 的 Website Inbox，而不是 API Inbox。

原因：

- APP 只需要通过 WebView 嵌入现成聊天窗口，开发成本低。
- Chatwoot Website Widget 已经提供完整聊天 UI。
- 支持用户身份设置、自定义属性、自动打开窗口、隐藏默认气泡等能力。
- 坐席侧仍然使用标准 Chatwoot 工作台。

## 4. APP 侧流程

用户路径：

```text
用户打开 APP
  -> 点击“联系客服”
  -> APP 打开内嵌 WebView
  -> WebView 加载 https://your-domain.com/support/chat
  -> H5 页面加载 Chatwoot Widget
  -> 自动打开聊天窗口
  -> 用户直接发送消息
```

APP 需要处理：

- 打开客服 H5 页面。
- 向 H5 页面传递登录态，建议使用短期 token 或已有 APP 会话 token。
- 处理 WebView 返回、页面刷新、登录失效等情况。
- 可选：接收 H5 通过 JSBridge 发出的事件，例如客服页加载完成、会话打开、未读消息变化。

## 5. H5 客服承载页

建议新增一个独立 H5 页面：

```text
https://your-domain.com/support/chat
```

该页面职责：

- 校验 APP 登录态。
- 向后端请求当前用户的 Chatwoot Profile。
- 加载 Chatwoot Website Widget。
- 调用 `setUser` 设置用户身份。
- 调用 `setCustomAttributes` 写入业务属性。
- 自动打开聊天窗口。
- 隐藏默认网页聊天气泡，让 APP 只展示完整客服页。

建议页面逻辑：

```text
H5 页面加载
  -> 从 URL、Cookie 或 JSBridge 获取 APP 登录 token
  -> 请求 /support/chatwoot-profile
  -> 后端返回 user_id、name、phone、email、identifier_hash、custom_attributes
  -> 加载 Chatwoot Widget script
  -> window.$chatwoot.setUser(...)
  -> window.$chatwoot.setCustomAttributes(...)
  -> window.$chatwoot.toggle("open")
```

## 6. 用户身份与安全

必须启用 Chatwoot Website Inbox 的 Identity Validation。

原因：

- APP 用户是登录用户，需要保证用户不能伪造其他人的身份。
- `identifier_hash` 必须由后端使用 Chatwoot 提供的 HMAC Secret 生成。
- HMAC Secret 不能出现在 APP 或 H5 前端代码里。

推荐流程：

```text
APP / H5 携带登录 token
  -> 后端校验 token
  -> 后端取得当前 APP 用户 ID
  -> 后端用 Chatwoot HMAC Secret 生成 identifier_hash
  -> 返回给 H5
  -> H5 调用 Chatwoot setUser
```

后端返回示例：

```json
{
  "identifier": "app_user_12345",
  "name": "张三",
  "phone_number": "+8613800000000",
  "email": "user@example.com",
  "identifier_hash": "generated_hmac_hash",
  "custom_attributes": {
    "app_user_id": "12345",
    "member_level": "gold",
    "app_version": "1.8.0",
    "platform": "ios"
  }
}
```

## 7. Chatwoot 配置项

需要在 Chatwoot 后台完成：

- 创建 Website Inbox。
- 获取 Website Widget script。
- 开启 Identity Validation。
- 配置客服团队和坐席。
- 配置自动分配规则。
- 配置营业时间和离线提示。
- 配置欢迎语。
- 配置快捷回复。
- 配置标签，例如 `ai_handled`、`handoff_required`、`vip_user`、`refund_issue`。
- 配置 Webhook，指向智能客服服务。

建议关闭或弱化 Pre-chat Form，因为 APP 已经有用户身份。

## 8. 智能客服服务

智能客服服务是 Chatwoot 前置处理层，独立于 Chatwoot 部署。

职责：

- 接收 Chatwoot Webhook。
- 判断消息来源，避免处理坐席消息或自己发出的机器人消息。
- 读取用户消息。
- 检索知识库或调用大模型生成回复。
- 判断是否可以自动回复。
- 通过 Chatwoot API 发出机器人回复。
- 必要时给会话加标签、添加内部备注、分配人工坐席。

建议接口：

```text
POST /chatwoot/webhook
GET  /support/chatwoot-profile
POST /support/handoff
```

内部模块：

```text
WebhookReceiver
  -> SignatureVerifier
  -> EventParser
  -> ConversationStateStore
  -> AIReplyService
  -> HandoffPolicy
  -> ChatwootApiClient
```

## 9. Webhook 处理逻辑

重点监听事件：

- `message_created`
- `conversation_created`
- `conversation_updated`

核心处理流程：

```text
收到 Chatwoot webhook
  -> 校验 webhook 签名
  -> 判断事件类型
  -> 只处理用户发出的 incoming message
  -> 检查该 conversation 是否已经转人工
  -> 检查消息是否需要直接转人工
  -> 调用智能客服生成答案
  -> 判断置信度
  -> 高置信度：调用 Chatwoot API 回复用户
  -> 低置信度：添加标签和内部备注，转人工
```

必须避免的问题：

- 机器人回复触发 webhook 后再次被机器人处理，造成循环回复。
- 坐席正在处理时，AI 继续插话。
- 用户明确要求人工时，AI 继续自动回复。
- Webhook 重试导致重复回复。

建议增加会话状态表：

```text
conversation_id
app_user_id
ai_enabled
handoff_required
last_ai_message_id
last_user_message_id
last_processed_event_id
created_at
updated_at
```

## 10. 自动回复与转人工策略

AI 可以自动处理：

- FAQ。
- 功能使用说明。
- 售前咨询。
- 简单规则类问题。
- 明确命中知识库的问题。

立即转人工：

- 用户输入“人工”、“客服”、“转人工”、“投诉”等关键词。
- 涉及退款、支付失败、订单纠纷。
- 涉及账号安全、封禁、实名、隐私。
- AI 检索无结果或置信度低。
- AI 连续 2 次无法解决。
- 用户否定 AI 答案。

转人工动作：

- 将 `ai_enabled` 置为 false。
- 给 conversation 添加 `handoff_required` 标签。
- 添加 private note，说明转人工原因、用户原始问题、AI 已尝试回答内容。
- 可选：分配到指定团队或坐席。
- 给用户发送提示，例如“我已经为你转接人工客服，请稍等。”

## 11. 推荐开发阶段

### 阶段一：Chatwoot Website Inbox 跑通

目标：APP 可以打开 H5 客服页，用户能和 Chatwoot 坐席正常聊天。

任务：

- 创建 Chatwoot Website Inbox。
- 编写 H5 客服承载页。
- APP WebView 打开 H5 页面。
- 自动打开 Chatwoot 对话窗口。
- 测试坐席收发消息。

验收标准：

- APP 内能打开客服页面。
- 用户可以发送消息。
- Chatwoot 坐席台能收到消息。
- 坐席回复后 APP 内能看到回复。

### 阶段二：登录用户身份打通

目标：Chatwoot 能识别 APP 登录用户。

任务：

- 后端新增 `/support/chatwoot-profile`。
- 接入 Identity Validation。
- H5 调用 `setUser`。
- 写入用户业务属性。

验收标准：

- 同一 APP 用户多次打开客服时，Chatwoot 能识别为同一 contact。
- 坐席侧能看到用户昵称、手机号、会员等级、APP 版本等信息。
- 用户不能伪造其他用户身份。

### 阶段三：Webhook 接入

目标：智能客服服务能收到 Chatwoot 事件。

任务：

- 配置 Chatwoot Webhook。
- 新增 `/chatwoot/webhook`。
- 保存 webhook 原始事件日志。
- 区分 incoming、outgoing、agent、bot 消息。

验收标准：

- 用户发消息时，智能客服服务能收到事件。
- 坐席发消息时，服务能识别但不误处理。
- webhook 重试不会导致重复处理。

### 阶段四：智能客服自动回复

目标：常见问题由 AI 自动回复。

任务：

- 接入现有知识库。
- 实现 FAQ 检索。
- 实现置信度判断。
- 调用 Chatwoot API 发送回复。
- 防止机器人消息循环处理。

验收标准：

- 命中 FAQ 的问题能自动回复。
- 未命中问题不会乱答。
- 自动回复在 Chatwoot 会话中可见。

### 阶段五：转人工机制

目标：AI 不能处理的问题进入人工坐席。

任务：

- 实现转人工关键词规则。
- 实现低置信度转人工。
- 给会话加标签。
- 添加内部备注。
- 可选：分配指定团队。

验收标准：

- 用户要求人工时立即停止 AI 自动回复。
- 坐席能看到转人工原因和上下文。
- 人工接管后 AI 不再插话。

### 阶段六：上线与运营

目标：稳定上线并支持客服运营。

任务：

- 增加日志、监控、告警。
- 统计 AI 解决率、转人工率、坐席响应时间。
- 整理 FAQ 和知识库更新流程。
- 做灰度发布。
- 准备异常兜底方案。

验收标准：

- 小流量用户可正常使用。
- Webhook、AI、Chatwoot API 异常时不影响人工客服兜底。
- 客服团队能基于标签和备注处理问题。

## 12. 最小可落地版本

第一版建议只做以下内容：

```text
1. Chatwoot Website Inbox
2. H5 客服承载页
3. APP WebView 打开客服 H5
4. setUser + Identity Validation
5. Chatwoot Webhook 日志接收
6. 简单 FAQ 自动回复
7. 关键词转人工
```

暂时不做：

- 深度改造 Chatwoot 源码。
- 自研完整聊天 UI。
- 复杂多轮机器人流程。
- 复杂工单系统。
- 复杂客服质检系统。

## 13. 风险与注意事项

- HMAC Secret 只能放后端。
- Webhook 必须做签名校验和幂等处理。
- AI 回复必须有边界，不确定就转人工。
- 人工接管后要停止 AI。
- 需要避免重复创建 contact 或 conversation。
- H5 页面要适配 APP WebView，包括返回键、软键盘、安全区、网络异常。
- Chatwoot API Token 不能暴露给 APP 或 H5。
- 上线前要准备人工兜底，避免 AI 服务异常导致用户无法联系客服。

## 14. 当前下一步

建议马上推进：

1. 在本地 Chatwoot 创建 Website Inbox。
2. 新建 H5 客服承载页。
3. 在 APP WebView 中打开该 H5 页面。
4. 后端实现 `/support/chatwoot-profile`。
5. 配置 Identity Validation。
6. 配置 Chatwoot Webhook 到智能客服服务。
7. 先记录 webhook 日志，再接入 FAQ 自动回复。

