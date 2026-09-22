# 模块职责：导出 FAQ 模块对外使用的稳定 API。

from .config import FAQSettings, get_faq_settings
from .index import BM25Matcher, JiebaTokenizer
from .models import FAQDocument, FAQMatch, FAQResult, FAQSnapshot
from .repositories import (
    FAQAnswerCache,
    FAQRepository,
    PostgreSQLConnectionPool,
    RedisFAQSnapshotRepository,
    ensure_faq_table,
)
from .service import (
    FAQRuntime,
    FAQRuntimeResult,
    FAQService,
    FAQSnapshotSynchronizer,
    FAQUpdateInProgress,
    FAQUpdateStatus,
    execute_faq_query,
    get_faq_service,
    get_runtime,
    initialize_faq_runtime,
    submit_faq_excel_refresh,
)
from .sync import (
    FAQExcelReader,
    FAQQnaSynchronizer,
    FAQSyncResult,
    refresh_faq_runtime_from_excel,
    sync_qna_from_excel,
)

__all__ = [
    "BM25Matcher",
    "FAQDocument",
    "FAQAnswerCache",
    "FAQExcelReader",
    "FAQMatch",
    "FAQRepository",
    "FAQResult",
    "FAQRuntime",
    "FAQRuntimeResult",
    "FAQService",
    "FAQSnapshot",
    "FAQSnapshotSynchronizer",
    "FAQSettings",
    "FAQSyncResult",
    "FAQUpdateInProgress",
    "FAQUpdateStatus",
    "FAQQnaSynchronizer",
    "JiebaTokenizer",
    "PostgreSQLConnectionPool",
    "RedisFAQSnapshotRepository",
    "ensure_faq_table",
    "execute_faq_query",
    "get_faq_settings",
    "get_faq_service",
    "get_runtime",
    "initialize_faq_runtime",
    "refresh_faq_runtime_from_excel",
    "submit_faq_excel_refresh",
    "sync_qna_from_excel",
]
