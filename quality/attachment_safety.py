"""质检附件安全口径：归档后台、Excel 与离线留档只放行图片、视频和音频（语音消息）。

客户经 H5 可上传任意文件，源端声明的类型（Chatwoot file_type/content_type）由上传方决定，
不可信；因此按文件名和链接路径的扩展名判断。只要任何一个名字带有非音视频图片扩展名，
就视为不安全文件：去掉所有可打开的地址，只保留文件名并提示安全原因。没有扩展名的文件在
Windows 上无法直接运行，此时才参考源端声明的类型。
"""

from __future__ import annotations

from urllib.parse import unquote, urlsplit

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp", ".heic", ".heif"})
VIDEO_SUFFIXES = frozenset({".mp4", ".webm", ".ogv", ".mov", ".m4v", ".avi", ".mkv", ".3gp"})
AUDIO_SUFFIXES = frozenset({".mp3", ".ogg", ".m4a", ".wav", ".aac", ".amr", ".opus"})
KIND_BY_SUFFIX = {**{s: "image" for s in IMAGE_SUFFIXES}, **{s: "video" for s in VIDEO_SUFFIXES},
                  **{s: "audio" for s in AUDIO_SUFFIXES}}
MEDIA_KINDS = ("image", "video", "audio")
BLOCKED_REASON = "非图片/视频文件，为防范病毒已禁止打开；如需查看内容请让客户改发截图"
# 源端可能带上的各种地址字段；拦截时全部去掉，原始元数据也不再下发。
URL_KEYS = ("source_url", "data_url", "download_url", "url", "thumb_url", "file_url")


def suffix_of(name) -> str:
    """Windows 会忽略结尾的点和空格，"a.exe. " 实际按 .exe 运行。"""
    text = str(name or "").replace("\\", "/").rsplit("/", 1)[-1].rstrip(". \t\r\n")
    return "." + text.rsplit(".", 1)[1].lower() if "." in text else ""


def _url_name(url) -> str:
    try:
        return unquote(urlsplit(str(url or "")).path).rsplit("/", 1)[-1]
    except ValueError:
        return ""


def attachment_kind(item: dict) -> str | None:
    """返回 image/video/audio；不安全或无法确认时返回 None。"""
    names = [item.get("filename"), *(_url_name(item.get(key)) for key in URL_KEYS if item.get(key))]
    suffixes = [suffix_of(name) for name in names if name]
    suffixes = [suffix for suffix in suffixes if suffix]
    if suffixes:
        kinds = {KIND_BY_SUFFIX.get(suffix) for suffix in suffixes}
        return kinds.pop() if len(kinds) == 1 and None not in kinds else None
    declared = str(item.get("media_type") or "").lower()
    return next((kind for kind in MEDIA_KINDS if declared.startswith(kind)), None)


def safe_attachment(item):
    if not isinstance(item, dict):
        return item
    kind = attachment_kind(item)
    if kind:
        return {**item, "kind": kind, "blocked": False}
    blocked = {key: value for key, value in item.items() if key not in URL_KEYS and key != "metadata"}
    return {**blocked, "source_url": None, "kind": "file", "blocked": True, "blocked_reason": BLOCKED_REASON}


def safe_attachments(items):
    return [safe_attachment(item) for item in items] if isinstance(items, list) else items
