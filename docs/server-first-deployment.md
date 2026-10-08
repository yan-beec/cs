# 智能客服机器人：Ubuntu 服务器首次部署测试手册

适用项目：`ics_agent` / `D:\project\CS`。编写日期：2026-10-08。

本文按你已确认的环境编写：**Ubuntu 22.04 LTS、x86_64，已有服务器端 Chatwoot，本次只部署机器人并接入 Chatwoot。** Ubuntu 24.04 可按相同步骤操作。未登录你的远端服务器执行部署；本文命令依据当前项目源码、Compose 配置及本地已验证的 Chatwoot 接口核对。

服务器登录信息显示系统盘约 49.10 GB、已用 15.1%，约有 41.7 GB 可用。`172.20.48.12` 是私网地址。CPU 核数、内存总量、公网 IP、域名及 Chatwoot 是否与机器人同机，仍需按第 2 节确认。

## 0. 先看部署目标和操作约定

部署后的结构：

```text
浏览器 / APP
   │
   ▼
已有 Chatwoot ── AgentBot Webhook ──► 机器人 FastAPI
   ▲                                    │
   └────────── Chatwoot API 回复 ─────────┤
                                        ├─ 机器人 PostgreSQL：FAQ、记忆、事件状态
                                        ├─ 本地模型：意图分类、向量召回、重排
                                        ├─ 本地 Chroma：知识库向量
                                        └─ 云端大模型 API
```

- **不重装 Chatwoot，不修改它的数据库。** 机器人使用自己独立的 PostgreSQL 数据卷。
- 主流程使用 Docker。服务器不需要安装 Anaconda，也不需要搬运 Windows 的 `cs` 环境；镜像内使用 Python 3.10.21。本文如有本机 Python 操作，使用你指定的 `C:\Users\lenovo\Anaconda3\envs\cs\python.exe`。
- 除明确标注“Windows PowerShell”外，命令都在 **Ubuntu SSH 终端**执行。
- 服务器项目目录统一为 `/opt/cs`。每次重新登录后先执行 `cd /opt/cs`。
- 本文统一使用 `docker compose -f docker-compose.yml ...`。不要省略 `-f`：仓库的 `docker-compose.override.yml` 会启用开发热重载，服务器首次部署采用镜像运行方式。
- 每个代码块执行完成、结果符合预期，再执行下一块。`YOUR_...`、`REPLACE_...`、示例域名和 ID 必须换成你的真实值。
- 先新建一个 **Chatwoot 测试 Website Inbox**。验收前不要把正在接待真实用户的 Inbox 切换到新机器人。

本文附带两个可直接修改的模板：

| 文件 | 用途 |
| --- | --- |
| `deploy/server/bot.env.example` | 服务器机器人配置模板，复制成 `/opt/cs/.env` |
| `deploy/server/nginx-bot.conf.example` | 宿主机 Nginx 站点模板，限制公网可访问接口 |

操作路线：**核对资源 → 上传代码和模型 → 填配置 → 构建镜像 → 初始化 FAQ 和向量库 → 启动并本地验收 → 配网络入口 → 绑定 Chatwoot → 完整测试 → 备份。**

## 1. 准备部署信息

先把下表填好；密码和 Token 保存在自己的密码管理器中，不要粘贴到截图或公开工单。

| 信息 | 示例或获取位置 |
| --- | --- |
| 机器人服务器 SSH 地址 | 云控制台的公网 IP，或 VPN/堡垒机提供的连接地址 |
| SSH 用户 | 你当前为 `ubuntu` |
| SSH 端口、私钥 | 云主机当前可用的登录方式；默认端口通常是 22 |
| 机器人公网域名（可选） | `bot.example.com`；没有域名看第 10B 节 |
| 已有 Chatwoot 根地址 | `https://chat.example.com`，不要带 `/app` 或 `/api` |
| Chatwoot Account ID | 打开后台，地址通常含 `/app/accounts/数字/...` |
| Chatwoot 用户 API Token | 账户头像 → 个人资料 / Profile Settings → Access Token；使用该账户有管理权限的用户 |
| 测试 Inbox ID、Website Token | Chatwoot 设置 → 收件箱 / Inboxes → 测试 Website Inbox |
| 云端模型名称、API Base URL、API Key | 使用你本机已经调用成功的一组配置，不直接照抄模板中的示例模型 |
| DNS、云安全组权限 | 配置域名和允许必要来源访问端口 |

需要区分五种信息：

| 名称 | 放在哪里 | 用途 |
| --- | --- | --- |
| 用户 API Token | `CHATWOOT_API_TOKEN` | 查坐席、修改状态、转人工和联系人操作 |
| AgentBot Access Token | `CHATWOOT_AGENT_BOT_ACCESS_TOKEN` | 以机器人身份发送回复 |
| AgentBot Secret | `CHATWOOT_AGENT_BOT_SECRET` | 验证 Chatwoot 发来的 Webhook 签名 |
| Website Token | `support-chat.html` 或页面参数 | 浏览器加载指定 Inbox 的 Widget；不是后台 API 密钥 |
| Website HMAC Token | 后端 `CHATWOOT_HMAC_TOKEN` | 已登录用户身份签名；匿名首测可暂不配置 |

## 2. 登录 Ubuntu，检查资源和已有服务

### 2.1 重新连接 SSH

你看到的 `client_loop: send disconnect: Connection reset` 表示 SSH 连接中断，不能据此判断机器人或服务器故障。先重新连接。

在 **Windows PowerShell** 执行，替换服务器地址：

```powershell
ssh -o ServerAliveInterval=30 -o ServerAliveCountMax=3 ubuntu@YOUR_SERVER_ADDRESS
```

如果平时用私钥或非 22 端口，保留你原来的 `-i` / `-p` 参数。`172.20.48.12` 只有在你的电脑能访问该私网时才可以直接使用。

### 2.2 在 Ubuntu 检查

```bash
cat /etc/os-release
uname -m
nproc
free -h
df -h /
timedatectl status
sudo ss -lntp
```

预期：

- 系统为 Ubuntu 22.04 或 24.04，架构是 `x86_64`。当前依赖固定 CPU 版 PyTorch，本文不用于 ARM 或 GPU 部署。
- 建议首次测试使用 **4 核或以上、16 GB 或以上内存**。这是容量建议，不是已经完成的服务器压测结论；如果 Chatwoot 也在同机，还要为它单独预留资源。
- 8 GB 内存机器可能在模型预热、重排或建立向量库时内存不足，不建议把加 Swap 视为解决方案；优先扩容或选择更大测试机。
- 当前本机四个模型目录合计约 **8.38 GiB**。服务器还要容纳上传包、镜像、构建缓存和向量库，建议开始时至少有 **25–30 GB 可用磁盘**，构建过程中继续检查余量。你的 49 GB 系统盘可做小数据量首测，但需要控制临时文件占用。
- 检查 3001、5433、80、443 是否已被使用。如果已有 Chatwoot 或反向代理，不要停止或覆盖它们。
- 系统时间应准确。Webhook 默认只接受时间偏差不超过 300 秒的签名。

如果时间同步未启用：

```bash
sudo timedatectl set-ntp true
timedatectl status
```

### 2.3 使用 tmux 防止 SSH 断开中止部署

```bash
sudo apt-get update
sudo apt-get install -y tmux curl ca-certificates openssl python3
tmux new -s cs-deploy
```

后面的耗时构建、导入、向量化都在这个 tmux 会话里运行。

- 暂时离开：先按 `Ctrl+B`，松开，再按 `D`。
- 重新 SSH 登录后继续：`tmux attach -t cs-deploy`。
- 如提示会话已存在，不要重新创建，直接 attach。

## 3. 检查或安装 Docker Compose

先检查已有环境：

```bash
docker version
docker compose version
sudo docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'
```

如果已经能正常运行 Docker 和 Compose V2，**跳过安装步骤**。你的登录信息里已有 Docker 网桥，服务器很可能已经装了 Docker。

如果 `docker` 存在但提示权限不足，用 `sudo docker version` 判断。可以让当前部署用户加入 Docker 组：

```bash
sudo usermod -aG docker ubuntu
```

退出并重新 SSH 登录，再运行 `docker version`；Docker 组具有相当高的主机权限，仅加入负责部署的账号。重新登录后重新 attach tmux。

