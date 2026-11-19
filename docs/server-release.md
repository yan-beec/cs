# ICS 服务器统一发布

本流程适用于已有 ICS 的服务器更新。共享发布与同步脚本在 `deploy/server/`，归档数据库专用迁移步骤在 `quality/deploy/`，数据库 SQL 在 `quality/database/`。两份应用 Dockerfile 均包含 `quality/`，可继续复用 ICS 代码。

只更新代码、从 Git `test` 分支拉取、逐步手动部署且不使用发布锁时，按 [SSH ics 代码更新手册](ssh-ics-code-only-update.md)操作。该手册单独同步、构建并更新应用，不调用本页的统一发布脚本或执行数据库迁移。

本次“最近 50 轮解析”优化按 Git `test` 分支发布；本地提交与推送、`ssh ics` 拉取、010 迁移、构建及三个容器分别更新的小步骤，见[本次归档优化上线操作](archive-recent-50-rounds-deployment.md)。

## 首次使用前

- 先提交并推送本机 `test` 分支，服务器 `/opt/cs-src` 拉取远端 `test`。
- `/opt/cs/.env` 已有机器人配置和独立的 `ARCHIVE_*` 配置，各账号使用该服务器数据库的实际密码。模板见 `deploy/server/bot.env.example`，不要用模板或本机 `.env` 覆盖服务器配置。
- 服务器使用 Bash、Git、rsync、coreutils、flock（Ubuntu 的 util-linux）和支持 `up --wait --wait-timeout` 的 Docker Compose V2。本脚本不安装软件。
- 发布前按现有备份流程备份。若是全新机器人部署，先完成[首次部署手册](server-first-deployment.md)中的目录、模型、业务数据及配置准备，再执行发布。

## Git 更新

本次归档优化优先使用上面链接的分步手册。常规更新也沿用远端 `test`；以下命令逐步执行。

先检查源码工作区，预期没有输出：

```bash
git -C /opt/cs-src status --short
```

有修改时先保存和核实，工作区干净后再继续。查看远端名称：

```bash
git -C /opt/cs-src remote
```

设置远端；如果服务器实际名称为 `origin`，将下面的 `chatwoot` 改为 `origin`：

```bash
ICS_RELEASE_REMOTE=chatwoot
```

切换到发布分支：

```bash
git -C /opt/cs-src switch test
```

单独拉取：

```bash
git -C /opt/cs-src pull --ff-only "$ICS_RELEASE_REMOTE" test
```

核对提交号与本地推送后的 `test` 一致：

```bash
git -C /opt/cs-src rev-parse HEAD
```

拉取失败、不能快进或提交号不符时停在当前步骤，不使用强制重置。

## 自动发布脚本（备用）

以下脚本会一次完成同步、迁移和构建等操作。**本次需要逐步定位错误，按[分步手册](archive-recent-50-rounds-deployment.md)操作，不执行此备用命令，也不要与分步操作同时运行。** 熟悉流程后，常规更新可在核对 Git 版本、完成备份后执行：

```bash
bash /opt/cs-src/deploy/server/release.sh --source /opt/cs-src --target /opt/cs
```

默认路径就是 `/opt/cs-src` 与 `/opt/cs`，可省略这两个参数。脚本不会自行拉取 Git，不会触碰部署分支；仓库有未提交变更时拒绝发布。脚本只操作所在服务器，不会登录其他主机。

执行顺序：

1. 对目标目录加发布锁，阻止并发发布；检查源码和配置。
2. 同步 ICS、提醒模块、`quality/`、部署文件和两份 Compose 配置，使用服务器后端 Dockerfile。
3. 检查 Compose 配置，仅启动归档数据库，并等待 healthy，超时 120 秒停止。
4. 把本版数据库脚本复制到容器独立临时目录，执行版本迁移。
5. 验证表、追加权限、只读账号和真实密码登录；验证样本通过事务回滚。
6. 构建 ICS 镜像，成功后更新应用容器，记录源码 SHA。

