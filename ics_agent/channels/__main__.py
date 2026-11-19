"""python -m ics_agent.channels initialize / list。"""

import argparse
import json
from dataclasses import asdict

from .config import list_channels


def main() -> None:
    parser = argparse.ArgumentParser(description="Channel resource management")
    parser.add_argument("command", choices=["initialize", "list"])
    args = parser.parse_args()
    if args.command == "initialize":
        from .bootstrap import initialize_channels
        result = initialize_channels()
    else:
        result = [asdict(channel) for channel in list_channels()]
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