注意：重新 attach 的旧 tmux 终端仍可能保留加入 Docker 组之前的权限。此时在该 tmux 终端执行 `newgrp docker`，再运行 `docker version`；也可以在重新登录后启动一个新的 tmux 会话。

只有确认服务器尚未安装 Docker 时，才执行以下官方软件源安装步骤。已经运行 Chatwoot 的主机不要盲目卸载现有 Docker 或 containerd。

```bash
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker ubuntu
```

重新登录后验证：

```bash
docker version
docker compose version
docker info --format '{{.DockerRootDir}}'
df -h /var/lib/docker
```

如果最后一项路径不存在，以 `DockerRootDir` 的实际值检查磁盘。安装源下载失败时先检查服务器出口、DNS 和代理；不要为此关闭 TLS 验证。

## 4. 从 Windows 准备并上传项目

### 4.1 必须传哪些文件

首次以你当前工作目录为准，包含尚未提交 Git 的修复；只拉远端仓库可能缺少刚加的图片转换、空回复兜底等代码。

```text
/opt/cs/
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── requirements.txt
├── .env.example
├── support-chat.html
├── ics_agent/                         完整应用代码，包含 retry/ 等新文件
├── knowledge_base_in/                 文档解析和向量化代码
├── deploy/server/                     本文附带配置模板
├── docs/                             本手册
├── models/
│   ├── bge-m3/
│   ├── bge-reranker-v2-m3/
│   ├── google-bert--bert-base-chinese/snapshots/master/
│   └── intent_classifier_bert/
└── database/
    ├── 最新问答对.xlsx
    ├── intent_dataset_4000.csv
    └── knowledge_base/                原始知识库文件
```

不要上传本机 `.env`、Anaconda 环境、历史日志、`.git`、Excel 临时锁文件或整套 Chatwoot 数据库。首次部署在服务器从知识库原文件重建向量库，不直接复制正在使用的 `knowledge_base_in/vector_store`。

`models/` 和向量库被 Git 忽略；模型、数据又被 `.dockerignore` 排除出镜像，运行时依靠目录挂载。**有镜像不等于有模型和业务数据。**

### 4.2 打包当前工作区

先保存并关闭正在编辑的 FAQ Excel。下面是 **Windows PowerShell** 命令，不是在 Ubuntu 执行：

```powershell
Set-Location D:\project\CS
tar.exe -h -czf D:\cs-server-bundle.tar.gz `
  --exclude=__pycache__ `
  --exclude=*.pyc `
  --exclude=knowledge_base_in/output `
  --exclude=knowledge_base_in/vector_store `
  Dockerfile docker-compose.yml .dockerignore requirements.txt .env.example `
  support-chat.html ics_agent knowledge_base_in deploy docs models `
  database/最新问答对.xlsx database/intent_dataset_4000.csv database/knowledge_base
if ($LASTEXITCODE -ne 0) { throw '打包失败，请检查上方错误。' }
Get-FileHash D:\cs-server-bundle.tar.gz -Algorithm SHA256
```

`-h` 会把模型符号链接所指向的实际内容打入包，避免迁移后链接失效。模型很大，打包可能持续较长时间。压缩包放在 `D:\`，不会把压缩包自身打进去。

### 4.3 上传

在 **Windows PowerShell** 的另一个终端执行：

```powershell
scp -o ServerAliveInterval=30 D:\cs-server-bundle.tar.gz ubuntu@YOUR_SERVER_ADDRESS:~/
```

如果连接容易断，可以用 WinSCP 的 SFTP 上传同一个压缩包，利用续传功能。仍使用你平时的 SSH 私钥或密码；不要临时关闭 SSH 认证。

### 4.4 在 Ubuntu 校验和解压

回到 Ubuntu 的 tmux 会话：

```bash
sha256sum ~/cs-server-bundle.tar.gz
```

与 Windows 输出的 SHA256 完全一致再继续：

```bash
sudo mkdir -p /opt/cs
sudo chown ubuntu:ubuntu /opt/cs
cd /opt/cs
tar -xzf ~/cs-server-bundle.tar.gz
mkdir -p log knowledge_base_in/output knowledge_base_in/vector_store
ls -la
du -sh models database
df -h /
```

首次部署 `/opt/cs` 应是新目录。如果该路径已经有另一套运行服务，先改用独立路径并调整后续命令，不要直接覆盖。

确认至少存在：

```bash
test -f Dockerfile && echo 'Dockerfile OK'
test -f support-chat.html && echo 'support-chat.html OK'
test -f ics_agent/retry/reconnect.py && echo 'retry code OK'
test -f ics_agent/app/chatwoot/message_format.py && echo 'image formatting code OK'
test -f models/intent_classifier_bert/model.safetensors && echo 'intent model OK'
test -f models/intent_classifier_bert/labels.json && echo 'intent metadata OK'
test -f database/intent_dataset_4000.csv && echo 'intent dataset OK'
test -f 'database/最新问答对.xlsx' && echo 'FAQ Excel OK'
find models -type l -print
```

如果仍有符号链接，确认不是悬空链接。不要只传模型的 `config.json` 而漏掉权重文件。

解压、校验成功后，如磁盘余量不足，可以删除上传包释放空间；Windows 原包仍保留：

```bash
rm -i ~/cs-server-bundle.tar.gz
```

## 5. 配置服务器 .env

### 5.1 创建独立配置文件

```bash
cd /opt/cs
umask 077
cp -n deploy/server/bot.env.example .env
chmod 600 .env
nano .env
```

`cp -n` 不覆盖已有 `.env`。如果是重复操作，直接编辑已有文件。Nano 保存：`Ctrl+O` → 回车；退出：`Ctrl+X`。

### 5.2 生成四份随机值

在 Ubuntu 执行以下命令四次，分别用于数据库密码、管理员密码、JWT Secret 和准备阶段的临时 Webhook Secret：

```bash
openssl rand -hex 32
```

将结果填到以下四个配置项，每项使用不同的随机值：

```dotenv
POSTGRES_PASSWORD=你生成的第一份值
ADMIN_INITIAL_PASSWORD=你生成的第二份值
ADMIN_JWT_SECRET=你生成的第三份值
CHATWOOT_AGENT_BOT_SECRET=你生成的第四份值
```

使用十六进制随机值能避免 `.env` 中 `$`、`#`、空格等特殊字符的转义问题。`ADMIN_USERNAME=csadmin` 是本手册的管理员账号；它与 Chatwoot 登录账号没有关系。

### 5.3 填写已有 Chatwoot 地址和用户 API Token

```dotenv
CHATWOOT_BASE_URL=https://你的Chatwoot域名
CHATWOOT_PUBLIC_BASE_URL=https://你的Chatwoot域名
CHATWOOT_ACCOUNT_ID=实际Account数字ID
CHATWOOT_API_TOKEN=实际用户API令牌
CHATWOOT_AUTO_REPLY_ENABLED=false
```

- `CHATWOOT_BASE_URL` 要能从机器人容器访问，用于后台 API 调用。
- `CHATWOOT_PUBLIC_BASE_URL` 要能从浏览器访问，用于加载 Widget。
- 使用公网 HTTPS 域名时，两者通常相同。
- 不要照搬 Windows Docker Desktop 的 `host.docker.internal`：Ubuntu 默认不提供同样的解析，当前 Compose 也没有配置它。
- 不要在容器里用 `http://127.0.0.1:3000` 指代另一台服务器或另一个容器的 Chatwoot。
- `CHATWOOT_CONTACT_API_TOKEN` 留空时，联系人操作回退使用用户级 `CHATWOOT_API_TOKEN`。
- AgentBot ID 和 Access Token 先留空；Secret 先填刚生成的随机值，第 11 节用 Chatwoot 返回的真实 Secret 替换。两个签名 Secret 均为空时，当前程序会跳过签名校验，因此不能将它们同时留空后公开 Webhook。

### 5.4 填写大模型配置

```dotenv
LLM_MODEL=本机已经调用成功的模型名称
LLM_MODEL_PROVIDER=openai
LLM_BASE_URL=该服务商要求的API根地址
LLM_API_KEY=你的模型API密钥
```

本项目可接 OpenAI 兼容接口。是否带 `/v1` 以你的实际服务商为准；不要自行拼接 `/chat/completions` 到 Base URL。

