# 模块职责：递归加载知识库文件，进行 OCR、中文递归分块并输出 JSONL。

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

if __package__ in {None, ""}:
    _project_root = Path(__file__).resolve().parents[1]
    if str(_project_root) not in sys.path:
        sys.path.insert(0, str(_project_root))

from ics_agent.app.config import PROJECT_ROOT, Settings, get_settings
from ics_agent.app.logging_config import configure_logging, log_event


logger = logging.getLogger(__name__)
SUPPORTED_IMAGE_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
)
SUPPORTED_TEXT_EXTENSIONS = frozenset(
    {".md", ".markdown", ".txt", ".text", ".rst"}
)
MARKDOWN_EXTENSIONS = frozenset({".md", ".markdown"})
MARKDOWN_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})[ \t]+(.+?)\s*$")
MARKDOWN_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
MARKDOWN_SEPARATOR_RE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")


def _resolve_project_path(path_value: str | Path) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _load_langchain_components() -> tuple[Any, Any, dict[str, Any]]:
    """Load LangChain components only when the parser is actually executed."""
    try:
        from langchain_community.document_loaders import (
            BSHTMLLoader,
            CSVLoader,
            Docx2txtLoader,
            PyMuPDFLoader,
            TextLoader,
            UnstructuredExcelLoader,
            UnstructuredPowerPointLoader,
        )
        from langchain_core.documents import Document
        from langchain_text_splitters import RecursiveCharacterTextSplitter
    except ImportError as exc:
        raise RuntimeError(
            "Knowledge base parsing dependencies are missing. Install "
            "requirements.txt in the cs environment first."
        ) from exc

    loaders = {
        ".pdf": PyMuPDFLoader,
        ".docx": Docx2txtLoader,
        ".csv": CSVLoader,
        ".html": BSHTMLLoader,
        ".htm": BSHTMLLoader,
        ".pptx": UnstructuredPowerPointLoader,
        ".xlsx": UnstructuredExcelLoader,
        ".xls": UnstructuredExcelLoader,
    }
    for extension in SUPPORTED_TEXT_EXTENSIONS:
        loaders[extension] = TextLoader
    return Document, RecursiveCharacterTextSplitter, loaders


@lru_cache(maxsize=2)
def _load_tokenizer(tokenizer_dir: str) -> Any:
    """Load a local tokenizer used only to measure real model token counts."""
    try:
        from transformers import AutoTokenizer
    except ImportError as exc:
        raise RuntimeError(
            "Transformers is required to split documents by model tokens."
        ) from exc

    tokenizer_path = _resolve_project_path(tokenizer_dir)
    if not tokenizer_path.is_dir():
        raise FileNotFoundError(
            f"Knowledge base tokenizer directory does not exist: {tokenizer_path}"
        )
    return AutoTokenizer.from_pretrained(
        str(tokenizer_path),
        local_files_only=True,
        use_fast=True,
    )


def _token_count(tokenizer: Any, text: str) -> int:
    return len(tokenizer.encode(text, add_special_tokens=False))


def _build_splitter(
    tokenizer: Any,
    splitter_class: Any,
    chunk_size_tokens: int,
    chunk_overlap_tokens: int,
) -> Any:
    if chunk_size_tokens < 1:
        raise ValueError("chunk_size_tokens must be at least 1.")
    if chunk_overlap_tokens < 0:
        raise ValueError("chunk_overlap_tokens cannot be negative.")
    if chunk_overlap_tokens >= chunk_size_tokens:
        raise ValueError(
            "chunk_overlap_tokens must be smaller than chunk_size_tokens."
        )

    # 中文优先按段落、换行、句号和中文标点递归拆分，最后才退化到字符级。
    separators = [
        "\n\n",
        "\n",
        "。",
        "！",
        "？",
        "；",
        "：",
        "，",
        "、",
        " ",
        "",
    ]
    return splitter_class(
        separators=separators,
        chunk_size=chunk_size_tokens,
        chunk_overlap=chunk_overlap_tokens,
        length_function=lambda text: _token_count(tokenizer, text),
        keep_separator=True,
        add_start_index=True,
    )


@dataclass(frozen=True)
class _MarkdownSection:
    """Markdown body text with the heading hierarchy active above it."""

    body: str
    body_start_index: int
    heading_path: tuple[tuple[int, str], ...]


