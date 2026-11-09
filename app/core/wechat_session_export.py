"""Stream one complete source-scoped WeChat timeline from pinned local inputs.

Code version: v1.5.0-codex.0
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


def forwarded_export_items(record):
    """Expose a proved HTML packet's leaves without changing its source body."""
    from .wechat_contacts import MAX_FORWARDED_COMPONENTS, MAX_FORWARDED_DEPTH

    items, recovered = [], False
    pending = [(record, (), 0)]
    count = 0
    while pending:
        current, prefix, depth = pending.pop()
        if not isinstance(current, dict) or depth > MAX_FORWARDED_DEPTH:
            return ()
        children = current.get("items", ())
        if not isinstance(children, (tuple, list)):
            return ()
        for number, child in reversed(tuple(enumerate(children, 1))):
            count += 1
            if count > MAX_FORWARDED_COMPONENTS:
                return ()
            path = (*prefix, number)
            context = child.messaging or {}
            nested = context.get("forwarded_record")
            if nested and not context.get("forwarded_preview_proof"):
                pending.append((nested, path, depth + 1))
                continue
            provenance = context.get("provenance", {})
            component = provenance.get("embedded_html_forwarded_component")
            if not isinstance(component, dict) or not isinstance(
                component.get("text"), str
            ):
                return ()
            proof = provenance.get("native_forwarded_clock_recovery")
            recovered = recovered or isinstance(proof, dict) and bool(proof)
            items.append(
                {
                    "id": child.stable_id,
                    "component_path": path,
                    "sender_id": component.get("sender_native_id")
                    or context.get("sender_id", ""),
                    "sender_label": child.author_label,
                    "viewer_display_name": context.get("viewer_display_name", ""),
                    "text": component["text"],
                    "timestamp_value": child.last_seen_at,
                    "timestamp_label": context.get("timestamp_label", ""),
                    "time_basis": context.get("time_basis", "Unavailable source time"),
                    "source_clock": provenance.get("source_time", ""),
                    "provenance": provenance,
                }
            )
    if not recovered:
        return ()
    return tuple(sorted(items, key=lambda item: item["component_path"]))


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


def _active_timeline_views(connection, message_table):
    """Follow the final TEMP view through every source overlay it actually uses."""
    pending, active = [message_table], set()
    while pending:
        name = pending.pop()
        if "." in name:
            schema, name = name.split(".", 1)
            if schema != "temp":
                continue
        if name in active:
            continue
        row = connection.execute(
            "SELECT sql FROM temp.sqlite_master WHERE type='view' AND name=?", (name,)
        ).fetchone()
        if row is None:
            continue
        active.add(name)
        if len(active) > 32:
            raise ValueError("The export timeline exceeds its supported view structure.")
        pending.extend(
            re.findall(
                r"\b(?:FROM|JOIN)\s+((?:[A-Za-z_][A-Za-z0-9_]*\.)?[A-Za-z_][A-Za-z0-9_]*)",
                row[0],
                re.IGNORECASE,
            )
        )
    return active


