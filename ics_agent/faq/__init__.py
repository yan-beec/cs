# 模块职责：导出 FAQ 模块对外使用的核心服务和数据模型。

from .models import FAQDocument, FAQMatch, FAQResult, FAQSnapshot
from .service import FAQService, get_faq_service

__all__ = [
    "FAQDocument",
    "FAQMatch",
    "FAQResult",
    "FAQService",
    "FAQSnapshot",
    "get_faq_service",
]
