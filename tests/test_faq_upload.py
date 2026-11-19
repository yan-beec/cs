"""FAQ 上传校验、规范命名及失败时保留原工作簿的回归测试。"""

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook

from ics_agent.app.admin import files, routes as admin
from ics_agent.app.core.auth import require_admin
from ics_agent.faq import sync


CHANNELS = ("xingliao", "yanliao", "xingyingtong", "wechat")
HEADERS = ("question", "answer", "need_human", "enabled")


def workbook_bytes(*, headers=HEADERS, rows=(("问题", "回答", 0, 1),)):
    workbook = Workbook()
    if headers is not None:
        workbook.active.append(headers)
    for row in rows:
        workbook.active.append(row)
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def faq_path(root: Path, channel_key="xingliao") -> Path:
    return root / "database" / f"channel_{channel_key}" / "最新问答对.xlsx"


def seed_workbook(root: Path, channel_key="xingliao"):
    destination = faq_path(root, channel_key)
    destination.parent.mkdir(parents=True, exist_ok=True)
    original = workbook_bytes(rows=(("原问题", "原回答", 0, 1),))
    destination.write_bytes(original)
    return destination, original


def assert_original_preserved(destination: Path, original: bytes):
    assert destination.read_bytes() == original
    assert set(destination.parent.iterdir()) == {destination}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(files, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        files,
        "get_faq_settings",
        lambda channel_key=None: SimpleNamespace(
            faq_source_xlsx_path=(
                f"database/channel_{channel_key or 'xingliao'}/最新问答对.xlsx"
            )
        ),
    )
    monkeypatch.setattr(
        files,
        "get_settings",
        lambda: SimpleNamespace(admin_file_max_size_mb=1),
    )
    app = FastAPI()
    app.include_router(admin.router)
    app.dependency_overrides[require_admin] = lambda: object()
    with TestClient(app) as test_client:
        yield test_client


def test_arbitrary_excel_names_are_normalized_and_channels_stay_isolated(client, tmp_path):
    uploaded = {}
    for key in CHANNELS:
        uploaded[key] = workbook_bytes(rows=(("同一问题", key, 0, 1),))
        response = client.post(
            "/admin/api/files/faq/upload",
            params={"channel_key": key},
            files={"file": (f"{key}导出的问答.XLSX", uploaded[key], "application/octet-stream")},
        )
        assert response.status_code == 200, response.text
        assert response.json()["name"] == "最新问答对.xlsx"
        destination = faq_path(tmp_path, key)
        assert destination.read_bytes() == uploaded[key]
        assert set(destination.parent.iterdir()) == {destination}

    replacement = workbook_bytes(rows=(("同一问题", "新微信回答", 0, 1),))
    response = client.post(
        "/admin/api/files/faq/upload",
        params={"channel_key": "wechat"},
        files={"file": ("更新版本.xlsx", replacement)},
    )
    assert response.status_code == 200, response.text
    uploaded["wechat"] = replacement
    for key in CHANNELS:
        assert faq_path(tmp_path, key).read_bytes() == uploaded[key]
        preview = client.get("/admin/api/files/faq/content", params={"channel_key": key})
        assert preview.status_code == 200
        assert preview.json()["sheets"][0]["rows"][1][1] == (
            "新微信回答" if key == "wechat" else key
        )


def test_custom_configured_workbook_path_matches_upload_preview_and_download(client, monkeypatch, tmp_path):
    destination = tmp_path / "custom" / "配置的FAQ.xlsx"
    monkeypatch.setattr(
        files,
        "get_faq_settings",
        lambda channel_key=None: SimpleNamespace(faq_source_xlsx_path=str(destination)),
    )
    content = workbook_bytes()
    response = client.post(
        "/admin/api/files/faq/upload",
        files={"file": ("任意文件名.xlsx", content)},
    )
    assert response.status_code == 200, response.text
    assert response.json()["name"] == destination.name
    assert destination.read_bytes() == content
    assert not faq_path(tmp_path).exists()
    assert client.get("/admin/api/files/faq").json()["name"] == destination.name
    assert client.get("/admin/api/files/faq/content").status_code == 200
    assert client.get("/admin/api/files/faq/download").content == content


