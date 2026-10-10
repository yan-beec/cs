# 智能客服机器人：Ubuntu 服务器首次部署测试手册

适用项目：`ics_agent` / `D:\project\CS`。更新日期：2026-10-08。本轮核对基线：分支 `test`，提交 `6b2e89a`，以及当前配置文件；本次国内网络构建修订还需提交、推送。实际发布以第 4 节记录的完整提交 SHA 为准。

本文按你已确认的环境编写：**Ubuntu 22.04 LTS、x86_64，已有服务器端 Chatwoot，本次只部署机器人并接入 Chatwoot。** Ubuntu 24.04 可按相同步骤操作。未登录你的远端服务器执行部署；本文命令依据当前项目源码、Compose 配置及本地已验证的 Chatwoot 接口核对。

当前 Chatwoot 根地址为 **`http://172.20.48.12:3000`，使用内网，没有公网域名**。前端已经单独部署，本次复用该前端。推荐通过 **Git 同步后端源码和配置模板，只单独上传模型包 `cs-models.tar.gz`**；服务器不能访问仓库时，仍可使用模型包与后端部署包 `cs-app.tar.gz` 分开上传的方案。

服务器最初登录信息显示系统盘约 49.10 GB、当时约有 41.7 GB 可用；上传模型后应重新检查余量。`172.20.48.12` 是私网地址，Windows 已配置 SSH 别名 `ics`。CPU 核数、内存总量及机器人是否与 Chatwoot 同机，仍需按第 2 节确认。

**你已完成的步骤：** 模型包已上传到 `/home/ubuntu/cs-models.tar.gz`，且与本机 SHA256 一致：

```text
f161f5bab8e534700fc9c1891c9132388e9cef733222974f7f83a608b9210831
```

如果之后没有重新训练或更换模型，这个包可继续使用，无需再打包、上传。这个校验说明传输完整，模型与当前意图 CSV 的配套关系还要按第 7.2 节验证。

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
- 服务器运行目录统一为 `/opt/cs`。每次重新登录后先执行 `cd /opt/cs`。采用 Git 时另用 `/opt/cs-src` 保存仓库，仅在该目录拉取代码；Docker 操作仍在 `/opt/cs` 执行。
- 本文统一使用 `docker compose -f docker-compose.yml ...`。不要省略 `-f`：仓库的 `docker-compose.override.yml` 会启用开发热重载，服务器首次部署采用镜像运行方式。
- 每个代码块执行完成、结果符合预期，再执行下一块。`YOUR_...`、`REPLACE_...`、示例域名和 ID 必须换成你的真实值。
- 先确认已有前端使用的 Chatwoot 和 Inbox。建议通过独立测试 Inbox 验收；切换已有开发测试 Inbox 的机器人前，记录原绑定并确认没有其他人正在测试。验收前不要切换正在接待真实用户的 Inbox。

本文附带以下部署文件：

| 文件 | 用途 |
| --- | --- |
| `deploy/server/bot.env.example` | 服务器机器人配置模板，复制成 `/opt/cs/.env` |
| `deploy/server/Dockerfile.backend-only` | 不依赖独立客服页面的构建文件，同步或解压后复制成服务器 `/opt/cs/Dockerfile` |
| `deploy/server/nginx-bot.conf.example` | 宿主机 Nginx 站点模板，限制公网可访问接口 |

操作路线：**核对资源 → Git 同步后端并上传模型（或分包上传）→ 填配置 → 构建镜像 → 初始化 FAQ 和向量库 → 启动并本地验收 → 配内网入口 → 绑定 Chatwoot → 在已有前端验收 → 备份。**

### 0.1 从你当前的进度继续

| 顺序 | 本次执行内容 | 对应章节 / 完成标志 |
| --- | --- | --- |
| 1 | Windows 执行 `ssh ics`，核对资源、Docker，进入 tmux | 第 2～3 节；已有 Docker 不重复安装 |
| 2 | 将当前版本提交、推送；给服务器配置 Git 只读权限并 clone | 第 4.6 节第一～三步；服务器与本机提交 SHA 一致 |
| 3 | 同步后端、首次复制业务文件 | 第 4.6 节第四～五步；不覆盖已有运行数据 |
| 4 | 解压已经上传的模型包 | 第 4.6 节第六步；跳过打包上传命令 |
| 5 | 创建并填写服务器 `.env` | 第 5 节；地址、密码、Token 和业务参数已核对 |
| 6 | 构建镜像、检查配置和模型、启动机器人数据库 | 第 7 节；检查 PASS，数据库 healthy |
| 7 | 导入 FAQ、解析知识库并建向量库 | 第 8 节；导入及建库成功 |
| 8 | 启动应用，验证后台和 FAQ/RAG | 第 9 节；不仅检查 `/health` |
| 9 | 绑定服务器私网地址，验证 Chatwoot 到机器人的网络 | 第 10B 节；跳过没有域名时不适用的第 10A 节 |
| 10 | 配置 AgentBot、启用自动回复并绑定测试 Inbox | 第 11 节；已有前端能收到机器人消息 |
| 11 | 验收故障兜底、咨询边界、人工接管，备份 | 第 12、14、17 节 |

### 0.2 当前版本与旧手册的主要差异

服务器模板现已与根 `.env.example` 的业务参数对齐：封禁 1 天、会话记忆 15 天、Webhook 并发 10、重排并发 4、LLM 并发 8、数据库连接池 12、获取连接等待 1 秒、LLM 单次超时 10 秒、Webhook 查询总预算 30 秒。完整配置表及资源不足时的调整见第 5.7 节。

国内网络构建默认使用清华普通 PyPI 镜像和南京大学 CPU PyTorch 镜像，并保留 pip 下载缓存、超时和重试。CPU 下载也使用国内来源；上海交大 CPU 镜像作为备用。已经卡在 `pip install` 的服务器直接按第 7.1A 节继续；无需重新上传模型包。

`deploy/server/bot.env.example` 是本手册唯一的服务器起始模板，根 `.env.example` 是通用示例；二者都不是实际密钥文件。旧版服务器 `.env` 不会自动更新，已经配置过的服务器需按第 5.7、13.1 节逐项修改。

## 1. 准备部署信息

先把下表填好；密码和 Token 保存在自己的密码管理器中，不要粘贴到截图或公开工单。

| 信息 | 示例或获取位置 |
| --- | --- |
| 机器人服务器 SSH 地址 | Windows 已配置别名 `ics`，可直接 `ssh ics`；地址由该 SSH 配置提供 |
| Git 仓库地址、部署分支 | 开发端运行 `git remote get-url origin` / `git branch --show-current`；本轮分支是 `test` |
| SSH 用户 | 你当前为 `ubuntu` |
| SSH 端口、私钥 | 云主机当前可用的登录方式；默认端口通常是 22 |
| 机器人公网域名（可选） | `bot.example.com`；没有域名看第 10B 节 |
| 已有 Chatwoot 根地址 | 已确认为 `http://172.20.48.12:3000`，不要带 `/app` 或 `/api` |
| Chatwoot Account ID | 打开后台，地址通常含 `/app/accounts/数字/...` |
| Chatwoot 用户 API Token | 账户头像 → 个人资料 / Profile Settings → Access Token；使用该账户有管理权限的用户 |
| 测试 Inbox ID、Website Token | Chatwoot 设置 → 收件箱 / Inboxes → 测试 Website Inbox |
| 云端模型名称、API Base URL、API Key | 使用你本机已经调用成功的一组配置，不直接照抄模板中的示例模型 |
| 云安全组 / 主机网络权限 | 允许 Chatwoot 所在主机或容器访问机器人私网端口；本次无需 DNS |

需要区分五种信息：

| 名称 | 放在哪里 | 用途 |
| --- | --- | --- |
| 用户 API Token | `CHATWOOT_API_TOKEN` | 查坐席、修改状态、转人工和联系人操作 |
| AgentBot Access Token | `CHATWOOT_AGENT_BOT_ACCESS_TOKEN` | 以机器人身份发送回复 |
| AgentBot Secret | `CHATWOOT_AGENT_BOT_SECRET` | 验证 Chatwoot 发来的 Webhook 签名 |
| Website Token | 已部署前端的配置或页面参数 | 浏览器加载指定 Inbox 的 Widget；不是后台 API 密钥 |
| Website HMAC Token | 后端 `CHATWOOT_HMAC_TOKEN` | 已登录用户身份签名；匿名首测可暂不配置 |

例如 Chatwoot 后台地址为 `http://172.20.48.12:3000/app/accounts/1/dashboard`，则根地址是 `http://172.20.48.12:3000`，Account ID 是 `1`。这里的 `1` 仅举例，不要在未核对时当作真实账号 ID。Inbox ID 从“设置 → 收件箱”打开目标 Inbox 的 URL 或第 11.3 节 API 获取。

## 2. 登录 Ubuntu，检查资源和已有服务

### 2.1 重新连接 SSH

你看到的 `client_loop: send disconnect: Connection reset` 表示 SSH 连接中断，不能据此判断机器人或服务器故障。先重新连接。

在 **Windows PowerShell** 执行：

```powershell
ssh -o ServerAliveInterval=30 -o ServerAliveCountMax=3 ics
```

`ics` 会沿用你 SSH 配置中的主机地址、用户名、端口和私钥；本文 Windows 侧的上传、隧道也使用它。这个别名仅代表“Windows 登录服务器”，服务器访问 Git 仓库的权限仍需按第 4.6 节单独配置。

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

## 4. 准备部署文件：Git 同步或分包上传

两种方式选一种：

- **推荐：Git + 模型包。** 你已有校验成功的模型包，直接从第 4.6 节开始，跳过第 4.1～4.5 节。如果模型更新才重新执行第 4.2 节；无需制作 `cs-app.tar.gz`。
- **备用：两个压缩包。** 按第 4.1～4.5 节操作，完成后直接进入第 5 节。适合服务器无法访问远端仓库的情况。

内网部署不妨碍使用 Git，前提是服务器能访问仓库所在地址，并具备读取权限；没有公网入站地址和不能访问外部仓库是两件不同的事。

### 4.1 两个包各包含什么

压缩包以本机当前工作目录为准；Git 只能取得已提交并推送到目标分支的版本。选择 Git 时，先按第 4.6 节确认部署模板和所需修复已发布到远端。

| 压缩包 | 包含内容 | 何时上传 |
| --- | --- | --- |
| `D:\cs-models.tar.gz` | 整个 `models/`，包含检索、重排、BERT 和已训练意图模型；目录原始大小约 8.38 GiB | 首次部署上传；模型变化后才重新打包上传 |
| `D:\cs-app.tar.gz` | 后端源码、依赖与 Compose 配置、部署模板、文档、FAQ Excel、意图 CSV 和原始知识库 | 首次部署上传；后续更新后端时主要更新此包 |

