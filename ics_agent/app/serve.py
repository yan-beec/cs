"""启动 Uvicorn，并将标准输出和错误输出写入有大小限制的轮转日志文件。"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import TextIO

from .core.config import PROJECT_ROOT, get_settings


class RotatingTextWriter:
    """将文本写入文件，并在超过字节限制前执行日志轮转。"""

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int,
        backup_count: int,
        encoding: str = "utf-8",
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes 必须为正数。")
        if backup_count < 1:
            raise ValueError("backup_count 必须为正数。")

        self.path = path
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.encoding = encoding
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open(
            "a",
            encoding=self.encoding,
            newline="",
        )
        self._size = self.path.stat().st_size
        if self._size >= self.max_bytes:
            self._rotate()

    def write(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            remaining = text
            while remaining:
                encoded_size = len(
                    remaining.encode(self.encoding, errors="replace")
                )
                available = self.max_bytes - self._size
                if available <= 0:
                    self._rotate()
                    available = self.max_bytes

                if encoded_size <= available:
                    self._file.write(remaining)
                    self._file.flush()
                    self._size += encoded_size
                    return

                cut = min(len(remaining), available)
                while cut > 0:
                    prefix = remaining[:cut]
                    prefix_size = len(
                        prefix.encode(self.encoding, errors="replace")
                    )
                    if prefix_size <= available:
                        break
                    cut -= 1

                if cut == 0:
                    self._rotate()
                    continue

                prefix = remaining[:cut]
                self._file.write(prefix)
                self._file.flush()
                self._size += len(
                    prefix.encode(self.encoding, errors="replace")
                )
                remaining = remaining[cut:]
                if remaining:
                    self._rotate()

    def close(self) -> None:
        with self._lock:
            self._file.close()

    def _rotate(self) -> None:
        self._file.close()
        oldest = self.path.with_name(
            f"{self.path.name}.{self.backup_count}"
        )
        if oldest.exists():
            oldest.unlink()

        for index in range(self.backup_count - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            target = self.path.with_name(f"{self.path.name}.{index + 1}")
            if source.exists():
                os.replace(source, target)

        if self.path.exists():
            os.replace(
                self.path,
                self.path.with_name(f"{self.path.name}.1"),
            )
        self._file = self.path.open(
            "a",
            encoding=self.encoding,
            newline="",
        )
        self._size = 0


class _StreamPump(threading.Thread):
    def __init__(
        self,
        stream: TextIO,
        writer: RotatingTextWriter,
    ) -> None:
        super().__init__(daemon=True)
        self.stream = stream
        self.writer = writer

    def run(self) -> None:
        try:
            for line in iter(self.stream.readline, ""):
                self.writer.write(line)
        finally:
            self.stream.close()


def _resolve_log_path(directory: str, filename: str) -> Path:
    path = Path(directory)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path / filename


def _build_parser() -> argparse.ArgumentParser:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        description="启动 ics_agent，并轮转保存 Uvicorn 的标准输出和错误输出日志。"
    )
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument(
        "--reload-dir",
        action="append",
        default=[],
        help="启用热重载时要监听的目录，可重复指定。",
    )
    parser.add_argument(
        "--log-max-bytes",
        type=int,
        default=settings.uvicorn_log_max_bytes,
        help="每个活动日志文件的最大大小。",
    )
    parser.add_argument(
        "--log-backup-count",
        type=int,
        default=settings.uvicorn_log_backup_count,
        help="保留的历史轮转日志文件数量。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    args = _build_parser().parse_args(argv)
    if args.log_max_bytes < 1024:
        raise SystemExit("--log-max-bytes 至少应为 1024。")
    if args.log_backup_count < 1:
        raise SystemExit("--log-backup-count 至少应为 1。")

    stdout_writer = RotatingTextWriter(
        _resolve_log_path(
            settings.uvicorn_log_directory,
            settings.uvicorn_stdout_log_filename,
        ),
        max_bytes=args.log_max_bytes,
        backup_count=args.log_backup_count,
    )
    stderr_writer = RotatingTextWriter(
        _resolve_log_path(
            settings.uvicorn_log_directory,
            settings.uvicorn_stderr_log_filename,
        ),
        max_bytes=args.log_max_bytes,
        backup_count=args.log_backup_count,
    )

    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "ics_agent.app.main:app",
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    if args.reload:
        command.append("--reload")
        for directory in args.reload_dir:
            command.extend(["--reload-dir", directory])

    process = subprocess.Popen(
        command,
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_pump = _StreamPump(process.stdout, stdout_writer)
    stderr_pump = _StreamPump(process.stderr, stderr_writer)
    stdout_pump.start()
    stderr_pump.start()

    try:
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        try:
            return process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            return process.wait()
    finally:
        stdout_pump.join(timeout=10)
        stderr_pump.join(timeout=10)
        stdout_writer.close()
        stderr_writer.close()


if __name__ == "__main__":
    raise SystemExit(main())
