import importlib
import shutil
import uuid
from contextlib import contextmanager
import unittest
from pathlib import Path
from unittest.mock import patch

from ics_agent.rag.retrieval.dense_search import DenseRetriever
from ics_agent.rag.config import RAGSettings
from knowledge_base_in.build_chromadb import (
    VectorRecord,
    _clear_collection,
    _remove_orphaned_segment_dirs,
)


build_module = importlib.import_module("knowledge_base_in.build_chromadb")


class _FakeCollection:
    def __init__(self, records: dict[str, str] | None = None) -> None:
        self.records = dict(records or {})
        self.deleted_batches: list[list[str]] = []
        self.upserted_batches: list[list[str]] = []

    def get(self, *, include: list[str]) -> dict[str, list[str]]:
        self.include = include
        return {"ids": list(self.records)}

    def delete(self, *, ids: list[str]) -> None:
        self.deleted_batches.append(list(ids))
        for record_id in ids:
            self.records.pop(record_id, None)

    def upsert(
        self,
        *,
        ids: list[str],
        documents: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, object]],
    ) -> None:
        del embeddings, metadatas
        self.upserted_batches.append(list(ids))
        self.records.update(dict(zip(ids, documents)))

    def count(self) -> int:
        return len(self.records)

    def query(
        self,
        *,
        query_embeddings: list[list[float]],
        n_results: int,
        include: list[str],
    ) -> dict[str, list[list[object]]]:
        del query_embeddings, include
        ids = list(self.records)[:n_results]
        return {
            "ids": [ids],
            "documents": [[self.records[record_id] for record_id in ids]],
            "metadatas": [[{} for _ in ids]],
            "distances": [[0.0 for _ in ids]],
        }


class _FakeClient:
    def __init__(self, collection: _FakeCollection) -> None:
        self.collection = collection
        self.delete_collection_called = False

    def get_or_create_collection(self, **kwargs: object) -> _FakeCollection:
        del kwargs
        return self.collection

    def delete_collection(self, name: str) -> None:
        del name
        self.delete_collection_called = True
        raise AssertionError("full update must preserve the collection object")


class _FakeChroma:
    def __init__(self, client: _FakeClient) -> None:
        self.client = client

    def PersistentClient(self, **kwargs: object) -> _FakeClient:
        del kwargs
        return self.client


class _FakeEmbedder:
    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(text))] for text in texts]


class _FakeQueryEmbedder:
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(text))] for text in texts]


@contextmanager
def _workspace_temp_dir():
    path = Path.cwd() / f".test_vector_store_refresh_{uuid.uuid4().hex}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class VectorStoreRefreshTests(unittest.TestCase):
    def test_clear_preserves_collection_and_removes_all_ids(self) -> None:
        collection = _FakeCollection({"old-1": "old", "old-2": "older"})

        cleared = _clear_collection(collection, batch_size=1)

        self.assertEqual(2, cleared)
        self.assertEqual({}, collection.records)
        self.assertEqual([["old-1"], ["old-2"]], collection.deleted_batches)
        self.assertEqual([], collection.include)

    def test_orphaned_segment_directories_are_removed(self) -> None:
        with _workspace_temp_dir() as target_dir:
            active_id = "11111111-1111-1111-1111-111111111111"
            orphan_id = "22222222-2222-2222-2222-222222222222"
            (target_dir / active_id).mkdir()
            (target_dir / orphan_id).mkdir()
            (target_dir / "not-a-segment").mkdir()

            removed = _remove_orphaned_segment_dirs(
                target_dir,
                {active_id},
            )

            self.assertEqual(1, removed)
            self.assertTrue((target_dir / active_id).is_dir())
            self.assertFalse((target_dir / orphan_id).exists())
            self.assertTrue((target_dir / "not-a-segment").is_dir())

    def test_full_update_does_not_delete_collection(self) -> None:
        collection = _FakeCollection({"old-1": "old"})
        client = _FakeClient(collection)
        fake_chroma = _FakeChroma(client)
        records = [
            VectorRecord("new-1", "new text", {"source": "test"}),
            VectorRecord("new-2", "newer text", {"source": "test"}),
        ]

        with _workspace_temp_dir() as temp_path:
            settings = RAGSettings(
                knowledge_base_output_path=str(temp_path / "chunks.jsonl"),
                knowledge_base_vector_db_dir=str(temp_path / "vector_store"),
                knowledge_base_embedding_model_dir=str(temp_path / "model"),
                knowledge_base_chroma_upsert_batch_size=1,
                knowledge_base_embedding_batch_pause_seconds=0,
            )
            with (
                patch.object(build_module, "_load_chromadb", return_value=fake_chroma),
                patch.object(build_module, "BGEM3Embedder", _FakeEmbedder),
                patch.object(build_module, "load_chunk_records", return_value=records),
            ):
                manifest = build_module.build_chromadb(settings=settings)

        self.assertFalse(client.delete_collection_called)
        self.assertEqual({"new-1": "new text", "new-2": "newer text"}, collection.records)
        self.assertEqual(2, manifest["collection_count"])
        self.assertIn("storage_maintenance", manifest)

    def test_existing_retriever_can_search_after_refresh(self) -> None:
        collection = _FakeCollection({"old-1": "old"})
        retriever = DenseRetriever.__new__(DenseRetriever)
        retriever.collection = collection
        retriever.collection_name = "knowledge_base"
        retriever.top_k = 2
        retriever.embedder = _FakeQueryEmbedder()

        _clear_collection(collection)
        collection.upsert(
            ids=["new-1"],
            documents=["new text"],
            embeddings=[[1.0]],
            metadatas=[{}],
        )

        results = retriever._search_sync("new query")

        self.assertEqual(["new-1"], [result.chunk_id for result in results])
        self.assertEqual(["new text"], [result.text for result in results])


if __name__ == "__main__":
    unittest.main()
