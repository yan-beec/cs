# SSH ics：从 Git test 分支只更新代码的操作手册

更新日期：2026-11-05。

适用于已经部署并正常运行的 ICS 服务器。本次目标是拉取 `test` 分支、同步代码、构建镜像并更新应用容器。本文不创建发布锁，不调用带发布锁和数据库迁移的 `release.sh`。

每个步骤单独执行，确认达到“预期”后再执行下一步。命令出错就停在对应步骤，保留报错信息，不把后面的命令继续粘贴执行。

## 使用前确认

- 待上线的代码已提交、合入并推送到远端 **`test` 分支**。本机尚未提交或推送的修改，服务器 `git pull` 无法取得。
- 服务器已有该版本所需的数据库结构。本手册不执行数据库迁移、FAQ 导入、向量库重建或模型上传；如果新版本要求这些操作，应使用对应功能的升级手册。
- 当前讨论的收藏夹图标和 H5 `userId` 检索修正不增加数据库迁移，可走本流程。
- 本文按现有 `/opt/cs-src` 与 `/opt/cs` 双目录部署方式编写。服务器只有一位操作人员执行本次更新，执行期间不要同时运行其他部署命令。
- 应用重建容器时会短暂中断对应服务。选择当前咨询请求结束后的空闲时间操作。

| 项目 | 本文采用的值 |
| --- | --- |
| SSH 登录 | `ssh ics` |
| Git 源码目录 | `/opt/cs-src` |
| 实际运行目录 | `/opt/cs` |
| 服务器实际配置 | `/opt/cs/.env` |
| 发布分支 | `test` |
| Git 远端 | 默认示例为 `chatwoot`，以服务器实际名称为准 |
| 主应用 | `ics-agent` |
| 归档接收器 | `quality-receiver`，只更新原来已启用的服务 |
| 归档解析进程 | `quality-worker`，只更新原来已启用的服务 |
| 三个应用共用镜像 | `ics-agent:py310` |

## 1. 登录服务器并检查当前服务

### 1.1 登录

在本机 PowerShell 执行：

```powershell
ssh ics
```

**说明：** 通过已配置的 SSH 别名登录服务器。以下命令全部在登录后的服务器 Bash 中执行。

**预期：** 登录成功，看到服务器命令提示符。

### 1.2 进入运行目录

```bash
cd /opt/cs
```

**说明：** 后续部署使用服务器当前的配置与运行目录。

**预期：** 没有报错。

### 1.3 定义本次 Compose 命令缩写

```bash
cscode() {
  docker compose --project-directory /opt/cs --env-file /opt/cs/.env \
    -f /opt/cs/docker-compose.yml -f /opt/cs/docker-compose.archive.yml \
    --profile quality-capture --profile quality-query "$@"
}
```

**说明：** `cscode` 固定使用服务器的两份 Compose 和实际 `.env`，避免加载开发环境的 `docker-compose.override.yml`。声明两个 profile 只是让对应服务可被选择，不会自动启动它们。

**预期：** 定义成功，通常没有输出。SSH 断线后重新登录，需要重新定义本函数。

### 1.4 查看当前容器

```bash
cscode ps -a
```

**说明：** 记录现有服务及其状态。`ics-agent` 应在运行；本机业务库 `postgres` 和归档库 `archive-postgres` 应正常。确认归档接收器、解析进程是否已经启用。

**预期：** 主应用为 `Up`，两个数据库为 `healthy`；已启用的接收器为 `healthy`，解析进程为 `Up`。如果某服务原来未部署或是有意停止的，跳过后面针对该服务的更新步骤，不借本次更新启用它。若原服务已经异常，先查清原因。

### 1.5 检查磁盘余量

```bash
df -h /opt/cs
```

**说明：** 本次需要保存代码备份并构建新镜像，保留旧镜像也需要空间。

**预期：** 所在分区有足够可用空间，没有接近占满。空间不足时先处理，再继续。

### 1.6 检查现有 Compose 配置

```bash
cscode config --quiet
```

