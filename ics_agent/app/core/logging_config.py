# 模块职责：配置全局日志格式，并提供统一的事件日志辅助函数。

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any


DEFAULT_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_EVENT_SEGMENT_TRANSLATIONS = {
    "admin": "管理后台",
    "admin_auth": "管理员认证",
    "admin_login": "管理员登录",
    "app": "应用",
    "answer": "答案",
    "answer_cache": "答案缓存",
    "acquire": "获取",
    "acquired": "已获取",
    "admin_user": "管理员用户",
    "answers_incomplete": "答案不完整",
    "answers_load_failed": "答案加载失败",
    "auth": "认证",
    "background": "后台",
    "batch": "批次",
    "bm25": "BM25",
    "call": "调用",
    "cache": "缓存",
    "circuit": "熔断器",
    "clear": "清理",
    "cli": "命令行",
    "collection": "集合",
    "completed": "已完成",
    "concurrency": "并发",
    "config_username_differs": "配置用户名不一致",
    "create": "创建",
    "created": "已创建",
    "dataset": "数据集",
    "deleted": "已删除",
    "dense": "稠密检索",
    "dense_retriever": "稠密检索器",
    "device": "设备",
    "embedding": "向量",
    "evaluation": "评估",
    "excel": "Excel",
    "failed": "失败",
    "faq": "FAQ",
    "faq_table": "FAQ 数据表",
    "faq_update": "FAQ 更新",
    "file": "文件",
    "files": "文件",
    "graph": "图",
    "health": "健康检查",
    "image": "图片",
    "intent": "意图",
    "intent_classifier": "意图分类",
    "intent_model": "意图模型",
    "initialized": "已初始化",
    "init_failed": "初始化失败",
    "initialize": "初始化",
    "invalid": "无效",
    "lifecycle": "生命周期",
    "llm_factory": "LLM 工厂",
    "llm_reply": "LLM 回复",
    "loaded": "已加载",
    "load": "加载",
    "model": "模型",
    "node": "节点",
    "open": "打开",
    "parse": "解析",
    "parsed": "已解析",
    "payload": "载荷",
    "postgres": "PostgreSQL",
    "pool": "连接池",
    "query": "查询",
    "rag": "RAG",
    "rag_warmup": "RAG 预热",
    "ready": "就绪",
    "read": "读取",
    "received": "已接收",
    "records": "记录",
    "rebuild": "重建",
    "rejected": "已拒绝",
    "refresh": "刷新",
    "released": "已释放",
    "replaced": "已替换",
    "reranker": "重排器",
    "retrieval": "检索",
    "runtime": "运行时",
    "route": "路由",
    "save": "保存",
    "search": "搜索",
    "service": "服务",
    "signature": "签名",
    "skipped": "已跳过",
    "snapshot": "快照",
    "split": "拆分",
    "start": "开始",
    "startup": "启动",
    "started": "已开始",
    "status_missing": "状态缺失",
    "stopped": "已停止",
    "submitted": "已提交",
    "success": "成功",
    "sync": "同步",
    "table": "数据表",
    "tokenizer": "分词器",
    "trainer": "训练器",
    "training": "训练",
    "updated": "已更新",
    "update": "更新",
    "upserted": "已写入",
    "uploaded": "已上传",
    "vector": "向量",
    "webhook": "Webhook",
    "warmup": "预热",
    "write": "写入",
    "reused": "已复用",
    "exists": "已存在",
    "exhausted": "已耗尽",
    "compiled": "已编译",
    "selected": "已选择",
    "clear_failed": "清理失败",
    "reload": "重载",
    "postgres_sync_failed": "PostgreSQL 同步失败",
    "submit_failed": "提交失败",
    "preview_failed": "预览失败",
    "upload_failed": "上传失败",
    "list_failed": "列表读取失败",
    "tempfile_cleanup_failed": "临时文件清理失败",
    "knowledge_base": "知识库",
    "build_index": "构建索引",
    "answers": "答案",
    "questions": "问题",
}


def _translate_event_name(event_name: str) -> str:
    """将内部事件标识转换为日志中显示的中文名称。"""
    return ".".join(
        _EVENT_SEGMENT_TRANSLATIONS.get(segment, segment)
        for segment in event_name.split(".")
    )


class LogBufferHandler(logging.Handler):
    """为管理后台保留有数量上限的进程内日志流。"""

    def __init__(self, max_records: int) -> None:
        super().__init__()
        self._records: deque[dict[str, Any]] = deque(maxlen=max_records)
        self._next_id = 1
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload = {
                "id": 0,
                "timestamp": time.strftime(
                    DEFAULT_DATE_FORMAT,
                    time.localtime(record.created),
                ),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            with self._lock:
                payload["id"] = self._next_id
                self._next_id += 1
                self._records.append(payload)
        except Exception:
            self.handleError(record)

    def snapshot(
        self,
        *,
        after_id: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        with self._lock:
            records = [
                record
                for record in self._records
                if int(record["id"]) > after_id
            ]
            records = records[-max(1, min(limit, 1000)) :]
            next_id = self._next_id - 1
        return {"items": records, "next_id": next_id}


_log_buffer_handler: LogBufferHandler | None = None


def get_log_snapshot(
    *,
    after_id: int = 0,
    limit: int = 200,
) -> dict[str, Any]:
    """返回管理后台使用的应用缓冲日志。"""
    if _log_buffer_handler is None:
        return {"items": [], "next_id": after_id}
    return _log_buffer_handler.snapshot(after_id=after_id, limit=limit)


def configure_logging(level: str | int | None = None) -> None:
    """配置进程级日志记录器，并更新根日志级别。"""
    global _log_buffer_handler
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                DEFAULT_LOG_FORMAT,
                datefmt=DEFAULT_DATE_FORMAT,
            )
        )
        root_logger.addHandler(handler)

    if _log_buffer_handler is None:
        from .config import get_settings

        _log_buffer_handler = LogBufferHandler(
            get_settings().admin_log_buffer_size
        )
        _log_buffer_handler.setLevel(logging.DEBUG)
        root_logger.addHandler(_log_buffer_handler)

    if level is None:
        from .config import get_settings

        level = get_settings().log_level

    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    root_logger.setLevel(level)
    logging.getLogger("jieba").setLevel(logging.WARNING)


def log_event(
    logger: logging.Logger,
    level: int,
    event_name: str,
    **fields: Any,
) -> None:
    """写入带紧凑键值字段、便于检索的事件标记。"""
    parts = [f"event={_translate_event_name(event_name)}"]
    for key, value in fields.items():
        if value is None:
            continue
        normalized = str(value).replace("\r", " ").replace("\n", " ")
        parts.append(f"{key}={normalized.replace(' ', '_')}")
    logger.log(level, " ".join(parts))
