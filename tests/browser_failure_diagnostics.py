"""Bounded failure notes for local resource browser integration tests.

Code version: v1.0.0
"""

from collections import deque
import json
from urllib.parse import urlsplit

from playwright.sync_api import Page, Response


class ResourceScriptDiagnostics:
    """Keep delivery and initialization evidence attached to the original failure."""

    def __init__(self, page: Page) -> None:
        self.page = page
        self.responses: deque[dict[str, object]] = deque(maxlen=12)
        self.errors: deque[str] = deque(maxlen=6)
        self.documents: deque[str] = deque(maxlen=6)
        page.on("response", self._record_response)
        page.on("pageerror", lambda error: self.errors.append(str(error)[:500]))
        page.on("console", self._record_console)
        page.on("domcontentloaded", lambda: self.documents.append(page.url[:500]))

    def _record_response(self, response: Response) -> None:
        if urlsplit(response.url).path.rsplit("/", 1)[-1] not in {
            "browser-search.js", "browser-source-filter.js", "fuse.min.mjs",
        }:
            return
        self.responses.append({
            "url": response.url[:500],
            "status": response.status,
            "content_type": response.headers.get("content-type", "")[:100],
        })

    def _record_console(self, message) -> None:
        if message.type == "error":
            self.errors.append(message.text[:500])

    def annotate(self, error: Exception) -> None:
        error.add_note("Resource browser diagnostics: " + json.dumps({
            "url": self.page.url[:500],
            "scripts": list(self.responses),
            "browser_errors": list(self.errors),
            "domcontentloaded": list(self.documents),
        }, ensure_ascii=True))
