# 模块职责：提供管理员页面使用的知识库和 FAQ 文件安全读写能力。

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from mimetypes import guess_type
from pathlib import Path
from typing import BinaryIO

from fastapi import UploadFile
from openpyxl import load_workbook

from .config import PROJECT_ROOT, Settings, get_settings
from ics_agent.faq.config import get_faq_settings
from ics_agent.rag.config import get_rag_settings


TEXT_PREVIEW_SUFFIXES = {
    ".csv",
    ".cfg",
    ".html",
    ".ini",
    ".json",
    ".jsonl",
    ".log",
    ".md",
    ".py",
    ".rst",
    ".sql",
    ".toml",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
MAX_PREVIEW_BYTES = 5 * 1024 * 1024


class AdminFileOperationError(ValueError):
    """管理员文件操作无效时抛出。"""


def resolve_project_path(path_value: str | Path) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _ensure_root(root: Path) -> Path:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def safe_child_path(
    root: Path,
    relative_path: str,
    *,
    allow_missing: bool = False,
) -> Path:
    """解析相对路径，并拒绝越出 ``root`` 的路径穿越。"""
    normalized = relative_path.replace("\\", "/").strip()
    if not normalized or normalized.startswith("/"):
        raise AdminFileOperationError("文件路径不能为空且必须是相对路径。")

    root = _ensure_root(root)
    candidate = (root / normalized).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise AdminFileOperationError("文件路径超出允许的目录范围。") from exc

    if not allow_missing and not candidate.exists():
        raise FileNotFoundError(candidate)
    return candidate


def relative_name(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def file_item(root: Path, path: Path) -> dict[str, object]:
    stat = path.stat()
    return {
        "name": relative_name(root, path),
        "size": stat.st_size,
        "modified_at": datetime.fromtimestamp(
            stat.st_mtime
        ).isoformat(timespec="seconds"),
        "suffix": path.suffix.lower(),
    }


def list_files(root: Path) -> list[dict[str, object]]:
    root = _ensure_root(root)
    files = sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: relative_name(root, path).casefold(),
    )
    return [file_item(root, path) for path in files]


def _copy_upload_to_path(
    upload: BinaryIO,
    destination: Path,
    *,
    max_bytes: int,
) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=".upload",
            dir=destination.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            while True:
                chunk = upload.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise AdminFileOperationError(
                        f"文件大小不能超过 {max_bytes // 1024 // 1024} MB。"
                    )
                temporary_file.write(chunk)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
        return total
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def save_upload(
    upload: UploadFile,
    destination: Path,
    *,
    settings: Settings | None = None,
) -> dict[str, object]:
    runtime_settings = settings or get_settings()
    _copy_upload_to_path(
        upload.file,
        destination,
        max_bytes=runtime_settings.admin_file_max_size_mb * 1024 * 1024,
    )
    return file_item(destination.parent, destination)


def delete_file(root: Path, relative_path: str) -> dict[str, object]:
    path = safe_child_path(root, relative_path)
    if not path.is_file():
        raise AdminFileOperationError("只能删除文件。")
    path.unlink()
    return {"name": relative_name(root, path), "deleted": True}


def file_media_type(path: Path) -> str:
    return guess_type(path.name)[0] or "application/octet-stream"


def knowledge_file_preview(
    root: Path,
    relative_path: str,
) -> dict[str, object]:
    """返回知识库文件预览所需的文本内容或元数据。"""
    path = safe_child_path(root, relative_path)
    if not path.is_file():
        raise AdminFileOperationError("只能浏览文件。")
    if path.stat().st_size > MAX_PREVIEW_BYTES:
        raise AdminFileOperationError("文件超过 5 MB，不支持在线预览，请下载查看。")

    media_type = file_media_type(path)
    if path.suffix.lower() not in TEXT_PREVIEW_SUFFIXES:
        return {
            "kind": "binary",
            "name": relative_name(root, path),
            "media_type": media_type,
            "size": path.stat().st_size,
        }

    return {
        "kind": "text",
        "name": relative_name(root, path),
        "media_type": media_type,
        "content": path.read_text(encoding="utf-8", errors="replace"),
    }


def faq_workbook_preview(
    settings: Settings | None = None,
) -> dict[str, object]:
    """读取配置的 FAQ 工作簿，并转换为浏览器可安全使用的表格数据。"""
    path = faq_file_path(settings)
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.stat().st_size > MAX_PREVIEW_BYTES:
        raise AdminFileOperationError("FAQ 文件超过 5 MB，不支持在线预览，请下载查看。")

    sheets: list[dict[str, object]] = []
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        for worksheet in workbook.worksheets:
            rows: list[list[object]] = []
            for row in worksheet.iter_rows(values_only=True):
                values = [
                    "" if value is None else str(value)
                    for value in row
                ]
                if any(value.strip() for value in values):
                    rows.append(values)
                if len(rows) >= 2000:
                    break
            sheets.append(
                {
                    "name": worksheet.title,
                    "rows": rows,
                    "truncated": len(rows) >= 2000,
                }
            )
    finally:
        workbook.close()
    return {
        "name": path.name,
        "sheets": sheets,
    }


def faq_file_path(settings: Settings | None = None) -> Path:
    path = resolve_project_path(get_faq_settings().faq_source_xlsx_path)
    if path.suffix.lower() != ".xlsx":
        raise AdminFileOperationError("FAQ 文件配置必须是 .xlsx 文件。")
    return path


def knowledge_base_root(settings: Settings | None = None) -> Path:
    return resolve_project_path(
        get_rag_settings().knowledge_base_input_dir
    )


def faq_file_info(settings: Settings | None = None) -> dict[str, object]:
    path = faq_file_path(settings)
    if not path.exists():
        return {
            "name": path.name,
            "exists": False,
            "size": 0,
            "modified_at": None,
        }
    return {
        "name": path.name,
        "exists": True,
        "size": path.stat().st_size,
        "modified_at": datetime.fromtimestamp(
            path.stat().st_mtime
        ).isoformat(timespec="seconds"),
    }
