"""验证旧资源兼容、模型路径搬迁，以及各渠道知识库的独立更新。"""

import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ics_agent.channels.bootstrap import ensure_vector_collection
from ics_agent.faq.repositories import _migrate_legacy_xingliao_faq
from ics_agent.rag.config import RAGSettings
from ics_agent.rag.graph.nodes import IntentClassifierModel
from ics_agent.rag.retrieval.dense_search import DenseRetriever
from knowledge_base_in import build_chromadb as builder


def test_faq_legacy_migration_is_once_only_even_if_new_table_is_later_cleared():
    class Cursor:
        marker = False
        populated = False
        copies = 0
        result = None

        def execute(self, sql, params=None):
            if "SELECT 1 FROM ics_channel_migrations" in sql:
                self.result = (1,) if self.marker else None
            elif "SELECT EXISTS" in sql:
                self.result = (self.populated,)
            elif "to_regclass" in sql:
                self.result = ("faq_qna_optimized",)
            elif "INSERT INTO faq_channel_xingliao" in sql:
                self.copies += 1
                self.populated = True
            elif "INSERT INTO ics_channel_migrations" in sql:
                self.marker = True

        def fetchone(self):
            return self.result

    cursor = Cursor()
    _migrate_legacy_xingliao_faq(cursor, "faq_channel_xingliao")
    assert cursor.populated and cursor.marker
    cursor.populated = False
    _migrate_legacy_xingliao_faq(cursor, "faq_channel_xingliao")
    assert cursor.copies == 1
    assert not cursor.populated


def test_relocated_intent_csv_keeps_fingerprint_validation(tmp_path):
    dataset = tmp_path / "moved.csv"
    dataset.write_text("text,label,label_id\n问题,咨询问题,0\n你好,闲聊,1\n", encoding="utf-8")
    labels = {"id2label": {"0": "咨询问题", "1": "闲聊"},
              "dataset": {"path": str(tmp_path / "missing.csv"),
                          "sha256": hashlib.sha256(dataset.read_bytes()).hexdigest()}}
    (tmp_path / "labels.json").write_text(json.dumps(labels), encoding="utf-8")
    model = SimpleNamespace(config=SimpleNamespace(num_labels=2))
    assert IntentClassifierModel._load_label_mapping(tmp_path, model, dataset) == {0: "咨询问题", 1: "闲聊"}
    dataset.write_text(dataset.read_text(encoding="utf-8") + "再问,咨询问题,0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="differs"):
        IntentClassifierModel._load_label_mapping(tmp_path, model, dataset)


def test_existing_vectors_are_preserved_under_new_logical_name(tmp_path):
    from chromadb.config import Settings as ChromaSettings
    from knowledge_base_in.chroma_telemetry import build_chroma_settings

    chromadb = builder._load_chromadb()
    client = chromadb.PersistentClient(path=str(tmp_path), settings=build_chroma_settings(ChromaSettings))
    original = client.create_collection("knowledge_base")
    original.add(ids=["old-1"], embeddings=[[1.0, 0.0]], documents=["原有兴聊文档"])
    settings = RAGSettings(_env_file=None, knowledge_base_vector_db_dir=str(tmp_path))
    for _ in range(2):
        result = ensure_vector_collection(settings)
        assert result == {"collection": "kb_channel_xingliao", "active_collection": "knowledge_base", "count": 1}
    assert original.get()["documents"] == ["原有兴聊文档"]


def test_empty_channel_does_not_load_embedding_model(tmp_path, monkeypatch):
    settings = RAGSettings(_env_file=None, knowledge_base_vector_db_dir=str(tmp_path),
                           knowledge_base_vector_collection="kb_channel_wechat")
    ensure_vector_collection(settings)
    retriever = DenseRetriever(settings=settings)
    embed = Mock(side_effect=AssertionError("empty channel must not embed"))
    monkeypatch.setattr(retriever.embedder, "embed", embed)
    assert asyncio.run(retriever.search("没有资料的问题")) == []
    assert retriever._search_sync("没有资料的问题") == []
    embed.assert_not_called()


def test_rebuilding_one_collection_does_not_replace_another_channels_documents(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "BGEM3Embedder", lambda *args, **kwargs: SimpleNamespace(
        embed=lambda texts: [[1.0, 0.0] for _ in texts]))
    monkeypatch.setattr(builder, "_compact_chroma_storage", lambda *args: {})
    configs = {}
    retrievers = {}
    for key in ("yanliao", "wechat"):
        chunks = tmp_path / f"{key}.jsonl"
        chunks.write_text(json.dumps({"id": "same-id", "text": key, "metadata": {"source": key}}), encoding="utf-8")
        settings = RAGSettings(_env_file=None, knowledge_base_vector_db_dir=str(tmp_path / "chroma"),
                               knowledge_base_vector_collection=f"kb_channel_{key}",
                               knowledge_base_output_path=str(chunks))
        configs[key] = settings
        builder.build_chromadb(settings=settings)
        retrievers[key] = DenseRetriever(settings=settings)
    chunks = tmp_path / "wechat.jsonl"
    chunks.write_text(json.dumps({"id": "same-id", "text": "wechat-v2", "metadata": {"source": "wechat"}}), encoding="utf-8")
    builder.build_chromadb(settings=configs["wechat"])
    assert retrievers["yanliao"]._search_sync("问题", query_embedding=[1.0, 0.0])[0].text == "yanliao"
    assert retrievers["wechat"]._search_sync("问题", query_embedding=[1.0, 0.0])[0].text == "wechat-v2"
