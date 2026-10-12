# 服务器同步更新操作手册

本文用于项目已经部署完成后的日常发布：在 Windows 开发端修改代码，推送到 Git 远端，同步到 Ubuntu 的 /opt/cs，重新构建并验证机器人。

转人工强弹窗提醒、工作台按钮及浏览器页面复用的专项更新，按[提醒功能服务器更新部署手册](reminder-server-update.md)执行；除服务器代码和配置外，还需更新客服电脑上的 Windows 客户端与浏览器插件。

当前部署约定：

| 项目 | 约定 |
| --- | --- |
| Windows 开发目录 | D:\project\CS |
| Python 环境 | C:\Users\lenovo\Anaconda3\envs\cs |
| Windows SSH 别名 | ics |
| Git 发布分支 | test |
| Git 源码目录 | /opt/cs-src |
| 实际运行目录 | /opt/cs |
| 应用服务 | ics-agent |
| 应用镜像 | ics-agent:py310 |
| 机器人地址 | http://172.20.48.12:3001 |
| Chatwoot 地址 | http://172.20.48.12:3000 |
| 前端页面 | 已独立部署，本手册不上传、不构建、不修改 |

## 一、改动类型和对应动作

| 改动 | 动作 | 是否 build |
| --- | --- | --- |
| ics_agent/、knowledge_base_in/ Python 代码 | Git 拉取、同步、构建、重建应用 | 是 |
| reminder/backend/、reminder/protocol.py 服务端代码 | Git 拉取、同步 reminder、构建、重建应用 | 是 |
| reminder/client/、extension/、packaging/ 客户端或插件 | 按专项手册构建安装包、升级客服电脑、重新加载浏览器插件 | 服务器通常无需 build；混合后端变更时需要 |
| requirements.txt、Dockerfile | Git 拉取、同步、构建、重建应用 | 是 |
| docker-compose.yml | Git 拉取、同步、重建应用 | 建议执行 |
| 服务器 /opt/cs/.env | 编辑后 force-recreate | 否 |
| deploy/server/bot.env.example | 拉取后人工合并到 .env | 否 |
| docs/、README | 拉取即可 | 否 |
| FAQ、知识库原始文件 | 管理后台或专门数据流程 | 通常否 |
| models/ 模型权重 | 单独打包、上传、校验、解压、重启预热 | 通常否 |
| 意图模型和 intent_dataset_4000.csv | 配套发布并检查指纹 | 代码需 build |

普通源码同步不会覆盖以下运行数据：/opt/cs/.env、models、database、log、knowledge_base_in/output、knowledge_base_in/vector_store 和 PostgreSQL volume。后端更新也不会改变已经单独部署的前端。

## 二、开发端发布

以下命令在 Windows PowerShell 执行。

### 1．检查工作区和分支

~~~powershell
Set-Location D:\project\CS
git branch --show-current
git status --short
git diff --stat
git remote get-url origin
~~~

确认分支是 test，所有改动都能解释。不要把 .env、密码、Token、模型权重和临时文件提交到 Git。

### 2．使用当前 Conda 环境测试

~~~powershell
& C:\Users\lenovo\Anaconda3\envs\cs\python.exe -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw '本地测试失败，停止发布。' }
& C:\Users\lenovo\Anaconda3\envs\cs\python.exe -m py_compile ics_agent\rag\retrieval\dense_search.py ics_agent\rag\config.py
if ($LASTEXITCODE -ne 0) { throw 'Python 编译失败，停止发布。' }
git diff --check
if ($LASTEXITCODE -ne 0) { throw 'Git 检查失败，停止发布。' }
~~~

部分测试因本地 PostgreSQL 未启动而 skipped 时，要记录原因，不能将 skipped 当作完整联调通过。

### 3．检查新增配置

微批处理的服务器模板应包含：

~~~powershell
Select-String -Path deploy\server\bot.env.example -Pattern 'KNOWLEDGE_BASE_EMBEDDING_MICRO_BATCH'
~~~

当前值：

~~~dotenv
KNOWLEDGE_BASE_EMBEDDING_MICRO_BATCH_WAIT_MS=20
KNOWLEDGE_BASE_EMBEDDING_MICRO_BATCH_MAX_SIZE=4
~~~