模板保守设置：Webhook 并发 2、模型并发 2、每次模型请求超时 30 秒、整轮检索和生成超时 120 秒。先验收，再依据 CPU、内存和响应耗时增加并发。

### 5.5 数据库配置保持一致

```dotenv
POSTGRES_DB=cs
POSTGRES_DATABASE=cs
POSTGRES_USER=cs
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_HOST_PORT=5433
POSTGRES_DSN=
ADMIN_WEB_HOST=127.0.0.1
ADMIN_WEB_PORT=3001
```

- `POSTGRES_DB` 创建数据库，`POSTGRES_DATABASE` 告诉应用连接哪个数据库，两者必须相同。
- 容器内连接 `postgres:5432`；宿主机映射为 `127.0.0.1:5433`。端口 5433 已占用时可改 `POSTGRES_HOST_PORT=5434`，应用内部仍使用 5432。
- 保持 `POSTGRES_DSN` 为空，避免旧连接串覆盖这些配置。
- 不要把这里填成 Chatwoot 正在使用的数据库。

### 5.6 数据保留设置

本模板建议首测：`RAG_MEMORY_RETENTION_DAYS=30`，Webhook 幂等记录保留 7 天。根目录原 `.env.example` 的会话记忆默认值为 `0`，表示永久保留；本手册明确选择了不同的测试保留期。

“已解决”和“用户 6 小时无动作”用于开启新上下文，不会立即物理删除旧记忆，也不会删除 Chatwoot 聊天记录。

## 6. 修改客服测试页，清除本机地址

当前 `support-chat.html` 中 `var CONFIG` 附近有本地 IP 和本地 Website Token。必须在构建镜像前处理：

```bash
cd /opt/cs
nano support-chat.html
```

按 `Ctrl+W` 搜索 `var CONFIG`，将相关字段改成：

```javascript
var CONFIG = {
  CHATWOOT_BASE_URL: "https://你的Chatwoot域名",
  WEBSITE_TOKEN: "REPLACE_WITH_CHATWOOT_WEBSITE_TOKEN",
  PROFILE_ENDPOINT: "/chatwoot/profile",
  RUNTIME_CONFIG_ENDPOINT: "/support-chat/config",
  REQUIRE_IDENTIFIER_HASH: true,
  ALLOW_ANONYMOUS_CHAT: true,
  DEFAULT_LOCALE: "zh_CN",
  PROFILE_TIMEOUT_MS: 10000,
  WIDGET_READY_TIMEOUT_MS: 15000
};
```

第一次先保留 Website Token 占位符，后面通过 URL 参数传入新测试 Inbox 的 Token，可避免建 Inbox 后又重新构建镜像。

`RUNTIME_CONFIG_ENDPOINT` 必须是 `/support-chat/config`，这样页面才能从当前机器人服务获取 `CHATWOOT_PUBLIC_BASE_URL`。它仍指向本机 `192.168.*` 地址时，远端页面可能访问失败或遭到 HTTPS 混合内容拦截。

首次按匿名访客测试，不加 `userId`、`identifier`、`appToken` 等参数。`CHATWOOT_PROFILE_ALLOW_QUERY_IDENTIFIERS=false` 保持不变。APP 的真实登录身份接入是独立工作，见第 15 节。

另一个 `chatwoot-chat.html` 不在当前 Dockerfile 中，也不是 `/support-chat` 实际返回的文件；修改它不会改变本次部署页面。

## 7. 构建镜像，启动机器人数据库

```bash
cd /opt/cs
docker compose -f docker-compose.yml config --quiet
docker compose -f docker-compose.yml build ics-agent
```

不要运行不带 `--quiet` 的 `docker compose config` 后把完整输出发给别人，它可能展开密钥。

首次构建需要下载 Python 依赖、CPU 版 PyTorch 等，可能耗时较久。使用 tmux 等待构建结束；不要因终端暂时没有新输出重复发起构建。成功时应显示镜像构建完成，命令退出码为 0。

```bash
docker compose -f docker-compose.yml up -d postgres
docker compose -f docker-compose.yml ps
```

等 PostgreSQL 显示 `healthy`，再执行：

```bash
docker compose -f docker-compose.yml exec -T postgres pg_isready -U cs -d cs
```

预期包含 `accepting connections`。若改了数据库用户名/库名，以上参数一并修改。

此时不要先启动正式应用，先初始化下一节的数据，避免 FAQ 以空表完成初始化。

## 8. 初始化 FAQ 和知识库

### 8.1 创建 FAQ 表

```bash
cd /opt/cs
docker compose -f docker-compose.yml run --rm --no-deps ics-agent \
  python -c 'from ics_agent.faq.repositories import ensure_faq_table; print("created:", ensure_faq_table())'
```

预期没有异常。`created: True` 表示首次创建，`False` 表示已经存在。

这里不能省略：应用正常启动只建表，不会自动把 Excel 导入；而 `sync-excel` 本身要求表已存在。

### 8.2 导入 FAQ Excel

```bash
docker compose -f docker-compose.yml run --rm --no-deps ics-agent \
  python -m ics_agent.faq sync-excel --excel-path 'database/最新问答对.xlsx'
```

预期看到 `FAQ sync completed` 和非零的 `source_rows`。本条命令没有要求刷新独立 CLI 进程的索引，因此 `bm25_documents=None`、`bm25_reloaded=False` 是正常的；以下面的数据库行数和 FAQ 实际查询为准。当前同步是全量替换；这里只对新建的机器人数据库操作。

检查行数：

```bash
docker compose -f docker-compose.yml exec -T postgres \
  psql -U cs -d cs -c 'SELECT COUNT(*) AS faq_rows FROM faq_qna_optimized;'
```

选 Excel 中一条确实存在的问题测试，例如将下面的问题替换成你确认的原句：

```bash
docker compose -f docker-compose.yml run --rm --no-deps ics-agent \
  python -m ics_agent.faq query '怎么充值'
```

应能返回 `route: faq` 和对应答案。如果示例问题不在 Excel 中，返回 `rag` 不能直接判定部署失败，改用 Excel 原句测试。

### 8.3 从原始知识库重建向量库

检查输入文件确实存在：

```bash
find database/knowledge_base -maxdepth 2 -type f | head -20
```

执行完整解析和向量化：

```bash
docker compose -f docker-compose.yml run --rm --no-deps ics-agent \
  python -m knowledge_base_in.parse_documents --fail-fast
```

此命令会生成 `knowledge_base_in/output/chunks.jsonl`，并自动重建 Chroma 向量库；**不需要随后再跑一遍 `build_chromadb`**。

首次涉及图片 OCR 时，相关库可能还需要初始化或下载模型资源。确认网络出口可用，且不要把解析报错当成成功。只看到生成分块文件，也不代表向量化已经完成；等待整个命令退出且退出码为 0。

```bash
echo $?
wc -l knowledge_base_in/output/chunks.jsonl
du -sh knowledge_base_in/vector_store
df -h /
```

`echo $?` 必须紧跟上一条解析命令执行，才表示该命令的退出状态。后两项应显示非空数据。向量化失败时先排错，不要带着空知识库继续验收。

## 9. 正式启动机器人并完成本地检查

### 9.1 启动

```bash
cd /opt/cs
docker compose -f docker-compose.yml up -d ics-agent
docker compose -f docker-compose.yml ps
```

首次模型预热可能需要一段时间，CPU 和内存会上升。另开 SSH 终端观察：

```bash
docker stats --no-stream
free -h
```

应用启动器把主要日志写入挂载的文件，**只看 `docker logs` 可能看不到真正的应用异常**：

```bash
cd /opt/cs
tail -n 100 log/uvicorn-admin.stdout.log
tail -n 100 log/uvicorn-admin.stderr.log
```

持续查看可使用 `tail -f log/uvicorn-admin.stderr.log`，退出查看按 `Ctrl+C`，不会停止服务。

### 9.2 健康接口

```bash
curl --fail --max-time 20 http://127.0.0.1:3001/health
```

预期：`{"status":"ok","service":"ics_agent"}`。

**这个接口只说明 HTTP 服务能响应，不验证模型、数据库、FAQ 或向量库。** 当前启动逻辑会记录部分初始化错误并继续启动，所以还必须完成下面的检查。

### 9.3 检查日志和数据库表