**说明：** 检查 `.env` 和 Compose 是否能正确解析，不在终端打印完整配置和密码。

**预期：** 命令成功结束，通常没有输出；不能有缺少变量、文件或配置格式错误。

## 2. 保存旧代码和旧镜像

这一节只保存应用回退资料，不停止数据库，不执行数据库导入、恢复或迁移。

### 2.1 设置备份标识和目录变量

```bash
ICS_CODE_STAMP=$(date +%Y%m%d-%H%M%S)
ICS_CODE_BACKUP="/opt/cs/deployment-records/code-only-$ICS_CODE_STAMP"
```

**说明：** 为本次更新生成独立目录。同一个 SSH 窗口保留这些变量，后面继续使用。

**预期：** 通常没有输出。重新登录后不要重新生成标识，应将这两个变量设回本次已创建的时间戳和完整目录。

### 2.2 创建备份目录

```bash
install -d -m 700 "$ICS_CODE_BACKUP"
```

**说明：** 创建仅当前用户可访问的备份目录，配置备份会放在这里。

**预期：** 没有权限或空间错误。

### 2.3 记录并保存备份路径

```bash
printf '%s\n' "$ICS_CODE_BACKUP"
```

**说明：** 将输出路径记在本次操作记录中，断线恢复时使用。

**预期：** 输出类似 `/opt/cs/deployment-records/code-only-20261105-153000`。

### 2.4 备份服务器实际配置

```bash
cp -p /opt/cs/.env "$ICS_CODE_BACKUP/env.before"
```

**说明：** 保存同步前的 `.env`。后面只更新代码时不覆盖实际配置。

**预期：** 复制成功，没有报错。

### 2.5 保存当前服务清单

```bash
cscode ps -a > "$ICS_CODE_BACKUP/services.before.txt"
```

**说明：** 记录更新前哪些应用已部署、哪些在运行，后续只更新原来启用的应用。

**预期：** 命令成功，清单文件已生成。

### 2.6 保存两个数据库的容器编号

```bash
cscode ps -q postgres archive-postgres > "$ICS_CODE_BACKUP/databases.before.txt"
```

**说明：** 用于更新后核对本次没有重建数据库容器。这里仅查询容器编号。

**预期：** 命令成功；第 1.4 步已确认两个数据库都正常运行。

### 2.7 给当前 ICS 镜像保留回退标签

```bash
docker image tag "$(docker inspect "$(cscode ps -q ics-agent)" --format '{{.Image}}')" "ics-agent:code-before-ics-$ICS_CODE_STAMP"
```

**说明：** 保留当前正在运行的实际镜像，防止构建新镜像后只剩下新版本标签。

**预期：** 成功时通常没有输出。容器或镜像查不到时，停在此处处理。

### 2.8 保留接收器旧镜像，仅在接收器已运行时执行

```bash
docker image tag "$(docker inspect "$(cscode ps -q quality-receiver)" --format '{{.Image}}')" "ics-agent:code-before-receiver-$ICS_CODE_STAMP"
```

**说明：** 单独保留接收器当前实际使用的镜像。

**预期：** 成功时通常没有输出；接收器原来未启用时跳过本步。

### 2.9 保留解析进程旧镜像，仅在解析进程已运行时执行

```bash
docker image tag "$(docker inspect "$(cscode ps -q quality-worker)" --format '{{.Image}}')" "ics-agent:code-before-worker-$ICS_CODE_STAMP"
```

**说明：** 单独保留解析进程当前实际使用的镜像。

**预期：** 成功时通常没有输出；解析进程原来未启用时跳过本步。

### 2.10 备份当前运行目录的代码和部署文件

```bash
tar --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='knowledge_base_in/output' --exclude='knowledge_base_in/vector_store' \
  --exclude='reminder/build' --exclude='reminder/dist' \
  -czf "$ICS_CODE_BACKUP/code.before.tar.gz" -C /opt/cs \
  ics_agent quality statistics knowledge_base_in reminder deploy docs \
  Dockerfile docker-compose.yml docker-compose.archive.yml .dockerignore requirements.txt .env.example
```