模板不会自动覆盖服务器实际 .env；需要时在服务器手工补齐。

### 4．提交并推送

~~~powershell
git add -A
git diff --cached --stat
git diff --cached --name-only
~~~

确认暂存区没有密钥、.env、模型和无关临时文件后：

~~~powershell
git commit -m "feat: update chatbot service"
if ($LASTEXITCODE -ne 0) { throw '提交失败，停止发布。' }
git push origin test
if ($LASTEXITCODE -ne 0) { throw '推送失败，停止发布。' }
$releaseCommit = (git rev-parse HEAD).Trim()
"本次发布提交：$releaseCommit"
~~~

只有已经 push 的提交，服务器才能拉取。

## 三、服务器更新前检查

### 1．登录并检查

~~~powershell
ssh ics
~~~

进入服务器：

~~~bash
cd /opt/cs
docker compose -f docker-compose.yml ps
curl -i --max-time 10 http://172.20.48.12:3001/health
df -h /
free -h
~~~

有真实用户请求时，先等待已接收任务完成。当前 Webhook 队列在应用内存中，强制重启不会自动重放未完成任务。

### 2．备份配置、源码和旧镜像

~~~bash
mkdir -p /opt/cs/deployment-records
chmod 700 /opt/cs/deployment-records
git -C /opt/cs-src rev-parse HEAD | tee /opt/cs/deployment-records/previous-source-commit.txt
docker image inspect ics-agent:py310 --format 'image={{.Id}} created={{.Created}}' | tee /opt/cs/deployment-records/previous-image.txt
release_tag=$(date +%Y%m%d-%H%M%S)
cp -p /opt/cs/.env "/opt/cs/deployment-records/env-$release_tag.backup"
chmod 600 "/opt/cs/deployment-records/env-$release_tag.backup"
printf '%s\n' "$release_tag" > /opt/cs/deployment-records/last-release-tag.txt
previous_image_id=$(docker image inspect ics-agent:py310 --format '{{.Id}}')
docker image tag "$previous_image_id" "ics-agent:rollback-$release_tag"
~~~

.env 备份只留在服务器，不能贴到聊天或提交 Git。发布前不要执行 docker system prune。

## 四、拉取并同步代码

### 1．检查服务器 checkout

~~~bash
git -C /opt/cs-src status --short
git -C /opt/cs-src branch --show-current
~~~

status 必须为空，分支必须是 test。发现本地修改时不要直接 reset --hard。

### 2．使用 HTTPS 地址单次快进拉取

确认当前分支为 test 后，使用以下仓库地址单次拉取 test 分支。该地址仅用于本次拉取，无需修改 origin 配置。

~~~bash
git -C /opt/cs-src pull --ff-only https://code-sayu.xingyongshe.com/backend/chatwoot.git test
git -C /opt/cs-src log -1 --oneline
~~~

权限错误、分支分叉、远端分支不存在或本地修改覆盖风险出现时，停止发布。

可将开发端记录的完整 SHA 粘贴到服务器核对：

~~~bash
expected_commit='把开发端记录的完整 SHA 粘贴到这里'
actual_commit=$(git -C /opt/cs-src rev-parse HEAD)
test "$actual_commit" = "$expected_commit" && echo '源码 SHA 校验通过' || { echo "源码 SHA 不一致：$actual_commit"; exit 1; }
~~~

### 3．同步指定目录

