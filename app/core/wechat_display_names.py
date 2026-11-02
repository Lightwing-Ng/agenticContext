"""Project local viewing names without changing WeChat identity or source evidence.

Code version: v1.5.0-codex.0
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import stat

from .wechat_contacts import canonical_contact_id, classify_wechat_identifier


MAX_PREFERENCES_BYTES = 64 * 1024
MAX_DISPLAY_NAME_BYTES = 512
MAX_HIDDEN_ACCOUNT_SOURCES = 128
_CONTACT_ID = re.compile(r"wechat:contact:[0-9a-f]{64}")
_SOURCE_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")


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


def _read_preferences(path: Path) -> dict:
    try:
        if path.parent.is_symlink() or path.parent.parent.is_symlink():
            return {}
        state = path.stat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(state.st_mode)
            or getattr(state, "st_flags", 0) & 0x40000000
        ):
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
        return preferences
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return {}


class WechatDisplayNames:
    """Read the small private preference file once for each page projection."""

    def __init__(self, local_store_root: Path | str):
        preferences = _read_preferences(display_names_path(local_store_root))
        self.names = preferences.get("contacts", {})
        hidden = preferences.get("hidden_account_sources", [])
        self.hidden_account_sources = (
            frozenset(hidden)
            if isinstance(hidden, list)
            and len(hidden) <= MAX_HIDDEN_ACCOUNT_SOURCES
            and all(
                isinstance(identifier, str) and _SOURCE_ID.fullmatch(identifier)
                for identifier in hidden
            )
            else frozenset()
        )

    def account_option_hidden(self, row: dict) -> bool:
        """Hide exact source partitions only from account navigation options."""
        partition = row.get("partition_id", row.get("id"))
        return isinstance(partition, str) and partition in self.hidden_account_sources

    def project_accounts(self, account_rows):
        """Share account labels and proven owner grouping across local navigation."""
        projected = tuple(
            {
                **row,
                "viewer_name": self.account_label(row, verified_accounts=account_rows),
            }
            for row in account_rows
        )
        options = []
        positions = {}
        for row in projected:
            if self.account_option_hidden(row):
                continue
            key = row["key"]
            if not self.account_option_owner(row, verified_accounts=account_rows):
                options.append(row)
            elif key not in positions:
                positions[key] = len(options)
                options.append(row)
            elif self.account_owner(row):
                options[positions[key]] = row
        return projected, tuple(options)

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

    def account_owner(self, row: dict, *, verified_accounts=()) -> str:
        """Prove an exact native owner independently of viewing-name preferences."""
        owner = row.get("account_id")
        account_key = row.get("account_key", row.get("key"))
        partition = row.get("partition_id", row.get("id"))
        if not isinstance(owner, str) or not owner or account_key != f"account:{owner}":
            return ""
        if (
            row.get("identity_status") == "Corroborated export ID"
            and isinstance(partition, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", partition)
            and not partition.startswith("ios-")
        ):
            if not isinstance(verified_accounts, (tuple, list)):
                return ""
            for candidate in verified_accounts:
                if (
                    not isinstance(candidate, dict)
                    or candidate.get("account_id") != owner
                ):
                    continue
                # The default call accepts only the original verified iOS gate.
                if self.account_owner(candidate) == owner:
                    return owner
            return ""
        if (
            partition != "ios-" + hashlib.md5(owner.encode()).hexdigest()
            or row.get("identity_status") != "Verified archive ID"
        ):
            return ""
        try:
            canonical_contact_id(owner, authority="native_account_owner")
        except ValueError:
            return ""
        return owner

    def account_option_owner(self, row: dict, *, verified_accounts=()) -> str:
        """Group proven export owners without granting native-only viewing aliases."""
        native_owner = self.account_owner(row, verified_accounts=verified_accounts)
        if native_owner:
            return native_owner
        owner = row.get("account_id")
        key = row.get("account_key", row.get("key"))
        partition = row.get("partition_id", row.get("id"))
        if (
            not isinstance(owner, str)
            or key != f"account:{owner}"
            or row.get("identity_status") != "Corroborated export ID"
            or not isinstance(partition, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,100}", partition) is None
            or partition.startswith("ios-")
            or classify_wechat_identifier(owner)["kind"]
            not in {"wechat_account", "wechat_identity"}
        ):
            return ""
        try:
            canonical_contact_id(owner, authority="corroborated_export_username")
        except ValueError:
            return ""
        return owner

    def account_label(self, row: dict, *, verified_accounts=()) -> str:
        owner = self.account_owner(row, verified_accounts=verified_accounts)
        if not owner:
            return ""
        identifier = canonical_contact_id(owner, authority="native_account_owner")
        name = self.names.get(identifier, "")
        return f"{name} · {owner[-6:]}" if name else ""
