"""Local storage for the OpenAI Secure MCP Tunnel credential pair.

Code version: v1.2.0-claude.0

The Tunnel connection lets ChatGPT reach this computer through an OpenAI
platform Tunnel. It needs a Tunnel ID (``tunnel_...``) and an API key that is
authorized for that Tunnel. The pair lives in its own owner-only file next to
the regular settings so the unencrypted API key never enters ``settings.json``
and is never rendered back into the UI. The file uses the same owner-only
boundary on every host: mode ``0600`` on POSIX and a protected current-user DACL
on Windows (see ``owner_only_files``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from app.core.config import default_settings_path
from app.core.owner_only_files import (
    ensure_owner_only,
    owner_only_problem,
    write_owner_only_text,
)

LOGGER = logging.getLogger(__name__)

TUNNEL_CREDENTIALS_FILENAME = "tunnel-credentials.json"
TUNNEL_SUPPORTED_PLATFORMS = frozenset({"chatgpt", "gemini"})


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
    _narrow_legacy_file(resolved_path)
    return TunnelCredentials(
        tunnel_id=str(payload.get("tunnel_id") or "").strip(),
        api_key=str(payload.get("api_key") or "").strip(),
    )


def _narrow_legacy_file(path: Path) -> None:
    """Bring a file saved before the shared boundary under it, without failing a read."""
    if not owner_only_problem(path):
        return
    try:
        ensure_owner_only(path)
    except OSError as exc:
        LOGGER.warning("Tunnel credentials are not owner-only: %s", exc)


def save_tunnel_credentials(
    credentials: TunnelCredentials,
    path: Path | None = None,
) -> None:
    """Atomically persist the pair; raise ``OSError`` rather than widen access."""
    resolved_path = path if path is not None else default_tunnel_credentials_path()
    payload = {"tunnel_id": credentials.tunnel_id, "api_key": credentials.api_key}
    write_owner_only_text(resolved_path, json.dumps(payload, indent=2))


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