~~~bash
(
  set -e
  test -f /opt/cs-src/deploy/server/Dockerfile.backend-only
  mkdir -p /opt/cs/ics_agent /opt/cs/knowledge_base_in /opt/cs/reminder /opt/cs/deploy /opt/cs/docs
  rsync -a --delete --exclude='__pycache__/' --exclude='*.pyc' /opt/cs-src/ics_agent/ /opt/cs/ics_agent/
  rsync -a --delete --exclude='/output/' --exclude='/vector_store/' --exclude='__pycache__/' --exclude='*.pyc' /opt/cs-src/knowledge_base_in/ /opt/cs/knowledge_base_in/
  rsync -a --delete --exclude='/build/' --exclude='/dist/' --exclude='__pycache__/' --exclude='*.pyc' --exclude='.pytest_cache/' /opt/cs-src/reminder/ /opt/cs/reminder/
  rsync -a /opt/cs-src/deploy/ /opt/cs/deploy/
  rsync -a /opt/cs-src/docs/ /opt/cs/docs/
  cp /opt/cs-src/docker-compose.yml /opt/cs-src/.dockerignore /opt/cs-src/requirements.txt /opt/cs-src/.env.example /opt/cs/
  cp /opt/cs-src/deploy/server/Dockerfile.backend-only /opt/cs/Dockerfile
)
~~~

这段命令不会覆盖 .env、模型、数据库、日志或向量库。--delete 只用于应用代码目录，不能加 --delete-excluded，也不能把目标改成整个 /opt/cs。

同步后：

~~~bash
git -C /opt/cs-src rev-parse HEAD > /opt/cs/deployment-records/source-commit-pending.txt
cmp -s /opt/cs/Dockerfile /opt/cs-src/deploy/server/Dockerfile.backend-only && echo 'Dockerfile 模板同步通过'
~~~

不要执行：

~~~bash
rsync -a --delete /opt/cs-src/ /opt/cs/
docker compose -f docker-compose.yml down -v
rm -rf /opt/cs/database
rm -rf /opt/cs/knowledge_base_in/vector_store
~~~

## 五、构建和重建

### A．代码、依赖、Dockerfile 或 Compose 改动

~~~bash
cd /opt/cs
docker compose -f docker-compose.yml config --quiet
docker compose --progress plain -f docker-compose.yml build ics-agent
docker compose -f docker-compose.yml up -d --force-recreate ics-agent
~~~

SSH 容易断开时使用 tmux：

~~~bash
tmux new -s cs-update
cd /opt/cs
docker compose --progress plain -f docker-compose.yml build ics-agent
~~~

按 Ctrl+B，再按 D 离开 tmux；重新登录后用 tmux attach -t cs-update 查看。也可直接执行：

~~~bash
docker compose --progress plain -f docker-compose.yml up -d --build ics-agent
~~~

不要用 docker compose down 作为普通更新步骤，否则可能中断 PostgreSQL。

### B．只改服务器 .env

~~~bash
cd /opt/cs
nano .env
docker compose -f docker-compose.yml config --quiet
docker compose -f docker-compose.yml up -d --force-recreate ics-agent
~~~

修改 CHATWOOT_AUTO_REPLY_ENABLED、Webhook Secret、API Token 后都要 recreate。ADMIN_INITIAL_PASSWORD 不会覆盖已有管理员密码；POSTGRES_PASSWORD 不会自动修改已有 PostgreSQL 用户密码。BUILD_PIP_INDEX_URL、BUILD_TORCH_INDEX_URL 是构建参数，改后必须 build。

### C．FAQ 和知识库

不要用普通源码同步覆盖运行数据，优先用管理后台更新。大量知识库更新不要和代码发布或模型预热同时执行。更新后检查：

~~~bash
tail -n 120 /opt/cs/log/uvicorn-admin.stderr.log
du -sh /opt/cs/knowledge_base_in/output /opt/cs/knowledge_base_in/vector_store
~~~

### D．模型和训练产物

模型不在 Git 中。Windows 端：

~~~powershell
Set-Location D:\project\CS
tar.exe -h -czf D:\cs-models.tar.gz models
if ($LASTEXITCODE -ne 0) { throw '模型打包失败。' }
Get-FileHash D:\cs-models.tar.gz -Algorithm SHA256
scp -o ServerAliveInterval=30 D:\cs-models.tar.gz ics:~/
~~~

服务器端核对并解压：

~~~bash
sha256sum ~/cs-models.tar.gz
cd /opt/cs
tar -xzf ~/cs-models.tar.gz
docker compose -f docker-compose.yml up -d --force-recreate ics-agent
~~~

两端 SHA256 必须一致。意图模型与 database/intent_dataset_4000.csv 必须作为配套版本发布并检查指纹。

## 六、发布后验证