```bash
grep -E 'ERROR|Traceback|失败|warmup|预热|ready|lifecycle' log/uvicorn-admin.stderr.log | tail -60
docker compose -f docker-compose.yml exec -T postgres psql -U cs -d cs -c '\dt'
```

应能看到 FAQ、`system_users`、`chatwoot_webhook_idempotency`、会话控制及记忆相关表。日志不应持续出现模型缺失、数据库连接失败、管理员初始化失败或预热失败。

### 9.4 验证模型文件和意图数据集

```bash
docker compose -f docker-compose.yml exec -T ics-agent python - <<'PY'
import hashlib
import json
from pathlib import Path
metadata = json.loads(Path('models/intent_classifier_bert/labels.json').read_text(encoding='utf-8'))
dataset = metadata.get('dataset', {})
print('model dataset:', dataset.get('path'))
path = Path('database/intent_dataset_4000.csv')
actual = hashlib.sha256(path.read_bytes()).hexdigest()
expected = dataset.get('sha256')
print('dataset fingerprint matches:', actual == expected)
assert expected and actual == expected, 'Dataset differs from training metadata'
PY
```

当前工作区的模型元数据使用相对路径 `database/intent_dataset_4000.csv`，可跨系统迁移。若你之后更换了模型或训练数据，需保持两者配套；不要把关闭校验作为默认修复办法。

### 9.5 FAQ 与 RAG 各做一次测试

先用 FAQ 中确实存在的原句：

```bash
curl --fail-with-body --max-time 180 \
  -H 'Content-Type: application/json' \
  -d '{"query":"怎么充值","thread_id":"server-check:faq-001"}' \
  http://127.0.0.1:3001/rag/query
```

再选一条“知识库里有依据，但 FAQ 不能直接命中”的实际问题，把 JSON 中的问题换掉：

```bash
curl --fail-with-body --max-time 180 \
  -H 'Content-Type: application/json' \
  -d '{"query":"请替换成知识库里有依据、FAQ中没有直接答案的问题","thread_id":"server-check:rag-001"}' \
  http://127.0.0.1:3001/rag/query
```

第二条必须替换成真实测试问题再执行。验收应同时满足：

- `route` 确实为 `rag`；`faq` 不能验证向量检索，`chitchat` 也不能验证知识库。
- `retrieved_count`、`reranked_count` 大于 0，`documents` 中资料与问题相关。
- `answer` 非空，内容符合知识库，且不是“当前智能客服正在维护……”的兜底文案。

RAG 测试会实际调用你配置的云端模型。手工 `/rag/query` 没有 Chatwoot 路径的 120 秒整体兜底；`curl --max-time` 只限制当前客户端等待，失败后先看日志，不要连续重复提交。

### 9.6 通过 SSH 隧道访问管理后台

在 **Windows PowerShell** 新开终端执行，窗口保持开启：

```powershell
ssh -N -o ServerAliveInterval=30 -L 3001:127.0.0.1:3001 ubuntu@YOUR_SERVER_ADDRESS
```

Windows 浏览器打开 `http://127.0.0.1:3001/admin`，用 `.env` 的 `ADMIN_USERNAME` 和 `ADMIN_INITIAL_PASSWORD` 登录。

若本机 3001 已被你本地客服服务占用，换本地端口，不改服务器端口：

```powershell
ssh -N -o ServerAliveInterval=30 -L 13001:127.0.0.1:3001 ubuntu@YOUR_SERVER_ADDRESS
```

此时打开 `http://127.0.0.1:13001/admin`。预期能看到 FAQ、知识库维护和日志界面。

管理员初始密码只在首次创建记录时生效。数据库已有管理员后，修改 `.env` 的初始密码并重启不会重置密码。

## 10. 让 Chatwoot 能访问机器人

先选择一种方式：

| 情况 | 选择 |
| --- | --- |
| 有可用机器人域名，希望稳定测试 | 10A：HTTPS 域名，推荐 |
| 暂无域名，但 Chatwoot 能访问机器人的私网 IP | 10B：受限的私网 HTTP 首测 |
| 暂无域名、两台机器私网不通 | 先完成第 9 节本地测试，再配置可达域名、VPN 或反向代理；电脑能 SSH 不代表 Chatwoot 能访问 Webhook |

端口含义：

| 端口 | 谁需要访问 | 建议 |
| --- | --- | --- |
| SSH 22 或自定义端口 | 你的管理电脑/堡垒机 | 保留现有规则，不在本次部署中改 SSH 端口 |
| 443 / 80 | Chatwoot、浏览器 / 证书校验 | HTTPS 方案使用 |
| 3001 | 本机反向代理，或受限私网中的 Chatwoot | HTTPS 方案绑定 127.0.0.1，不直接向公网开放 |
| 5433 | 仅主机本地 | 无需任何公网入站规则 |

### 10A. HTTPS 域名方式

#### 第一步：配置 DNS

将 `bot.example.com` 的 A 记录指向机器人服务器公网 IP，云安全组允许所需的 80/443 入站流量。不要把公网 DNS 指向 `172.20.48.12` 这样的私网地址。

在 Ubuntu 检查解析和已有监听：

```bash
getent ahostsv4 bot.example.com
sudo ss -lntp | grep -E ':80 |:443 '
```

下文所有 `bot.example.com` 都换成你的域名。

#### 第二步：根据已有反向代理选择操作

- 已有**宿主机 Nginx**：只新增机器人站点，不覆盖 Chatwoot 站点。
- 已有宝塔、Nginx Proxy Manager、Caddy、云负载均衡或容器反向代理：在现有产品里新增域名和路由，按下面的路径规则配置，不再安装第二套代理抢占 80/443。
- 尚无代理且 80/443 空闲：安装宿主机 Nginx：

```bash
sudo apt-get install -y nginx certbot python3-certbot-nginx
sudo systemctl enable --now nginx
```

如果代理本身运行在容器内，`127.0.0.1:3001` 指向代理容器而不是机器人，不能直接套用本例。应由现有运维按 Docker 网络配置可达上游；也可采用下一节已经限制来源的主机私网地址作为上游。

#### 第三步：安装机器人站点配置

以下仅适用于宿主机 Nginx：

```bash
sudo cp /opt/cs/deploy/server/nginx-bot.conf.example /etc/nginx/sites-available/ics-agent
sudo nano /etc/nginx/sites-available/ics-agent
```

把 `server_name bot.example.com;` 改成真实域名。再执行：

```bash
sudo ln -s /etc/nginx/sites-available/ics-agent /etc/nginx/sites-enabled/ics-agent
sudo nginx -t
sudo systemctl reload nginx
```

如果提示软链接已存在，不用再建；先检查它指向的文件。只有 `nginx -t` 成功才 reload。

模板只公开 `/webhooks/chatwoot`、`/health`、`/support-chat`、`/support-chat.html` 和 `/support-chat/config`，其他路径默认返回 404。特别是当前 **`/rag/query` 没有认证**，会调用付费模型，不应通过公网默认开放。管理员使用第 9.6 节的 SSH 隧道。

#### 第四步：申请 HTTPS 证书

已有宿主机 Nginx 不代表已经安装 Certbot，先检查：

```bash
command -v certbot
```

没有输出时，只补装证书工具：

```bash
sudo apt-get install -y certbot python3-certbot-nginx
```

然后申请：

```bash
sudo certbot --nginx -d bot.example.com
```

根据提示填写邮箱，同意服务条款，启用 HTTP → HTTPS 跳转。前提是 DNS 正确、80/443 可达，且没有代理拦截证书验证。

```bash
curl --fail --max-time 20 https://bot.example.com/health
sudo certbot renew --dry-run
```

如果已经有统一证书管理，使用现有方式，不重复申请。

#### 第五步：验证签名保护

发一个**不带签名**的探测请求：

```bash
curl -sS -o /dev/null -w '%{http_code}\n' \
  -H 'Content-Type: application/json' -d '{}' \
  https://bot.example.com/webhooks/chatwoot
```

预期是 **401**。如果是 202，立即检查临时 `CHATWOOT_AGENT_BOT_SECRET` 是否已填写，并通过 `up -d --force-recreate ics-agent` 更新容器环境后再测。不要把“未签名请求能通过”当作连通成功。

Webhook 最终地址是：

