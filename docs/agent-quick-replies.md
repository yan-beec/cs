# 坐席快捷回复跨设备同步

## 功能

坐席客户端（1.4.0 起）的"我的快捷回复"保存在 ICS 数据库中，每个坐席（Chatwoot 工作区 + 用户）一份。坐席在任何电脑登录客户端都能取回并修改自己的快捷回复；同一台电脑切换坐席时，客户端自动换成该坐席的内容，其他坐席看不到。客户端在会话输入框按 F1 弹出列表，每页 5 条，1～5 填入，Q/E 翻页。

客户端本机保留一份缓存：ICS 暂时连不上时 F1 仍能用缓存内容，但不能修改。未登录强提醒服务或 ICS 未部署本功能时，修改暂存本机，连上后自动上传。旧版客户端只存在本机的快捷回复，在该坐席第一次连上服务器时自动上传；服务器上已有内容时，把本机独有的回复（按标题 + 内容去重）追加到末尾。

## 与其他功能的关系

本功能独立于智能客服主链路和黑名单：使用自己的小连接池（最多 2 个连接，语句 3 秒、锁 2 秒上限），启动失败或数据库异常只影响快捷回复接口，客户端会退回本机缓存。身份核对复用黑名单模块的 `ChatwootAgentGateway`（向 Chatwoot `/api/v1/profile` 核对，缓存 60 秒，不落库、不写日志）。

## 部署

1. 更新 ICS 代码并重启。启动时自动在 ICS 主库创建表 `agent_quick_replies`（`CREATE TABLE IF NOT EXISTS`，失败时每隔一段时间重试）。默认 `CHATWOOT_QUICK_REPLIES_ENABLED=true`，设为 `false` 时接口返回 404，客户端按"服务器未部署"处理，改为只存本机。
2. 无需执行归档库迁移。
3. Nginx 无需修改：接口位于已开放的 `/reminder/` 前缀下，只用 GET/POST。单个坐席整份列表上限 200 条、合计 30 万字，请求体在 Nginx 默认 `client_max_body_size 1m` 以内。
4. 重新构建并安装坐席客户端 1.4.0。

## 数据表

| 列 | 说明 |
| --- | --- |
| `account_id`, `agent_id` | 主键：Chatwoot 工作区 ID、用户 ID（以 Chatwoot 核对结果为准，不信任客户端传入的用户 ID） |
| `revision` | UUID，每次保存更新，用于乐观锁 |
| `items` | JSONB 数组，元素为 `{id, title, content}`，按显示顺序 |
| `item_count`, `updated_by_name`, `created_at`, `updated_at` | 便于排查 |

## 接口（坐席客户端使用）

认证：客户端转发当前 Chatwoot 登录会话的 `X-Chatwoot-Access-Token`、`X-Chatwoot-Client`、`X-Chatwoot-Uid`。坐席只能读写自己在其所属工作区下的那一份。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/reminder/quick-replies?account_id=` | 返回 `{revision, items, updated_at, agent_id, agent_name}`；从未保存过时 `revision` 为空字符串 |
| POST | `/reminder/quick-replies` | 请求 `{account_id, revision, items}`，整份替换；`revision` 必须等于当前版本（首次保存传空字符串），否则 409 |

校验规则与客户端一致：最多 200 条；编号 `[A-Za-z0-9-]{1,64}` 且不重复；标题 1～80 字；内容 1～12000 字；标题与内容合计不超过 30 万字。错误码：401 登录失效、403 不属于该工作区、409 已在其他设备修改、422 内容不合规、502 Chatwoot 不可用、503 数据库或服务未就绪。

客户端会核对返回的 `agent_id` 与工作台登录坐席一致，ICS 连接的 Chatwoot 与客户端工作台不是同一个时不会读取或覆盖数据。
