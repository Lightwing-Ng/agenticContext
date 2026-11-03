"""Stream one complete source-scoped WeChat timeline from pinned local inputs.

Code version: v1.2.0-codex.0
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import stat

from .wechat_archive import WECHAT_KINDS, WechatArchiveRepository, _file_identity
from .wechat_contacts import WechatContactRepository
from .wechat_display_names import WechatDisplayNames, display_names_path


class WechatSessionSourceChanged(RuntimeError):
    """Stop an export when its published source snapshot changes."""


def _identity(path):
    value = path.stat()
    if path.is_symlink() or not stat.S_ISREG(value.st_mode):
        raise ValueError("A WeChat export needs a regular local archive.")
    if getattr(value, "st_flags", 0) & 0x40000000:
        raise WechatSessionSourceChanged("The archive is not locally available.")
    if any(
        path.with_name(path.name + suffix).exists()
        for suffix in ("-wal", "-shm", "-journal")
    ):
        raise WechatSessionSourceChanged("The archive is being published.")
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


class WechatSessionExport:
    """Keep one read-only cursor open instead of materializing a paginated view."""

    def __init__(self, local_store_root, session_id, *, account="", kind="all"):
        self.connection = None
        if (
            not isinstance(session_id, str)
            or re.fullmatch(r"wechat:[0-9a-f]{64}", session_id) is None
        ):
            raise ValueError("A canonical WeChat conversation ID is required.")
        if kind not in WECHAT_KINDS:
            raise ValueError("Unsupported WeChat conversation type.")
        repository = WechatArchiveRepository(local_store_root)
        if not repository.path.exists():
            raise ValueError("The WeChat conversation is unavailable.")
        self.sources = {repository.path: _identity(repository.path)}
        resource_path = repository.path.with_name("resource_references.sqlite3")
        try:
            resource_identity = _identity(resource_path)
        except (OSError, ValueError, WechatSessionSourceChanged):
            resource_identity = None
        try:
            self.connection = sqlite3.connect(
                repository.path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
            )
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA temp_store=MEMORY")
            conversations, messages, self.resource_status = repository._timeline_tables(
                self.connection, _file_identity(repository.path)
            )
            if self.resource_status == "ready":
                if (
                    resource_identity is None
                    or _identity(resource_path) != resource_identity
                ):
                    raise WechatSessionSourceChanged(
                        "The resource index changed during export."
                    )
                self.sources[resource_path] = resource_identity
            where, parameters = ["c.id=?"], [session_id]
            if account:
                where.append("c.account_key=?")
                parameters.append(account)
            if kind != "all":
                where.append("c.kind=?")
                parameters.append(kind)
            row = self.connection.execute(
                "SELECT c.*,p.account_label,p.account_id,p.identity_status,"
                "p.source_label,p.source_file FROM "
                + conversations
                + " c JOIN main.partitions p ON p.id=c.partition_id WHERE "
                + " AND ".join(where),
                parameters,
            ).fetchone()
            if row is None:
                raise ValueError(
                    "The WeChat conversation is unavailable in this scope."
                )
            self.conversation = dict(row)
            self.session_id = session_id
            from .wechat_conversation_events import (
                canonical_message_table,
                conversation_events,
            )

            registry_path = WechatContactRepository(local_store_root).path
            try:
                registry_identity = _identity(registry_path)
            except (OSError, ValueError, WechatSessionSourceChanged):
                registry_identity = None
            events = conversation_events(
                repository,
                self.connection,
                session_id,
                self.conversation,
                messages,
                _file_identity(repository.path),
            )
            if events["preferred_by_id"]:
                if (
                    registry_identity is None
                    or _identity(registry_path) != registry_identity
                ):
                    raise WechatSessionSourceChanged(
                        "The contact evidence changed during event resolution."
                    )
                self.sources[registry_path] = registry_identity
            self.message_table = canonical_message_table(
                self.connection, messages, events
            )
            self.conversation["message_count"] = events["display_count"]
            self._prepare_viewer_names(local_store_root)
            self._check_sources()
        except BaseException:
            self.close()
            raise

    def _prepare_viewer_names(self, local_store_root):
        """Resolve aliases only when a stable local preference actually needs them."""
        self.display_names = None
        self.contact_repository = None
        preferences = display_names_path(local_store_root)
        try:
            preferences_identity = _identity(preferences)
        except (OSError, ValueError, WechatSessionSourceChanged):
            return
        names = WechatDisplayNames(local_store_root)
        if not names.names:
            return
        if _identity(preferences) != preferences_identity:
            raise WechatSessionSourceChanged(
                "The viewing-name preferences changed during export."
            )
        contacts = WechatContactRepository(local_store_root)
        try:
            contacts_identity = _identity(contacts.path)
        except (OSError, ValueError, WechatSessionSourceChanged):
            return
        self.sources[preferences] = preferences_identity
        self.sources[contacts.path] = contacts_identity
        self.display_names = names
        self.contact_repository = contacts

    def _project_viewer_names(self, rows):
        """Reuse verified message bindings while keeping every source field intact."""
        items = [dict(row) for row in rows]
        if self.display_names is None:
            return items
        conversation = self.conversation
        snapshots = WechatArchiveRepository._source_snapshots(
            self.connection, items, conversation["partition_id"]
        )
        requests = []
        for item in items:
            provenance = json.loads(item["provenance"])
            provenance = provenance if isinstance(provenance, dict) else {}
            source_snapshot = provenance.get("source_snapshot")
            snapshot = (
                snapshots.get(source_snapshot, {})
                if type(source_snapshot) is int
                else {}
            )
            source_context = {
                **conversation,
                **snapshot.get("routed_messages", {}).get(item["id"], {}),
            }
            record_id = provenance.get("record_id")
            locator = provenance.get("locator")
            requests.append(
                {
                    "id": item["id"],
                    "source_id": source_context["partition_id"],
                    "source_snapshot_id": snapshot.get("snapshot_id", ""),
                    "conversation_id": item["conversation_id"],
                    "sender_id": item["sender_id"] or "",
                    "sender_label": item["sender_label"],
                    "account_id": source_context["account_id"],
                    "peer_id": source_context["peer_id"],
                    "conversation_kind": source_context["kind"],
                    "text_sha256": hashlib.sha256(item["text"].encode()).hexdigest(),
                    "timestamp_value": item["timestamp_value"],
                    "source_record_id": record_id if isinstance(record_id, str) else "",
                    "source_locator": locator if isinstance(locator, dict) else {},
                    "is_system": provenance.get("text_extraction_kind")
                    == "system_text",
                }
            )
        contacts = self.contact_repository.resolve_messages(requests)
        self._check_sources()
        for item in items:
            name = self.display_names.contact_name(contacts[item["id"]])
            if name:
                item["viewer_display_name"] = name
        return items

    def _check_sources(self):
        try:
            if any(
                _identity(path) != expected for path, expected in self.sources.items()
            ):
                raise WechatSessionSourceChanged(
                    "The WeChat source changed during export."
                )
        except (OSError, ValueError) as error:
            raise WechatSessionSourceChanged(
                "The WeChat source is no longer available."
            ) from error

    def items(self):
        """Yield preferred conversation events, with missing clocks ordered last."""
        try:
            for known_clock in (True, False):
                self._check_sources()
                # Two index-ordered reads keep the unknown-clock tail without
                # sorting every message and its body before sending the first row.
                cursor = self.connection.execute(
                    "SELECT m.* FROM "
                    + self.message_table
                    + " m WHERE m.conversation_id=? AND m.time_sort"
                    + (
                        "!=0 ORDER BY m.time_sort,m.ordinal,m.id"
                        if known_clock
                        else "=0 ORDER BY m.ordinal,m.id"
                    ),
                    (self.session_id,),
                )
                try:
                    while True:
                        self._check_sources()
                        rows = cursor.fetchmany(200)
                        self._check_sources()
                        if not rows:
                            break
                        yield from self._project_viewer_names(rows)
                finally:
                    cursor.close()
        finally:
            self.close()

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