```text
https://bot.example.com/webhooks/chatwoot
```

### 10B. 无域名的私网测试方式

先确认 Chatwoot 所在主机和它的任务容器能访问 `172.20.48.12`。它是私网 IP，不是 Internet 公网入口。

1. 云安全组为机器人服务器的 TCP 3001 添加一条**只允许 Chatwoot 实际来源 IP/安全组访问**的规则，不允许 `0.0.0.0/0`。
2. 如果两者同机，核实 Docker 容器到主机私网 IP 的访问及主机防火墙规则。
3. 编辑 `/opt/cs/.env`，将 `ADMIN_WEB_HOST=127.0.0.1` 改为服务器实际私网 IP，例如 `ADMIN_WEB_HOST=172.20.48.12`。
4. 重建应用容器环境和端口映射：

```bash
cd /opt/cs
docker compose -f docker-compose.yml up -d --force-recreate ics-agent
curl --fail http://172.20.48.12:3001/health
```

改成私网绑定后，本文后续所有**在服务器执行**的 `http://127.0.0.1:3001` 都改为 `http://172.20.48.12:3001`。Nginx 的 127.0.0.1 上游也不再适用；10A 与 10B 是两条选择路径。

SSH 隧道也相应改成：

```powershell
ssh -N -o ServerAliveInterval=30 -L 13001:172.20.48.12:3001 ubuntu@YOUR_SERVER_ADDRESS
```

从 Chatwoot 主机访问：

```bash
curl --fail --max-time 10 http://172.20.48.12:3001/health
```

如果 Chatwoot 使用 Compose，实际 Webhook 一般由 Sidekiq 容器发出。请在**已有 Chatwoot 的部署目录**检查容器侧网络，服务名按实际修改：

```bash
docker compose exec -T sidekiq ruby -rnet/http -e 'u=URI("http://172.20.48.12:3001/health"); puts Net::HTTP.get_response(u).code'
```

预期为 200。这只验证网络，不代表 Chatwoot 的 URL 安全检查已允许访问私网。

本地验证的 Chatwoot 4.17.1 对内网 Webhook 有访问限制；当错误日志明确提示私网地址被拒绝时，可由 Chatwoot 管理员在其已有部署配置中为 Rails 和 Sidekiq 设置 `SAFE_FETCH_ALLOW_PRIVATE_NETWORK=true`，再重建对应容器。不同服务器版本可能不同，先核对该版本支持的配置。此设置扩大 Chatwoot 可请求的网络范围，优先使用 10A 的有效 HTTPS 公网域名。

本路径的 Webhook 地址为：

```text
http://172.20.48.12:3001/webhooks/chatwoot
```

这里仍必须使用真实签名 Secret。使用与 10A 相同的未签名探测，把 URL 换成私网地址，预期同样为 401。

Docker 发布端口时可能绕过部分 UFW 规则，不能只凭 `ufw status` 判断已经限制来源；核对云安全组和实际连通结果。浏览器仍通过 SSH 隧道测试，Chatwoot 的浏览器地址仍填写已有的 HTTPS 域名。

## 11. 创建测试 AgentBot，连接已有 Chatwoot

### 11.1 新建或选择测试 Website Inbox

在已有 Chatwoot 后台：

1. 打开“设置 → 收件箱 / Inboxes → 添加收件箱”。
2. 选择 Website 网站渠道，名称例如“智能客服服务器测试”。
3. 网站地址填写本次测试页所在地址；有域名时为 `https://bot.example.com`。
4. 把用于验收的人工坐席加入这个 Inbox。机器人与人工在同一个 Inbox 内切换。
5. 保存后，在 Widget 安装代码中找到 `websiteToken`，记录该值。
6. 记录 Inbox ID。一般能在 Inbox 设置页地址或下面的 API 查询结果中看到。

不要把 Account ID、Inbox ID、AgentBot ID 混用。第一次测试可保持所有人工坐席离线，验证机器人后再让一名测试坐席上线。

### 11.2 选择 UI 配置或 API 配置

如果 Chatwoot 后台提供“Agent Bots / 机器人”页面：创建 **Webhook 类型**机器人，Outgoing URL 填第 10 节的 Webhook 地址，记录 AgentBot ID、Access Token、Secret，先不要绑定真实业务 Inbox。三个值分别填入机器人 `.env`，再把 `CHATWOOT_AUTO_REPLY_ENABLED=true`。

如果 UI 没有这个入口，使用下面经过本地 Chatwoot 4.17.1 源码核对的 Account API 操作。已有服务器版本仍需支持这些接口；出现 404 或响应没有 Secret 时停止核对版本，不能靠关闭签名校验继续。

**UI 和 API 二选一，不要重复创建两次机器人。** 第 11.3–11.5 节是完整 API 路径。

### 11.3 API 路径：确认账号与 Inbox

以下命令在**机器人 Ubuntu 服务器**执行。先填写变量；重新 SSH 登录或新开终端后，变量不会自动保留：

```bash
cd /opt/cs
umask 077
CW_DEPLOY_BASE='https://你的Chatwoot域名'
CW_DEPLOY_ACCOUNT='实际Account数字ID'
CW_DEPLOY_INBOX='实际测试Inbox数字ID'
CW_DEPLOY_WEBHOOK='https://你的机器人域名/webhooks/chatwoot'
read -r -s -p '粘贴 Chatwoot 用户 API Token，然后回车：' CW_DEPLOY_TOKEN
printf '\n'
```

10B 私网方式把 `CW_DEPLOY_WEBHOOK` 换成私网地址。输入 Token 时不显示字符是正常的。

获取测试 Inbox 信息，先保存到权限受限的本地文件：

```bash
curl --fail-with-body -sS \
  -H "api_access_token: $CW_DEPLOY_TOKEN" \
  "$CW_DEPLOY_BASE/api/v1/accounts/$CW_DEPLOY_ACCOUNT/inboxes/$CW_DEPLOY_INBOX" \
  -o .env.chatwoot-inbox.json
```

只打印用于核对的非后台密钥字段：

```bash
python3 - <<'PY'
import json
from pathlib import Path
d = json.loads(Path('.env.chatwoot-inbox.json').read_text())
print({k: d.get(k) for k in ('id', 'name', 'channel_type', 'website_token')})
PY
```

必须确认名称与 ID 是测试 Inbox，渠道为 Website/WebWidget。401 表示 Token 错误，403 常见于账号权限不足，404 先核对 Account ID 和 Inbox ID。

### 11.4 API 路径：创建机器人并写入配置

生成请求文件：

```bash
python3 - "$CW_DEPLOY_WEBHOOK" > .env.agentbot-create.json <<'PY'
import json
import sys
print(json.dumps({
    'name': '智能客服服务器测试',
    'description': 'ics_agent 首次服务器部署测试',
    'outgoing_url': sys.argv[1],
    'bot_type': 'webhook',
}, ensure_ascii=False))
PY
```

创建一次即可；重复执行 POST 会创建多个机器人：

```bash
curl --fail-with-body -sS -X POST \
  -H "api_access_token: $CW_DEPLOY_TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary @.env.agentbot-create.json \
  "$CW_DEPLOY_BASE/api/v1/accounts/$CW_DEPLOY_ACCOUNT/agent_bots" \
  -o .env.agentbot-response.json
```

成功响应包含机器人 ID、Access Token 和 Secret。以下脚本会备份 `.env`，把这三个字段和自动回复开关写入 `.env`，**不会把密钥打印出来**：

```bash
python3 - <<'PY'
import json
import shutil
from datetime import datetime
from pathlib import Path

bot = json.loads(Path('.env.agentbot-response.json').read_text(encoding='utf-8'))
required = ('id', 'access_token', 'secret')
if not all(bot.get(k) for k in required):
    raise SystemExit('响应缺少 id/access_token/secret；先核对 API 权限及 Chatwoot 版本，不要绑定 Inbox。')
updates = {
    'CHATWOOT_AGENT_BOT_ID': str(bot['id']),
    'CHATWOOT_AGENT_BOT_ACCESS_TOKEN': str(bot['access_token']),
    'CHATWOOT_AGENT_BOT_SECRET': str(bot['secret']),
    'CHATWOOT_AUTO_REPLY_ENABLED': 'true',
}
env = Path('.env')
shutil.copy2(env, '.env.before-agentbot-' + datetime.now().strftime('%Y%m%d%H%M%S'))
lines = env.read_text(encoding='utf-8').splitlines()
new_lines = []
written = set()
for line in lines:
    key = line.split('=', 1)[0].strip() if '=' in line and not line.lstrip().startswith('#') else ''
    if key in updates:
        if key not in written:
            new_lines.append(key + '=' + updates[key])
            written.add(key)
    else:
        new_lines.append(line)
new_lines.extend(k + '=' + v for k, v in updates.items() if k not in written)
env.write_text('\n'.join(new_lines) + '\n', encoding='utf-8')
env.chmod(0o600)
print('已写入机器人配置，AgentBot ID =', bot['id'])
PY
```

