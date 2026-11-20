"""Capture local group member snapshots through a scoped loopback-only action.

Code version: v1.0.1-codex.0
"""

from threading import Lock
from urllib.parse import urlsplit
import re
import sqlite3

from flask import abort, redirect, request, url_for

from app.core.agent import is_loopback_address
from app.core.storage import WechatGroupRosterRepository, resolve_wechat_refresh_scope


_SESSION = re.compile(r"wechat:[0-9a-f]{64}")
_CAPTURE_LOCK = Lock()
_FEEDBACK = {
    "saved": "Member snapshot saved. Differences describe observations, not departure reasons.",
    "unavailable": "A verified local member source is unavailable for this account.",
    "failed": "The member snapshot could not be verified or saved. Previous snapshots are preserved.",
    "busy": "Another member snapshot is being saved. Try again shortly.",
}


def roster_capture_feedback(value):
    return _FEEDBACK.get(value, "")


def register_wechat_group_roster_routes(blueprint, local_store_root):
    @blueprint.post("/browser/wechat/group-roster")
    def wechat_group_roster_capture():
        if not is_loopback_address(request.remote_addr):
            abort(403)
        origin = request.headers.get("Origin")
        if (
            request.headers.get("Sec-Fetch-Site") == "cross-site"
            or origin is not None and (
                urlsplit(origin).scheme != request.scheme
                or urlsplit(origin).netloc != request.host
            )
        ):
            abort(403)
        if request.content_length is not None and request.content_length > 4096:
            abort(413)
        if (
            set(request.form) - {"session_id", "page"}
            or len(request.form.getlist("session_id")) != 1
            or len(request.form.getlist("page")) > 1
        ):
            abort(400)
        session = request.form.get("session_id", "")
        page = request.form.get("page", "1")
        if _SESSION.fullmatch(session) is None or re.fullmatch(r"[0-9]{1,7}", page) is None:
            abort(400)
        try:
            scope = resolve_wechat_refresh_scope(local_store_root, session)
        except (OSError, ValueError, sqlite3.DatabaseError):
            abort(404)
        if scope.kind != "group":
            abort(400)
        result, snapshot_id = "busy", ""
        if _CAPTURE_LOCK.acquire(blocking=False):
            try:
                from app.core.storage import (
                    capture_group_roster, roster_source_available,
                )

                if not roster_source_available(scope.account_id):
                    result = "unavailable"
                else:
                    snapshot = capture_group_roster(scope.account_id, scope.peer_id)
                    if (
                        not isinstance(snapshot, dict)
                        or snapshot.get("account_id") != scope.account_id
                        or snapshot.get("peer_id") != scope.peer_id
                    ):
                        raise ValueError("The member snapshot crossed its selected scope.")
                    snapshot_id = WechatGroupRosterRepository(local_store_root).save(snapshot)
                    result = "saved"
            except (OSError, ValueError, RuntimeError, sqlite3.DatabaseError):
                result = "failed"
            finally:
                _CAPTURE_LOCK.release()
        return redirect(url_for(
            "local_resources.browser", view="text", source="wechat",
            account="account:" + scope.account_id, chat_kind="group",
            session=session, page=max(1, int(page)), sort="newest",
            roster_result=result, roster_snapshot=snapshot_id or None,
            _anchor="wechat_group_roster",
        ), code=303)
