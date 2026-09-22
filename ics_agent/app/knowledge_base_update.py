# 模块职责：管理知识库全量解析、向量重建后台任务及其幂等状态。

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ics_agent.app.config import PROJECT_ROOT
from ics_agent.app.logging_config import log_event
from ics_agent.rag.config import RAGSettings, get_rag_settings


logger = logging.getLogger(__name__)


class KnowledgeBaseUpdateInProgress(RuntimeError):
    """Raised when another knowledge-base rebuild is already running."""


@dataclass(frozen=True)
class KnowledgeBaseUpdateStatus:
    """Public state of one knowledge-base rebuild task."""

    job_id: str
    source_hash: str
    status: str
    source_dir: str
    result: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "source_hash": self.source_hash,
            "status": self.status,
            "source_dir": self.source_dir,
            "result": self.result,
            "error": self.error,
        }


def _resolve_project_path(path_value: str | Path) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _fingerprint_directory(source_dir: Path) -> str:
    """Hash all file paths and contents in a stable order."""
    if not source_dir.is_dir():
        raise FileNotFoundError(
            f"Knowledge base directory does not exist: {source_dir}"
        )

    digest = hashlib.sha256()
    files = sorted(
        (path for path in source_dir.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(source_dir).as_posix().lower(),
    )
    if not files:
        raise ValueError(f"Knowledge base directory is empty: {source_dir}")

    for path in files:
        relative_path = path.relative_to(source_dir).as_posix()
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as input_file:
            for block in iter(lambda: input_file.read(1024 * 1024), b""):
                digest.update(block)
        digest.update(b"\0")
    return digest.hexdigest()


class KnowledgeBaseUpdateManager:
    """Submit and observe one background full vector-store rebuild."""

    def __init__(self, settings: RAGSettings | None = None) -> None:
        self.settings = settings or get_rag_settings()
        self._state_lock = threading.Lock()
        self._active_task: asyncio.Task | None = None
        self._active_hash: str | None = None
        self._last_completed_hash: str | None = None
        self._statuses: dict[str, KnowledgeBaseUpdateStatus] = {}

    async def submit_full_update(
        self,
        *,
        force: bool = False,
    ) -> KnowledgeBaseUpdateStatus:
        source_dir = _resolve_project_path(
            self.settings.knowledge_base_input_dir
        )
        source_hash = await asyncio.to_thread(
            _fingerprint_directory,
            source_dir,
        )

        with self._state_lock:
            active_task = self._active_task
            if active_task is not None and not active_task.done():
                if self._active_hash == source_hash:
                    active_job_id = self._find_active_job_id()
                    return self._statuses[active_job_id]
                raise KnowledgeBaseUpdateInProgress(
                    "A different knowledge-base update is already running."
                )

            if not force and self._last_completed_hash == source_hash:
                return KnowledgeBaseUpdateStatus(
                    job_id="",
                    source_hash=source_hash,
                    status="already_applied",
                    source_dir=str(source_dir),
                )

            job_id = uuid.uuid4().hex
            status = KnowledgeBaseUpdateStatus(
                job_id=job_id,
                source_hash=source_hash,
                status="submitted",
                source_dir=str(source_dir),
            )
            self._statuses[job_id] = status
            self._active_hash = source_hash
            self._active_task = asyncio.create_task(
                self._run_update(
                    job_id=job_id,
                    source_dir=source_dir,
                    source_hash=source_hash,
                )
            )
            self._active_task.add_done_callback(
                lambda task: self._finish_task(job_id, task)
            )

        log_event(
            logger,
            logging.INFO,
            "knowledge_base.update.submitted",
            job_id=job_id,
            source_dir=source_dir,
            source_hash=source_hash,
            force=force,
        )
        return status

    async def _run_update(
        self,
        *,
        job_id: str,
        source_dir: Path,
        source_hash: str,
    ) -> None:
        self._set_status(job_id, "running")
        started_at = time.perf_counter()
        try:
            from knowledge_base_in.parse_documents import (
                parse_and_update_vector_store,
            )

            result = await parse_and_update_vector_store(
                settings=self.settings,
                input_dir=source_dir,
                output_path=self.settings.knowledge_base_output_path,
                skip_vector_store=False,
            )
            result_summary = {
                "parse": result.get("parse"),
                "vector_store": result.get("vector_store"),
            }
            self._set_status(
                job_id,
                "completed",
                result=result_summary,
            )
            with self._state_lock:
                self._last_completed_hash = source_hash
            log_event(
                logger,
                logging.INFO,
                "knowledge_base.update.completed",
                job_id=job_id,
                source_hash=source_hash,
                elapsed_seconds=round(
                    time.perf_counter() - started_at,
                    3,
                ),
            )
        except Exception as exc:
            self._set_status(
                job_id,
                "failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            log_event(
                logger,
                logging.ERROR,
                "knowledge_base.update.failed",
                job_id=job_id,
                source_hash=source_hash,
                error_type=type(exc).__name__,
            )

    def _set_status(
        self,
        job_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self._state_lock:
            previous = self._statuses[job_id]
            self._statuses[job_id] = KnowledgeBaseUpdateStatus(
                job_id=previous.job_id,
                source_hash=previous.source_hash,
                status=status,
                source_dir=previous.source_dir,
                result=result if result is not None else previous.result,
                error=error,
            )

    def _find_active_job_id(self) -> str:
        for job_id, status in self._statuses.items():
            if status.status in {"submitted", "running"}:
                return job_id
        raise RuntimeError("Active knowledge-base task has no status record.")

    def _finish_task(
        self,
        job_id: str,
        task: asyncio.Task | None = None,
    ) -> None:
        if task is not None and not task.cancelled():
            try:
                task.exception()
            except asyncio.CancelledError:
                pass
        with self._state_lock:
            if task is None or self._active_task is task:
                self._active_task = None
                self._active_hash = None
            if job_id not in self._statuses:
                logger.warning(
                    "event=knowledge_base.update.status_missing job_id=%s",
                    job_id,
                )

    def get_status(self, job_id: str) -> KnowledgeBaseUpdateStatus | None:
        with self._state_lock:
            return self._statuses.get(job_id)


_manager: KnowledgeBaseUpdateManager | None = None
_manager_lock = threading.Lock()


def get_knowledge_base_update_manager() -> KnowledgeBaseUpdateManager:
    """Return the process-level knowledge-base update manager."""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = KnowledgeBaseUpdateManager()
    return _manager
