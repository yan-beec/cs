# 统计报表服务器增量更新手册

适用本次后台更新：统计报表下的“时间统计”“坐席细化”“质检统计”，以及对应的筛选、重置、图表、表格和指标说明，同时补齐服务器尚未完成的既有归档迁移。操作顺序参考 [对话归档与客服状态更新手册](archive-feature-update.md)，沿用 **Git 仓库 `test` 分支拉取发布**，保留服务器配置，只增量更新应用代码及归档数据库。

本手册只说明操作，尚未在目标服务器执行。示例沿用 SSH 别名 `ics`、Git 源码目录 `/opt/cs-src`、运行目录 `/opt/cs`、Compose 项目名 `cs`；如实际不同，先调整第三节对应参数。Git 拉取只更新源码目录，随后将指定应用代码同步到运行目录。

## 一、本次更新边界

| 项目 | 本次处理 |
| --- | --- |
| `statistics/` | 增加统计逻辑和原生 SVG 图表、表格资源 |
| `ics_agent/` | 更新后台菜单、入口、统计路由及配套代码，包含新增的 `ics_agent/statistics.py` |
| `quality/` 应用代码 | 同步配套查询代码；保留服务器原有 `quality/database/` 和 `quality/deploy/` |
| `.env`、其他环境文件、现有 Compose 及覆盖文件 | 保留，不用仓库配置替换它们 |
| `requirements.txt`、服务器原 Dockerfile、`.dockerignore` | 保留；只为本次构建生成一份额外的 Dockerfile |
| 渠道、模型、知识库、提示词、业务资料、日志 | 保留，不在本次同步范围 |
| PostgreSQL、归档数据库、Webhook、接收器 | 沿用现有部署，不重新建库、不修改既有角色密码、不重建这些服务 |
| 解析进程 `quality-worker` | 需要迁移时暂停，升级后仅恢复原本运行的实例 |
| 新依赖、统计专用环境变量、统计专用数据库迁移 | 均不需要 |

其中归档库需要按本手册检查并补齐既有 `001–009`：已应用且校验一致的版本跳过，只执行缺失版本。需要迁移时，在新镜像构建成功后暂停 ICS 和原本运行的 worker；迁移验证通过后更新 ICS，并恢复该 worker。两套数据库和接收器保持运行，不删除或重新初始化数据卷。

**整个流程不使用 `sudo`，不读取或保存系统提权密码。** 发布用户须已能读写应用代码目录、读取现有配置，并直接使用 Docker。权限不足时检查失败即停止，由管理员另行处理权限；不临时改成全目录开放权限，不用提权容器代替宿主机文件授权。SSH 登录认证仍沿用现有方式，与系统提权密码不同。

不要直接照搬旧文档的全量同步段落，也不要在本次执行 `deploy/server/release.sh` 或 `sync-code.sh`：现有通用入口会复制 Compose、依赖文件等，并可能执行数据库迁移，与本次保留全部现有配置的要求不同。

### 必须先具备的条件

1. 服务器已部署归档与客服状态模块，ICS、归档库、接收器和解析进程原本工作正常。
2. 独立归档库已初始化角色与 schema，至少已完成 `001_capture`。不要求事先完成全部迁移，第四节会识别当前版本，第6.2节补齐至 `009_dual_round_reviews`；本次没有新增第 `010` 个迁移。
3. 现有 `ARCHIVE_QUERY_ENABLED=true`、归档查询凭据与连接已可用。坐席在线曲线和发配量分别依赖原有状态采样、成功分配记录；缺失的历史记录不能由报表补采。
4. 服务器使用后端镜像部署，构建目录为 `/opt/cs`，应用位于容器 `/app`。生产容器没有把宿主机的应用源码挂载到 `/app/ics_agent`、`/app/quality`、`/app/statistics`，也没有整目录挂载 `/app`。

如果服务器仍只有 `001–007`，本流程新增 `008`、`009`；只有 `001–008` 时新增 `009`；已有完整 `001–009` 时核对后跳过迁移。更早的连续版本同样按编号补齐。迁移历史缺失、版本断档、出现未知版本或校验不一致时停止核实，不伪造历史、不删库重做。

## 二、Windows：检查并推送 Git 的 test 发布分支

在 **Windows PowerShell** 执行，使用指定的 `cs` 环境。先按现有项目流程，将本次确认发布的代码及完整归档迁移提交、合并到 `test` 分支，再在该分支执行以下检查。不要直接把包含无关改动的工作区切换、重置或全部提交。

本项目开发机的远端名称为 `chatwoot`；其他开发机先核对实际远端名称。服务器只能拉到已经提交并推送的内容。

```powershell
Set-Location D:\project\CS
git branch --show-current
git status --short
git remote -v
if ((git branch --show-current).Trim() -ne 'test') { throw '请先按现有流程准备好test发布分支，停止。' }
if (git status --porcelain) { throw '工作区仍有未提交修改，停止发布。' }
git diff --check
if ($LASTEXITCODE -ne 0) { throw '差异检查未通过，停止。' }
$statisticsReleaseCommit = (git rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw '读取发布提交失败，停止。' }

$statisticsTestStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
& 'C:\Users\lenovo\Anaconda3\envs\cs\python.exe' -m pytest `
  statistics/tests/test_statistics_reports.py `
  statistics/tests/test_statistics_ui.py `
  quality/tests/test_admin_ui.py `
  -q -p no:cacheprovider --basetemp "tmp/pytest-statistics-release-$statisticsTestStamp"
if ($LASTEXITCODE -ne 0) { throw '统计或后台检查失败，停止。' }

# 检查迁移入口的Linux换行，只检查Shell，不改SQL字节。
& 'C:\Users\lenovo\Anaconda3\envs\cs\python.exe' -c "from pathlib import Path; paths=[Path('quality/database/apply-migrations.sh'),Path('quality/database/verify-schema.sh')]; bad=[str(p) for p in paths if b'\r' in p.read_bytes()]; print('Shell CRLF files:',bad); raise SystemExit(bool(bad))"
if ($LASTEXITCODE -ne 0) { throw '迁移Shell存在CRLF，修正后重新检查。' }

if ((git branch --show-current).Trim() -ne 'test' -or
    (git rev-parse HEAD).Trim() -ne $statisticsReleaseCommit -or
    (git status --porcelain)) { throw '检查期间源码发生变化，重新检查后再发布。' }
git push chatwoot test
if ($LASTEXITCODE -ne 0) { throw '推送失败，停止发布。' }
"本次发布完整Git提交SHA：$statisticsReleaseCommit"
```

