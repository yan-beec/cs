# 模块职责：读取知识库分块 JSONL，使用本地 BGE-M3 生成向量并写入 ChromaDB。

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, ClassVar, Iterable

if __package__ in {None, ""}:
    _project_root = Path(__file__).resolve().parents[1]
    if str(_project_root) not in sys.path:
        sys.path.insert(0, str(_project_root))

from ics_agent.app.core.config import PROJECT_ROOT
from ics_agent.app.core.logging_config import configure_logging, log_event
from ics_agent.rag.config import RAGSettings, get_rag_settings
from knowledge_base_in.vector_store_lock import vector_store_lock


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VectorRecord:
    """一个可写入向量存储的解析分块。"""

    chunk_id: str
    text: str
    metadata: dict[str, Any]


def _resolve_project_path(path_value: str | Path) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _batched(items: list[Any], batch_size: int) -> Iterable[list[Any]]:
    for index in range(0, len(items), batch_size):
        yield items[index:index + batch_size]


def _load_chromadb() -> Any:
    try:
        import chromadb
    except ImportError as exc:
        raise RuntimeError(
            "ChromaDB is required to build the knowledge base vector store. "
            "Install requirements.txt in the cs environment first."
        ) from exc
    return chromadb


def _normalise_metadata_value(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return json.dumps(value, ensure_ascii=False)


def _normalise_metadata(metadata: dict[str, Any]) -> dict[str, str | int | float | bool]:
    normalised: dict[str, str | int | float | bool] = {}
    for key, value in metadata.items():
        normalised_value = _normalise_metadata_value(value)
        if normalised_value is not None:
            normalised[str(key)] = normalised_value
    return normalised


def _clear_collection(collection: Any, *, batch_size: int = 1000) -> int:
    """Remove all records while preserving the collection object itself."""
    existing = collection.get(include=[])
    existing_ids = [str(record_id) for record_id in existing.get("ids", [])]
    for batch in _batched(existing_ids, batch_size):
        collection.delete(ids=batch)
    return len(existing_ids)


def _load_active_segment_ids(sqlite_path: Path) -> set[str] | None:
    """读取仍被 Chroma 引用的 segment 目录名。"""
    if not sqlite_path.is_file():
        return None

    try:
        with sqlite3.connect(str(sqlite_path), timeout=5) as connection:
            rows = connection.execute("SELECT id FROM segments").fetchall()
    except sqlite3.Error as exc:
        log_event(
            logger,
            logging.WARNING,
            "knowledge_base.vector.storage.segment_scan_failed",
            path=sqlite_path,
            error_type=type(exc).__name__,
        )
        return None

    active_segment_ids: set[str] = set()
    for (segment_id,) in rows:
        if not segment_id:
            continue
        try:
            active_segment_ids.add(str(uuid.UUID(str(segment_id))))
        except (TypeError, ValueError) as exc:
            log_event(
                logger,
                logging.WARNING,
                "knowledge_base.vector.storage.segment_id_invalid",
                path=sqlite_path,
                error_type=type(exc).__name__,
            )
            return None
    return active_segment_ids


def _remove_orphaned_segment_dirs(
    target_dir: Path,
    active_segment_ids: set[str],
) -> int:
    """删除不再被 Chroma segments 表引用的旧 UUID 索引目录。"""
    if not target_dir.is_dir():
        return 0

    resolved_target_dir = target_dir.resolve()
    removed_count = 0
    for candidate in target_dir.iterdir():
        if not candidate.is_dir():
            continue
        if candidate.resolve().parent != resolved_target_dir:
            continue
        try:
            candidate_id = str(uuid.UUID(candidate.name))
        except ValueError:
            continue
        if candidate_id in active_segment_ids:
            continue
        shutil.rmtree(candidate)
        removed_count += 1
    return removed_count


def _compact_chroma_storage(
    client: Any,
    collection: Any,
    target_dir: Path,
) -> dict[str, Any]:
    """回收 Chroma WAL、压缩 SQLite 并删除旧 segment 文件。"""
    maintenance = {
        "vector_segment_persisted": False,
        "wal_purged": False,
        "sqlite_vacuumed": False,
        "orphaned_segment_dirs_removed": 0,
    }
    sqlite_path = target_dir / "chroma.sqlite3"
    chroma_system = getattr(client, "_system", None)
    sqlite_db: Any | None = None

    if chroma_system is not None:
        try:
            from chromadb.db.impl.sqlite import SqliteDB
            from chromadb.ingest.impl.utils import (
                trigger_vector_segments_max_seq_id_migration,
            )
            from chromadb.segment import SegmentManager, VectorReader

            sqlite_db = chroma_system.instance(SqliteDB)
            segment_manager = chroma_system.instance(SegmentManager)
            vector_segment = segment_manager.get_segment(
                collection.id,
                VectorReader,
            )
            persist_segment = getattr(vector_segment, "_persist", None)
            if callable(persist_segment):
                persist_segment()
                maintenance["vector_segment_persisted"] = True
            trigger_vector_segments_max_seq_id_migration(
                sqlite_db,
                segment_manager,
            )
            sqlite_db.purge_log(collection.id)
            maintenance["wal_purged"] = True
        except Exception as exc:
            log_event(
                logger,
                logging.WARNING,
                "knowledge_base.vector.storage.wal_purge_failed",
                path=target_dir,
                error_type=type(exc).__name__,
            )

    if sqlite_db is not None:
        try:
            sqlite_db.vacuum()
            maintenance["sqlite_vacuumed"] = True
        except Exception as exc:
            log_event(
                logger,
                logging.WARNING,
                "knowledge_base.vector.storage.vacuum_failed",
                path=target_dir,
                error_type=type(exc).__name__,
            )

        try:
            config = sqlite_db.config
            config.set_parameter("automatically_purge", True)
            sqlite_db.set_config(config)
        except Exception as exc:
            log_event(
                logger,
                logging.WARNING,
                "knowledge_base.vector.storage.auto_purge_config_failed",
                path=target_dir,
                error_type=type(exc).__name__,
            )
    elif sqlite_path.is_file():
        # Keep a best-effort fallback for tests and Chroma versions without the
        # private maintenance objects used above.
        try:
            with sqlite3.connect(str(sqlite_path), timeout=5) as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute("VACUUM")
            maintenance["sqlite_vacuumed"] = True
        except sqlite3.Error as exc:
            log_event(
                logger,
                logging.WARNING,
                "knowledge_base.vector.storage.fallback_vacuum_failed",
                path=sqlite_path,
                error_type=type(exc).__name__,
            )

    active_segment_ids = _load_active_segment_ids(sqlite_path)
    if active_segment_ids is not None:
        try:
            maintenance["orphaned_segment_dirs_removed"] = (
                _remove_orphaned_segment_dirs(
                    target_dir,
                    active_segment_ids,
                )
            )
        except OSError as exc:
            log_event(
                logger,
                logging.WARNING,
                "knowledge_base.vector.storage.orphan_cleanup_failed",
                path=target_dir,
                error_type=type(exc).__name__,
            )

    log_event(
        logger,
        logging.INFO,
        "knowledge_base.vector.storage.maintenance_completed",
        path=target_dir,
        **maintenance,
    )
    return maintenance


def load_chunk_records(chunks_path: str | Path) -> list[VectorRecord]:
    path = _resolve_project_path(chunks_path)
    if not path.is_file():
        raise FileNotFoundError(f"Knowledge base chunks file does not exist: {path}")

    records: list[VectorRecord] = []
    seen_ids: set[str] = set()
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Invalid JSON at {path}:{line_number}."
            ) from exc

        chunk_id = str(payload.get("id", "")).strip()
        text = str(payload.get("text", "")).strip()
        metadata = payload.get("metadata") or {}
        if not chunk_id:
            raise ValueError(f"Missing chunk id at {path}:{line_number}.")
        if chunk_id in seen_ids:
            raise ValueError(f"Duplicate chunk id at {path}:{line_number}: {chunk_id}")
        if not text:
            continue
        if not isinstance(metadata, dict):
            raise ValueError(f"Chunk metadata must be an object at {path}:{line_number}.")

        seen_ids.add(chunk_id)
        records.append(
            VectorRecord(
                chunk_id=chunk_id,
                text=text,
                metadata={
                    **metadata,
                    "chunk_id": chunk_id,
                    "text_token_count": payload.get("token_count"),
                },
            )
        )

    if not records:
        raise ValueError(f"No usable chunk records were found in {path}.")
    log_event(
        logger,
        logging.INFO,
        "knowledge_base.vector.records.loaded",
        path=path,
        count=len(records),
    )
    return records


