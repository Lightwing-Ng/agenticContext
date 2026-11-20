#!/usr/bin/env python3
"""Stage or publish reviewed native resource details without rewriting archives.

Code version: v1.1.0-codex.0
"""

import argparse
import json
import os
from pathlib import Path
import stat
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.owner_only_files import ensure_owner_only, owner_only_problem
from app.core.wechat_resource_detail_repair import (
    publish_resource_detail_repair, stage_resource_detail_repair,
)


WORKSTATIONS = Path.home() / (
    "Library/CloudStorage/OneDrive-Personal/WeChat Text Archive/Workstations"
)
MAX_PACKET_FILE_BYTES = 128 * 1024**2


def _output(path):
    path = path.expanduser().absolute()
    if (
        path.exists() or path.is_symlink() or not path.parent.is_dir()
        or path.parent.is_symlink()
        or not path.resolve().is_relative_to(WORKSTATIONS.resolve())
    ):
        raise ValueError("Use a new output in the canonical private WeChat workstation.")
    return path


def _read(path):
    path = path.expanduser().absolute()
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode) or path.is_symlink()
        or owner_only_problem(path) or before.st_size > MAX_PACKET_FILE_BYTES
        or getattr(before, "st_flags", 0) & 0x40000000
    ):
        raise ValueError("Selected repair evidence must be bounded, private and resident.")
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
    )
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("Selected repair evidence changed before opening.")
        raw = stream.read(MAX_PACKET_FILE_BYTES + 1)
    after = path.lstat()
    if (
        len(raw) > MAX_PACKET_FILE_BYTES or owner_only_problem(path)
        or tuple(getattr(before, key) for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"))
        != tuple(getattr(after, key) for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"))
    ):
        raise ValueError("Selected repair evidence changed during reading.")
    return json.loads(raw)


def _write(path, value):
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            ensure_owner_only(path)
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    stage = commands.add_parser("stage", help="Prepare a candidate and review report only.")
    stage.add_argument("--local-store", type=Path, required=True)
    stage.add_argument("--packet", type=Path, action="append", required=True)
    stage.add_argument("--candidate", type=Path, required=True)
    stage.add_argument("--report", type=Path, required=True)
    stage.add_argument("--message-id", action="append")
    publish = commands.add_parser("publish", help="Publish an unchanged reviewed candidate.")
    publish.add_argument("--local-store", type=Path, required=True)
    publish.add_argument("--report", type=Path, required=True)
    publish.add_argument("--rollback", type=Path, required=True)
    publish.add_argument("--result", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "stage":
        candidate, destination = _output(args.candidate), _output(args.report)
        if candidate == destination:
            parser.error("The candidate and report need separate paths.")

        if len(args.packet) > 128 or sum(path.stat().st_size for path in args.packet) > MAX_PACKET_FILE_BYTES:
            parser.error("The combined packet input exceeds its bounded review budget.")
        values = [_read(path) for path in args.packet]
        if any(not isinstance(value, dict) for value in values):
            parser.error("Repair packets must be JSON objects.")
        manifested = ["manifest" in value for value in values]
        if any(manifested) and not all(manifested):
            parser.error("Use consistent captured manifests for the reviewed batch.")
        expected_rows = {} if all(manifested) else None
        target_sessions = {}
        packets = []
        for value in values:
            for entry in value.get("manifest", []):
                identifier = entry["message_id"]
                digest = entry["original_row_sha256"]
                if identifier in expected_rows and expected_rows[identifier] != digest:
                    raise ValueError("Captured manifests conflict on one upstream resource.")
                expected_rows[identifier] = digest
                if "target_conversation_id" in entry:
                    session = entry["target_conversation_id"]
                    if identifier in target_sessions and target_sessions[identifier] != session:
                        raise ValueError("Captured manifests disagree on the target conversation.")
                    target_sessions[identifier] = session
            if "packets" in value:
                if "manifest" not in value or not isinstance(value["packets"], list):
                    parser.error("An aggregate capture needs its exact target manifest.")
                packets.extend(value["packets"])
            else:
                packets.append(value.get("packet", value))
        if target_sessions and target_sessions.keys() != expected_rows.keys():
            parser.error("Every target needs its conversation when using explicit captured scopes.")

        report = stage_resource_detail_repair(
            args.local_store, candidate, packets, message_ids=args.message_id,
            expected_rows=expected_rows, target_sessions=target_sessions or None,
        )
        _write(destination, report)
    else:
        rollback, destination = _output(args.rollback), _output(args.result)
        if rollback == destination:
            parser.error("The rollback and result need separate paths.")
        report = publish_resource_detail_repair(
            args.local_store, _read(args.report), rollback_path=rollback,
        )
        _write(destination, report)
    print(json.dumps({
        key: report[key] for key in (
            "version", "published", "changed", "unchanged", "total_entries",
            "preserved_entries", "counts", "rejected", "candidate_bytes",
        ) if key in report
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
