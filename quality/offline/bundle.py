"""在容量预算内流式提取消息，生成“HTML + 附件 ZIP”双包离线留档。

archive.html：样式与查看脚本内联；每轮对话一个（或多个分段）非执行的 JSON 数据块，只含文字与目录，
体积与消息量成正比，浏览器打开快。
media.zip（下载名“<标题>-附件.zip”）：图片、视频和音频按 URL 去重后以原文件存入 ASCII 命名的文件夹
（如 ics-attachments-2026-10/）
（不压缩、不做 base64）。把 ZIP 解压到 HTML 所在目录后，页面按相对路径加载附件。
PDF 等其他文件不下载，只保留文件信息与原始链接。单个附件超限、附件包累计超限或磁盘预算不足的也改为链接。
附件边下载边写入 ZIP，临时目录里同一时刻最多只有一个附件。
"""
import base64
from contextlib import closing
from datetime import datetime
import hashlib
import html
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import time
from urllib.parse import urlsplit
import zipfile

from quality.attachment_safety import BLOCKED_REASON, attachment_kind
from quality.query.repository import statistics_metrics
from quality.query.service_metrics import RoundServiceMetrics
from quality.query.filter_contract import filter_contract, FILTER_CONTRACT_VERSION, FORMAT_VERSION, normalize_ids
from quality.processing.service_facts import FACT_VERSION
from .filter_snapshot import CUSTOMER_FIELDS, ROUND_FIELDS as FILTER_ROUND_FIELDS, MESSAGE_FIELDS as FILTER_MESSAGE_FIELDS, VERSION_FIELDS, safe_rating_review

from .media import MediaTooLarge, MediaUnavailable
from .naming import BEIJING, archive_title, media_folder

WEB = Path(__file__).parent / "web"


class BundleLimit(ValueError):
    pass


def _json_default(value):
    # 时间统一为 ISO 8601（带 T 分隔符），str(datetime) 的空格格式 Safari 无法解析。
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def encoded(value):
    return json.dumps(value, ensure_ascii=True, default=_json_default, separators=(",", ":"))


def script_json(value):
    """写入 <script type="application/json"> 的数据；中文不转义以减小体积，< > & 转义防止正文闭合标签。"""
    text = json.dumps(value, ensure_ascii=False, default=_json_default, separators=(",", ":"))
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
              "image/avif": ".avif", "video/mp4": ".mp4", "video/webm": ".webm", "video/ogg": ".ogv",
              "video/quicktime": ".mov", "audio/mpeg": ".mp3", "audio/ogg": ".ogg", "audio/mp4": ".m4a",
              "audio/wav": ".wav", "application/pdf": ".pdf"}
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif"}
VIDEO_SUFFIXES = {".mp4", ".webm", ".ogv", ".mov", ".m4v", ".avi", ".mkv", ".3gp"}
AUDIO_SUFFIXES = {".mp3", ".ogg", ".m4a", ".wav", ".aac", ".amr", ".opus"}
GENERIC_TYPES = {"", "file", "application/octet-stream", "binary/octet-stream"}
EMBEDDED_KINDS = {"image", "video", "audio", "unknown"}
LINK_REASONS = {"pdf": "文件不内嵌到页面，可通过原始链接查看",
                "file": "文件不内嵌到页面，可通过原始链接查看"}
KIND_NAMES = {"image": "图片", "video": "视频", "audio": "音频"}
FULL_REASON = "附件包已达总量上限（ARCHIVE_OFFLINE_MAX_EMBEDDED_TOTAL_BYTES），可通过原始链接查看"
DISK_REASON = "留档目录磁盘预算或剩余空间不足（ARCHIVE_OFFLINE_DISK_BUDGET_BYTES），未放入附件包，可通过原始链接查看"
MEDIA_ARCHIVE = "media.zip"
MEDIA_SUFFIX = "-附件.zip"
MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$")


def sniff_media_type(path, declared=None):
    """附件服务常返回 application/octet-stream；按文件头识别真实类型。"""
    with open(path, "rb") as media:
        head = media.read(64)
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand == b"qt  ":
            return "video/quicktime"
        if brand in (b"M4A ", b"M4B "):
            return "audio/mp4"
        return "video/mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    if head.startswith(b"OggS"):
        return "audio/ogg" if str(declared or "").startswith("audio") else "video/ogg"
    if head.startswith(b"ID3") or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    return None


