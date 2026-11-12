"""Expose account-scoped local WeChat refresh jobs without exposing credentials.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

import re

from flask import current_app, jsonify, request, url_for

from app.core.agent import is_loopback_address
from app.core.storage import classify_wechat_identifier


WECHAT_REFRESH_EXTENSION = "wechat_mac_refresh"
_SESSION = re.compile(r"wechat:[0-9a-f]{64}")
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


def register_wechat_refresh_routes(blueprint):
    """Keep refresh execution and its opaque job status on this computer."""

    @blueprint.post("/api/browser/wechat/refresh")
    def browser_wechat_refresh():
        if not is_loopback_address(request.remote_addr):
            return _response(
                {"error": "Chat history can only be refreshed on this computer."}, 403
            )
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
        try:
            started = service.start(payload["session_id"])
        except LookupError:
            return _response({"error": "This conversation is unavailable."}, 404)
        except (ValueError, PermissionError, RuntimeError):
            return _response(
                {
                    "error": "This conversation cannot be refreshed right now. Check its local database key configuration and try again."
                },
                409,
            )
        except OSError:
            return _response(
                {
                    "error": "Local source records are updating. Please try again shortly."
                },
                503,
            )
        token = started.get("task_id") if isinstance(started, dict) else None
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            return _response({"error": "The refresh could not be started."}, 503)
        return _response(
            {
                "state": "running",
                "status_url": url_for(
                    "local_resources.browser_wechat_refresh_status", token=token
                ),
            },
            202,
        )

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
                {"error": "This refresh is unavailable or has expired."}, 404
            )
        if not isinstance(result, dict) or result.get("state") not in {
            "running",
            "succeeded",
            "failed",
        }:
            return _response(
                {"error": "Refresh status is temporarily unavailable."}, 503
            )
        # Only bounded presentation fields cross the HTTP boundary.
        payload = {"state": result["state"], "partial": bool(result.get("partial"))}
        if isinstance(result.get("message"), str):
            payload["message"] = result["message"][:600]
        for field in ("added", "total"):
            value = result.get(field)
            if type(value) is int and value >= 0:
                payload[field] = value
        return _response(payload)
