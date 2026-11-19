# 模块职责：导出 RAG 回复链路使用的系统提示词、动态提示词和 few-shot 示例。

from .few_shot import FEW_SHOT_EXAMPLES, render_few_shot_examples
from .templates import (
    ANSWER_BOUNDARIES,
    SYSTEM_PROMPT,
    build_dynamic_prompt,
)

__all__ = [
    "ANSWER_BOUNDARIES",
    "FEW_SHOT_EXAMPLES",
    "SYSTEM_PROMPT",
    "build_dynamic_prompt",
    "render_few_shot_examples",
]
