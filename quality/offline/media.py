"""仅从明确配置的附件来源流式下载。"""
from http.client import HTTPException
from pathlib import Path
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class MediaUnavailable(ValueError):
    pass


class MediaTooLarge(MediaUnavailable):
    """超过单文件上限；原始链接可能仍有效，留档改为保留链接。"""


def origin(url):
    try:
        parts = urlsplit(url)
        if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password
                or any(ord(c) <= 32 for c in url)):
            raise ValueError()
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return parts.scheme.lower(), parts.hostname.lower(), port
    except (ValueError, TypeError):
        raise MediaUnavailable("附件地址不可用") from None


class _Redirect(HTTPRedirectHandler):
    def __init__(self, validate):
        self.validate = validate

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, self.validate(newurl))


class MediaDownloader:
    def __init__(self, origins, *, replacements=None, timeout=20, max_file_bytes=2 * 1024**3):
        self.origins = {origin(url) for url in origins if url}
        self.replacements = {origin(a): b for a, b in (replacements or {}).items() if a and b}
        self.timeout = timeout
        self.max_file_bytes = max_file_bytes
        self.opener = build_opener(_Redirect(self.validate))

    def validate(self, url):
        source = origin(url)
        if source not in self.origins:
            raise MediaUnavailable("附件来源未配置，无法下载")
        target = self.replacements.get(source)
        if target:
            parts, replacement = urlsplit(url), urlsplit(target)
            url = urlunsplit((replacement.scheme, replacement.netloc, parts.path, parts.query, ""))
            if origin(url) not in self.origins:
                raise MediaUnavailable("附件来源未配置，无法下载")
        return url

    def download(self, item, destination: Path, check, limit=None):
        url = self.validate(item.get("source_url") or "")
        expected = item.get("expected_byte_count")
        # 按附件类别传入的上限（图片 / 视频不同）与全局单文件上限取较小值。
        maximum = min(self.max_file_bytes, limit) if limit else self.max_file_bytes
        if isinstance(expected, int) and expected > maximum:
            raise MediaTooLarge("附件超过单文件大小限制")
        written = 0
        try:
            check()
            # 签名附件地址无需 API 令牌，不向附件服务器转发管理员凭据。
            with self.opener.open(Request(url, headers={"User-Agent": "ICS-Offline-Archive/1"}), timeout=self.timeout) as response:
                if response.status != 200:
                    raise MediaUnavailable("附件下载不完整，未收到完整响应")
                content_type = response.headers.get_content_type()
                if content_type in {"text/html", "application/xhtml+xml"}:
                    raise MediaUnavailable("附件已失效")
                try:
                    length = int(response.headers["Content-Length"]) if response.headers.get("Content-Length") else None
                except ValueError:
                    raise MediaUnavailable("附件响应长度无效") from None
                if length is not None and length > maximum:
                    raise MediaTooLarge("附件超过单文件大小限制")
                with destination.open("wb") as output:
                    while True:
                        check()
                        block = response.read(256 * 1024)
                        if not block:
                            break
                        written += len(block)
                        if written > maximum:
                            raise MediaTooLarge("附件超过单文件大小限制")
                        output.write(block)
                        check()
                if length is not None and written != length:
                    raise MediaUnavailable("附件下载不完整")
                if isinstance(expected, int) and expected > 0 and written != expected:
                    raise MediaUnavailable("附件下载不完整，与源端记录大小不一致")
                return content_type
        except (HTTPError, URLError, TimeoutError, socket.timeout, ConnectionError, HTTPException, MediaUnavailable) as error:
            destination.unlink(missing_ok=True)
            if isinstance(error, MediaUnavailable):
                raise
            raise MediaUnavailable("附件已失效或暂时无法下载") from None
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
