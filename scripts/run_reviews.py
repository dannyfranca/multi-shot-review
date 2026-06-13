#!/usr/bin/env python3
"""Run one parallel pass for each currently eligible review slice."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from review_state import ReviewStateError, run_reviews


def main() -> int:
    parser = argparse.ArgumentParser(description="Run eligible review slices in parallel once and update review state.")
    parser.add_argument("--review-dir", required=True, type=Path)
    parser.add_argument("--summary-json", type=Path, help="Atomically write the final compact summary JSON to PATH.")
    parser.add_argument("--no-stdout", action="store_true", help="Do not write the final JSON to stdout; requires --summary-json.")
    parser.add_argument(
        "--stream-progress",
        action="store_true",
        help="Opt into legacy per-slice progress on stderr for local debugging.",
    )
    parser.add_argument("--pretty-json", action="store_true", help="Pretty-print summary JSON for local debugging.")
    parser.add_argument(
        "--child-timeout-seconds",
        type=float,
        default=0,
        help="Timeout for each child review process; 0 or omitted means no timeout.",
    )
    args = parser.parse_args()

    if args.no_stdout and args.summary_json is None:
        print("error: --no-stdout requires --summary-json", file=sys.stderr)
        return 1
    if args.stream_progress and args.no_stdout:
        print("error: --stream-progress is incompatible with --no-stdout", file=sys.stderr)
        return 1

    try:
        rc, _summary = run_reviews(
            args.review_dir,
            summary_json=args.summary_json,
            no_stdout=args.no_stdout,
            stdout_json=not args.no_stdout,
            stream_progress=args.stream_progress,
            progress_stream=sys.stderr,
            pretty_json=args.pretty_json,
            child_timeout_seconds=args.child_timeout_seconds or None,
        )
        return rc
    except ReviewStateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
