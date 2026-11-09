"""Private, bounded WeChat exchange downloads and reviewed local imports.

Code version: v1.1.0-codex.0
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import secrets
import shutil
import tempfile
import threading
import time
from pathlib import Path

from flask import jsonify, request, send_file, url_for
from werkzeug.exceptions import RequestEntityTooLarge


MAX_UPLOAD_BYTES = 256 * 1024**2
PREVIEW_LIFETIME_SECONDS = 30 * 60
MAX_PENDING_PREVIEWS = 4
_RESERVE_BYTES = 256 * 1024**2


@dataclass
class _Pending:
    """Keep an opaque review capability separate from user-supplied filenames."""

    directory: tempfile.TemporaryDirectory
    path: Path
    expires: float
    state: object


@dataclass
class _Export:
    directory: tempfile.TemporaryDirectory
    path: Path
    expires: float
    session_id: str
    status: str = "preparing"
    error: str = ""


def _summary(verified, report):
    """Expose counts and external account claims without accepting UI markup."""
    manifest = verified.manifest
    counts = manifest.get("counts", {})
    result = {
        "package_id": verified.package_id,
        "format_version": manifest.get("format_version", 1),
        "counts": {
            name: int(counts.get(name, 0))
            for name in (
                "accounts",
                "sources",
                "conversations",
                "messages",
                "resources",
            )
        },
        "accounts": manifest.get("accounts", []),
        "warnings": [
            "Imported account identities and contact names stay separate from local records.",
            "Media references are included; media files are not included.",
        ],
        "added_messages": int(report.get("inserted_messages", 0)),
        "duplicate_messages": int(report.get("replayed_messages", 0)),
        "conflicting_messages": int(report.get("conflicting_messages", 0)),
    }
    result["status"] = "duplicate" if report.get("replayed") else "ready"
    dependencies = int(counts.get("dependency_conversations", 0))
    result["counts"]["dependency_conversations"] = dependencies
    if dependencies:
        result["warnings"].append(
            f"{dependencies:,} additional conversation(s) are retained as quoted or forwarded source dependencies."
        )
    return result


def register_wechat_exchange_routes(blueprint, context):
    """Reuse the application's network, access-session, and CSRF boundaries."""
    pending = {}
    exports = {}
    inflight = 0
    lock = threading.RLock()
    root = context.media_catalog.local_store_root

    def discard(token):
        item = pending.pop(token, None)
        if item is not None:
            item.directory.cleanup()

    def expire():
        for token, item in tuple(pending.items()):
            if item.expires <= time.monotonic():
                discard(token)

    def expire_token(token):
        with lock:
            item = pending.get(token)
            if item is not None and item.expires <= time.monotonic():
                discard(token)

    def expire_export(token):
        with lock:
            item = exports.get(token)
            if (
                item is not None
                and item.expires <= time.monotonic()
                and item.status != "preparing"
            ):
                exports.pop(token)
                item.directory.cleanup()

    def prepare_export(token, item, account, kind):
        from app.core.wechat_exchange import export_conversation

        try:
            if (
                shutil.disk_usage(item.directory.name).free
                < MAX_UPLOAD_BYTES + _RESERVE_BYTES
            ):
                raise OSError("Insufficient temporary storage")
            export_conversation(
                root, item.session_id, item.path, account=account, kind=kind
            )
            with lock:
                item.status = "ready"
        except Exception:
            with lock:
                item.status = "failed"
                item.error = "The dataset could not be prepared. Its source may have changed or exceeded the supported storage budget."
            item.directory.cleanup()
        finally:
            with lock:
                item.expires = time.monotonic() + PREVIEW_LIFETIME_SECONDS
            timer = threading.Timer(PREVIEW_LIFETIME_SECONDS, expire_export, (token,))
            timer.daemon = True
            timer.start()

    @blueprint.post("/api/browser/wechat/exchange/export")
    def browser_wechat_exchange_export_start():
        request.max_content_length = 8192
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Choose a conversation to export."}), 400
        session = payload.get("session_id")
        account, kind = payload.get("account", ""), payload.get("chat_kind", "all")
        if (
            not isinstance(session, str)
            or re.fullmatch(r"wechat:[0-9a-f]{64}", session) is None
            or not isinstance(account, str)
            or len(account) > 512
            or not isinstance(kind, str)
            or kind not in {"all", "direct", "group", "unclassified"}
        ):
            return jsonify({"error": "Choose a valid WeChat conversation scope."}), 400
        with lock:
            for token in tuple(exports):
                expire_export(token)
                previous = exports.get(token)
                if previous is not None and previous.status == "failed":
                    exports.pop(token)
                    previous.directory.cleanup()
            if len(exports) >= 2 or any(
                item.status == "preparing" for item in exports.values()
            ):
                return jsonify(
                    {
                        "error": "A dataset is already being prepared. Wait for it to finish."
                    }
                ), 409
            directory = tempfile.TemporaryDirectory(prefix="wechat-exchange-export-")
            token = secrets.token_urlsafe(32)
            item = _Export(
                directory,
                Path(directory.name) / "conversation.wechatx",
                float("inf"),
                session,
            )
            exports[token] = item
            worker = threading.Thread(
                target=prepare_export, args=(token, item, account, kind), daemon=True
            )
            worker.start()
        response = jsonify({"token": token, "status": "preparing"})
        response.cache_control.no_store = True
        return response, 202

    @blueprint.get("/api/browser/wechat/exchange/export/<token>")
    def browser_wechat_exchange_export_status(token):
        expire_export(token)
        with lock:
            item = exports.get(token)
            if item is None:
                return jsonify(
                    {
                        "error": "The export expired or was interrupted. Prepare it again."
                    }
                ), 404
            result = {"status": item.status}
            if item.status == "ready":
                result["download_url"] = url_for(
                    "local_resources.browser_wechat_exchange_download", token=token
                )
            elif item.status == "failed":
                result["error"] = item.error
        response = jsonify(result)
        response.cache_control.no_store = True
        return response

    @blueprint.get("/browser/wechat/exchange/download/<token>")
    def browser_wechat_exchange_download(token):
        expire_export(token)
        with lock:
            item = exports.get(token)
            if item is None or item.status != "ready":
                return jsonify(
                    {"error": "This dataset is not ready or has expired."}
                ), 404
            response = send_file(
                item.path,
                mimetype="application/vnd.agenticcontext.wechat-exchange",
                as_attachment=True,
                download_name=f"wechat-conversation-{item.session_id[-12:]}.wechatx",
                conditional=False,
                etag=False,
                max_age=0,
            )
            exports.pop(token)
            response.direct_passthrough = False
            response.call_on_close(item.directory.cleanup)
        response.cache_control.no_store = True
        return response

    @blueprint.get("/browser/wechat/session/<session_id>/exchange")
    def browser_wechat_session_exchange(session_id):
        from app.core.wechat_exchange import export_conversation

        directory = tempfile.TemporaryDirectory(prefix="wechat-exchange-export-")
        path = Path(directory.name) / "conversation.wechatx"
        try:
            export_conversation(
                root,
                session_id,
                path,
                account=request.args.get("account", ""),
                kind=request.args.get("chat_kind", "all"),
            )
            response = send_file(
                path,
                mimetype="application/vnd.agenticcontext.wechat-exchange",
                as_attachment=True,
                download_name=f"wechat-conversation-{session_id[-12:]}.wechatx",
                conditional=False,
                etag=False,
                max_age=0,
            )
            response.cache_control.no_store = True
            # Disable passthrough so the WSGI close callback owns temporary files.
            response.direct_passthrough = False
            response.call_on_close(directory.cleanup)
            return response
        except (ValueError, LookupError):
            directory.cleanup()
            return jsonify(
                {"error": "This conversation cannot be exported in the selected scope."}
            ), 400
        except (OSError, RuntimeError):
            directory.cleanup()
            return jsonify(
                {
                    "error": "The archive is changing or export storage is unavailable. Try again shortly."
                }
            ), 503

    @blueprint.post("/api/browser/wechat/exchange/preview")
    def browser_wechat_exchange_preview():
        nonlocal inflight

        from app.core.wechat_exchange import read_package
        from app.core.wechat_exchange_import import archive_state, preview_package

        request.max_content_length = MAX_UPLOAD_BYTES + 1024**2
        directory = None
        reserved = False
        try:
            with lock:
                expire()
                if len(pending) + inflight >= MAX_PENDING_PREVIEWS:
                    return jsonify(
                        {
                            "error": "Close an existing import preview before selecting another package."
                        }
                    ), 409
                inflight += 1
                reserved = True
            upload = request.files.get("file")
            if upload is None or not upload.filename:
                return jsonify(
                    {"error": "Choose a WeChat dataset exchange package."}
                ), 400
            directory = tempfile.TemporaryDirectory(prefix="wechat-exchange-import-")
            path = Path(directory.name) / "package.wechatx"
            if (
                shutil.disk_usage(directory.name).free
                < MAX_UPLOAD_BYTES + _RESERVE_BYTES
            ):
                raise OSError("Insufficient temporary storage")
            size = 0
            with path.open("xb") as output:
                while chunk := upload.stream.read(1024**2):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise RequestEntityTooLarge()
                    output.write(chunk)
            verified = read_package(path)
            state = archive_state(root)
            report = preview_package(root, verified)
            if archive_state(root) != state:
                return jsonify(
                    {
                        "error": "Local records changed during preview. Select the package again."
                    }
                ), 409
            response = _summary(verified, report)
            with lock:
                expire()
                if len(pending) >= MAX_PENDING_PREVIEWS:
                    return jsonify(
                        {
                            "error": "Too many active previews. Close an import preview and try again."
                        }
                    ), 409
                token = secrets.token_urlsafe(32)
                pending[token] = _Pending(
                    directory, path, time.monotonic() + PREVIEW_LIFETIME_SECONDS, state
                )
                inflight -= 1
                reserved = False
                directory = None
                timer = threading.Timer(
                    PREVIEW_LIFETIME_SECONDS, expire_token, (token,)
                )
                timer.daemon = True
                timer.start()
            response["token"] = token
            return jsonify(response)
        except RequestEntityTooLarge:
            return jsonify(
                {"error": "The exchange package exceeds the 256 MiB upload limit."}
            ), 413
        except (ValueError, LookupError):
            return jsonify(
                {
                    "error": "The exchange package is invalid, incomplete, or unsupported."
                }
            ), 400
        except (OSError, RuntimeError):
            return jsonify(
                {
                    "error": "The archive is busy or temporary storage is insufficient. Try again shortly."
                }
            ), 503
        finally:
            if reserved:
                with lock:
                    inflight -= 1
            if directory is not None:
                directory.cleanup()

    @blueprint.post("/api/browser/wechat/exchange/cancel")
    def browser_wechat_exchange_cancel():
        payload = request.get_json(silent=True) or {}
        token = payload.get("token") if isinstance(payload, dict) else None
        with lock:
            expire()
            if isinstance(token, str):
                discard(token)
        return jsonify({"status": "cancelled"})

    @blueprint.post("/api/browser/wechat/exchange/import")
    def browser_wechat_exchange_import():
        from app.core.wechat_exchange import read_package
        from app.core.wechat_exchange_import import import_package

        payload = request.get_json(silent=True) or {}
        token = payload.get("token") if isinstance(payload, dict) else None
        with lock:
            expire()
            item = pending.pop(token, None) if isinstance(token, str) else None
        if item is None:
            return jsonify(
                {"error": "This import preview expired. Select the package again."}
            ), 409
        try:
            verified = read_package(item.path)
            report = import_package(root, verified, expected_state=item.state)
            response = _summary(verified, report)
            response["status"] = "duplicate" if report.get("replayed") else "imported"
            response["redirect_url"] = url_for(
                "local_resources.browser",
                view="text",
                source="wechat",
                session_view="1",
            )
            return jsonify(response)
        except (ValueError, LookupError):
            return jsonify(
                {
                    "error": "The package or local records changed after preview. Select the package again."
                }
            ), 409
        except (OSError, RuntimeError):
            return jsonify(
                {
                    "error": "The archive is busy or import storage is insufficient. Select the package again."
                }
            ), 503
        finally:
            item.directory.cleanup()