### 1．容器、端口、健康接口

~~~bash
cd /opt/cs
docker compose -f docker-compose.yml ps -a
docker compose -f docker-compose.yml port ics-agent 3001
curl -i --max-time 20 http://172.20.48.12:3001/health
~~~

预期 ics-agent 为 Up，端口显示 172.20.48.12:3001->3001/tcp，健康接口返回 HTTP 200。

### 2．启动、预热和微批日志

~~~bash
tail -n 160 /opt/cs/log/uvicorn-admin.stderr.log
grep -E 'Application startup complete|app.lifecycle.started|app.rag_warmup|embedding.micro_batch|Traceback|ERROR|failed_fallback' /opt/cs/log/uvicorn-admin.stderr.log | tail -n 100
~~~

看到应用生命周期启动完成后再做业务验证。微批正常日志示例：

~~~text
event=knowledge_base.embedding.micro_batch.completed batch_size=4 elapsed_ms=...
~~~

batch_size=1 也正常，表示 20 毫秒窗口内没有收集到更多 Query。

### 3．核对非敏感配置

~~~bash
docker compose -f docker-compose.yml exec -T ics-agent python -c '
from ics_agent.rag.config import get_rag_settings
from ics_agent.app.core.config import get_settings
r = get_rag_settings()
s = get_settings()
print("host=", s.host)
print("port=", s.port)
print("worker_count=", r.webhook_worker_count)
print("micro_batch_wait_ms=", r.knowledge_base_embedding_micro_batch_wait_ms)
print("micro_batch_max_size=", r.knowledge_base_embedding_micro_batch_max_size)
print("reranker_enabled=", r.rag_reranker_enabled)
print("reranker_max_concurrency=", r.rag_reranker_max_concurrency)
print("rag_timeout_seconds=", r.webhook_rag_timeout_seconds)
'
~~~

这条命令不打印 API Key、Token 或密码。

`RAG_RERANKER_ENABLED=false` 表示直接使用双塔 Top5，启动时不加载或预热 reranker；`true` 沿用已有召回与精排参数。未配置的旧环境默认 `true`。修改服务器 `.env` 后需重新创建 `ics-agent` 容器，再用上面的命令确认实际值。

### 4．验证管理后台和 Webhook

如果绑定在 172.20.48.12:3001，在 Windows PowerShell 另开窗口：

~~~powershell
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 13001:172.20.48.12:3001 ics
~~~

浏览器打开 http://127.0.0.1:13001/admin。

服务器验证未签名 POST：

~~~bash
curl -sS --max-time 10 -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' -d '{"event":"deployment_probe"}' http://172.20.48.12:3001/webhooks/chatwoot
~~~

预期返回 401，表示入口连通且签名校验生效。浏览器直接打开 Webhook 地址发送 GET，返回 405 是正常的。

然后在测试 Inbox 发送普通问题，确认机器人回复、会话不串线、维护兜底正常、已解决和六小时边界正常。

### 5．记录本次发布

~~~bash
cd /opt/cs
release_tag=$(cat /opt/cs/deployment-records/last-release-tag.txt)
git -C /opt/cs-src rev-parse HEAD > "/opt/cs/deployment-records/source-commit-$release_tag.txt"
docker image inspect ics-agent:py310 --format 'image={{.Id}} created={{.Created}}' > "/opt/cs/deployment-records/image-$release_tag.txt"
cat "/opt/cs/deployment-records/source-commit-$release_tag.txt"
cat "/opt/cs/deployment-records/image-$release_tag.txt"
~~~

## 七、失败时回滚

### 1．回滚旧应用镜像

~~~bash
cd /opt/cs
release_tag=$(cat /opt/cs/deployment-records/last-release-tag.txt)
docker image tag "ics-agent:rollback-$release_tag" ics-agent:py310
docker compose -f docker-compose.yml up -d --force-recreate ics-agent
~~~

然后重新检查健康接口、日志和测试 Inbox。不会删除 PostgreSQL、FAQ 或向量库。

### 2．没有旧镜像时回滚源码

~~~bash
git -C /opt/cs-src status --short
git -C /opt/cs-src fetch origin test
git -C /opt/cs-src checkout KNOWN_GOOD_COMMIT_SHA
~~~

