#!/usr/bin/env python3
"""Create and print a collision-resistant local review artifact directory."""

from __future__ import annotations

import argparse
import secrets
from datetime import datetime, timezone
from pathlib import Path


def session_id() -> str:
    now = datetime.now(timezone.utc)
    timestamp = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    timestamp = timestamp.replace(":", "-")
    return f"{timestamp}-{secrets.token_hex(4)}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a .review/<timestamp-random>/ directory in the current working tree."
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Execution path where .review should be created. Defaults to the current directory.",
    )
    args = parser.parse_args()

    review_root = Path(args.root).resolve() / ".review"
    for _ in range(10):
        review_dir = review_root / session_id()
        try:
            review_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            if review_dir.exists():
                continue
            raise
        print(review_dir)
        return 0

    raise RuntimeError("could not create a unique review directory after 10 attempts")


if __name__ == "__main__":
    raise SystemExit(main())
