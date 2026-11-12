"""Expose only opaque read-page task state, never records or source paths.

Code version: v1.0.0-codex.0
"""

import re

from flask import current_app, jsonify, url_for


WECHAT_READ_EXTENSION = "wechat_browser_read"
_TOKEN = re.compile(r"[A-Za-z0-9_-]{24,128}")


def read_job_payload(public):
    token = public.get("task_id", "")
    payload = {key: public[key] for key in ("task_id", "generation", "state", "message")}
    payload["status_url"] = url_for("local_resources.browser_wechat_read_status", token=token)
    payload["retry_url"] = payload["status_url"]
    return payload


def register_wechat_read_routes(blueprint):
    @blueprint.route("/api/browser/wechat/read/<token>", methods=["GET", "POST"])
    def browser_wechat_read_status(token):
        from flask import request

        service = current_app.extensions[WECHAT_READ_EXTENSION]
        value = None
        if _TOKEN.fullmatch(token):
            if request.method == "POST":
                try:
                    value = service.retry(token)
                except (LookupError, OSError, ValueError, RuntimeError):
                    pass
            else:
                value = service.status(token)
        if value is None:
            response = jsonify({"error": "This saved-view task has expired. Load the view again."})
            response.status_code = 404
        else:
            response = jsonify(read_job_payload(value))
        response.cache_control.no_store = True
        return response