class BGEM3Embedder:
    """本地 BGE-M3 稠密向量封装。"""

    _model_cache: ClassVar[dict[tuple[str, str], Any]] = {}
    _model_cache_lock: ClassVar[Lock] = Lock()
    _encode_lock: ClassVar[Lock] = Lock()

    def __init__(
        self,
        model_dir: str | Path,
        *,
        max_length: int,
        batch_size: int,
        cpu_threads: int = 2,
        cpu_interop_threads: int = 1,
    ) -> None:
        self.model_dir = _resolve_project_path(model_dir)
        if not self.model_dir.is_dir():
            raise FileNotFoundError(f"BGE-M3 model directory does not exist: {self.model_dir}")
        self.max_length = max_length
        self.batch_size = batch_size
        if cpu_threads < 1:
            raise ValueError("cpu_threads must be at least 1.")
        if cpu_interop_threads < 1:
            raise ValueError("cpu_interop_threads must be at least 1.")
        self.cpu_threads = cpu_threads
        self.cpu_interop_threads = cpu_interop_threads
        self._tokenizer = None
        self._model = None
        self._device = None

    @staticmethod
    def _configure_cpu_environment(thread_count: int) -> None:
        # These variables must be set before importing torch/transformers.
        os.environ["OMP_NUM_THREADS"] = str(thread_count)
        os.environ["MKL_NUM_THREADS"] = str(thread_count)
        os.environ["TOKENIZERS_PARALLELISM"] = "false"

    @staticmethod
    def _configure_cpu_threads(
        torch: Any,
        *,
        requested_threads: int,
        requested_interop_threads: int,
    ) -> tuple[int, int]:
        cpu_count = os.cpu_count() or 1
        effective_threads = max(1, min(requested_threads, cpu_count))
        effective_interop_threads = max(
            1,
            min(requested_interop_threads, cpu_count),
        )
        torch.set_num_threads(effective_threads)
        try:
            torch.set_num_interop_threads(effective_interop_threads)
        except RuntimeError:
            # PyTorch only permits changing inter-op threads before parallel
            # work starts. The intra-op limit above remains effective.
            pass
        return effective_threads, effective_interop_threads

    def _load(self) -> None:
        if self._model is not None:
            return
        self._configure_cpu_environment(self.cpu_threads)
        try:
            import torch
            from FlagEmbedding import BGEM3FlagModel
        except ImportError as exc:
            raise RuntimeError(
                "FlagEmbedding and Torch are required for BGE-M3 dense "
                "embedding. Install requirements.txt in the cs environment."
            ) from exc

        device = "cuda" if torch.cuda.is_available() else "cpu"
        effective_cpu_threads = None
        effective_cpu_interop_threads = None
        if device == "cpu":
            (
                effective_cpu_threads,
                effective_cpu_interop_threads,
            ) = self._configure_cpu_threads(
                torch,
                requested_threads=self.cpu_threads,
                requested_interop_threads=self.cpu_interop_threads,
            )
        cache_key = (str(self.model_dir), device)
        with self._model_cache_lock:
            model = self._model_cache.get(cache_key)
            if model is None:
                model = BGEM3FlagModel(
                    str(self.model_dir),
                    use_fp16=device == "cuda",
                )
                self._model_cache[cache_key] = model
                loaded = True
            else:
                loaded = False

        self._model = model
        self._device = device
        log_event(
            logger,
            logging.INFO,
            (
                "knowledge_base.embedding.model.loaded"
                if loaded
                else "knowledge_base.embedding.model.reused"
            ),
            model_dir=self.model_dir,
            device=device,
            max_length=self.max_length,
            cpu_threads=effective_cpu_threads,
            cpu_interop_threads=effective_cpu_interop_threads,
        )

    def warm_up(self) -> None:
        """在处理第一个用户查询前加载共享模型。"""
        self._load()

    def embed(
        self,
        texts: list[str],
    ) -> list[list[float]]:
        self._load()
        assert self._model is not None
        # The update job and online retrieval share the cached model. Serializing
        # encode calls prevents concurrent batches from multiplying CPU usage.
        with self._encode_lock:
            output = self._model.encode(
                texts,
                batch_size=self.batch_size,
                max_length=self.max_length,
                return_dense=True,
                return_sparse=False,
                return_colbert_vecs=False,
            )
        dense_vectors = output["dense_vecs"]
        return (
            dense_vectors.tolist()
            if hasattr(dense_vectors, "tolist")
            else [list(vector) for vector in dense_vectors]
        )