保存输出的完整 Git 提交 SHA，第四节拉取后必须核对一致。测试中的前端交互检查使用开发机现有 Node；不因此给服务器安装 Node。数据库聚合测试须在隔离测试库执行，不能指向生产库；上面的命令不包含数据库集成测试。

发布提交须包含 `ics_agent/`、`quality/`、`statistics/` 的配套更新；`quality/database/` 中须有完整 `001–009` 和迁移、验证脚本。第四节校验历史 SQL 的原始字节，第6.2节从 Git 源码目录复制到归档容器的独立临时目录执行，不覆盖生产原迁移目录。不要对已应用 SQL 批量转换换行。仓库内即使有环境、Compose、构建模板的更新，第五节也不把它们同步到运行目录。

## 三、服务器：检查权限并沿用实际部署配置

登录后进入 **Bash**，后续服务器代码块在同一个 Bash 会话中按顺序执行。不要在 Windows PowerShell 中粘贴服务器命令。

```powershell
ssh ics
```

下面开始为 **服务器 Bash**：

```bash
bash
set -euo pipefail
umask 077

statistics_target=/opt/cs
statistics_source=/opt/cs-src
statistics_project=cs
cd "$statistics_target"
for statistics_tool in git docker python3 rsync tar sha256sum flock; do
  command -v "$statistics_tool" >/dev/null || { echo "缺少工具：$statistics_tool"; exit 1; }
done
test -r .env && test -w .
docker info >/dev/null
docker compose version

# 与通用发布入口共用锁；保留当前会话，直到验收或回退结束。
exec 9>"$statistics_target/.deployment.lock"
flock -n 9 || { echo '已有发布进行中，停止。'; exit 1; }

statistics_container=$(docker ps \
  --filter "label=com.docker.compose.project=$statistics_project" \
  --filter 'label=com.docker.compose.service=ics-agent' --format '{{.ID}}')
test -n "$statistics_container"
test "$(printf '%s\n' "$statistics_container" | wc -l)" -eq 1

# 从正在运行的容器取得全部 Compose 文件及顺序，避免漏掉资源限制等覆盖文件。
statistics_config_list=$(docker inspect -f \
  '{{index .Config.Labels "com.docker.compose.project.config_files"}}' "$statistics_container")
test -n "$statistics_config_list" && test "$statistics_config_list" != '<no value>'
IFS=',' read -r -a statistics_config_files <<< "$statistics_config_list"
statistics_compose=(docker compose --project-directory "$statistics_target" \
  --env-file "$statistics_target/.env" -p "$statistics_project")
for statistics_config in "${statistics_config_files[@]}"; do
  test -f "$statistics_config" && test -r "$statistics_config"
  statistics_compose+=(-f "$statistics_config")
done
"${statistics_compose[@]}" config --quiet
"${statistics_compose[@]}" --profile quality-capture --profile quality-query ps -a

statistics_stamp=$(date +%Y%m%d-%H%M%S)
statistics_record="$HOME/cs-backups/statistics-$statistics_stamp"
mkdir -p "$statistics_record"
chmod 700 "$statistics_record"

# 完整合并配置可能含密钥，不输出到终端；只提取后续需要的非敏感构建信息。
"${statistics_compose[@]}" config --format json | python3 -c '
import json, pathlib, sys
c = json.load(sys.stdin)
s = c["services"]["ics-agent"]
b = s.get("build")
assert isinstance(b, dict), "缺少服务器现有构建配置"
root = pathlib.Path(sys.argv[1]).resolve()
assert pathlib.Path(b["context"]).resolve() == root, "构建目录不同，需先核对部署方式"
assert not b.get("dockerfile_inline"), "当前为内联 Dockerfile，不能套用本流程"
f = pathlib.Path(b.get("dockerfile", "Dockerfile"))
f = f if f.is_absolute() else root / f
assert f.is_file(), "原 Dockerfile 不存在"
code_roots = tuple(pathlib.PurePosixPath(p) for p in
    ("/app/ics_agent", "/app/quality", "/app/statistics"))
for service in ("ics-agent", "quality-receiver", "quality-worker"):
    for v in c["services"].get(service, {}).get("volumes", []):
        t = pathlib.PurePosixPath(v["target"])
        assert not any(t == p or t in p.parents or p in t.parents for p in code_roots), f"{service}存在源码挂载，停止"
image = s.get("image", "")
assert image and "@" not in image, "需要可构建的原镜像标签"
print(image)
print(f.resolve())
' "$statistics_target" > "$statistics_record/build-info.txt"
mapfile -t statistics_build_info < "$statistics_record/build-info.txt"
statistics_image=${statistics_build_info[0]}
statistics_dockerfile=${statistics_build_info[1]}
statistics_old_image=$(docker inspect -f '{{.Image}}' "$statistics_container")
test "$(docker image inspect -f '{{.Id}}' "$statistics_image")" = "$statistics_old_image"

# 仅检查现有功能开关是否启用，不输出环境变量或密码。
"${statistics_compose[@]}" exec -T ics-agent python -c '
from quality.query.config import QuerySettings
s = QuerySettings()
assert s.query_enabled, "归档查询尚未启用，停止"
s.connection_kwargs
print("PASS: 现有归档查询配置已启用")
'

# 后续统一通过现有归档容器访问数据库，禁止交互式数据库密码提示。
statistics_archive_psql() {
  "${statistics_compose[@]}" exec -T archive-postgres sh -c \
    'exec psql -X -w -U archive_admin -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 "$@"' sh "$@"
}
```

