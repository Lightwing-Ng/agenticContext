"""Resolve preserved resource metadata to account-scoped Mac Finder destinations.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import hashlib
from itertools import islice
import math
import os
from pathlib import Path
import re
import stat
from typing import Any


_OWNER = re.compile(r"wxid_[A-Za-z0-9_-]{1,100}")
_CHECKSUM = re.compile(r"[0-9a-fA-F]{32}")
_ACCOUNT_LIMIT = 256
_FILE_LIMIT = 2_048
_DISPLAY_ZONE = timezone(timedelta(hours=8))


def _dataless(state) -> bool:
    return bool(
        getattr(state, "st_flags", 0) & getattr(stat, "UF_DATALESS", 0x40000000)
    )


def _md5_identifier(value: str) -> str:
    """Match WeChat's native locator format, never a security identifier."""
    return hashlib.md5(value.encode(), usedforsecurity=False).hexdigest()


def _filename(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 255
        or value in {".", ".."}
        or any(character in value for character in ("/", "\\"))
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return ""
    return value


def _month(value: Any) -> str:
    if isinstance(value, bool):
        return ""
    try:
        timestamp = float(value)
        if not math.isfinite(timestamp) or not 0 <= timestamp <= 4_102_444_800:
            return ""
        return datetime.fromtimestamp(timestamp, tz=_DISPLAY_ZONE).strftime("%Y-%m")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


class WechatMacResourceResolver:
    """Use native account identities and bounded metadata inspection only.

    No media is opened, decrypted, downloaded, or recursively enumerated. Paths
    returned by ``resolve`` are private backend values and must not be serialized
    to the browser. Use one instance per page query and a fresh instance immediately
    before a Finder action; discovery is cached only for that instance.
    """

    def __init__(
        self,
        local_store_root,
        *,
        home=None,
        modern_root=None,
        legacy_root=None,
    ):
        self.local_store_root = Path(local_store_root).expanduser()
        self._account_cache = {}
        self.home = Path(home if home is not None else Path.home()).absolute()
        container = self.home / "Library/Containers/com.tencent.xinWeChat/Data"
        self.modern_root = Path(
            modern_root
            if modern_root is not None
            else container / "Documents/xwechat_files"
        ).absolute()
        self.legacy_root = Path(
            legacy_root
            if legacy_root is not None
            else container
            / "Library/Application Support/com.tencent.xinWeChat/2.0b4.0.9"
        ).absolute()

    def _metadata(self, path: Path):
        """Reject symlinks and cloud placeholders at every path component."""
        if ".." in path.parts or not path.is_relative_to(self.home):
            return None
        current = Path(path.anchor)
        try:
            for index, part in enumerate(path.parts[1:]):
                current /= part
                state = current.lstat()
                if stat.S_ISLNK(state.st_mode) or _dataless(state):
                    return None
                if index < len(path.parts) - 2 and not stat.S_ISDIR(state.st_mode):
                    return None
            return state
        except OSError:
            return None

    def _directory(self, path: Path) -> bool:
        state = self._metadata(path)
        return state is not None and stat.S_ISDIR(state.st_mode)

    def _entries(self, path: Path, limit: int):
        if not self._directory(path):
            return None
        try:
            with os.scandir(path) as iterator:
                entries = list(islice(iterator, limit + 1))
            return entries if len(entries) <= limit else None
        except OSError:
            return None

    def _account_roots(self, account_id: Any):
        if not isinstance(account_id, str) or not _OWNER.fullmatch(account_id):
            return []
        if account_id in self._account_cache:
            return [
                root
                for root in self._account_cache[account_id]
                if self._directory(root[0])
            ]
        roots = []
        entries = self._entries(self.modern_root, _ACCOUNT_LIMIT)
        expression = re.compile(re.escape(account_id) + r"_[0-9a-fA-F]{4}")
        for entry in entries or ():
            path = self.modern_root / entry.name
            if expression.fullmatch(entry.name) and self._directory(path):
                roots.append((path, "modern", "native_username_directory"))
        legacy = self.legacy_root / _md5_identifier(account_id)
        if self._directory(legacy):
            roots.append((legacy, "legacy", "legacy_directory_hash"))
        roots = sorted(set(roots), key=lambda root: (root[1] != "modern", str(root[0])))
        self._account_cache[account_id] = tuple(roots)
        return roots

    def account_mapping(self, account_id) -> dict[str, Any]:
        """Expose account evidence without revealing host filesystem locations."""
        roots = self._account_roots(account_id)
        return {
            "status": "mapped" if roots else "unmapped",
            "root_count": len(roots),
            "evidence": sorted({root[2] for root in roots}),
        }

    def _scopes(self, roots, kind: str, peer_id: Any, month: str):
        peer = (
            _md5_identifier(peer_id)
            if isinstance(peer_id, str)
            and 0 < len(peer_id) <= 256
            and "\0" not in peer_id
            else ""
        )
        scopes = []
        fallback = []
        general = []
        for root, layout, _evidence in roots:
            if layout == "modern":
                messages = root / "msg"
                if kind == "image" and peer:
                    conversation = messages / "attach" / peer
                    if month:
                        scopes.append(conversation / month / "Img")
                    fallback.extend(
                        (
                            conversation / month / "Img",
                            conversation / month,
                            conversation,
                        )
                        if month
                        else (conversation,)
                    )
                elif kind in {"video", "file"} and month:
                    scopes.append(messages / kind / month)
                    fallback.append(messages / kind / month)
                general.extend((messages, root))
            else:
                if peer:
                    conversation = root / "Message/MessageTemp" / peer
                    child = {
                        "image": "Image",
                        "video": "Video",
                        "file": "OpenData",
                    }.get(kind)
                    if child:
                        scopes.append(conversation / child)
                        fallback.append(conversation / child)
                    fallback.append(conversation)
                general.append(root)
        return scopes, [*fallback, *general]

    def _files(self, paths):
        matches = {}
        for path in paths:
            state = self._metadata(path)
            if state is not None and stat.S_ISREG(state.st_mode):
                matches[str(path)] = (path, state)
        return list(matches.values())

    @staticmethod
    def _result(status, path, label, evidence):
        return {"status": status, "path": path, "label": label, "evidence": evidence}

    def resolve(self, resource: Mapping[str, Any]) -> dict[str, Any]:
        """Resolve an archived reference, preserving uncertainty and account scope."""
        missing = self._result(
            "missing", None, "Not available on this Mac", "no_local_match"
        )
        if not isinstance(resource, Mapping):
            return missing
        roots = self._account_roots(resource.get("account_id"))
        if not roots:
            return missing
        kind = resource.get("kind")
        kind = (
            kind
            if isinstance(kind, str)
            and kind in {"image", "video", "file", "audio", "sticker", "app"}
            else "unknown"
        )
        scopes, fallback = self._scopes(
            roots, kind, resource.get("peer_id"), _month(resource.get("unix_timestamp"))
        )
        checksum = resource.get("checksum_md5")
        checksum = (
            checksum.lower()
            if isinstance(checksum, str) and _CHECKSUM.fullmatch(checksum)
            else ""
        )
        if checksum and kind in {"image", "video"}:
            suffixes = (
                (".dat", ".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif")
                if kind == "image"
                else (".mp4", ".mov", ".m4v", ".webm", ".dat")
            )
            matches = self._files(
                scope / (checksum + suffix) for scope in scopes for suffix in suffixes
            )
            if len(matches) == 1:
                thumbnail = resource.get("variant") == "thumbnail"
                return self._result(
                    "file",
                    matches[0][0],
                    "Show local thumbnail in Finder"
                    if thumbnail
                    else "Show local copy in Finder",
                    "checksum_thumbnail_reference"
                    if thumbnail
                    else "checksum_filename",
                )
            if not matches:
                thumbnails = self._files(
                    scope / (checksum + suffix)
                    for scope in scopes
                    for suffix in ("_t.dat", "_thumb.jpg")
                )
                if len(thumbnails) == 1:
                    return self._result(
                        "file",
                        thumbnails[0][0],
                        "Show local thumbnail in Finder",
                        "checksum_thumbnail_filename",
                    )
        filename = _filename(resource.get("filename"))
        size = resource.get("byte_size")
        if kind == "file" and filename and type(size) is int and size >= 0:
            name = Path(filename)
            collision = re.compile(
                re.escape(name.stem) + r"(?: ?\([0-9]+\))?" + re.escape(name.suffix)
            )
            matches = []
            complete = True
            for scope in scopes:
                entries = self._entries(scope, _FILE_LIMIT)
                if entries is None:
                    complete = False
                    continue
                for entry in entries:
                    if collision.fullmatch(entry.name):
                        for path, state in self._files((scope / entry.name,)):
                            if state.st_size == size:
                                matches.append(path)
            matches = sorted(set(matches))
            if complete and len(matches) == 1:
                return self._result(
                    "candidate",
                    matches[0],
                    "Show possible local copy in Finder",
                    "unique_filename_and_size",
                )
        for path in dict.fromkeys(fallback):
            if self._directory(path):
                return self._result(
                    "folder", path, "Open local WeChat folder", "account_scoped_folder"
                )
        return missing
