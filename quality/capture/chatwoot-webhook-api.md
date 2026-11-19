# 通过 API 配置内网账户 Webhook

当前本地 Chatwoot 源码已核对：账户级 Webhook 创建、更新接口使用模型校验，允许 HTTP/HTTPS 内网 URL；请求直接调用 API，可避开前端 URL 校验。五个订阅事件均在服务端 `Webhook::ALLOWED_WEBHOOK_EVENTS` 中。无需修改 Chatwoot 数据库或关闭 ICS 的签名校验。服务器版本不同或返回 422 时，先核对具体错误及该版本的服务端校验。

| 操作 | API |
| --- | --- |
| 查询已有 Webhook | `GET /api/v1/accounts/{account_id}/webhooks` |
| 新建 | `POST /api/v1/accounts/{account_id}/webhooks` |
| 更新已有记录 | `PATCH /api/v1/accounts/{account_id}/webhooks/{webhook_id}` |

使用该 Chatwoot 账户管理员的个人 API Access Token，在个人资料页获取，通过 `api_access_token` 请求头传入。不能使用 AgentBot Token、Website Token 或采集入口的签名 Secret 代替。

## 1. 准备

在 Ubuntu 服务器的 Bash 终端执行。地址示例沿用 ICS 部署文档，替换为服务器实际内网 IP，账号 ID 从 Chatwoot 的 `/app/accounts/数字ID/...` 地址核对：

```bash
cd /opt/cs
umask 077
CW_QUALITY_BASE='http://172.20.48.12:3000'
CW_QUALITY_ACCOUNT_ID='1'
read -r -s -p '粘贴该账户管理员的个人 API Token，然后回车：' CW_QUALITY_TOKEN
printf '\n'
```

先查已有记录，避免重复创建。输出仅包含 ID、名称、URL 和订阅：

```bash
curl --fail-with-body --silent --show-error \
  -H "api_access_token: $CW_QUALITY_TOKEN" \
  "$CW_QUALITY_BASE/api/v1/accounts/$CW_QUALITY_ACCOUNT_ID/webhooks" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps([{k:w.get(k) for k in ("id","name","url","subscriptions")} for w in d["payload"]["webhooks"]],ensure_ascii=False,indent=2))'
```

若已有专用于归档的记录，用第 3 节更新；否则执行第 2 节。不要修改已有 ICS AgentBot 配置或其他业务 Webhook。

## 2. 新建并订阅五个事件

确认下面 JSON 的目标 IP 是实际服务器内网地址，再执行一次。API 地址是 Chatwoot 的 3000 端口，JSON 中接收地址是采集服务的 3010 端口。请求体使用 `webhook` 包装；不传 `inbox_id`，作为账户级订阅。

```bash
curl --fail-with-body --silent --show-error \
  -X POST \
  -H "api_access_token: $CW_QUALITY_TOKEN" \
  -H 'Content-Type: application/json' \
  --output .env.quality-webhook-result.json \
  --data-binary @- \
  "$CW_QUALITY_BASE/api/v1/accounts/$CW_QUALITY_ACCOUNT_ID/webhooks" <<'JSON'
{
  "webhook": {
    "name": "ICS conversation archive",
    "url": "http://172.20.48.12:3010/quality/webhooks/chatwoot",
    "subscriptions": [
      "message_created",
      "message_updated",
      "conversation_created",
      "conversation_updated",
      "conversation_status_changed"
    ]
  }
}
JSON
```

命令成功后，响应保存在当前目录的 `.env.quality-webhook-result.json`，结构为 `payload.webhook`，包含 `id`、`url`、`subscriptions` 和自动生成的 `secret`。该文件含密钥，沿用项目 `.env.*` 忽略规则；在服务器本地查看，不要粘贴到聊天或工单。若命令失败，先看响应中的错误，不要继续把错误内容当作成功结果。

只展示保存结果，不输出 Secret：

```bash
python3 -c 'import json; w=json.load(open(".env.quality-webhook-result.json"))["payload"]["webhook"]; print(json.dumps({k:w.get(k) for k in ("id","url","subscriptions")},ensure_ascii=False,indent=2)); print("Secret present:",bool(w.get("secret")))'
```

在服务器本地打开响应文件，将 `payload.webhook.secret` 填入 `/opt/cs/.env` 的 `ARCHIVE_WEBHOOK_SECRET`；`ARCHIVE_ALLOWED_ACCOUNT_IDS` 填上述实际账号 ID。当前接口不接受自定义 `secret` 参数，必须使用 Chatwoot 返回的值。这也与原有 AgentBot 的 Secret 分开。

如果之前已启动接收服务且签名 Secret 不同，更新 `.env` 后按[消息接收说明](README.md#2-发布并启动)重新创建接收容器，使环境变量生效。仅修改 `.env` 或普通 `restart` 不会刷新已有容器的环境。

## 3. 更新已有专用 Webhook

先从第 1 节查询结果确认归档 Webhook ID。将第 2 节创建命令的 `-X POST` 改为 `-X PATCH`，请求地址改为：

```text
http://服务器内网IP:3000/api/v1/accounts/实际账号ID/webhooks/实际WebhookID
```

请求体仍使用同一份 `webhook` JSON。`subscriptions` 会替换该记录的订阅列表，因此需要一次传入完整五项。更新通常保留原 Secret；以返回的 `payload.webhook.secret` 为准核对采集配置。接口没有单条查询路由，保存后再次使用第 1 节的列表 API 读回 URL 和订阅即可。

## 4. 检查真实投递

API 成功保存仅说明记录已创建。接收服务启动并配置正确 Secret 后，从已有 Chatwoot 的部署目录执行：

```bash
docker compose exec -T sidekiq ruby -rnet/http -e 'u=URI("http://172.20.48.12:3010/health"); r=Net::HTTP.start(u.host,u.port,open_timeout:3,read_timeout:5) { |h| h.get(u.request_uri) }; puts r.code'
```

预期 200。这验证容器网络；实际 Webhook 投递还受 Chatwoot `SafeFetch` 的私网限制控制。若实际投递提示私网地址被拒绝，在 **Chatwoot 的 Rails 和 Sidekiq** 环境设置 `SAFE_FETCH_ALLOW_PRIVATE_NETWORK=true`，按原部署流程重建对应容器，并确认容器中的值生效。写在 ICS `.env` 中无效；当前本地版本支持该配置，服务器需核对版本。

在测试会话发送客户消息、机器人回复、人工回复，并编辑/删除测试消息，再按[接收验收步骤](README.md#4-验收)确认 `archive.events` 出现对应记录且原文仍在。前端以后仍可能拒绝再次编辑该内网 URL，后续继续使用 API 更新。

当前 Chatwoot 账户级 Webhook 失败不会自动重试。创建记录到接收服务就绪之间尚不算正式归档，期间只做测试；正式采集完整性仍需后续的投递重试或补录机制。

完成后清除当前终端中的 Token，并删除含 Secret 的临时响应文件：

```bash
unset CW_QUALITY_TOKEN
rm -f -- .env.quality-webhook-result.json
```

401 通常为 Token 无效，403 为账户权限不足，404 需核对账号、路径或版本，422 需检查 URL、重复 URL 和订阅。当前版本同一账号下 URL 唯一，已存在时应查询并 PATCH 对应专用记录。