def build_chromadb(
    settings: RAGSettings | None = None,
    *,
    chunks_path: str | Path | None = None,
    persist_dir: str | Path | None = None,
    collection_name: str | None = None,
    model_dir: str | Path | None = None,
    embedding_batch_size: int | None = None,
    embedding_max_length: int | None = None,
    upsert_batch_size: int | None = None,
    embedding_cpu_threads: int | None = None,
    embedding_cpu_interop_threads: int | None = None,
    batch_pause_seconds: float | None = None,
    reset_collection: bool = True,
) -> dict[str, Any]:
    runtime_settings = settings or get_rag_settings()
    source_path = _resolve_project_path(
        chunks_path or runtime_settings.knowledge_base_output_path
    )
    target_dir = _resolve_project_path(
        persist_dir or runtime_settings.knowledge_base_vector_db_dir
    )
    target_collection = (
        collection_name or runtime_settings.knowledge_base_vector_collection
    )
    embedding_model_dir = _resolve_project_path(
        model_dir or runtime_settings.knowledge_base_embedding_model_dir
    )
    effective_embedding_batch_size = (
        embedding_batch_size
        if embedding_batch_size is not None
        else runtime_settings.knowledge_base_embedding_batch_size
    )
    effective_embedding_max_length = (
        embedding_max_length
        if embedding_max_length is not None
        else runtime_settings.knowledge_base_embedding_max_length
    )
    effective_upsert_batch_size = (
        upsert_batch_size
        if upsert_batch_size is not None
        else runtime_settings.knowledge_base_chroma_upsert_batch_size
    )
    effective_embedding_cpu_threads = (
        embedding_cpu_threads
        if embedding_cpu_threads is not None
        else runtime_settings.knowledge_base_embedding_cpu_threads
    )
    effective_embedding_cpu_interop_threads = (
        embedding_cpu_interop_threads
        if embedding_cpu_interop_threads is not None
        else runtime_settings.knowledge_base_embedding_cpu_interop_threads
    )
    effective_batch_pause_seconds = (
        batch_pause_seconds
        if batch_pause_seconds is not None
        else runtime_settings.knowledge_base_embedding_batch_pause_seconds
    )

    if effective_embedding_batch_size < 1:
        raise ValueError("embedding_batch_size must be at least 1.")
    if effective_upsert_batch_size < 1:
        raise ValueError("upsert_batch_size must be at least 1.")
    if effective_embedding_cpu_threads < 1:
        raise ValueError("embedding_cpu_threads must be at least 1.")
    if effective_embedding_cpu_interop_threads < 1:
        raise ValueError("embedding_cpu_interop_threads must be at least 1.")
    if effective_batch_pause_seconds < 0:
        raise ValueError("batch_pause_seconds cannot be negative.")

    records = load_chunk_records(source_path)
    chromadb = _load_chromadb()
    from chromadb.config import Settings as ChromaSettings
    from knowledge_base_in.chroma_telemetry import build_chroma_settings

    target_dir.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(
        path=str(target_dir),
        settings=build_chroma_settings(ChromaSettings),
    )
    collection = client.get_or_create_collection(
        name=target_collection,
        metadata={
            "hnsw:space": "cosine",
            "embedding_model": str(embedding_model_dir),
            "retrieval_mode": "dense",
            "source_chunks": str(source_path),
        },
    )
    embedder = BGEM3Embedder(
        embedding_model_dir,
        max_length=effective_embedding_max_length,
        batch_size=effective_embedding_batch_size,
        cpu_threads=effective_embedding_cpu_threads,
        cpu_interop_threads=effective_embedding_cpu_interop_threads,
    )

    upserted_count = 0
    embedding_dimension: int | None = None
    with vector_store_lock:
        if reset_collection:
            cleared_count = _clear_collection(collection)
            if cleared_count:
                log_event(
                    logger,
                    logging.INFO,
                    "knowledge_base.vector.collection.cleared",
                    collection=target_collection,
                    cleared=cleared_count,
                )

        for batch in _batched(records, effective_upsert_batch_size):
            texts = [record.text for record in batch]
            embeddings = embedder.embed(texts)
            if embeddings:
                embedding_dimension = len(embeddings[0])
            collection.upsert(
                ids=[record.chunk_id for record in batch],
                documents=texts,
                embeddings=embeddings,
                metadatas=[
                    _normalise_metadata(record.metadata)
                    for record in batch
                ],
            )
            upserted_count += len(batch)
            log_event(
                logger,
                logging.INFO,
                "knowledge_base.vector.batch.upserted",
                collection=target_collection,
                upserted=upserted_count,
                total=len(records),
            )
            if (
                effective_batch_pause_seconds > 0
                and upserted_count < len(records)
            ):
                time.sleep(effective_batch_pause_seconds)

        manifest = {
            "retrieval_mode": "dense",
            "chunks_path": str(source_path),
            "persist_dir": str(target_dir),
            "collection": target_collection,
            "embedding_model_dir": str(embedding_model_dir),
            "embedding_batch_size": effective_embedding_batch_size,
            "embedding_max_length": effective_embedding_max_length,
            "upsert_batch_size": effective_upsert_batch_size,
            "embedding_cpu_threads": effective_embedding_cpu_threads,
            "embedding_cpu_interop_threads": effective_embedding_cpu_interop_threads,
            "batch_pause_seconds": effective_batch_pause_seconds,
            "reset_collection": reset_collection,
            "record_count": len(records),
            "embedding_dimension": embedding_dimension,
            "collection_count": collection.count(),
            "storage_maintenance": _compact_chroma_storage(
                client,
                collection,
                target_dir,
            ),
        }
    manifest_path = target_dir / f"{target_collection}.manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log_event(
        logger,
        logging.INFO,
        "knowledge_base.vector.completed",
        collection=target_collection,
        record_count=len(records),
        persist_dir=target_dir,
    )
    return manifest


