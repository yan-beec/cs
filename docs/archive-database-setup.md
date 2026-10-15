# ICS 独立归档与质检数据库

日期：2026-10-15。数据库基础设施已配置，首版业务表、权限迁移及验证位于项目顶层 `quality/`；消息接收和后台查询接入为下一步开发。

## 结构

| 项目 | 值 |
| --- | --- |
| Compose 项目 | `cs`，沿用现有项目 |
| 新服务 | `archive-postgres` |
| 数据库 | `ics_quality`，可在首次初始化前配置 |
| 容器内地址 | `archive-postgres:5432` |
| 持久化数据卷 | 默认 `cs_archive_postgres_data` |
| 宿主机端口 | 不发布，ICS 与采集服务通过同一 Docker 网络连接 |
| 私密配置 | `D:\project\CS\.env`，已有忽略规则保护 |

原机器人继续使用现有 `postgres` 服务、数据库和 `postgres_data` 卷，原有连接值保持不变。归档配置合并到同一 `.env` 中，两个数据库仍使用不同账号、密码和连接池。

## 账号和分区

| 账号 | 用途 |
| --- | --- |
| `archive_admin` | 首次初始化和受控维护；具有高权限，不用于应用运行 |
| `archive_owner` | 不可登录的对象所有者，迁移时通过 `SET ROLE archive_owner` 建表 |
| `archive_ingest` | 在 `archive` 中查询及追加原始记录，在 `ops` 中维护任务状态 |
| `archive_reader` | 只读查询原始记录、质检结果及任务状态，供后续后台接入 |
| `archive_quality` | 读取原始记录；目前仅只读采集任务，未来在 `quality` 中追加结果 |

三个分区：`archive` 保存只追加证据，`quality` 保存只追加结果版本，`ops` 保存可更新的处理状态。

初始化脚本设置账号、分区和原始证据的默认权限，业务表由 `quality/database` 中的版本迁移创建。**后续迁移必须以 `archive_owner` 身份创建对象**；`ops` 表按用途显式授权，不默认给质检账号采集任务写权限。原始证据不允许日常运行账号修改、删除或清空。

## 建表与业务权限

代码位于 ICS 顶层 `quality/`，共享现有认证、渠道配置、Chatwoot 客户端和适用依赖。完整表结构、权限矩阵和后续采集约定见 [quality 模块说明](../quality/README.md)。从 ICS 项目根目录执行：

```sh
docker compose --env-file .env -f docker-compose.yml -f docker-compose.archive.yml cp quality/database/. archive-postgres:/tmp/ics-quality-database
docker compose --env-file .env -f docker-compose.yml -f docker-compose.archive.yml exec -T archive-postgres sh /tmp/ics-quality-database/apply-migrations.sh
docker compose --env-file .env -f docker-compose.yml -f docker-compose.archive.yml exec -T archive-postgres sh /tmp/ics-quality-database/verify-schema.sh
```

迁移可重复执行，相同版本和校验值会跳过；禁止修改已应用 SQL，后续变更新增编号文件。无需清空数据卷或再次运行首次账号初始化脚本。验证会实际写入样本、尝试受限操作并回滚，不保留样本记录。当前不创建每日模型评分表。

## 配置与启动

统一使用项目根目录 `.env` 保存连接配置，原有机器人使用 `POSTGRES_*`，归档库使用 `ARCHIVE_*`。参考 [项目配置模板](/D:/project/CS/.env.example)、[生产部署模板](/D:/project/CS/deploy/server/bot.env.example)或[归档配置片段](/D:/project/CS/deploy/archive/archive.env.example)，将新增字段合入生产环境已有 `.env`，为各账号设置独立密码；不要用示例文件覆盖生产配置。

Windows PowerShell，从 `D:\project\CS` 执行：

```powershell
docker compose --env-file .env -f docker-compose.yml -f docker-compose.archive.yml config --quiet
docker compose --env-file .env -f docker-compose.yml -f docker-compose.archive.yml up -d --no-deps archive-postgres
docker compose --env-file .env -f docker-compose.yml -f docker-compose.archive.yml ps archive-postgres
```

仅对 `archive-postgres` 操作，不需要重建机器人或现有数据库。新增服务与现有 ICS 在同一 Compose 网络。

`ARCHIVE_*` 连接名称为后续集成预留，现有 ICS 尚未读取它们。采集和后台接入时需要显式传入适用凭证并创建独立连接池；不要将初始化管理凭证交给应用。

## 验证

验证脚本通过事务创建临时验证表，检查允许的写入和被拒绝的更新、删除、清空、越权建表行为，最后回滚，并通过 TCP 实际验证三个应用账号的密码。

Windows PowerShell：

```powershell
Get-Content -Raw deploy/archive/verify-database.sh | docker compose --env-file .env -f docker-compose.yml -f docker-compose.archive.yml exec -T archive-postgres sh
```

Linux：

```sh
docker compose --env-file .env -f docker-compose.yml -f docker-compose.archive.yml exec -T archive-postgres sh < deploy/archive/verify-database.sh
```

输出应包含权限验证及三个运行账号认证通过，退出码为 0。验证不保留测试表或消息。

## 单一配置文件与凭据隔离

本机原归档配置已按原值合入 `.env`，没有轮换数据库密码。确认全部字段一致后移除旧 `.env.archive`，后续启动命令只读取 `.env`。

合并会让一份文件集中持有两个数据库的凭据，因此继续保持 `.env` 不提交 Git、不发送到前端或日志，并在服务器上限制文件读取权限。

现有 ICS 服务通过 `env_file: .env` 加载变量，基础部署配置已显式将 `ARCHIVE_POSTGRES_ADMIN_PASSWORD`、`ARCHIVE_INGEST_PASSWORD` 和 `ARCHIVE_QUALITY_PASSWORD` 置空，避免它获得归档库管理和写入权限。只读账号为后续后台查询预留；实际连接代码仍需开发。归档数据库仅接收初始化需要的指定变量。未来采集及质检服务也应显式注入各自所需凭据。

生产更新同步 `docker-compose.yml`、`docker-compose.archive.yml` 和初始化目录，使用已有生产密码补齐 `.env`，按本文命令操作新增数据库。初始化账号和密码只在空卷建库时创建；已有归档库的密码不能被本机配置或示例值覆盖。

## 后续维护

- 密码和初始化脚本只在空数据卷首次初始化时生效。已有卷改密需要显式执行受控账号改密，同时更新对应服务配置，不能靠修改环境变量完成。
- Shell 初始化文件已设置 Git 属性保持 LF 换行，避免 Windows 提交后在 Linux 初始化失败。
- 数据卷与容器分离，重启和普通重建保留数据；不要执行会移除数据卷的清理操作。
- 数据库与附件仍需独立备份；本次新增数据库不代表已配置定期备份或上线到远程服务器。
- 若本机 Python 需要直接连接，可另行添加仅监听 `127.0.0.1` 的未占用宿主机端口。当前配置以容器间访问为目标。