两个包均不包含本机 `.env`、Anaconda 环境、历史日志、`.git`、现有向量库或已部署的独立前端。`support-chat.html` 和 `chatwoot-chat.html` 都不打包。

后端包携带 `deploy/server/Dockerfile.backend-only`。服务器解压后把它复制成根目录 `Dockerfile`，它不再要求 `COPY support-chat.html`，因此不上传前端也能构建。**本机原 `Dockerfile` 不需要修改。** 应用内管理后台仍随 `ics_agent/` 保留，SSH 隧道管理方式不受影响。

两个包最终都解压到同一个 `/opt/cs`，目录应为：

```text
/opt/cs/
├── Dockerfile                        解压后由后端构建模板复制生成
├── docker-compose.yml
├── .dockerignore
├── requirements.txt
├── .env.example
├── ics_agent/                        后端包：完整应用代码
├── knowledge_base_in/                后端包：解析和向量化代码
├── deploy/server/                    后端包：配置模板、Dockerfile.backend-only
├── docs/                             后端包：本手册
├── models/                           模型包：仅此目录
│   ├── bge-m3/
│   ├── bge-reranker-v2-m3/
│   ├── google-bert--bert-base-chinese/snapshots/master/
│   └── intent_classifier_bert/
└── database/                         后端包：首次初始化用的业务数据
    ├── 最新问答对.xlsx
    ├── intent_dataset_4000.csv
    └── knowledge_base/
```

首次部署在服务器从知识库原文件重建向量库，不复制正在使用的 `knowledge_base_in/vector_store`。`models/` 被 Git 忽略且不进入镜像，运行时依靠挂载；只上传后端包无法运行完整检索链路。

### 4.2 第一次打包：只打包模型

在 **Windows PowerShell** 执行，不是在 Ubuntu 执行：

```powershell
Set-Location D:\project\CS
tar.exe -h -czf D:\cs-models.tar.gz models
if ($LASTEXITCODE -ne 0) { throw '模型包打包失败，请检查上方错误。' }
Get-FileHash D:\cs-models.tar.gz -Algorithm SHA256
```

记录模型包的 SHA256。`-h` 将模型符号链接指向的实际内容打入包，避免迁移后链接失效。模型很大，打包可能持续较长时间；不要在训练或下载模型还未完成时打包。

### 4.3 第二次打包：后端源码和首次业务数据

先保存并关闭正在编辑的 FAQ Excel，再在 **Windows PowerShell** 执行：

```powershell
Set-Location D:\project\CS
tar.exe -h -czf D:\cs-app.tar.gz `
  --exclude=__pycache__ `
  --exclude=*.pyc `
  --exclude=knowledge_base_in/output `
  --exclude=knowledge_base_in/vector_store `
  docker-compose.yml .dockerignore requirements.txt .env.example `
  ics_agent knowledge_base_in deploy docs `
  database/最新问答对.xlsx database/intent_dataset_4000.csv database/knowledge_base
if ($LASTEXITCODE -ne 0) { throw '后端包打包失败，请检查上方错误。' }
Get-FileHash D:\cs-app.tar.gz -Algorithm SHA256
```

记录后端包自己的 SHA256，不能与模型包的哈希混用。此命令没有 `models`，也没有根目录 `Dockerfile` 或客服 HTML 页面；对应构建文件由 `deploy/` 中的模板提供。两个压缩包均放在 `D:\`，不会把压缩包自身再次打进去。

首次这两个包应来自同一份工作区：模型包中的意图分类器与后端包中的 `intent_dataset_4000.csv` 必须配套。两次打包之间不要更换意图模型或训练 CSV。

### 4.4 分别上传两个包

在 **Windows PowerShell** 执行，沿用已配置的 SSH 别名 `ics`：

```powershell
scp -o ServerAliveInterval=30 D:\cs-models.tar.gz ics:~/
if ($LASTEXITCODE -ne 0) { throw '模型包上传失败，请检查网络或使用续传。' }
```

第一份上传成功后，再上传后端包：

```powershell
scp -o ServerAliveInterval=30 D:\cs-app.tar.gz ics:~/
if ($LASTEXITCODE -ne 0) { throw '后端包上传失败，请检查网络或使用续传。' }
```

连接容易断时，可用 WinSCP 的 SFTP 分别上传这两个文件并续传。已经成功传完且校验一致的模型包不必因为后端包上传失败而重传。

### 4.5 在 Ubuntu 分别校验、解压

回到 Ubuntu 的 tmux 会话，分别计算两个文件的哈希：

```bash
sha256sum ~/cs-models.tar.gz
sha256sum ~/cs-app.tar.gz
```

两行分别与 Windows 对应文件的 SHA256 一致，才继续。首次部署 `/opt/cs` 应是新目录；如果已有另一套运行服务，先改用独立路径，不要直接覆盖。

```bash
sudo mkdir -p /opt/cs
sudo chown ubuntu:ubuntu /opt/cs
cd /opt/cs
tar -xzf ~/cs-models.tar.gz
tar -xzf ~/cs-app.tar.gz
cp deploy/server/Dockerfile.backend-only Dockerfile
mkdir -p log knowledge_base_in/output knowledge_base_in/vector_store
ls -la
du -sh models database
df -h /
```

**两个包都在 `/opt/cs` 下解压，不要进入 `/opt/cs/models` 再解模型包**，否则会形成错误的 `models/models` 目录。`cp` 构建模板这一步也不能省略，否则根目录没有本次需要的 Dockerfile，或仍使用原先依赖前端页面的版本。

检查文件：

```bash
test -f Dockerfile && echo 'Dockerfile OK'
cmp -s deploy/server/Dockerfile.backend-only Dockerfile && echo 'backend-only Dockerfile OK'
test -f ics_agent/retry/reconnect.py && echo 'retry code OK'
test -f ics_agent/app/chatwoot/message_format.py && echo 'image formatting code OK'
test -f models/intent_classifier_bert/model.safetensors && echo 'intent model OK'
test -f models/intent_classifier_bert/labels.json && echo 'intent metadata OK'
test -f database/intent_dataset_4000.csv && echo 'intent dataset OK'
test -f 'database/最新问答对.xlsx' && echo 'FAQ Excel OK'
find models -type l -print
```

如果仍有符号链接，确认不是悬空链接。不要只传模型的 `config.json` 而漏掉权重文件。这里不再检查 `support-chat.html`，因为本次不部署该页面。

校验、解压和文件检查均成功后，如需释放系统盘空间，可以分别删除已解压的上传包；Windows 原包仍保留：

```bash
rm -i ~/cs-models.tar.gz
rm -i ~/cs-app.tar.gz
```

以后只改后端代码时，跳过模型打包上传，按第 13.2 节更新后端包。更换或重新训练模型时才更新模型包；意图模型变化还要同步配套 CSV。

### 4.6 推荐方式：Git 同步后端，仅上传模型包

#### 第一步：明确 Git 同步范围

| 内容 | 当前仓库情况 | 服务器处理方式 |
| --- | --- | --- |
| 后端源码、依赖、Compose、配置模板 | 通过 Git 管理 | 首次 clone，后续 pull 后同步到运行目录 |
| `models/` | 已被 Git 忽略，约 8.38 GiB | 首次或模型更新时单独上传模型包 |
| `.env` | 已被 Git 忽略 | 第 5 节在服务器创建，后续保留服务器值 |
| FAQ Excel、意图 CSV、`database/knowledge_base/` | 已纳入 Git | 首次复制初始化；日常代码更新不覆盖 |
| `knowledge_base_in/output/` | 当前已有分块文件纳入 Git | 不复制这些历史产物，第 8 节在服务器重建 |
| 向量库、运行日志、数据库卷 | 运行数据 | 不通过 Git 更新 |

Git 拉取可能同时取得仓库中已跟踪的前端 HTML，但下列同步命令不将它们复制到运行目录或镜像，也不会更新你已经部署的前端。

本方案使用两个目录：`/opt/cs-src` 是保持干净的 Git 仓库；`/opt/cs` 是实际运行目录。这样既能保留后台修改的业务文件，又不必在 Git 仓库中覆盖根目录 Dockerfile。不要在 `/opt/cs-src` 启动 Compose，也不要把整个仓库无差别覆盖到 `/opt/cs`。

#### 第二步：在 Windows 确认并发布部署版本

在 **Windows PowerShell** 检查：

```powershell
Set-Location D:\project\CS
git status --short
git branch --show-current
git remote get-url origin
git diff --stat
```

本次核对的本地分支是 **`test`**，已不同于旧手册的分支。尚未联网验证远端分支；下面用 `YOUR_DEPLOY_BRANCH` 占位，沿用当前分支就替换成 `test`。仓库地址从 Git 平台“克隆”入口取得，或查看 `git remote get-url origin`；不要把访问令牌直接写进地址。

**先提交并推送当前代码、最新手册和服务器配置模板。** `.gitattributes` 和 `Dockerfile.backend-only` 已纳入本地仓库，但本轮模板修订仍需发布；只在本机保存文件，服务器无法取得它们。确认改动内容后，可在 Windows 暂存部署相关文件：

```powershell
git add -- .gitattributes Dockerfile docker-compose.yml requirements.txt README.md docs/server-first-deployment.md deploy/server/Dockerfile.backend-only deploy/server/bot.env.example deploy/server/nginx-bot.conf.example
if ($LASTEXITCODE -ne 0) { throw '暂存失败，请检查。' }
git diff --cached --stat
```

核对暂存区只包含你准备发布的改动；如果原来已有其他暂存文件，先处理好它们，再提交：

```powershell
git commit -m "docs: align server deployment with current configuration"
if ($LASTEXITCODE -ne 0) { throw '提交失败，请检查；若文件已提交则无需重复提交。' }
git push -u origin YOUR_DEPLOY_BRANCH
if ($LASTEXITCODE -ne 0) { throw '推送失败，请先解决远端分支或权限问题。' }
git rev-parse HEAD
```

`YOUR_DEPLOY_BRANCH` 替换为当前准备发布的真实分支；沿用本次分支时填 `test`。如果还有需要部署的业务代码未提交，也要一并核对并发布。已经提交过的版本不必重复 commit，直接检查 push 结果。推送成功后记录最后显示的完整提交 SHA，稍后与服务器比对。

#### 第三步：Ubuntu 安装工具并克隆仓库

先在 **Ubuntu SSH 终端**安装工具：

```bash
sudo apt-get update
sudo apt-get install -y git rsync
```

**私有仓库推荐配置独立的只读部署密钥。** 可以使用你已有 Git 平台账号给仓库添加权限，但服务器不会继承开发电脑的 Git 登录状态，`ssh ics` 也不能替代这一步。不要复制开发电脑上的私钥。

在 Ubuntu 创建独立密钥目录并生成密钥：

```bash
install -d -m 700 ~/.ssh
ssh-keygen -t ed25519 -C 'ics-server-readonly' -f ~/.ssh/ics_repo_ed25519
```

若提示同名文件已存在，先选择不覆盖并核对已有密钥。新建时按提示选择口令；无口令便于部署机非交互拉取，有口令则拉取前需解锁。查看并复制**公钥**：

```bash
cat ~/.ssh/ics_repo_ed25519.pub
```

在 Git 平台打开“目标仓库 → 设置 → Deploy Keys / 部署公钥”，添加上述整行公钥，**只启用读取权限**。各平台入口名称可能不同；如果只支持账号级 SSH 公钥，该账号本身必须有仓库读取权限。私钥文件 `ics_repo_ed25519` 不上传。

编辑服务器的 SSH 配置，不覆盖其中已有内容：

```bash
touch ~/.ssh/config
chmod 600 ~/.ssh/config
nano ~/.ssh/config
```

在文件末尾添加，`YOUR_GIT_HOST` 换成仓库域名，例如你的 GitHub、Gitee、GitLab 或内网 Git 服务地址。自建服务用户名、端口以平台提供的 SSH 克隆地址为准：

```sshconfig
Host ics-repo
    HostName YOUR_GIT_HOST
    User git
    IdentityFile ~/.ssh/ics_repo_ed25519
    IdentitiesOnly yes
