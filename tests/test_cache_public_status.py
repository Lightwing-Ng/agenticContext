"""Retain exact user actions while keeping provider diagnostics private.

Code version: v1.0.0-codex.0
"""

from app.web.cache_routes import _public_cache_snapshot


ACTION = (
    "Zhihu requires human verification. Open Zhihu in the selected browser, "
    "complete verification manually, then retry."
)


def test_exact_verification_action_survives_the_public_status_boundary():
    snapshot = {"phase": "failed", "last_error": ACTION, "message": ACTION}
    public = _public_cache_snapshot(snapshot)
    assert public["last_error"] == public["message"] == ACTION
    assert public["recent_events"] == [ACTION]


def test_verification_action_with_provider_diagnostics_remains_private():
    private = ACTION + " Provider diagnostic: /private/synthetic-token"
    snapshot = {
        "phase": "failed", "last_error": private, "message": private,
        "recent_events": [private],
    }
    public = _public_cache_snapshot(snapshot)
    assert public["last_error"] == public["message"] == "The cache task reported an error. Please try again."
    assert public["recent_events"] == [public["message"]]
    assert snapshot["last_error"] == private