`docker info` 若权限不足会直接报错，不会出现系统提权密码提示。不要临时在命令前加提权指令。若实际使用其他 `--env-file`、多个环境文件或外部注入的 Compose 插值变量，先把 `statistics_compose` 数组改为日常发布的真实调用方式；仅 `.env` 文件未改，不代表重新创建容器时可以遗漏原调用参数。不要打印完整 `docker compose config` 或完整 `docker inspect`。

确认列表中的原主库、归档库、receiver、worker 都处于原本的正常状态。某服务原本异常时先处理，不借本次统计发布重建数据库。

### 只读查看当前数据库版本

这里确认已有归档基线并显示实际版本，不要求现在已经有009：

```bash
statistics_archive_psql <<'SQL'
DO $check$
BEGIN
  IF to_regclass('ops.schema_migrations') IS NULL THEN
    RAISE EXCEPTION '迁移历史不存在，需先核实既有归档库的初始化基线';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM ops.schema_migrations WHERE version='001_capture') THEN
    RAISE EXCEPTION '001_capture尚未完成，不属于本手册的已有归档库增量升级';
  END IF;
END
$check$;
SELECT version,applied_at FROM ops.schema_migrations ORDER BY version;
SQL
```

记录显示的版本，继续第四节。这里使用现有归档容器内的数据库管理员读取历史；`-w` 禁止数据库密码交互，不修改角色或密码。只有到第6.2节才执行增量迁移。

## 四、拉取 Git 的 test 分支，核对提交并备份

源码目录与运行目录分开：只在 `/opt/cs-src` 拉取 `test`，不在 `/opt/cs` 执行 Git 覆盖运行配置。把下面提交 SHA 占位值替换为第二节开发机输出，不能保留占位文字继续执行。

拉取地址沿用原手册的仓库地址，不改服务器已保存的远端设置；Git 认证沿用现有方式，不在命令中填写密码或令牌。源码目录必须已在 `test` 分支且工作区干净；不满足时先处理，不自动切换分支、隐藏修改或强制重置。