```

保存后验证：

```bash
ssh -T ics-repo
git ls-remote --exit-code YOUR_REPOSITORY_URL refs/heads/YOUR_DEPLOY_BRANCH
```

这里 `YOUR_REPOSITORY_URL` 使用别名地址，例如平台原地址为 `git@YOUR_GIT_HOST:团队/仓库.git`，则填写 `git@ics-repo:团队/仓库.git`。两个地方的分支一致；首次连接先核对平台公布的主机指纹，再接受。部分 Git 平台的 `ssh -T` 会提示不提供 shell 并返回非零码，这是正常的；以 `git ls-remote` 能返回目标分支的 SHA 为读取成功依据。

若使用 HTTPS，可跳过 SSH 密钥部分，使用平台提供的 HTTPS 克隆地址，并在认证提示中提供账号和具有仓库读取权限的访问令牌；令牌不写进克隆 URL。令牌过期后需重新认证。仓库无法访问或权限未通过时，先解决网络/权限，或采用分包方式。

确认有读取权限后创建部署目录。以下仅适用于首次部署：`/opt/cs-src` 应尚未存在或是空目录，`/opt/cs` 应为新建空目录。如果已有服务在 `/opt/cs` 运行，按第 13.2A 节更新，不要重复初始化数据。

```bash
sudo mkdir -p /opt/cs /opt/cs-src
sudo chown ubuntu:ubuntu /opt/cs /opt/cs-src
git clone --branch YOUR_DEPLOY_BRANCH --single-branch YOUR_REPOSITORY_URL /opt/cs-src
```

替换真实分支和仓库地址，确认 clone 成功再继续。若提示 `Permission denied (publickey)`，核对密钥和仓库权限；若提示目录非空，不要直接删除，先确认是不是已有 checkout，转到第 13.2A 节。

```bash
git -C /opt/cs-src status --short
git -C /opt/cs-src rev-parse HEAD
git -C /opt/cs-src ls-files --eol -- database/intent_dataset_4000.csv
test -f /opt/cs-src/deploy/server/Dockerfile.backend-only && echo 'backend build template OK'
test -f /opt/cs-src/deploy/server/bot.env.example && echo 'server env template OK'
```

状态应为空，SHA 应与 Windows 准备部署的提交一致，CSV 应显示 `w/crlf` 和 `eol=crlf`，两个文件检查均应显示 OK；否则先核对分支、推送状态和缺失文件。

当前意图模型按 CSV 的原始字节校验指纹，换行符也参与校验。根目录 `.gitattributes` 已将这个 CSV 的检出换行固定为 CRLF，让 Windows 和 Linux 取得与现有模型一致的内容；仓库内部存为 LF 是正常的。必须把这条规则与部署版本一起推送，不要通过关闭指纹校验绕过问题。第 9.4 节还会检查模型与实际运行 CSV 是否配套。

#### 第四步：仅同步运行所需的代码与模板

首次部署和以后更新都使用下面这一段。它只同步指定目录，并排除知识库生成产物：

```bash
(
  set -e
  test -f /opt/cs-src/deploy/server/Dockerfile.backend-only
  mkdir -p /opt/cs/ics_agent /opt/cs/knowledge_base_in /opt/cs/deploy /opt/cs/docs
  rsync -a --delete --exclude='__pycache__/' --exclude='*.pyc' \
    /opt/cs-src/ics_agent/ /opt/cs/ics_agent/
  rsync -a --delete --exclude='/output/' --exclude='/vector_store/' \
    --exclude='__pycache__/' --exclude='*.pyc' \
    /opt/cs-src/knowledge_base_in/ /opt/cs/knowledge_base_in/
  rsync -a /opt/cs-src/deploy/ /opt/cs/deploy/
  rsync -a /opt/cs-src/docs/ /opt/cs/docs/
  cp /opt/cs-src/docker-compose.yml /opt/cs-src/.dockerignore \
    /opt/cs-src/requirements.txt /opt/cs-src/.env.example /opt/cs/
  cp /opt/cs-src/deploy/server/Dockerfile.backend-only /opt/cs/Dockerfile
)
```

整段无报错、退出码为 0 后继续。`--delete` 只用于两个代码目录，清掉新版已移除的旧代码；被排除的 `output/` 和 `vector_store/` 会保留。不要加入 `--delete-excluded`，也不要把同步目标改成整个 `/opt/cs`。运行目录内的代码和模板视为可替换文件，定制代码应先在开发端修改并提交；实际配置修改在 `/opt/cs/.env` 中进行。

#### 第五步：首次复制业务数据

仅在第一次部署时执行。下面只复制必需的初始资料，并保留目标中已经存在的文件：

```bash
(
  set -e
  mkdir -p /opt/cs/database/knowledge_base /opt/cs/log \
    /opt/cs/knowledge_base_in/output /opt/cs/knowledge_base_in/vector_store
  cp -n '/opt/cs-src/database/最新问答对.xlsx' /opt/cs/database/
  cp -n /opt/cs-src/database/intent_dataset_4000.csv /opt/cs/database/
  rsync -a --ignore-existing /opt/cs-src/database/knowledge_base/ /opt/cs/database/knowledge_base/
)
```

后续更新代码不再执行这一步。FAQ 和知识库通过第 13.3、13.4 节维护；意图 CSV 要与模型包中的训练指纹一致。如果复用旧的源码 checkout，仅拉取新增 `.gitattributes` 可能不会重写既有 CSV；应先确认仓库无本地改动，再用 `git -C /opt/cs-src checkout-index --force -- database/intent_dataset_4000.csv` 按新属性重新检出，确认显示 `w/crlf` 后再做首次复制。此命令只用于可从 Git 恢复的源码副本，不用于服务器后台维护的数据。

#### 第六步：上传模型并进入常规部署

**你已完成模型包上传与校验，本次模型未变更时跳过下面的上传命令，直接解压。** 第一次从其他电脑部署或重新更换模型时，才按第 4.2 节制作模型包，并在 **Windows PowerShell** 上传：

```powershell
scp -o ServerAliveInterval=30 D:\cs-models.tar.gz ics:~/
if ($LASTEXITCODE -ne 0) { throw '模型包上传失败，请检查网络或使用续传。' }
```

在 Ubuntu 校验：

```bash
sha256sum ~/cs-models.tar.gz
```

与 Windows 的模型包 SHA256 一致后，解压到运行目录：

```bash
cd /opt/cs
tar -xzf ~/cs-models.tar.gz
cmp -s deploy/server/Dockerfile.backend-only Dockerfile && echo 'backend-only Dockerfile OK'
test -f models/intent_classifier_bert/model.safetensors && echo 'intent model OK'
test -f models/intent_classifier_bert/labels.json && echo 'intent metadata OK'
test -f database/intent_dataset_4000.csv && echo 'intent dataset OK'
test -f 'database/最新问答对.xlsx' && echo 'FAQ Excel OK'
find models -type l -print
du -sh models database
df -h /
```

确认解压无报错，各检查显示 OK，模型不存在失效链接。若服务器已经拥有同一版本且完整的模型目录，可跳过模型上传与解压。成功后可用 `rm -i ~/cs-models.tar.gz` 释放上传包占用的空间，保留 Windows 原包。

**到这里无需 `cs-app.tar.gz`，直接继续第 5～12 节。** clone/pull 本身不会安装依赖、构建镜像、初始化数据库或重启机器人，仍需完成后续步骤。

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

使用十六进制随机值能避免 `.env` 中 `$`、`#`、空格等特殊字符的转义问题。当前服务器模板使用 `ADMIN_USERNAME=Denpans`，与代码默认一致；它与 Chatwoot 登录账号没有关系。若旧部署已经用 `csadmin` 建立管理员，应保留并使用原账号；不要以为更新模板会自动修改已有账号或密码。

### 5.3 填写已有 Chatwoot 地址和用户 API Token

```dotenv
CHATWOOT_BASE_URL=http://172.20.48.12:3000
CHATWOOT_PUBLIC_BASE_URL=http://172.20.48.12:3000
CHATWOOT_ACCOUNT_ID=实际Account数字ID
CHATWOOT_API_TOKEN=实际用户API令牌
CHATWOOT_AUTO_REPLY_ENABLED=false
```

- `CHATWOOT_BASE_URL` 要能从机器人容器访问，用于后台 API 调用。
- `CHATWOOT_PUBLIC_BASE_URL` 要能从浏览器访问，用于加载 Widget。
- 你的内网部署两者都填 `http://172.20.48.12:3000`。`PUBLIC_BASE_URL` 指浏览器访问地址，不要求是公网域名；复用的已有前端也应连接这同一套 Chatwoot。
- 不要照搬 Windows Docker Desktop 的 `host.docker.internal`：Ubuntu 默认不提供同样的解析，当前 Compose 也没有配置它。
- 不要在容器里用 `http://127.0.0.1:3000` 指代另一台服务器或另一个容器的 Chatwoot。
- `CHATWOOT_CONTACT_API_TOKEN` 留空时，联系人操作回退使用用户级 `CHATWOOT_API_TOKEN`。
- AgentBot ID 和 Access Token 先留空；Secret 先填刚生成的随机值，第 11 节用 Chatwoot 返回的真实 Secret 替换。两个签名 Secret 均为空时，当前程序会跳过签名校验，因此不能将它们同时留空后公开 Webhook。

