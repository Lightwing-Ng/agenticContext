"""Run explicit, bounded Mac WeChat conversation refreshes in background workers.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import os
from pathlib import Path
import secrets
import shutil
import sys
import tempfile
import time
from threading import Event, RLock, Thread

from . import wechat_macos_recovery_bank as bank
from .wechat_archive import WechatArchiveRepository, wechat_archive_path
from .wechat_mac_refresh_projection import prepare_packet, resolve_scope
from .wechat_mac_refresh_source import KeyRegistry, RefreshError, read_database


MAX_TASKS = 32
MAX_PACKET_BYTES = 128 * 1024**2
MAX_BANK_BYTES = 2 * 1024**3
FREE_SPACE_RESERVE = 512 * 1024**2


class WechatMacRefreshService:
    """Expose metadata availability and explicit per-conversation start/status calls."""

    def __init__(self, local_store_root, *, key_registry=None, platform=None):
        self.root = Path(local_store_root)
        self.registry = key_registry if key_registry is not None else KeyRegistry()
        self.platform = sys.platform if platform is None else platform
        self._guard = RLock()
        self._tasks = {}
        self._active = None
        self._shutdown = Event()

    def available(self, account_id, peer_id, kind):
        """Inspect only configured-key metadata; never open or decrypt a database."""
        if (
            self.platform != "darwin"
            or kind not in {"direct", "group"}
            or not isinstance(account_id, str)
            or not isinstance(peer_id, str)
            or not peer_id
            or peer_id.startswith("gh_")
            or (kind == "group") != peer_id.endswith(("@chatroom", "@im.chatroom"))
        ):
            return False
        try:
            return bool(self.registry.references(account_id))
        except (OSError, ValueError, TypeError):
            return False

    def start(self, session_id):
        """Resolve scope server-side and return an opaque token plus safe initial state."""
        scope = resolve_scope(self.root, session_id)
        if not self.available(scope.account_id, scope.peer_id, scope.kind):
            raise RefreshError(
                "Database keys are not configured for this conversation."
            )
        with self._guard:
            if self._shutdown.is_set():
                raise RefreshError("The local refresh service is shutting down.")
            if self._active is not None:
                active = self._tasks[self._active]
                if active["scope"] == scope:
                    return dict(active["public"])
                raise RefreshError(
                    "Another conversation is refreshing. Try again shortly."
                )
            while len(self._tasks) >= MAX_TASKS:
                self._tasks.pop(next(iter(self._tasks)))
            token = secrets.token_urlsafe(24)
            public = {
                "task_id": token,
                "state": "running",
                "message": "Refreshing chat history.",
                "added": 0,
                "total": 0,
                "partial": False,
                "checked": 0,
            }
            thread = Thread(
                target=self._run,
                args=(token, scope),
                daemon=True,
                name="wechat-conversation-refresh",
            )
            self._tasks[token] = {"scope": scope, "public": public, "thread": thread}
            self._active = token
            try:
                thread.start()
            except Exception:
                self._tasks.pop(token, None)
                self._active = None
                raise RefreshError(
                    "The local refresh worker could not be started."
                ) from None
            return dict(public)

    def status(self, token):
        if not isinstance(token, str):
            return None
        with self._guard:
            task = self._tasks.get(token)
            return dict(task["public"]) if task is not None else None

    def shutdown(self, *, wait=True, timeout=5.0):
        """Stop accepting work; workers check cancellation before publication."""
        self._shutdown.set()
        with self._guard:
            threads = [task["thread"] for task in self._tasks.values()]
        if wait:
            deadline = time.monotonic() + max(0, float(timeout))
            for thread in threads:
                thread.join(max(0, deadline - time.monotonic()))

    def _update(self, token, **values):
        with self._guard:
            self._tasks[token]["public"].update(values)

    def _check_running(self):
        if self._shutdown.is_set():
            raise RefreshError("The local refresh was interrupted before publication.")

    def _publish(self, packets, owner, baseline):
        history = wechat_archive_path(self.root)
        target = bank.mac_recovery_bank_path(self.root)
        bank_bytes = target.stat().st_size if target.exists() else 0
        packet_bytes = sum(len(bank._json(packet).encode()) for packet in packets)
        if bank_bytes > MAX_BANK_BYTES or packet_bytes > MAX_PACKET_BYTES:
            raise RefreshError(
                "This refresh exceeds the bounded temporary storage budget."
            )
        if (
            shutil.disk_usage(history.parent).free
            < bank_bytes + packet_bytes * 3 + FREE_SPACE_RESERVE
        ):
            raise RefreshError(
                "Not enough temporary storage is available to refresh this chat."
            )
        self._check_running()
        if bank._inputs(history) != baseline:
            raise RefreshError("The archive changed during refresh. Try again shortly.")
        # Only an owned runtime candidate is created; encrypted sources and plaintext
        # remain memory-only. Canonical stage/publish retain their cross-process CAS.
        with tempfile.TemporaryDirectory(
            prefix=".mac-refresh-", dir=history.parent
        ) as directory:
            os.chmod(directory, 0o700)
            candidate = Path(directory) / "candidate.sqlite3"
            receipt = bank.stage_mac_recovery_bank(
                self.root,
                packets,
                candidate,
                account_ids=[owner],
                progress=lambda _progress: self._check_running(),
            )
            self._check_running()
            if receipt["input_identities"] != baseline:
                raise RefreshError(
                    "The archive changed before refresh staging. Try again shortly."
                )
            return bank.publish_mac_recovery_bank(
                self.root, candidate, receipt, before_replace=self._check_running
            )

    def _run(self, token, scope):
        try:
            if sys.version_info[:2] < (3, 14):
                from .wechat_mac_refresh_worker import run_compatible_worker

                result = run_compatible_worker(
                    self.root, scope.session_id, self._shutdown
                )
                self._check_running()
                self._update(token, **result)
                return
            if resolve_scope(self.root, scope.session_id) != scope:
                raise RefreshError("The conversation changed before refresh started.")
            history = wechat_archive_path(self.root)
            baseline = bank._inputs(history)
            references = self.registry.references(scope.account_id, load_keys=True)
            if not references:
                raise RefreshError(
                    "The configured database keys are no longer available."
                )
            partial = bool(self.registry.missing_database_count(references))
            packets = []
            checked = 0
            for reference in references:
                self._check_running()
                database = read_database(reference, check_cancel=self._check_running)
                try:
                    packet, counts = prepare_packet(
                        self.root, scope, database, check_cancel=self._check_running
                    )
                    checked += counts["total"]
                    partial = partial or database.partial or bool(counts["excluded"])
                    if packet is not None:
                        packets.append(packet)
                    if (
                        sum(len(bank._json(value).encode()) for value in packets)
                        > MAX_PACKET_BYTES
                    ):
                        raise RefreshError(
                            "This refresh exceeds its bounded message budget."
                        )
                finally:
                    database.close()
                self._update(token, checked=checked, partial=partial)
            self._check_running()
            if bank._inputs(history) != baseline:
                raise RefreshError(
                    "The archive changed during refresh. Try again shortly."
                )
            added = 0
            if packets:
                receipt = self._publish(packets, scope.account_id, baseline)
                added = receipt["added"]
                partial = partial or bool(receipt["conflicts"] or receipt["variants"])
                self._update(token, added=added)
            self._check_running()
            current = WechatArchiveRepository(self.root).query(
                account="account:" + scope.account_id,
                kind=scope.kind,
                session=scope.session_id,
                page_size=1,
                persist_event_cache=False,
            )
            if current["current"] is None:
                raise RefreshError(
                    "The refreshed conversation is temporarily unavailable."
                )
            self._update(
                token,
                state="succeeded",
                added=added,
                total=current["count"],
                partial=partial,
                message=(
                    "Available chat history refreshed; some sources or messages remain unavailable."
                    if partial
                    else "Chat history refreshed."
                ),
            )
        except Exception:
            # Source errors may contain native paths or SQLite cell values. Keep them
            # out of browser responses and logs, including chained exception text.
            self._update(
                token,
                state="failed",
                partial=True,
                message="Chat history could not be refreshed safely. Try again shortly.",
            )
        finally:
            with self._guard:
                if self._active == token:
                    self._active = None
