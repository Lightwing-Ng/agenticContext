"""Inspect the rendered WeChat page with standard-library HTML parsing.

Code version: v1.0.0-codex.0
"""

from html.parser import HTMLParser
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
