"""Measure verified conversation-list loads without rendering private message text.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import argparse
import faulthandler
from functools import wraps
import json
from pathlib import Path
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _observe(owner, name):
    original = getattr(owner, name)

    @wraps(original)
    def measured(*args, **kwargs):
        started = time.perf_counter()
        print(json.dumps({"stage": name, "state": "started"}), flush=True)
        try:
            return original(*args, **kwargs)
        finally:
            print(
                json.dumps(
                    {"stage": name, "seconds": round(time.perf_counter() - started, 6)}
                ),
                flush=True,
            )

    setattr(owner, name, measured)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=PROJECT_ROOT / "local_store")
    parser.add_argument("--runs", type=int, default=3)
    arguments = parser.parse_args()

    from app.core import wechat_macos_recovery_bank as bank
    from app.core import wechat_media_event_index as media
    from app.core import wechat_media_placeholders as placeholders
    from app.core.wechat_archive import WechatArchiveRepository

    for owner, name in (
        (WechatArchiveRepository, "_resource_timeline_tables"),
        (placeholders, "attach_media_placeholders"),
        (media, "attach_media_event_index"),
        (bank, "attach_mac_recovery_bank"),
        (bank, "_validate_shadow_sources"),
    ):
        _observe(owner, name)

    faulthandler.dump_traceback_later(60, repeat=True)
    repository = WechatArchiveRepository(arguments.store)
    try:
        for number in range(max(1, arguments.runs)):
            requested_page = 2 if number == 1 else 1
            started = time.perf_counter()
            result = repository.query(page=requested_page, persist_event_cache=False)
            print(
                json.dumps(
                    {
                        "run": number + 1,
                        "requested_page": requested_page,
                        "seconds": round(time.perf_counter() - started, 6),
                        "conversations": result["conversation_count"],
                        "visible_conversations": len(result["conversations"]),
                        "messages": result["message_count"],
                        "resource_status": result["resource_index_status"],
                    }
                ),
                flush=True,
            )
    finally:
        faulthandler.cancel_dump_traceback_later()


if __name__ == "__main__":
    main()
