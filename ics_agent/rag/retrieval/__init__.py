# 模块职责：导出 RAG 知识库 dense 检索服务。

from .retriever import (
    RetrievedDocument,
    RetrievalService,
    aretrieve_documents,
    get_retrieval_service,
)

__all__ = [
    "RetrievedDocument",
    "RetrievalService",
    "aretrieve_documents",
    "get_retrieval_service",
]