class WechatSessionExport:
    """Keep one read-only cursor open instead of materializing a paginated view."""

    def __init__(self, local_store_root, session_id, *, account="", kind="all", write_event_cache=True):
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
        from .wechat_forwarded_clocks import WechatForwardedClocks, registry_digest

        self.local_store_root = local_store_root
        self.forwarded_clock_digest = registry_digest(local_store_root)
        self.forwarded_clocks = WechatForwardedClocks(local_store_root)
        from .wechat_native_resource_revalidation import select_resource_index

        resource_path = select_resource_index(repository.path, conversation_id=session_id)
        try:
            resource_identity = _identity(resource_path)
        except (OSError, ValueError, WechatSessionSourceChanged):
            resource_identity = None
        placeholder_path = repository.path.with_name("media_placeholders.json")
        try:
            placeholder_identity = _identity(placeholder_path)
        except (OSError, ValueError, WechatSessionSourceChanged):
            placeholder_identity = None
        media_event_path = repository.path.with_name("media_events.sqlite3")
        try:
            media_event_identity = _identity(media_event_path)
        except (OSError, ValueError, WechatSessionSourceChanged):
            media_event_identity = None
        recovery_path = repository.path.with_name("macos_recovery.json")
        try:
            recovery_identity = _identity(recovery_path)
        except (OSError, ValueError, WechatSessionSourceChanged):
            recovery_identity = None
        bank_path = repository.path.with_name("macos_recovery.sqlite3")
        try:
            bank_identity = _identity(bank_path)
        except (OSError, ValueError, WechatSessionSourceChanged):
            bank_identity = None
        try:
            self.connection = sqlite3.connect(
                repository.path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
            )
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA temp_store=MEMORY")
            conversations, messages, self.resource_status = repository._timeline_tables(
                self.connection, _file_identity(repository.path), conversation_id=session_id
            )
            try:
                recovery_after = _identity(recovery_path)
            except (OSError, ValueError, WechatSessionSourceChanged):
                recovery_after = None
            if recovery_after != recovery_identity:
                raise WechatSessionSourceChanged(
                    "The Mac recovery metadata changed during timeline projection."
                )
            try:
                bank_after = _identity(bank_path)
            except (OSError, ValueError, WechatSessionSourceChanged):
                bank_after = None
            if bank_after != bank_identity:
                raise WechatSessionSourceChanged(
                    "The Mac recovery bank changed during timeline projection."
                )
            active_views = _active_timeline_views(self.connection, messages)
            for view, path, expected in (
                ("timeline_messages", resource_path, resource_identity),
                ("placeholder_timeline_messages", placeholder_path, placeholder_identity),
                ("media_event_timeline_messages", media_event_path, media_event_identity),
                ("macos_recovery_timeline_messages", recovery_path, recovery_identity),
                ("macos_recovery_bank_timeline_messages", bank_path, bank_identity),
            ):
                if view in active_views:
                    if expected is None or _identity(path) != expected:
                        raise WechatSessionSourceChanged(
                            "The timeline source metadata changed during export."
                        )
                    self.sources[path] = expected
            if "macos_recovery_bank_timeline_messages" in active_views and self.connection.execute(
                "SELECT 1 FROM macos_recovery_bank.sqlite_master WHERE type='table' AND name='shadow_replacements'"
            ).fetchone() is not None:
                bank_inputs = json.loads(self.connection.execute(
                    "SELECT value FROM macos_recovery_bank.archive_info WHERE key='input_identities'"
                ).fetchone()[0])
                for source in self.connection.execute(
                    "SELECT DISTINCT source_file_name FROM macos_recovery_bank.shadow_replacements WHERE conversation_id=?",
                    (session_id,),
                ):
                    name = source[0]
                    if name not in {"resource_references.sqlite3", "resource_revalidated.sqlite3"}:
                        raise ValueError("A shadow export requires its original resource catalog.")
                    source_path = repository.path.with_name(name)
                    expected_source = bank_inputs.get(name)
                    actual_source = _identity(source_path)
                    if not isinstance(expected_source, dict) or actual_source != tuple(
                        expected_source.get(key) for key in ("dev", "ino", "size", "mtime_ns", "ctime_ns")
                    ):
                        raise WechatSessionSourceChanged("An original shadow source changed during export.")
                    self.sources[source_path] = actual_source
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
                persist=write_event_cache,
            )
            self.events = events
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
        from .wechat_macos_recovery import (
            mark_mac_recovery_rows,
            validated_recovery_receipt,
        )

        mark_mac_recovery_rows(self.connection, items)
        for item in items:
            receipt = validated_recovery_receipt(item)
            if receipt is not None:
                item.update(
                    source_label="WeChat for Mac local database",
                    source_file="WeChat for Mac local database: "
                    + receipt["database_relative_path"],
                    source_snapshot_id=receipt["snapshot_id"],
                    backup_id="",
                )
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
        from .wechat_forwarded_clocks import registry_digest

        try:
            if any(
                _identity(path) != expected for path, expected in self.sources.items()
            ):
                raise WechatSessionSourceChanged(
                    "The WeChat source changed during export."
                )
            if registry_digest(self.local_store_root) != self.forwarded_clock_digest:
                raise WechatSessionSourceChanged(
                    "The forwarded clock evidence changed during export."
                )
        except (OSError, ValueError) as error:
            raise WechatSessionSourceChanged(
                "The WeChat source is no longer available."
            ) from error

    def _project_forwarded_clocks(self, rows):
        """Add child observations only after the entire source packet is proved."""
        from .wechat_forwarded import (
            apply_forwarded_clock_evidence,
            project_html_forwarded,
        )

        projections = self.forwarded_clocks.project(rows)
        for item in rows:
            clocks = projections.get(item["id"])
            if not clocks:
                from .wechat_exchange_presentation import frozen_forwarded_items

                frozen = frozen_forwarded_items(
                    json.loads(item["provenance"]), self.conversation, parent_id=item["id"]
                )
                if frozen:
                    item["forwarded_items"] = frozen
                continue
            provenance = json.loads(item["provenance"])
            record = project_html_forwarded(
                provenance,
                parent_id=item["id"],
                account_id=self.conversation["account_id"],
                text=item["text"],
                components=provenance.get("forwarded_components"),
                source_proof_status="source_proof_verified",
            )
            if record is not None:
                apply_forwarded_clock_evidence(record, clocks)
                item["forwarded_items"] = forwarded_export_items(record)
        self._check_sources()
        return rows

    def items(self):
        """Yield chronological source parents with proved packet leaves attached."""
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
                        yield from self._project_forwarded_clocks(
                            self._project_viewer_names(rows)
                        )
                finally:
                    cursor.close()
        finally:
            self.close()

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
