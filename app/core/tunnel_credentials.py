"""Local storage for the OpenAI Secure MCP Tunnel credential pair.

Code version: v1.0.0-codex.0

The Tunnel connection lets ChatGPT reach this computer through an OpenAI
platform Tunnel. It needs a Tunnel ID (``tunnel_...``) and an API key that is
authorized for that Tunnel. The pair lives in its own owner-only file next to
the regular settings so the unencrypted API key never enters ``settings.json``
and is never rendered back into the UI.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from app.core.config import default_settings_path

TUNNEL_CREDENTIALS_FILENAME = "tunnel-credentials.json"
TUNNEL_SUPPORTED_PLATFORMS = frozenset({"chatgpt"})


@dataclass(frozen=True, slots=True)
class TunnelCredentials:
    """One saved Tunnel ID and API key pair."""

    tunnel_id: str = ""
    api_key: str = ""

    @property
    def configured(self) -> bool:
        """Return whether both credentials are present."""
        return bool(self.tunnel_id and self.api_key)

    def snapshot(self) -> dict[str, object]:
        """Return a UI-safe view that never exposes the API key."""
        return {
            "configured": self.configured,
            "tunnel_id": self.tunnel_id,
            "api_key_saved": bool(self.api_key),
            "api_key_hint": f"…{self.api_key[-4:]}" if len(self.api_key) >= 8 else "",
        }


def default_tunnel_credentials_path() -> Path:
    """Return the credentials file beside the current settings file."""
    return default_settings_path().parent / TUNNEL_CREDENTIALS_FILENAME


def load_tunnel_credentials(path: Path | None = None) -> TunnelCredentials:
    """Load the saved pair, or an empty pair when none is readable."""
    resolved_path = path if path is not None else default_tunnel_credentials_path()
    try:
        payload = json.loads(resolved_path.read_text())
    except (OSError, json.JSONDecodeError):
        return TunnelCredentials()
    if not isinstance(payload, dict):
        return TunnelCredentials()
    return TunnelCredentials(
        tunnel_id=str(payload.get("tunnel_id") or "").strip(),
        api_key=str(payload.get("api_key") or "").strip(),
    )


def save_tunnel_credentials(
    credentials: TunnelCredentials,
    path: Path | None = None,
) -> None:
    """Atomically persist the pair with owner-only permissions."""
    resolved_path = path if path is not None else default_tunnel_credentials_path()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = resolved_path.with_name(f".{resolved_path.name}.tmp")
    payload = {"tunnel_id": credentials.tunnel_id, "api_key": credentials.api_key}
    file_descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(file_descriptor, "w") as handle:
        json.dump(payload, handle, indent=2)
    os.replace(temporary_path, resolved_path)


def merge_tunnel_credentials(
    current: TunnelCredentials,
    tunnel_id: str | None,
    api_key: str | None,
) -> TunnelCredentials:
    """Apply one Settings submission.

    A blank API key keeps the saved key, because the UI never receives it.
    Clearing the Tunnel ID clears the whole pair, since a key alone is unusable.
    """
    next_tunnel_id = current.tunnel_id if tunnel_id is None else tunnel_id.strip()
    submitted_key = (api_key or "").strip()
    next_api_key = submitted_key or current.api_key
    if not next_tunnel_id:
        next_api_key = ""
    return TunnelCredentials(tunnel_id=next_tunnel_id, api_key=next_api_key)
