# 模块职责：使用本地 BERT 模型和 transformers.Trainer 完成意图分类微调。

from __future__ import annotations

import inspect
import json
import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ics_agent.app.config import PROJECT_ROOT
from ics_agent.app.logging_config import log_event
from ics_agent.rag.config import RAGSettings, get_rag_settings

from .data import (
    IntentClassificationDataset,
    IntentClassificationExample,
    build_intent_label_mapping,
    calculate_intent_dataset_sha256,
)


logger = logging.getLogger(__name__)
CHECKPOINT_PREFIX = "checkpoint-"
LABEL_METADATA_SCHEMA_VERSION = 1
TRAINING_ARTIFACT_NAMES = frozenset(
    {
        "trainer_state.json",
        "training_args.bin",
    }
)


@dataclass(frozen=True)
class IntentTrainingResult:
    """微调任务完成后返回的摘要。"""

    output_dir: str
    label2id: dict[str, int]
    id2label: dict[int, str]
    metrics: dict[str, Any]
    dataset_path: str | None = None
    dataset_sha256: str | None = None


def _compute_classification_metrics(eval_prediction: Any) -> dict[str, float]:
    """无需 scikit-learn 即可计算准确率和宏平均 F1。"""
    predictions = eval_prediction.predictions
    if isinstance(predictions, tuple):
        predictions = predictions[0]
    predicted_ids = predictions.argmax(axis=-1).tolist()
    expected_ids = eval_prediction.label_ids.tolist()

    if not expected_ids:
        return {"accuracy": 0.0, "macro_f1": 0.0}

    accuracy = sum(
        predicted == expected
        for predicted, expected in zip(predicted_ids, expected_ids)
    ) / len(expected_ids)

    labels = sorted(set(expected_ids) | set(predicted_ids))
    f1_scores: list[float] = []
    for label_id in labels:
        true_positive = sum(
            predicted == label_id and expected == label_id
            for predicted, expected in zip(predicted_ids, expected_ids)
        )
        false_positive = sum(
            predicted == label_id and expected != label_id
            for predicted, expected in zip(predicted_ids, expected_ids)
        )
        false_negative = sum(
            predicted != label_id and expected == label_id
            for predicted, expected in zip(predicted_ids, expected_ids)
        )
        precision_denominator = true_positive + false_positive
        recall_denominator = true_positive + false_negative
        precision = (
            true_positive / precision_denominator
            if precision_denominator
            else 0.0
        )
        recall = (
            true_positive / recall_denominator
            if recall_denominator
            else 0.0
        )
        f1_scores.append(
            2 * precision * recall / (precision + recall)
            if precision + recall
            else 0.0
        )

    return {
        "accuracy": accuracy,
        "macro_f1": sum(f1_scores) / len(f1_scores),
    }