保存 ID、Token、Secret 到自己的安全记录中。`.env.agentbot-response.json` 含密钥，不要上传或贴到日志问题截图。

### 11.5 让配置生效，再绑定测试 Inbox

无论 UI 还是 API 路径，填完 `.env` 后执行：

```bash
docker compose -f docker-compose.yml up -d --force-recreate ics-agent
```

**修改 `.env` 后仅执行 `restart` 不会更新 Compose 注入的环境变量；应使用上面的 recreate 命令。** 等服务启动，再检查 `/health` 和应用日志。

API 路径继续取刚创建的 ID：

```bash
CW_DEPLOY_BOT_ID=$(python3 -c "import json; print(json.load(open('.env.agentbot-response.json'))['id'])")
printf '即将将测试 Inbox %s 绑定到 AgentBot %s\n' "$CW_DEPLOY_INBOX" "$CW_DEPLOY_BOT_ID"
```

核对显示的是**测试 Inbox**后再执行绑定。这个操作会替换该 Inbox 原来的机器人绑定，因此不应对真实业务 Inbox 直接照抄：

```bash
curl --fail-with-body -sS -X POST \
  -H "api_access_token: $CW_DEPLOY_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"agent_bot\":$CW_DEPLOY_BOT_ID}" \
  "$CW_DEPLOY_BASE/api/v1/accounts/$CW_DEPLOY_ACCOUNT/inboxes/$CW_DEPLOY_INBOX/set_agent_bot"
```

成功可能没有响应正文，HTTP 成功状态即可。读回绑定关系：

```bash
curl --fail-with-body -sS \
  -H "api_access_token: $CW_DEPLOY_TOKEN" \
  "$CW_DEPLOY_BASE/api/v1/accounts/$CW_DEPLOY_ACCOUNT/inboxes/$CW_DEPLOY_INBOX/agent_bot" \
  -o .env.inbox-agentbot-check.json
python3 - <<'PY'
import json
from pathlib import Path
data = json.loads(Path('.env.inbox-agentbot-check.json').read_text())
bot = data.get('agent_bot', data)
if not isinstance(bot, dict) or not bot.get('id'):
    raise SystemExit('响应中未确认机器人绑定，请通过 Chatwoot UI 或该版本响应结构核对。')
print('绑定结果的非敏感摘要：')
print({k: bot.get(k) for k in ('id', 'name', 'outgoing_url')})
PY
unset CW_DEPLOY_TOKEN
```

如服务器版本返回包裹在 `agent_bot` 等字段内的结果，请在私有终端查看文件结构或通过 UI 核对；不要为了排查把完整响应公开。UI 路径则在测试 Inbox 设置的机器人选项里选择刚创建的机器人，并启用该绑定。

### 11.6 检查会话状态事件；按需补联系人事件

本地验证的 Chatwoot 4.17.1 AgentBot 会投递消息创建、会话状态变化及会话更新事件，足以支持当前的消息处理和“已解决”边界。你的已有服务器版本可能不同，务必通过第 12 节的“已解决后再咨询”实测确认。

AgentBot 通常不负责独立的 `contact_updated` 事件。若还要验证“坐席静音联系人后，按配置天数自动解除”的功能，可在 Chatwoot“设置 → 集成 / Integrations → Webhooks”增加 Account Webhook：

- URL：与本次机器人的 `/webhooks/chatwoot` 地址相同。
- 订阅 `contact_updated`；只有发现 AgentBot 没有送达状态变化时，再补 `conversation_status_changed` / `conversation_updated`。
- 将此 **Account Webhook 自己的 Secret** 填入 `CHATWOOT_WEBHOOK_SECRET`，不要用它覆盖 AgentBot Secret。
- 保存后执行 `docker compose -f docker-compose.yml up -d --force-recreate ics-agent`。

不要额外订阅另一份 `message_created` 来重复驱动同一机器人。当前支持两个 Secret，是为了验证不同来源的签名，不是要求同一消息通过两路触发。

## 12. 浏览器端完整验收

### 12.1 打开测试页面

有域名时，Windows 浏览器打开：

```text
https://bot.example.com/support-chat?websiteToken=测试Inbox的WebsiteToken
```

没有域名时，通过第 9.6 或 10B 节的 SSH 隧道打开：

```text
http://127.0.0.1:13001/support-chat?websiteToken=测试Inbox的WebsiteToken
```

如果你隧道本地端口是 3001，URL 也用 3001。本次不要添加任何用户身份参数。页面应加载已有 Chatwoot 的聊天窗口，Chatwoot 后台能看到消息进入**测试 Inbox**。

### 12.2 逐项验收清单

| 顺序 | 操作 | 通过标准 |
| --- | --- | --- |
| 1 | 输入 Excel 中一个标准 FAQ 问题 | 收到正确答案；日志 route 为 faq |
| 2 | 输入知识库有依据、FAQ 不能命中的问题 | 收到有依据的 RAG 回答；不是维护文案 |
| 3 | 问一个需要看图片的 FAQ | 支持的图片在 Widget 中显示；源图片地址可被浏览器访问 |
| 4 | 问含链接的 FAQ | 点击后地址正确，没有把后面的中文句子带进 URL |
| 5 | 一名测试坐席设为 online，用户发送“转人工” | 会话进入人工接管，分配至当前 Inbox 的可用坐席 |
| 6 | 人工接管后用户补充一句问题 | 机器人不再抢答，人工可以正常回复 |
| 7 | 坐席将会话标为“已解决”，用户重新发消息 | 恢复机器人处理，使用新一轮上下文 |
| 8 | 没有在线空闲坐席时申请转人工 | 收到无可用坐席提示，不伪装成已转接成功 |
| 9 | 重启机器人后，在测试会话继续提问 | 数据库和模型挂载仍有效，服务恢复正常 |

普通 AI 处理期间，会话会尽力保持 `pending`，避免进入人工的 open 待接管队列。转人工时才进入对应人工流程。人工坐席必须加入 **Widget 所在的同一个 Inbox**；当前项目不执行跨 Inbox 迁移。

第 4 项需要单独注意：图片标签转换已经加入，但普通裸链接紧跟中文标点的问题仍应在 FAQ 中改成明确的 Markdown 链接，例如 `[新人指南](https://example.com/guide)`；当前图片格式修复不等于自动修好了所有裸链接。

### 12.3 验证“已解决”确实切换上下文

在同一个浏览器、同一个 Chatwoot 会话里：

1. 先说一句带明显测试标记的话，例如“这次测试的代号是蓝鲸 427”。
2. 补问“我刚才的测试代号是什么”，观察连续上下文。
3. 坐席点击“已解决”。
4. 再问“我上一轮说的测试代号是什么”。机器人不应再依赖上一轮上下文。
5. 同时核对日志收到 `conversation_status_changed` 或会话更新事件。模型回答只能作观察，数据库中的咨询轮次变化才是更可靠的确认方式。

如果“已解决”后仍静默，优先检查状态 Webhook 是否送达、Inbox AgentBot 绑定是否仍启用，以及该会话是否仍被 Chatwoot 分配给其他处理对象。

### 12.4 验证 6 小时空闲边界

正式值 `CHATWOOT_SESSION_IDLE_TIMEOUT_SECONDS=21600`，是用户两条消息之间的间隔；机器人回复、坐席回复和页面浏览不刷新它。边界在下一条用户消息到来时判断，不会在第 6 小时主动发送消息或自动解决会话。

首次不用真的等 6 小时。仅在没有其他测试流量、没有真实业务 Inbox 绑定的环境中：

