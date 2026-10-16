"""Run explicitly: python -m quality.processing.worker [--once]."""

import argparse
import logging
import signal
import threading
import uuid

from .config import ProcessingSettings
from .repository import ProcessingRepository


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
    parser.add_argument("--source-key")
    parser.add_argument("--account-id", type=int)
    parser.add_argument("--conversation-id", type=int)
    args = parser.parse_args(argv)
    if args.rebuild and not args.source_key:
        parser.error("--rebuild requires --source-key")
    if not args.rebuild and any(value is not None for value in (args.source_key, args.account_id, args.conversation_id)):
        parser.error("Scope arguments are supported only with --rebuild")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = ProcessingSettings()
    repository = ProcessingRepository(settings, channel_bindings())
    stopped = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stopped.set())
    repository.open()
    try:
        if args.rebuild:
            count = repository.rebuild(args.source_key, args.account_id, args.conversation_id)
            logging.info("Rebuilt conversations=%s", count)
        elif args.once:
            repository.process_next("once-" + uuid.uuid4().hex)
        else:
            worker_id = "worker-" + uuid.uuid4().hex
            while not stopped.is_set():
                if not repository.process_next(worker_id):
                    stopped.wait(settings.processor_poll_seconds)
    finally:
        repository.close()


if __name__ == "__main__":
    main()
