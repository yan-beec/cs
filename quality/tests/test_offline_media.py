"""使用真实 HTTP 服务验证附件复制、跳转及中断。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

from quality.offline.bundle import BundleLimit
from quality.offline.media import MediaDownloader, MediaUnavailable


@pytest.fixture
def source():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/gone":
                self.send_error(410)
                return
            if self.path in ("/redirect", "/outside"):
                self.send_response(302)
                self.send_header("Location", "/video" if self.path == "/redirect" else "http://untrusted.invalid/file")
                self.end_headers()
                return
            data = b"original-video-bytes"
            self.send_response(206 if self.path == "/partial" else 200)
            self.send_header("Content-Type", "video/mp4")
            if self.path != "/no-length":
                self.send_header("Content-Length", str(len(data) + (5 if self.path == "/truncated" else 0)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_real_media_copy_redirect_expiry_and_incomplete_bytes(source, tmp_path):
    downloader = MediaDownloader([source])
    target = tmp_path / "media"
    assert downloader.download({"source_url": source + "/redirect"}, target, lambda: None) == "video/mp4"
    assert target.read_bytes() == b"original-video-bytes"
    for suffix in ("/gone", "/outside", "/truncated"):
        with pytest.raises(MediaUnavailable):
            downloader.download({"source_url": source + suffix}, target, lambda: None)
        assert not target.exists()


def test_media_does_not_swallow_archive_budget_or_cancellation(source, tmp_path):
    def abort():
        raise BundleLimit("总预算已耗尽")
    with pytest.raises(BundleLimit):
        MediaDownloader([source]).download({"source_url": source + "/video"}, tmp_path / "media", abort)


def test_real_media_file_size_limit_removes_partial_data(source, tmp_path):
    with pytest.raises(MediaUnavailable, match="大小限制"):
        MediaDownloader([source], max_file_bytes=4).download(
            {"source_url": source + "/video"}, tmp_path / "media", lambda: None)
    assert not (tmp_path / "media").exists()


def test_per_attachment_limit_is_capped_by_global_limit(source, tmp_path):
    from quality.offline.media import MediaTooLarge
    # 按类别传入的上限更小时生效；更大时仍受全局单文件上限约束。
    with pytest.raises(MediaTooLarge):
        MediaDownloader([source]).download({"source_url": source + "/video"}, tmp_path / "a", lambda: None, limit=4)
    with pytest.raises(MediaTooLarge):
        MediaDownloader([source], max_file_bytes=4).download(
            {"source_url": source + "/video"}, tmp_path / "b", lambda: None, limit=10**9)
    assert not (tmp_path / "a").exists() and not (tmp_path / "b").exists()


@pytest.mark.parametrize("suffix", ["/video", "/no-length", "/partial"])
def test_source_size_mismatch_and_partial_response_are_not_complete(source, tmp_path, suffix):
    target = tmp_path / "media"
    with pytest.raises(MediaUnavailable, match="不完整"):
        MediaDownloader([source]).download({"source_url": source + suffix, "expected_byte_count": 1000}, target, lambda: None)
    assert not target.exists()
