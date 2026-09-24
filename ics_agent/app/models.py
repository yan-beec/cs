# 模块职责：定义 Chatwoot Webhook 事件的数据模型。

from typing import Any

from pydantic import BaseModel, Field


class ChatwootWebhookEvent(BaseModel):
    event: str = ""
    id: str | int | None = None
    thread_id: str | None = None
    account: dict[str, Any] | None = None
    conversation: dict[str, Any] | None = None
    message_type: str | None = None
    content: str | None = None
    sender: dict[str, Any] | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class RAGQueryRequest(BaseModel):
    """本地 RAG 测试接口的请求体。"""

    query: str = Field(min_length=1, max_length=4096)
    thread_id: str | None = Field(default=None, min_length=1, max_length=256)


class AdminLoginRequest(BaseModel):
    """系统管理员登录请求。"""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class AdminFileDeleteRequest(BaseModel):
    """后台文件删除请求。"""

    relative_path: str = Field(min_length=1, max_length=1024)