```bash
statistics_expected_commit='替换为Windows输出的完整Git提交SHA'
[[ "$statistics_expected_commit" =~ ^[0-9a-f]{40}$ ]]
test "$statistics_source" != "$statistics_target"
test "$(git -C "$statistics_source" rev-parse --show-toplevel)" = "$statistics_source"
git -C "$statistics_source" status --short
git -C "$statistics_source" branch --show-current
test "$(git -C "$statistics_source" branch --show-current)" = test || {
  echo '服务器源码目录不在test分支，停止。'; exit 1;
}
test -z "$(git -C "$statistics_source" status --porcelain)" || {
  echo '服务器源码目录有未提交修改，停止。'; exit 1;
}
git -C "$statistics_source" rev-parse HEAD > "$statistics_record/source-before.txt"
git -C "$statistics_source" pull --ff-only https://code-sayu.xingyongshe.com/backend/chatwoot.git test
statistics_release_commit=$(git -C "$statistics_source" rev-parse HEAD)
test "$statistics_release_commit" = "$statistics_expected_commit" || {
  echo '拉取提交与开发机记录不一致，停止发布。'; exit 1;
}

# 同步和迁移前再次核对，防止会话中途源码被其他操作改变。
statistics_verify_source() {
  test "$(git -C "$statistics_source" branch --show-current)" = test
  test "$(git -C "$statistics_source" rev-parse HEAD)" = "$statistics_release_commit"
  test -z "$(git -C "$statistics_source" status --porcelain)"
}
statistics_verify_source
printf '%s\n' "$statistics_release_commit" > "$statistics_record/source-after.txt"
git -C "$statistics_source" log -1 --oneline

for statistics_file in \
  ics_agent/statistics.py ics_agent/app/main.py ics_agent/app/admin.html \
  quality/query/runtime.py statistics/routes.py statistics/repository.py \
  statistics/agent_quality.py statistics/quality_reports.py \
  statistics/web/reports.js statistics/web/reports.css \
  quality/database/apply-migrations.sh quality/database/verify-schema.sh \
  quality/database/verify-schema.sql; do
  test -f "$statistics_source/$statistics_file"
done

# 先核对全部历史版本及校验值；这一阶段不运行任何迁移。
statistics_archive_psql -At > "$statistics_record/versions-before.json" <<'SQL'
SELECT COALESCE(json_agg(t ORDER BY t.version), '[]'::json)
FROM (SELECT version,checksum_sha256 FROM ops.schema_migrations) t;
SQL
python3 - "$statistics_source" "$statistics_record" <<'PY'
from pathlib import Path
import hashlib, json, sys

source, record = Path(sys.argv[1]), Path(sys.argv[2])
expected = ["001_capture", "002_query", "003_assignment_writer", "004_agent_presence",
    "005_presence_retention", "006_presence_retention_state", "007_presence_agent_management",
    "008_round_reviews", "009_dual_round_reviews"]
files = sorted((source / "quality/database/migrations").glob("*.sql"))
assert [p.stem for p in files] == expected, "本次Git源码的001-009不完整或包含未知版本"
history = json.loads((record / "versions-before.json").read_text())
versions = [r["version"] for r in history]
assert versions and versions == expected[:len(versions)], "历史版本不连续、为空或出现未知版本，停止"
for p, row in zip(files, history):
    assert hashlib.sha256(p.read_bytes()).hexdigest() == row["checksum_sha256"], \
        f"{p.name}校验不一致，停止；不能修改数据库历史校验值"
for name in ("apply-migrations.sh", "verify-schema.sh"):
    assert b"\r" not in (source / "quality/database" / name).read_bytes(), f"{name}存在CRLF"
pending = expected[len(versions):]
(record / "needs-migration.txt").write_text("1" if pending else "0")
print("已应用：", ", ".join(versions))
print("待执行：", ", ".join(pending) if pending else "无，001-009已齐全")
PY
statistics_needs_migration=$(cat "$statistics_record/needs-migration.txt")

# 保存worker原状态；原本停止的worker不自动启用。
statistics_worker_was_running=0
if test -n "$("${statistics_compose[@]}" --profile quality-query ps --status running -q quality-worker)"; then
  statistics_worker_was_running=1
fi

# 记录writer角色是否已存在，用于迁移时保护它们现有的密码。
statistics_assignment_existed=$(statistics_archive_psql -At \
  -c "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='archive_assignment');")
statistics_presence_existed=$(statistics_archive_psql -At \
  -c "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='archive_presence');")
[[ "$statistics_assignment_existed" =~ ^(t|f)$ && "$statistics_presence_existed" =~ ^(t|f)$ ]]

# 备份只读应用代码，不访问可能由容器写成其他属主的模型、向量库等业务目录。
statistics_code_dirs=(ics_agent quality)
statistics_had_directory=0
if test -d "$statistics_target/statistics"; then
  statistics_code_dirs+=(statistics)
  statistics_had_directory=1
fi
tar --exclude=__pycache__ --exclude='*.pyc' -czf "$statistics_record/code-before.tar.gz" \
  -C "$statistics_target" "${statistics_code_dirs[@]}"
tar -tzf "$statistics_record/code-before.tar.gz" >/dev/null

statistics_rollback_image="ics-agent:before-statistics-$statistics_stamp"
docker image tag "$statistics_old_image" "$statistics_rollback_image"

# 配置备份仅保存在服务器私有目录；用哈希检查本次前后是否保持一致。
statistics_guard_files=("$statistics_target/.env" "$statistics_dockerfile" \
  "$statistics_target/requirements.txt" "$statistics_target/.dockerignore" \
  "${statistics_config_files[@]}")
for statistics_optional in "$statistics_target"/.env.* "$statistics_target/database/channels.json"; do
  if test -f "$statistics_optional"; then
    statistics_guard_files+=("$statistics_optional")
  fi
done
sha256sum "${statistics_guard_files[@]}" > "$statistics_record/config-before.sha256"
tar -czf "$statistics_record/config-before.tar.gz" -- "${statistics_guard_files[@]}"
docker ps --format '{{.ID}} {{.Names}} {{.Image}}' > "$statistics_record/containers-before.txt"

# 归档库一致性快照及角色备份；不备份、不改动原ICS主库。
"${statistics_compose[@]}" exec -T archive-postgres sh -c \
  'exec pg_dump -w -U archive_admin -d "$POSTGRES_DB" -Fc' \
  > "$statistics_record/archive-before.dump"
test -s "$statistics_record/archive-before.dump"
"${statistics_compose[@]}" exec -T archive-postgres pg_restore --list \
  < "$statistics_record/archive-before.dump" > "$statistics_record/archive-before.list"
"${statistics_compose[@]}" exec -T archive-postgres sh -c \
  'exec pg_dumpall -w -U archive_admin --roles-only' \
  > "$statistics_record/archive-roles-before.sql"
test -s "$statistics_record/archive-roles-before.sql"

# 只保存密码校验摘要用于前后核对，不打印密码或角色SQL。
statistics_archive_psql -At > "$statistics_record/role-passwords-before.json" <<'SQL'
SELECT json_object_agg(rolname,md5(COALESCE(rolpassword,'')))
FROM pg_authid WHERE left(rolname,8)='archive_';
SQL

# 保存恢复会话和回退需要的参数；这些参数不含密码。
declare -p statistics_target statistics_project statistics_record statistics_source \
  statistics_stamp statistics_image statistics_old_image statistics_rollback_image \
  statistics_dockerfile statistics_compose statistics_had_directory \
  statistics_expected_commit statistics_release_commit \
  statistics_needs_migration statistics_worker_was_running \
  statistics_assignment_existed statistics_presence_existed \
  > "$statistics_record/session.sh"
declare -f statistics_archive_psql >> "$statistics_record/session.sh"
declare -f statistics_verify_source >> "$statistics_record/session.sh"
printf '备份完成，记录目录：%s\n' "$statistics_record"
```

备份任一步失败都停止，旧 ICS 此时仍正常运行。不要跳过失败检查继续同步；不为读取整套业务资料而临时索取系统提权密码。配置、归档数据和角色备份含敏感内容，只保留在这个受限目录，不上传或提交 Git。`pg_restore --list`验证备份可读，不代替完整恢复演练。

记录打印的目录。如果 SSH 中断，重新进入 Bash 后执行以下三行，再继续尚未完成的步骤；将路径替换为实际记录目录。不要在发布已结束后重复执行第五节覆盖新的业务版本。

