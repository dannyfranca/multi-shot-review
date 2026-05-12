#!/usr/bin/env python3
"""Run one pass for each currently eligible review slice."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from review_state import ReviewStateError, run_reviews


def main() -> int:
    parser = argparse.ArgumentParser(description="Run eligible review slices once and update review state.")
    parser.add_argument("--review-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        return run_reviews(args.review_dir)
    except ReviewStateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
