"""Render ChatGPT interactive-message tokens as portable Markdown text.

ChatGPT's conversation mapping embeds private-use control sequences in text parts for
citations, entity chips, generative UI widgets, and highlight spans. The cache keeps the
readable content as Markdown, drops provider-only reference markers, and never invents
links or sources that the mapping did not provide.
"""

# Code version: v1.1.0-claude.0

from __future__ import annotations

import json
import re
import unicodedata

# ChatGPT wraps a provider token as START kind [SEPARATOR payload] END. The code points sit in
# the private-use block, so they are spelled with chr() instead of literal characters.
_START = chr(0xE200)
_END = chr(0xE201)
_SEPARATOR = chr(0xE202)
_REPLACEMENT = chr(0xFFFD)
_HORIZONTAL_SPACE = " \t" + chr(0xA0)
_NEEDS_NORMALIZATION_PATTERN = re.compile(f"[{chr(0xD800)}-{chr(0xDFFF)}{chr(0xE200)}-{chr(0xE2FF)}]")
_PROVIDER_CHARACTER_PATTERN = re.compile(f"[{chr(0xE200)}-{chr(0xE2FF)}]")
_LONE_SURROGATE_PATTERN = re.compile(f"[{chr(0xD800)}-{chr(0xDFFF)}]")
# A terminated token ends at END. A truncated token has no END, so it ends at the line break,
# the next token opener, or the end of the text, and its payload is limited to ASCII reference syntax.
_TOKEN_PATTERN = re.compile(
    rf"{_START}(?P<kind>[^{_START}{_END}{_SEPARATOR}\n]*)"
    rf"(?:{_SEPARATOR}(?P<payload>[^{_START}{_END}\n]*))?{_END}"
    rf"|{_START}(?P<stray>[A-Za-z_]+)(?:{_SEPARATOR}[A-Za-z0-9_.,:/-]*)*"
    rf"(?![^\n{_START}]*{_END})"
)
# Citation, memory, image-reference, and placeholder tokens carry no readable text of their own.
_DROPPED_KINDS = frozenset({"cite", "filecite", "i", "memcite", "map", "entity_metadata"})
# Citation pointers reference search results that the mapping does not expose as text.
_DROPPED_GENUI_KEYS = frozenset({"citation", "refs"})
_HTTP_URL_PATTERN = re.compile(r"https?://[^\s<>]+")
_MARKDOWN_ESCAPE_PATTERN = re.compile(r"([\\`*_\[\]])")


def normalize_chatgpt_markdown(value: object) -> str:
    """Return Markdown with ChatGPT provider tokens rendered as text or removed.

    Text without provider tokens or lone surrogates is returned unchanged, so ordinary messages
    keep their exact bytes and their existing content hashes.
    """
    text = str(value or "")
    if not _NEEDS_NORMALIZATION_PATTERN.search(text):
        return text
    segments = _split_segments(text)
    output = ""
    pending_block_break = False
    for index, segment in enumerate(segments):
        if segment[0] == "text":
            piece = _clean_characters(segment[1])
            if pending_block_break:
                # A block widget already separates paragraphs, so the space after it is not needed.
                piece = piece.lstrip(_HORIZONTAL_SPACE)
                if piece or "\n" in segment[1]:
                    pending_block_break = False
            output += piece
            continue
        rendered, is_block = _render_token(segment[1], segment[2], _is_standalone(segments, index))
        rendered = _clean_characters(rendered)
        if rendered and is_block:
            output = output.rstrip(_HORIZONTAL_SPACE) + f"\n\n{rendered}\n\n"
            pending_block_break = True
        elif rendered:
            output += rendered
        elif _starts_new_word_or_punctuation(segments, index):
            # A dropped inline marker should not leave the space that preceded it behind.
            output = output.rstrip(_HORIZONTAL_SPACE)
    return re.sub(r"\n{3,}", "\n\n", output).strip()


def _clean_characters(value: str) -> str:
    """Remove provider control code points and replace lone surrogates that cannot be encoded."""
    return _LONE_SURROGATE_PATTERN.sub(_REPLACEMENT, _PROVIDER_CHARACTER_PATTERN.sub("", value))


def _split_segments(text: str) -> list[tuple[str, str, str]]:
    """Split text into ("text", value, "") and ("token", kind, payload) segments."""
    segments: list[tuple[str, str, str]] = []
    position = 0
    for match in _TOKEN_PATTERN.finditer(text):
        kind = match.group("kind")
        if kind is None:
            kind = match.group("stray")
        segments.append(("text", text[position:match.start()], ""))
        segments.append(("token", kind, match.group("payload") or ""))
        position = match.end()
    segments.append(("text", text[position:], ""))
    return segments


