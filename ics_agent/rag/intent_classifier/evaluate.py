# 模块职责：加载微调后的意图分类模型，并全量评估独立标注测试集。

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Iterable

if __package__ in {None, ""}:
    # 直接执行文件时，将项目根目录加入导入路径。
    _project_root = Path(__file__).resolve().parents[3]
    if str(_project_root) not in sys.path:
        sys.path.insert(0, str(_project_root))
    from ics_agent.rag.intent_classifier.data import (
        IntentClassificationExample,
        build_intent_label_mapping,
        load_intent_csv,
    )
else:
    from .data import (
        IntentClassificationExample,
        build_intent_label_mapping,
        load_intent_csv,
    )

from ics_agent.app.config import PROJECT_ROOT
from ics_agent.app.logging_config import configure_logging, log_event
from ics_agent.rag.config import RAGSettings, get_rag_settings


logger = logging.getLogger(__name__)


def _resolve_project_path(path_value: str) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _load_runtime_dependencies() -> tuple[Any, Any]:
    try:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
    except ImportError as exc:
        raise RuntimeError(
            "Model evaluation dependencies are missing. "
            "Install requirements.txt in the cs environment first."
        ) from exc
    return torch, (AutoModelForSequenceClassification, AutoTokenizer)


def _select_device(torch: Any) -> tuple[Any, str, str | None]:
    if torch.cuda.is_available():
        device = torch.device("cuda:0")
        gpu_name = torch.cuda.get_device_name(0)
        log_event(
            logger,
            logging.INFO,
            "rag.intent_classifier.evaluation.device",
            device=str(device),
            gpu_name=gpu_name,
            gpu_count=torch.cuda.device_count(),
        )
        return device, str(device), gpu_name

    device = torch.device("cpu")
    log_event(
        logger,
        logging.INFO,
        "rag.intent_classifier.evaluation.device",
        device=str(device),
        reason="cuda_unavailable",
    )
    return device, str(device), None


def _load_label_mapping(
    model_dir: Path,
    model: Any,
) -> dict[int, str]:
    labels_path = model_dir / "labels.json"
    if labels_path.exists():
        payload = json.loads(labels_path.read_text(encoding="utf-8"))
        raw_id_to_label = payload.get("id2label", {})
        if raw_id_to_label:
            return {
                int(label_id): str(label)
                for label_id, label in raw_id_to_label.items()
            }

    raw_id_to_label = getattr(model.config, "id2label", {}) or {}
    return {
        int(label_id): str(label)
        for label_id, label in raw_id_to_label.items()
    }


def _classification_metrics(
    expected_ids: list[int],
    predicted_ids: list[int],
    label_ids: Iterable[int],
) -> dict[str, Any]:
    """计算汇总指标和按行归一化的混淆矩阵。"""
    if len(expected_ids) != len(predicted_ids):
        raise ValueError("Expected and predicted label counts do not match.")
    if not expected_ids:
        raise ValueError("Cannot calculate metrics for an empty evaluation set.")

    labels = sorted(set(label_ids) | set(expected_ids) | set(predicted_ids))
    accuracy = sum(
        expected == predicted
        for expected, predicted in zip(expected_ids, predicted_ids)
    ) / len(expected_ids)

    per_label: dict[str, dict[str, float | int | str]] = {}
    confusion_matrix = [
        [0 for _ in labels]
        for _ in labels
    ]
    label_index = {label_id: index for index, label_id in enumerate(labels)}
    for expected, predicted in zip(expected_ids, predicted_ids):
        confusion_matrix[label_index[expected]][label_index[predicted]] += 1

    f1_scores: list[float] = []
    for label_id in labels:
        true_positive = sum(
            predicted == label_id and expected == label_id
            for expected, predicted in zip(expected_ids, predicted_ids)
        )
        false_positive = sum(
            predicted == label_id and expected != label_id
            for expected, predicted in zip(expected_ids, predicted_ids)
        )
        false_negative = sum(
            predicted != label_id and expected == label_id
            for expected, predicted in zip(expected_ids, predicted_ids)
        )
        support = true_positive + false_negative
        precision_denominator = true_positive + false_positive
        precision = (
            true_positive / precision_denominator
            if precision_denominator
            else 0.0
        )
        recall = true_positive / support if support else 0.0
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision + recall
            else 0.0
        )
        f1_scores.append(f1)
        per_label[str(label_id)] = {
            "label_id": label_id,
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": support,
        }

    return {
        "accuracy": accuracy,
        "macro_f1": sum(f1_scores) / len(f1_scores),
        "per_label": per_label,
        "confusion_matrix_labels": labels,
        "confusion_matrix": confusion_matrix,
        "sample_count": len(expected_ids),
    }