def _parse_markdown_sections(text: str) -> list[_MarkdownSection]:
    """Split Markdown into non-empty body sections without losing heading paths."""
    sections: list[_MarkdownSection] = []
    heading_stack: list[tuple[int, str]] = []
    body_lines: list[str] = []
    body_start_index: int | None = None
    offset = 0
    fence_marker: str | None = None

    def flush_body() -> None:
        nonlocal body_lines, body_start_index
        if body_lines and body_start_index is not None:
            sections.append(
                _MarkdownSection(
                    body="".join(body_lines),
                    body_start_index=body_start_index,
                    heading_path=tuple(heading_stack),
                )
            )
        body_lines = []
        body_start_index = None

    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        fence_match = MARKDOWN_FENCE_RE.match(line)

        if fence_marker is not None:
            if (
                fence_match is not None
                and fence_match.group(1)[0] == fence_marker[0]
                and len(fence_match.group(1)) >= len(fence_marker)
            ):
                fence_marker = None
            if body_start_index is None:
                body_start_index = offset
            body_lines.append(raw_line)
            offset += len(raw_line)
            continue

        if fence_match is not None:
            if body_start_index is None:
                body_start_index = offset
            body_lines.append(raw_line)
            fence_marker = fence_match.group(1)
            offset += len(raw_line)
            continue

        heading_match = MARKDOWN_HEADING_RE.match(line)
        if heading_match is not None:
            flush_body()
            level = len(heading_match.group(1))
            title = re.sub(r"[ \t]+#+[ \t]*$", "", heading_match.group(2))
            title = title.strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            if title:
                heading_stack.append((level, title))
            offset += len(raw_line)
            continue

        if body_start_index is None:
            body_start_index = offset
        body_lines.append(raw_line)
        offset += len(raw_line)

    flush_body()
    return sections


def _has_substantive_content(text: str) -> bool:
    """Return False for blank, separator-only, or heading-only text."""
    content_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if MARKDOWN_HEADING_RE.fullmatch(stripped):
            continue
        if MARKDOWN_SEPARATOR_RE.fullmatch(stripped):
            continue
        content_lines.append(stripped)
    if not content_lines:
        return False
    return re.search(r"[A-Za-z0-9\u3400-\u9fff]", "\n".join(content_lines)) is not None


def _truncate_to_tokens(tokenizer: Any, text: str, max_tokens: int) -> str:
    if max_tokens < 1:
        return ""
    token_ids = tokenizer.encode(text, add_special_tokens=False)
    if len(token_ids) <= max_tokens:
        return text
    return tokenizer.decode(
        token_ids[:max_tokens],
        skip_special_tokens=True,
        clean_up_tokenization_spaces=False,
    ).strip()


def _heading_context(
    heading_path: tuple[tuple[int, str], ...],
    tokenizer: Any,
    chunk_size_tokens: int,
) -> tuple[str, int]:
    """Build the longest useful heading path that fits inside the chunk budget."""
    if not heading_path:
        return "", 0

    selected = [heading_path[-1]]
    for heading in reversed(heading_path[:-1]):
        candidate = [heading, *selected]
        candidate_text = "\n".join(
            f"{'#' * level} {title}" for level, title in candidate
        )
        if _token_count(tokenizer, f"{candidate_text}\n\n") <= max(
            1, chunk_size_tokens - 8
        ):
            selected = candidate

    context = "\n".join(
        f"{'#' * level} {title}" for level, title in selected
    ).strip()
    prefix = f"{context}\n\n"
    prefix_tokens = _token_count(tokenizer, prefix)
    if prefix_tokens >= chunk_size_tokens:
        last_level, last_title = selected[-1]
        marker = f"{'#' * last_level} "
        available = max(1, chunk_size_tokens - _token_count(tokenizer, marker) - 2)
        truncated_title = _truncate_to_tokens(
            tokenizer,
            last_title,
            available,
        )
        context = f"{marker}{truncated_title}".strip()
        prefix = f"{context}\n\n"
        prefix_tokens = _token_count(tokenizer, prefix)
    return context, prefix_tokens