def main() -> None:
    settings = get_rag_settings()
    parser = argparse.ArgumentParser(
        description="Embed knowledge-base chunks with local BGE-M3 and write them to ChromaDB.",
    )
    parser.add_argument(
        "--chunks",
        default=settings.knowledge_base_output_path,
        help="Input chunks JSONL path.",
    )
    parser.add_argument(
        "--persist-dir",
        default=settings.knowledge_base_vector_db_dir,
        help="ChromaDB persistent directory.",
    )
    parser.add_argument(
        "--collection",
        default=settings.knowledge_base_vector_collection,
        help="ChromaDB collection name.",
    )
    parser.add_argument(
        "--model-dir",
        default=settings.knowledge_base_embedding_model_dir,
        help="Local BGE-M3 model directory.",
    )
    parser.add_argument(
        "--embedding-batch-size",
        type=int,
        default=settings.knowledge_base_embedding_batch_size,
        help="Number of texts encoded by BGE-M3 per forward pass.",
    )
    parser.add_argument(
        "--embedding-max-length",
        type=int,
        default=settings.knowledge_base_embedding_max_length,
        help="Maximum BGE-M3 tokenizer length.",
    )
    parser.add_argument(
        "--upsert-batch-size",
        type=int,
        default=settings.knowledge_base_chroma_upsert_batch_size,
        help="Number of records sent to ChromaDB per upsert.",
    )
    parser.add_argument(
        "--embedding-cpu-threads",
        type=int,
        default=settings.knowledge_base_embedding_cpu_threads,
        help="Maximum PyTorch intra-op CPU threads for embedding.",
    )
    parser.add_argument(
        "--embedding-cpu-interop-threads",
        type=int,
        default=settings.knowledge_base_embedding_cpu_interop_threads,
        help="Maximum PyTorch inter-op CPU threads for embedding.",
    )
    parser.add_argument(
        "--batch-pause-seconds",
        type=float,
        default=settings.knowledge_base_embedding_batch_pause_seconds,
        help="Pause between ChromaDB batches to yield CPU time.",
    )
    parser.add_argument(
        "--no-reset",
        action="store_true",
        help="Upsert into the existing collection instead of rebuilding it.",
    )
    args = parser.parse_args()

    configure_logging()
    manifest = build_chromadb(
        settings=settings,
        chunks_path=args.chunks,
        persist_dir=args.persist_dir,
        collection_name=args.collection,
        model_dir=args.model_dir,
        embedding_batch_size=args.embedding_batch_size,
        embedding_max_length=args.embedding_max_length,
        upsert_batch_size=args.upsert_batch_size,
        embedding_cpu_threads=args.embedding_cpu_threads,
        embedding_cpu_interop_threads=args.embedding_cpu_interop_threads,
        batch_pause_seconds=args.batch_pause_seconds,
        reset_collection=not args.no_reset,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
