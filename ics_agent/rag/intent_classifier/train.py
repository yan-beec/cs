# 模块职责：提供基于可配置 CSV 数据集的意图分类微调命令行入口。

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

if __package__ in {None, ""}:
    # 直接执行文件时，将项目根目录加入导入路径，再使用绝对导入。
    _project_root = Path(__file__).resolve().parents[3]
    if str(_project_root) not in sys.path:
        sys.path.insert(0, str(_project_root))
    from ics_agent.rag.intent_classifier.data import (
        load_intent_csv,
        split_intent_examples,
    )
    from ics_agent.rag.intent_classifier.trainer import IntentClassifierTrainer
else:
    from .data import load_intent_csv, split_intent_examples
    from .trainer import IntentClassifierTrainer

from ics_agent.app.core.config import PROJECT_ROOT
from ics_agent.app.core.logging_config import configure_logging, log_event
from ics_agent.rag.config import get_rag_settings


logger = logging.getLogger(__name__)


def main() -> None:
    settings = get_rag_settings()
    parser = argparse.ArgumentParser(
        description="Fine-tune the local BERT model for intent classification.",
    )
    parser.add_argument(
        "--dataset",
        default=settings.rag_intent_dataset_path,
        help="CSV file containing text, label and label_id columns.",
    )
    parser.add_argument(
        "--eval-ratio",
        type=float,
        default=settings.rag_eval_ratio,
        help="Evaluation ratio for deterministic stratified splitting.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=settings.rag_seed,
        help="Random seed used for the dataset split.",
    )
    args = parser.parse_args()

    configure_logging()
    dataset_path = Path(args.dataset)
    if not dataset_path.is_absolute():
        dataset_path = PROJECT_ROOT / dataset_path
    examples = load_intent_csv(dataset_path)
    train_examples, eval_examples = split_intent_examples(
        examples,
        eval_ratio=args.eval_ratio,
        seed=args.seed,
    )
    log_event(
        logger,
        logging.INFO,
        "rag.intent_classifier.cli.start",
        dataset=Path(args.dataset).name,
        train_count=len(train_examples),
        eval_count=len(eval_examples),
    )
    result = IntentClassifierTrainer(settings=settings).train(
        train_examples=train_examples,
        eval_examples=eval_examples,
        dataset_path=dataset_path,
    )
    print(
        json.dumps(
            {
                "output_dir": result.output_dir,
                "dataset_path": result.dataset_path,
                "dataset_sha256": result.dataset_sha256,
                "label2id": result.label2id,
                "id2label": result.id2label,
                "metrics": result.metrics,
            },
            ensure_ascii=False,
            indent=2,
            default=float,
        )
    )


if __name__ == "__main__":
    main()