def _predict_examples(
    model: Any,
    tokenizer: Any,
    examples: list[IntentClassificationExample],
    device: Any,
    torch: Any,
    max_length: int,
    batch_size: int,
) -> tuple[list[int], list[list[float]]]:
    predicted_ids: list[int] = []
    probabilities: list[list[float]] = []
    model.eval()
    with torch.inference_mode():
        for start in range(0, len(examples), batch_size):
            batch = examples[start:start + batch_size]
            encoded = tokenizer(
                [example.text for example in batch],
                truncation=True,
                max_length=max_length,
                padding=True,
                return_tensors="pt",
            )
            encoded = {
                key: value.to(device)
                for key, value in encoded.items()
            }
            outputs = model(**encoded)
            batch_probabilities = torch.softmax(outputs.logits, dim=-1)
            batch_predictions = batch_probabilities.argmax(dim=-1)
            predicted_ids.extend(batch_predictions.cpu().tolist())
            probabilities.extend(batch_probabilities.cpu().tolist())
    return predicted_ids, probabilities


def evaluate_model(
    dataset_path: str | Path,
    settings: RAGSettings | None = None,
    model_path: str | Path | None = None,
    batch_size: int | None = None,
) -> dict[str, Any]:
    """使用独立标注 CSV 的每一行评估已保存的模型。"""
    runtime_settings = settings or get_rag_settings()
    torch, (model_class, tokenizer_class) = _load_runtime_dependencies()
    test_dataset_path = _resolve_project_path(str(dataset_path))
    model_dir = _resolve_project_path(
        model_path or runtime_settings.rag_output_dir
    )
    if not model_dir.is_dir():
        raise FileNotFoundError(f"Fine-tuned model directory does not exist: {model_dir}")

    tokenizer = tokenizer_class.from_pretrained(
        str(model_dir),
        local_files_only=True,
    )
    model = model_class.from_pretrained(
        str(model_dir),
        local_files_only=True,
    )
    device, device_name, gpu_name = _select_device(torch)
    model.to(device)
    label_mapping = _load_label_mapping(model_dir, model)
    if not label_mapping:
        raise ValueError(f"No id2label mapping found in model directory: {model_dir}")

    examples = load_intent_csv(test_dataset_path)
    _, dataset_id_to_label = build_intent_label_mapping(examples)
    if dataset_id_to_label != label_mapping:
        raise ValueError(
            "Dataset labels do not match the label mapping saved with the model: "
            f"dataset={dataset_id_to_label}, model={label_mapping}"
        )

    predicted_ids, _ = _predict_examples(
        model=model,
        tokenizer=tokenizer,
        examples=examples,
        device=device,
        torch=torch,
        max_length=runtime_settings.rag_max_length,
        batch_size=batch_size or runtime_settings.rag_eval_batch_size,
    )
    metrics = _classification_metrics(
        expected_ids=[example.label_id for example in examples],
        predicted_ids=predicted_ids,
        label_ids=label_mapping,
    )
    result = {
        "mode": "evaluation",
        "model_dir": str(model_dir),
        "dataset": str(test_dataset_path),
        "device": device_name,
        "gpu_name": gpu_name,
        "evaluation_scope": "full_independent_dataset",
        "metrics": metrics,
    }
    log_event(
        logger,
        logging.INFO,
        "rag.intent_classifier.evaluation.success",
        model_dir=model_dir,
        sample_count=metrics["sample_count"],
        accuracy=metrics["accuracy"],
        macro_f1=metrics["macro_f1"],
        device=device_name,
    )
    return result


def main() -> None:
    settings = get_rag_settings()
    parser = argparse.ArgumentParser(
        description="Evaluate the fine-tuned intent classifier on an independent CSV.",
    )
    parser.add_argument(
        "--dataset",
        default=settings.rag_eval_dataset_path,
        help=(
            "Independent labeled CSV; every row will be used for testing. "
            "Defaults to RAG_EVAL_DATASET_PATH."
        ),
    )
    parser.add_argument(
        "--model-dir",
        default=settings.rag_output_dir,
        help="Fine-tuned model directory.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=settings.rag_eval_batch_size,
        help="Inference batch size for independent test evaluation.",
    )
    args = parser.parse_args()

    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1.")
    configure_logging()
    result = evaluate_model(
        dataset_path=args.dataset,
        settings=settings,
        model_path=args.model_dir,
        batch_size=args.batch_size,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=float))


if __name__ == "__main__":
    main()