### 5.4 填写大模型配置

```dotenv
LLM_MODEL=gpt-5.6-sol
LLM_MODEL_PROVIDER=openai
LLM_BASE_URL=https://ai-sfo.apiveer.com/v1
LLM_API_KEY=你的模型API密钥
LLM_MODEL2=deepseek-flash
LLM_MODEL_PROVIDER2=openai
LLM_BASE_URL2=https://备用模型服务地址/v1
LLM_API_KEY2=你的备用模型API密钥
```

主模型名称和 Base URL 来自根 `.env.example`；备用模型地址及两组 API Key 需自行填写。主备模型分别核对模型名称、提供商、地址和密钥。`LLM_MODEL_PROVIDER2` 留空时沿用主模型提供商，备用地址和密钥不会自动复用主模型值。本项目可接 OpenAI 兼容接口；是否带 `/v1` 以实际服务商为准，不自行拼接 `/chat/completions` 到 Base URL。

当前设置是 **主模型最多等待 10 秒、Webhook 查询阶段总预算 30 秒**。主模型超时、异常或空回复后立即调用备用模型，备用模型使用同一次查询的剩余预算，切换时不重新计满 30 秒。排队与回复写回的时间边界见第 5.7、12.5 节。

### 5.5 数据库配置保持一致

```dotenv
POSTGRES_DB=cs
POSTGRES_DATABASE=cs
POSTGRES_USER=cs
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_HOST_PORT=5433
POSTGRES_DSN=
POSTGRES_POOL_SIZE=12
POSTGRES_POOL_ACQUIRE_TIMEOUT=1.0
ADMIN_WEB_HOST=127.0.0.1
ADMIN_WEB_PORT=3001
```

- `POSTGRES_DB` 创建数据库，`POSTGRES_DATABASE` 告诉应用连接哪个数据库，两者必须相同。
- 容器内连接 `postgres:5432`；宿主机映射为 `127.0.0.1:5433`。端口 5433 已占用时可改 `POSTGRES_HOST_PORT=5434`，应用内部仍使用 5432。
- 保持 `POSTGRES_DSN` 为空，避免旧连接串覆盖这些配置。
- 不要把这里填成 Chatwoot 正在使用的数据库。
- 连接池显式设置为 12，获取连接最多等待 1 秒；不要漏掉这两项，否则代码默认等待仅 0.05 秒，冷启动或负载下更容易获取连接失败。池大小不是整个 PostgreSQL 实例的总连接上限，会话记忆还使用独立的异步连接池。

### 5.6 数据保留设置

当前服务器模板与根 `.env.example` 均使用 `RAG_MEMORY_RETENTION_DAYS=15`；代码本身省略该项时仍默认 `0`（永久保留），所以服务器明确写入 15。静音 / 封禁时长为 `CHATWOOT_BLOCK_DURATION_SECONDS=86400`（1 天），Webhook 幂等记录保留 7 天。

| 数据 / 状态 | 当前清理方式 |
| --- | --- |
| 机器人会话记忆 | 根据咨询线程最后活动时间清理闲置超过 15 天的线程和检查点；启动时尝试清理，此后每 24 小时一次，每批最多 500 个 |
| Webhook 幂等记录 | 每 24 小时清理 `completed` / `failed` 且超过 7 天的记录，每批最多 1000 条；不是对全部事件状态无条件删除 |
| 联系人静音 / 封禁 | 收到对应事件并记录后，按 1 天期限由后台任务尝试解除；轮询默认 60 秒，依赖 Chatwoot API 可用及第 11.6 节事件配置 |
| 应用日志 | stdout / stderr 各按 30 MiB 轮转，最多各保留 5 份历史文件；Docker 自身日志另有轮转 |

清理任务依赖服务持续运行和数据库可用，达到天数不代表当秒删除；批次上限也可能产生待清理积压。FAQ、原始知识库、模型和 Chatwoot 数据不属于这些自动清理范围。

“已解决”和“用户 6 小时无动作”用于开启新上下文，不会立即物理删除旧记忆，也不会删除 Chatwoot 聊天记录。

### 5.7 核对当前并发、超时与服务器专用项

无需重复追加同名配置；模板已有以下字段时，直接核对或修改原行：

| 配置项 | 当前服务器模板值 | 作用 |
| --- | --- | --- |
| `WEBHOOK_WORKER_COUNT` | `10` | 单进程内 Webhook 任务并发，不是 10 个 Uvicorn 进程 |
| `RAG_RERANKER_ENABLED` | `false` | 关闭精排，直接使用双塔 Top5；`true` 沿用原召回与精排策略 |
| `RAG_RERANKER_MAX_CONCURRENCY` | `4` | 启用精排时的重排并发上限 |
| `LLM_MAX_CONCURRENCY` | `8` | 共享云模型调用并发上限 |
| `LLM_CONCURRENCY_WAIT_TIMEOUT` | `15` | 等待模型并发槽位的最长秒数，也会消耗查询预算 |
| `POSTGRES_POOL_SIZE` / `POSTGRES_POOL_ACQUIRE_TIMEOUT` | `12` / `1.0` | 同步数据库连接池容量 / 获取连接等待秒数 |
| `LLM_TIMEOUT` / `LLM_MAX_RETRIES` | `10` / `2` | 普通工厂调用的 SDK 超时 / 重试默认值；回复节点主模型最多 10 秒，主备模型均显式禁用 SDK 重试 |
| `WEBHOOK_RAG_TIMEOUT_SECONDS` | `30` | worker 开始查询后，FAQ、记忆、检索、重排与生成的总预算 |
| `WEBHOOK_PROCESSING_TIMEOUT_SECONDS` | `300` | worker 每次任务处理的外层时限，包含查询以外的 Chatwoot 操作 |
| `WEBHOOK_MAX_RETRIES` | `3` | 未被查询兜底吸收的任务异常，最多额外重试 3 次 |
| `CHATWOOT_SESSION_IDLE_TIMEOUT_SECONDS` | `21600` | 用户消息间隔达到 6 小时，下一条消息开启新上下文 |

关闭精排时不加载或预热 reranker 模型，固定使用双塔 Top5，现有 `RAG_RERANKER_*` 调参仅在重新启用精排时生效。未填写 `RAG_RERANKER_ENABLED` 的旧环境默认 `true`，保持原链路；切换后需重新创建 `ics-agent` 容器，使新环境变量生效。

在线 RAG 的 Query 向量化现在使用微批处理：`KNOWLEDGE_BASE_EMBEDDING_MICRO_BATCH_WAIT_MS=20` 最多等待 20 毫秒，`KNOWLEDGE_BASE_EMBEDDING_MICRO_BATCH_MAX_SIZE=4` 最多合并 4 个 Query，然后一次调用 BGE-M3 并按原请求分别继续检索。它只合并短时间内到达的 embedding 请求，不合并不同用户的最终回复；同一会话仍按原顺序处理。

为降低首次部署的积压和批次内存占用，服务器模板还显式使用 `WEBHOOK_QUEUE_MAX_SIZE=100`、`KNOWLEDGE_BASE_EMBEDDING_BATCH_SIZE=2`、`RAG_RERANKER_BATCH_SIZE=2`；代码默认分别为 1000、8、8。这三项属于部署取值，与上表当前业务超时和并发基线分开记录。

当前并发基线还没有在你目标服务器上压测。如果 CPU / 内存紧张，可先在服务器 `.env` 中将 `WEBHOOK_WORKER_COUNT=2`、`RAG_RERANKER_MAX_CONCURRENCY=1`、`LLM_MAX_CONCURRENCY=2`，其他超时和保留策略保持当前值，低并发验收成功后再逐步调整；并发降低不能代替满足模型加载内存需求。

服务器始终保留 `APP_ENV=production`、`CHATWOOT_PROFILE_ALLOW_QUERY_IDENTIFIERS=false`、准备阶段的 `CHATWOOT_AUTO_REPLY_ENABLED=false`。不要把本地用于调试的 `.env` 整份覆盖过来。`CHATWOOT_PROFILE_ALLOWED_ORIGINS` 仅在需要浏览器访问本机器人身份接口时填写真实前端来源；现有前端只与 Chatwoot 通信时无需启用该接口。

模板的 `ADMIN_WEB_HOST=127.0.0.1` 用于初始本机检查，第 10B 节才切到机器人实际私网 IP。模板里的 Chatwoot 地址是 3000，机器人对外端口是 3001，数据库宿主机端口是 5433，不要混用。

## 6. 复用已有前端，不上传客服页面

这次部署只更换或新增机器人后端，使用以下链路：

```text
已经部署的前端 → 已有内网 Chatwoot → 新部署的机器人 Webhook
```

先核对已有前端实际连接的 Chatwoot 根地址和 Website Token。如果它连接的就是 `http://172.20.48.12:3000` 和你用于测试的 Inbox，这两项均无需修改，后面只调整该 Inbox 的 AgentBot 绑定和 Webhook 目标地址。

如果“开发环境 Chatwoot”实际上是另一套实例，应先确认前端、机器人 `.env`、Account ID 和 Inbox 都对应同一套 Chatwoot；只启动机器人不会自动把前端转接到另一套 Chatwoot。

第 4 节同步或解压时复制的 `Dockerfile.backend-only` 不要求客服 HTML 文件。此镜像没有内置的 `/support-chat` 页面，访问该路径出现 404 是预期行为，不表示机器人部署失败。管理后台 `/admin` 仍然可用。

本次无需修改、打包或上传 `support-chat.html` / `chatwoot-chat.html`，也无需重新发布现有前端。只要使用原来的 Inbox，Website Token 不变；如果为了隔离测试另外新建 Inbox，则需要在现有前端支持的测试配置中选择新 Token。

已有 APP 身份接口也保持原有部署；不要把它盲目切换为本次机器人的 `/chatwoot/profile`。若还未接入真实登录身份，可先用已有前端的匿名模式验收，保持 `CHATWOOT_PROFILE_ALLOW_QUERY_IDENTIFIERS=false`。身份接入边界见第 15 节。

## 7. 构建镜像，启动机器人数据库

### 7.1 校验 Compose 并构建后端镜像

