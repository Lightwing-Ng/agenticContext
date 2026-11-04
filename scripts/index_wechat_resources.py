"""Build metadata-only WeChat resource placeholders from a preserved snapshot.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("preservation_snapshot", type=Path)
    parser.add_argument("--local-store", type=Path, required=True)
    parser.add_argument("--account", action="append", default=[])
    parser.add_argument("--report", type=Path)
    arguments = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.core.wechat_resource_index import build_resource_index

    report = build_resource_index(
        arguments.local_store,
        arguments.preservation_snapshot,
        account_ids=arguments.account,
    )
    if arguments.report:
        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        arguments.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        arguments.report.chmod(0o600)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
