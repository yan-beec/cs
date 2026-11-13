from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class OfflineSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARCHIVE_OFFLINE_", env_file=".env",
                                     env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True)
    directory: str = "database/archive-exports"
    max_bytes: int = Field(default=20 * 1024**3, ge=1024)
    disk_budget_bytes: int = Field(default=50 * 1024**3, ge=1024)
    min_free_bytes: int = Field(default=2 * 1024**3, ge=0)
    max_file_bytes: int = Field(default=2 * 1024**3, ge=1)
    # 单页 HTML 内嵌图片、视频和音频；单个超过上限或全页累计超过上限时改为保留原始链接，不让任务失败。
    max_embed_bytes: int = Field(default=5 * 1024**2, ge=1)            # 单张图片
    max_embed_video_bytes: int = Field(default=100 * 1024**2, ge=1, le=300 * 1024**2)  # 单个视频/音频
    # 全页内嵌附件原始字节累计上限；base64 后约 ×1.33，800 MiB ≈ 1.07 GB 页面，超过 1~2 GB 浏览器易卡死。
    max_embedded_total_bytes: int = Field(default=800 * 1024**2, ge=0)
    timeout_seconds: int = Field(default=86400, ge=10)
    media_timeout_seconds: int = Field(default=20, ge=1, le=120)
    # 任务记录（列表中的历史）保留天数；HTML 文件本身见 downloaded_hours / unclaimed_hours。
    retention_days: int = Field(default=7, ge=1, le=365)
    # 首次下载完成后服务器端再保留的小时数，期间可重新下载（防止代理缓冲、浏览器取消保存等情况误删）。
    downloaded_hours: int = Field(default=2, ge=1, le=24 * 30)
    # 已生成但一直没人下载（或下载中断）的 HTML，超过该小时数自动删除。
    unclaimed_hours: int = Field(default=24, ge=1, le=24 * 30)
    # 完整来源地址（协议、域名、可选端口），多个来源用逗号分隔。
    media_origins: str = ""

    # ---- 导出前数量预估：超过提醒线需二次确认，超过上限直接拒绝 ----
    # 依据：浏览器打开时整个 HTML 读入内存，消息/轮次过多会让页面卡死或打不开。
    warn_messages: int = Field(default=30_000, ge=1)
    max_messages: int = Field(default=300_000, ge=1)
    warn_rounds: int = Field(default=5_000, ge=1)
    max_rounds: int = Field(default=50_000, ge=1)
    # 预估查询的数据库超时（秒）；超时说明范围过大，正式导出大概率也会超时，按超限处理。
    estimate_timeout_seconds: int = Field(default=60, ge=5, le=600)

    # ---- 资源限制：生成过程以服务稳定为先，宁可慢 ----
    # 在独立子进程中生成，不与 webhook / 后台接口争用主进程 GIL；仅测试时关闭。
    isolated_process: bool = True
    # 子进程平均最多占用的 CPU 核数（0.5 = 半个核）；0 表示不限速。
    cpu_limit: float = Field(default=0.5, ge=0, le=64)
    # 子进程调度优先级（Linux nice 值，19 最低）；Windows 使用“低于正常”优先级。
    nice: int = Field(default=19, ge=0, le=19)
    # 子进程虚拟内存上限（字节，仅 Linux）；超过即任务失败而不是挤占服务内存；0 表示不限。
    memory_limit_bytes: int = Field(default=4 * 1024**3, ge=0)