本次服务器模板已包含以下构建参数；如果 `.env` 是旧版创建的，先按第 7.1A 节补齐：

```dotenv
BUILD_PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
BUILD_TORCH_INDEX_URL=https://mirrors.nju.edu.cn/pytorch/whl/cpu
BUILD_PIP_TIMEOUT=120
BUILD_PIP_RETRIES=8
```

这四项由 Compose 的 `build.args` 传给镜像构建，修改后必须重新 `build`；它们不会修改 Windows `cs` 环境的 pip 配置。仅执行 `restart` 或把它们放入没有对应 `build.args` 的旧 Compose 文件，不会改变依赖下载方式。

2026-10-08 已核对南大和交大镜像：均列出了 `torch-2.6.0+cpu-cp310-cp310-linux_x86_64.whl`，并能以 HTTP 206 返回真实 wheel 文件的开头；文件约 170 MiB，实际下载没有跳回官方站点。此项验证只确认所需版本及下载入口可用，不代表已经在你的服务器完成整包下载或测速。

若南大线路在你的服务器上不畅，将 CPU 源那一行改为下列备用地址，再重新 build：

```dotenv
BUILD_TORCH_INDEX_URL=https://mirror.sjtu.edu.cn/pytorch-wheels/cpu
```

普通 PyPI 镜像与 CPU 轮子专用镜像是两种仓库，PyTorch 并不要求使用官方源。这里保留 `torch==2.6.0+cpu` 精确版本，使用国内镜像提供的对应安装包。CPU 索引参数应填到 `/cpu` 层级，不要多加 `/torch`，pip 会自己拼接包名。

也已核对阿里专用目录 `https://mirrors.aliyun.com/pytorch-wheels/cpu/`：它提供同一个 CPU 安装包，但这是平铺文件目录，pip 使用方式为 `--no-index --find-links`，不能直接填进当前的 `BUILD_TORCH_INDEX_URL`（对应的 `/cpu/torch/` 不存在）。本手册采用与现有 `--index-url` 构建参数兼容的南大 / 交大地址，服务器可以直接改一行继续构建。清华普通 PyPI 的 torch 页面未列出本次所需 `2.6.0+cpu` 文件；这不影响它用于其他普通依赖。

```bash
cd /opt/cs
docker compose -f docker-compose.yml config --quiet
```

无报错且退出码为 0 后，继续构建：

```bash
docker compose --progress plain -f docker-compose.yml build ics-agent
```

不要运行不带 `--quiet` 的 `docker compose config` 后把完整输出发给别人，它可能展开密钥。

首次构建需要下载 Python 依赖、CPU 版 PyTorch 等，可能耗时较久。使用 tmux 等待构建结束；不要因终端暂时没有新输出重复发起构建。成功时应显示镜像构建完成，命令退出码为 0。

构建分两次安装：先只从独立 CPU 源取得固定的 `torch==2.6.0+cpu`，再从普通 PyPI 镜像安装其他依赖并补齐 torch 的依赖。第二阶段仍保留 CPU 版精确版本约束，最后执行 `pip check` 检查依赖一致性。普通包不再逐个访问 PyTorch 的额外源。

构建使用 BuildKit 的 pip 缓存，同一构建器后续重试可复用已缓存的完整下载和成功的镜像层；不要添加 `--no-cache` 或先清理构建缓存。这不保证未下载完的大文件能跨构建断点续传。默认 120 秒是 pip 网络超时参数，8 次为连接重试参数，不是整个构建的最长时限，也不能保证所有中途断流都会自动恢复。

入口采用内网并不表示完全离线部署：服务器仍需能够访问 Git 仓库、Docker 镜像源、Python 包源、CPU PyTorch 下载源，以及你配置的云模型服务。图片 OCR 也可能初始化额外资源。如果没有任何出站网络，本手册的在线构建步骤不能直接使用，需另行准备离线镜像和依赖。

### 7.1A 已因 pip 下载慢而失败：从当前进度恢复

**如果你已经同步过上一版“两个源分别下载”的构建文件，现在只需切换 CPU 源：** 编辑 `/opt/cs/.env`，把原 `BUILD_TORCH_INDEX_URL` 的值改为 `https://mirrors.nju.edu.cn/pytorch/whl/cpu`（或上面的交大备用地址），保存后执行 `docker compose --progress plain -f docker-compose.yml build ics-agent`。旧 `.env` 的官方地址会覆盖新版默认值，因此要修改实际 `.env` 中的原行。已有构建支持该参数时，无需等待新代码即可切换；模型和业务配置保持原值。

若还没同步过支持 `build.args` 的构建文件，执行下面的完整恢复流程：

1. **Windows 开发端：** 按第 4.6 节第二步提交并推送本次改动，尤其是 `docker-compose.yml`、`deploy/server/Dockerfile.backend-only` 和 `deploy/server/bot.env.example`。
2. **Ubuntu 服务器：** 确认源码仓库干净，再拉取：

```bash
git -C /opt/cs-src status --short
```

没有本地改动时继续：

```bash
git -C /opt/cs-src pull --ff-only
```

3. 确认 pull 成功，执行 **第 4.6 节第四步的整段同步命令**，将新 Compose 和构建模板同步到 `/opt/cs`。仅在 `/opt/cs-src` 拉取，不会更新 `/opt/cs/Dockerfile`。无需复制初始业务数据或解压模型。
4. 编辑实际运行配置：

```bash
cd /opt/cs
nano .env
```

加入上面第 7.1 节的四个 `BUILD_...` 配置项；已存在则修改原行，不重复追加。保存退出后核对根 Dockerfile 已更新：

```bash
cmp -s deploy/server/Dockerfile.backend-only Dockerfile && echo 'backend build template OK'
grep -nE 'ARG.*INDEX_URL|mount=type=cache|pip check' Dockerfile
docker compose -f docker-compose.yml config --quiet
```

应看到两个下载源参数、缓存挂载和 `pip check`；Compose 校验无报错后重新执行：

```bash
docker compose --progress plain -f docker-compose.yml build ics-agent
```

这是重新构建应用镜像，不会清空已有数据库、模型或后台业务文件。构建成功后继续第 7.2 节，不要重复上传大模型包。

按日志区分剩余问题：

| 卡住位置 / 现象 | 下一步 |
| --- | --- |
| `pandas`、`FastAPI`、`LangChain` 等普通依赖下载超时 | 查看实际 `Looking in indexes` 是否是所设国内源；确认新 Compose、Dockerfile、`.env` 都已生效。可将 `BUILD_PIP_INDEX_URL` 换为你网络可达的其他可信 PyPI 镜像，再 build |
| `torch-2.6.0+cpu` 下载慢 | 核对实际 `.env` 是否已将旧官方地址换成南大 CPU 镜像；若南大线路不畅，改用第 7.1 节已核对版本的交大 CPU 镜像，再 build |
| `No matching distribution found for torch==2.6.0+cpu` | 核对 CPU 源、架构和 Python 版本；不要改成不带 `+cpu` 的版本来绕过，否则可能拉入大型 CUDA 依赖 |
| `load metadata for ... python` 或 `postgres` 镜像拉取失败 | 属于 Docker 镜像源网络问题，修改 pip 源无效；按主机现有 Docker 镜像代理 / 企业镜像仓库配置处理，不覆盖正在运行 Chatwoot 的 Docker 设置 |
| 反复出现 `pip is looking at multiple versions` | 可能是依赖解析回溯，换源不能保证解决；保留末尾冲突信息，再按具体约束处理，不盲目升级全部依赖 |
| `--mount option requires BuildKit` | 使用第 3 节的 Compose V2 / Buildx；不要用已停用 BuildKit 的旧构建方式 |

保留 HTTPS 校验，无需增加 `trusted-host`。此处是构建网络调整，不涉及云模型运行时的 `LLM_BASE_URL`。若仍失败，记录失败的包名及构建末尾 30 行错误，避免仅凭进度长时间不变判定原因。

### 7.2 启动前检查配置与模型配套

以下一次性容器只读取配置和模型文件，不连接数据库、不调用云端模型、不发送 Chatwoot 消息。可在正式启动前发现占位值和模型 CSV 不配套的问题：

```bash
docker compose -f docker-compose.yml run --rm --no-deps -T ics-agent python - <<'PY'
import hashlib
import json
from pathlib import Path
from pydantic import ValidationError
from ics_agent.app.core.config import AppSettings
from ics_agent.rag.config import RAGSettings
from ics_agent.faq.config import FAQSettings

try:
    app = AppSettings()
    rag = RAGSettings()
    FAQSettings()
except ValidationError as exc:
    for item in exc.errors():
        print('配置类型错误：', '.'.join(map(str, item['loc'])))
    raise SystemExit('请修正配置后重试；不要公开完整配置内容。')

required = (
    'postgres_password', 'admin_initial_password', 'admin_jwt_secret',
    'chatwoot_api_token', 'chatwoot_agent_bot_secret', 'llm_api_key',
    'chatwoot_base_url', 'llm_model', 'llm_base_url',
)
missing = []
for key in required:
    value = str(getattr(app, key)).strip()
    if not value or any(marker in value.lower() for marker in ('replace_', 'change-me', 'your_')):
        missing.append(key.upper())
if not app.chatwoot_account_id.isdigit():
    missing.append('CHATWOOT_ACCOUNT_ID（应为实际数字 ID）')
if missing:
    raise SystemExit('请填写配置项：' + ', '.join(missing))
assert app.app_env == 'production', 'APP_ENV 应为 production'
assert not app.chatwoot_profile_allow_query_identifiers, '服务器需关闭 URL 身份调试'
assert rag.webhook_rag_timeout_seconds < rag.webhook_processing_timeout_seconds, '查询预算必须小于任务总时限'

model_dir = Path(rag.rag_output_dir)
assert (model_dir / 'model.safetensors').is_file(), '缺少意图模型权重'
model_directories = [rag.knowledge_base_embedding_model_dir, rag.rag_pretrained_model_dir]
if rag.rag_reranker_enabled:
    model_directories.append(rag.rag_reranker_model_dir)
for directory in model_directories:
    assert (Path(directory) / 'config.json').is_file(), '缺少模型配置：' + directory
metadata = json.loads((model_dir / 'labels.json').read_text(encoding='utf-8'))
expected = metadata.get('dataset', {}).get('sha256')
actual = hashlib.sha256(Path(rag.rag_intent_dataset_path).read_bytes()).hexdigest()
assert expected and actual == expected, '意图 CSV 与模型指纹不一致，先检查 CRLF 和模型版本'
print('PASS: 配置必填项、类型、模型基础文件和意图 CSV 指纹检查通过')
print('当前策略：', {
    'worker': rag.webhook_worker_count,
    'reranker_enabled': rag.rag_reranker_enabled,
    'reranker': rag.rag_reranker_max_concurrency,
    'llm_concurrency': app.llm_max_concurrency,
    'llm_timeout': app.llm_timeout,
    'rag_timeout': rag.webhook_rag_timeout_seconds,
    'db_pool': app.postgres_pool_size,
    'db_acquire_timeout': app.postgres_pool_acquire_timeout,
    'memory_days': rag.rag_memory_retention_days,
    'block_seconds': app.chatwoot_block_duration_seconds,
})
PY
```