```bash
set -euo pipefail
source "$HOME/cs-backups/statistics-实际时间戳/session.sh"
exec 9>"$statistics_target/.deployment.lock"; flock -n 9
```

本节的 `archive-before.dump` 是迁移前归档库的一致性快照，`code-before.tar.gz` 和 `config-before.tar.gz` 分别是应用代码和配置备份。快照期间接收器继续写入，恢复快照会影响备份后的新记录，因此普通应用回退不直接导入这个数据库备份。

## 五、只同步应用代码，保留现有配置

预检已排除源码挂载，因此以下宿主机代码同步不会让旧容器在运行中读到一半的新代码。当前会话不要与其他发布、配置修改同时进行，也不要再次拉取或修改 `/opt/cs-src`。

```bash
statistics_verify_source
cd "$statistics_target"
for statistics_dir in ics_agent quality; do
  test -d "$statistics_target/$statistics_dir"
  test ! -L "$statistics_target/$statistics_dir"
  test -w "$statistics_target/$statistics_dir"
done
test ! -L "$statistics_target/statistics"

# 使用内容比较，避免相同时间戳导致漏更新；不进行整目录清理。
# 仅公开应用代码使用普通读取权限；备份、配置和会话文件仍沿用私有权限。
(
  umask 022
  mkdir -p "$statistics_target/statistics"
  rsync -rc --exclude='__pycache__/' --exclude='*.pyc' \
    "$statistics_source/ics_agent/" "$statistics_target/ics_agent/"
  rsync -rc --exclude='__pycache__/' --exclude='*.pyc' \
    --exclude='/database/' --exclude='/deploy/' \
    "$statistics_source/quality/" "$statistics_target/quality/"
  rsync -rc --exclude='__pycache__/' --exclude='*.pyc' \
    "$statistics_source/statistics/" "$statistics_target/statistics/"
)

sha256sum -c "$statistics_record/config-before.sha256"
```

应全部显示 `OK`。这三次同步不复制项目根目录，不写 `.env`、现有 Compose、依赖、构建配置、渠道 JSON 或业务数据；也不会执行数据库迁移。任一步失败时，旧容器仍保留旧镜像，可按第八节恢复宿主机代码后再排查。

## 六、先构建，再补齐归档迁移并更新应用

### 6.1 沿用服务器原构建参数，先构建新镜像

服务器旧 Dockerfile 可能还没有 `COPY statistics`。下面保留原文件，仅在私有发布目录生成一份临时构建文件，在末尾补上复制统计目录的指令。原 Dockerfile 的镜像基底、依赖安装、启动命令和环境设置均沿用；本次不替换 `requirements.txt`。

```bash
python3 - "$statistics_dockerfile" "$statistics_record" <<'PY'
from pathlib import Path
import json, sys

original, record = Path(sys.argv[1]), Path(sys.argv[2])
content = original.read_bytes()
assert b"COPY quality" in content and b"COPY ics_agent" in content, "请先核对服务器原构建文件"
temporary = record / "Dockerfile.statistics"
temporary.write_bytes(content + b"\nCOPY statistics /app/statistics\n")
# 如原构建文件有专用忽略规则，也沿用它；否则继续使用原根目录 .dockerignore。
ignore = Path(str(original) + ".dockerignore")
if ignore.is_file():
    Path(str(temporary) + ".dockerignore").write_bytes(ignore.read_bytes())
override = {"services": {"ics-agent": {"build": {"dockerfile": str(temporary)}}}}
(record / "build-override.json").write_text(json.dumps(override), encoding="utf-8")
PY

# 额外文件只覆盖 build.dockerfile，原构建参数、镜像名及所有生产覆盖配置继续合并。
"${statistics_compose[@]}" -f "$statistics_record/build-override.json" config --quiet
"${statistics_compose[@]}" -f "$statistics_record/build-override.json" build ics-agent

# 新建临时容器只检查模块和资源，不启动 ICS，不对外开放端口。
"${statistics_compose[@]}" run --rm -T --no-deps --pull never --entrypoint python ics-agent -c '
from pathlib import Path
import statistics
from ics_agent.statistics import routes, quality_reports
assert hasattr(statistics, "mean"), "标准库statistics被遮蔽"
assert (routes.WEB / "reports.js").is_file()
assert (routes.WEB / "reports.css").is_file()
assert Path("/app/statistics/quality_reports.py").is_file()
print("PASS: 新镜像包含统计接口、质检统计和前端资源")
'
sha256sum -c "$statistics_record/config-before.sha256"
```

构建期间旧 ICS 保持运行。构建或模块检查失败时，停在这里，不开始数据库迁移、不停止旧服务。服务器原 Dockerfile 保持原样，因此**以后重新全量构建时，也要使用包含统计目录的构建文件或沿用本节临时构建方式**。

### 6.2 补齐既有001–009，并验证结构、权限及登录

本节无论是否缺版本都执行：有缺失时暂停 ICS 和原本运行的 worker 后补齐；没有缺失时仅验证，不停止它们。receiver 继续接收原始归档事件，两个数据库不重建。暂停 worker 期间解析会积压，恢复后处理已保存任务；ICS 自身接待、分配同步和状态采样在停机期间不能保证连续，应安排维护时间。

| 迁移 | 作用 |
| --- | --- |
| 001 | 原始事件、消息/会话快照、持久化任务与迁移历史 |
| 002 | 轮次、消息、坐席、评价等查询索引 |
| 003 | 成功分配记录的独立受限角色 |
| 004 | 客服状态样本、坐席监管范围及受限采样角色 |
| 005、006 | 状态历史保留和持久化清理预算 |
| 007 | 坐席管理标记与受限权限 |
| 008 | 持久化人工质检结果、备注和修改版本 |
| 009 | 独立AI/人工质检结果；原单项结果归入人工，AI默认待定，原备注及版本保留 |

