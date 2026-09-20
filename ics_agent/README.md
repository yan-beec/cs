# ics_agent

独立运行的智能客服服务。

当前边界：

- Chatwoot Webhook 接收、签名校验和事件解析。
- FAQ/MySQL 读取。
- Redis 缓存。
- BM25 相似问题召回。
- Chatwoot API 自动回复。
- 转人工策略和会话状态。

当前版本先完成服务骨架和 Webhook 接收，不会自动回复 Chatwoot 用户消息。

## 本地运行

在项目根目录 `D:\project\CS` 执行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item ics_agent/.env.example .env
uvicorn ics_agent.app.main:app --host 0.0.0.0 --port 3001 --reload
```

项目根目录的 `requirements.txt` 是整个项目唯一的 Python 依赖清单。

构建智能客服镜像时，也要从项目根目录执行：

```powershell
docker build -f ics_agent/Dockerfile -t ics-agent .
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