**说明：** 保存将被代码同步替换的文件，不把模型、数据库卷和知识库生成数据打入代码备份。

**预期：** 归档成功，没有目录缺失、文件变化、权限或磁盘错误。

### 2.11 检查代码备份可读取

```bash
tar -tzf "$ICS_CODE_BACKUP/code.before.tar.gz" >/dev/null
```

**说明：** 确认备份文件至少能完整列出内容。

**预期：** 命令成功结束，没有报错。

## 3. 从 Git 拉取 test 分支

### 3.1 进入源码仓库

```bash
cd /opt/cs-src
```

**说明：** Git 操作在源码仓库执行，不在 `/opt/cs` 运行目录中执行。

**预期：** 切换目录成功。

### 3.2 检查源码工作区

```bash
git status --short
```

**说明：** 确认服务器源码没有尚未处理的本地修改。

**预期：** 没有输出。有输出时先核实并保存修改，不用强制重置或清理覆盖它们。

### 3.3 查看远端名称

```bash
git remote
```

**说明：** 确认服务器这个仓库实际使用的远端名称。

**预期：** 看到 `chatwoot` 或服务器原来配置的名称，例如 `origin`。

### 3.4 设置远端变量

```bash
ICS_CODE_REMOTE=chatwoot
```

**说明：** 如果上一步实际输出的远端为 `origin`，本步改为 `ICS_CODE_REMOTE=origin`。后续命令不再改动。

**预期：** 变量设置成功，通常没有输出。

### 3.5 切换到 test 分支

```bash
git switch test
```

**说明：** 本次只从 `test` 分支上线。

**预期：** 显示切换成功或已经位于 `test`。本地不存在该分支或切换失败时，先处理仓库状态。

### 3.6 拉取远端 test

```bash
git pull --ff-only "$ICS_CODE_REMOTE" test
```

**说明：** 仅允许快进更新，避免在服务器上自动生成合并提交。

**预期：** 成功更新，或显示 `Already up to date.`。如果出现网络、权限、分支分叉或无法快进错误，停在本步。

### 3.7 再次确认当前分支

```bash
git branch --show-current
```

**说明：** 核实拉取后的实际工作分支。

**预期：** 输出必须是 `test`。

### 3.8 核对本地和远端提交号

```bash
git rev-parse HEAD "refs/remotes/$ICS_CODE_REMOTE/test"
```

**说明：** 第一行是服务器源码当前提交，第二行是本次拉取后的远端 `test` 提交。还应与开发端推送后记录的目标 SHA 一致。

**预期：** 两行完整 SHA 完全相同，且是准备上线的版本。仅看到 `Already up to date.` 不能替代本步核对。

### 3.9 检查是否包含数据库迁移变化

```bash
diff -qr /opt/cs/quality/database/migrations /opt/cs-src/quality/database/migrations
```

**说明：** 在同步覆盖运行目录之前，对比本次源码和当前部署目录的迁移文件。这里不会执行 SQL。

**预期：** 没有输出。出现新增、删除或内容不同的迁移文件时，不继续按“只更新代码”发布；先核对该版本的数据库升级要求。此检查以前提“原部署所需迁移已完成”为基础，不替代数据库升级验收。

## 4. 同步代码到运行目录

### 4.1 执行代码同步

```bash
bash /opt/cs-src/deploy/server/sync-code.sh /opt/cs-src /opt/cs
```

**说明：** 把源码同步到实际构建目录，包含管理页面、图标、Python 代码、部署文件和服务器专用 Dockerfile。脚本本身不创建发布锁，也不执行数据库迁移。

**预期：** 最后显示：

```text
ICS code and quality migrations synchronized; runtime configuration and data preserved.
```

这里的 `migrations synchronized` 只表示复制迁移文件，不表示执行迁移。脚本保留服务器 `.env`、`database/`、`models/`、日志、知识库生成目录和数据库卷。

### 4.2 确认实际配置未被代码同步覆盖

```bash
cmp -s "$ICS_CODE_BACKUP/env.before" /opt/cs/.env
```