def resolve_media_type(path, served, declared=None):
    """优先使用服务端返回的已知类型；未知或通用类型时再按文件头识别。"""
    if served in EXTENSIONS:
        return served
    return sniff_media_type(path, declared) or served


def _suffix(item, url):
    try:
        path = urlsplit(url).path if url else ""
    except ValueError:
        path = ""
    for name in (item.get("filename"), path):
        suffix = PurePosixPath(str(name or "")).suffix.lower()
        if suffix:
            return suffix
    return ""


def media_kind(item, url):
    """按源端声明类型和扩展名判断附件类别，决定是否需要下载。"""
    declared = str(item.get("media_type") or "").lower()
    for prefix in ("image", "video", "audio"):
        if declared.startswith(prefix):
            return prefix
    suffix = _suffix(item, url)
    if declared == "application/pdf" or suffix == ".pdf":
        return "pdf"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    if declared in GENERIC_TYPES and not suffix:
        return "unknown"  # 无类型无扩展名，下载（受单张上限约束）后按文件头判断
    return "file"


def kind_of_type(media_type):
    media_type = str(media_type or "")
    if media_type == "application/pdf":
        return "pdf"
    head = media_type.split("/")[0]
    return head if head in ("image", "video", "audio") else "file"


def safe_link(url):
    try:
        return url if urlsplit(url).scheme in ("http", "https") else None
    except ValueError:
        return None


class Budget:
    def __init__(self, root, settings, check):
        self.root, self.settings, self.cancel = Path(root), settings, check
        self.input_bytes = 0
        self.last = 0
        self.started = time.monotonic()
        # 仅在开始时扫描一次旧产物，后续按写入量增量计数，避免海量附件触发平方级扫描。
        self.disk_bytes = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
        self.watched = {}
        for path in (self.root / "jobs.sqlite3", self.root / "jobs.sqlite3-journal"):
            self.watched[path] = path.stat().st_size if path.exists() else 0

    def watch(self, path):
        self.watched[Path(path)] = 0

    def reserve(self, size):
        self.disk_bytes += size
        self.check()

    def discard(self, path):
        path = Path(path)
        if path in self.watched:
            self.disk_bytes -= self.watched.pop(path)
        elif path.exists():
            self.disk_bytes -= path.stat().st_size
        path.unlink(missing_ok=True)

    def check(self, *, force=False):
        self.cancel()
        now = time.monotonic()
        if now - self.started > self.settings.timeout_seconds:
            raise BundleLimit("留档生成超时，请调整 ARCHIVE_OFFLINE_TIMEOUT_SECONDS 或缩小范围。")
        if force or now - self.last > 0.5:
            self.last = now
            if shutil.disk_usage(self.root).free < self.settings.min_free_bytes:
                raise BundleLimit("留档目录磁盘剩余空间不足，未生成完整留档。")
            # SQLite 最多跟踪几个文件，其余输出由 reserve 精确计数。
            for path, previous in self.watched.items():
                current = path.stat().st_size if path.exists() else 0
                self.disk_bytes += current - previous
                self.watched[path] = current
        if self.disk_bytes > self.settings.disk_budget_bytes:
            raise BundleLimit("留档目录已超过 ARCHIVE_OFFLINE_DISK_BUDGET_BYTES，请清理旧文件或调整预算。")

    def add(self, size):
        self.input_bytes += size
        if self.input_bytes > self.settings.max_bytes:
            raise BundleLimit("留档数据超过 ARCHIVE_OFFLINE_MAX_BYTES，未生成完整留档。")
        self.reserve(size)


class Output:
    """顺序写入的文本文件，每次写入都计入磁盘预算。"""

    def __init__(self, path, budget):
        self.path, self.budget = Path(path), budget
        self.stream = self.path.open("wb")

    def write(self, value):
        data = value.encode("utf-8", "replace") if isinstance(value, str) else value
        self.budget.reserve(len(data))
        self.stream.write(data)

    def close(self):
        if not self.stream.closed:
            self.stream.close()


def _csp_hash(text):
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode() + "'"


