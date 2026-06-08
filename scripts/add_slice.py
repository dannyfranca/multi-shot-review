#!/usr/bin/env python3
"""Register a slice in a state-managed review directory."""

from __future__ import annotations

import sys

from review_state import ReviewStateError, add_slice_from_args, parse_add_slice_args


def main() -> int:
    try:
        args = parse_add_slice_args()
        add_slice_from_args(args)
    except ReviewStateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"added slice {args.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
