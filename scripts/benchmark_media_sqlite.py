"""Benchmark deterministic synthetic media metadata without account data.

Code version: v1.0.0
"""

from __future__ import annotations

import argparse
from pathlib import Path
import platform
import sqlite3
import sys
import tempfile

import pyarrow as pa
import pyarrow.parquet as pq

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.migrate_media_sqlite import (  # noqa: E402
    SPECS,
    _json_report,
    _store,
    benchmark,
    file_state,
)


def synthetic_rows(schema: pa.Schema, key: str, count: int) -> list[dict]:
    """Use public synthetic values with distinct identities and bounded repeats."""
    rows = []
    for ordinal in range(count):
        row = {}
        for field in schema:
            if pa.types.is_string(field.type):
                value = f"synthetic-{field.name}-{ordinal}"
                if field.name.endswith("_url"):
                    value = f"https://example.invalid/collection/{ordinal // 5}/item/{ordinal}"
                elif field.name == "content_sha256":
                    value = f"{ordinal:064x}"
                elif field.name == "status":
                    value = ("discovered", "completed", "failed")[ordinal % 3]
                elif field.name.endswith("_at"):
                    value = "2026-01-01T00:00:00Z"
                elif field.name == "prompt_markdown":
                    value = (
                        f"Synthetic image request {ordinal // 5}. "
                        + "A public test landscape description. " * 8
                    )
                row[field.name] = value
            elif pa.types.is_list(field.type):
                row[field.name] = [f"https://example.invalid/item/{ordinal}"]
            elif pa.types.is_integer(field.type):
                row[field.name] = (
                    (3 if key == "relative_tweet_dir" else 2)
                    if field.name == "schema_version"
                    else ordinal % 1000
                )
            elif pa.types.is_floating(field.type):
                row[field.name] = (
                    None if ordinal % 17 == 0 else 1_700_000_000.0 + ordinal
                )
            else:
                raise ValueError(f"Unsupported synthetic fixture type: {field.type}.")
        row[key] = f"key-{ordinal:09d}"
        rows.append(row)
    return rows


def run(counts: list[int], repeats: int, report: Path) -> dict:
    result = {
        "version": "v1.0.0",
        "dataset": "deterministic synthetic metadata; no account data; not a production size forecast",
        "runtime": {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "pyarrow": pa.__version__,
        },
        "entries": [],
    }
    with tempfile.TemporaryDirectory(prefix="media-synthetic-benchmark-") as directory:
        root = Path(directory)
        for count in counts:
            for name, (schema, key, _indexes) in SPECS.items():
                source = root / str(count) / name
                source.parent.mkdir(parents=True, exist_ok=True)
                target = source.with_suffix(".sqlite3")
                rows = synthetic_rows(schema, key, count)
                table = pa.Table.from_pylist(rows, schema=schema)
                pq.write_table(table, source, compression="zstd")
                _store(target, schema).replace_rows(rows)
                result["entries"].append(
                    {
                        "store": name,
                        "rows": count,
                        "parquet": file_state(source),
                        "sqlite": file_state(target),
                        "benchmark": benchmark(source, target, table, repeats),
                    }
                )
                _json_report(report, result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", nargs="+", type=int, default=[1000, 10000])
    parser.add_argument("--repeats", type=int, default=9)
    parser.add_argument("--report", type=Path, required=True)
    arguments = parser.parse_args()
    if any(count <= 0 for count in arguments.rows):
        parser.error("--rows values must be positive")
    run(arguments.rows, arguments.repeats, arguments.report)
    print(f"Synthetic benchmark written to {arguments.report}")


if __name__ == "__main__":
    main()