1. 将 `.env` 中这个值临时改为 `60`。
2. 执行 `docker compose -f docker-compose.yml up -d --force-recreate ics-agent`。
3. 在新测试轮次里发送一个测试标记。
4. 等超过 60 秒，中间不发用户消息，再询问之前的标记，观察新上下文。
5. 测完立即改回 `21600`，再次 recreate。

当前处于人工接管的会话，即使达到空闲边界，也不会因此自动解除人工接管。

### 12.5 验证空回复兜底，不破坏真实模型配置

以下命令只在容器里的独立进程模拟空回复，不修改运行服务，不调用真实模型，不给 Chatwoot 发消息：

```bash
docker compose -f docker-compose.yml exec -T ics-agent python - <<'PY'
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from ics_agent.rag.graph import nodes

async def check():
    model = AsyncMock(return_value=SimpleNamespace(content=None))
    with patch.object(nodes, 'call_cloud_llm', model), patch('ics_agent.retry.reconnect.asyncio.sleep', new=AsyncMock()):
        result = await nodes.llm_reply_node({'query': '服务器空回复测试', 'reranked_documents': []})
    assert result['answer'] == nodes.LLM_REPLY_FALLBACK_ANSWER
    assert model.await_count == 3
    print('PASS: 空回复重试后返回指定维护提示')

asyncio.run(check())
PY
```

预期最后输出 PASS。不要通过修改正在使用的 API Key 来模拟故障。

真实故障时固定维护文案为：

> 当前智能客服正在维护，请您发送「转人工」申请人工客服协助！

它是提示用户申请人工，不代表系统已经自动完成转接。

### 12.6 模拟部署后的正常重启

仅在当前测试请求已经结束后：

```bash
docker compose -f docker-compose.yml restart ics-agent
```

等待启动和预热结束，重新检查健康、后台登录和一次 FAQ 问答。重启保留数据卷和挂载目录；但当前内存队列没有自动持久化重放能力，不要在有未完成请求时把强制重启当作正常操作。

## 13. 日常更新和配置修改

### 13.1 只改 .env

```bash
cd /opt/cs
nano .env
docker compose -f docker-compose.yml up -d --force-recreate ics-agent
```

然后检查健康和日志。修改数据库密码不属于普通环境变量变更：PostgreSQL 已有数据卷时，改 `POSTGRES_PASSWORD` 不会自动修改已有数据库用户密码，可能导致应用连接失败。

### 13.2 修改 Python 代码、网页、依赖或 Dockerfile

先上传新文件，再执行：

```bash
docker compose -f docker-compose.yml up -d --build ics-agent
```

服务器未使用开发 override，所以只改文件不会自动进入已构建镜像；`support-chat.html` 也需要重新构建才会变化。

### 13.3 更新 FAQ

优先通过管理后台上传 Excel 并执行 FAQ 更新，可刷新当前 Web 进程的内存索引。

如果使用命令行：

```bash
docker compose -f docker-compose.yml run --rm --no-deps ics-agent \
  python -m ics_agent.faq sync-excel --excel-path 'database/最新问答对.xlsx'
docker compose -f docker-compose.yml restart ics-agent
```

同步是全量替换，更新前先备份。CLI 的 `--refresh-runtime` 只刷新该 CLI 进程，不能代替当前 Web 服务的刷新。

### 13.4 更新知识库

通过管理后台上传原文件并发起知识库更新，等待任务成功完成。大量更新放在测试空闲期，观察内存和磁盘。不要同时运行多个解析/重建任务。

### 13.5 暂停机器人自动回复

将 `CHATWOOT_AUTO_REPLY_ENABLED=false` 后 recreate 可停止普通 AI 自动回复，但**不等于完全停用机器人**，人工接管等逻辑可能仍处理事件。需要完全停止测试时，在测试 Inbox 解绑/禁用此 AgentBot，待已接收任务处理结束，再停止应用：

```bash
docker compose -f docker-compose.yml stop ics-agent
```

数据库可以继续运行。重新启用时先启动并验证应用，再恢复 Inbox 绑定。

## 14. 备份、恢复和版本留档

### 14.1 应备份的内容

| 内容 | 原因 |
| --- | --- |
| 机器人 PostgreSQL | FAQ、管理员、记忆、Webhook 幂等和人工接管状态 |
| `.env` | 实际服务器配置，包含密钥，应加密保存 |
| `database/` | FAQ Excel、意图 CSV、原始知识库 |
| `knowledge_base_in/output/` 和 `vector_store/` | 当前分块、向量库及活动版本信息 |
| 实际部署代码、镜像版本、模型版本 | 恢复到同一实现；模型体积大可单独保存一次 |

Chatwoot 聊天记录属于已有 Chatwoot 的备份体系，本项目备份不会替它备份数据库和附件。

### 14.2 首测验收后做一次备份

先让测试 Inbox 停止产生新机器人请求，等待当前回复完成；确认磁盘足够。本例备份到用户目录，随后应下载到其他机器，不应只留在同一系统盘。

```bash
cd /opt/cs
CS_BACKUP_DIR="$HOME/cs-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -m 700 "$CS_BACKUP_DIR"
docker compose -f docker-compose.yml stop ics-agent
docker compose -f docker-compose.yml exec -T postgres \
  sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' \
  > "$CS_BACKUP_DIR/bot-postgres.dump"
```

确认上一条命令退出码为 0、备份文件非空，再继续：

```bash
test -s "$CS_BACKUP_DIR/bot-postgres.dump" && echo '数据库备份文件已生成'
tar -czf "$CS_BACKUP_DIR/bot-files.tar.gz" \
  .env database knowledge_base_in/output knowledge_base_in/vector_store \
  ics_agent knowledge_base_in/*.py support-chat.html Dockerfile \
  docker-compose.yml requirements.txt deploy docs
chmod 600 "$CS_BACKUP_DIR/bot-postgres.dump" "$CS_BACKUP_DIR/bot-files.tar.gz"
docker compose -f docker-compose.yml start ics-agent
```

停止应用后再归档向量库，是为避免复制正在写入的数据库文件。如果中间备份失败，也要在排错后记得 `start ics-agent` 并检查服务。模型目录另外保留原始部署包或同步到备份存储。

### 14.3 新服务器空库恢复顺序

1. 在新服务器完成 Docker、代码、模型、`.env` 和挂载目录准备。
2. 应用保持停止，只启动机器人 PostgreSQL。
3. 从 `bot-files.tar.gz` 恢复文件到正确项目目录，并核对 `.env` 地址和密钥；不要覆盖正在运行的另一套系统。
4. **只对新建空数据库**执行以下恢复命令；已有数据的库需单独制定回滚计划，不要直接覆盖：

```bash
docker compose -f docker-compose.yml exec -T postgres \
  pg_restore -U cs -d cs --no-owner < /实际备份路径/bot-postgres.dump
```

5. 恢复成功后执行 `docker compose -f docker-compose.yml up -d --build ics-agent`，在新服务器构建并启动应用，再检查管理员登录、FAQ、RAG 和 Chatwoot 绑定。
6. 已恢复 FAQ 数据时，不需要再执行全量 `sync-excel` 覆盖它。

不要使用 `docker compose down -v` 排错，`-v` 会删除该 Compose 管理的数据卷。也不要直接复制运行中的 PostgreSQL 数据目录替代 `pg_dump`。

### 14.4 留下本次成功版本

当前 `requirements.txt` 有版本范围，没有完整锁文件；不同日期重新构建可能安装不同依赖。验收成功后记录：

```bash
mkdir -p deployment-records
docker compose -f docker-compose.yml exec -T ics-agent python -m pip freeze \
  > deployment-records/pip-freeze.txt
docker image inspect ics-agent:py310 --format '{{.Id}}' \
  > deployment-records/image-id.txt
docker image tag ics-agent:py310 ics-agent:server-verified-20261008
```

标签日期改成本次实际验收日期。保留旧的已验证镜像，可在新版本验证失败时切回；标签本身不会复制一份完整镜像。需要跨服务器恢复时，可另行导出镜像或推到你自己的镜像仓库。

## 15. APP 登录身份、性能和当前边界

### 15.1 匿名首测与 APP 身份接入

本手册用匿名 Widget 先验证部署链路。正式 APP 应由业务后端根据已验证的登录态生成 `identifier` 和 `identifier_hash`，而不是让浏览器随意传 `userId` 就获得某个用户的身份签名。

