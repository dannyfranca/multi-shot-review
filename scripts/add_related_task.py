#!/usr/bin/env python3
"""Add or replace related/future task context for a review session."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from review_state import ReviewStateError, add_related_task


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Add or replace a related/future task under an initialized review directory."
    )
    parser.add_argument("--review-dir", required=True, type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--text")
    parser.add_argument("--file", type=Path)
    parser.add_argument("--dir", dest="directory", type=Path)
    args = parser.parse_args()
    try:
        add_related_task(
            args.review_dir,
            args.name,
            text=args.text,
            file=args.file,
            directory=args.directory,
        )
    except (OSError, ReviewStateError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"added related task {args.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
