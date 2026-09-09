"""Bounded, independently controlled Web Agent sessions.

Code version: v1.5.0-codex.1
"""

from contextlib import contextmanager
from functools import partial
import os
from pathlib import Path
import re
import stat as stat_module
from threading import RLock
from uuid import uuid4

from app.core.agent.compute_jobs import (
    ACTIVE_STATES,
    ComputeJobError,
    MAX_METADATA_SCAN_RECORDS,
    compute_job_workspace_identity,
    scan_compute_job_metadata,
)
from app.core.config import is_windows_host
from app.core.computer_use_agent import (
    ComputerUseAgentService,
    normalize_agent_conversation_url,
    _path_crosses_link_like_component,
)


class AgentSessionPool:
    """Admit bounded workers, with one owner for a shared Windows CDP browser."""

    limit = 2
    _MAX_COMPUTE_RUNTIME_ROOTS = 512
    _WORKSPACE_CONFLICT = (
        "This workspace already has an active write-capable Agent task. "
        "Wait for it to finish or stop it before starting another write-capable task."
    )
    _WORKSPACE_UNVERIFIED = (
        "The active Agent workspace identity could not be verified safely. "
        "Wait for that task to finish or stop it before starting another write-capable task."
    )
    _COMPUTE_JOB_CONFLICT = (
        "This workspace has an active durable compute job. Wait for it to "
        "finish or stop it before starting a write-capable Agent task."
    )
    _COMPUTE_JOB_UNVERIFIED = (
        "Durable compute-job metadata could not be verified safely. "
        "Resolve the runtime record before starting a write-capable Agent task."
    )

    def __init__(self, primary):
        self._lock = RLock()
        self._closed = False
        self._primary = primary
        self._root = primary._runtime_root / "sessions"
        self._services = {"primary": primary}
        self._workspace_leases = {}
        self._attach(primary)
        if not _path_crosses_link_like_component(self._root) and self._root.is_dir():
            for path in sorted(self._root.iterdir()):
                if re.fullmatch(r"[0-9a-f]{32}", path.name) and path.is_dir() and not path.is_symlink():
                    self._services[path.name] = self._create(path.name)

    def _attach(self, service):
        service._admission_guard = partial(self._admit, service)
        return service

    def _create(self, session_id):
        return self._attach(ComputerUseAgentService(
            self._primary._settings_store,
            runner=self._primary._runner,
            runtime_root=self._root / session_id,
            browser_opener=self._primary._browser_opener,
            config_provider=self._primary._config_provider,
        ))

    def get(self, session_id="primary"):
        with self._lock:
            if session_id not in self._services:
                raise ValueError("The selected Agent session is unavailable. Choose a session from the sidebar.")
            return self._services[session_id]

    @contextmanager
    def _admit(self, service, settings, target_url, workspace, read_only):
        with self._lock:
            if self._closed:
                raise RuntimeError("The Agent service is shutting down.")
            snapshots = list(self._snapshot_rows().values())
            active = [item for item in snapshots if item.get("running")]
            previous = service.snapshot()
            if previous.get("running"):
                raise RuntimeError("An Agent request is already running.")
            if previous.get("run_id") and (
                previous.get("browser"), previous.get("platform")
            ) != (settings.browser, settings.platform):
                raise RuntimeError("The selected Agent session belongs to another browser or provider. Start a new session.")
            reason = self._capacity_reason(active, settings.browser, settings.platform)
            if reason:
                raise RuntimeError(reason)
            conversation = normalize_agent_conversation_url(settings.platform, target_url)
            if conversation and any(
                normalize_agent_conversation_url(item.get("platform", ""), item.get("conversation_url", "")) == conversation
                for item in active
            ):
                raise RuntimeError("This conversation already has a running Agent task. Select that session to view or stop it.")
            try:
                workspace_identities = self._workspace_identity_chain(workspace)
            except (OSError, RuntimeError, ValueError) as exc:
                raise RuntimeError(
                    "The selected workspace identity could not be verified safely."
                ) from exc
            workspace_reason = self._workspace_reason(
                active,
                workspace,
                read_only=bool(read_only),
                workspace_identities=workspace_identities,
            )
            if workspace_reason:
                raise RuntimeError(workspace_reason)
            compute_reason = self._active_compute_job_reason(
                workspace,
                read_only=bool(read_only),
                workspace_identities=workspace_identities,
            )
            if compute_reason:
                raise RuntimeError(compute_reason)
            self._workspace_leases[service] = ("", workspace_identities)
            try:
                yield
            finally:
                latest = service.snapshot()
                if latest.get("running"):
                    self._workspace_leases[service] = (
                        str(latest.get("run_id") or ""),
                        workspace_identities,
                    )
                else:
                    self._workspace_leases.pop(service, None)

    def _snapshot_rows(self):
        """Attach fixed admission identities to running snapshots and release stale leases."""
        snapshots = {}
        active_services = set()
        for service in self._services.values():
            snapshot = service.snapshot()
            if snapshot.get("running"):
                active_services.add(service)
                run_id = str(snapshot.get("run_id") or "")
                lease = self._workspace_leases.get(service)
                if lease is None or lease[0] != run_id:
                    try:
                        identities = self._workspace_identity_chain(
                            snapshot.get("workspace_path", "")
                        )
                    except (OSError, RuntimeError, ValueError):
                        identities = ()
                    lease = (run_id, identities)
                    self._workspace_leases[service] = lease
                snapshot = dict(snapshot)
                snapshot["_workspace_identity_chain"] = lease[1]
            snapshots[service] = snapshot
        for service in tuple(self._workspace_leases):
            if service not in active_services:
                self._workspace_leases.pop(service, None)
        return snapshots

    @staticmethod
    def _workspace_identity_chain(workspace):
        """Return the selected directory and every ancestor as stable identities."""
        resolved = Path(str(workspace or "")).expanduser().resolve(strict=True)
        identities = []
        for directory in (resolved, *resolved.parents):
            metadata = directory.stat()
            if not stat_module.S_ISDIR(metadata.st_mode):
                raise ValueError("Workspace identity components must be directories.")
            identities.append((int(metadata.st_dev), int(metadata.st_ino)))
        return tuple(identities)

    @classmethod
    def _workspace_identity(cls, workspace):
        """Return one stable directory identity with a normalized-path fallback."""
        candidate = Path(str(workspace or "")).expanduser()
        try:
            device, inode = cls._workspace_identity_chain(candidate)[0]
            return ("directory", device, inode)
        except (OSError, RuntimeError, ValueError):
            try:
                fallback = candidate.resolve(strict=False)
            except (OSError, RuntimeError, ValueError):
                fallback = candidate.absolute()
            return ("path", os.path.normcase(str(fallback)))

    @staticmethod
    def _identity_chains_overlap(left, right):
        """Return whether either selected root is an ancestor of the other."""
        return bool(left and right and (left[0] in right or right[0] in left))

    @classmethod
    def _workspaces_overlap(cls, left, right):
        """Return whether two selected roots can address any of the same files."""
        try:
            left_identities = cls._workspace_identity_chain(left)
            right_identities = cls._workspace_identity_chain(right)
        except (OSError, RuntimeError, ValueError):
            return False
        return cls._identity_chains_overlap(left_identities, right_identities)

    def _workspace_reason(
        self,
        active,
        workspace,
        *,
        read_only,
        workspace_identities=None,
    ):
        if read_only:
            return ""
        if workspace_identities is None:
            try:
                workspace_identities = self._workspace_identity_chain(workspace)
            except (OSError, RuntimeError, ValueError):
                return self._WORKSPACE_UNVERIFIED
        for item in active:
            if bool(item.get("read_only")):
                continue
            active_workspace = str(item.get("workspace_path") or "").strip()
            active_identities = item.get("_workspace_identity_chain")
            if not active_identities and active_workspace:
                try:
                    active_identities = self._workspace_identity_chain(active_workspace)
                except (OSError, RuntimeError, ValueError):
                    active_identities = ()
            if not active_identities:
                return self._WORKSPACE_UNVERIFIED
            if self._identity_chains_overlap(
                active_identities,
                workspace_identities,
            ):
                return self._WORKSPACE_CONFLICT
        return ""

    @classmethod
    def _compute_job_identity_chain(cls, metadata):
        """Return the strongest verified identity chain available for one job."""
        persisted_identity = compute_job_workspace_identity(metadata)
        try:
            path_identities = cls._workspace_identity_chain(metadata.get("workspace", ""))
        except (OSError, RuntimeError, ValueError):
            path_identities = ()
        if persisted_identity is None:
            return path_identities, bool(path_identities)
        if path_identities and path_identities[0] == persisted_identity:
            return path_identities, True
        return (persisted_identity,), False

    def _active_compute_job_reason(
        self,
        workspace,
        *,
        read_only,
        workspace_identities=None,
    ):
        """Keep a detached workspace worker from overlapping a new writer."""
        if read_only:
            return ""
        if workspace_identities is None:
            try:
                workspace_identities = self._workspace_identity_chain(workspace)
            except (OSError, RuntimeError, ValueError):
                return self._COMPUTE_JOB_UNVERIFIED
        scanned_roots = set()
        scanned_records = 0
        for service in self._services.values():
            runtime_root = Path(service._runtime_root).expanduser()
            try:
                runtime_key = str(runtime_root.resolve(strict=False))
            except (OSError, RuntimeError, ValueError):
                return self._COMPUTE_JOB_UNVERIFIED
            if runtime_key in scanned_roots:
                continue
            if len(scanned_roots) >= self._MAX_COMPUTE_RUNTIME_ROOTS:
                return self._COMPUTE_JOB_UNVERIFIED
            scanned_roots.add(runtime_key)
            remaining_records = MAX_METADATA_SCAN_RECORDS - scanned_records
            try:
                records = scan_compute_job_metadata(
                    runtime_root,
                    maximum_records=max(1, remaining_records),
                    reconcile_liveness=True,
                )
            except (ComputeJobError, OSError, RuntimeError, ValueError):
                return self._COMPUTE_JOB_UNVERIFIED
            if len(records) > remaining_records:
                return self._COMPUTE_JOB_UNVERIFIED
            scanned_records += len(records)
            for metadata in records:
                if metadata.get("state") not in ACTIVE_STATES:
                    continue
                try:
                    job_identities, ancestry_verified = (
                        self._compute_job_identity_chain(metadata)
                    )
                except ComputeJobError:
                    return self._COMPUTE_JOB_UNVERIFIED
                if not job_identities:
                    return self._COMPUTE_JOB_UNVERIFIED
                if self._identity_chains_overlap(
                    job_identities,
                    workspace_identities,
                ):
                    return self._COMPUTE_JOB_CONFLICT
                if not ancestry_verified:
                    return self._COMPUTE_JOB_UNVERIFIED
        return ""

    def _capacity_reason(self, active, browser, platform):
        if self._closed:
            return "The Agent service is shutting down."
        effective_limit = self._concurrency_limit(browser)
        if len(active) >= effective_limit:
            if effective_limit == 1:
                return (
                    "The Windows debug browser supports one active Agent task at a time. "
                    "Wait for it to finish or stop it."
                )
            return "Both Agent slots are in use (2 of 2). Wait for a task to finish or stop one session."
        if active and (
            (browser, platform) != ("edge", "chatgpt")
            or any((item.get("browser"), item.get("platform")) != ("edge", "chatgpt") for item in active)
        ):
            return "Concurrent execution is available only for ChatGPT in Edge. Wait for the active task to finish."
        return ""

    def _concurrency_limit(self, browser):
        """Keep the shared Windows CDP browser single-owner."""
        if is_windows_host() and browser in {"edge", "chrome"}:
            return 1
        return self.limit

    def start(self, session_id, *args, **kwargs):
        with self._lock:
            if self._closed:
                raise RuntimeError("The Agent service is shutting down.")
            created = session_id == "new"
            if created:
                session_id = uuid4().hex
                self._services[session_id] = self._create(session_id)
            service = self.get(session_id)
            try:
                service.start(*args, **kwargs)
            except Exception:
                if created and not service.snapshot().get("run_id"):
                    self._services.pop(session_id, None)
                raise
            return session_id

    def catalog(self, browser, platform, workspace, *, read_only=False):
        with self._lock:
            snapshot_rows = self._snapshot_rows()
            snapshots = [
                (key, snapshot_rows[service])
                for key, service in self._services.items()
            ]
            active = [item for _, item in snapshots if item.get("running")]
            reason = self._capacity_reason(active, browser, platform) or self._workspace_reason(
                active,
                workspace,
                read_only=bool(read_only),
            )
            if not reason:
                reason = self._active_compute_job_reason(
                    workspace,
                    read_only=bool(read_only),
                )
        active_count = len(active)
        fields = ("workspace_path", "project_url", "conversation_url", "session_title", "running", "paused", "phase", "message", "started_at", "finished_at", "run_id", "read_only")
        sessions = [
            {"session_id": key, **{field: item.get(field) for field in fields}}
            for key, item in snapshots
            if item.get("run_id") and (item.get("browser"), item.get("platform"))
            == (browser, platform)
        ]
        sessions.sort(key=lambda item: (not item["running"], str(item["started_at"] or "")))
        return {"sessions": sessions, "active_count": active_count, "concurrency_limit": self._concurrency_limit(browser),
                "can_start": not reason, "start_blocked_reason": reason}

    def has_active_worker(self, browser, platform=None) -> bool:
        """Return whether a matching browser worker is currently running."""
        with self._lock:
            return any(
                snapshot.get("running")
                and snapshot.get("browser") == browser
                and (platform is None or snapshot.get("platform") == platform)
                for snapshot in (
                    service.snapshot() for service in self._services.values()
                )
            )

    def stop_at_exit(self):
        with self._lock:
            self._closed = True
            services = list(self._services.values())
        for service in services:
            service.request_stop()
        for service in services:
            service.stop_at_exit()
        with self._lock:
            self._workspace_leases.clear()