- Website HMAC Token 只放后端。
- 保持 `CHATWOOT_PROFILE_ALLOW_QUERY_IDENTIFIERS=false`。
- 实际业务后端提供身份接口后，再修改测试页的 Profile 接口接入；该接口的跨域允许来源应是实际页面域名。
- 修改代码后重新构建镜像。
- 新服务器首次部署无需执行 README 中针对旧匿名/实名历史数据的迁移 SQL。

### 15.2 首测不要横向扩容

当前使用单个应用进程、内存队列和进程内会话锁，FAQ 也有进程内索引。首测不要加多个 Uvicorn workers，不要执行 `--scale ics-agent=2`。想支持多个实例，需要先改造共享队列、会话串行和索引刷新方式。

本项目主要在 CPU 上运行本地检索模型，生成回复通过云端大模型。先测少量并发，关注 `docker stats`、内存、等待时间和模型服务限流，不要直接复制本机较大的 worker 配置。

### 15.3 当前服务恢复边界

- 数据库、知识库和正常会话状态在持久化存储中；容器正常重启不会清空。
- 已接收但尚未完成的请求排在内存队列，服务崩溃后没有自动重放所有待办消息的机制。
- Chatwoot 已收到 202 的请求不能假设一定会再次投递。
- 模型和检索异常有维护文案兜底；如果连 Chatwoot API 都无法发送消息，仍可能最终发送失败。

这些是当前 MVP 的实际能力边界，首次测试应记录结果，再决定是否增加任务恢复和人工告警。

## 16. 常见问题逐项排查

| 现象 | 先看哪里 | 处理 |
| --- | --- | --- |
| SSH 再次出现 Connection reset | 网络、云控制台、SSH 日志 | 重新连接后 attach tmux；构建可能仍在运行，不要重复启动 |
| `docker: permission denied` | 当前用户是否已加入 docker 组、是否重新登录 | 重新登录，或由管理员使用 sudo；不改 Docker socket 为所有人可写 |
| `port is already allocated` | `sudo ss -lntp` | 调整机器人 3001 / 数据库宿主机 5433 映射，不抢占 Chatwoot 原端口 |
| 构建下载超时 | 服务器能否访问 Python 源、PyTorch CPU 源、Docker Registry | 修复出口和 DNS 后重试，保留 TLS 校验 |
| 容器退出码 137 / `Killed` | `free -h`、`docker stats`、内核 OOM 日志 | 增加内存、减小批次/并发；检查是否在同机同时跑 Chatwoot 与向量化 |
| 磁盘满 | `df -h`、`docker system df`、`du -sh models log knowledge_base_in/vector_store` | 优先清理已核对的上传临时包；不要删除数据库卷或盲目 prune |
| `/health` 正常，但一直维护提示 | 应用 stderr 日志，不能只看 docker logs | 检查模型文件、向量库、数据库和模型 API；健康接口不是完整就绪检查 |
| FAQ 全走 RAG | FAQ 表行数、是否执行导入、是否已刷新服务内存 | 导入新库；若服务已先启动，导入后 restart 或使用后台更新 |
| 意图模型 fingerprint mismatch | CSV 与 `labels.json` 是否配套 | 重新上传配套 CSV/训练产物，检查传输是否改变文件内容 |
| `No such file` 或 Windows 路径 | `.env` 和模型元数据路径 | 使用 `/app` 相对路径，补齐模型实际权重；不复制 Windows 环境路径 |
| OCR 初始化失败 | stderr 的缺失依赖或模型下载错误 | 按明确错误补齐资源并重新构建；确认所需图片已成功提取文字再验收 |
| 数据库认证失败 | `.env`、数据库初始化时的密码 | 已有卷不会因改 env 自动改数据库密码，恢复正确配置或由管理员执行密码变更 |
| 管理员密码不对 | 首次初始化时的配置、system_users 是否已有记录 | 改 `ADMIN_INITIAL_PASSWORD` 不会覆盖已有密码；走账号重置流程，不删除整个库 |
| Webhook 401 | AgentBot Secret、Account Secret、时间同步 | Secret 与来源对应；recreate 应用；核对 Chatwoot 是否发送签名头 |
| Chatwoot 收到消息但机器人没日志 | Outgoing URL、Inbox AgentBot 绑定、Sidekiq 网络 | 从 Chatwoot 实际任务容器探测机器人，检查 Sidekiq 投递日志 |
| Chatwoot 报私网 URL 被拒 | Chatwoot 版本和 safe-fetch 日志 | 优先 HTTPS 域名；内网模式按该版本配置受控的私网请求支持 |
| 有答案日志但 Chatwoot 没收到 | `CHATWOOT_AUTO_REPLY_ENABLED`、API Token、发送错误 | 配置 true 并 recreate；用户 Token 与机器人 Token 各就其位 |
| 转人工提示没有坐席 | 测试 Inbox 成员、坐席 online 状态、open 会话数 | 坐席加入同一个 Inbox 并上线；确认未达到每人 5 个会话限制 |
| 已解决后仍不恢复机器人 | 会话状态事件是否到达、绑定是否启用 | 验证 AgentBot 状态事件，必要时补带正确 Secret 的状态 Webhook |
| 测试页访问本机 192.168 地址 | `support-chat.html` 中 CONFIG | 运行配置接口改为相对路径；重新 build，刷新页面缓存 |
| 页面提示缺少 websiteToken | 页面 URL 参数 / CONFIG | 用当前测试 Inbox 的 Website Token，不是用户 API Token |
| 页面身份校验 403 | URL 是否携带用户身份，Profile 接口是否关闭 | 首测移除身份参数，用匿名模式；正式身份由业务后端验证 |
| 图片不显示 | FAQ 原始内容、浏览器图片请求 | 确认图片链接返回图片且浏览器可访问；旧消息不会被自动回写 |
| HTTPS 证书失败 | DNS、80/443、安全组、已有代理 | 按代理现状排查，避免安装第二套代理造成端口冲突 |
| 改 .env 后行为没变化 | 是否只用了 restart | 改用 `up -d --force-recreate ics-agent` |
| 改代码或页面后没变化 | 是否重新 build | 服务器镜像模式执行 `up -d --build ics-agent` |

常用只读排查命令：

```bash
cd /opt/cs
docker compose -f docker-compose.yml ps
docker compose -f docker-compose.yml logs --tail 100 postgres
tail -n 100 log/uvicorn-admin.stderr.log
docker stats --no-stream
df -h /
free -h
sudo journalctl -k --since '30 minutes ago' | grep -i -E 'oom|out of memory|killed process'
```

发给他人排查时，提供错误类型、发生时间、相关消息/任务 ID 和脱敏日志；不要发完整 `.env`、API 响应密钥或真实用户整段对话。

## 17. 首次部署完成的验收记录

建议复制下面的清单，每完成一项填写日期和结果：

- [ ] Ubuntu 版本、CPU 核数、内存和剩余磁盘已记录。
- [ ] 服务器模型文件完整，意图 CSV 与模型指纹一致。
- [ ] Compose 使用显式 `-f docker-compose.yml`，没有开发热重载或多实例。
- [ ] PostgreSQL healthy，FAQ 表有数据。
- [ ] 知识库解析和向量化完成，RAG 测试确实使用了知识片段。
- [ ] `/health` 正常，应用日志没有持续初始化错误。
- [ ] 管理后台通过 SSH 隧道可登录。
- [ ] 公网没有直接暴露未认证的 `/rag/query` 和 PostgreSQL。
- [ ] 未签名 Webhook 返回 401，真实 Chatwoot 投递可通过。
- [ ] AgentBot 绑定的是测试 Inbox，自动回复以机器人身份发出。
- [ ] FAQ、RAG、图片、链接分别验收。
- [ ] 转人工、人工期间静默、已解决后新上下文分别验收。
- [ ] 6 小时边界测试后已恢复为 21600 秒。
- [ ] 空回复模拟验证通过，真实模型配置保持有效。
- [ ] 正常重启后，数据库、管理后台和问答仍可用。
- [ ] 备份已生成并移出服务器，成功镜像和依赖版本已记录。

只有完成上述验收，再考虑把已有业务 Inbox 接到新机器人；先记录业务 Inbox 原来的机器人绑定，保留切回方法。
