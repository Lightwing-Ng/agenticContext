"""Expose account-scoped local WeChat refresh jobs without exposing credentials.

Code version: v1.3.0-codex.0
"""

from __future__ import annotations

import re

from flask import current_app, jsonify, request, url_for

from app.core.agent import is_loopback_address
from app.core.storage import (
    WECHAT_REFRESH_BUSY_MESSAGE as BUSY_MESSAGE,
    WECHAT_REFRESH_MAX_COUNTER as MAX_COUNTER,
    GlobalRefreshBusy,
    RefreshNotice,
    classify_wechat_identifier,
    safe_wechat_refresh_progress as safe_progress,
)
from app.web.wechat_read_routes import WECHAT_SUMMARY_EXTENSION


WECHAT_REFRESH_EXTENSION = "wechat_mac_refresh"
_SESSION = re.compile(r"wechat:[0-9a-f]{64}")
_ACCOUNT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,151}")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{24,128}")


def conversation_refresh_available(service, messaging) -> bool:
    """Resolve eligibility from canonical archive metadata and private host state."""
    if not isinstance(messaging, dict):
        return False
    account = messaging.get("account_id")
    peer = messaging.get("peer_id")
    kind = messaging.get("kind")
    if not account or not peer or kind not in {"direct", "group"}:
        return False
    if classify_wechat_identifier(peer)["kind"] == "official_account":
        return False
    try:
        return bool(service.available(account, peer, kind))
    except (OSError, ValueError, RuntimeError):
        return False


def _response(payload, status=200):
    response = jsonify(payload)
    response.status_code = status
    response.cache_control.no_store = True
    return response


def _start_refresh(start, identifier, scope):
    try:
        started = start(identifier)
    except GlobalRefreshBusy:
        return _response({"error": BUSY_MESSAGE}, 409)
    except RefreshNotice as exc:
        return _response({"error": exc.issue.value}, 409)
    except LookupError:
        return _response({"error": f"This {scope} is unavailable."}, 404)
    except (ValueError, PermissionError, RuntimeError):
        return _response({"error": (
            f"The local service could not start this {scope}'s refresh. Try again shortly."
        )}, 409)
    except OSError:
        return _response({"error": "Local source records are updating. Please try again shortly."}, 503)
    token = started.get("task_id") if isinstance(started, dict) else None
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        return _response({"error": "The refresh could not be started."}, 503)
    return _response(
        {"state": "running", "status_url": url_for(
            "local_resources.browser_wechat_refresh_status", token=token,
        )},
        202,
    )


def selected_refresh_account(account_key, messaging):
    """Accept only a selected native account offered by the archive navigation."""
    if not isinstance(account_key, str) or not account_key.startswith("account:"):
        return ""
    account_id = account_key.removeprefix("account:")
    if not _ACCOUNT.fullmatch(account_id) or not isinstance(messaging, dict):
        return ""
    return account_id if any(
        isinstance(account, dict) and account.get("key") == account_key
        for account in messaging.get("accounts", ())
    ) else ""


def register_wechat_refresh_routes(blueprint):
    """Keep refresh execution and its opaque job status on this computer."""

    @blueprint.post("/api/browser/wechat/refresh")
    def browser_wechat_refresh():
        if not is_loopback_address(request.remote_addr):
            return _response(
                {"error": "Chat history can only be refreshed on this computer."}, 403
            )
        origin = request.headers.get("Origin")
        if (
            request.headers.get("Sec-Fetch-Site") == "cross-site"
            or origin is not None and origin != f"{request.scheme}://{request.host}"
        ):
            return _response({"error": "Refresh chat history from this page."}, 403)
        if request.content_length is not None and request.content_length > 4_096:
            return _response({"error": "Choose one conversation to refresh."}, 413)
        payload = request.get_json(silent=True)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"session_id"}
            or not isinstance(payload["session_id"], str)
            or not _SESSION.fullmatch(payload["session_id"])
        ):
            return _response({"error": "Choose one conversation to refresh."}, 400)
        service = current_app.extensions[WECHAT_REFRESH_EXTENSION]
        return _start_refresh(service.start, payload["session_id"], "conversation")

    @blueprint.post("/api/browser/wechat/refresh/account")
    def browser_wechat_account_refresh():
        if not is_loopback_address(request.remote_addr):
            return _response({"error": "Chat history can only be refreshed on this computer."}, 403)
        choose_account = {"error": "Choose one WeChat account to refresh."}
        if request.content_length is not None and request.content_length > 4_096:
            return _response(choose_account, 413)
        payload = request.get_json(silent=True)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"account_id"}
            or not isinstance(payload["account_id"], str)
            or not _ACCOUNT.fullmatch(payload["account_id"])
        ):
            return _response(choose_account, 400)
        service = current_app.extensions[WECHAT_REFRESH_EXTENSION]
        return _start_refresh(service.start_account, payload["account_id"], "WeChat account")

    @blueprint.get("/api/browser/wechat/refresh/<token>")
    def browser_wechat_refresh_status(token):
        if not is_loopback_address(request.remote_addr):
            return _response(
                {"error": "Chat history can only be refreshed on this computer."}, 403
            )
        if not _TOKEN.fullmatch(token):
            return _response(
                {"error": "This refresh is unavailable or has expired."}, 404
            )
        result = current_app.extensions[WECHAT_REFRESH_EXTENSION].status(token)
        if result is None:
            return _response(
                {"error": "This refresh is unavailable or has expired."},
                404,
            )
        if not isinstance(result, dict) or result.get("state") not in {
            "running",
            "succeeded",
            "failed",
            "interrupted",
        }:
            return _response(
                {"error": "Refresh status is temporarily unavailable."}, 503
            )
        # Only bounded presentation fields cross the HTTP boundary.
        payload = {"state": result["state"], "partial": bool(result.get("partial"))}
        if result["state"] == "interrupted":
            payload["publication_unknown"] = result.get("publication_unknown") is True
        if isinstance(result.get("message"), str):
            payload["message"] = result["message"][:600]
        for field in ("added", "total"):
            value = result.get(field)
            if type(value) is int and 0 <= value <= MAX_COUNTER:
                payload[field] = value
        try:
            payload.update(safe_progress(result))
        except ValueError:
            return _response(
                {"error": "Refresh status is temporarily unavailable."}, 503
            )
        if result["state"] == "succeeded":
            summary = current_app.extensions.get(WECHAT_SUMMARY_EXTENSION)
            if summary is not None:
                summary.wake()
        return _response(payload)