应看到 PASS 及当前非敏感策略值。该检查不验证云模型凭据是否可用，也不加载所有大模型权重，后面仍需真实 FAQ / RAG 验收。若只有 CSV 指纹失败，先检查第 4.6 节的 `.gitattributes` 和 CRLF，确认与模型版本一致；不要关闭校验或直接修改模型的预期指纹。

### 7.3 启动机器人自己的 PostgreSQL

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
- `retrieved_count` 大于 0，`documents` 中资料与问题相关。启用精排时 `reranked_count` 大于 0；关闭精排时 `retrieved_count` 最多为 5、`reranked_count=0`，文档 `rank` 为双塔召回排名、`score=null`。
- `answer` 非空，内容符合知识库，且不是“当前智能客服正在维护……”的兜底文案。

RAG 测试会实际调用你配置的云端模型。手工 `/rag/query` 没有 Chatwoot 路径的 **30 秒查询整体兜底**；`curl --max-time` 只限制当前客户端等待，失败后先看日志，不要连续重复提交。检索故障在这个直接查询接口可能表现为 HTTP 错误，实际 Chatwoot 的维护回复路径按第 12.5 节验证。

### 9.6 通过 SSH 隧道访问管理后台

在 **Windows PowerShell** 新开终端执行，窗口保持开启：

```powershell
ssh -N -o ServerAliveInterval=30 -L 3001:127.0.0.1:3001 ics
```

Windows 浏览器打开 `http://127.0.0.1:3001/admin`，用 `.env` 的 `ADMIN_USERNAME` 和 `ADMIN_INITIAL_PASSWORD` 登录。

若本机 3001 已被你本地客服服务占用，换本地端口，不改服务器端口：

```powershell
ssh -N -o ServerAliveInterval=30 -L 13001:127.0.0.1:3001 ics
```

此时打开 `http://127.0.0.1:13001/admin`。预期能看到 FAQ、知识库维护和日志界面。

管理员初始密码只在首次创建记录时生效。数据库已有管理员后，修改 `.env` 的初始密码并重启不会重置密码。

## 10. 让 Chatwoot 能访问机器人

**你当前没有公网域名，主流程使用第 10B 节的私网方式。** 第 10A 节保留为以后需要 HTTPS 域名时的可选操作，本次可以跳过。

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

模板只公开 `/webhooks/chatwoot` 和 `/health`，其他路径默认返回 404，已有前端仍由原来的站点提供。特别是当前 **`/rag/query` 没有认证**，会调用付费模型，不应通过公网默认开放。管理员使用第 9.6 节的 SSH 隧道。

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
```

等待模型预热结束。可用 `tail -f log/uvicorn-admin.stderr.log` 查看新一次启动日志，看到 `Application startup complete` 后按 `Ctrl+C` 退出查看，再检查私网入口：

```bash
curl --fail --max-time 20 http://172.20.48.12:3001/health
```

`up -d` 返回只代表容器已启动，不代表模型预热已完成；立即请求得到连接拒绝时先看启动日志。日志已有初始化失败时按第 16 节排错，不要仅靠反复 recreate 继续。

改成私网绑定后，本文后续所有**在服务器执行**的 `http://127.0.0.1:3001` 都改为 `http://172.20.48.12:3001`。Nginx 的 127.0.0.1 上游也不再适用；10A 与 10B 是两条选择路径。

SSH 隧道也相应改成：

```powershell
ssh -N -o ServerAliveInterval=30 -L 13001:172.20.48.12:3001 ics
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

本地验证的 Chatwoot 4.17.1 对内网 Webhook 有访问限制；当错误日志明确提示私网地址被拒绝时，可由 Chatwoot 管理员在其已有部署配置中为 Rails 和 Sidekiq 设置 `SAFE_FETCH_ALLOW_PRIVATE_NETWORK=true`，再重建对应容器。不同服务器版本可能不同，先核对该版本支持的配置。这是 **Chatwoot 侧**配置，写进机器人的 `.env` 不生效；此设置扩大 Chatwoot 可请求的网络范围，应配合本次受控的内网访问范围使用。

本路径的 Webhook 地址为：

```text
http://172.20.48.12:3001/webhooks/chatwoot
```

这里仍必须使用签名 Secret；准备阶段是第 5 节生成的临时值，绑定前替换为真实 AgentBot Secret。在 Ubuntu 执行以下未签名探测：

```bash
curl -sS --max-time 10 -o /dev/null -w '%{http_code}\n' \
  -X POST -H 'Content-Type: application/json' \
  -d '{"event":"deployment_probe"}' \
  http://172.20.48.12:3001/webhooks/chatwoot
```

预期为 `401`，因为缺少合法签名；不是 `200` 或 `202`。这里不用 `curl --fail`，因为预期响应就是 401。此请求不包含用户消息。网络连通、签名拒绝均正常后再配置 AgentBot。

Docker 发布端口时可能绕过部分 UFW 规则，不能只凭 `ufw status` 判断已经限制来源；核对云安全组和实际连通结果。管理后台通过 SSH 隧道访问；用户问答通过已有前端测试，浏览器需要能访问内网 Chatwoot `http://172.20.48.12:3000`。

## 11. 创建测试 AgentBot，连接已有 Chatwoot

### 11.1 确认已有前端所用的测试 Website Inbox

先在已有前端配置中找到 Website Token，在 Chatwoot Inbox 设置中对应到同一个 Website Inbox。如果这是专门用于开发测试的 Inbox，可以复用，但切换前记录原 AgentBot 的 ID 和绑定状态，确认当前没有其他测试者使用。这样前端完全不需要重新部署。

如果该 Inbox 已有真实用户，使用独立测试 Inbox。新建方法如下：

在已有 Chatwoot 后台：

1. 打开“设置 → 收件箱 / Inboxes → 添加收件箱”。
2. 选择 Website 网站渠道，名称例如“智能客服服务器测试”。
3. 网站地址填写你已经部署的前端测试入口地址。
4. 把用于验收的人工坐席加入这个 Inbox。机器人与人工在同一个 Inbox 内切换。
5. 保存后，在 Widget 安装代码中找到 `websiteToken`，记录该值。
6. 记录 Inbox ID。一般能在 Inbox 设置页地址或下面的 API 查询结果中看到。

新 Inbox 的 Website Token 与原来不同，需要在已有前端支持的测试配置中选择它；仅创建 Inbox 不会改变前端原来的连接。不要把 Account ID、Inbox ID、AgentBot ID 混用。第一次测试可保持所有人工坐席离线，验证机器人后再让一名测试坐席上线。

### 11.2 选择 UI 配置或 API 配置

如果 Chatwoot 后台提供“Agent Bots / 机器人”页面：创建 **Webhook 类型**机器人，Outgoing URL 填第 10 节的 Webhook 地址，记录 AgentBot ID、Access Token、Secret，先不要绑定真实业务 Inbox。三个值分别填入机器人 `.env`，再把 `CHATWOOT_AUTO_REPLY_ENABLED=true`。

如果 UI 没有这个入口，使用下面经过本地 Chatwoot 4.17.1 源码核对的 Account API 操作。已有服务器版本仍需支持这些接口；出现 404 或响应没有 Secret 时停止核对版本，不能靠关闭签名校验继续。

**UI 和 API 二选一，不要重复创建两次机器人。** 第 11.3–11.5 节是完整 API 路径。

### 11.3 API 路径：确认账号与 Inbox

以下命令在**机器人 Ubuntu 服务器**执行。先填写变量；重新 SSH 登录或新开终端后，变量不会自动保留：

```bash
cd /opt/cs
umask 077
CW_DEPLOY_BASE='http://172.20.48.12:3000'
CW_DEPLOY_ACCOUNT='实际Account数字ID'
CW_DEPLOY_INBOX='实际测试Inbox数字ID'
CW_DEPLOY_WEBHOOK='http://172.20.48.12:3001/webhooks/chatwoot'
read -r -s -p '粘贴 Chatwoot 用户 API Token，然后回车：' CW_DEPLOY_TOKEN
printf '\n'
```

以上示例按机器人也部署在 `172.20.48.12`、采用第 10B 节 3001 私网入口填写；如果机器人部署在另一台机器，只将 `CW_DEPLOY_WEBHOOK` 改为机器人实际地址。`CW_DEPLOY_BASE` 始终是已有 Chatwoot 的 3000 地址。输入 Token 时不显示字符是正常的。

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

AgentBot 通常不负责独立的 `contact_updated` 事件。为使拉黑立即自动结束当前人工接待、下一条消息恢复 AI，并按配置期限自动解除，需在 Chatwoot“设置 → 集成 / Integrations → Webhooks”增加 Account Webhook：

- URL：与本次机器人的 `/webhooks/chatwoot` 地址相同。
- 订阅 `contact_updated`；只有发现 AgentBot 没有送达状态变化时，再补 `conversation_status_changed` / `conversation_updated`。
- 将此 **Account Webhook 自己的 Secret** 填入 `CHATWOOT_WEBHOOK_SECRET`，不要用它覆盖 AgentBot Secret。
- 保存后执行 `docker compose -f docker-compose.yml up -d --force-recreate ics-agent`。
- 若要测试短时长（例如 `CHATWOOT_BLOCK_DURATION_SECONDS=60`），同时设置
  `CHATWOOT_BLOCK_REAPER_INTERVAL_SECONDS=5`；到期任务会在截止时间后最多再等待一个轮询周期。
- 已经写入数据库的拉黑记录保存的是绝对 `blocked_until`，修改 `.env` 不会回溯旧记录。测试前请先在 Chatwoot 解除并重新拉黑该联系人；不要把新时长盲目套用到全部历史记录。

不要额外订阅另一份 `message_created` 来重复驱动同一机器人。当前支持两个 Secret，是为了验证不同来源的签名，不是要求同一消息通过两路触发。

## 12. 浏览器端完整验收

### 12.1 打开测试页面

打开你**已经上传并部署的前端原地址**，无需上传新页面，也不要访问机器人服务器的 `/support-chat` 路径。