def _is_standalone(segments: list[tuple[str, str, str]], index: int) -> bool:
    """Return whether a token sits alone on its line, ignoring adjacent provider tokens."""
    for segment in reversed(segments[:index]):
        if segment[0] == "token":
            continue
        if "\n" in segment[1]:
            if segment[1].rsplit("\n", 1)[1].strip():
                return False
            break
        if segment[1].strip():
            return False
    for segment in segments[index + 1:]:
        if segment[0] == "token":
            continue
        if "\n" in segment[1]:
            if segment[1].split("\n", 1)[0].strip():
                return False
            break
        if segment[1].strip():
            return False
    return True


def _starts_new_word_or_punctuation(segments: list[tuple[str, str, str]], index: int) -> bool:
    """Return whether the text after a dropped token begins a word boundary or punctuation.

    Only then may the space before the token be removed; before a letter or digit the space
    separates two words and must stay.
    """
    for segment in segments[index + 1:]:
        if segment[0] == "token" or not segment[1]:
            continue
        first = segment[1][0]
        return first.isspace() or unicodedata.category(first).startswith("P")
    return True


def _render_token(kind: str, payload: str, standalone: bool) -> tuple[str, bool]:
    """Return (markdown, is_block) for one provider token; an empty string drops it."""
    if kind in _DROPPED_KINDS:
        return "", False
    if kind == "entity":
        name = _entity_name(payload)
        if not name:
            return "", False
        if standalone:
            return f"**{_escape_markdown(name)}**", True
        return name, False
    if kind == "url":
        return _render_url(payload), False
    if kind == "navlist":
        title = _clean_line(payload.split(_SEPARATOR, 1)[0])
        return (f"**{_escape_markdown(title)}**", True) if title else ("", False)
    if kind == "genui":
        body = _json_object(payload)
        if body is None:
            return "", False
        blocks = [
            _render_genui_widget(key, value)
            for key, value in body.items()
            if key not in _DROPPED_GENUI_KEYS
        ]
        return "\n\n".join(block for block in blocks if block), True
    if kind == "image_group":
        body = _json_object(payload) or {}
        queries = [_clean_line(item) for item in _string_list(body.get("query"))]
        queries = [query for query in queries if query]
        return (f"*Images: {'; '.join(queries)}*", True) if queries else ("", False)
    body = _json_object(payload)
    if body is None:
        return "", False
    return _fenced_json({kind: body}), True


def _render_genui_widget(key: str, value: object) -> str:
    """Render one generative UI widget; a widget whose known shape yields no text keeps its JSON."""
    renderers = {
        "map_widget": _render_map,
        "charts_widget_v2": _render_chart,
        "math_block_widget_always_prefetch_v2": _render_math,
        "pronunciation_block_widget_v4_audio_fix_v2": _render_pronunciation,
        "ask_user_input": _render_questions,
        "suggest_automation": _render_suggestion,
    }
    renderer = renderers.get(key)
    rendered = renderer(value) if renderer else ""
    return rendered or _fenced_json({key: value})


def _render_map(value: object) -> str:
    """List named map points with their category and address, then the widget's answer."""
    if not isinstance(value, dict):
        return ""
    lines: list[str] = []
    points = value.get("points")
    for point in points if isinstance(points, list) else []:
        if not isinstance(point, dict):
            continue
        name = _clean_line(point.get("name"))
        if not name:
            continue
        label = f"**{_escape_markdown(name)}**"
        category = _clean_line(point.get("category"))
        address = _clean_line(point.get("address"))
        if category and not _contains_word(name, category):
            label += f" ({category})"
        if address:
            label += f" — {address}"
        lines.append(f"- {label}")
    blocks = ["*Map*", "\n".join(lines)] if lines else []
    answer = _content_text(value, "answer")
    if answer:
        blocks.append(answer)
    return "\n\n".join(blocks)


