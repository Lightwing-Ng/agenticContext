"""Project local viewing names without changing WeChat identity or source evidence.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import stat

from .wechat_contacts import canonical_contact_id


MAX_PREFERENCES_BYTES = 64 * 1024
MAX_DISPLAY_NAME_BYTES = 512
_CONTACT_ID = re.compile(r"wechat:contact:[0-9a-f]{64}")


def display_names_path(local_store_root: Path | str) -> Path:
    return (
        Path(local_store_root).expanduser()
        / "messaging"
        / "wechat"
        / "display_names.json"
    )


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate display-name preference key.")
        result[key] = value
    return result


def _read_names(path: Path) -> dict[str, str]:
    try:
        if path.parent.is_symlink() or path.parent.parent.is_symlink():
            return {}
        if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
            return {}
        with path.open("rb") as stream:
            raw = stream.read(MAX_PREFERENCES_BYTES + 1)
        if len(raw) > MAX_PREFERENCES_BYTES:
            return {}
        preferences = json.loads(raw, object_pairs_hook=_unique_object)
        if (
            not isinstance(preferences, dict)
            or type(preferences.get("schema_version")) is not int
            or preferences["schema_version"] != 1
            or not isinstance(preferences.get("contacts"), dict)
        ):
            return {}
        names = preferences["contacts"]
        for identifier, name in names.items():
            if (
                _CONTACT_ID.fullmatch(identifier) is None
                or not isinstance(name, str)
                or not name
                or name != name.strip()
                or len(name.encode("utf-8")) > MAX_DISPLAY_NAME_BYTES
                or any(
                    ord(character) < 32
                    or ord(character) == 127
                    or character in "\u2028\u2029"
                    for character in name
                )
            ):
                return {}
        return names
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return {}


class WechatDisplayNames:
    """Read the small private preference file once for each page projection."""

    def __init__(self, local_store_root: Path | str):
        self.names = _read_names(display_names_path(local_store_root))

    def contact_name(self, contact: dict) -> str:
        identifier = contact.get("id")
        if (
            contact.get("platform") != "wechat"
            or contact.get("identity_status") != "verified"
            or not isinstance(identifier, str)
            or _CONTACT_ID.fullmatch(identifier) is None
        ):
            return ""
        return self.names.get(identifier, "")

    def account_label(self, row: dict) -> str:
        owner = row.get("account_id")
        account_key = row.get("account_key", row.get("key"))
        partition = row.get("partition_id", row.get("id"))
        if (
            not isinstance(owner, str)
            or not owner
            or account_key != f"account:{owner}"
            or partition != "ios-" + hashlib.md5(owner.encode()).hexdigest()
            or row.get("identity_status") != "Verified archive ID"
        ):
            return ""
        try:
            identifier = canonical_contact_id(owner, authority="native_account_owner")
        except ValueError:
            return ""
        name = self.names.get(identifier, "")
        return f"{name} · {owner[-6:]}" if name else ""