如果复用原开发测试 Inbox，页面的 Chatwoot 地址和 Website Token 保持原值。若另外创建了测试 Inbox，则按现有前端支持的方式选择该 Inbox 的 Website Token；只有确认前端支持 URL 参数时，才使用 `?websiteToken=...`，不要假设所有页面都支持。

发一条唯一的测试消息，确认 Chatwoot 后台在**预期的测试 Inbox** 收到它，再检查新服务器日志是否出现对应事件。消息进入其他 Inbox，或只在旧开发机器人日志里出现时，先检查前端 Token 与 AgentBot 绑定，不要同时打开两套机器人处理相同消息。

通过 SSH 隧道打开的 `http://127.0.0.1:13001/admin` 只用于管理后端；它不是本次复用的用户聊天入口。

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

### 12.5 验证空回复、检索故障和查询超时兜底

当前代码的处理边界如下：

| 情况 | 当前行为 |
| --- | --- |
| FAQ 基础设施查询异常 | 尝试转到 RAG；后续 RAG 也失败时才走维护提示 |
| 主模型等待达到 10 秒、请求异常，或返回空字符串、全空白、`None`、没有有效文本的内容块 | 立即切换 `LLM_MODEL2`，主备各调用一次；备用模型使用剩余查询预算，仍失败或空回复则返回维护提示 |
| 意图识别、检索、重排、查询中的记忆访问异常 | Chatwoot 查询外层捕获，返回维护提示；不为这类已兜底的查询继续重试整个 Webhook 任务 |
| 查询阶段达到当前 30 秒预算 | Chatwoot 查询外层取消等待并返回维护提示，包括正在进行的备用模型调用 |
| 检索正常完成但没有命中文档 | 不等同于“检索异常”，仍可能进入回复生成；验收知识库时不能仅凭无报错判定成功 |
| Chatwoot API 回复发送失败、查询外的数据库控制状态失败 | 不属于上述查询兜底保证；任务可能重试并最终失败，用户可能收不到维护提示 |

回复节点对主备模型都显式设置 `max_retries=0`，不再执行原来的 1 秒、3 秒退避重连。`LLM_MAX_RETRIES=2` 仅作为其他直接工厂调用的默认值。30 秒查询预算包含 FAQ、记忆、检索、重排、模型并发槽位等待和主备模型调用；不包含进入 worker 前的队列等待、查询前的部分会话控制操作，以及查询后的 Chatwoot 写回耗时。CPU 线程或第三方调用的取消也可能存在延迟，因此它是应用等待预算，不是用户侧严格的送达时限。

以下命令在容器里的独立进程模拟三种故障，不修改运行服务，不调用真实模型，不给 Chatwoot 发消息。为了不用等待 30 秒，超时分支仅在这个独立进程临时用 0.01 秒测试：

```bash
docker compose -f docker-compose.yml exec -T ics-agent python - <<'PY'
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from ics_agent.app.chatwoot import routes
from ics_agent.rag.graph import nodes

async def check():
    model = AsyncMock(return_value=SimpleNamespace(content=None))
    with patch.object(nodes, 'call_cloud_llm', model):
        result = await nodes.llm_reply_node({'query': '服务器空回复测试', 'reranked_documents': []})
    assert result['answer'] == nodes.LLM_REPLY_FALLBACK_ANSWER
    assert model.await_count == 2
    assert [call.kwargs['use_fallback_model'] for call in model.await_args_list] == [False, True]
    print('PASS: 主备模型均空回复后返回指定维护提示')

    with patch.object(routes, '_run_rag_query', new=AsyncMock(side_effect=RuntimeError('retrieval unavailable'))):
        result = await routes._build_webhook_answer(
            content='服务器检索故障测试', thread_id='deployment-check:retrieval', event_key='deployment-check:retrieval'
        )
    assert result['route'] == 'rag_fallback'
    assert result['answer'] == nodes.LLM_REPLY_FALLBACK_ANSWER
    print('PASS: 检索异常返回指定维护提示')

    async def stalled_query(*args, **kwargs):
        await asyncio.sleep(1)

    with patch.object(routes, '_run_rag_query', new=stalled_query), patch.object(
        routes, 'rag_settings', SimpleNamespace(webhook_rag_timeout_seconds=0.01)
    ):
        result = await routes._build_webhook_answer(
            content='服务器查询超时测试', thread_id='deployment-check:timeout', event_key='deployment-check:timeout'
        )
    assert result['answer'] == nodes.LLM_REPLY_FALLBACK_ANSWER
    assert 'TimeoutError' in result['rag_error']
    print('PASS: 查询超时返回指定维护提示')

asyncio.run(check())
PY
```

预期输出三条 PASS，期间出现模拟故障日志是正常的。不要通过修改正在使用的 API Key 来模拟故障。这个检查验证兜底分支；真实回复的 Chatwoot 投递能力仍以第 12.2 节前端验收为准。

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

| 改了什么 | 需要执行什么 |
| --- | --- |
| 只改服务器 `.env` 的运行参数 | 修改原文件后 recreate 应用；普通 restart 不更新容器环境 |
| 改 `.env` 中的 `BUILD_...` 下载源 / 构建参数 | 重新 build；单独 recreate 不能重新安装镜像内的依赖 |
| 改 Python、依赖、Dockerfile | Git 拉取、同步代码、重新 build 并启动应用 |
| 只改仓库中的 `.env.example` / 服务器模板 | 模板不会自动覆盖运行 `.env`，先手工合并参数，再 recreate |
| 改 FAQ / 知识库 | 管理后台更新，或第 13.3～13.4 节；单纯 git pull 不会更新运行数据 |
| 换模型或训练 CSV | 作为配套版本单独部署，检查指纹并重新预热；不属于普通代码同步 |

### 13.1 只改 .env

本节针对机器人运行参数；`BUILD_...` 构建参数按第 7.1 节重新构建镜像后再更新应用。

```bash
cd /opt/cs
nano .env
docker compose -f docker-compose.yml up -d --force-recreate ics-agent
```

然后检查健康和日志。修改数据库密码不属于普通环境变量变更：PostgreSQL 已有数据卷时，改 `POSTGRES_PASSWORD` 不会自动修改已有数据库用户密码，可能导致应用连接失败。

**从旧手册配置升级到当前版本：** 保留原服务器密钥、地址和已建立的管理员账号，在 `/opt/cs/.env` 修改对应原行：

```dotenv
CHATWOOT_BLOCK_DURATION_SECONDS=86400
RAG_MEMORY_RETENTION_DAYS=15
WEBHOOK_WORKER_COUNT=10
RAG_RERANKER_ENABLED=false
RAG_RERANKER_MAX_CONCURRENCY=4
LLM_MAX_CONCURRENCY=8
LLM_CONCURRENCY_WAIT_TIMEOUT=15
POSTGRES_POOL_SIZE=12
POSTGRES_POOL_ACQUIRE_TIMEOUT=1.0
LLM_MODEL2=deepseek-flash
LLM_MODEL_PROVIDER2=openai
LLM_API_KEY2=你的备用模型API密钥
LLM_BASE_URL2=https://备用模型服务地址/v1
LLM_TIMEOUT=10
WEBHOOK_RAG_TIMEOUT_SECONDS=30
```

缺少的项才追加；不要给同一键保留两个不同值。LLM 模型、Base URL 和 API Key 按第 5.4 节成组核对。若主动采用了第 5.7 节的小并发方案，则保留你选定的并发值。修改后 recreate 并按第 7.2 节读回配置摘要。

### 13.2 更新后端代码，不重复传模型或前端

#### 13.2A Git 部署方式

先在 Windows 提交并推送新版本。服务器按第 14 节备份，记录当前镜像版本，在当前请求结束后操作。

若首次使用 Git 接管之前的压缩包部署，只执行第 4.6 节第三步中安装 Git/rsync、创建 `/opt/cs-src` 并 clone 的操作，保留已有 `/opt/cs`。不要再次复制初始业务数据或解压模型。确认源码仓库版本正确后，再继续下列流程。

1. 在 Ubuntu 检查源码仓库是否干净：

```bash
git -C /opt/cs-src status --short
git -C /opt/cs-src log -1 --oneline
```

`status` 没有输出才继续。如果有本地改动，先查清原因并保留需要的修改；不要用 `reset --hard` 清除它们。

2. 拉取当前分支的新提交：

```bash
git -C /opt/cs-src pull --ff-only
git -C /opt/cs-src log -1 --oneline
```

确认 pull 成功且提交 SHA 是本次目标版本。如果提示分支分叉、权限或网络错误，停止这次发布，先解决 Git 问题。

3. 重新执行 **第 4.6 节第四步“仅同步运行所需的代码与模板”** 的完整代码块。无需执行第五步的初始数据复制。
4. 在运行目录校验配置、构建并更新应用：

```bash
cd /opt/cs
docker compose -f docker-compose.yml config --quiet
```

配置检查成功后再执行：

```bash
docker compose -f docker-compose.yml up -d --build ics-agent
```

等待预热完成，按第 9 节检查日志、FAQ、RAG 和健康接口，再从已有前端验收。成功后记录实际采用的源码版本：

```bash
mkdir -p /opt/cs/deployment-records
git -C /opt/cs-src rev-parse HEAD > /opt/cs/deployment-records/source-commit.txt
```

日常同步保留服务器 `.env`、`models/`、`database/`、知识库分块与向量库、日志及数据库卷。新版本若增加环境变量，需阅读配置模板并手工补到服务器 `.env`，不要直接用模板覆盖已有值。部署目录的 Nginx 文件只是模板，同步不会自动更新 `/etc/nginx/` 中已启用的站点。

#### 13.2B 压缩包部署方式

如果只修改 Python 代码、依赖或后端构建模板：

1. 在 Windows 重新执行第 4.3 节，生成新的 `D:\cs-app.tar.gz` 并记录新 SHA256。
2. 只上传这个后端包：

```powershell
scp -o ServerAliveInterval=30 D:\cs-app.tar.gz ics:~/
if ($LASTEXITCODE -ne 0) { throw '后端包上传失败。' }
```

3. 在 Ubuntu 执行 `sha256sum ~/cs-app.tar.gz`，与本次 Windows 结果核对。
4. 先按第 14 节备份，在当前测试请求已结束后更新：

```bash
cd /opt/cs
tar --exclude=database -xzf ~/cs-app.tar.gz
cp deploy/server/Dockerfile.backend-only Dockerfile
docker compose -f docker-compose.yml up -d --build ics-agent
```