def page_shell(title):
    """返回页面头部（含内联样式）与尾部（含内联脚本）；CSP 只放行这两段的哈希。"""
    css = (WEB / "viewer.css").read_text(encoding="utf-8")
    script = (WEB / "filters.js").read_text(encoding="utf-8") + "\n" + (WEB / "viewer.js").read_text(encoding="utf-8")
    template = (WEB / "index.html").read_text(encoding="utf-8")
    if "</style" in css.lower() or "</script" in script.lower():
        raise RuntimeError("查看页资源包含非法结束标签")
    # 附件以原文件放在旁边的文件夹里，按相对路径（file:）加载；页面本身仍不能联网。
    csp = ("default-src 'none'; script-src " + _csp_hash(script) + "; style-src " + _csp_hash(css)
           + "; img-src data: blob: 'self' file:; media-src blob: 'self' file:; connect-src 'none'; font-src 'none'; object-src 'none';"
           " frame-src 'none'; base-uri 'none'; form-action 'none'")
    head, tail = template.split("<!--ICS-DATA-->")
    head = head.replace("__CSP__", csp).replace("__TITLE__", html.escape(title)).replace("__STYLE__", css)
    return head, tail.replace("__SCRIPT__", script)


# 只把查看聊天所需的字段写进每条消息；轮次信息放在目录里，不在每条消息重复。
MESSAGE_FIELDS = ("message_id", "sent_at", "sender_id", "sender_name", "sender_role", "is_private",
                  "is_deleted", "is_rating", "content", "visitor_rating", *FILTER_MESSAGE_FIELDS)
ROUND_FIELDS = ("conversation_id", "round_id", "round_no", "customer_round_seq", "round_started_at",
                "round_status", "reception_type", "ai_review_status", "human_review_status", "review_remark",
                "round_rating_status", "round_rating", "round_rating_feedback", "rating_review", "resolved_at", "closed_at",
                "last_message_at", "responsibility_basis", "responsible_assigned_at", "handoff_requested_at",
                "takeover_inherited", "responsibility_trigger_message_id", *FILTER_ROUND_FIELDS)
FACT_FIELDS = ("assigned_agent_id", "assigned_agent_name", "responsible_agent_id", "responsible_agent_name",
               "assisted", "responding_seats", "is_lost", "handoff_at", "human_replied",
               "first_response_seconds", "average_response_seconds", "response_samples")
PART_BYTES = 4 * 1024**2
PREVIEW_CHARS = 60


def _preview(text):
    text = " ".join(str(text or "").split())
    return text[:PREVIEW_CHARS] + ("…" if len(text) > PREVIEW_CHARS else "")