**说明：** 将同步前后的实际 `.env` 逐字节比较。

**预期：** 命令退出码为 `0`，通常没有输出。下一步查看退出码。

### 4.3 查看配置比较结果

```bash
echo $?
```

**说明：** 必须紧接上一步执行，中间不要执行其他命令。

**预期：** 输出 `0`。非 `0` 时先查明 `.env` 为什么发生变化。

### 4.4 再次验证 Compose

```bash
cscode config --quiet
```

**说明：** 检查同步后的 Compose 与服务器现有配置是否匹配。

**预期：** 成功结束，没有缺少变量或配置错误。出现新必填变量时先按该版本说明处理。

## 5. 可选：同时将 JWT 有效期设为 7 天

只更新代码、不修改登录有效期时，跳过本节。如果已在 `/opt/cs/.env` 设置过 7 天，可直接核对第 5.2 步。

### 5.1 编辑服务器实际配置

```bash
nano /opt/cs/.env
```

**说明：** 找到并修改下面这一项；不存在时再新增，避免重复定义：

```dotenv
ADMIN_ACCESS_TOKEN_EXPIRE_SECONDS=604800
```

保留原有 `ADMIN_JWT_SECRET` 和其他配置。`Ctrl+O` 保存、回车确认，`Ctrl+X` 退出。

**预期：** 文件已保存。此时旧容器尚未重新创建，新有效期还没有加载到旧进程中。

### 5.2 核对有效期配置

```bash
grep -n '^ADMIN_ACCESS_TOKEN_EXPIRE_SECONDS=' /opt/cs/.env
```

**说明：** 只查看有效期这一项，不输出整份 `.env`。

**预期：** 只有一行有效配置，数值是 `604800`。

## 6. 构建镜像并分别更新应用

以下三个应用共用镜像，只构建一次。全部 `up` 命令明确指定应用名称并使用 `--no-deps`，不借本次更新启动或重建数据库。

本节不运行 `release.sh`、数据库迁移命令、`docker compose down` 或不带服务名的全项目 `up`。

### 6.1 构建新镜像

```bash
cscode build ics-agent
```

**说明：** 从同步后的 `/opt/cs` 构建 `ics-agent:py310`，将新页面、图标和后端查询代码写入镜像。依赖未变化时通常复用构建缓存，不使用 `--no-cache`。

**预期：** 构建成功。构建期间原应用继续使用旧镜像运行；构建失败时停在本步，不继续更新容器。

### 6.2 查看新镜像编号

```bash
docker image inspect ics-agent:py310 --format '{{.Id}}'
```

**说明：** 记录本次构建的镜像编号，后续核对应用实际使用的版本。

**预期：** 输出一个 `sha256:` 开头的镜像 ID，没有镜像不存在的错误。

### 6.3 更新原来已启用的归档接收器

```bash
cscode up -d --no-deps --force-recreate --wait --wait-timeout 90 quality-receiver
```

**说明：** 接收器原来已运行时执行；否则跳过。重新创建接收器，等待其健康检查通过。更新时该接收入口会有短暂中断。

**预期：** 命令成功完成，接收器达到 `healthy`。失败时先查看 `cscode logs --tail 100 quality-receiver`，不要继续下一应用。

### 6.4 更新原来已启用的归档解析进程

```bash
cscode up -d --no-deps --force-recreate quality-worker
```

**说明：** 解析进程原来已运行时执行；否则跳过。它会使用新镜像继续消费已有持久化任务。

**预期：** 容器重新创建并启动，没有配置或连接错误。

### 6.5 检查解析进程日志

```bash
cscode logs --tail 100 quality-worker
```

**说明：** 仅在执行第 6.4 步后检查，确认解析进程能正常启动和继续处理队列。空闲时日志较少也正常。

**预期：** 没有持续重启、密码认证失败、缺少表或字段等错误。出现数据库结构不匹配时停止本次代码更新，不临时修改数据或旧迁移 SQL。

### 6.6 更新 ICS 主应用

```bash
cscode up -d --no-deps --force-recreate ics-agent
```

