"""Adapt observed ChatGPT component text for display without changing stored history.

Code version: v1.0.0-codex.0
Component attributes remain inert source data. This adapter never emits HTML or
interprets JSX expressions, and unsupported components retain their source text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
import re
import unicodedata

from markdown_it import MarkdownIt


_MARKDOWN = MarkdownIt("commonmark", {"html": False})
_ROOT = re.compile(r"(?im)^[ \t]{0,3}<box\b")
_CITATION = re.compile(
    r"<cite\s+ref\s*=\s*(['\"])(turn[0-9]+[A-Za-z0-9_.:-]*)\1\s*"
    r"(?:/\s*>|>\s*</cite\s*>)",
    re.IGNORECASE,
)
_TICKS = re.compile(r"`+")
_PARAGRAPH_BOUNDARY = re.compile(r"\r?\n[ \t]*\r?\n(?=[^\s<])")
_SVG_TAGS = frozenset({
    "svg", "g", "rect", "path", "circle", "line", "polyline", "polygon",
    "ellipse", "text", "tspan", "title", "desc",
})


def _inline_code_ranges(text: str, offset: int) -> list[tuple[int, int]]:
    ranges = []
    runs = list(_TICKS.finditer(text))
    position = 0
    while position < len(runs):
        opening = runs[position]
        slashes = len(text[:opening.start()]) - len(text[:opening.start()].rstrip("\\"))
        escaped = slashes % 2
        opening_length = len(opening.group()) - escaped
        if not opening_length:
            position += 1
            continue
        closing = next((index for index in range(position + 1, len(runs))
                        if len(runs[index].group()) == opening_length), None)
        if closing is None:
            position += 1
            continue
        ranges.append((offset + opening.start() + escaped, offset + runs[closing].end()))
        position = closing + 1
    return ranges


def _protect_code(text: str) -> tuple[str, dict[str, str]]:
    offsets = [0] + [match.end() for match in re.finditer(r"\r\n|\r|\n", text)]
    if offsets[-1] != len(text):
        offsets.append(len(text))
    blocks = sorted({(offsets[token.map[0]], offsets[token.map[1]])
                     for token in _MARKDOWN.parse(text)
                     if token.type in {"fence", "code_block"} and token.map})
    ranges = []
    start = 0
    for left, right in blocks:
        ranges.extend(_inline_code_ranges(text[start:left], start))
        ranges.append((left, right))
        start = right
    ranges.extend(_inline_code_ranges(text[start:], start))
    prefix = "CHATGPTDISPLAYPROTECTEDCODE"
    while prefix in text:
        prefix += "X"
    replacements = {}
    pieces = []
    start = 0
    for index, (left, right) in enumerate(ranges):
        key = f"{prefix}{index}END"
        value = text[left:right]
        ending = "\r\n" if value.endswith("\r\n") else "\n" if value.endswith("\n") else "\r" if value.endswith("\r") else ""
        replacements[key] = value[:-len(ending)] if ending else value
        pieces.extend((text[start:left], key, ending))
        start = right
    pieces.append(text[start:])
    return "".join(pieces), replacements


def _restore_code(text: str, replacements: dict[str, str]) -> str:
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


class _CitationParser(HTMLParser):
    """Locate actual citation tags, excluding comments and quoted attributes."""

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.offsets = [0] + [match.end() for match in re.finditer("\n", source)]
        self.starts: set[int] = set()

    def handle_starttag(self, tag, attrs):
        if tag == "cite":
            line, column = self.getpos()
            self.starts.add(self.offsets[line - 1] + column)

    handle_startendtag = handle_starttag


def _remove_citations(text: str) -> str:
    if not _CITATION.search(text):
        return text
    parser = _CitationParser(text)
    try:
        parser.feed(text)
        parser.close()
    except AssertionError:
        # HTMLParser rejects malformed marked sections; preserve uncertain source.
        return text
    pieces = []
    start = 0
    for match in _CITATION.finditer(text):
        if match.start() not in parser.starts:
            continue
        left = text[start:match.start()]
        right = text[match.end():]
        if not right or right[0].isspace() or unicodedata.category(right[0]).startswith("P"):
            left = left.rstrip(" \t")
        pieces.append(left)
        before = "".join(pieces)[-1:]
        if before and right and before.isascii() and right[0].isascii():
            if before.isalnum() and right[0].isalnum():
                pieces.append(" ")
        start = match.end()
    pieces.append(text[start:])
    return "".join(pieces)


@dataclass
class _Node:
    tag: str
    children: list["_Node | str"] = field(default_factory=list)


class _ComponentParser(HTMLParser):
    """Read a balanced component tree while retaining an exact source boundary."""

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source = source
        self.offsets = [0] + [match.end() for match in re.finditer("\n", source)]
        self.root: _Node | None = None
        self.stack: list[_Node] = []
        self.end: int | None = None
        self.valid = True

    def handle_starttag(self, tag, attrs):
        if self.end is not None:
            return
        node = _Node(tag)
        if len(self.stack) >= 64:
            self.valid = False
        if self.stack:
            self.stack[-1].children.append(node)
        elif self.root is None:
            self.root = node
        self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.end is not None or not self.stack:
            return
        if self.stack[-1].tag == tag:
            self.stack.pop()
        else:
            self.valid = False
            matches = [index for index, node in enumerate(self.stack) if node.tag == tag]
            if not matches:
                return
            del self.stack[matches[-1]:]
        if not self.stack:
            line, column = self.getpos()
            position = self.offsets[line - 1] + column
            self.end = self.source.find(">", position) + 1

    def handle_data(self, data):
        if self.end is None and self.stack:
            self.stack[-1].children.append(data)

    def handle_comment(self, data):
        if self.end is None:
            self.valid = False

    def handle_decl(self, decl):
        if self.end is None:
            self.valid = False

    def handle_pi(self, data):
        if self.end is None:
            self.valid = False


def _text(node: _Node) -> str:
    return "".join(child if isinstance(child, str) else _text(child) for child in node.children).strip()


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", r"\|")


def _row(node: _Node) -> list[str]:
    cells = []
    for child in node.children:
        if isinstance(child, str):
            value = child.strip()
        elif child.tag in {"text", "title"} and all(isinstance(item, str) for item in child.children):
            value = _text(child)
        else:
            raise ValueError("Unsupported row structure.")
        if value:
            cells.append(_cell(value))
    if len(cells) != 2:
        raise ValueError("Only observed two-cell rows are supported.")
    return cells


def _diagram_labels(node: _Node) -> list[str]:
    if node.tag not in _SVG_TAGS:
        raise ValueError("Unsupported diagram component.")
    if node.tag in {"text", "tspan", "title", "desc"}:
        for child in node.children:
            if isinstance(child, _Node):
                _diagram_labels(child)
        text = _text(node)
        return [_cell(text)] if text else []
    labels = []
    for child in node.children:
        if isinstance(child, _Node):
            labels.extend(_diagram_labels(child))
        elif child.strip():
            raise ValueError("Unsupported diagram text location.")
    return labels


def _render_box(node: _Node) -> str:
    if node.tag != "box":
        raise ValueError("Unsupported root component.")
    blocks = []
    rows = []

    def flush_rows():
        if rows:
            blocks.append("\n".join([
                "| " + " | ".join(rows[0]) + " |", "| --- | --- |",
                *("| " + " | ".join(row) + " |" for row in rows[1:]),
            ]))
            rows.clear()

    for child in node.children:
        if isinstance(child, str) and not child.strip():
            continue
        if isinstance(child, _Node) and child.tag == "row":
            rows.append(_row(child))
            continue
        flush_rows()
        if isinstance(child, str):
            rendered = child.strip()
        elif child.tag == "box":
            rendered = _render_box(child)
        elif child.tag == "svg":
            labels = _diagram_labels(child)
            rendered = "Diagram labels: " + "; ".join(labels) if labels else ""
        elif child.tag in {"caption", "text", "title"} and all(isinstance(item, str) for item in child.children):
            rendered = _text(child)
        else:
            raise ValueError("Unsupported box structure.")
        if rendered:
            blocks.append(rendered)
    flush_rows()
    if not blocks:
        raise ValueError("Component has no readable content.")
    return "\n\n".join(blocks)


def _fenced_source(source: str, replacements: dict[str, str]) -> str:
    source = _restore_code(source, replacements)
    fence = "`" * max(3, 1 + max((len(match.group()) for match in _TICKS.finditer(source)), default=0))
    return f"{fence}text\n{source}\n{fence}"


def format_chatgpt_display_markdown(value: object) -> str:
    """Format known component text outside code, preserving ordinary Markdown bytes."""
    text = str(value or "")
    if not _ROOT.search(text) and not _CITATION.search(text):
        return text
    masked, replacements = _protect_code(text)
    masked = _remove_citations(masked)
    pieces = []
    start = 0
    while match := _ROOT.search(masked, start):
        root_start = match.start() + len(match.group()) - len(match.group().lstrip())
        parser = _ComponentParser(masked[root_start:])
        try:
            parser.feed(masked[root_start:])
            parser.close()
        except AssertionError:
            # A complete preceding component is still usable when later prose is malformed.
            if parser.end is None:
                parser.valid = False
        length = parser.end
        if length is None:
            boundary = _PARAGRAPH_BOUNDARY.search(masked, root_start)
            length = (boundary.start() if boundary else len(masked)) - root_start
        source = masked[match.start():root_start + length]
        try:
            if parser.end is None or not parser.valid or parser.root is None:
                raise ValueError("Unbalanced component source.")
            if any(key in source and any(character in value for character in "|\r\n")
                   for key, value in replacements.items()):
                raise ValueError("Complex code must retain its exact component source.")
            rendered = _render_box(parser.root)
        except (ValueError, RecursionError):
            rendered = _fenced_source(source, replacements)
        pieces.extend((masked[start:match.start()], rendered))
        start = root_start + length
    pieces.append(masked[start:])
    return _restore_code("".join(pieces), replacements)