将 KNOWN_GOOD_COMMIT_SHA 替换为已记录的完整 SHA，再执行第四节同步和第五节构建。回滚结束后恢复发布分支：

~~~bash
git -C /opt/cs-src checkout test
git -C /opt/cs-src pull --ff-only origin test
~~~

### 3．恢复服务器配置

~~~bash
cd /opt/cs
cp -p /opt/cs/deployment-records/env-YYYYMMDD-HHMMSS.backup .env
chmod 600 .env
docker compose -f docker-compose.yml up -d --force-recreate ics-agent
~~~

将文件名替换为实际备份名。不要用 .env.example 覆盖生产 .env。

### 4．不要用数据库回滚代替代码回滚

除非已有明确并验证过的迁移脚本，否则不要删除 PostgreSQL volume、重新初始化数据库或手工删除幂等记录。

## 八、常见问题

### 改了代码但服务仍是旧版本

依次确认：开发端已 push；服务器源码 SHA 正确；rsync/cp 成功；执行了 build；执行了 up -d --force-recreate。只执行 git pull 不会改变 Docker 镜像，只执行 build 也不会替换运行中的容器。

### 改了 .env 但行为没变化

执行：

~~~bash
docker compose -f docker-compose.yml up -d --force-recreate ics-agent
~~~

不要只执行 restart。

### 构建慢或 pip 超时

~~~bash
grep -E '^(BUILD_PIP_INDEX_URL|BUILD_TORCH_INDEX_URL|BUILD_PIP_TIMEOUT|BUILD_PIP_RETRIES)=' /opt/cs/.env
~~~

保留 Docker pip 缓存重试，不要因为构建慢重复上传已校验成功的模型包。

### /health 连接拒绝

~~~bash
docker compose -f docker-compose.yml ps -a
docker compose -f docker-compose.yml port ics-agent 3001
tail -n 120 /opt/cs/log/uvicorn-admin.stderr.log
~~~

容器 Up 不一定代表模型预热完成；端口绑定为 127.0.0.1:3001 时只能通过本机或相应 SSH 隧道访问。

### 容器退出码 137

~~~bash
free -h
docker stats --no-stream
sudo journalctl -k --since '30 minutes ago' | grep -i -E 'oom|out of memory|killed process'
~~~

先降低 Webhook、重排和模型并发，确认不是同机 Chatwoot 或知识库更新占满内存，再考虑扩容。

### Chatwoot 没有回复

~~~bash
grep -E 'webhook.received|job.answer_ready|failed_fallback|chatwoot_reply|llm_reply.fallback' /opt/cs/log/uvicorn-admin.stderr.log | tail -n 100
~~~

确认 CHATWOOT_AUTO_REPLY_ENABLED=true 已写入服务器 .env 并 recreate。检查 AgentBot Secret、用户 API Token、AgentBot Access Token 没有混用。

## 九、每次发布的最短清单

开发端：

~~~powershell
Set-Location D:\project\CS
git status --short
& C:\Users\lenovo\Anaconda3\envs\cs\python.exe -m unittest discover -s tests -v
git add -A
git diff --cached --stat
git commit -m "feat: update chatbot service"
git push origin test
git rev-parse HEAD
~~~

服务器端：

~~~bash
ssh ics
cd /opt/cs
git -C /opt/cs-src status --short
git -C /opt/cs-src fetch origin test
git -C /opt/cs-src checkout test
git -C /opt/cs-src pull --ff-only origin test

# 执行第四节的 rsync/cp 同步代码块
docker compose -f docker-compose.yml config --quiet
docker compose --progress plain -f docker-compose.yml build ics-agent
docker compose -f docker-compose.yml up -d --force-recreate ics-agent
docker compose -f docker-compose.yml ps -a
curl -i --max-time 20 http://172.20.48.12:3001/health
tail -n 120 log/uvicorn-admin.stderr.log
~~~

第一次请按完整章节执行，熟悉后再使用最短清单。涉及模型、FAQ、知识库、服务器 .env 或数据库时，回到对应章节执行专门流程。
