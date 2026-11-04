"""Shared fakes for the ported iPhone tests: a synthetic WDA channel and frame data.

Ported from iPhone Use 0.3.10 tests/test_controller.py, tests/test_screen.py and
tests/test_latency.py (MIT, Copyright (c) 2026 ZHONG XIN); see app/core/iphone/LICENSE-iphone-use.txt.
Nothing here contacts a device, WebDriverAgent, or the network.

Code version: v1.0.0-claude.0
"""
import base64
import copy
import re
import struct
import time
import xml.etree.ElementTree as ET
import zlib

from app.core.iphone import wda_image
from app.core.iphone.wda_client import WDAError
from app.core.iphone.wda_controller import ELEMENT_KEY


def node(label="Row 1", y=200, kind="Cell", **extra):
    return {"type": "XCUIElementType" + kind, "label": label, "name": label,
            "x": 60, "y": y, "width": 250, "height": 44, **extra}


class FakeWDA:
    def __init__(self):
        self.calls = []
        self.timeouts = []
        self.session_id = None
        self.locked = False
        self.status_ready = True
        self.app = "com.example.phone"
        self.size = {"width": 390, "height": 844}
        self.nodes = [node()]
        self.source_pages = None
        self.swipe_count = 0
        self.elements = [{"id": "target", "label": "Target", "kind": "XCUIElementTypeTextField",
                          "rect": {"x": 70, "y": 200, "width": 250, "height": 44},
                          "hittable": True, "value": ""}]
        self.input_override = None
        self.click_error = None
        self.home_effective = True
        self.home_error = None
        self.activate_effective = True
        self.activate_error = None
        self.foreground_sequence = None
        self.gesture_effects = None
        self.focused = None
        self.rich_elements = False
        self.source_root = None
        self.settings = []
        self.screenshot = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jXuoAAAAASUVORK5CYII=")

    def request(self, method, path, payload=None, timeout=None):
        self.calls.append((method, path, payload))
        if method == "POST" and path == "/wda/homescreen":
            if self.home_error:
                raise self.home_error
            if self.home_effective:
                self.app = "com.apple.springboard"
            return {"value": None}
        if path == "/wda/activeAppInfo":
            if self.foreground_sequence:
                self.app = self.foreground_sequence.pop(0)
            return {"value": {"bundleId": self.app}}
        if path == "/screenshot":
            return {"value": base64.b64encode(self.screenshot).decode("ascii")}
        if path == "/wda/locked":
            return {"value": self.locked}
        if path == "/status":
            return {"value": {"ready": self.status_ready}}
        raise AssertionError("Unexpected request: " + path)

    def ensure_session(self):
        if self.session_id is None:
            self.calls.append(("POST", "/session", None))
            self.session_id = "fake-session"
        return self.session_id

    def source(self):
        nodes = self.nodes if self.source_pages is None else self.source_pages[min(self.swipe_count, len(self.source_pages)-1)]
        # A real WDA source root is the application itself, with its bundle ID and screen size.
        root = ET.Element("AppiumAUT") if self.source_root is None else ET.Element(
            "XCUIElementTypeApplication", {"type": "XCUIElementTypeApplication", "x": "0", "y": "0",
                                           **{k: str(v) for k, v in self.source_root.items()}})
        for item in nodes:
            ET.SubElement(root, item["type"], {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in item.items()})
        return ET.tostring(root, encoding="unicode")

    def session(self, method, path, payload=None, timeout=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        self.timeouts.append((method, path, timeout))
        if path.startswith("/source"):
            return self.source()
        if path == "/window/size":
            return self.size.copy()
        if path == "/element/active":
            if self.focused is None:
                raise WDAError("no such element", "No element has keyboard focus")
            answer = {ELEMENT_KEY: self.focused["id"]}
            return {**answer, "type": self.focused["kind"]} if self.rich_elements else answer
        if path == "/elements":
            value = payload["value"]
            labels = re.findall(r"label == '([^']*)'", value)
            matches = [item for item in self.elements if not labels or item["label"] == labels[0]]
            enabled = re.search(r"enabled == (true|false)", value)
            if enabled:
                matches = [item for item in matches if item.get("enabled", True) is (enabled[1] == "true")]
            if "CONTAINS" in value:
                fragment = re.search(r"label CONTAINS '([^']*)'", value)[1]
                matches = [item for item in self.elements if fragment in item["label"]]
            if not self.rich_elements:
                return [{ELEMENT_KEY: item["id"]} for item in matches]
            # The pinned WDA answers like this once compact responses are switched off.
            return [{ELEMENT_KEY: item["id"], "ELEMENT": item["id"], "type": item["kind"], "label": item["label"],
                     "rect": item["rect"].copy(), "enabled": item.get("enabled", True),
                     "attribute/name": item.get("name", item["label"]), "attribute/value": item["value"] or None}
                    for item in matches]
        if path.startswith("/element/"):
            parts = path.split("/")
            element = next(item for item in self.elements if item["id"] == parts[2])
            suffix = "/".join(parts[3:])
            if suffix == "rect":
                return element["rect"].copy()
            if suffix == "attribute/hittable":
                return element["hittable"]
            if suffix == "attribute/type":
                return element["kind"]
            if suffix == "attribute/value":
                return element["value"]
            if suffix == "click":
                if self.click_error:
                    raise self.click_error
                self.focused = element
                return None
            if suffix == "clear":
                element["value"] = ""
                return None
            if suffix == "value":
                self.focused = element
                element["value"] = self.input_override if self.input_override is not None else element["value"] + payload["text"]
                return None
        if path in ("/wda/dragfromtoforduration", "/wda/swipe"):
            self.swipe_count += 1
            if self.gesture_effects and self.swipe_count <= len(self.gesture_effects):
                effect = self.gesture_effects[self.swipe_count - 1]
                if effect:
                    effect()
            return None
        if path == "/wda/apps/activate":
            if self.activate_error:
                raise self.activate_error
            if self.activate_effective:
                self.app = payload["bundleId"]
            return None
        if path == "/wda/keys":
            text = "".join(payload["value"])
            if self.focused is not None and text != "\n" and self.input_override is None:
                self.focused["value"] += text
            return None
        if path in ("/wda/tap", "/wda/pressButton"):
            return None
        if path == "/appium/settings":
            self.settings.append(payload["settings"])
            return {}
        raise AssertionError("Unexpected session request: " + path)

    def actions(self):
        return [(method, path, body) for method, path, body in self.calls
                if method == "POST" and path not in ("/elements", "/appium/settings")]

    def reapply_settings(self):
        pass

    def metrics(self):
        return {"retained_requests": len(self.calls)}

    def clear_metrics(self):
        pass


def jpeg(width=440, height=956, payload=b"pixels"):
    # Synthetic SOF header; the preview parser deliberately does not decode pixels.
    header = bytes([8]) + height.to_bytes(2, "big") + width.to_bytes(2, "big") + b"\x01\x01\x11\x00"
    return b"\xff\xd8\xff\xe0\x00\x04AB\xff\xc0\x00\x0b" + header + b"\xff\xda\x00\x02" + payload + b"\xff\xd9"


def eventually(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for owned test stream.")


def png(width, height):
    """A valid grayscale PNG of the given size, built without an image library."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    rows = (b"\x00" + bytes(width)) * height
    return (wda_image.PNG_SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 1)) + chunk(b"IEND", b""))
