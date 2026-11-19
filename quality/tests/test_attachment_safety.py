"""质检附件安全口径：只有图片、视频、音频保留可打开的地址，其余一律拦截。"""
import pytest

from quality.attachment_safety import BLOCKED_REASON, attachment_kind, safe_attachment, safe_attachments, suffix_of
from quality.query.excel import _attachment_text
from quality.query.repository import _flatten


@pytest.mark.parametrize("item, kind", [
    ({"filename": "截图.PNG", "source_url": "https://c.test/blobs/x/%E6%88%AA%E5%9B%BE.PNG"}, "image"),
    ({"filename": "clip.mp4", "media_type": "video"}, "video"),
    ({"filename": "voice.amr"}, "audio"),
    ({"source_url": "https://c.test/blobs/x/a.jpg?sig=1"}, "image"),
    ({"media_type": "image/png"}, "image"),  # 无扩展名时才参考源端类型
])
def test_media_attachments_are_allowed(item, kind):
    assert attachment_kind(item) == kind
    assert safe_attachment(item)["blocked"] is False


@pytest.mark.parametrize("item", [
    {"filename": "报价单.exe", "media_type": "file", "source_url": "https://c.test/blobs/x/a.exe"},
    {"filename": "合同.pdf", "media_type": "application/pdf"},
    {"filename": "表.xlsx"}, {"filename": "a.zip"}, {"filename": "page.html"}, {"filename": "a.svg"},
    {"filename": "订单截图.jpg.exe", "media_type": "image"},  # 双扩展名伪装
    {"filename": "a.exe. ", "media_type": "image"},  # Windows 忽略结尾点和空格
    {"filename": "截图.png", "media_type": "image", "source_url": "https://c.test/blobs/x/run.bat"},  # 名字与链接不一致
    {"filename": "a.pdf", "media_type": "image/png"},  # 声明类型不可信
    {"filename": "blob", "media_type": "file"}, {"media_type": ""}, {},
])
def test_everything_else_is_blocked_without_any_openable_address(item):
    raw = {**item, "data_url": "https://c.test/raw", "thumb_url": "https://c.test/t", "metadata": {"data_url": "x"}}
    blocked = safe_attachment(raw)
    assert blocked["blocked"] is True and blocked["source_url"] is None and blocked["blocked_reason"] == BLOCKED_REASON
    assert not {"data_url", "thumb_url", "metadata"} & blocked.keys()
    assert blocked.get("filename") == item.get("filename")


def test_suffix_follows_windows_rules():
    assert suffix_of("C:\\a\\b.EXE") == ".exe" and suffix_of("a.exe .. ") == ".exe"
    assert suffix_of("noext") == "" and suffix_of("") == "" and suffix_of(None) == ""
    assert safe_attachments(None) is None and safe_attachments([1]) == [1]


def test_query_rows_and_excel_never_expose_blocked_addresses():
    row = _flatten({"message_id": 1, "data": {"attachments": [
        {"filename": "a.exe", "source_url": "https://c.test/a.exe", "metadata": {"data_url": "https://c.test/a.exe"}},
        {"filename": "b.png", "source_url": "https://c.test/b.png"}]}})
    exe, png = row["attachments"]
    assert exe["blocked"] and exe["source_url"] is None and "metadata" not in exe
    assert png["source_url"] == "https://c.test/b.png"
    text = _attachment_text(row["attachments"])
    assert "c.test/a.exe" not in text and "已安全拦截" in text and "https://c.test/b.png" in text
