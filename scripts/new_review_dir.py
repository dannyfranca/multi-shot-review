#!/usr/bin/env python3
"""Compatibility wrapper for init_state.py."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from review_state import ReviewStateError, init_review_state


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create .review/<timestamp-random>/ with an initialized _state.json file."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=".",
        help="Execution path where .review should be created. Defaults to the current directory.",
    )
    parser.add_argument("--task", help="Original user request text for this review session.")
    parser.add_argument(
        "--task-file",
        type=Path,
        help="Path to a Markdown/text file containing the original user request, or '-' for stdin.",
    )
    args = parser.parse_args()
    if (args.task is not None) == (args.task_file is not None):
        parser.error("choose exactly one task source: --task or --task-file")
    try:
        task = args.task
        if args.task_file is not None:
            task = sys.stdin.read() if str(args.task_file) == "-" else args.task_file.read_text(encoding="utf-8")
        review_dir = init_review_state(args.root, task or "")
    except (OSError, ReviewStateError) as exc:
        parser.error(str(exc))
    print(review_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