def _split_markdown_document(
    document: Any,
    splitter_class: Any,
    tokenizer: Any,
    chunk_size_tokens: int,
    chunk_overlap_tokens: int,
    include_heading_context: bool,
) -> list[Any]:
    """Split Markdown sections while repeating their heading context per chunk."""
    split_documents: list[Any] = []
    for section in _parse_markdown_sections(document.page_content):
        body = section.body.strip()
        if not _has_substantive_content(body):
            continue

        context = ""
        prefix_tokens = 0
        if include_heading_context:
            context, prefix_tokens = _heading_context(
                section.heading_path,
                tokenizer,
                chunk_size_tokens,
            )
        content_budget = max(1, chunk_size_tokens - prefix_tokens)
        content_overlap = min(
            chunk_overlap_tokens,
            max(0, content_budget - 1),
        )
        section_splitter = _build_splitter(
            tokenizer,
            splitter_class,
            content_budget,
            content_overlap,
        )
        body_leading_offset = len(section.body) - len(section.body.lstrip())
        body = body.strip()
        section_metadata = {
            **document.metadata,
            "heading_path": [title for _, title in section.heading_path],
            "heading_levels": [level for level, _ in section.heading_path],
            "heading_context": context,
            "section_start_index": section.body_start_index + body_leading_offset,
        }
        split_documents.extend(
            section_splitter.create_documents(
                [body],
                metadatas=[section_metadata],
            )
        )
    return split_documents


def _normalise_source(
    metadata: dict[str, Any],
    source_path: Path,
    input_dir: Path,
) -> dict[str, Any]:
    normalised = dict(metadata)
    try:
        relative_source = source_path.resolve().relative_to(input_dir.resolve())
        normalised["source"] = relative_source.as_posix()
    except ValueError:
        normalised["source"] = str(source_path.resolve())
    normalised["source_path"] = str(source_path.resolve())
    normalised["file_name"] = source_path.name
    normalised["file_extension"] = source_path.suffix.lower()
    return normalised


def _load_with_langchain(
    source_path: Path,
    input_dir: Path,
    loader_class: Any,
) -> list[Any]:
    kwargs: dict[str, Any] = {}
    if loader_class.__name__ == "TextLoader":
        # 当前知识库文本统一按 UTF-8 保存，显式指定编码可避免
        # TextLoader 为编码探测额外引入 chardet 依赖。
        kwargs["encoding"] = "utf-8"

    loader = loader_class(str(source_path), **kwargs)
    documents = loader.load()
    for document in documents:
        document.metadata = _normalise_source(
            document.metadata,
            source_path,
            input_dir,
        )
        document.metadata["loader"] = loader_class.__name__
    return documents


@lru_cache(maxsize=1)
def _load_ocr_engine() -> Any:
    try:
        from rapidocr import RapidOCR
    except ImportError as exc:
        raise RuntimeError(
            "RapidOCR is required to parse image files. Install rapidocr "
            "in the cs environment first."
        ) from exc
    return RapidOCR()


def _extract_ocr_text(result: Any) -> tuple[list[str], list[float]]:
    """Support both current RapidOCR output objects and older tuple output."""
    texts = getattr(result, "txts", None)
    scores = getattr(result, "scores", None)
    if texts is None and isinstance(result, tuple):
        if len(result) >= 2:
            texts = result[1]
        if len(result) >= 3:
            scores = result[2]

    text_list = [str(text).strip() for text in (texts or [])]
    score_list = [float(score) for score in (scores or [])]
    if len(score_list) < len(text_list):
        score_list.extend([1.0] * (len(text_list) - len(score_list)))
    return text_list, score_list


def _load_image_with_ocr(
    source_path: Path,
    input_dir: Path,
    text_score: float,
) -> list[Any]:
    Document, _, _ = _load_langchain_components()
    result = _load_ocr_engine()(
        source_path,
        text_score=text_score,
    )
    texts, scores = _extract_ocr_text(result)
    recognised_lines = [
        text
        for text, score in zip(texts, scores)
        if text and score >= text_score
    ]
    metadata = _normalise_source(
        {"ocr": True, "ocr_engine": "RapidOCR"},
        source_path,
        input_dir,
    )
    metadata["ocr_text_count"] = len(recognised_lines)
    return [
        Document(
            page_content="\n".join(recognised_lines),
            metadata=metadata,
        )
    ]


