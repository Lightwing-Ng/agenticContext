"""Bounded, independently controlled Web Agent sessions.

Code version: v1.0.0-codex.1
"""

from contextlib import contextmanager
from functools import partial
import re
from threading import RLock
from uuid import uuid4

from app.core.computer_use_agent import (
    ComputerUseAgentService,
    normalize_agent_conversation_url,
    _path_crosses_link_like_component,
)


class AgentSessionPool:
    """Keep two Edge ChatGPT workers, with admission held through worker startup."""

    limit = 2

    def __init__(self, primary):
        self._lock = RLock()
        self._closed = False
        self._primary = primary
        self._root = primary._runtime_root / "sessions"
        self._services = {"primary": primary}
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
    def _admit(self, service, settings, target_url):
        with self._lock:
            if self._closed:
                raise RuntimeError("The Agent service is shutting down.")
            snapshots = [item.snapshot() for item in self._services.values()]
            active = [item for item in snapshots if item.get("running")]
            if len(active) >= self.limit:
                raise RuntimeError("Both Agent slots are in use (2 of 2). Wait for a task to finish or stop one session.")
            if active and (
                (settings.browser, settings.platform) != ("edge", "chatgpt")
                or any((item.get("browser"), item.get("platform")) != ("edge", "chatgpt") for item in active)
            ):
                raise RuntimeError("Concurrent execution is available only for ChatGPT in Edge. Wait for the active task to finish.")
            conversation = normalize_agent_conversation_url(settings.platform, target_url)
            if conversation and any(
                normalize_agent_conversation_url(item.get("platform", ""), item.get("conversation_url", "")) == conversation
                for item in active
            ):
                raise RuntimeError("This conversation already has a running Agent task. Select that session to view or stop it.")
            yield

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

    def catalog(self, browser, platform, workspace):
        with self._lock:
            snapshots = [(key, service.snapshot()) for key, service in self._services.items()]
        active_count = sum(bool(item.get("running")) for _, item in snapshots)
        fields = ("session_title", "running", "paused", "phase", "message", "started_at", "finished_at", "run_id")
        sessions = [
            {"session_id": key, **{field: item.get(field) for field in fields}}
            for key, item in snapshots
            if item.get("run_id") and (item.get("browser"), item.get("platform"), item.get("workspace_path"))
            == (browser, platform, workspace)
        ]
        sessions.sort(key=lambda item: (not item["running"], str(item["started_at"] or "")))
        return {"sessions": sessions, "active_count": active_count, "concurrency_limit": self.limit}

    def stop_at_exit(self):
        with self._lock:
            self._closed = True
            services = list(self._services.values())
        for service in services:
            service.request_stop()
        for service in services:
            service.stop_at_exit()
