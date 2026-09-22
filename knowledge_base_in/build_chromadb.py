# 模块职责：读取知识库分块 JSONL，使用本地 BGE-M3 生成向量并写入 ChromaDB。

from __future__ import annotations

import argparse
import json
import logging
import sys
from threading import Lock
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Iterable

if __package__ in {None, ""}:
    _project_root = Path(__file__).resolve().parents[1]
    if str(_project_root) not in sys.path:
        sys.path.insert(0, str(_project_root))

from ics_agent.app.config import PROJECT_ROOT
from ics_agent.app.logging_config import configure_logging, log_event
from ics_agent.rag.config import RAGSettings, get_rag_settings


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VectorRecord:
    """One parsed chunk ready for vector storage."""

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
    """Local BGE-M3 dense embedding wrapper."""

    _model_cache: ClassVar[dict[tuple[str, str], Any]] = {}
    _model_cache_lock: ClassVar[Lock] = Lock()

    def __init__(
        self,
        model_dir: str | Path,
        *,
        max_length: int,
        batch_size: int,
    ) -> None:
        self.model_dir = _resolve_project_path(model_dir)
        if not self.model_dir.is_dir():
            raise FileNotFoundError(f"BGE-M3 model directory does not exist: {self.model_dir}")
        self.max_length = max_length
        self.batch_size = batch_size
        self._tokenizer = None
        self._model = None
        self._device = None

    def _load(self) -> None:
        if self._model is not None:
            return
        try:
            import torch
            from FlagEmbedding import BGEM3FlagModel
        except ImportError as exc:
            raise RuntimeError(
                "FlagEmbedding and Torch are required for BGE-M3 dense "
                "embedding. Install requirements.txt in the cs environment."
            ) from exc

        device = "cuda" if torch.cuda.is_available() else "cpu"
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
        )

    def warm_up(self) -> None:
        """Load the shared model before serving the first user query."""
        self._load()

    def embed(
        self,
        texts: list[str],
    ) -> list[list[float]]:
        self._load()
        assert self._model is not None
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

    if effective_embedding_batch_size < 1:
        raise ValueError("embedding_batch_size must be at least 1.")
    if effective_upsert_batch_size < 1:
        raise ValueError("upsert_batch_size must be at least 1.")

    records = load_chunk_records(source_path)
    chromadb = _load_chromadb()
    target_dir.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(target_dir))
    if reset_collection:
        try:
            client.delete_collection(target_collection)
            log_event(
                logger,
                logging.INFO,
                "knowledge_base.vector.collection.deleted",
                collection=target_collection,
            )
        except ValueError:
            pass

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
    )

    upserted_count = 0
    embedding_dimension: int | None = None
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

    manifest = {
        "retrieval_mode": "dense",
        "chunks_path": str(source_path),
        "persist_dir": str(target_dir),
        "collection": target_collection,
        "embedding_model_dir": str(embedding_model_dir),
        "embedding_batch_size": effective_embedding_batch_size,
        "embedding_max_length": effective_embedding_max_length,
        "upsert_batch_size": effective_upsert_batch_size,
        "reset_collection": reset_collection,
        "record_count": len(records),
        "embedding_dimension": embedding_dimension,
        "collection_count": collection.count(),
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
        reset_collection=not args.no_reset,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
