"""Unlock recovery uses real shared preview state and a fake WDA channel.

Ported from iPhone Use 0.3.10 tests/test_preview_recovery.py
(MIT, Copyright (c) 2026 ZHONG XIN); see app/core/iphone/LICENSE-iphone-use.txt.

Code version: v1.0.0-claude.0
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import pytest

pytest.importorskip("fcntl")

from app.core.iphone.mcp_server import Runtime  # noqa: E402
from app.core.iphone.wda_client import WDAError  # noqa: E402
from app.core.iphone.wda_controller import PhoneController  # noqa: E402
from app.core.iphone.wda_screen import ScreenHub  # noqa: E402
from tests.iphone_wda_fakes import FakeWDA, jpeg  # noqa: E402


class PreviewRecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = Runtime(directory.name)
        self.addCleanup(self.runtime.close)
        self.client = FakeWDA()
        self.client.close = Mock()
        self.runtime.client = self.client
        self.runtime.phone = PhoneController(self.client, directory.name)
        self.runtime.phone.screen = self.runtime.screen
        self.hub = self.runtime.screen
        self.other = ScreenHub(directory.name)
        self.addCleanup(self.other.close)
        self.runtime.setup_manager.mirroring_running = Mock(return_value=False)

    def ready(self):
        return self.runtime.call("pua_ready", {"screenshot": False, "recover": False})

    def lock_failure(self):
        self.client.locked = True
        with self.assertRaises(WDAError) as caught:
            self.ready()
        self.assertEqual(caught.exception.code, "phone_locked")

    def test_unlock_ready_clears_shared_lock_pause_and_allows_new_pixels(self):
        self.lock_failure()
        self.assertEqual(self.other.start()["pause_reason"], "device_locked")
        self.assertIsNone(self.other.start()["frame"])
        self.client.locked = False
        result = self.ready()
        self.assertTrue(result["ready"])
        self.assertEqual(result["preview"], {"paused": False, "pause_reason": None})
        self.assertFalse(self.other.paused())
        self.other._publish_frame(jpeg(), 440, 956, self.other._stop)
        self.assertTrue(self.other._wire(self.other._read_state())["frame_available"])
        # READY may create a session, but performs no user gesture or input.
        self.assertEqual([p for _, p, _ in self.client.actions()], ["/session"])
        self.assertFalse(any(p == "/screenshot" for _, p, _ in self.client.calls))

    def test_explicit_and_legacy_pause_survive_ready_but_user_refresh_can_resume(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                self.hub.set_paused(False)
                if legacy:
                    self.hub._state_path.write_text(json.dumps({"paused": True}))
                else:
                    self.runtime.call("pua_screen", {"action": "pause"})
                self.lock_failure()
                reason = "unknown" if legacy else "authentication"
                self.assertEqual(self.other.start()["pause_reason"], reason)
                self.client.locked = False
                self.assertTrue(self.ready()["preview"]["paused"])
                self.assertTrue(self.runtime.call("pua_screen", {})["paused"])
                probe = FakeWDA()
                probe.close = Mock()
                with patch("app.core.iphone.mcp_server.WDAClient", return_value=probe):
                    self.assertFalse(self.runtime.screen_action("refresh")["paused"])
                self.assertEqual(probe.calls, [("GET", "/status", None), ("GET", "/wda/locked", None)])
                self.assertFalse(self.runtime.call("pua_screen", {"action": "resume"})["paused"])

    def test_failed_health_check_does_not_clear_a_lock_pause(self):
        self.lock_failure()
        self.client.locked = False
        self.client.status_ready = False
        with self.assertRaises(WDAError):
            self.ready()
        self.assertEqual(self.hub.pause_status(), {"paused": True, "pause_reason": "device_locked"})

    def test_new_pause_during_unlock_check_cannot_be_cleared(self):
        for reason in ("authentication", "device_locked"):
            with self.subTest(reason=reason):
                self.hub.set_paused(False)
                self.lock_failure()
                self.client.locked = False
                observe = self.runtime.phone.observe
                def concurrent_pause(*args, **kwargs):
                    result = observe(*args, **kwargs)
                    self.other.set_paused(True, reason=reason)
                    return result
                with patch.object(self.runtime.phone, "observe", side_effect=concurrent_pause):
                    result = self.ready()
                self.assertTrue(result["ready"])
                self.assertEqual(result["preview"], {"paused": True, "pause_reason": reason})

    def test_user_refresh_resumes_only_a_verified_unlocked_lock_pause(self):
        for condition in ("unlocked", "locked", "service_offline", "probe_error", "new_auth_pause"):
            with self.subTest(condition=condition):
                self.hub.set_paused(False)
                self.lock_failure()
                probe = FakeWDA()
                probe.locked = condition == "locked"
                probe.status_ready = condition != "service_offline"
                probe.close = Mock()
                request = probe.request
                def checked(method, path, payload=None):
                    if path == "/wda/locked":
                        if condition == "probe_error":
                            raise WDAError("pua_unreachable", "unavailable")
                        if condition == "new_auth_pause":
                            self.other.set_paused(True)
                    return request(method, path, payload)
                probe.request = checked
                with patch("app.core.iphone.mcp_server.WDAClient", return_value=probe):
                    result = self.runtime.screen_action("refresh")
                self.assertEqual(result["paused"], condition != "unlocked")
                self.assertEqual(probe.actions(), [])
                self.assertFalse(any(p in ("/screenshot", "/source") for _, p, _ in probe.calls))

    def test_pause_generation_is_private_and_survives_activity_transactions(self):
        self.hub.set_paused(True, reason="device_locked")
        identifier = self.hub.locked_pause_id()
        token = self.other.begin("ready")
        self.other.end(token)
        self.other.set_viewport({"width": 440, "height": 956})
        self.assertEqual(self.other.locked_pause_id(), identifier)
        self.assertNotIn(identifier, json.dumps(self.hub.start()))
        self.assertTrue(self.other.resume_after_unlock(identifier))
        self.assertFalse(self.hub.resume_after_unlock(identifier))

    def test_no_focus_returns_visual_replan_without_typing_or_proposed_old_point(self):
        with self.assertRaises(WDAError) as caught:
            self.runtime.call("pua_type_text", {"text": "query"})
        error = caught.exception
        self.assertEqual(error.code, "no_focused_field")
        self.assertFalse(error.details["action_executed"])
        recovery = error.details["recovery"]
        self.assertIn("popup", recovery["next_step"])
        self.assertIn("do not repeat", recovery["next_step"])
        self.assertIn("MCP image content", recovery["next_step"])
        self.assertNotIn("next_arguments", recovery)
        self.assertTrue(Path(error.details["observation"]["image"]["path"]).is_file())
        self.assertEqual(self.client.actions(), [])
        self.assertFalse(self.hub.paused())
