"""Run explicit, bounded Mac WeChat refreshes in background workers.

Code version: v1.8.0-codex.0
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from itertools import chain
import os
from pathlib import Path
import platform
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from threading import Event, RLock, Thread

from . import wechat_macos_recovery_bank as bank
from .owner_only_files import ensure_owner_only_directory
from .wechat_archive import (
    WechatArchiveRepository,
    WechatTimelineProjectionError,
    wechat_archive_path,
)
from .wechat_global_guard import GlobalRefreshBusy, global_refresh_busy
from .wechat_group_rosters import WechatGroupRosterRepository
from .wechat_mac_refresh_projection import (
    ConversationComparison,
    native_projection_executor,
    prepare_packet,
    resolve_scope,
)
from .wechat_mac_refresh_status import (
    ACCOUNT_TIMEOUT_MESSAGE,
    FAILED_MESSAGE,
    MAX_COUNTER,
    publication_failure_message,
    safe_progress,
)
from .wechat_mac_refresh_source import (
    KeyRegistry,
    RefreshError,
    RefreshIssue,
    RefreshNotice,
    SourceAccountBindingUnavailable,
    read_database,
)
from .wechat_refresh_receipts import WechatRefreshReceipts, interrupted_status


MAX_TASKS = 32
MAX_PACKET_BYTES = 128 * 1024**2
FREE_SPACE_RESERVE = 512 * 1024**2
RECEIPT_CHECKPOINT_INTERVAL = 10


@dataclass(frozen=True)
class _AccountScope:
    account_id: str


def _refresh_staging_parent(archive_directory):
    if platform.system() != "Darwin":
        # The refresh is Mac-only. Keep in-process synthetic tests portable.
        return archive_directory
    configured = Path(subprocess.check_output(
        ["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], text=True, timeout=5,
    ).strip())
    if not configured.is_absolute():
        raise RefreshError("The Mac user temporary directory is unavailable.")
    staging = configured.resolve(strict=True)
    archive = archive_directory.resolve(strict=True)
    info = staging.stat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
        or getattr(info, "st_flags", 0) & 0x40000000
        or info.st_dev != archive.stat().st_dev
        or staging.is_relative_to(archive)
    ):
        raise RefreshError("A private temporary directory on the archive volume is required.")
    return staging


class WechatMacRefreshService:
    """Expose metadata availability and explicit account or conversation refreshes."""

    def __init__(self, local_store_root, *, key_registry=None, platform=None, durable_status=False):
        self.root = Path(local_store_root)
        self.registry = key_registry if key_registry is not None else KeyRegistry()
        self.platform = sys.platform if platform is None else platform
        self._guard = RLock()
        self._tasks = {}
        self._active = None
        self._shutdown = Event()
        self._receipts = WechatRefreshReceipts(
            wechat_archive_path(self.root).parent / "browser_cache" / "refresh_receipts"
        ) if durable_status else None

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
            if self.registry.references(account_id):
                return True
        except (OSError, ValueError, TypeError):
            pass
        if kind != "group":
            return False
        from .wechat_group_roster_source import roster_source_available

        return roster_source_available(account_id, registry=self.registry)

    def start(self, session_id):
        """Resolve scope server-side and return an opaque token plus safe initial state."""
        self._check_running()
        scope = resolve_scope(self.root, session_id)
        if self.platform != "darwin":
            raise RefreshNotice(RefreshIssue.UNSUPPORTED_HOST)
        if not self.available(scope.account_id, scope.peer_id, scope.kind):
            raise RefreshNotice(RefreshIssue.CONFIGURATION_UNAVAILABLE)
        return self._start_scope(scope)

    def account_available(self, account_id):
        """Require a canonical archive scope and configured metadata for this Mac."""
        from .wechat_global_refresh import archived_account_refresh_available

        if self.platform != "darwin":
            return False
        try:
            return bool(
                archived_account_refresh_available(self.root, account_id)
                and self.registry.references(account_id)
            )
        except (OSError, ValueError, TypeError, sqlite3.Error):
            return False

    def start_account(self, account_id):
        """Refresh only existing conversations belonging to one canonical account."""
        self._check_running()
        if not self.account_available(account_id):
            raise RefreshNotice(RefreshIssue.ACCOUNT_UNAVAILABLE)
        return self._start_scope(_AccountScope(account_id))

    def _start_scope(self, scope):
        with self._guard:
            if self._shutdown.is_set():
                raise RefreshNotice(RefreshIssue.INTERRUPTED)
            if self._active is not None:
                active = self._tasks[self._active]
                if active["scope"] == scope:
                    return dict(active["public"])
                raise RefreshNotice(RefreshIssue.BUSY)
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
                "phase": "preparing",
                "parts_total": 0,
                "parts_completed": 0,
                "part_current": 0,
                "candidates": 0,
                "skipped_parts": 0,
                "unavailable_parts": 0,
                "excluded": 0,
                "upgraded": 0,
                "reparsed": 0,
                "conflicts": 0,
            }
            thread = Thread(
                target=self._run,
                args=(token, scope),
                daemon=True,
                name="wechat-refresh",
            )
            lease = None
            if self._receipts is not None:
                lease = self._receipts.claim(token)
                if lease is None:
                    raise RefreshNotice(RefreshIssue.BUSY)
                try:
                    self._receipts.write(token, public, publication_possible=False)
                except BaseException:
                    lease.close()
                    raise
            self._tasks[token] = {
                "scope": scope, "public": public, "thread": thread, "lease": lease,
                "publication_possible": False, "checkpoint_at": time.monotonic(),
            }
            self._active = token
            try:
                thread.start()
            except Exception:
                self._tasks.pop(token, None)
                self._active = None
                if lease is not None:
                    lease.close()
                raise RefreshNotice(RefreshIssue.WORKER_START_FAILED) from None
            return dict(public)

    def status(self, token):
        if not isinstance(token, str):
            return None
        with self._guard:
            task = self._tasks.get(token)
            if task is not None:
                return dict(task["public"])
        return self._receipts.read(token) if self._receipts is not None else None

    def shutdown(self, *, wait=True, timeout=5.0):
        """Stop accepting work; workers check cancellation before publication."""
        self._shutdown.set()
        with self._guard:
            threads = [task["thread"] for task in self._tasks.values()]
        if wait:
            deadline = time.monotonic() + max(0, float(timeout))
            for thread in threads:
                thread.join(max(0, deadline - time.monotonic()))

    def _update(self, token, *, publication_possible=False, **values):
        with self._guard:
            task = self._tasks[token]
            previous = task["public"]
            state = {**previous, **values}
            safe_progress(state)
            if any(
                type(state[field]) is not int or not 0 <= state[field] <= MAX_COUNTER
                for field in ("added", "total")
            ):
                raise RefreshError("Refresh status has an invalid message count.")
            if self._receipts is not None:
                require_publication_checkpoint = publication_possible or state["phase"] == "saving"
                publication_possible = require_publication_checkpoint or task["publication_possible"]
                first_publication_checkpoint = publication_possible and not task["publication_possible"]
                if state["state"] == "interrupted":
                    state = interrupted_status(state, publication_possible=publication_possible)
                now = time.monotonic()
                phase_changed = previous["phase"] != state["phase"]
                if (
                    first_publication_checkpoint
                    or state["state"] != "running"
                    or phase_changed and (
                        state["phase"] in {"saving", "verifying"}
                        or now - task["checkpoint_at"] >= RECEIPT_CHECKPOINT_INTERVAL
                    )
                ):
                    try:
                        self._receipts.write(token, state, publication_possible=publication_possible)
                    except OSError:
                        if first_publication_checkpoint and require_publication_checkpoint:
                            # Publication can start only after the uncertainty checkpoint is durable.
                            raise
                    else:
                        task["checkpoint_at"] = now
                        task["publication_possible"] = publication_possible
            task["public"] = state

    def _check_running(self):
        if self._shutdown.is_set():
            raise RefreshNotice(RefreshIssue.INTERRUPTED)
        if global_refresh_busy(self.root):
            raise GlobalRefreshBusy()

    def _publish(self, packets, owner, baseline, *, progress=None):
        history = wechat_archive_path(self.root)
        target = bank.mac_recovery_bank_path(self.root)
        bank_bytes = target.stat().st_size if target.exists() else 0
        packet_bytes = sum(len(bank._json(packet).encode()) for packet in packets)
        if packet_bytes > MAX_PACKET_BYTES:
            raise RefreshNotice(RefreshIssue.MESSAGE_BUDGET)
        staging_parent = _refresh_staging_parent(history.parent)
        # Filesystems without clone support may need a full copy of the existing bank.
        # SQLite pages stay on disk; reserve candidate capacity independently of packets.
        if (
            shutil.disk_usage(staging_parent).free
            < bank_bytes + packet_bytes * 3 + FREE_SPACE_RESERVE
        ):
            raise RefreshNotice(RefreshIssue.DISK_SPACE)
        return self._stage_and_publish(
            packets, owner, baseline, staging_parent=staging_parent,
            conversation_id=packets[0]["session_id"], progress=progress,
        )

    def _stage_and_publish(
        self, packets, owner, baseline, *, staging_parent, conversation_id=None, progress=None,
    ):
        history = wechat_archive_path(self.root)
        self._check_running()
        if bank._inputs(history) != baseline:
            raise RefreshNotice(RefreshIssue.ARCHIVE_CHANGED)
        # Only an owned runtime candidate is created; encrypted sources and plaintext
        # remain memory-only. Canonical stage/publish retain their cross-process CAS.
        with tempfile.TemporaryDirectory(
            prefix=".mac-refresh-", dir=staging_parent
        ) as directory:
            workspace = Path(directory)
            ensure_owner_only_directory(workspace)
            # A ctime-only recheck requires two private owned parent directories.
            staged = workspace / "staged"
            staged.mkdir(mode=0o700)
            ensure_owner_only_directory(staged)
            candidate = staged / "candidate.sqlite3"
            receipt = bank.stage_mac_recovery_bank(
                self.root,
                packets,
                candidate,
                account_ids=[owner],
                conversation_id=conversation_id,
                progress=lambda _progress: self._check_running(),
                scope_comparison=conversation_id is None,
                clone_baseline=True,
                check_cancel=self._check_running,
            )
            self._check_running()
            if receipt["input_identities"] != baseline:
                raise RefreshNotice(RefreshIssue.ARCHIVE_CHANGED)
            if progress is not None:
                progress(phase="saving")
            return bank.publish_mac_recovery_bank(
                self.root, candidate, receipt, before_replace=self._check_running,
                reuse_validation=True,
                check_cancel=self._check_running,
            )

    def _capture_member_snapshot(self, token, scope):
        from .wechat_group_roster_source import capture_group_roster, roster_source_available

        self._check_running()
        if not roster_source_available(scope.account_id, registry=self.registry):
            self._update(token, member_snapshot="unavailable")
            return
        self._update(token, phase="members")
        try:
            snapshot = capture_group_roster(
                scope.account_id, scope.peer_id, registry=self.registry,
                check_cancel=self._check_running,
            )
            self._check_running()
            if (
                resolve_scope(self.root, scope.session_id) != scope
                or not isinstance(snapshot, dict)
                or snapshot.get("account_id") != scope.account_id
                or snapshot.get("peer_id") != scope.peer_id
            ):
                raise RefreshError("The member snapshot crossed its selected scope.")
            self._update(token, publication_possible=True)
            self._check_running()
            WechatGroupRosterRepository(self.root).save(snapshot)
        except (OSError, ValueError, RuntimeError, sqlite3.DatabaseError):
            self._check_running()
            self._update(token, member_snapshot="failed")
        else:
            self._update(token, member_snapshot="saved")

    def _run(self, token, scope):
        try:
            if sys.version_info[:2] < (3, 14):
                from .wechat_mac_refresh_worker import run_compatible_worker

                if self._receipts is not None:
                    # The child can publish between its periodic progress frames.
                    self._update(token, publication_possible=True)
                result = run_compatible_worker(
                    self.root,
                    scope.account_id if isinstance(scope, _AccountScope) else scope.session_id,
                    self._shutdown,
                    on_progress=lambda values: self._update(
                        token, **safe_progress(values)
                    ),
                    **({"account": True} if isinstance(scope, _AccountScope) else {}),
                )
                self._check_running()
                self._update(token, **result)
                return
            if isinstance(scope, _AccountScope):
                self._run_account(token, scope.account_id)
                return
            if resolve_scope(self.root, scope.session_id) != scope:
                raise RefreshNotice(RefreshIssue.ARCHIVE_CHANGED)
            history = wechat_archive_path(self.root)
            baseline = bank._inputs(history)
            if scope.kind == "group":
                self._capture_member_snapshot(token, scope)
            references = self.registry.references(scope.account_id, load_keys=True)
            if not references:
                raise RefreshNotice(RefreshIssue.CONFIGURATION_UNAVAILABLE)
            unavailable = self.registry.missing_database_count(references)
            partial = bool(unavailable)
            packets = []
            checked = candidates = skipped = excluded = packet_bytes = 0
            self._update(
                token,
                parts_total=len(references),
                unavailable_parts=unavailable,
                partial=partial,
            )
            comparison = ConversationComparison(history, scope, baseline)
            with native_projection_executor() as (executor, in_flight):
                for ordinal, reference in enumerate(references, 1):
                    self._check_running()
                    self._update(token, phase="reading", part_current=ordinal)
                    database = read_database(reference, check_cancel=self._check_running)
                    try:
                        self._update(token, phase="comparing")
                        try:
                            packet, counts = prepare_packet(
                                self.root, scope, database, check_cancel=self._check_running,
                                comparison=comparison, projection_executor=executor,
                                projection_in_flight=in_flight,
                            )
                        except SourceAccountBindingUnavailable:
                            partial = True
                            skipped += 1
                            self._update(
                                token,
                                checked=checked,
                                partial=True,
                                skipped_parts=skipped,
                                parts_completed=ordinal,
                            )
                            continue
                        checked += counts["total"]
                        excluded += counts["excluded"]
                        partial = partial or database.partial or bool(counts["excluded"])
                        if packet is not None:
                            packet_bytes += len(bank._json(packet).encode())
                            if packet_bytes > MAX_PACKET_BYTES:
                                raise RefreshNotice(RefreshIssue.MESSAGE_BUDGET)
                            packets.append(packet)
                            candidates += len(packet["messages"])
                    finally:
                        database.close()
                    self._update(
                        token,
                        checked=checked,
                        partial=partial,
                        candidates=candidates,
                        excluded=excluded,
                        parts_completed=ordinal,
                    )
            del comparison
            self._check_running()
            if bank._inputs(history) != baseline:
                raise RefreshNotice(RefreshIssue.ARCHIVE_CHANGED)
            added = 0
            if packets:
                self._update(token, phase="checking")
                receipt = self._publish(
                    packets,
                    scope.account_id,
                    baseline,
                    progress=lambda **values: self._update(token, **values),
                )
                added = receipt["added"]
                partial = partial or bool(receipt["conflicts"] or receipt["variants"])
                self._update(
                    token,
                    added=added,
                    upgraded=receipt["upgraded"],
                    reparsed=receipt.get("reparsed", 0),
                    conflicts=receipt["conflicts"],
                )
            self._check_running()
            self._update(token, phase="verifying")
            repository = WechatArchiveRepository(self.root)
            selection = dict(
                account="account:" + scope.account_id,
                kind=scope.kind,
                session=scope.session_id,
                page_size=1,
            )
            try:
                current = repository.query(
                    **selection, persist_event_cache=bool(packets), strict_timeline=bool(packets),
                )
            except WechatTimelineProjectionError:
                current = repository.query(**selection, persist_event_cache=False)
            if current["current"] is None or (
                packets and current["resource_index_status"] not in {"ready", "absent"}
            ):
                raise RefreshNotice(RefreshIssue.VIEW_UNAVAILABLE)
            self._update(
                token,
                state="succeeded",
                phase="complete",
                added=added,
                total=current["count"],
                partial=partial,
                message=(
                    "Available chat history refreshed; some sources or messages remain unavailable."
                    if partial
                    else "Chat history refreshed."
                ),
            )
        except RefreshNotice as exc:
            state = "interrupted" if exc.issue is RefreshIssue.INTERRUPTED and self._receipts else "failed"
            self._update(token, state=state, partial=True, message=exc.issue.value)
        except Exception as error:
            from .wechat_mac_refresh_worker import AccountRefreshDeadlineExceeded

            # Source errors may contain native paths or SQLite cell values. Keep them
            # out of browser responses and logs, including chained exception text.
            phase = self.status(token)["phase"]
            message = FAILED_MESSAGE
            if isinstance(error, AccountRefreshDeadlineExceeded):
                message = ACCOUNT_TIMEOUT_MESSAGE
            elif phase == "saving":
                message = publication_failure_message(error)
            self._update(
                token,
                state="failed",
                partial=True,
                message=message,
            )
        finally:
            with self._guard:
                lease = self._tasks[token]["lease"]
                if lease is not None:
                    lease.close()
                if self._active == token:
                    self._active = None

    def _run_account(self, token, owner):
        from .wechat_global_refresh import inventory_global_refresh, iter_global_refresh_packets

        history = wechat_archive_path(self.root)
        baseline = bank._inputs(history)
        inventory = inventory_global_refresh(self.root, self.registry, account_id=owner)
        if inventory.account_ids != (owner,):
            raise RefreshError("The configured account sources are no longer available.")

        coverage = {}

        def update_coverage(values):
            coverage.update(values)
            partial = bool(
                values["unavailable_sources"] or values["skipped_sources"]
                or values["skipped_scopes"] or values["excluded"]
                or values["reason_codes"].get("source_incomplete_wal_coverage")
            )
            total = values["selected_sources"]
            completed = values["completed_sources"]
            skipped = values["skipped_sources"]
            self._update(
                token,
                phase="checking" if values["phase"] == "complete" else values["phase"],
                parts_total=total,
                part_current=min(total, completed + skipped + 1),
                parts_completed=completed,
                checked=values["checked"],
                candidates=values["candidates"],
                skipped_parts=skipped,
                unavailable_parts=values["unavailable_sources"],
                excluded=values["excluded"],
                partial=partial,
            )

        def checked_packets(packets, staging_parent):
            for packet in packets:
                self._check_running()
                if not isinstance(packet, dict) or packet.get("account_id") != owner:
                    raise RefreshError("A refresh packet belongs to another account.")
                packet_bytes = len(bank._json(packet).encode())
                if packet_bytes > MAX_PACKET_BYTES:
                    raise RefreshError("An account refresh packet exceeds its bounded message budget.")
                if shutil.disk_usage(staging_parent).free < packet_bytes * 3 + FREE_SPACE_RESERVE:
                    raise RefreshNotice(RefreshIssue.DISK_SPACE)
                yield packet

        receipt = {}
        with closing(iter_global_refresh_packets(
            self.root, registry=self.registry, inventory=inventory,
            check_cancel=self._check_running, on_progress=update_coverage,
        )) as packets:
            first = next(packets, None)
            if first is not None:
                staging_parent = _refresh_staging_parent(history.parent)
                target = bank.mac_recovery_bank_path(self.root)
                bank_bytes = target.stat().st_size if target.exists() else 0
                # Keep space for a full candidate and SQLite's on-disk work.
                if shutil.disk_usage(staging_parent).free < 2 * bank_bytes + FREE_SPACE_RESERVE:
                    raise RefreshNotice(RefreshIssue.DISK_SPACE)
                self._update(token, phase="checking")
                receipt = self._stage_and_publish(
                    checked_packets(chain((first,), packets), staging_parent), owner, baseline,
                    staging_parent=staging_parent,
                    progress=lambda **values: self._update(token, **values),
                )
        self._check_running()
        if not receipt and bank._inputs(history) != baseline:
            raise RefreshNotice(RefreshIssue.ARCHIVE_CHANGED)
        self._update(token, phase="verifying")
        if receipt:
            bank.verify_published_account_refresh(self.root, owner, receipt)
        partial = bool(
            self.status(token)["partial"] or receipt.get("conflicts")
            or receipt.get("variants")
            or coverage.get("processed_scopes", 0) < len(inventory.scopes)
        )
        self._update(
            token,
            state="succeeded",
            phase="complete",
            added=receipt.get("added", 0),
            upgraded=receipt.get("upgraded", 0),
            reparsed=receipt.get("reparsed", 0),
            conflicts=receipt.get("conflicts", 0),
            # The transport reserves total for conversation history results.
            total=0,
            partial=partial,
            message=(
                "Available chat history refreshed; some sources or messages remain unavailable."
                if partial else "Chat history refreshed."
            ),
        )