**说明：** 重新创建主应用，使其同时加载新镜像和 `/opt/cs/.env`。如果第 5 节修改了 JWT 有效期，本步会一并加载。单独执行 `restart` 不能替代本步。

**预期：** 主应用重新创建并启动。随后需要等待模型及应用初始化完成，不能只凭 `Started` 判断业务已经可用。

### 6.7 查看所有服务状态

```bash
cscode ps -a
```

**说明：** 与更新前的服务清单对照，确认本次更新的应用正常运行。

**预期：** `ics-agent`、已更新的解析进程为 `Up`；已更新的接收器为 `healthy`；两个数据库仍为 `healthy`，未启用的可选服务没有被启动。

### 6.8 核对实际运行镜像

```bash
cscode images
```

**说明：** 查看应用容器实际使用的镜像 ID，与第 6.2 步的新镜像 ID 对照。该命令可能显示缩写 ID，用其前缀核对。

**预期：** 所有本次更新的应用都使用新镜像，不存在主应用已更新而本次应更新的接收器、解析进程仍使用旧镜像的情况。

### 6.9 核对数据库容器编号没有变化

```bash
diff -u "$ICS_CODE_BACKUP/databases.before.txt" <(cscode ps -q postgres archive-postgres)
```

**说明：** 对比本次更新前后的两个数据库容器编号。

**预期：** 没有输出，表示数据库容器没有被重建。如果编号发生变化，先查清原因，不把本次操作记为正常完成。

## 7. 更新后的接口与页面手测

### 7.1 查看 ICS 启动日志

```bash
cscode logs --tail 150 ics-agent
```

**说明：** 等待应用初始化、渠道加载和模型预热，检查是否有持续报错。

**预期：** 应用启动完成，没有持续重启、数据库认证失败或缺少模块等错误。若控制台日志较少，结合服务器 `/opt/cs/log/` 下最新应用日志核对。

### 7.2 检查 ICS 健康接口

```bash
cscode exec -T ics-agent python -c 'import os,urllib.request; print(urllib.request.urlopen("http://127.0.0.1:"+os.environ.get("PORT","3001")+"/health",timeout=10).status)'
```

**说明：** 从容器内检查主应用是否已能响应。初始化未完成时，等待启动日志完成后重试本步。

**预期：** 输出 `200`。这个结果只证明接口可响应，下面仍需实际登录和业务手测。

### 7.3 核对容器加载的 JWT 有效期，仅在本次调整有效期时执行

```bash
cscode exec -T ics-agent python -c 'from ics_agent.app.core.config import get_settings; print(get_settings().admin_access_token_expire_seconds)'
```

**说明：** 查看应用实际加载的值，不只检查服务器文件。

**预期：** 输出 `604800`。

### 7.4 验证后台登录

在浏览器打开：

```text
http://172.20.48.12:3001/admin
```

**说明：** 使用原有管理员账号登录。若原来已登录、且本次修改了 JWT 有效期，退出后再登录一次，以取得新有效期的凭证。

**预期：** 登录成功，后台页面正常。如果第 5 节设为了 7 天，新 JWT 从这次登录起按 7 天计算；之前已经签发的 JWT 不会自动延长。

### 7.5 验证收藏夹图标

**操作：** 页面使用 `Ctrl+F5` 刷新，查看浏览器标签上的图标，再查看收藏夹。旧收藏图标未刷新时，删除该收藏后重新添加。

**说明：** 本次 `test` 已包含图标改动时执行本项。

**预期：** 标签及新建收藏显示蓝色站点图标。

### 7.6 单独检查图标地址

```bash
curl -fsS -o /dev/null -w '%{http_code} %{content_type}\n' http://172.20.48.12:3001/admin/favicon.svg
```

**说明：** 本次包含图标改动时，用于区分服务器资源问题和浏览器缓存问题。

**预期：** 输出 `200 image/svg+xml`，或包含该媒体类型的结果；不能是 `404`。

### 7.7 验证 H5 用户 ID 完整检索

