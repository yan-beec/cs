# 模块职责：加载意图分类 CSV 数据，校验标签并生成 Trainer 兼容的数据集。

from __future__ import annotations

import csv
import hashlib
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ics_agent.app.logging_config import log_event


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IntentClassificationExample:
    """意图分类 CSV 数据集中的一个文本分类样本。"""

    text: str
    label: str
    label_id: int

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("Intent classification text cannot be empty.")
        if not self.label.strip():
            raise ValueError("Intent classification label cannot be empty.")
        if self.label_id < 0:
            raise ValueError("Intent classification label_id cannot be negative.")


def calculate_intent_dataset_sha256(path: str | Path) -> str:
    """计算训练 CSV 的文件指纹，用于绑定模型与其训练数据。"""
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"Intent classification file does not exist: {source}")

    digest = hashlib.sha256()
    with source.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_examples(
    examples: Iterable[IntentClassificationExample],
) -> list[IntentClassificationExample]:
    example_list = list(examples)
    if not example_list:
        raise ValueError("Intent classification dataset contains no examples.")

    label_to_id: dict[str, int] = {}
    id_to_label: dict[int, str] = {}
    for example in example_list:
        previous_id = label_to_id.setdefault(example.label, example.label_id)
        if previous_id != example.label_id:
            raise ValueError(
                f"Label {example.label!r} maps to both {previous_id} "
                f"and {example.label_id}."
            )

        previous_label = id_to_label.setdefault(example.label_id, example.label)
        if previous_label != example.label:
            raise ValueError(
                f"label_id {example.label_id} maps to both "
                f"{previous_label!r} and {example.label!r}."
            )

    expected_ids = list(range(len(id_to_label)))
    actual_ids = sorted(id_to_label)
    if actual_ids != expected_ids:
        raise ValueError(
            "Intent label_id values must be continuous and start at zero; "
            f"received {actual_ids}."
        )
    return example_list


def load_intent_csv(path: str | Path) -> list[IntentClassificationExample]:
    """加载项目的 ``id,text,label,label_id`` CSV 数据集。"""
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"Intent classification file does not exist: {source}")

    examples: list[IntentClassificationExample] = []
    with source.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        fieldnames = set(reader.fieldnames or [])
        required_fields = {"text", "label", "label_id"}
        missing_fields = required_fields - fieldnames
        if missing_fields:
            raise ValueError(
                f"Intent classification CSV is missing columns: "
                f"{sorted(missing_fields)}."
            )

        for row_number, row in enumerate(reader, start=2):
            text = (row.get("text") or "").strip()
            label = (row.get("label") or "").strip()
            raw_label_id = (row.get("label_id") or "").strip()
            if not text or not label or not raw_label_id:
                raise ValueError(
                    f"Intent classification CSV row {row_number} contains an empty "
                    "text, label, or label_id."
                )
            try:
                label_id = int(raw_label_id)
            except ValueError as exc:
                raise ValueError(
                    f"Intent classification CSV row {row_number} has invalid "
                    f"label_id: {raw_label_id!r}."
                ) from exc
            examples.append(
                IntentClassificationExample(
                    text=text,
                    label=label,
                    label_id=label_id,
                )
            )

    validated_examples = _validate_examples(examples)
    label_count = len({example.label_id for example in validated_examples})
    log_event(
        logger,
        logging.INFO,
        "rag.intent_classifier.dataset.loaded",
        path=source.name,
        sample_count=len(validated_examples),
        label_count=label_count,
    )
    return validated_examples


def build_intent_label_mapping(
    examples: Iterable[IntentClassificationExample],
) -> tuple[dict[str, int], dict[int, str]]:
    """根据 CSV 的 ``label_id`` 列构建标签映射。"""
    example_list = _validate_examples(examples)
    id_to_label = {
        example.label_id: example.label
        for example in example_list
    }
    label2id = {
        label: label_id
        for label_id, label in sorted(id_to_label.items())
    }
    return label2id, {
        label_id: label
        for label_id, label in sorted(id_to_label.items())
    }


def split_intent_examples(
    examples: Iterable[IntentClassificationExample],
    eval_ratio: float = 0.2,
    seed: int = 42,
) -> tuple[list[IntentClassificationExample], list[IntentClassificationExample]]:
    """创建确定性的分层训练集/评估集拆分。"""
    if not 0 < eval_ratio < 1:
        raise ValueError("eval_ratio must be greater than 0 and less than 1.")

    example_list = _validate_examples(examples)
    grouped: dict[int, list[IntentClassificationExample]] = {}
    for example in example_list:
        grouped.setdefault(example.label_id, []).append(example)

    generator = random.Random(seed)
    train_examples: list[IntentClassificationExample] = []
    eval_examples: list[IntentClassificationExample] = []
    for label_id in sorted(grouped):
        group = list(grouped[label_id])
        if len(group) < 2:
            raise ValueError(
                f"Label {label_id} needs at least two examples for a train/eval split."
            )
        generator.shuffle(group)
        eval_count = max(1, round(len(group) * eval_ratio))
        eval_count = min(len(group) - 1, eval_count)
        eval_examples.extend(group[:eval_count])
        train_examples.extend(group[eval_count:])

    log_event(
        logger,
        logging.INFO,
        "rag.intent_classifier.dataset.split",
        train_count=len(train_examples),
        eval_count=len(eval_examples),
        eval_ratio=eval_ratio,
        seed=seed,
    )
    return train_examples, eval_examples


class IntentClassificationDataset:
    """可供 ``transformers.Trainer`` 使用的轻量级分词数据集。"""

    def __init__(
        self,
        examples: Iterable[IntentClassificationExample],
        tokenizer: Any,
        max_length: int,
    ) -> None:
        self._examples = _validate_examples(examples)
        self._encodings = tokenizer(
            [example.text for example in self._examples],
            truncation=True,
            max_length=max_length,
            padding=False,
        )

    def __len__(self) -> int:
        return len(self._examples)

    def __getitem__(self, index: int) -> dict[str, Any]:
        item = {
            key: values[index]
            for key, values in self._encodings.items()
        }
        item["labels"] = self._examples[index].label_id
        return item
