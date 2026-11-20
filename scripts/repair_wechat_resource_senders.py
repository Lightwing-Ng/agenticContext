#!/usr/bin/env python3
"""Review or stage source-proven WeChat resource sender recovery.

Code version: v1.0.1-codex.0

Without --candidate this command only reviews explicitly selected message IDs.
Staging uses an APFS resource-catalog clone and never publishes or clones history.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.wechat_native_resource_revalidation import _resident
from app.core.wechat_native_resource_senders import (
    VERSION,
    recover_native_resource_sender,
    stage_native_resource_senders,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--native-database", type=Path, required=True)
    parser.add_argument("--message-id", action="append", required=True)
    parser.add_argument("--dictionary", action="append", default=[], metavar="ID=PATH")
    parser.add_argument("--candidate", type=Path)
    args = parser.parse_args(argv)
    dictionaries = {}
    for value in args.dictionary:
        identifier, separator, raw_path = value.partition("=")
        if (
            not separator
            or not identifier.isascii()
            or not identifier.isdecimal()
            or int(identifier) <= 0
        ):
            parser.error("A dictionary must be an explicit positive ID=PATH pair.")
        identifier = int(identifier)
        if identifier in dictionaries:
            parser.error("Select each dictionary ID only once.")
        path = Path(raw_path).absolute()
        before = _resident(path)
        if before["size"] > 16 * 1024**2:
            parser.error("A selected dictionary exceeds its byte budget.")
        content = path.read_bytes()
        if _resident(path) != before:
            parser.error("A selected dictionary changed while reading.")
        dictionaries[identifier] = content
    if args.candidate:
        result = stage_native_resource_senders(
            args.history,
            args.catalog,
            args.candidate,
            args.native_database,
            message_ids=args.message_id,
            dictionaries=dictionaries,
        )
    else:
        if not 1 <= len(args.message_id) <= 100 or len(set(args.message_id)) != len(
            args.message_id
        ):
            parser.error("Select between one and 100 distinct message IDs.")
        result = {
            "version": VERSION,
            "published": False,
            "resolutions": [
                recover_native_resource_sender(
                    args.history,
                    args.catalog,
                    identifier,
                    args.native_database,
                    dictionaries=dictionaries,
                )["resolution"]
                for identifier in args.message_id
            ],
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
