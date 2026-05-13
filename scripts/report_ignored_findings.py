#!/usr/bin/env python3
"""Report ignored findings for a state-managed review run."""

from __future__ import annotations

import sys

from review_state import ReviewStateError, parse_report_ignored_args, report_ignored_from_args


def main() -> int:
    try:
        args = parse_report_ignored_args()
        _changed, message = report_ignored_from_args(args)
    except ReviewStateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
