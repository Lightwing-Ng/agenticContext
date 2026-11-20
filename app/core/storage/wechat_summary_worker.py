"""Prepare exact WeChat conversation summaries outside document requests."""

# Code version: v1.0.0-codex.0

from contextlib import closing
import logging
import math
import sqlite3
from threading import Condition, Thread
import time

from ..wechat_archive import WechatArchiveRepository, _file_identity
from ..wechat_browser_read import WechatBrowserReadService
from ..wechat_browser_snapshot import _pin, load_conversation_snapshot


_LOGGER = logging.getLogger(__name__)
_SETTLE_SECONDS = 5.0
_MAX_RETRY_SECONDS = 300.0
_MAX_SHUTDOWN_WAIT_SECONDS = 0.5


class WechatSummaryWorker:
    """Warm one stable publication at a time, sharing the browser's snapshot builder."""

    def __init__(self, root, *, interval_seconds=30.0):
        if not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError("The summary polling interval must be positive and finite.")
        self._root = root
        self.repository = WechatArchiveRepository(root)
        self.interval_seconds = interval_seconds
        self._condition = Condition()
        self._thread = None
        self._wake_requested = False
        self._recheck_requested = False
        self._closed = False
        self._publication_state = "running"
        self._revision = 0
        self._validator = None

    @property
    def active(self):
        """Report whether this process is running its explicit prewarm worker."""
        with self._condition:
            return not self._closed and self._thread is not None and self._thread.is_alive()

    def start(self):
        """Schedule preparation without opening the archive on the caller's thread."""
        with self._condition:
            if self._thread is not None or self._closed:
                return
            self._thread = Thread(
                target=self._run, name="wechat-summary-worker", daemon=True
            )
            self._thread.start()

    def wake(self, *, recheck=False):
        """Observe new sources or request a rate-limited repair of a missing summary."""
        with self._condition:
            if not self._closed:
                self._wake_requested = True
                if recheck and self._publication_state == "ready":
                    self._recheck_requested = True
                self._condition.notify_all()

    def wait_for_publication(self, *, timeout=1.0):
        """Wait briefly for state to change; readers must still verify their own pin."""
        with self._condition:
            if not self.active:
                return "stopped"
            revision = self._revision
            self._condition.wait_for(
                lambda: self._closed or self._revision != revision,
                max(0.0, min(timeout, 1.0)),
            )
            return self._publication_state if self.active else "stopped"

    def shutdown(self, *, wait=True, timeout=_MAX_SHUTDOWN_WAIT_SECONDS):
        """Stop polling and leave an in-flight shared build to finish or exit safely."""
        with self._condition:
            self._closed = True
            thread = self._thread
            self._condition.notify_all()
        if wait and thread is not None:
            thread.join(max(0.0, min(timeout, _MAX_SHUTDOWN_WAIT_SECONDS)))

    def _wait(self, delay):
        with self._condition:
            self._condition.wait_for(lambda: self._closed or self._wake_requested, delay)
            recheck = self._recheck_requested
            self._wake_requested = False
            self._recheck_requested = False
            return not self._closed, recheck

    def _set_state(self, state):
        with self._condition:
            if self._publication_state != state:
                self._publication_state = state
                self._revision += 1
                self._condition.notify_all()

    def _still_current(self, identity, pin):
        with self._condition:
            if self._closed:
                return False
        return _pin(self.repository, identity) == pin

    def _prepare(self, identity, pin):
        from .wechat_summary_read import publish_account_summaries

        repository = self.repository
        if self._validator is None:
            self._validator = WechatBrowserReadService(self._root)
        self._validator._validate_contacts(repository.path.with_name("contacts.sqlite3"))
        if not self._still_current(identity, pin):
            return False
        with closing(sqlite3.connect(
            repository.path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
        )) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA temp_store=MEMORY")
            if load_conversation_snapshot(repository, connection, identity) is None:
                return False
        return (
            self._still_current(identity, pin)
            and publish_account_summaries(repository, identity)
            and self._still_current(identity, pin)
        )

    def _run(self):
        prepared, observed, failed, recheck_for = None, None, None, None
        stable_after, retry_after, repair_after = 0.0, 0.0, 0.0
        failures = 0
        delay = 0.0
        while True:
            active, recheck = self._wait(delay)
            if not active:
                return
            if recheck:
                recheck_for = prepared
            delay = self.interval_seconds
            try:
                identity = _file_identity(self.repository.path)
                pin = _pin(self.repository, identity)
            except (OSError, ValueError, TypeError):
                observed = None
                self._set_state("failed")
                continue

            now = time.monotonic()
            if pin != observed:
                observed = pin
                stable_after = now + min(_SETTLE_SECONDS, self.interval_seconds)
                recheck_for = recheck_for if recheck_for == pin else None
                self._set_state("ready" if pin == prepared else "running")
            if pin == prepared:
                if recheck_for != pin:
                    continue
                self._set_state("running")
                if now < repair_after:
                    delay = min(delay, repair_after - now)
                    continue
                prepared, recheck_for = None, None
            earliest = max(stable_after, retry_after if pin == failed else 0.0)
            if now < earliest:
                delay = min(delay, earliest - now)
                continue

            try:
                self._set_state("running")
                if self._prepare(identity, pin):
                    prepared, failed, failures = pin, None, 0
                    repair_after = time.monotonic() + max(_SETTLE_SECONDS, self.interval_seconds)
                    self._set_state("ready")
                    continue
            except Exception:
                # Source exceptions can contain private paths or message diagnostics.
                _LOGGER.debug("Could not prepare the saved WeChat conversation summaries.")
            failures = min(failures + 1, 6) if pin == failed else 1
            failed = pin
            retry_after = time.monotonic() + min(
                _MAX_RETRY_SECONDS, self.interval_seconds * 2 ** (failures - 1)
            )
            self._set_state("failed")
