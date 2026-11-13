"""Inspect the rendered WeChat page with standard-library HTML parsing.

Code version: v1.2.0-codex.0
"""

from html.parser import HTMLParser
import time
from urllib.parse import urljoin
from xml.etree.ElementTree import Element, SubElement


_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
})


class _BrowserMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.root = Element("document")
        self.stack = [self.root]

    def _add_element(self, tag, attributes):
        return SubElement(
            self.stack[-1], tag, {key: value or "" for key, value in attributes},
        )

    def handle_starttag(self, tag, attributes):
        element = self._add_element(tag, attributes)
        if tag not in _VOID_TAGS:
            self.stack.append(element)

    def handle_startendtag(self, tag, attributes):
        self._add_element(tag, attributes)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, value):
        element = self.stack[-1]
        if len(element):
            last_child = element[-1]
            last_child.tail = (last_child.tail or "") + value
        else:
            element.text = (element.text or "") + value


def parse_browser_markup(data: bytes) -> Element:
    """Return an element tree for the explicit tags of a rendered browser view."""
    parser = _BrowserMarkup()
    parser.feed(data.decode("utf-8"))
    parser.close()
    return parser.root


def _wait_for_saved_view_task(fetch_status, *, task_id, generation):
    deadline = time.monotonic() + 3
    while True:
        status = fetch_status()
        assert status["task_id"] == task_id
        assert status["generation"] == generation
        if status["state"] != "running":
            break
        assert time.monotonic() < deadline, "The saved-view task did not finish."
        time.sleep(0.01)
    assert status["state"] == "succeeded", status["message"]


def get_verified_browser_page(client, path, **kwargs):
    """Follow one saved-view task to render the HTML a browser would display."""
    response = client.get(path, **kwargs)
    assert response.status_code == 200
    if b"data-wechat-read-pending" not in response.data:
        return response

    shell = parse_browser_markup(response.data).find(".//*[@data-wechat-read-pending]")
    assert shell is not None and shell.get("data-state") == "running"

    def fetch_status():
        poll = client.get(shell.get("data-status-url"))
        assert poll.status_code == 200
        return poll.get_json()

    _wait_for_saved_view_task(
        fetch_status,
        task_id=shell.get("data-task-id"),
        generation=int(shell.get("data-generation")),
    )
    response = client.get(path, **kwargs)
    assert response.status_code == 200
    assert b"data-wechat-read-pending" not in response.data
    return response


def reload_browser_after_verification(page):
    """Load the verified response when page scripts are disabled."""
    shell = page.locator("[data-wechat-read-pending]")
    if not shell.count():
        return

    assert shell.get_attribute("data-state") == "running"
    status_url = urljoin(page.url, shell.get_attribute("data-status-url"))

    def fetch_status():
        poll = page.request.get(status_url)
        assert poll.status == 200
        return poll.json()

    _wait_for_saved_view_task(
        fetch_status,
        task_id=shell.get_attribute("data-task-id"),
        generation=int(shell.get_attribute("data-generation")),
    )
    response = page.reload(wait_until="domcontentloaded")
    assert response is not None and response.status == 200
    assert not shell.count()
