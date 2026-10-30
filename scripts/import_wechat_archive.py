"""Import a previously verified text archive into Local resources."""

# Code version: v1.0.0-codex.0

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--local-store", type=Path, required=True)
    parser.add_argument(
        "--source-kind", action="append", default=[], metavar="SOURCE=KIND"
    )
    parser.add_argument("--report", type=Path)
    arguments = parser.parse_args()
    kinds = {}
    for value in arguments.source_kind:
        source, separator, kind = value.partition("=")
        if not separator or not source:
            parser.error("--source-kind requires SOURCE=direct, group, or unclassified")
        kinds[source] = kind
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.core.wechat_archive import import_wechat_archive

    report = import_wechat_archive(
        arguments.archive, arguments.local_store, source_kinds=kinds
    )
    if arguments.report:
        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        arguments.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        arguments.report.chmod(0o600)
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "read_files"},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