任一步失败都会停止后续步骤并返回非零状态。数据库未就绪、迁移失败或权限验证失败时，不构建、不更新 ICS 容器；镜像构建失败时也不执行容器更新。同一版本迁移会跳过，已执行 SQL 被修改时停止。不要修改已应用迁移，后续通过新增编号 SQL 升级。

迁移事务只覆盖单个 SQL 文件，不覆盖整次发布；如果迁移已成功但后续步骤失败，数据库版本会保留，可修复问题后重试。未来迁移应兼容发布期间仍运行的旧版应用。发布脚本不自动回滚数据库。

同步会替换指定目录的代码和模板；`.env`、`database/`、`models/`、日志、知识库生成产物、提醒构建产物和数据库卷会保留。原始对话的只追加权限不变，管理密码不会交给机器人进程。

完成后等待模型预热，按首次部署手册第 9 节检查 `/health`、管理员登录、FAQ、RAG 和 Chatwoot 消息。容器更新命令成功不代表业务验收已完成。Git 发布的版本记录位于 `/opt/cs/deployment-records/source-commit.txt`。

## 启用消息接收

本部署中 Chatwoot、ICS 和采集服务位于同一服务器，沿用服务器内网 IP 直接互通。采集服务使用 3010 端口，ICS 保留 3001；无需为采集新增公网域名或 Nginx 转发。

在服务器同一份 `.env` 补齐 `ARCHIVE_SOURCE_KEY`、`ARCHIVE_ALLOWED_ACCOUNT_IDS`、专用账户 Webhook 的 `ARCHIVE_WEBHOOK_SECRET`，并将 `ARCHIVE_RECEIVER_HOST` 设置为服务器实际内网 IP（沿用现有 ICS 地址），`ARCHIVE_RECEIVER_PORT=3010`。模板的 `127.0.0.1` 仅用于初始本机检查。发布命令增加 `--with-receiver`：

```bash
bash /opt/cs-src/deploy/server/release.sh --source /opt/cs-src --target /opt/cs --with-receiver
```

脚本在构建成功后启动或更新共享镜像中的 `quality-receiver`，健康检查通过后才更新机器人；接收服务不健康则停止后续步骤。后续更新继续带该参数。完整路由、签名、网络及验收方式见[消息接收说明](../quality/capture/README.md)。默认发布流程仍不启动该可选服务。发布后在 Chatwoot Sidekiq 容器内检查 `http://服务器内网IP:3010/health` 返回 200，再将专用账户 Webhook 设置为 `http://服务器内网IP:3010/quality/webhooks/chatwoot`。

## 启用归档查询与解析

归档查询首版默认关闭。先按上述发布流程应用新增 `002_query` 迁移，并在同一份 `.env` 设置 `ARCHIVE_QUERY_ENABLED=true`；ICS 只使用已有 reader 凭据。发布构建成功后，从服务器运行目录显式启动或更新独立解析进程：

```bash
cd /opt/cs
```

再单独执行：

```bash
docker compose --env-file .env -f docker-compose.yml -f docker-compose.archive.yml --profile quality-query up -d quality-worker
```

以后更新也需要执行该命令以让解析进程使用新镜像；现有 `release.sh` 的 `--with-receiver` 仅管理采集器。后台从 `/admin` 的“对话归档”进入，详细配置、资源限制和索引重建见[归档查询首版说明](../quality/query/README.md)。查询未开启或不可用不会改变现有客服入口。

## 只更新数据库

已同步代码并配置 `.env` 后，从服务器运行目录执行：

```bash
cd /opt/cs
```

再单独执行：

```bash
bash quality/deploy/migrate.sh
```

此命令不构建或重启 ICS。迁移使用数据库事务锁，完整发布另有目标目录发布锁。

仅更新代码、运行普通 `docker compose up` 或重启容器，不会自行执行迁移和验证。分步发布按专用手册先通过数据库迁移及验证，再更新应用；使用自动发布脚本时，也需另外更新 worker 并完成业务验收。