@pytest.mark.parametrize("filename", ("问答.xls", "问答.xlsm", "问答.csv", "问答.pdf", "问答.xlsx.txt"))
def test_non_xlsx_filename_is_rejected_without_overwriting(client, tmp_path, filename):
    destination, original = seed_workbook(tmp_path)
    response = client.post(
        "/admin/api/files/faq/upload",
        files={"file": (filename, workbook_bytes(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert response.status_code == 400
    assert ".xlsx" in response.json()["detail"]
    assert_original_preserved(destination, original)


def unrelated_zip_bytes():
    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr("notes.txt", "This is not an Excel workbook.")
    return stream.getvalue()


def word_office_zip_bytes():
    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '</Types>',
        )
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>',
        )
    return stream.getvalue()


@pytest.mark.parametrize(
    "content",
    (b"not an Excel workbook", b"", unrelated_zip_bytes(), word_office_zip_bytes(), workbook_bytes()[:-128]),
    ids=("renamed-text", "empty-file", "unrelated-zip", "renamed-word-office", "truncated-workbook"),
)
def test_fake_or_damaged_xlsx_is_rejected_without_overwriting(client, tmp_path, content):
    destination, original = seed_workbook(tmp_path)
    response = client.post(
        "/admin/api/files/faq/upload",
        files={"file": ("最新问答对.xlsx", content)},
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"]
    assert_original_preserved(destination, original)


def test_damaged_workbook_closes_source_before_cleaning_temporary_file(client, monkeypatch, tmp_path):
    destination, original = seed_workbook(tmp_path)
    sources = []

    def fail_loading(source, **kwargs):
        sources.append(source)
        raise KeyError("simulated damaged workbook package")

    monkeypatch.setattr(sync, "load_workbook", fail_loading)
    response = client.post(
        "/admin/api/files/faq/upload",
        files={"file": ("损坏工作簿.xlsx", workbook_bytes())},
    )
    assert response.status_code == 400, response.text
    assert len(sources) == 1
    assert sources[0].closed
    assert_original_preserved(destination, original)


@pytest.mark.parametrize(
    ("headers", "rows"),
    (
        (None, ()),
        (HEADERS, ()),
        (("question", "answer"), (("问题", "回答"),)),
        ((*HEADERS, "answer"), (("问题", "回答", 0, 1, "重复列"),)),
        (HEADERS, (("", "回答", 0, 1),)),
        (HEADERS, (("问题", "", 0, 1),)),
        (HEADERS, (("问题", "回答", 0, 1), ("问题", "另一回答", 0, 1))),
        (HEADERS, (("问题", "回答", 2, 1),)),
        (HEADERS, (("问题", "回答", 0, "yes"),)),
    ),
    ids=("empty-sheet", "no-data", "missing-columns", "duplicate-column", "empty-question", "empty-answer", "duplicate-question", "invalid-need-human", "invalid-enabled"),
)
def test_workbook_that_cannot_be_imported_is_rejected_without_overwriting(client, tmp_path, headers, rows):
    destination, original = seed_workbook(tmp_path)
    response = client.post(
        "/admin/api/files/faq/upload",
        files={"file": ("新问答.xlsx", workbook_bytes(headers=headers, rows=rows))},
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"]
    assert_original_preserved(destination, original)


def test_invalid_first_upload_leaves_no_workbook_or_temporary_files(client, tmp_path):
    response = client.post(
        "/admin/api/files/faq/upload",
        files={"file": ("伪造.xlsx", b"not a workbook")},
    )
    assert response.status_code == 400, response.text
    assert not faq_path(tmp_path).exists()
    assert not any(path.is_file() for path in tmp_path.rglob("*"))


def test_oversized_upload_preserves_original_and_cleans_temporary_files(client, tmp_path):
    destination, original = seed_workbook(tmp_path)
    response = client.post(
        "/admin/api/files/faq/upload",
        files={"file": ("过大.xlsx", b"x" * (1024 * 1024 + 1))},
    )
    assert response.status_code == 400
    assert "1 MB" in response.json()["detail"]
    assert_original_preserved(destination, original)


@pytest.mark.parametrize("failed_operation", ("fsync", "replace"))
def test_io_failure_preserves_original_and_cleans_temporary_files(client, monkeypatch, tmp_path, failed_operation):
    destination, original = seed_workbook(tmp_path)

    def fail(*args, **kwargs):
        raise OSError("simulated upload storage failure")

    monkeypatch.setattr(files.os, failed_operation, fail)
    response = client.post(
        "/admin/api/files/faq/upload",
        files={"file": ("新问答.xlsx", workbook_bytes())},
    )
    assert response.status_code == 503, response.text
    assert_original_preserved(destination, original)
