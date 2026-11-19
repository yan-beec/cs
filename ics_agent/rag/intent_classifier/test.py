# 模块职责：提供独立标注测试集的全量模型效果测试命令行入口。

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    _project_root = Path(__file__).resolve().parents[3]
    if str(_project_root) not in sys.path:
        sys.path.insert(0, str(_project_root))
    from ics_agent.rag.intent_classifier.evaluate import main
else:
    from .evaluate import main


if __name__ == "__main__":
    main()
