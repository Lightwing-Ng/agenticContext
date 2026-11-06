"""Audit every HTML forwarded packet and publish independently verified iPad clocks.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.wechat_forwarded_clock_batch import (  # noqa: E402
    assess_inventory,
    inventory,
    publish_assessment,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-store", type=Path, default=Path("local_store"))
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--assessment", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    def progress(value):
        print(json.dumps(value), flush=True)

    if not args.inventory.exists():
        progress(inventory(args.local_store, args.inventory, progress=progress))
    if not args.assessment.exists():
        progress(
            assess_inventory(
                args.local_store, args.inventory, args.assessment, progress=progress
            )
        )
    if args.apply:
        progress(publish_assessment(args.local_store, args.assessment))
    else:
        print("Audit complete. Use --apply to publish verified clock pointers.")


if __name__ == "__main__":
    main()