不直接调用通用 `migrate.sh`，因为它固定使用两份 Compose，可能遗漏服务器额外覆盖配置；它调用的原迁移脚本还会按配置设置 writer 角色密码。本节复用已经核对的完整 Compose，并在**单次迁移进程中**移除既有 writer 角色对应的密码输入，从而保留它们原密码。只有这次新建的 writer 角色，才允许使用归档容器已有的真实配置值完成初始密码设置，不写 `.env`、不生成新密码。原来未启用的状态采样或分配同步不自动启用。

```bash
statistics_verify_source
test -s "$statistics_record/archive-before.dump"
test -s "$statistics_record/archive-roles-before.sql"
rm -f -- "$statistics_record/migration-verified.txt"
sha256sum -c "$statistics_record/config-before.sha256"

# 使用全新容器临时目录，原宿主机历史SQL和初始化目录均不覆盖。
statistics_migration_stage=$("${statistics_compose[@]}" exec -T archive-postgres \
  mktemp -d /tmp/ics-statistics-migrate.XXXXXX)
[[ "$statistics_migration_stage" =~ ^/tmp/ics-statistics-migrate\.[[:alnum:]]+$ ]]
printf '%s\n' "$statistics_migration_stage" > "$statistics_record/migration-stage.txt"
"${statistics_compose[@]}" cp "$statistics_source/quality/database/." \
  "archive-postgres:$statistics_migration_stage"

if test "$statistics_needs_migration" -eq 1; then
  if test "$statistics_worker_was_running" -eq 1; then
    "${statistics_compose[@]}" --profile quality-query stop quality-worker
  fi
  "${statistics_compose[@]}" stop ics-agent
  date -Is > "$statistics_record/migration-started.txt"

  # 参数只传目录与角色是否已存在，不传密码。进程内unset不修改容器或.env配置。
  if ! "${statistics_compose[@]}" exec -T archive-postgres sh -c '
    set -eu
    export PGPASSWORD="${POSTGRES_PASSWORD:?Missing existing archive admin password}"
    case "$2" in t) unset ARCHIVE_ASSIGNMENT_PASSWORD ;; f) : ;; *) exit 2 ;; esac
    case "$3" in t) unset ARCHIVE_PRESENCE_PASSWORD ;; f) : ;; *) exit 2 ;; esac
    sh "$1/apply-migrations.sh"
  ' sh "$statistics_migration_stage" "$statistics_assignment_existed" "$statistics_presence_existed" \
    > "$statistics_record/migration.log" 2>&1; then
    printf '迁移失败，保持ICS/worker停止，检查私有日志：%s/migration.log\n' "$statistics_record"
    exit 1
  fi
else
  printf '%s\n' '001-009已齐全，跳过应用迁移，继续验证。'
fi

# 验证脚本中的探测记录在事务中回滚；随后用现有密码检查真实TCP登录。
if ! "${statistics_compose[@]}" exec -T archive-postgres sh -c '
  set -eu
  export PGPASSWORD="${POSTGRES_PASSWORD:?Missing existing archive admin password}"
  sh "$1/verify-schema.sh"
' sh "$statistics_migration_stage" > "$statistics_record/migration-verify.log" 2>&1; then
  printf '验证失败，不启动新版应用；检查私有日志：%s/migration-verify.log\n' "$statistics_record"
  exit 1
fi

statistics_archive_psql -At > "$statistics_record/versions-after.json" <<'SQL'
SELECT COALESCE(json_agg(t ORDER BY t.version), '[]'::json)
FROM (SELECT version,checksum_sha256 FROM ops.schema_migrations) t;
SQL
statistics_archive_psql -At > "$statistics_record/role-passwords-after.json" <<'SQL'
SELECT json_object_agg(rolname,md5(COALESCE(rolpassword,'')))
FROM pg_authid WHERE left(rolname,8)='archive_';
SQL
python3 - "$statistics_source" "$statistics_record" <<'PY'
from pathlib import Path
import hashlib, json, sys

source, record = Path(sys.argv[1]), Path(sys.argv[2])
history = json.loads((record / "versions-after.json").read_text())
files = sorted((source / "quality/database/migrations").glob("*.sql"))
assert len(history) == len(files) == 9, "迁移记录不完整"
for p, row in zip(files, history):
    assert row["version"] == p.stem and row["checksum_sha256"] == hashlib.sha256(p.read_bytes()).hexdigest(), \
        f"{p.name}迁移后的记录与本次Git源码不一致"
before = json.loads((record / "role-passwords-before.json").read_text())
after = json.loads((record / "role-passwords-after.json").read_text())
assert all(after.get(name) == value for name, value in before.items()), "既有角色密码摘要变化，停止核实"
print("PASS: 001-009完整，全部SQL校验一致，既有角色密码保持不变")
PY
sha256sum -c "$statistics_record/config-before.sha256"
date -Is > "$statistics_record/migration-verified.txt"

# 清理仅限上面生成并校验过的容器临时目录；备份与日志保留。
"${statistics_compose[@]}" exec -T archive-postgres rm -rf -- "$statistics_migration_stage"
```

成功时 `migration.log` 中已应用项显示 `unchanged migration skipped`，缺失项显示 `Applying ...`；验证日志末尾包含 `PASS: capture schema, append-only evidence, task permissions and account authentication.`，终端显示上述完整版本和密码核对的 `PASS`。已全部迁移时不重新执行 SQL，但仍要通过验证。