这里特意排除解压 `database/`，避免单纯更新代码时覆盖服务器已经通过管理后台维护的 FAQ、知识库或意图 CSV。模型目录不在后端包内，`.env` 也不在包内，二者保持原样。知识库/FAQ 更新分别按第 13.3、13.4 节操作。

服务器未使用开发 override，所以代码变化后需要重新构建。现有前端独立维护，无需跟随后端重新上传。

只有模型发生变化时才重新执行第 4.2 节并上传模型包。更新模型应在停止机器人应用后进行，检查意图模型与 CSV 指纹配套，再启动验收；不要把新模型目录直接混入正在使用的旧模型版本。

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
  ics_agent knowledge_base_in/*.py Dockerfile \
  docker-compose.yml .dockerignore .env.example requirements.txt deploy docs
chmod 600 "$CS_BACKUP_DIR/bot-postgres.dump" "$CS_BACKUP_DIR/bot-files.tar.gz"
docker compose -f docker-compose.yml start ics-agent
```

停止应用后再归档向量库，是为避免复制正在写入的数据库文件。如果中间备份失败，也要在排错后记得 `start ics-agent` 并检查服务。模型目录另外保留 `cs-models.tar.gz` 或同步到备份存储；已有前端按其原部署方式单独备份，不要求本服务器存在客服 HTML 文件。

### 14.3 新服务器空库恢复顺序

1. 在新服务器安装 Docker，准备一个独立项目目录和备份文件，暂不启动应用或数据库。
2. 先从 `bot-files.tar.gz` 恢复文件到正确项目目录，再从 `cs-models.tar.gz` 恢复模型。确认 `.dockerignore` 已恢复，避免构建时把整个模型目录送入镜像构建上下文；不要覆盖正在运行的另一套系统。
3. 核对恢复后的 `.env` 地址、密钥、数据库名、用户名和密码，确认目录完整后再执行 `docker compose -f docker-compose.yml up -d postgres`，等待 healthy。应用仍保持停止。新空库首次初始化必须使用这份已核对的配置，不要先用临时密码建库、再覆盖成备份中的密码。
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

Git 部署方式还需在验收成功时记录本次采用的提交；记录后再开始下一次拉取更新：

```bash
git -C /opt/cs-src rev-parse HEAD > /opt/cs/deployment-records/source-commit.txt
```

## 15. APP 登录身份、性能和当前边界

### 15.1 匿名首测与 APP 身份接入

本手册复用已经部署的前端和身份链路；如尚未完成身份接入，可用匿名 Widget 先验证部署。正式 APP 应由业务后端根据已验证的登录态生成 `identifier` 和 `identifier_hash`，而不是让浏览器随意传 `userId` 就获得某个用户的身份签名。

- Website HMAC Token 只放后端。
- 保持 `CHATWOOT_PROFILE_ALLOW_QUERY_IDENTIFIERS=false`。
- 实际业务后端提供身份接口后，在独立前端的部署配置中接入；该接口的跨域允许来源应是实际页面来源。
- 本次后端镜像不包含独立前端，前端变更通过其原发布流程生效。
- 新服务器首次部署无需执行 README 中针对旧匿名/实名历史数据的迁移 SQL。

### 15.2 首测不要横向扩容

当前使用单个应用进程、内存队列和进程内会话锁，FAQ 也有进程内索引。首测不要加多个 Uvicorn workers，不要执行 `--scale ics-agent=2`。想支持多个实例，需要先改造共享队列、会话串行和索引刷新方式。

本项目主要在 CPU 上运行本地检索模型，生成回复通过云端大模型。当前模板保留你最新的 10 / 4 / 8 并发基线，但它不是目标服务器的容量证明；先用少量测试会话观察 `docker stats`、内存、等待时间和模型服务限流。资源不足按第 5.7 节降低并发并记录实际取值，仍只启动一个应用进程。

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
| Git 提示 `Permission denied (publickey)` | 服务器独立密钥、SSH 别名、仓库 Deploy Key | 按第 4.6 节配置读取权限；Windows 的 `ssh ics` 不能提供 Git 仓库认证 |
| Git 提示远端分支不存在 / 找不到新模板 | 当前分支、推送结果、提交 SHA | 本轮分支为 `test`，核对已推送的实际版本；仅保存未提交的文件无法拉取 |
| `docker: permission denied` | 当前用户是否已加入 docker 组、是否重新登录 | 重新登录，或由管理员使用 sudo；不改 Docker socket 为所有人可写 |
| `port is already allocated` | `sudo ss -lntp` | 调整机器人 3001 / 数据库宿主机 5433 映射，不抢占 Chatwoot 原端口 |
| 构建下载超时 | 具体失败包 / Docker Registry 与生效的构建参数 | 按第 7.1A 节区分普通 PyPI、CPU PyTorch 与 Docker 镜像；同步新构建文件和参数后保留缓存重试 |
| 容器退出码 137 / `Killed` | `free -h`、`docker stats`、内核 OOM 日志 | 增加内存、减小批次/并发；检查是否在同机同时跑 Chatwoot 与向量化 |
| 磁盘满 | `df -h`、`docker system df`、`du -sh models log knowledge_base_in/vector_store` | 优先清理已核对的上传临时包；不要删除数据库卷或盲目 prune |
| `/health` 正常，但一直维护提示 | 应用 stderr 日志，不能只看 docker logs | 检查模型文件、向量库、数据库和模型 API；健康接口不是完整就绪检查 |
| FAQ 全走 RAG | FAQ 表行数、是否执行导入、是否已刷新服务内存 | 导入新库；若服务已先启动，导入后 restart 或使用后台更新 |
| 意图模型 fingerprint mismatch | CSV 与 `labels.json` 是否配套 | 重新上传配套 CSV/训练产物，检查传输是否改变文件内容 |
| `No such file` 或 Windows 路径 | `.env` 和模型元数据路径 | 使用 `/app` 相对路径，补齐模型实际权重；不复制 Windows 环境路径 |
| OCR 初始化失败 | stderr 的缺失依赖或模型下载错误 | 按明确错误补齐资源并重新构建；确认所需图片已成功提取文字再验收 |
| 数据库认证失败 | `.env`、数据库初始化时的密码 | 已有卷不会因改 env 自动改数据库密码，恢复正确配置或由管理员执行密码变更 |
| 数据库连接池获取超时 | 生效的连接池配置、数据库状态、并发 | 核对当前 `POSTGRES_POOL_SIZE=12`、`POSTGRES_POOL_ACQUIRE_TIMEOUT=1.0` 已 recreate 生效；不要把连接池耗尽误当密码错误 |
| Chatwoot 经常收到维护提示 | 查询耗时和失败日志、模型限流、CPU / 内存 | 按第 12.5 节区分空回复、检索异常与 30 秒查询预算耗尽；先排查瓶颈，再决定是否调整预算 |
| 管理员密码不对 | 首次初始化时的配置、system_users 是否已有记录 | 改 `ADMIN_INITIAL_PASSWORD` 不会覆盖已有密码；走账号重置流程，不删除整个库 |
| Webhook 401 | AgentBot Secret、Account Secret、时间同步 | Secret 与来源对应；recreate 应用；核对 Chatwoot 是否发送签名头 |
| Chatwoot 收到消息但机器人没日志 | Outgoing URL、Inbox AgentBot 绑定、Sidekiq 网络 | 从 Chatwoot 实际任务容器探测机器人，检查 Sidekiq 投递日志 |
| Chatwoot 报私网 URL 被拒 | Chatwoot 版本和 safe-fetch 日志 | 按第 10B 节在 Chatwoot 侧配置该版本支持的私网访问；写到机器人 .env 无效 |
| 有答案日志但 Chatwoot 没收到 | `CHATWOOT_AUTO_REPLY_ENABLED`、API Token、发送错误 | 配置 true 并 recreate；用户 Token 与机器人 Token 各就其位 |
| 转人工提示没有坐席 | 测试 Inbox 成员、坐席 online 状态、open 会话数 | 坐席加入同一个 Inbox 并上线；确认未达到每人 5 个会话限制 |
| 已解决后仍不恢复机器人 | 会话状态事件是否到达、绑定是否启用 | 验证 AgentBot 状态事件，必要时补带正确 Secret 的状态 Webhook |
| 已有前端消息未到新机器人 | 前端实际连接的 Chatwoot、Website Token、Inbox 绑定 | 确认与后端配置的同一套内网 Chatwoot 对应，不混用开发环境的另一个 Inbox |
| 构建仍提示缺少 support-chat.html | 服务器根 Dockerfile 是否仍是原版 | 执行 `cp deploy/server/Dockerfile.backend-only Dockerfile` 后重新 build |
| 机器人 `/support-chat` 返回 404 | 当前是否使用后端专用构建 | 属于预期；打开原有独立前端，管理后台使用 `/admin` |
| 页面提示缺少 websiteToken | 页面 URL 参数 / CONFIG | 用当前测试 Inbox 的 Website Token，不是用户 API Token |
| 页面身份校验 403 | URL 是否携带用户身份，Profile 接口是否关闭 | 首测移除身份参数，用匿名模式；正式身份由业务后端验证 |
| 图片不显示 | FAQ 原始内容、浏览器图片请求 | 确认图片链接返回图片且浏览器可访问；旧消息不会被自动回写 |
| HTTPS 证书失败 | DNS、80/443、安全组、已有代理 | 按代理现状排查，避免安装第二套代理造成端口冲突 |
| 改 .env 后行为没变化 | 是否只用了 restart | 改用 `up -d --force-recreate ics-agent` |
| 改后端代码后没变化 | 是否重新 build | 服务器镜像模式执行 `up -d --build ics-agent`；独立前端使用其原发布流程 |

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
- [ ] 模型包 SHA256 已核对，解压到 `/opt/cs`；代码通过 Git 同步且记录提交 SHA，或备用后端包已校验解压。
- [ ] 使用当前服务器模板配置；实际并发、10 秒 LLM 超时、30 秒查询预算、15 天记忆与 1 天封禁策略已核对。
- [ ] 第 7.2 节配置与模型检查通过，没有占位凭据，数据库连接池参数已显式填写。
- [ ] 根 Dockerfile 已从后端模板复制，未重复上传独立前端。
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
- [ ] 空回复、检索异常、查询超时三项模拟兜底验证通过，真实模型配置保持有效。
- [ ] 正常重启后，数据库、管理后台和问答仍可用。
- [ ] 备份已生成并移出服务器，成功镜像和依赖版本已记录。

只有完成上述验收，再考虑把已有业务 Inbox 接到新机器人；先记录业务 Inbox 原来的机器人绑定，保留切回方法。