def _render_chart(value: object) -> str:
    """Render a chart's title, description, and data as a Markdown table."""
    content = value.get("content") if isinstance(value, dict) else None
    if not isinstance(content, dict):
        return ""
    meta = content.get("meta") if isinstance(content.get("meta"), dict) else {}
    x_key = _clean_line(content.get("xKey"))
    columns = [(x_key, _clean_line(content.get("xAxisLabel")) or x_key)]
    series = content.get("series")
    for item in series if isinstance(series, list) else []:
        if not isinstance(item, dict) or not _clean_line(item.get("dataKey")):
            continue
        header = _clean_line(item.get("label")) or _clean_line(item.get("dataKey"))
        axis_label = _clean_line(item.get("axisLabel"))
        if axis_label and axis_label != header:
            header = f"{header} ({axis_label})"
        columns.append((_clean_line(item.get("dataKey")), header))
    blocks: list[str] = []
    title = _clean_line(meta.get("title"))
    if title:
        blocks.append(f"**{_escape_markdown(title)}**")
    description = _content_text(meta, "description")
    if description:
        blocks.append(description)
    rows = content.get("data")
    if len(columns) > 1 and isinstance(rows, list) and rows:
        table = [
            "| " + " | ".join(_table_cell(header) for _key, header in columns) + " |",
            "| " + " | ".join("---" for _ in columns) + " |",
        ]
        for row in rows:
            if isinstance(row, dict):
                cells = [_table_cell(row.get(key)) for key, _header in columns]
                table.append("| " + " | ".join(cells) + " |")
        blocks.append("\n".join(table))
    return "\n\n".join(blocks)


def _render_math(value: object) -> str:
    """Keep LaTeX in a fenced block, because the chat renderer has no math extension."""
    content = _content_text(value, "content")
    return _fenced(content, "latex") if content else ""


def _render_pronunciation(value: object) -> str:
    content = _clean_line(value.get("content")) if isinstance(value, dict) else ""
    if not content:
        return ""
    hint = _clean_line(value.get("pronunciation_hint"))
    return f"**{_escape_markdown(content)}**" + (f" ({hint})" if hint and hint != content else "")


def _render_questions(value: object) -> str:
    """Keep each clarifying question with the options the user was offered."""
    questions = value.get("questions") if isinstance(value, dict) else None
    blocks: list[str] = []
    for question in questions if isinstance(questions, list) else []:
        if not isinstance(question, dict):
            continue
        prompt = _clean_line(question.get("question"))
        if not prompt:
            continue
        options = [_clean_line(option) for option in _string_list(question.get("options"))]
        option_lines = [f"- {option}" for option in options if option]
        blocks.append("\n".join([f"**{_escape_markdown(prompt)}**", *option_lines]))
    return "\n\n".join(blocks)


def _render_suggestion(value: object) -> str:
    label = _content_text(value, "label")
    return f"*Suggested automation: {label}*" if label else ""


def _render_url(payload: str) -> str:
    """Link a titled URL only when it is a plain HTTP(S) address."""
    fields = payload.split(_SEPARATOR)
    title = _clean_line(fields[0])
    url = _clean_line(fields[1]) if len(fields) > 1 else ""
    if not title:
        title = url
    if url and _HTTP_URL_PATTERN.fullmatch(url):
        # A backslash would escape the link's closing parenthesis, so percent-encode it. Angle
        # brackets let a destination carry balanced parentheses such as Wikipedia article titles.
        destination = url.replace("\\", "%5C")
        if "(" in destination or ")" in destination:
            destination = f"<{destination}>"
        return f"[{_escape_markdown(title)}]({destination})"
    return title


def _entity_name(payload: str) -> str:
    """Return the display name from an entity payload such as ["place", "Name", "detail"]."""
    try:
        fields = json.loads(payload)
    except ValueError:
        return ""
    if isinstance(fields, list) and len(fields) >= 2 and isinstance(fields[1], str):
        return _clean_line(fields[1])
    return ""


def _json_object(payload: str) -> dict | None:
    try:
        body = json.loads(payload)
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _string_list(value: object) -> list[str]:
    """Return only string items from a list-valued field, ignoring any other shape."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _content_text(source: object, key: str) -> str:
    """Return a stripped multi-line string field, keeping paragraph breaks."""
    if not isinstance(source, dict):
        return ""
    value = source.get(key)
    return value.strip() if isinstance(value, str) else ""


def _clean_line(value: object) -> str:
    """Collapse a single-line label, returning an empty string for non-text values."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


def _contains_word(text: str, word: str) -> bool:
    """Return whether word appears in text as a whole word, ignoring case."""
    return re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text, re.IGNORECASE) is not None


def _table_cell(value: object) -> str:
    if value is None:
        return ""
    return _clean_line(str(value)).replace("|", "\\|")


def _escape_markdown(value: str) -> str:
    return _MARKDOWN_ESCAPE_PATTERN.sub(r"\\\1", value)


def _fenced_json(value: object) -> str:
    return _fenced(json.dumps(value, ensure_ascii=False, indent=2), "json")


def _fenced(body: str, info: str) -> str:
    fence = "````" if "```" in body else "```"
    return f"{fence}{info}\n{body}\n{fence}"