def _load_file(
    source_path: Path,
    input_dir: Path,
    settings: Settings,
    loaders: dict[str, Any],
) -> list[Any]:
    extension = source_path.suffix.lower()
    if extension in SUPPORTED_IMAGE_EXTENSIONS:
        if not settings.knowledge_base_ocr_enabled:
            log_event(
                logger,
                logging.INFO,
                "knowledge_base.image.skipped",
                source=source_path.name,
                reason="ocr_disabled",
            )
            return []
        return _load_image_with_ocr(
            source_path,
            input_dir,
            settings.knowledge_base_ocr_text_score,
        )

    loader_class = loaders.get(extension)
    if loader_class is None:
        log_event(
            logger,
            logging.WARNING,
            "knowledge_base.file.skipped",
            source=source_path.name,
            reason="unsupported_extension",
        )
        return []
    return _load_with_langchain(source_path, input_dir, loader_class)


def _split_documents(
    documents: Iterable[Any],
    splitter: Any,
    splitter_class: Any,
    tokenizer: Any,
    chunk_size_tokens: int,
    chunk_overlap_tokens: int,
    include_heading_context: bool,
    drop_heading_only_chunks: bool,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for document in documents:
        content = document.page_content.strip()
        if not content:
            continue

        is_markdown = (
            str(document.metadata.get("file_extension", "")).lower()
            in MARKDOWN_EXTENSIONS
        )
        if is_markdown and include_heading_context:
            chunks = _split_markdown_document(
                document,
                splitter_class,
                tokenizer,
                chunk_size_tokens,
                chunk_overlap_tokens,
                include_heading_context=True,
            )
        else:
            chunks = splitter.split_documents([document])

        prepared_chunks: list[tuple[str, dict[str, Any]]] = []
        for chunk in chunks:
            raw_text = chunk.page_content.strip()
            if not raw_text:
                continue
            metadata = dict(chunk.metadata)
            context = str(metadata.get("heading_context", "")).strip()
            text = (
                f"{context}\n\n{raw_text}".strip()
                if is_markdown and include_heading_context and context
                else raw_text
            )
            if drop_heading_only_chunks and not _has_substantive_content(text):
                continue

            if is_markdown and include_heading_context:
                prefix = f"{context}\n\n" if context else ""
                if _token_count(tokenizer, text) > chunk_size_tokens:
                    body_budget = max(
                        1,
                        chunk_size_tokens - _token_count(tokenizer, prefix),
                    )
                    raw_text = _truncate_to_tokens(
                        tokenizer,
                        raw_text,
                        body_budget,
                    )
                    text = f"{prefix}{raw_text}".strip()
                if "section_start_index" in metadata:
                    local_start = int(metadata.get("start_index", 0))
                    metadata["start_index"] = (
                        int(metadata["section_start_index"]) + local_start
                    )
            prepared_chunks.append((text, metadata))

        chunk_count = len(prepared_chunks)
        for chunk_index, (text, metadata) in enumerate(prepared_chunks):
            token_count = _token_count(tokenizer, text)
            source = str(metadata.get("source", ""))
            stable_id = hashlib.sha256(
                f"{source}\n{chunk_index}\n{text}".encode("utf-8")
            ).hexdigest()
            records.append(
                {
                    "id": stable_id,
                    "text": text,
                    "token_count": token_count,
                    "metadata": {
                        **metadata,
                        "chunk_index": chunk_index,
                        "chunk_count": chunk_count,
                        "token_count": token_count,
                    },
                }
            )
    return records


def parse_knowledge_base(
    settings: Settings | None = None,
    *,
    input_dir: str | Path | None = None,
    output_path: str | Path | None = None,
    chunk_size_tokens: int | None = None,
    chunk_overlap_tokens: int | None = None,
    fail_fast: bool = False,
) -> dict[str, Any]:
    """Recursively parse and chunk all supported files under the input directory."""
    runtime_settings = settings or get_settings()
    source_dir = _resolve_project_path(
        input_dir or runtime_settings.knowledge_base_input_dir
    )
    target_path = _resolve_project_path(
        output_path or runtime_settings.knowledge_base_output_path
    )
    if not source_dir.is_dir():
        raise FileNotFoundError(f"Knowledge base directory does not exist: {source_dir}")

    effective_chunk_size = (
        chunk_size_tokens
        if chunk_size_tokens is not None
        else runtime_settings.knowledge_base_chunk_size_tokens
    )
    effective_overlap = (
        chunk_overlap_tokens
        if chunk_overlap_tokens is not None
        else runtime_settings.knowledge_base_chunk_overlap_tokens
    )
    tokenizer = _load_tokenizer(runtime_settings.knowledge_base_tokenizer_dir)
    _, splitter_class, loaders = _load_langchain_components()
    splitter = _build_splitter(
        tokenizer,
        splitter_class,
        effective_chunk_size,
        effective_overlap,
    )

    source_files = sorted(
        (
            path
            for path in source_dir.rglob("*")
            if path.is_file()
        ),
        key=lambda path: str(path).lower(),
    )
    output_records: list[dict[str, Any]] = []
    parsed_file_count = 0
    skipped_file_count = 0
    error_files: list[dict[str, str]] = []
    for source_path in source_files:
        try:
            documents = _load_file(
                source_path,
                source_dir,
                runtime_settings,
                loaders,
            )
            records = _split_documents(
                documents,
                splitter,
                splitter_class,
                tokenizer,
                effective_chunk_size,
                effective_overlap,
                runtime_settings.knowledge_base_include_heading_context,
                runtime_settings.knowledge_base_drop_heading_only_chunks,
            )
            if records:
                parsed_file_count += 1
                output_records.extend(records)
                log_event(
                    logger,
                    logging.INFO,
                    "knowledge_base.file.parsed",
                    source=source_path.name,
                    chunks=len(records),
                )
            else:
                skipped_file_count += 1
        except Exception as exc:
            error_files.append(
                {
                    "source": str(source_path),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            log_event(
                logger,
                logging.ERROR,
                "knowledge_base.file.failed",
                source=source_path.name,
                error=type(exc).__name__,
            )
            if fail_fast:
                raise

    target_path.parent.mkdir(parents=True, exist_ok=True)
    with target_path.open("w", encoding="utf-8", newline="\n") as output_file:
        for record in output_records:
            output_file.write(
                json.dumps(record, ensure_ascii=False)
                + "\n"
            )

    manifest = {
        "input_dir": str(source_dir),
        "output_path": str(target_path),
        "source_file_count": len(source_files),
        "parsed_file_count": parsed_file_count,
        "skipped_file_count": skipped_file_count,
        "failed_file_count": len(error_files),
        "chunk_count": len(output_records),
        "chunk_size_tokens": effective_chunk_size,
        "chunk_overlap_tokens": effective_overlap,
        "include_heading_context": (
            runtime_settings.knowledge_base_include_heading_context
        ),
        "drop_heading_only_chunks": (
            runtime_settings.knowledge_base_drop_heading_only_chunks
        ),
        "tokenizer_dir": str(
            _resolve_project_path(runtime_settings.knowledge_base_tokenizer_dir)
        ),
        "errors": error_files,
    }
    manifest_path = target_path.with_suffix(".manifest.json")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log_event(
        logger,
        logging.INFO,
        "knowledge_base.parse.completed",
        source_file_count=len(source_files),
        chunk_count=len(output_records),
        failed_file_count=len(error_files),
        output_path=target_path,
    )
    return manifest


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        description="Recursively parse and chunk the knowledge base.",
    )
    parser.add_argument(
        "--input-dir",
        default=settings.knowledge_base_input_dir,
        help="Knowledge base input directory.",
    )
    parser.add_argument(
        "--output",
        default=settings.knowledge_base_output_path,
        help="JSONL output path.",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=settings.knowledge_base_chunk_size_tokens,
        help="Maximum model tokens per chunk.",
    )
    parser.add_argument(
        "--chunk-overlap",
        type=int,
        default=settings.knowledge_base_chunk_overlap_tokens,
        help="Model token overlap between adjacent chunks.",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop immediately when one file fails to parse.",
    )
    args = parser.parse_args()
    configure_logging()
    manifest = parse_knowledge_base(
        settings=settings,
        input_dir=args.input_dir,
        output_path=args.output,
        chunk_size_tokens=args.chunk_size,
        chunk_overlap_tokens=args.chunk_overlap,
        fail_fast=args.fail_fast,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
