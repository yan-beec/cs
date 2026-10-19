# 服务器同步更新操作手册

本文用于项目已经部署完成后的日常发布：在 Windows 开发端修改代码，推送到 Git 远端，同步到 Ubuntu 的 /opt/cs，重新构建并验证机器人。

**归档与客服状态监管模块首次上线或更新，请按[归档功能更新文档](archive-feature-update.md)执行。** 该文档覆盖独立归档数据库、001–006迁移、专用Webhook、解析服务、环境配置和验收；只重建原ICS容器不能完成归档模块发布。

**本次从单渠道升级为多渠道，请直接按[第十节：本次多渠道版本覆盖升级](#十本次多渠道版本覆盖升级首次执行)执行。** 该节提供直接上传本地版本和 Git 同步两种方式，包含首次资料目录迁移、渠道配置、启动校验及后台导入操作；仅执行日常代码同步不会补齐服务器的渠道数据目录。

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
| 首次升级多渠道、增加渠道配置和提示词 | 按第十节同步代码、补充配置、迁移兴聊资料、构建并重建应用 | 是 |
| database/channels.json 收件箱映射 | 校验配置后 restart 应用 | 否 |
| database/channel_*/prompt_*.md 提示词 | 保存到服务器对应渠道目录，下一次生成回复时读取 | 否 |
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

首次升级多渠道时，启动应用前还必须完成第十节的资料目录迁移和渠道配置。上面的代码同步故意保留 `database/`，不会自动复制 `channels.json` 或渠道提示词。

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
docker compose -f docker-compose.yml up -d --no-deps --force-recreate ics-agent
~~~

SSH 容易断开时使用 tmux：

~~~bash
tmux new -s cs-update
cd /opt/cs
docker compose --progress plain -f docker-compose.yml build ics-agent
~~~

按 Ctrl+B，再按 D 离开 tmux；重新登录后用 tmux attach -t cs-update 查看。也可直接执行：

~~~bash
docker compose --progress plain -f docker-compose.yml up -d --build --no-deps --force-recreate ics-agent
~~~

`--build` 负责构建镜像；只有挂载的 `database/channels.json` 改变时，镜像可能仍使用缓存，已有容器也可能被复用。发布命令明确加 `--force-recreate`，确保重新启动应用并加载配置。单独修改渠道 JSON 时，使用 `docker compose -f docker-compose.yml restart ics-agent` 即可；修改 `.env` 则必须 recreate。

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

两端 SHA256 必须一致。意图模型与 database/channel_xingliao/intent_dataset_4000.csv 必须作为配套版本发布并检查指纹。

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

## 十、本次多渠道版本覆盖升级（首次执行）

适用于服务器已经运行单渠道版本，现在覆盖为兴聊、言聊、星盈通、微信四渠道版本。按 **10.1 → 10.2 → … → 10.8** 顺序执行；源码来源在 10.1 和 10.3 中选择对应的一种方式。**本次跳过备份：10.2 只执行暂停应用的短代码块，然后进入 10.3。源码准备仍需执行，它与备份没有依赖关系。** 命令中的运行目录仍是 `/opt/cs`，SSH 别名仍是 `ics`，应用服务仍是 `ics-agent`。

本次更新后的管理平台名称为“零新智能客服”，左侧管理四个渠道，系统日志单独成页。四个渠道共用回复流程，分别选择自己的 FAQ 表、知识库集合和提示词：

| 渠道 | 渠道标识 | FAQ 表 | 知识库逻辑集合 | 提示词文件（相对 /opt/cs） |
| --- | --- | --- | --- | --- |
| 兴聊 | xingliao | faq_channel_xingliao | kb_channel_xingliao | database/channel_xingliao/prompt_xingliao.md |
| 言聊 | yanliao | faq_channel_yanliao | kb_channel_yanliao | database/channel_yanliao/prompt_yanliao.md |
| 星盈通 | xingyingtong | faq_channel_xingyingtong | kb_channel_xingyingtong | database/channel_xingyingtong/prompt_xingyingtong.md |
| 微信 | wechat | faq_channel_wechat | kb_channel_wechat | database/channel_wechat/prompt_wechat.md |

### 10.1 在 Windows 准备本次版本（二选一）

#### 方式 A：直接上传当前本地版本

此方式打包当前工作区，不要求先提交 Git。更新包包含后端代码、服务器部署模板、文档、渠道 JSON 和四份提示词；业务 Excel、知识文档、意图 CSV 使用服务器现有版本，在 10.5 中迁移。

在 **Windows PowerShell** 执行：

~~~powershell
Set-Location D:\project\CS
& 'C:\Users\lenovo\Anaconda3\envs\cs\python.exe' -B -c "from ics_agent.channels import list_channels; print([(c.key, c.inboxes) for c in list_channels()])"
if ($LASTEXITCODE -ne 0) { throw '渠道配置无效，先修复 channels.json。' }

New-Item -ItemType Directory -Force .\tmp | Out-Null
$multiChannelPackage = Join-Path $PWD 'tmp\cs-multichannel-update.tar.gz'
tar.exe -czf $multiChannelPackage `
  --exclude=__pycache__ --exclude=*.pyc `
  --exclude=knowledge_base_in/output --exclude=knowledge_base_in/vector_store `
  --exclude=reminder/build --exclude=reminder/dist --exclude=.pytest_cache `
  ics_agent knowledge_base_in reminder deploy docs `
  docker-compose.yml .dockerignore requirements.txt .env.example `
  database/channels.json `
  database/channel_xingliao/prompt_xingliao.md `
  database/channel_yanliao/prompt_yanliao.md `
  database/channel_xingyingtong/prompt_xingyingtong.md `
  database/channel_wechat/prompt_wechat.md
if ($LASTEXITCODE -ne 0) { throw '多渠道更新包打包失败。' }

Get-FileHash $multiChannelPackage -Algorithm SHA256
scp -o ServerAliveInterval=30 $multiChannelPackage ics:~/cs-multichannel-update.tar.gz
if ($LASTEXITCODE -ne 0) { throw '更新包上传失败。' }
~~~

记录输出的 SHA256，稍后与服务器核对。包内没有本机 `.env`、模型权重、日志、向量库、`docker-compose.override.yml` 或独立 H5 页面。应用内管理后台随 `ics_agent/` 一起更新。后端镜像使用 `deploy/server/Dockerfile.backend-only`，服务器无需安装 Anaconda。

#### 方式 B：沿用 Git 发布

按第二节完成测试、提交并推送 `test` 分支。必须包括 `ics_agent/channels/`、相关后端修改、`database/channels.json`、四个 `prompt_*.md`、解析脚本、部署模板和文档。Git 方式的服务器拉取命令见 10.3，不需要再制作方式 A 的压缩包。

### 10.2 在 Ubuntu 准备运行目录并暂停应用

先在 Windows 执行 `ssh ics`，以下命令均在 **Ubuntu 服务器** 执行。安排本次维护时间，等待正在处理的机器人请求结束，暂停后台 FAQ/知识库更新。数据库容器保持运行。

~~~bash
cd /opt/cs
mkdir -p deployment-records
chmod 700 deployment-records
docker compose -f docker-compose.yml ps -a
docker compose -f docker-compose.yml stop ics-agent
~~~

确认 PostgreSQL 为 healthy。应用此前已停止时，重复执行 stop 也可以。**按本次选择，直接进入 10.3，跳过下面的可选备份。**

#### 可选：另做备份（本次跳过）

仅在需要备份时执行下面的代码块；先确认用户目录有足够空间保存数据库、原始资料和向量库。后续源码准备和更新不依赖此代码块。

~~~bash
(
  set -e
  cd /opt/cs
  mkdir -p deployment-records
  chmod 700 deployment-records
  multi_release_tag=$(date +%Y%m%d-%H%M%S)
  multi_backup_root="$HOME/cs-backups"
  mkdir -p "$multi_backup_root"
  chmod 700 "$multi_backup_root"
  multi_backup_dir="$multi_backup_root/multichannel-$multi_release_tag"
  mkdir -m 700 "$multi_backup_dir"
  printf '%s\n' "$multi_backup_dir" > deployment-records/last-multichannel-backup.txt

  multi_previous_container_id=$(docker compose -f docker-compose.yml ps -a -q ics-agent)
  test -n "$multi_previous_container_id"
  docker inspect "$multi_previous_container_id" --format '{{.Image}}' > "$multi_backup_dir/previous-image-id.txt"
  docker image tag "$(cat "$multi_backup_dir/previous-image-id.txt")" "ics-agent:rollback-multichannel-$multi_release_tag"
  cp -p .env "$multi_backup_dir/env.backup"
  sudo -v

  docker compose -f docker-compose.yml stop ics-agent
  docker compose -f docker-compose.yml exec -T postgres \
    sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' \
    > "$multi_backup_dir/postgres.dump"
  test -s "$multi_backup_dir/postgres.dump"
  docker compose -f docker-compose.yml exec -T postgres \
    pg_restore --list < "$multi_backup_dir/postgres.dump" > /dev/null
  sudo tar -czf "$multi_backup_dir/data-files.tar.gz" \
    database knowledge_base_in/output knowledge_base_in/vector_store

  multi_code_paths=()
  for path in ics_agent reminder knowledge_base_in deploy docs Dockerfile docker-compose.yml .dockerignore requirements.txt .env.example; do
    if [ -e "$path" ]; then multi_code_paths+=("$path"); fi
  done
  sudo tar --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='knowledge_base_in/output' --exclude='knowledge_base_in/vector_store' \
    --exclude='reminder/build' --exclude='reminder/dist' \
    -czf "$multi_backup_dir/previous-code.tar.gz" "${multi_code_paths[@]}"
  sudo tar -tzf "$multi_backup_dir/data-files.tar.gz" > /dev/null
  sudo tar -tzf "$multi_backup_dir/previous-code.tar.gz" > /dev/null
  sudo chown "$(id -u):$(id -g)" "$multi_backup_dir/data-files.tar.gz" "$multi_backup_dir/previous-code.tar.gz"
  chmod 600 "$multi_backup_dir"/*
  echo "备份完成：$multi_backup_dir"
)
~~~

选择备份时，只有出现“备份完成”才代表完整备份生成成功。若按本次选择跳过备份，直接进入 10.3。如果需要暂时恢复旧服务、尚未替换应用容器，可执行 `docker compose -f docker-compose.yml start ics-agent`。完整备份放在当前服务器用户的 `~/cs-backups/`，位于 Docker 构建目录外；备份中的 `.env` 和数据库包含敏感数据，保留在这个受限目录中。

部分 FAQ、文档或向量文件由容器以 root 身份写入，`ubuntu` 用户可能无法读取，因此以上打包命令使用 `sudo tar`，归档后将备份文件所有者交回当前用户。若此前普通 `tar` 报 `Cannot open: Permission denied` 或 `Exiting with failure status`，整次备份未完成，生成的 `data-files.tar.gz` 不能当作完整备份。可直接重新执行本节完整备份代码块，它会创建新的时间戳目录；全部归档和校验成功后才打印“备份完成”。

### 10.3 准备服务器端新版源码（二选一）

`deployment-records/multichannel-source-dir.txt` 由本节生成，用于告诉 10.4、10.5 新版源码在哪里。若报这个文件不存在，说明源码准备尚未完成或记录文件已被移除，请先完成本节所选方式；不要创建空记录文件或直接跳到覆盖代码。

#### 对应方式 A：解压上传包到临时目录

~~~bash
sha256sum ~/cs-multichannel-update.tar.gz
~~~

与 10.1 的 Windows SHA256 核对一致后执行：

~~~bash
(
  set -e
  test -s "$HOME/cs-multichannel-update.tar.gz" || { echo '更新包不存在，请先完成 10.1 方式 A 的上传。'; exit 1; }
  mkdir -p /opt/cs/deployment-records /opt/cs/tmp
  multi_source_dir=$(mktemp -d /opt/cs/tmp/multichannel-source.XXXXXX)
  tar -xzf ~/cs-multichannel-update.tar.gz -C "$multi_source_dir"
  test -f "$multi_source_dir/deploy/server/Dockerfile.backend-only"
  test -f "$multi_source_dir/database/channels.json"
  test -f "$multi_source_dir/ics_agent/channels/config.py"
  printf '%s\n' "$multi_source_dir" > /opt/cs/deployment-records/multichannel-source-dir.txt
  sha256sum ~/cs-multichannel-update.tar.gz > /opt/cs/deployment-records/multichannel-package.sha256
  echo "新版源码目录：$multi_source_dir"
)
~~~

先解压到临时目录，后续只同步指定的代码和配置文件。

#### 对应方式 B：拉取 Git 发布版本

~~~bash
mkdir -p /opt/cs/deployment-records
git -C /opt/cs-src status --short
git -C /opt/cs-src branch --show-current
~~~

确认工作区干净、分支为 `test`，再执行：

~~~bash
git -C /opt/cs-src rev-parse HEAD > /opt/cs/deployment-records/multichannel-previous-source-commit.txt
git -C /opt/cs-src pull --ff-only https://code-sayu.xingyongshe.com/backend/chatwoot.git test
~~~

仅在 pull 成功、提交 SHA 与开发端记录一致后继续：

~~~bash
test -f /opt/cs-src/database/channels.json
test -f /opt/cs-src/ics_agent/channels/config.py
printf '%s\n' /opt/cs-src > /opt/cs/deployment-records/multichannel-source-dir.txt
git -C /opt/cs-src rev-parse HEAD > /opt/cs/deployment-records/multichannel-new-source-commit.txt
~~~

### 10.4 覆盖后端代码及服务器构建模板

两种源码来源从这里开始使用相同命令：

~~~bash
(
  set -e
  test -s /opt/cs/deployment-records/multichannel-source-dir.txt || { echo '请先完成 10.3，生成新版源码目录记录。'; exit 1; }
  multi_source_dir=$(cat /opt/cs/deployment-records/multichannel-source-dir.txt)
  test -f "$multi_source_dir/deploy/server/Dockerfile.backend-only"
  mkdir -p /opt/cs/ics_agent /opt/cs/knowledge_base_in /opt/cs/reminder /opt/cs/deploy /opt/cs/docs
  rsync -a --delete --exclude='__pycache__/' --exclude='*.pyc' "$multi_source_dir/ics_agent/" /opt/cs/ics_agent/
  rsync -a --delete --exclude='/output/' --exclude='/vector_store/' --exclude='__pycache__/' --exclude='*.pyc' "$multi_source_dir/knowledge_base_in/" /opt/cs/knowledge_base_in/
  rsync -a --delete --exclude='/build/' --exclude='/dist/' --exclude='__pycache__/' --exclude='*.pyc' --exclude='.pytest_cache/' "$multi_source_dir/reminder/" /opt/cs/reminder/
  rsync -a "$multi_source_dir/deploy/" /opt/cs/deploy/
  rsync -a "$multi_source_dir/docs/" /opt/cs/docs/
  cp "$multi_source_dir/docker-compose.yml" "$multi_source_dir/.dockerignore" "$multi_source_dir/requirements.txt" "$multi_source_dir/.env.example" /opt/cs/
  cp "$multi_source_dir/deploy/server/Dockerfile.backend-only" /opt/cs/Dockerfile
  cmp -s /opt/cs/Dockerfile "$multi_source_dir/deploy/server/Dockerfile.backend-only"
  echo '后端代码及构建模板已同步'
)
~~~

后续全部显式使用 `-f docker-compose.yml`，避免把本地开发热重载 override 带到服务器。原服务器若有 Compose 自定义项，核对后保留仍需使用的设置；端口和服务地址优先沿用服务器 `.env`。

### 10.5 补充渠道框架并复制服务器兴聊资料

此步骤使用服务器现有镜像中的 Python 执行文件复制，不启动 Web 服务。复制进程使用容器 root 身份读取旧资料，避免遇到与备份相同的权限错误；新增文件和目录归当前 Ubuntu 用户所有，便于后续编辑。新版提示词和渠道配置来自本次源码；兴聊业务资料来自服务器自己的旧目录。已存在的新目录文件会保留，旧路径也保留，便于回滚。

~~~bash
(
  set -e
  cd /opt/cs
  multi_source_dir=$(cat deployment-records/multichannel-source-dir.txt)
  docker compose -f docker-compose.yml run --rm -T --no-deps \
    --user 0:0 \
    --volume "$multi_source_dir/database:/release-database:ro" \
    --entrypoint python ics-agent - /app/database /release-database "$(id -u)" "$(id -g)" <<'PY'
import json
import os
import shutil
import sys
from pathlib import Path

data_dir = Path(sys.argv[1])
release_dir = Path(sys.argv[2])
owner_uid, owner_gid = int(sys.argv[3]), int(sys.argv[4])
channel_keys = ("xingliao", "yanliao", "xingyingtong", "wechat")

def ensure_directory(target):
    missing = []
    current = target
    while not current.exists():
        missing.append(current)
        current = current.parent
    target.mkdir(parents=True, exist_ok=True)
    for directory in missing:
        os.chown(directory, owner_uid, owner_gid)

def copy_missing(source, target):
    if target.exists():
        print("保留已有文件：", target)
        return
    if not source.is_file():
        raise FileNotFoundError(source)
    ensure_directory(target.parent)
    shutil.copy2(source, target)
    os.chown(target, owner_uid, owner_gid)
    print("新增文件：", target)

copy_missing(release_dir / "channels.json", data_dir / "channels.json")
json.loads((data_dir / "channels.json").read_text(encoding="utf-8"))
for key in channel_keys:
    channel_dir = data_dir / f"channel_{key}"
    ensure_directory(channel_dir / "knowledge_base")
    copy_missing(release_dir / f"channel_{key}" / f"prompt_{key}.md",
                 channel_dir / f"prompt_{key}.md")

for filename in ("最新问答对.xlsx", "intent_dataset_4000.csv"):
    source = data_dir / filename
    if source.is_file():
        copy_missing(source, data_dir / "channel_xingliao" / filename)

legacy_documents = data_dir / "knowledge_base"
if legacy_documents.is_dir():
    for source in sorted(legacy_documents.rglob("*")):
        if source.is_file():
            copy_missing(source, data_dir / "channel_xingliao" / "knowledge_base"
                         / source.relative_to(legacy_documents))
print("渠道目录和兴聊资料复制完成；请继续核对收件箱及服务器配置。")
PY
)
~~~

新目录应为：

~~~text
/opt/cs/database/
├── channels.json
├── channel_xingliao/
│   ├── 最新问答对.xlsx                 复制服务器已有文件；未保存源 Excel 时可稍后后台补传
│   ├── intent_dataset_4000.csv          与服务器现有意图模型配套
│   ├── knowledge_base/                 复制服务器已有文档及子目录
│   └── prompt_xingliao.md
├── channel_yanliao/
│   ├── knowledge_base/                 初始可为空
│   └── prompt_yanliao.md
├── channel_xingyingtong/
│   ├── knowledge_base/                 初始可为空
│   └── prompt_xingyingtong.md
└── channel_wechat/
    ├── knowledge_base/                 初始可为空
    └── prompt_wechat.md
~~~

三个新渠道的 `最新问答对.xlsx` 由后续后台上传生成，当前无需制作空 Excel。已有服务器使用自定义资料路径时，保持自定义内容并在下一步核对实际生效路径；上面的脚本只复制默认旧目录中的文件。服务器若没有意图 CSV，需要取回与服务器模型配套的版本，不能随意换成本地另一版本。

### 10.6 核对服务器收件箱和环境配置

~~~bash
cd /opt/cs
nano database/channels.json
~~~

逐个核对服务器 Chatwoot 的 `account_id` 和 `inbox_id`。**本地与服务器可能使用不同编号，不能把开发环境绑定直接当成服务器绑定。** 本文编写时，本地配置为账号 `1` 下兴聊 `3`、言聊 `6`、星盈通 `7`、微信 `4`，这些值只供核对；服务器应填写各实际收件箱的编号。

每个渠道的格式如下，示例中的数字需换成服务器实际值：

~~~json
"inboxes": [{"account_id": 1, "inbox_id": 6}]
~~~

暂未接入的渠道可写 `"inboxes": []`。不要留成 `"inbox_id":`，也不要填 `null` 或重复绑定同一对编号；配置语法错误会导致整个应用启动失败。未绑定的收件箱会回退兴聊。

四个实际收件箱绑定到现有服务器机器人，并使用现有 Webhook 地址 `http://172.20.48.12:3001/webhooks/chatwoot`（已有 HTTPS 入口时使用原 HTTPS 地址）。沿用服务器现有 AgentBot、Webhook 签名密钥和有效 API Token；接入时确认消息确实进入预期收件箱。

然后编辑服务器原 `.env`：

~~~bash
nano .env
~~~

若原先使用项目默认路径，将对应原行调整为下面的值；缺失时添加，避免同名配置重复。已经使用自定义表、目录或模型路径的服务器先核对覆盖规则，再决定是否调整，保留其现有数据来源。

~~~dotenv
FAQ_SOURCE_TABLE=faq_channel_xingliao
FAQ_SOURCE_XLSX_PATH=database/channel_xingliao/最新问答对.xlsx
KNOWLEDGE_BASE_INPUT_DIR=database/channel_xingliao/knowledge_base
KNOWLEDGE_BASE_OUTPUT_PATH=knowledge_base_in/output/chunks.jsonl
KNOWLEDGE_BASE_VECTOR_COLLECTION=kb_channel_xingliao
RAG_INTENT_DATASET_PATH=database/channel_xingliao/intent_dataset_4000.csv
~~~

原来的 `POSTGRES_*`、模型目录、云模型配置、端口、管理员和提醒配置均沿用服务器现有值。`KNOWLEDGE_BASE_VECTOR_DB_DIR` 仍指向服务器原向量目录。

如果新渠道 H5 使用本项目 `/chatwoot/profile` 签发身份签名，额外补充各收件箱的真实 HMAC 密钥：

~~~dotenv
CHATWOOT_CHANNEL_HMAC_TOKENS={"yanliao":"言聊收件箱HMAC密钥","xingyingtong":"星盈通收件箱HMAC密钥","wechat":"微信收件箱HMAC密钥"}
~~~

兴聊继续使用原 `CHATWOOT_HMAC_TOKEN`；由独立业务后端签发身份时，按该后端现有方式为对应 Inbox 签名。HMAC 密钥放 `.env`，不要写入 `channels.json`。H5 的公开 Website Token 与 HMAC 密钥用途不同；独立部署的各 H5 需要使用各自收件箱的 Website Token。后端收件箱路由以 Webhook 来源为准，页面上的渠道名称不能替代收件箱配置。本次后端包不负责更新独立 H5 页面。

~~~bash
chmod 600 .env
docker compose -f docker-compose.yml config --quiet
~~~

### 10.7 构建、校验，再强制重建应用

先构建新版镜像：

~~~bash
cd /opt/cs
docker compose --progress plain -f docker-compose.yml build ics-agent
~~~

构建成功后，执行以下预检查。它读取新镜像代码和服务器挂载资料，校验渠道绑定、提示词、知识库目录及意图 CSV 指纹，并打印实际使用的资源名称；不执行 FAQ 导入或向量重建。

~~~bash
docker compose -f docker-compose.yml run --rm -T --no-deps \
  --entrypoint python ics-agent - <<'PY'
import hashlib
import json
from pathlib import Path
from ics_agent.channels import list_channels, resolve_chatwoot_channel
from ics_agent.faq.config import get_faq_settings
from ics_agent.rag.config import get_rag_settings

root = Path("/app")
for channel in list_channels():
    faq = get_faq_settings(channel.key)
    rag = get_rag_settings(channel.key)
    prompt = root / channel.prompt_path
    assert prompt.is_file() and prompt.read_text(encoding="utf-8").strip(), prompt
    assert (root / rag.knowledge_base_input_dir).is_dir(), rag.knowledge_base_input_dir
    for account_id, inbox_id in channel.inboxes:
        assert resolve_chatwoot_channel(account_id, int(inbox_id)) == channel.key
    print(channel.key, "inboxes=", channel.inboxes,
          "faq=", faq.faq_table_name, "kb=", rag.knowledge_base_vector_collection,
          "faq_file=", faq.faq_source_xlsx_path, "documents=", rag.knowledge_base_input_dir)

shared = get_rag_settings()
dataset = root / shared.rag_intent_dataset_path
metadata = json.loads((root / shared.rag_output_dir / "labels.json").read_text(encoding="utf-8"))
actual_sha = hashlib.sha256(dataset.read_bytes()).hexdigest()
expected_sha = metadata.get("dataset", {}).get("sha256")
if expected_sha:
    assert actual_sha == expected_sha, "意图 CSV 与服务器模型指纹不一致，请恢复配套 CSV。"
else:
    print("旧模型未记录 CSV 指纹；启动后仍需核对标签和意图模型日志。")
print("意图 CSV SHA256:", actual_sha)
print("渠道配置和资料预检查通过")
PY
~~~

只有最后显示“渠道配置和资料预检查通过”才执行启动命令。若出现 `JSONDecodeError`，回到 10.6 修复服务器 JSON；若提示词、文档目录或 CSV 缺失，回到 10.5 补齐。

~~~bash
cd /opt/cs
docker compose -f docker-compose.yml up -d --no-deps --force-recreate ics-agent
docker compose -f docker-compose.yml ps -a
tail -n 160 log/uvicorn-admin.stderr.log
curl -i --max-time 20 http://172.20.48.12:3001/health
~~~

模型预热期间可能暂时无法访问，等本次启动日志出现 `Application startup complete` 后再次请求 `/health`，预期 HTTP 200、`"status":"ok"`。只看到容器 `Up` 不代表应用已经成功启动。

应用启动时自动创建四个 FAQ 表和知识库集合。默认旧表 `faq_qna_optimized` 在首次迁移时复制到空的 `faq_channel_xingliao`，旧表保留，并记录一次迁移标记。兴聊的 `kb_channel_xingliao` 通过 active 指针复用服务器原 `knowledge_base` 物理集合，保留原向量；三个新渠道初始为空。首次升级不需要重新导入兴聊 Excel 或重算向量，先验收现有链路，避免把旧源 Excel 再次全量覆盖到更新过的数据库。

### 10.8 验证四个渠道和新后台

先核对本次启动日志中的四条渠道记录，确认 `channel_key`、`inboxes`、`faq_table` 和知识库集合符合服务器配置，且没有渠道初始化失败：

~~~bash
tail -n 200 /opt/cs/log/uvicorn-admin.stderr.log
~~~

核对四个 FAQ 表的数据量：

~~~bash
cd /opt/cs
docker compose -f docker-compose.yml exec -T postgres \
  sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1' <<'SQL'
SELECT 'xingliao' AS channel, COUNT(*) AS faq_count FROM faq_channel_xingliao
UNION ALL SELECT 'yanliao', COUNT(*) FROM faq_channel_yanliao
UNION ALL SELECT 'xingyingtong', COUNT(*) FROM faq_channel_xingyingtong
UNION ALL SELECT 'wechat', COUNT(*) FROM faq_channel_wechat;
SELECT migration_key, applied_at FROM ics_channel_migrations;
SQL
~~~

兴聊应保留原 FAQ；三个新渠道未导入资料时为 `0`。自定义 FAQ 表按 10.7 打印的实际表名核对。

在 Windows 另开 PowerShell，保持隧道窗口运行：

~~~powershell
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 13001:172.20.48.12:3001 ics
~~~

浏览器打开 `http://127.0.0.1:13001/admin`，使用服务器原管理员账号登录。确认名称为“零新智能客服”，左侧能切换兴聊、言聊、星盈通、微信；兴聊页面可看到复制后的原 FAQ Excel（已保存源文件时）和知识文档；“系统日志”进入独立页面。浏览器缓存旧页面时强制刷新。

接着从四个实际 H5/测试收件箱各发送一条“你是谁？”以及一条本渠道业务问题，在后台“系统日志”或服务器日志核对：

~~~text
event=Webhook.channel.resolved ... account_id=... inbox_id=... channel_key=yanliao
event=Webhook.job.answer_ready ... channel_key=yanliao route=...
~~~

四个请求应分别出现对应 `channel_key`；兴聊原 FAQ、知识库检索、人工转接和会话结束后再咨询均正常。新渠道未导入业务资料时会使用自己的提示词回复，不会借用兴聊 FAQ 或文档。若新渠道仍回答“小兴”，先看日志的 `inbox_id` 是否与服务器绑定一致，再核对 H5 Website Token 是否属于该收件箱。

### 10.9 后续通过后台导入各渠道资料

完成代码和框架升级后，新渠道的业务资料可以全部在后台维护：

1. 左侧选择“言聊”“星盈通”或“微信”，确认页面显示正确渠道。
2. 上传该渠道的 `.xlsx` FAQ 工作簿，预览无误后点击“更新 FAQ 数据库”。上传可以使用任意文件名，后台保存为该渠道的 `最新问答对.xlsx`；更新会全量替换本渠道 FAQ。
3. 上传该渠道的知识文档，确认文件列表无误后点击“更新知识库”，等待更新成功。后台使用与兴聊相同的解析、分块、向量化和发布流程，写入本渠道自己的集合。
4. 从该渠道的 H5 发问验证；切换到其他渠道确认资料仍各自独立。

上传源文件与更新数据库是两个操作，上传后必须执行对应更新。后台 FAQ/知识库更新成功后即可使用，无需重新构建镜像。调整提示词时编辑服务器 `database/channel_<渠道>/prompt_<渠道>.md`，下一次生成回复读取新内容；FAQ 命中直接返回 Excel 中的答案，不会被提示词重写。提示词放在渠道根目录，业务文档放在其 `knowledge_base/` 下。

以后只调整收件箱绑定时，修改并校验服务器 `database/channels.json` 后执行：

~~~bash
cd /opt/cs
docker compose -f docker-compose.yml run --rm -T --no-deps --entrypoint python ics-agent \
  -c 'from ics_agent.channels import list_channels; print([(c.key, c.inboxes) for c in list_channels()])'
~~~

仅在校验成功后重启：

~~~bash
docker compose -f docker-compose.yml restart ics-agent
~~~

`.env` 改动使用 `up -d --no-deps --force-recreate ics-agent`；后端代码改动先 build，再 force-recreate。单独 `up -d --build` 不保证因挂载 JSON 的变化而重启已有容器。

### 10.10 首次升级失败时回滚应用

以下命令适用于已执行 10.2 可选备份、拥有对应旧镜像和 `.env` 记录的情况；本次跳过备份时，不把该步骤作为后续操作。若尚未替换应用容器，可直接 `start ics-agent` 恢复旧容器。

在尚未通过新后台更新业务资料的验收阶段，有备份时可以恢复备份的环境配置并切回旧镜像。10.5 保留了原兴聊文件路径，原 FAQ 表和向量集合也由迁移逻辑保留。

~~~bash
(
  set -e
  cd /opt/cs
  multi_backup_dir=$(cat deployment-records/last-multichannel-backup.txt)
  test -s "$multi_backup_dir/previous-image-id.txt"
  test -f "$multi_backup_dir/env.backup"
  cp -p "$multi_backup_dir/env.backup" .env
  chmod 600 .env
  docker image tag "$(cat "$multi_backup_dir/previous-image-id.txt")" ics-agent:py310
  docker compose -f docker-compose.yml up -d --no-deps --force-recreate ics-agent
)
~~~

随后按第六节检查健康接口和原兴聊 Inbox。本步骤不执行 build，以免重新生成新版镜像；直接上传方式的旧代码保存在本次 `previous-code.tar.gz`，Git 方式另有旧提交记录。若升级后已经修改了新 FAQ 或知识库，旧镜像可能读到旧表或旧集合，需要先保存升级后的资料并确认数据恢复方案，不能直接用发布前数据库备份覆盖正在使用的数据库。

更详细的渠道目录、API 参数和资料维护规则见[多渠道使用说明](multi-channel-setup.md)。