def build_bundle(work: Path, rows, settings, downloader, progress, check, *, scope=None, metadata=None):
    scope = normalize_ids(scope or {})
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    budget = Budget(work.parent, settings, check)
    spool = work / "source.jsonl"
    cache_path = work / "media-cache.sqlite3"
    toc_path = work / "toc.jsonl"
    customers_path = work / "customers.jsonl"
    media_dir = work / "media"
    archive_path = work / "archive.html"
    media_path = work / MEDIA_ARCHIVE
    exported = datetime.now(BEIJING)
    # 标题日期与下载文件名一致，取任务创建时间（入队时写入 requested_at）。
    requested = (scope or {}).get("requested_at") or exported
    title = archive_title(scope or {}, requested)
    folder = media_folder(scope or {}, requested)
    count = 0
    version_parts = 0
    extracted_versions = 0
    snapshot_at = (scope or {}).get("snapshot_at")
    max_messages = getattr(settings, "max_messages", None)
    page = None
    opened = []
    state = None
    try:
        # 开始下载附件前，先读完并关闭一致性快照，释放数据库事务。
        progress(stage="extracting", messages=0)
        try:
            with spool.open("w", encoding="utf-8", newline="\n") as output:
                for row in rows:
                    row = normalize_ids(row)
                    if row.get("_type") == "search_version" and row.get("version") is not None:
                        row["version"] = str(row["version"])
                    snapshot_at = row.get("snapshot_at") or snapshot_at
                    line = encoded(row) + "\n"
                    budget.add(len(line.encode("utf-8")))
                    output.write(line)
                    if row.get("_type") != "search_version":
                        count += 1
                    else:
                        extracted_versions += 1
                    # 兜底：生成前已在同一快照内计数拦截，这里防止任何路径下无上限地写下去。
                    if max_messages and count > max_messages:
                        raise BundleLimit(f"导出消息超过单个离线页面上限 {max_messages:,} 条"
                                          "（ARCHIVE_OFFLINE_MAX_MESSAGES），请缩小范围后分批导出。")
                    if (row.get("_type") != "search_version" and count % 1000 == 0) or (extracted_versions and extracted_versions % 32 == 0):
                        progress(stage="extracting", messages=count, search_version_parts=extracted_versions)
        finally:
            close = getattr(rows, "close", None)
            if close:
                close()

        snapshot_at = (metadata or {}).get("snapshot_at") or snapshot_at
        budget.add(len(encoded((metadata or {}).get("casefold_map", {})).encode("utf-8")))
        media_dir.mkdir()
        budget.watch(cache_path)
        budget.watch(work / "media-cache.sqlite3-journal")
        head, tail = page_shell(title)
        page = Output(archive_path, budget)
        opened.append(page)
        toc = Output(toc_path, budget)
        opened.append(toc)
        customers = Output(customers_path, budget)
        opened.append(customers)
        page.write(head)
        stats = {"images": 0, "videos": 0, "linked": 0, "blocked": 0, "unavailable": 0, "rounds": 0, "groups": 0, "unrounded_messages": 0, "customers": 0}
        embedded_bytes = 0
        unique_media = 0
        processed = 0
        state = {"customer": None, "group": None, "reported": 0.0, "zip": None}
        # 文字页面最终约与临时消息文件同量级：下载附件前为它预留磁盘预算，附件放不下时改为链接而不是整单失败。
        text_reserve = spool.stat().st_size

        def report():
            # 进度写入任务库，最多每秒一次，避免每张图片都产生一次 SQLite 事务。
            now = time.monotonic()
            if now - state["reported"] < 1:
                return
            state["reported"] = now
            progress(stage="media", messages=count, processed=processed, attachments=stats["images"],
                     videos=stats["videos"], rounds=stats["rounds"], unrounded_messages=stats["unrounded_messages"],
                     unavailable_attachments=stats["unavailable"], linked_attachments=stats["linked"], blocked_attachments=stats["blocked"])

        def embed(path, media_type):
            """把附件原样（不压缩）写入附件包的“<标题>-附件/”文件夹，返回编号、包内文件名和校验值。"""
            nonlocal unique_media
            media_id = unique_media
            unique_media += 1
            name = f"{media_id:06d}{EXTENSIONS.get(media_type, '')}"
            digest = hashlib.sha256()
            with path.open("rb") as media:
                while block := media.read(1024 * 1024):
                    budget.check()
                    digest.update(block)
            archive = state["zip"]
            if archive is None:
                archive = state["zip"] = zipfile.ZipFile(media_path, "w", zipfile.ZIP_STORED, allowZip64=True)
                budget.watch(media_path)
            archive.write(path, f"{folder}/{name}")
            budget.check(force=True)
            return media_id, name, digest.hexdigest()

        def blocked(item, **extra):
            # 非图片/音视频不下载、不打包，也不保留可点击的原始链接。
            stats["blocked"] += 1
            return {"filename": item.get("filename"), "kind": "file", "media_type": item.get("media_type"),
                    "byte_count": item.get("expected_byte_count"), "source_url": None, **extra,
                    "status": "blocked", "reason": BLOCKED_REASON}

        def attachment(item, cache):
            if item.get("blocked") or not attachment_kind(item):
                return blocked(item)
            url = item.get("source_url") or ""
            kind = media_kind(item, url)
            base = {"filename": item.get("filename"), "kind": kind, "media_type": item.get("media_type"),
                    "byte_count": item.get("expected_byte_count"), "source_url": safe_link(url)}
            if kind not in EMBEDDED_KINDS:
                stats["linked"] += 1
                return {**base, "status": "linked", "reason": LINK_REASONS.get(kind, LINK_REASONS["file"])}
            existing = cache.execute("SELECT metadata FROM media WHERE url=?", (url,)).fetchone()
            if existing:
                cached = json.loads(existing[0])
            elif embedded_bytes >= settings.max_embedded_total_bytes:
                # 已达到页面内嵌总量，后续附件不再下载。
                cached = {"status": "linked", "reason": FULL_REASON}
            else:
                cached = download_media(item, kind)
                cache.execute("INSERT INTO media VALUES (?,?)", (url, encoded(cached)))
                report()
            if cached["status"] == "blocked":
                return blocked(item, byte_count=cached.get("byte_count"), media_type=cached.get("media_type"))
            if cached["status"] == "embedded":
                stats["images" if cached.get("kind") == "image" else "videos"] += 1
            else:
                stats["linked" if cached["status"] == "linked" else "unavailable"] += 1
            return {**base, **cached}

        def limit_text(limit):
            return f"{limit / 1024**2:g} MB" if limit >= 1024**2 else f"{max(1, limit // 1024)} KB"

        def kind_limit(kind):
            return settings.max_embed_bytes if kind == "image" else settings.max_embed_video_bytes

        def download_media(item, kind):
            nonlocal embedded_bytes
            destination = media_dir / f"asset-{unique_media:08d}"
            before = 0

            def media_check():
                nonlocal before
                size = destination.stat().st_size if destination.exists() else 0
                # 附件只占磁盘预算，不计入 MAX_BYTES（该上限只管提取的消息数据）。
                budget.reserve(max(0, size - before))
                before = size

            def release():
                destination.unlink(missing_ok=True)
                budget.disk_bytes -= before

            def too_large(found):
                name = KIND_NAMES.get(found, "附件")
                return {"status": "linked", "reason": f"{name}超过 {limit_text(kind_limit(found))} 单个附件上限，可通过原始链接查看"}

            # 类型未知时先按较大的上限下载，识别出真实类型后再按该类型上限判断。
            limit = (max(settings.max_embed_bytes, settings.max_embed_video_bytes) if kind == "unknown"
                     else kind_limit(kind))
            expected = item.get("expected_byte_count")
            # 附件在临时文件和附件包里各占一份，再给文字页面留出余量；放不下就保留链接，不让整单失败。
            need = 2 * (expected if isinstance(expected, int) and 0 < expected <= limit else limit) + text_reserve
            if (budget.disk_bytes + need > settings.disk_budget_bytes
                    or shutil.disk_usage(budget.root).free - need < settings.min_free_bytes):
                return {"status": "linked", "reason": DISK_REASON}
            try:
                served = downloader.download(item, destination, media_check, limit=limit)
                media_check()
            except MediaTooLarge:
                release()
                return too_large(kind)
            except MediaUnavailable as error:
                release()
                return {"status": "unavailable", "error": str(error)}
            media_type = str(resolve_media_type(destination, served, item.get("media_type")) or "").lower()
            found = kind_of_type(media_type)
            size = destination.stat().st_size
            if found == "image" and MEDIA_TYPE.match(media_type) and media_type not in IMAGE_TYPES:
                release()  # 浏览器无法显示的图片格式（如 HEIC）保留链接
                return {"status": "linked", "kind": "image", "media_type": media_type, "byte_count": size,
                        "reason": "浏览器无法显示该图片格式，可通过原始链接查看"}
            if found not in KIND_NAMES or not MEDIA_TYPE.match(media_type):
                # 扩展名像图片/视频、内容却不是：按伪装文件拦截，不留链接。
                release()
                return {"status": "blocked", "media_type": media_type or None, "byte_count": size}
            if size > kind_limit(found):
                release()
                return {**too_large(found), "kind": found, "media_type": media_type, "byte_count": size}
            if embedded_bytes + size > settings.max_embedded_total_bytes:
                release()
                return {"status": "linked", "kind": found, "media_type": media_type, "byte_count": size,
                        "reason": FULL_REASON}
            media_id, file_name, checksum = embed(destination, media_type)
            embedded_bytes += size
            # 已写入附件包（其大小由预算按文件跟踪），仅删除临时文件。
            destination.unlink(missing_ok=True)
            budget.disk_bytes -= before
            return {"status": "embedded", "kind": found, "file": file_name, "media_type": media_type,
                    "byte_count": size, "checksum_sha256": checksum}

        def write_rows(group):
            if not group["rows"]:
                return
            page.write(f'<script type="application/json" id="g{group["entry"]["index"]}.{group["parts"]}">'
                       + script_json(group["rows"]) + "</script>\n")
            group["parts"] += 1
            group["rows"], group["bytes"] = [], 0

        def close_group():
            group = state["group"]
            if not group:
                return
            write_rows(group)
            group["entry"]["parts"] = group["parts"]
            if group["metrics"] is not None:
                group["entry"]["service_metrics"] = statistics_metrics(group["metrics"].result(), group["fact"])
            toc.write(script_json(group["entry"]) + "\n")
            state["group"] = None

        def open_group(row, key):
            entry = {"index": stats["groups"], "customer": stats["customers"] - 1,
                     **{field: row.get(field) for field in (*ROUND_FIELDS, *FACT_FIELDS)},
                     "first_at": row.get("sent_at"), "last_at": row.get("sent_at"), "messages": 0,
                     "preview": "", "rating": row.get("round_rating") if row.get("round_rating_status") == "rated" else None,
                     "rating_feedback": row.get("round_rating_feedback") if row.get("round_rating_status") == "rated" else None,
                     "rated": row.get("round_rating_status") == "rated",
                     "rating_review": row.get("rating_review"),
                     "images": 0, "videos": 0, "attachments": 0, "unavailable": 0, "has_agent": False, "has_private": False}
            stats["groups"] += 1
            metrics = None
            if row.get("round_id") is not None:
                # 轮次去重存磁盘；超长分段和重复分组都不能虚增真实轮次数。
                added = cache.execute("INSERT OR IGNORE INTO rounds VALUES (?)", (row["round_id"],)).rowcount
                stats["rounds"] += added
                metrics = RoundServiceMetrics({**row, "started_at": row.get("round_started_at"),
                                               "status": row.get("round_status")})
            fact = {field: row.get(field) for field in FACT_FIELDS}
            state["group"] = {"key": key, "entry": entry, "rows": [], "bytes": 0, "parts": 0,
                              "metrics": metrics, "fact": fact}

        report()
        with closing(sqlite3.connect(cache_path)) as cache, spool.open(encoding="utf-8") as source:
            # 去重索引写入磁盘，避免海量附件元数据随消息数量占满内存。
            cache.execute("CREATE TABLE media (url TEXT PRIMARY KEY,metadata TEXT NOT NULL)")
            cache.execute("CREATE TABLE rounds (round_id TEXT PRIMARY KEY)")
            cache.execute("CREATE TABLE filter_options (kind TEXT,key TEXT,value TEXT,PRIMARY KEY(kind,key))")
            for line in source:
                budget.check()
                row = json.loads(line)
                if row.get("_type") == "search_version":
                    chunk = {key: row.get(key) for key in VERSION_FIELDS}
                    page.write(f'<script type="application/json" id="v{version_parts}">' + script_json(chunk) + '</script>\n')
                    version_parts += 1
                    continue
                row["rating_review"] = safe_rating_review(row.get("rating_review"))
                # 字典去重存磁盘，避免所有轮次的坐席和收件箱元数据常驻内存。
                source_scope = {"source_key": row.get("source_key"), "account_id": row.get("account_id")}
                if all(value is not None for value in source_scope.values()):
                    agents = [*(row.get("replied_agent_ids") or []), *(row.get("assigned_agent_ids") or []),
                              row.get("responsible_agent_id"), row.get("assigned_agent_id"), row.get("current_assignee_id")]
                    for kind, id_key, values in (("agents", "agent_id", agents), ("inboxes", "inbox_id", [row.get("inbox_id")])):
                        for value in values:
                            if value is None:
                                continue
                            option = {**source_scope, id_key: str(value)}
                            serialized = script_json(option)
                            cache.execute("INSERT OR IGNORE INTO filter_options VALUES (?,?,?)", (kind, serialized, serialized))
                customer_key = (row.get("source_key"), row.get("account_id"), row.get("customer_id"))
                if customer_key != state["customer"]:
                    close_group()
                    state["customer"] = customer_key
                    customers.write(script_json({"id": row.get("customer_id"), "name": row.get("customer_name"),
                                                 "contact_id": row.get("contact_id"),
                                                 "h5_user_id": row.get("h5_user_id"),
                                                 **{key: row.get(key) for key in CUSTOMER_FIELDS}}) + "\n")
                    stats["customers"] += 1
                group_key = (row.get("conversation_id"), row.get("round_id"))
                if not state["group"] or state["group"]["key"] != group_key:
                    close_group()
                    open_group(row, group_key)
                group = state["group"]
                if group["metrics"] is not None:
                    # 指标只读取临时文件里的整轮消息，分段写出不会重置计算器。
                    group["metrics"].observe(row)
                else:
                    stats["unrounded_messages"] += 1
                attachments = [attachment(item, cache) for item in row.get("attachments") or []]
                message = {field: row.get(field) for field in MESSAGE_FIELDS}
                message["attachments"] = attachments
                entry = group["entry"]
                entry["messages"] += 1
                entry["last_at"] = row.get("sent_at") or entry["last_at"]
                entry["first_at"] = entry["first_at"] or row.get("sent_at")
                entry["attachments"] += len(attachments)
                entry["images"] += sum(a["status"] == "embedded" and a.get("kind") == "image" for a in attachments)
                entry["videos"] += sum(a["status"] == "embedded" and a.get("kind") in ("video", "audio") for a in attachments)
                entry["unavailable"] += sum(a["status"] == "unavailable" for a in attachments)
                role = row.get("sender_role")
                if row.get("is_private"):
                    entry["has_private"] = True
                elif role == "agent":
                    entry["has_agent"] = True
                if not entry["preview"] and role == "contact" and not row.get("is_private") and str(row.get("content") or "").strip():
                    entry["preview"] = _preview(row.get("content"))
                group["rows"].append(message)
                group["bytes"] += len(line)
                processed += 1
                # 超长轮次分段写出，单次缓存的消息不超过约 4 MiB。
                if group["bytes"] >= PART_BYTES:
                    write_rows(group)
                if processed % 1000 == 0:
                    cache.commit()
                    report()
            close_group()
            # 字典仍逐行输出；空文件也提供两个明确的字典键。
            page.write('<script type="application/json" id="filter-options">{')
            for index, kind in enumerate(("agents", "inboxes")):
                page.write(("," if index else "") + script_json(kind) + ":[")
                for number, option in enumerate(cache.execute("SELECT value FROM filter_options WHERE kind=? ORDER BY key", (kind,))):
                    page.write(("," if number else "") + option[0])
                page.write("]")
            page.write("}</script>\n")
        toc.close()
        customers.close()
        if state["zip"] is not None:
            state["zip"].close()
            budget.check(force=True)
        report()
        budget.check(force=True)
        budget.discard(spool)
        budget.discard(cache_path)
        budget.discard(work / "media-cache.sqlite3-journal")
        progress(stage="finishing", messages=count, processed=processed, attachments=stats["images"],
                 videos=stats["videos"], rounds=stats["rounds"], unrounded_messages=stats["unrounded_messages"],
                 unavailable_attachments=stats["unavailable"], linked_attachments=stats["linked"], blocked_attachments=stats["blocked"])
        meta = {"format": "ics-offline-archive", "version": FORMAT_VERSION, "title": title, "exported_at": exported.isoformat(),
                "filter_contract_version": FILTER_CONTRACT_VERSION,
                "fact_version": (metadata or {}).get("fact_version", FACT_VERSION),
                "filter_contract": filter_contract((scope or {}).get("filters"), casefold_map=(metadata or {}).get("casefold_map")),
                "search_version_parts": version_parts,
                "options_block": "filter-options",
                "messages": count, "rounds": stats["rounds"], "groups": stats["groups"],
                "unrounded_messages": stats["unrounded_messages"], "customers": stats["customers"],
                "summary": (scope or {}).get("summary"), "view": (scope or {}).get("view", "rounds"),
                "as_of": ((scope or {}).get("filters") or {}).get("as_of") or (scope or {}).get("as_of"),
                "snapshot_at": snapshot_at,
                "images": stats["images"], "videos": stats["videos"], "linked_attachments": stats["linked"],
                # 附件包：解压到 HTML 同一目录后按“<media_folder>/<文件名>”加载。
                "media_folder": folder if unique_media else None, "media_files": unique_media,
                "media_archive": title + MEDIA_SUFFIX if unique_media else None,
                "media_bytes": embedded_bytes,
                "unavailable_attachments": stats["unavailable"], "blocked_attachments": stats["blocked"],
                "scope": scope or {}}
        page.write('<script type="application/json" id="catalog">{"meta":' + script_json(meta))
        for name, path in (("customers", customers_path), ("entries", toc_path)):
            page.write(f',"{name}":[')
            with path.open("rb") as lines:
                first = True
                for line in lines:
                    budget.check()
                    page.write((b"" if first else b",") + line.rstrip(b"\n"))
                    first = False
            page.write("]")
            budget.discard(path)
        page.write("}</script>\n")
        page.write(tail)
        page.close()
        shutil.rmtree(media_dir, ignore_errors=True)
        budget.check(force=True)
        return archive_path
    except BaseException:
        # 先关闭句柄再删除；Windows 下未关闭的文件会导致任务目录无法清理。
        for output in opened:
            output.close()
        if state is not None and state.get("zip") is not None:
            state["zip"].close()
        archive_path.unlink(missing_ok=True)
        media_path.unlink(missing_ok=True)
        raise