**操作：** 进入“对话归档”，重置其他筛选，在“用户”输入 `85704084`，点击“查询归档”。打开匹配客户详情，查看“H5 用户 ID（userId）”。

**说明：** 本次 `test` 已包含 H5 检索修正时执行。应使用归档中实际已有的 H5 用户；如果没有 `85704084` 的归档，换成详情页已记录的另一个 H5 ID。

**预期：** 能找到该 H5 ID 对应的用户，不要求该值等于 Chatwoot 联系人 ID；详情中的 H5 ID 与输入对应。

### 7.8 验证 H5 用户 ID 部分检索

**操作：** 对于已确认存在的 `85704084`，把“用户”改为 `7040`，重新查询。

**说明：** 确认 H5 ID 沿用模糊匹配方式。若上一步使用其他 H5 ID，本步选取那个 ID 的一部分查询。

**预期：** 结果仍包含该用户；也可能包含其他同样含有 `7040` 的匹配用户，不要求结果只有一条。

### 7.9 验证不同浏览方式和筛选导出

**操作：** 用完整 H5 ID 筛选后，分别查看客户列表、轮次列表、展开概况，并导出筛选结果。

**说明：** 用户筛选应在各入口使用一致的匹配条件；概况统计的是匹配轮次，客户卡片的累计消息数仍按原有完整历史规则展示。

**预期：** 各入口都能筛选到对应客户记录；导出能够成功生成并打开，内容与筛选范围对应。归档暂无轮次或还有待解析任务时，先确认处理进度，不直接以数量差异判断检索失败。

### 7.10 验证原有姓名和联系人 ID 查询

**操作：** 分别用该客户的姓名、Chatwoot 联系人 ID 重新筛选。

**说明：** 这两种原有检索方式继续保留。

**预期：** 两种方式仍可找到对应的已归档记录。

### 7.11 做一次原有业务回归

**操作：** 从原有测试入口发起咨询，分别核对 FAQ / 知识库回答、转人工及原本已经启用的提醒；归档接收器已启用时，确认新测试消息随后能在后台归档中查到。

**说明：** 确认更新后现有业务链路仍可用。不要仅以 `/health` 为更新成功的依据。

**预期：** 原有功能正常，新消息能继续进入原有归档流程，没有持续积压或后台不可用提示。

## 8. 验收后记录上线版本

### 8.1 写入已验收的源码提交号

```bash
git -C /opt/cs-src rev-parse HEAD > /opt/cs/deployment-records/source-commit.txt
```

**说明：** 仅在计划更新的应用已使用新镜像、且前面验收完成后记录。构建或业务验收失败时，不提前把目标提交记成已上线成功。

**预期：** 文件写入成功。

### 8.2 查看上线记录

```bash
cat /opt/cs/deployment-records/source-commit.txt
```

**说明：** 核对最终记录。

**预期：** 与第 3.8 步确认的 `test` 提交 SHA 完全一致。

## 操作中断时如何处理

| 中断位置 | 处理方式 |
| --- | --- |
| Git 拉取失败或 SHA 不符 | 停在源码阶段，先处理仓库状态，不继续同步或构建。 |
| 迁移文件有变化 | 本次已超出“只更新代码”前提，改按该版本数据库升级说明操作。 |
| 同步后 `.env` 比较不一致 | 先核对变更原因；备份在本次目录的 `env.before`，不要直接用模板覆盖。 |
| 镜像构建失败 | 原运行容器尚未因构建而替换，先解决构建问题，成功后再进入容器更新步骤。 |
| 某应用更新后异常 | 停在该应用，查看它的状态和日志。旧镜像已分别保留 `code-before-*` 标签，旧代码位于 `code.before.tar.gz`。不要用清空数据卷或重建数据库排错。 |
| SSH 断线 | 重新 `ssh ics`、定义 `cscode`，将 `ICS_CODE_STAMP` 和 `ICS_CODE_BACKUP` 恢复为本次记录的值，再从中断步骤核对后继续。 |

本手册只提供操作步骤。编写手册时没有连接 `ssh ics` 执行服务器更新。
