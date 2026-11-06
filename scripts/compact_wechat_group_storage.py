"""Inspect or reversibly share exact bodies across native group or private accounts.

Code version: v1.1.0-codex.0
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-store", type=Path, required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--conversation", action="append")
    selection.add_argument("--all-private-chats", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-directory", type=Path)
    arguments = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.core.wechat_archive import wechat_archive_path
    from app.core.wechat_shared_storage import (
        analyze_shared_conversation_bodies,
        analyze_shared_conversation_scopes,
        discover_reciprocal_private_scopes,
    )
    from app.core.wechat_shared_storage_publish import (
        publish_shared_conversation_bodies,
        publish_shared_conversation_scopes,
    )

    path = wechat_archive_path(arguments.local_store).absolute()
    if not path.is_file() or path.is_symlink() or getattr(path.stat(), "st_flags", 0) & 0x40000000:
        parser.error("Select an existing, downloaded regular history database")
    discovery = None
    if arguments.all_private_chats:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
            discovery = discover_reciprocal_private_scopes(connection)
        if not discovery["scopes"]:
            parser.error("No reciprocal verified native private conversations are available")
        scopes = discovery["scopes"]
    if arguments.apply:
        if arguments.backup_directory is None:
            parser.error("--apply requires a new --backup-directory")
        publish = publish_shared_conversation_scopes if arguments.all_private_chats else publish_shared_conversation_bodies
        report = publish(
            arguments.local_store, scopes if arguments.all_private_chats else arguments.conversation,
            backup_directory=arguments.backup_directory,
        )
    else:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
            report = (
                analyze_shared_conversation_scopes(connection, scopes)
                if arguments.all_private_chats
                else analyze_shared_conversation_bodies(connection, arguments.conversation)
            )
        report.update(changed=False, dry_run=True, filesystem_bytes_reclaimed=0)
    if discovery is not None:
        report["skipped_private_pairs"] = discovery["skipped"]
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