出现 `checksum differs` 时，保持停止并核对服务器已应用文件及原发布提交对应的 SQL；不要转换 SQL 换行、修改历史校验值或强行跳过。密码认证失败时核对已有凭据与数据库是否一致，本流程不会通过覆盖密码来让检查通过。

每个 SQL 自身有事务，整组001–009不是一个总事务：中途失败，前面的版本可能已提交。保留记录目录和备份，修复原因后恢复第四节的同一会话，再执行第6.2节；它会使用新的临时目录，核对后跳过已完成版本。不要重新初始化归档卷，不导入本地测试库。迁移失败后不要继续第6.3节，也不要直接用旧镜像恢复质检写入。

### 6.3 验证通过后更新ICS，按原状态恢复worker

```bash
test -s "$statistics_record/migration-verified.txt"
sha256sum -c "$statistics_record/config-before.sha256"
"${statistics_compose[@]}" up -d --no-deps --no-build --pull never --force-recreate ics-agent
if test "$statistics_needs_migration" -eq 1 && test "$statistics_worker_was_running" -eq 1; then
  "${statistics_compose[@]}" --profile quality-query up -d --no-deps --no-build \
    --pull never --force-recreate quality-worker
fi
```

ICS 会短暂中断并重新预热模型。只有实际补迁移且 worker 原本运行时，才恢复该 worker 并使用同批新镜像；原本停止的不自动启用。receiver 与两个数据库保持原容器，归档采集的旧原始事件继续保留，不因本次升级丢弃。

receiver、worker 与 ICS 沿用同一个镜像标签：构建更新标签，不会自动重启已有容器。不要执行不带服务名的 `up`，也不要执行 `down`。009升级后应刷新所有已打开的后台页面，旧单项质检保存接口已撤销授权，不能继续使用旧页面提交。

## 七、检查运行结果和三个报表页面

### 7.1 等待 ICS 健康，确认配置保持原样

```bash
statistics_healthy=0
for statistics_attempt in $(seq 1 60); do
  if "${statistics_compose[@]}" exec -T ics-agent python -c '
import os, urllib.request
port = os.environ.get("PORT", "3001")
with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
    assert r.status == 200
' >/dev/null 2>&1; then
    statistics_healthy=1
    break
  fi
  sleep 5
done
test "$statistics_healthy" -eq 1 || { echo 'ICS健康检查超时，停止验收并检查日志或回退。'; exit 1; }

"${statistics_compose[@]}" --profile quality-capture --profile quality-query ps -a
sha256sum -c "$statistics_record/config-before.sha256"
docker ps --format '{{.ID}} {{.Names}} {{.Image}}' > "$statistics_record/containers-after.txt"
diff -u "$statistics_record/containers-before.txt" "$statistics_record/containers-after.txt" || true
"${statistics_compose[@]}" logs --tail 80 ics-agent
```

数据库与 receiver 应保持原容器。ICS 被重新创建；有补迁移且原 worker 运行时，它也应按第6.3节恢复运行，检查其日志并确认持久任务逐步被处理。没有补迁移时 worker 保持原容器。日志若写入挂载目录，还应检查 `/opt/cs/log/uvicorn-admin.stderr.log` 的启动和模型预热结果。`/health` 正常只证明应用能响应，不等于统计查询已通过，继续验证下面的后台接口。

### 7.2 用原管理员账号登录验收

进入原 ICS 后台并强制刷新页面。客服状态下面应有“统计报表”，包含“时间统计”“坐席细化”“质检统计”。保留原登录地址、管理员账号、密码、端口和渠道设置。

| 页面/操作 | 预期结果 |
| --- | --- |
| 时间统计 | 显示进客量、纯AI接待量、转人工量、转人工拒接量、有效人工量、低评分数，右侧有趋势和明细表 |
| 日/月/年切换 | 横轴依次为小时/日/月；从年切回月或日，以北京时间当前月或当天作为默认值，不跳回1月1日 |
| 重置 | 恢复北京时间当天按日展示并重新查询最新数据，清除渠道筛选、恢复图例和第一页 |
| 渠道与图例 | 渠道只在筛选中选择，趋势不额外列各渠道进客量；全选位于图例前部，全部选中时点击变为全不选，否则恢复全选 |
| 坐席细化 | 可用客服曲线、客服回复质量、坐席回复工作量正常；有对话量、高质量对话量、发配量，且均位于回复询盘总量前面 |
| 咨询状态总时长 | 按该坐席每轮首次至最后一次公开回复计算，合并重叠区间；单次回复为0秒 |
| 质检统计 | 人工质检表按坐席显示六项指标，AI质检表显示对话轮数、转人工、纯AI回复、转人工失败、不合格量及不合格率 |
| 指标解释 | 悬停各统计项后的“？”可显示口径 |
| 原有功能 | 对话归档、双项质检结果/备注、原客服状态和智能接待页面仍可正常使用 |

在**已登录的后台页面**打开浏览器开发者工具的 Console，执行下面只读检查。它使用当前登录会话，不输入或打印密码、Cookie。

```javascript
for (const mode of ['day', 'month', 'year']) {
  for (const report of ['time', 'agents', 'quality']) {
    const response = await fetch(`/admin/api/statistics/${report}?mode=${mode}`, {
      credentials: 'same-origin'
    });
    console.log(mode, report, response.status);
  }
}
```

九项应均为 `200`；还要检查页面中的查询结果。`401` 表示登录态失效，`404` 多为旧镜像或入口未更新，`503` 需要检查归档查询配置、reader连接、001–009、查询负载和相关日志。若反向代理对子路径单独限制，还应确认统计 API 与资源走原 ICS 后端。

不要把“真实无数据”和“查询失败”混为一谈。报表只使用已采集、已解析的数据；状态历史沿用原45天保留期，缺样本不补成在线。未保存不合格结论时不合格量可以为0，分母为0时不合格率显示“—”。