class IntentClassifierTrainer:
    """使用 ``transformers.Trainer`` 构建并运行 BERT 序列分类器。"""

    def __init__(self, settings: RAGSettings | None = None) -> None:
        self.settings = settings or get_rag_settings()

    @staticmethod
    def _resolve_project_path(path_value: str) -> Path:
        path = Path(path_value)
        return path if path.is_absolute() else PROJECT_ROOT / path

    @staticmethod
    def _load_transformers() -> tuple[Any, Any, Any, Any, Any]:
        try:
            from transformers import (
                AutoModelForSequenceClassification,
                AutoTokenizer,
                DataCollatorWithPadding,
                Trainer,
                TrainingArguments,
            )
        except ImportError as exc:
            raise RuntimeError(
                "Intent classification training dependencies are missing. "
                "Install requirements.txt in the cs environment before training."
            ) from exc
        return (
            AutoTokenizer,
            AutoModelForSequenceClassification,
            DataCollatorWithPadding,
            Trainer,
            TrainingArguments,
        )

    @staticmethod
    def _detect_device() -> tuple[bool, str, str | None, int]:
        """检测 CUDA，并返回训练是否应使用 GPU。"""
        try:
            import torch
        except ImportError as exc:
            raise RuntimeError(
                "PyTorch is required for intent classification training."
            ) from exc

        if not torch.cuda.is_available():
            log_event(
                logger,
                logging.INFO,
                "rag.intent_classifier.device.selected",
                device="cpu",
                reason="cuda_unavailable",
            )
            return False, "cpu", None, 0

        gpu_count = torch.cuda.device_count()
        gpu_name = torch.cuda.get_device_name(0)
        device = "cuda:0"
        log_event(
            logger,
            logging.INFO,
            "rag.intent_classifier.device.selected",
            device=device,
            gpu_name=gpu_name,
            gpu_count=gpu_count,
        )
        return True, device, gpu_name, gpu_count

    def _build_training_arguments(
        self,
        training_arguments_class: Any,
        checkpoint_dir: Path,
        has_eval_dataset: bool,
        use_cuda: bool,
    ) -> Any:
        settings = self.settings
        kwargs: dict[str, Any] = {
            "output_dir": str(checkpoint_dir),
            "num_train_epochs": settings.rag_num_train_epochs,
            "learning_rate": settings.rag_learning_rate,
            "per_device_train_batch_size": settings.rag_train_batch_size,
            "per_device_eval_batch_size": settings.rag_eval_batch_size,
            "weight_decay": settings.rag_weight_decay,
            "warmup_ratio": settings.rag_warmup_ratio,
            "logging_strategy": "steps",
            "logging_steps": settings.rag_logging_steps,
            "save_strategy": "epoch",
            "save_total_limit": settings.rag_save_total_limit,
            "seed": settings.rag_seed,
            "report_to": [],
            "remove_unused_columns": True,
        }

        parameter_names = inspect.signature(training_arguments_class).parameters
        if "use_cpu" in parameter_names:
            kwargs["use_cpu"] = not use_cuda
        elif "no_cuda" in parameter_names:
            kwargs["no_cuda"] = not use_cuda

        if has_eval_dataset:
            evaluation_key = (
                "eval_strategy"
                if "eval_strategy" in parameter_names
                else "evaluation_strategy"
            )
            kwargs[evaluation_key] = "epoch"
            kwargs.update(
                {
                    "load_best_model_at_end": True,
                    "metric_for_best_model": "macro_f1",
                    "greater_is_better": True,
                }
            )
        return training_arguments_class(**kwargs)

    def build_trainer(
        self,
        train_examples: Iterable[IntentClassificationExample],
        eval_examples: Iterable[IntentClassificationExample] | None = None,
        checkpoint_dir: Path | None = None,
    ) -> tuple[Any, Any, dict[str, int], dict[int, str]]:
        """创建已配置的 Transformers Trainer，但不启动训练。"""
        (
            auto_tokenizer,
            auto_model,
            data_collator_with_padding,
            trainer_class,
            training_arguments_class,
        ) = self._load_transformers()
        use_cuda, device, gpu_name, gpu_count = self._detect_device()

        train_list = list(train_examples)
        eval_list = list(eval_examples) if eval_examples is not None else None
        if not train_list:
            raise ValueError("Intent classification train dataset cannot be empty.")
        all_examples = train_list + (eval_list or [])
        label2id, id2label = build_intent_label_mapping(all_examples)

        model_dir = self._resolve_project_path(
            self.settings.rag_pretrained_model_dir
        )
        if not model_dir.is_dir():
            raise FileNotFoundError(
                f"Local pretrained model directory does not exist: {model_dir}"
            )

        tokenizer = auto_tokenizer.from_pretrained(
            str(model_dir),
            local_files_only=True,
        )
        model = auto_model.from_pretrained(
            str(model_dir),
            num_labels=len(label2id),
            label2id=label2id,
            id2label=id2label,
            local_files_only=True,
            ignore_mismatched_sizes=True,
        )
        train_dataset = IntentClassificationDataset(
            train_list,
            tokenizer,
            self.settings.rag_max_length,
        )
        eval_dataset = (
            IntentClassificationDataset(
                eval_list,
                tokenizer,
                self.settings.rag_max_length,
            )
            if eval_list
            else None
        )
        output_dir = self._resolve_project_path(self.settings.rag_output_dir)
        checkpoint_dir = checkpoint_dir or output_dir
        training_arguments = self._build_training_arguments(
            training_arguments_class,
            checkpoint_dir,
            has_eval_dataset=eval_dataset is not None,
            use_cuda=use_cuda,
        )

        trainer_kwargs: dict[str, Any] = {
            "model": model,
            "args": training_arguments,
            "train_dataset": train_dataset,
            "eval_dataset": eval_dataset,
            "data_collator": data_collator_with_padding(tokenizer=tokenizer),
            "compute_metrics": _compute_classification_metrics,
        }
        trainer_parameters = inspect.signature(trainer_class).parameters
        if "processing_class" in trainer_parameters:
            trainer_kwargs["processing_class"] = tokenizer
        else:
            trainer_kwargs["tokenizer"] = tokenizer

        trainer = trainer_class(**trainer_kwargs)
        log_event(
            logger,
            logging.INFO,
            "rag.intent_classifier.trainer.created",
            model_dir=model_dir,
            output_dir=output_dir,
            checkpoint_dir=checkpoint_dir,
            device=device,
            gpu_name=gpu_name,
            gpu_count=gpu_count,
            train_count=len(train_dataset),
            eval_count=0 if eval_dataset is None else len(eval_dataset),
            label_count=len(label2id),
        )
        return trainer, tokenizer, label2id, id2label

    def train(
        self,
        train_examples: Iterable[IntentClassificationExample],
        eval_examples: Iterable[IntentClassificationExample] | None = None,
        dataset_path: str | Path | None = None,
    ) -> IntentTrainingResult:
        """训练、评估并保存分类器及其标签元数据。"""
        output_dir = self._resolve_project_path(self.settings.rag_output_dir)
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        self._clean_final_output(output_dir)
        resolved_dataset_path = (
            self._resolve_project_path(str(dataset_path))
            if dataset_path is not None
            else None
        )
        dataset_sha256 = (
            calculate_intent_dataset_sha256(resolved_dataset_path)
            if resolved_dataset_path is not None
            else None
        )
        log_event(
            logger,
            logging.INFO,
            "rag.intent_classifier.training.start",
            output_dir=output_dir,
            dataset_path=resolved_dataset_path,
            dataset_sha256=dataset_sha256,
        )

        with tempfile.TemporaryDirectory(
            prefix=f".{output_dir.name}-",
            dir=str(output_dir.parent),
        ) as temporary_dir:
            checkpoint_dir = Path(temporary_dir)
            trainer, tokenizer, label2id, id2label = self.build_trainer(
                train_examples=train_examples,
                eval_examples=eval_examples,
                checkpoint_dir=checkpoint_dir,
            )
            train_output = trainer.train()
            # load_best_model_at_end 已将验证集指标最好的模型放回内存。
            trainer.save_model(str(output_dir))
            tokenizer.save_pretrained(str(output_dir))
            metrics = dict(train_output.metrics)
            if trainer.eval_dataset is not None:
                metrics.update(trainer.evaluate())
            metrics["train_samples"] = len(trainer.train_dataset)
            if trainer.eval_dataset is not None:
                metrics["eval_samples"] = len(trainer.eval_dataset)

        self._remove_training_artifacts(output_dir)

        labels_path = output_dir / "labels.json"
        labels_payload: dict[str, Any] = {
            "schema_version": LABEL_METADATA_SCHEMA_VERSION,
            "label_count": len(id2label),
            "label2id": label2id,
            "id2label": {
                str(label_id): label
                for label_id, label in id2label.items()
            },
        }
        if resolved_dataset_path is not None:
            try:
                dataset_display_path = resolved_dataset_path.relative_to(PROJECT_ROOT)
            except ValueError:
                dataset_display_path = resolved_dataset_path
            labels_payload["dataset"] = {
                "path": str(dataset_display_path).replace("\\", "/"),
                "sha256": dataset_sha256,
            }
        labels_path.write_text(
            json.dumps(
                labels_payload,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        log_event(
            logger,
            logging.INFO,
            "rag.intent_classifier.training.success",
            output_dir=output_dir,
            train_samples=metrics["train_samples"],
            eval_samples=metrics.get("eval_samples"),
        )
        return IntentTrainingResult(
            output_dir=str(output_dir),
            label2id=label2id,
            id2label=id2label,
            metrics=metrics,
            dataset_path=(
                None
                if resolved_dataset_path is None
                else str(resolved_dataset_path)
            ),
            dataset_sha256=dataset_sha256,
        )

    @staticmethod
    def _clean_final_output(output_dir: Path) -> None:
        """删除之前训练运行遗留的过期检查点目录。"""
        if not output_dir.exists():
            return
        for child in output_dir.iterdir():
            if child.is_dir() and child.name.startswith(CHECKPOINT_PREFIX):
                shutil.rmtree(child)

    @staticmethod
    def _remove_training_artifacts(output_dir: Path) -> None:
        """保留推理文件，同时删除仅供 Trainer 使用的状态文件。"""
        for child in output_dir.iterdir():
            if child.name in TRAINING_ARTIFACT_NAMES:
                child.unlink()
