"""创建渠道资源，并保留兴聊升级前的 FAQ 和向量数据。"""

from __future__ import annotations

from typing import Any

from ics_agent.channels import get_channel, list_channels
from ics_agent.faq.config import get_faq_settings
from ics_agent.faq.repositories import ensure_faq_table
from ics_agent.rag.config import RAGSettings, get_rag_settings
from knowledge_base_in.vector_store_lock import vector_store_lock


def ensure_vector_collection(settings: RAGSettings) -> dict[str, Any]:
    """初始化空集合；兴聊用逻辑名称指向原集合，保留已有向量和在线检索。"""
    from knowledge_base_in.build_chromadb import (
        _active_collection_pointer_path, _load_chromadb,
        _read_active_collection_name, _resolve_project_path,
        _write_active_collection_name,
    )
    from knowledge_base_in.chroma_telemetry import build_chroma_settings

    chromadb = _load_chromadb()
    from chromadb.config import Settings as ChromaSettings
    target_dir = _resolve_project_path(settings.knowledge_base_vector_db_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    logical_name = settings.knowledge_base_vector_collection
    client = chromadb.PersistentClient(
        path=str(target_dir), settings=build_chroma_settings(ChromaSettings),
    )
    with vector_store_lock:
        active_name = _read_active_collection_name(target_dir, logical_name)
        names = {str(item) if isinstance(item, str) else item.name
                 for item in client.list_collections()}
        legacy_name = _read_active_collection_name(target_dir, "knowledge_base")
        pointer = _active_collection_pointer_path(target_dir, logical_name)
        if (logical_name == "kb_channel_xingliao" and not pointer.exists()
                and active_name not in names and legacy_name in names):
            # 延续原有 staging/active 指针机制。改名不读取或改写已有向量；
            # 下一次正常知识库重建将发布 kb_channel_xingliao 的新版本集合。
            _write_active_collection_name(target_dir, logical_name, legacy_name)
            active_name = legacy_name
        collection = (client.get_collection(name=active_name) if active_name in names
                      else client.get_or_create_collection(
                          name=active_name, metadata={"hnsw:space": "cosine"},
                      ))
        return {"collection": logical_name, "active_collection": active_name,
                "count": collection.count()}


def initialize_channel(channel_key: str) -> dict[str, Any]:
    channel = get_channel(channel_key)
    faq_settings = get_faq_settings(channel.key)
    created = ensure_faq_table(settings=faq_settings)
    return {"channel_key": channel.key, "faq_table": faq_settings.faq_table_name,
            "faq_table_created": created,
            "knowledge_base": ensure_vector_collection(get_rag_settings(channel.key))}


def initialize_channels() -> list[dict[str, Any]]:
    return [initialize_channel(channel.key) for channel in list_channels()]
