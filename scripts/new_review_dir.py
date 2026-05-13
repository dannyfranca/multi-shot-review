#!/usr/bin/env python3
"""Compatibility wrapper for init-state.py."""

from __future__ import annotations

import argparse
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
    args = parser.parse_args()
    try:
        review_dir = init_review_state(args.root)
    except (OSError, ReviewStateError) as exc:
        parser.error(str(exc))
    print(review_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