人工不合格率分母是各坐席的回复询盘总量，AI不合格率分母是所选区间的全部对话轮。人工按实际回复时间归集，AI按轮开始时间归集，不要求两个表的总量一致。完整指标说明见 [统计模块说明](../statistics/README.md)。

### 7.3 保存发布结果并结束会话

确认通过后执行：

```bash
statistics_new_container=$("${statistics_compose[@]}" ps -q ics-agent)
docker inspect -f '{{.Image}}' "$statistics_new_container" > "$statistics_record/image-after.txt"
printf 'git_branch=test\ngit_commit=%s\nreleased_at=%s\nmigrations_applied=%s\nresult=accepted\n' \
  "$statistics_release_commit" "$(date -Is)" "$statistics_needs_migration" > "$statistics_record/result.txt"
sha256sum -c "$statistics_record/config-before.sha256"
flock -u 9
exec 9>&-
```

保留本次私有记录、临时构建文件和旧镜像，验收结束前不清理 Docker 镜像。

## 八、失败时回退应用代码和镜像

先区分数据库是否已发生升级：

- **没有新增迁移提交**：可以用下面命令恢复旧应用代码和镜像；若 worker 曾暂停，恢复原容器运行。
- **已经提交新迁移，包括迁移中途失败但前面版本已提交**：保留现有数据库，优先修复原因并继续第6.2、6.3节。尤其009撤销旧单项质检保存授权，不能把旧应用镜像回退当作完整恢复。下面命令会检查版本变化并阻止这种回退；若必须恢复旧业务版本，需另行准备兼容当前schema的应用或安排包含新增事件处理的数据恢复，不能直接覆盖导入旧库。

使用第四节打印的实际记录目录。下面代码在一个新的 Bash 会话中执行，先结束原发布会话或释放原锁，再进行回退。

```bash
set -euo pipefail
umask 077
source "$HOME/cs-backups/statistics-实际时间戳/session.sh"
cd "$statistics_target"
exec 9>"$statistics_target/.deployment.lock"
flock -n 9 || { echo '发布锁仍被占用，请先结束原发布会话。'; exit 1; }

test -f "$statistics_record/code-before.tar.gz"
docker image inspect "$statistics_rollback_image" >/dev/null
sha256sum -c "$statistics_record/config-before.sha256"
statistics_archive_psql -At > "$statistics_record/versions-at-rollback.json" <<'SQL'
SELECT COALESCE(json_agg(t ORDER BY t.version), '[]'::json)
FROM (SELECT version,checksum_sha256 FROM ops.schema_migrations) t;
SQL
python3 - "$statistics_record" <<'PY'
from pathlib import Path
import json, sys

record = Path(sys.argv[1])
before = json.loads((record / "versions-before.json").read_text())
current = json.loads((record / "versions-at-rollback.json").read_text())
if current != before:
    raise SystemExit("已提交新迁移，停止旧镜像回退。保留归档数据，修复后继续第6.2、6.3节。")
PY
statistics_restore=$(mktemp -d "$statistics_record/restore.XXXXXX")
tar -xzf "$statistics_record/code-before.tar.gz" -C "$statistics_restore"

# 删除同步只允许发生在下列明确的应用代码目录内，不处理整个运行目录。
for statistics_dir in ics_agent quality; do
  test -d "$statistics_restore/$statistics_dir"
  test ! -L "$statistics_target/$statistics_dir"
  rsync -rcp --delete "$statistics_restore/$statistics_dir/" "$statistics_target/$statistics_dir/"
done
if test "$statistics_had_directory" -eq 1; then
  test ! -L "$statistics_target/statistics"
  rsync -rcp --delete "$statistics_restore/statistics/" "$statistics_target/statistics/"
fi
# 若更新前没有 statistics，保留新增目录供排查；旧入口和旧镜像不会加载它。

docker image tag "$statistics_rollback_image" "$statistics_image"
"${statistics_compose[@]}" up -d --no-deps --no-build --pull never --force-recreate ics-agent
if test "$statistics_worker_was_running" -eq 1; then
  "${statistics_compose[@]}" --profile quality-query start quality-worker
fi
sha256sum -c "$statistics_record/config-before.sha256"
"${statistics_compose[@]}" --profile quality-capture --profile quality-query ps -a
printf 'rolled_back_at=%s\n' "$(date -Is)" > "$statistics_record/rollback.txt"
flock -u 9
exec 9>&-
```

回退后按第7.1节等待健康并用原后台验证。**回退不执行构建**，否则会再次得到新镜像。回退只恢复运行目录和旧镜像，不重置 `/opt/cs-src` 的 Git 分支或改写提交历史。配置校验异常时先核对是否有人另行修改了生产配置，不直接把备份配置全量覆盖回去；本流程不修改环境配置，正常回退也不需要恢复 `.env`。

## 九、这次发布的操作顺序

Windows检查并推送test分支、记录完整提交SHA → 普通发布用户检查权限与原服务 → 沿用完整现有Compose → 在/opt/cs-src快进拉取test并核对提交SHA → 识别历史版本并核对全部已应用SQL → 备份归档库/角色/代码/配置，保留旧镜像 → 同步三个代码目录到/opt/cs → 构建并检查新镜像 → 有缺失时暂停ICS和原worker → 补齐001–009、验证结构及授权、核对既有密码未变 → 更新ICS并按原状态恢复worker → 三个报表及原功能验收 → 核对配置哈希并记录Git发布结果。

本次不需要新增系统提权密码交互，不需要覆盖本地配置到服务器，不需要重建数据库、修改Webhook或重新采集历史数据。
