"""Run explicitly: python -m quality.processing.worker [--once]."""

import argparse
import json
import logging
import signal
import threading
import time
import uuid

from .config import ProcessingSettings
from .repository import ProcessingRepository


MAINTENANCE_SLICE_SECONDS = 90


def run_loop(repository, worker_id, stopped, poll_seconds, *, clock=time.monotonic):
    """持续事件期间按时间片执行维护，空闲时连续推进维护。"""
    last_maintenance = clock()
    while not stopped.is_set():
        if repository.process_next(worker_id):
            if clock() - last_maintenance >= MAINTENANCE_SLICE_SECONDS:
                repository.maintenance_step()
                last_maintenance = clock()
            continue
        if not repository.maintenance_step():
            stopped.wait(poll_seconds)
        last_maintenance = clock()


def channel_bindings():
    # Reuse the shared configuration but deliberately avoid the bot resolver's
    # fallback-to-default behavior. Unknown inboxes stay unclassified.
    from ics_agent.channels.config import list_channels
    return {binding: channel.key for channel in list_channels() for binding in channel.inboxes}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Replay captured evidence into quality query projections")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Process one ready conversation batch")
    mode.add_argument("--rebuild", action="store_true", help="Rebuild projections from immutable events")
    mode.add_argument("--rebuild-plan", action="store_true", help="Generate a read-only consistent candidate manifest")
    parser.add_argument("--source-key")
    parser.add_argument("--account-id", type=int)
    parser.add_argument("--conversation-id", type=int)
    parser.add_argument("--output-dir")
    parser.add_argument("--plan-chunk-size", type=int)
    parser.add_argument("--manifest")
    parser.add_argument("--result-file")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    if args.rebuild and not args.manifest and not args.source_key:
        parser.error("--rebuild requires --source-key")
    if not args.rebuild and not args.rebuild_plan and any(value is not None for value in (args.source_key, args.account_id, args.conversation_id)):
        parser.error("Scope arguments are supported only with --rebuild")
    if args.rebuild_plan and (not args.source_key or not args.account_id or not args.output_dir or args.conversation_id):
        parser.error("Plan requires source, account and output directory")
    if args.plan_chunk_size is not None and not args.rebuild_plan:
        parser.error("Chunk size requires plan mode")
    args.plan_chunk_size = args.plan_chunk_size if args.plan_chunk_size is not None else 200000
    if not 1000 <= args.plan_chunk_size <= 2000000:
        parser.error("Plan chunk size must be between 1000 and 2000000")
    if args.manifest and (not args.rebuild or not args.result_file or any((args.source_key, args.account_id, args.conversation_id))):
        parser.error("Manifest rebuild requires result file and excludes scope arguments")
    if (args.resume or args.result_file) and not args.manifest:
        parser.error("Resume/result require manifest")
    if args.output_dir and not args.rebuild_plan:
        parser.error("Output directory requires plan mode")
    if args.source_key is not None and (not args.source_key or args.source_key != args.source_key.strip()):
        parser.error("Source key must be nonblank without surrounding whitespace")
    if any(value is not None and not 0 < value < 2**63 for value in (args.account_id, args.conversation_id)):
        parser.error("Scope IDs must be positive bigint values")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = ProcessingSettings()
    repository = ProcessingRepository(settings, channel_bindings())
    stopped = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stopped.set())
    repository.open()
    try:
        from .rebuild_batch import generate_plan, execute_batch
        if args.rebuild_plan:
            result = generate_plan(repository, args.source_key, args.account_id, args.output_dir, args.plan_chunk_size, stopped=stopped)
            logging.info("Plan candidates=%s directory=%s", result["candidate_count"], args.output_dir)
            return 0
        elif args.manifest:
            return execute_batch(repository, args.manifest, args.result_file, args.resume, stopped)
        elif args.rebuild:
            report = repository.rebuild(args.source_key, args.account_id, args.conversation_id, stopped=stopped)
            logging.info("Rebuilt completed=%s skipped=%s failed=%s", len(report.completed), len(report.skipped), len(report.failed))
            return 0 if report.successful else 1
        elif args.once:
            repository.process_next("once-" + uuid.uuid4().hex)
        else:
            worker_id = "worker-" + uuid.uuid4().hex
            run_loop(repository, worker_id, stopped, settings.processor_poll_seconds)
    except (ValueError, KeyError, json.JSONDecodeError) as error:
        logging.error("Invalid batch: %s", type(error).__name__)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        logging.error("Rebuild failed: %s", type(error).__name__)
        return 1
    finally:
        repository.close()


if __name__ == "__main__":
    raise SystemExit(main())
