"""Read WechatExporter HTML text without extracting files or executing scripts.

Code version: v1.5.1

The exporter's ``msgid`` is MesLocalID, not a server message identifier. Portrait
filenames retain native usernames; the displayed ``wxid`` attribute may be an alias.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import stat
import struct
import unicodedata
from urllib.parse import unquote, urlsplit
import zipfile


PARSER_VERSION = "v1.5.1"
MAX_ARCHIVE_BYTES = 2 * 1024**3
MAX_MEMBERS = 100_000
MAX_CENTRAL_DIRECTORY_BYTES = 32 * 1024**2
MAX_TEXT_MEMBER_BYTES = 16 * 1024**2
MAX_SELECTED_TEXT_BYTES = 128 * 1024**2
MAX_COMPRESSION_RATIO = 200
MAX_HTML_NODES = 1_000_000
MAX_HTML_DEPTH = 96
MAX_MESSAGE_NODES = 250_000
MAX_ASYNC_PAGES = 10_000
_REVIEWED_HARD_LIMITS = {
    "archive_bytes": 32 * 1024**3,
    "selected_text_bytes": 2 * 1024**3,
    "html_nodes": 32_000_000,
    "message_nodes": 3_000_000,
    "archive_members": 250_000,
    "central_directory_bytes": 64 * 1024**2,
}
_VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_HTML_CLOCK = re.compile(
    r"(?P<year>[0-9]{4})-(?P<month>[0-9]{1,2})-(?P<day>[0-9]{1,2}) "
    r"(?:(?P<period>凌晨|上午|中午|下午)\s*)?"
    r"(?P<hour>[0-9]{1,2}):(?P<minute>[0-9]{2})"
    r"(?::(?P<second>[0-9]{2}))?(?:\s+(?P<meridiem>AM|PM))?",
    re.IGNORECASE,
)
_MEDIA_PLACEHOLDER = re.compile(
    r"\[(?:Photo|图片|Video|视频|Emoji|表情|Audio(?: [0-9: .]+)?|语音(?: [0-9: .]+)?)\]"
    r"|\[(?:File|文件):[^\]]*\]",
    re.DOTALL,
)
_JS_PREFIX = re.compile(r"\s*\(function\(\)\s*\{\s*var\s+msgArray\s*=\s*")
_JS_SUFFIX = re.compile(
    r"\s*;\s*for\s*\(var idx = 0; idx < msgArray.length; idx\+\+\)\s*"
    r"\{\s*window.moreWechatMsgs.push\(msgArray\[idx\]\);\s*\}\s*"
    r"msgArray = null;\s*loadMsgsForNextPage\(\);\s*\}\)\(\);\s*"
)


def normalize_html_clock(raw_time):
    """Normalize only an explicit exported date and clock, retaining precision.

    The canonical value is a naive source clock, never an inferred timezone or
    the time of the enclosing forwarded message. Minute-only exports use zero
    seconds as a storage boundary and must retain their minute precision.
    """
    if not isinstance(raw_time, str):
        return "", ""
    match = _HTML_CLOCK.fullmatch(raw_time.strip())
    if match is None:
        return "", ""
    values = match.groupdict()
    period, meridiem = values["period"], values["meridiem"]
    if period and meridiem:
        return "", ""
    hour = int(values["hour"])
    if meridiem:
        if not 1 <= hour <= 12:
            return "", ""
        hour = hour % 12 + (12 if meridiem.upper() == "PM" else 0)
    elif period == "下午":
        if not 1 <= hour <= 12:
            return "", ""
        hour = hour % 12 + 12
    elif period == "上午":
        if not 1 <= hour <= 12:
            return "", ""
        hour %= 12
    elif period == "凌晨":
        if hour not in {0, 1, 2, 3, 4, 5, 12}:
            return "", ""
        hour %= 12
    elif period == "中午":
        if hour != 12:
            return "", ""
    try:
        stamp = datetime(
            int(values["year"]),
            int(values["month"]),
            int(values["day"]),
            hour,
            int(values["minute"]),
            int(values["second"] or 0),
        )
    except ValueError:
        return "", ""
    if stamp.date() == datetime(1970, 1, 1).date():
        return "", ""
    precision = "second" if values["second"] is not None else "minute"
    return stamp.isoformat(), precision


@dataclass(frozen=True)
class ReviewedExportLimits:
    """Explicit, bounded overrides after reviewing an export's central directory.

    Per-member bytes, compression ratio, paths, HTML depth, and asynchronous
    script validation retain their independent fixed safeguards. ZIP64 requires
    an explicit opt-in and a complete, single-volume, bounded metadata layout.
    """

    archive_bytes: int = MAX_ARCHIVE_BYTES
    selected_text_bytes: int = MAX_SELECTED_TEXT_BYTES
    html_nodes: int = MAX_HTML_NODES
    message_nodes: int = MAX_MESSAGE_NODES
    archive_members: int = MAX_MEMBERS
    central_directory_bytes: int = MAX_CENTRAL_DIRECTORY_BYTES
    allow_zip64: bool = False

    def __post_init__(self):
        for name, maximum in _REVIEWED_HARD_LIMITS.items():
            value = getattr(self, name)
            if type(value) is not int or not 0 < value <= maximum:
                raise ValueError(
                    f"Reviewed {name} must be a positive integer at most {maximum}."
                )
        if type(self.allow_zip64) is not bool:
            raise ValueError("Reviewed allow_zip64 must be a boolean.")


def _limits(reviewed_limits=None):
    defaults = {
        "archive_bytes": MAX_ARCHIVE_BYTES,
        "selected_text_bytes": MAX_SELECTED_TEXT_BYTES,
        "html_nodes": MAX_HTML_NODES,
        "message_nodes": MAX_MESSAGE_NODES,
        "archive_members": MAX_MEMBERS,
        "central_directory_bytes": MAX_CENTRAL_DIRECTORY_BYTES,
        "allow_zip64": False,
    }
    if reviewed_limits is None:
        return ReviewedExportLimits(**defaults)
    if isinstance(reviewed_limits, ReviewedExportLimits):
        return ReviewedExportLimits(**asdict(reviewed_limits))
    if not isinstance(reviewed_limits, Mapping):
        raise ValueError("Reviewed limits must be a ReviewedExportLimits or mapping.")
    if set(reviewed_limits) - set(defaults):
        raise ValueError("Unknown reviewed export resource limit.")
    return ReviewedExportLimits(**(defaults | dict(reviewed_limits)))


@dataclass
class _Budget:
    nodes: int = 0
    messages: int = 0
    text_bytes: int = 0
    limits: ReviewedExportLimits = field(default_factory=_limits)


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    children: list = field(default_factory=list)

    @property
    def classes(self):
        return set(self.attrs.get("class", "").split())


class _Tree(HTMLParser):
    def __init__(self, budget):
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {})
        self.stack = [self.root]
        self.budget = budget

    def handle_starttag(self, tag, attrs):
        if len(self.stack) >= MAX_HTML_DEPTH:
            raise ValueError("HTML nesting exceeds the resource limit.")
        self.budget.nodes += 1
        if self.budget.nodes > self.budget.limits.html_nodes:
            raise ValueError("HTML nodes exceed the resource limit.")
        names = [name for name, value in attrs]
        if len(set(names)) != len(names):
            raise ValueError("Duplicate HTML attributes are ambiguous.")
        node = _Node(tag, {name: value or "" for name, value in attrs})
        self.stack[-1].children.append(node)
        if "msg" in node.classes:
            self.budget.messages += 1
            if self.budget.messages > self.budget.limits.message_nodes:
                raise ValueError("Messages exceed the resource limit.")
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, value):
        self.stack[-1].children.append(value)


def _walk(node):
    for child in node.children:
        if isinstance(child, _Node):
            yield child
            yield from _walk(child)


def _find(node, *, tag=None, cls=None):
    return next(
        (
            item
            for item in _walk(node)
            if (tag is None or item.tag == tag) and (cls is None or cls in item.classes)
        ),
        None,
    )


def _text(node):
    if node is None or node.tag in {"script", "style", "iframe", "object"}:
        return ""
    if node.tag == "br":
        return "\n"
    if node.tag == "img":
        # ResManager restores the original inline token in EMOJI_RAW; its image
        # and translated title are not the textual source.
        return (
            node.attrs.get("rawemoji", node.attrs.get("alt", ""))
            if "wxemoji" in node.classes
            else ""
        )
    return "".join(
        child if isinstance(child, str) else _text(child) for child in node.children
    )


def _parse_html(value, budget):
    parser = _Tree(budget)
    parser.feed(value)
    parser.close()
    return parser.root


def _safe_path(value, *, canonical=True):
    if not value or "\\" in value or any(ord(char) < 32 for char in value):
        raise ValueError("Unsafe ZIP member path.")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or re.match(r"^[A-Za-z]:", value):
        raise ValueError("Unsafe ZIP member path.")
    if canonical and any(part in {"", "."} for part in value.rstrip("/").split("/")):
        raise ValueError("Noncanonical ZIP member paths are ambiguous.")


def _preflight_zip(path, *, limits=None):
    # Bound central-directory allocation before ZipFile constructs ZipInfo objects.
    limits = _limits(limits)
    size = path.stat().st_size
    tail_start = max(0, size - 65_557)
    with path.open("rb") as stream:
        stream.seek(tail_start)
        tail = stream.read(65_557)
    end = tail.rfind(b"PK\x05\x06")
    while end >= 0:
        if len(tail) - end >= 22:
            fields = struct.unpack_from("<4s4H2LH", tail, end)
            if end + 22 + fields[-1] == len(tail):
                break
        end = tail.rfind(b"PK\x05\x06", 0, end)
    if end < 0:
        raise ValueError("The source is not a complete supported ZIP archive.")
    _, disk, central_disk, disk_entries, entries, central_bytes, central_offset, _ = (
        fields
    )
    if disk or central_disk or disk_entries != entries:
        raise ValueError("Split ZIP archives are unsupported.")
    end_offset = tail_start + end
    locator_offset = end_offset - 20
    with path.open("rb") as stream:
        stream.seek(max(0, locator_offset))
        locator = stream.read(20) if locator_offset >= 0 else b""
    has_locator = locator.startswith(b"PK\x06\x07")
    needs_zip64 = (
        entries == 0xFFFF or central_bytes == 0xFFFFFFFF or central_offset == 0xFFFFFFFF
    )
    if needs_zip64 or has_locator:
        if not limits.allow_zip64:
            raise ValueError("ZIP64 exports require separate format review.")
        if not has_locator or len(locator) != 20:
            raise ValueError("A ZIP64 archive needs its complete end locator.")
        _, locator_disk, zip64_offset, total_disks = struct.unpack("<4sLQL", locator)
        if locator_disk != 0 or total_disks != 1:
            raise ValueError("Split ZIP64 archives are unsupported.")
        if zip64_offset + 56 != locator_offset or zip64_offset < 0:
            raise ValueError("The ZIP64 end record has invalid bounds.")
        with path.open("rb") as stream:
            stream.seek(zip64_offset)
            record = stream.read(56)
        if len(record) != 56:
            raise ValueError("The ZIP64 end record is truncated.")
        (
            signature,
            record_bytes,
            _made_by,
            required_version,
            zip64_disk,
            zip64_central_disk,
            zip64_disk_entries,
            zip64_entries,
            zip64_central_bytes,
            zip64_central_offset,
        ) = struct.unpack("<4sQ2H2L4Q", record)
        if signature != b"PK\x06\x06" or record_bytes != 44:
            raise ValueError("The ZIP64 end record has an unsupported structure.")
        if required_version < 45:
            raise ValueError("The ZIP64 end record has an invalid required version.")
        if (
            zip64_disk != 0
            or zip64_central_disk != 0
            or zip64_disk_entries != zip64_entries
        ):
            raise ValueError("Split ZIP64 archives are unsupported.")
        for declared, sentinel, actual in (
            (entries, 0xFFFF, zip64_entries),
            (central_bytes, 0xFFFFFFFF, zip64_central_bytes),
            (central_offset, 0xFFFFFFFF, zip64_central_offset),
        ):
            if declared != sentinel and declared != actual:
                raise ValueError("ZIP64 and standard end metadata disagree.")
        entries = zip64_entries
        central_bytes = zip64_central_bytes
        central_offset = zip64_central_offset
        central_end = zip64_offset
    else:
        central_end = end_offset
    if (
        entries > limits.archive_members
        or central_bytes > limits.central_directory_bytes
    ):
        raise ValueError("The ZIP central directory exceeds the resource limit.")
    if central_offset + central_bytes != central_end:
        raise ValueError("The ZIP central directory has invalid bounds.")
    return {
        "zip64": has_locator,
        "members": entries,
        "central_directory_bytes": central_bytes,
        "central_directory_offset": central_offset,
    }


def _display_path(info):
    value = info.filename
    if not info.flag_bits & 0x800:
        try:
            value = value.encode("cp437").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return value


def _local_reference(member_path, value):
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return None
    relative = unquote(parsed.path)
    try:
        _safe_path(relative, canonical=False)
    except ValueError:
        return None
    resolved = posixpath.normpath(
        posixpath.join(str(PurePosixPath(member_path).parent), relative)
    )
    try:
        _safe_path(resolved)
    except ValueError:
        return None
    return resolved


def _native_avatar_id(value):
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return ""
    decoded = unquote(parsed.path)
    try:
        _safe_path(decoded, canonical=False)
    except ValueError:
        return ""
    path = PurePosixPath(decoded)
    if path.parent.name != "Portrait" or path.suffix.lower() != ".jpg":
        return ""
    return path.stem


def _message_nodes(root):
    # Top-level messages are sibling nodes; do not promote nested message markup.
    def visit(node):
        for child in node.children:
            if not isinstance(child, _Node):
                continue
            if "msg" in child.classes:
                yield child
            elif child.tag not in {"script", "style"}:
                yield from visit(child)

    return list(visit(root))


def _metadata_text(spans, *, empty_labels=()):
    values = []
    for item in spans:
        value = _text(item)
        stripped = value.strip()
        if (
            stripped
            and stripped not in empty_labels
            and not _MEDIA_PLACEHOLDER.fullmatch(stripped)
        ):
            values.append(value)
    return "\n\n".join(values)


def _forwarded_app_metadata(content, spans):
    # Lost forwarded subtypes may still retain the exporter's share-card template.
    # Only its visible title and summary qualify; icons and app attribution do not.
    descendants = list(_walk(content))
    appinfo = [item for item in descendants if "appinfo" in item.classes]
    if len(appinfo) != 1:
        return ""
    attribution_nodes = {id(appinfo[0])} | {id(item) for item in _walk(appinfo[0])}
    visible_spans = [item for item in spans if id(item) not in attribution_nodes]
    anchors = [item for item in descendants if item.tag == "a"]
    if not visible_spans or len(anchors) != 1:
        return ""
    anchor = anchors[0]
    try:
        target = urlsplit(anchor.attrs.get("href", ""))
    except ValueError:
        return ""
    if target.scheme not in {"http", "https"} or not target.netloc:
        return ""
    visible_span_ids = {id(item) for item in visible_spans}
    if not any(
        id(item) in visible_span_ids and _text(item).strip() for item in _walk(anchor)
    ):
        return ""
    if any(
        item.tag in {"audio", "video", "object", "iframe"}
        or (
            item.tag == "img"
            and "wxemoji" not in item.classes
            and not ("app-icon" in item.classes and id(item) in attribution_nodes)
        )
        for item in descendants
    ):
        return ""
    return _metadata_text(visible_spans)


def _message(node, member_path, page, ordinal):
    classes = node.classes
    speaker = _find(node, cls="dspname")
    avatar = _find(node, cls="avatar")
    clock = _find(node, cls="nt-box")
    content = _find(node, cls="content-box")
    raw_time = (
        "".join(value for value in clock.children if isinstance(value, str)).strip()
        if clock
        else ""
    )
    timestamp, precision = normalize_html_clock(raw_time)
    flags = ["export_msgid_is_meslocalid", "export_time_zone_unavailable"]
    if any("wxemoji" in item.classes for item in _walk(node)):
        flags.append("inline_emoji_restored_from_exported_token")
    if raw_time and not timestamp:
        flags.append("unrecognized_export_timestamp")
    if re.match(r"1970-0?1-0?1(?: |$)", raw_time.strip()):
        flags.append("possible_epoch_zero_placeholder")
    if precision == "minute":
        flags.append("export_timestamp_precision_minutes")
    avatar_path = avatar.attrs.get("src", "") if avatar else ""
    record = {
        "export_msgid": node.attrs.get("msgid", ""),
        "type": node.attrs.get("msgtype", ""),
        "ordinal": ordinal,
        "direction": "sent"
        if "right" in classes
        else "received"
        if "left" in classes
        else "unknown",
        "sender_export_id": speaker.attrs.get("wxid", "") if speaker else "",
        "sender_native_id": _native_avatar_id(avatar_path),
        "avatar_path": avatar_path,
        "sender_label": _text(speaker),
        "raw_timestamp": raw_time,
        "text": "",
        "text_kind": "deferred",
        "source_locator": {
            "member_path": member_path,
            "page": page,
            "message_ordinal": ordinal,
        },
        "flags": flags,
        "forwarded_components": [],
    }
    if "fmsgtag" in classes:
        record["text"] = _text(content or node).strip()
        record["text_kind"] = "forward_boundary"
        return record
    if "chat-notice" in classes:
        record["text"] = _text(content or node).strip()
        record["text_kind"] = "notice"
        return record
    if "chat" not in classes or content is None:
        return record
    spans = [item for item in _walk(content) if "msg-text" in item.classes]
    if record["type"] == "1":
        record["text"] = "\n\n".join(_text(item) for item in spans)
        record["text_kind"] = "plain_text"
    elif record["type"] == "48":
        location_spans = spans or [item for item in _walk(content) if item.tag == "pre"]
        text = _metadata_text(location_spans, empty_labels={"[Location]", "[位置]"})
        if text:
            record["text"] = text
            record["text_kind"] = "rendered_location_metadata"
            flags.append("location_metadata_from_rendered_export")
    elif "channels" in classes and record["type"] in {"", "channels"}:
        text = _metadata_text(
            [item for item in spans if item.classes & {"card-name", "channel-title"}],
            empty_labels={"[Channels]", "[视频号]"},
        )
        if text:
            record["text"] = text
            record["text_kind"] = "rendered_channel_metadata"
            flags.append("channel_metadata_from_rendered_export")
    elif record["type"] == "49" or (
        "fmsg" in classes
        and not record["type"]
        and any("refermsg" in item.classes for item in spans)
    ):
        reference = [item for item in spans if "refermsg" in item.classes]
        if reference:
            record["text"] = "\n\n".join(
                _text(item) for item in spans if "refermsg" not in item.classes
            )
            record["text_kind"] = "quoted_reply"
            record["quoted_components"] = [
                {"text": _text(item), "kind": "rendered_quote"} for item in reference
            ]
            flags.append("quoted_original_identity_and_type_unavailable")
            if not record["type"]:
                flags.append("forwarded_original_type_unavailable")
        else:
            values = [_text(item) for item in spans]
            for anchor in (item for item in _walk(content) if item.tag == "a"):
                # share.html has a title outside msg-text; plainshare.html does not.
                if not any("msg-text" in item.classes for item in _walk(anchor)):
                    title = _text(anchor)
                    if title.strip():
                        values.insert(0, title)
            record["text"] = "\n\n".join(value for value in values if value.strip())
            record["text_kind"] = "rendered_app_text"
            flags.append("app_subtype_unavailable_in_html")
    elif "fmsg" in classes and not record["type"]:
        descendants = list(_walk(content))
        disallowed = any(
            item.tag in {"audio", "video", "object", "iframe", "a"}
            or (item.tag == "img" and "wxemoji" not in item.classes)
            for item in descendants
        )
        if spans and not disallowed and _find(content, cls="appinfo") is None:
            record["text"] = "\n\n".join(_text(item) for item in spans)
            record["text_kind"] = "forwarded_rendered_text"
            flags.append("forwarded_original_type_unavailable")
        else:
            text = _forwarded_app_metadata(content, spans)
            if text:
                record["text"] = text
                record["text_kind"] = "rendered_forwarded_app_metadata"
                flags.extend(
                    [
                        "forwarded_original_type_unavailable",
                        "app_subtype_unavailable_in_html",
                    ]
                )
    if (record["type"] == "49" or "fmsg" in classes) and _MEDIA_PLACEHOLDER.fullmatch(
        record["text"].strip()
    ):
        # Missing images/files can use the plain-message template. The HTML has
        # lost their subtype, so retain the ZIP and defer this ambiguous token.
        record["text"] = ""
        record["text_kind"] = "deferred"
        flags.append("possible_exporter_media_placeholder")
    return record


def _async_messages(value, *, max_messages=None):
    prefix = _JS_PREFIX.match(value)
    if prefix is None:
        raise ValueError("Unknown asynchronous message wrapper.")
    try:
        values, end = json.JSONDecoder().raw_decode(value, prefix.end())
    except (json.JSONDecodeError, RecursionError) as error:
        raise ValueError("Invalid asynchronous message JSON.") from error
    if (
        not isinstance(values, list)
        or len(values) > (MAX_MESSAGE_NODES if max_messages is None else max_messages)
        or not all(isinstance(item, str) for item in values)
    ):
        raise ValueError("Asynchronous messages must be a bounded string array.")
    if _JS_SUFFIX.fullmatch(value[end:]) is None:
        raise ValueError("Unknown asynchronous message trailer.")
    return values


def _declared_async(root):
    scripts = "\n".join(
        _text_script(item) for item in _walk(root) if item.tag == "script"
    )
    counts = {}
    for key in ("numberOfMsgs", "numberOfPages"):
        matches = re.findall(
            r"\bvar\s+" + key + r"\s*=\s*parseInt\(\s*['\"](\d+)['\"]\s*\)\s*\|\|\s*0",
            scripts,
        )
        if len(matches) > 1:
            raise ValueError("Duplicate asynchronous page declarations.")
        counts[key] = int(matches[0]) if matches else 0
    path = re.findall(
        r"\bscript\.src\s*=\s*['\"]([^'\"]+)/msg-['\"]\s*\+\s*nextPage\s*\+\s*['\"]\.js['\"]",
        scripts,
    )
    if counts["numberOfPages"] > MAX_ASYNC_PAGES:
        raise ValueError("Asynchronous page count exceeds the resource limit.")
    if counts["numberOfPages"] and len(set(path)) != 1:
        raise ValueError("Missing or ambiguous asynchronous page path.")
    counts["data_path"] = path[0] if path else ""
    return counts


def _text_script(node):
    return "".join(value for value in node.children if isinstance(value, str))


def _index_sessions(root, member_path):
    sessions = {}
    for row in (item for item in _walk(root) if item.tag == "tr"):
        anchors = [item for item in _walk(row) if item.tag == "a"]
        if len(anchors) != 1:
            continue
        target = _local_reference(member_path, anchors[0].attrs.get("href", ""))
        if target is None or not target.lower().endswith(".html"):
            continue
        if target in sessions:
            raise ValueError("Duplicate index conversation links.")
        avatar = _find(row, tag="img")
        label = _text(anchors[0])
        alias = re.search(r"\bWeChat ID\s*:\s*([^\s)）]+)", label, re.IGNORECASE)
        sessions[target] = {
            "index_avatar_native_id": _native_avatar_id(avatar.attrs.get("src", ""))
            if avatar
            else "",
            "index_avatar_path": avatar.attrs.get("src", "") if avatar else "",
            "index_public_id": alias.group(1) if alias else "",
        }
    return sessions


def _reviewed_plan(value, archive_sha256):
    """Accept only an explicit, source-bound session resolution schema."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "archive_sha256",
        "resolutions",
        "unavailable_index_paths",
    }:
        raise ValueError("A reviewed session plan has an invalid schema.")
    digest = value["archive_sha256"]
    if (
        not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or digest != archive_sha256
    ):
        raise ValueError("The reviewed session plan needs the exact archive SHA-256.")
    if not isinstance(value["resolutions"], list) or not isinstance(
        value["unavailable_index_paths"], list
    ):
        raise ValueError("Reviewed session plan entries must be lists.")
    return value


def _planned_sessions(sessions, all_html, index_path, plan, limits):
    """Resolve exact references only through a complete reviewed one-to-one plan."""
    if plan is None:
        if all_html != {index_path, *sessions}:
            raise ValueError("Unreferenced HTML members require format review.")
        return [
            (member, member, identity, None) for member, identity in sessions.items()
        ], []
    if (
        len(plan["resolutions"]) + len(plan["unavailable_index_paths"])
        > limits.archive_members
    ):
        raise ValueError("The reviewed session plan exceeds the resource limit.")
    resolutions = {}
    unavailable = {}
    physical = set()
    # Folding detects ambiguity only; it never selects or repairs a physical path.
    reference_counts = Counter(
        unicodedata.normalize("NFC", member).casefold() for member in sessions
    )
    for row in plan["resolutions"]:
        if not isinstance(row, dict) or set(row) != {
            "index_member_path",
            "physical_member_path",
            "main_html_sha256",
            "basis",
        }:
            raise ValueError("A reviewed session resolution has an invalid schema.")
        logical = row["index_member_path"]
        member = row["physical_member_path"]
        if not isinstance(logical, str) or not isinstance(member, str):
            raise ValueError("Reviewed session paths must be exact strings.")
        _safe_path(logical)
        _safe_path(member)
        if logical not in sessions or logical in resolutions:
            raise ValueError("Reviewed index references must be unique and present.")
        if member == index_path or member not in all_html or member in physical:
            raise ValueError(
                "Reviewed physical HTML members must be unique and present."
            )
        digest = row["main_html_sha256"]
        basis = row["basis"]
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or not isinstance(basis, str)
            or not basis.strip()
            or "\0" in basis
            or len(basis.encode()) > 64 * 1024
        ):
            raise ValueError("Reviewed session evidence needs its SHA-256 and basis.")
        peer = sessions[logical]["index_avatar_native_id"]
        reassigned = unicodedata.normalize("NFC", logical) != unicodedata.normalize(
            "NFC", member
        )
        conflicting = (
            reference_counts[unicodedata.normalize("NFC", logical).casefold()] > 1
        )
        if (reassigned or conflicting) and (
            not peer or peer.endswith(("@chatroom", "@im.chatroom"))
        ):
            raise ValueError(
                "A reassigned physical path needs a verifiable direct peer."
            )
        resolutions[logical] = dict(row)
        physical.add(member)
    for row in plan["unavailable_index_paths"]:
        if not isinstance(row, dict) or set(row) != {"index_member_path", "reason"}:
            raise ValueError("A reviewed unavailable entry has an invalid schema.")
        logical = row["index_member_path"]
        reason = row["reason"]
        if not isinstance(logical, str):
            raise ValueError("Reviewed unavailable references must be exact strings.")
        _safe_path(logical)
        if logical not in sessions or logical in resolutions or logical in unavailable:
            raise ValueError("Reviewed index references must be accounted for once.")
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or "\0" in reason
            or len(reason.encode()) > 64 * 1024
        ):
            raise ValueError("A reviewed unavailable reference needs its reason.")
        unavailable[logical] = {
            "index_member_path": logical,
            **sessions[logical],
            "reason": reason,
        }
    if set(resolutions) | set(unavailable) != set(sessions):
        raise ValueError("The reviewed plan must account for every index reference.")
    if physical != all_html - {index_path}:
        raise ValueError(
            "The reviewed plan must account for every physical HTML member."
        )
    resolved = [
        (
            resolutions[logical]["physical_member_path"],
            logical,
            identity,
            resolutions[logical],
        )
        for logical, identity in sessions.items()
        if logical in resolutions
    ]
    return resolved, [
        unavailable[logical] for logical in sessions if logical in unavailable
    ]


def _reassigned_peer_evidence(
    index_member, physical_member, identity, top_level, *, conflicting_reference=False
):
    if not conflicting_reference and unicodedata.normalize(
        "NFC", index_member
    ) == unicodedata.normalize("NFC", physical_member):
        return None
    peer = identity["index_avatar_native_id"]
    received = [record for record in top_level if record["direction"] == "received"]
    sent = [record for record in top_level if record["direction"] == "sent"]
    received_ids = {record["sender_native_id"] for record in received}
    sent_ids = {record["sender_native_id"] for record in sent}
    if (
        received_ids != {peer}
        or len(sent_ids) != 1
        or "" in sent_ids
        or peer in sent_ids
    ):
        raise ValueError(
            "The reassigned direct peer lacks independent native-frame proof."
        )
    return {
        "received_native_ids": sorted(received_ids),
        "sent_native_ids": sorted(sent_ids),
        "received_observations": len(received),
        "sent_observations": len(sent),
    }


def _attach_forwarded(records, counts, warnings):
    top_level = []
    stack = []
    last = None
    for record, classes in records:
        if "fmsgtag" in classes:
            label = record["text"].strip()
            if label.startswith("<<"):
                if last is None:
                    warnings.append("orphan_forward_boundary")
                    stack.append(None)
                else:
                    stack.append(last)
                    last["flags"].append("forwarded_text_only")
            elif label.endswith(">>") and stack:
                stack.pop()
            else:
                warnings.append("unrecognized_forward_boundary")
            counts["forward_boundaries"] += 1
            continue
        # Media templates can replace EXTRA_CLS and lose the fmsg class. The
        # exporter's paired boundary nodes therefore own every intervening node.
        if "fmsg" in classes or stack:
            counts["forwarded_nodes"] += 1
            parent = stack[-1] if stack else None
            if parent is None:
                counts["deferred_forwarded"] += 1
                warnings.append("orphan_forwarded_component")
            elif record["text_kind"] == "deferred" or not record["text"].strip():
                counts["omitted_forwarded_non_text"] += 1
                parent["flags"].append("forwarded_non_text_or_unreadable_items_omitted")
            else:
                record["component_ordinal"] = len(parent["forwarded_components"]) + 1
                parent["forwarded_components"].append(record)
                counts["retained_forwarded_text"] += 1
            last = record
            continue
        last = record
        counts["top_level_nodes"] += 1
        top_level.append(record)
    if stack:
        warnings.append("unterminated_forward_boundary")
    messages = []
    for record in top_level:
        if not record["text"].strip() and record["forwarded_components"]:
            record["text"] = "\n\n".join(
                item["text"] for item in record["forwarded_components"]
            )
            record["text_kind"] = "forwarded_text"
        if record["text_kind"] == "deferred" or not record["text"].strip():
            counts["deferred_top_level"] += 1
        else:
            messages.append(record)
    return messages, top_level


def _node_audit_rows(records, top_level, messages):
    """Yield lightweight disposition evidence for every original rendered node."""
    top_ids = {id(record) for record in top_level}
    retained_top_ids = {id(record) for record in messages}
    retained_forward_ids = set()
    pending = list(messages)
    while pending:
        parent = pending.pop()
        for child in parent["forwarded_components"]:
            retained_forward_ids.add(id(child))
            pending.append(child)
    for record, classes in records:
        if "fmsgtag" in classes:
            role, status = "boundary", "forward_boundary"
        elif id(record) in top_ids:
            role = "top_level"
            status = (
                "retained_top_level_text"
                if id(record) in retained_top_ids
                else "deferred_top_level"
            )
        else:
            role = "forwarded"
            status = (
                "retained_forwarded_text"
                if id(record) in retained_forward_ids
                else "omitted_forwarded_non_text"
            )
        yield {
            "role": role,
            "status": status,
            "source_locator": dict(record["source_locator"]),
            "export_msgid": record["export_msgid"],
            "type": record["type"],
            "text_kind": record["text_kind"],
            "classes": sorted(classes),
            "flags": list(record["flags"]),
            "text_length": len(record["text"]),
            "text_sha256": hashlib.sha256(record["text"].encode()).hexdigest(),
            "direction": record["direction"],
            "raw_timestamp": record["raw_timestamp"],
            "sender_export_id": record["sender_export_id"],
            "sender_native_id": record["sender_native_id"],
            "avatar_path": record["avatar_path"],
            "sender_label": record["sender_label"],
        }


def parse_html_export(
    zip_path,
    *,
    reviewed_limits=None,
    conversation_sink=None,
    node_audit_sink=None,
    reviewed_session_plan=None,
):
    """Return text and native-identity evidence; leave the source ZIP untouched.

    Only selected UTF-8 HTML and fixed-wrapper JSON message pages are decompressed.
    Media, attachments, linked resources, and scripts are never opened or executed.
    Callers must retain the original ZIP because unknown rendered types are deferred.

    ReviewedExportLimits or a mapping can explicitly increase reviewed cumulative
    budgets within their hard ceilings; defaults remain unchanged. ZIP64 is an
    explicit opt-in and retains independent end-record structural validation.
    A sink receives one complete conversation at a time. Its returned metadata
    omits messages. Sink writes must remain staged until this function returns
    successfully, because final archive-wide checks run after the callbacks.
    A node audit sink receives conversation metadata and a lazy metadata iterator
    covering every rendered message node, including deferred content. It must
    consume that iterator inside the callback; no raw bodies are added to it.
    A reviewed session plan can explicitly bind original index references to
    physical HTML members and retain known unavailable entries. It is pinned to
    the archive and each main HTML SHA-256, covers all references and files once,
    and never accepts external peer identities. Non-Unicode path reassignment or
    conflicting index references require received-peer and distinct sent-owner
    frame evidence from the complete top-level conversation.
    """
    limits = _limits(reviewed_limits)
    if conversation_sink is not None and not callable(conversation_sink):
        raise ValueError("The conversation sink must be callable.")
    if node_audit_sink is not None and not callable(node_audit_sink):
        raise ValueError("The node audit sink must be callable.")
    path = Path(zip_path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("The HTML export must be a regular, non-symlink ZIP file.")
    before = path.stat()
    if before.st_size > limits.archive_bytes:
        raise ValueError("The archive exceeds the resource limit.")
    structure = _preflight_zip(path, limits=limits)
    with path.open("rb") as stream:
        archive_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    session_plan = _reviewed_plan(reviewed_session_plan, archive_sha256)
    selected = []
    warnings = []
    budget = _Budget(limits=limits)
    all_counts = Counter()
    owners = Counter()
    conversations = []
    audited_nodes = 0
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > limits.archive_members:
            raise ValueError("Archive members exceed the resource limit.")
        if len(infos) != structure["members"]:
            raise ValueError("Archive member count differs from end metadata.")
        members = {}
        collision_keys = set()
        raw_names = set()
        for info in infos:
            # ZipInfo normalizes separators on Windows and truncates NUL suffixes.
            _safe_path(info.orig_filename)
            display = _display_path(info)
            _safe_path(info.filename)
            _safe_path(display)
            key = unicodedata.normalize("NFC", display).casefold()
            if info.filename in raw_names or key in collision_keys:
                raise ValueError("Duplicate or Unicode-colliding archive paths.")
            raw_names.add(info.filename)
            collision_keys.add(key)
            file_type = stat.S_IFMT(info.external_attr >> 16)
            if file_type not in {0, stat.S_IFREG, stat.S_IFDIR} or info.flag_bits & 1:
                raise ValueError(
                    "Encrypted, symlink, or special archive members are unsupported."
                )
            if not info.is_dir():
                members[display] = info

        def read_text(member):
            info = members.get(member)
            if info is None:
                raise ValueError("A referenced text member is missing.")
            if (
                info.file_size > MAX_TEXT_MEMBER_BYTES
                or info.file_size / max(info.compress_size, 1) > MAX_COMPRESSION_RATIO
            ):
                raise ValueError("A text member exceeds the resource limit.")
            budget.text_bytes += info.file_size
            if budget.text_bytes > limits.selected_text_bytes:
                raise ValueError("Selected text exceeds the resource limit.")
            with archive.open(info) as stream:
                raw = stream.read(MAX_TEXT_MEMBER_BYTES + 1)
                if len(raw) != info.file_size:
                    raise ValueError("Text member size does not match the directory.")
            try:
                value = raw.decode("utf-8-sig")
            except UnicodeDecodeError as error:
                raise ValueError("Text members must contain valid UTF-8.") from error
            selected.append(
                {
                    "member_path": member,
                    "zip_filename": info.filename,
                    "size": info.file_size,
                    "compressed_size": info.compress_size,
                    "crc32": f"{info.CRC:08x}",
                    "header_offset": info.header_offset,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "filename_utf8_recovered": member != info.filename,
                }
            )
            return value

        indexes = [
            name
            for name in members
            if PurePosixPath(name).name == "index.html"
            and not name.startswith("__MACOSX/")
        ]
        if len(indexes) != 1:
            raise ValueError("Exactly one HTML conversation index is required.")
        index_root = _parse_html(read_text(indexes[0]), budget)
        sessions = _index_sessions(index_root, indexes[0])
        del index_root
        if not sessions:
            raise ValueError("No local conversation links were found in the index.")
        all_html = {
            name
            for name in members
            if name.lower().endswith(".html") and not name.startswith("__MACOSX/")
        }
        resolved_sessions, unavailable_sessions = _planned_sessions(
            sessions, all_html, indexes[0], session_plan, limits
        )
        reference_counts = Counter(
            unicodedata.normalize("NFC", member).casefold() for member in sessions
        )
        used_js = set()
        for member, index_member, identity, resolution in resolved_sessions:
            value = read_text(member)
            if resolution and selected[-1]["sha256"] != resolution["main_html_sha256"]:
                raise ValueError("The reviewed main HTML SHA-256 no longer matches.")
            root = _parse_html(value, budget)
            del value
            body = _find(root, tag="body")
            title = _find(root, cls="sname") or _find(root, tag="title")
            title_text = _text(title)
            body_uid = body.attrs.get("uid", "") if body else ""
            body_sid = body.attrs.get("sid", "") if body else ""
            del body, title
            declared = _declared_async(root)
            records = []
            ordinal = 0

            def append_nodes(nodes, source_path, page, position_start=0):
                nonlocal ordinal
                for position, node in enumerate(nodes, start=position_start + 1):
                    ordinal += 1
                    record = _message(node, source_path, page, ordinal)
                    record["source_locator"]["page_message_ordinal"] = position
                    records.append((record, node.classes))
                return position_start + len(nodes)

            append_nodes(_message_nodes(root), member, 0)
            del root
            async_items = 0
            for page in range(1, declared["numberOfPages"] + 1):
                page_path = _local_reference(
                    member, declared["data_path"] + f"/msg-{page}.js"
                )
                if page_path is None:
                    raise ValueError("An asynchronous page uses an unsafe path.")
                used_js.add(page_path)
                fragments = _async_messages(
                    read_text(page_path), max_messages=limits.message_nodes
                )
                async_items += len(fragments)
                position = 0
                for fragment in fragments:
                    fragment_root = _parse_html(fragment, budget)
                    part = _message_nodes(fragment_root)
                    # A single native record can render a parent and several flat
                    # forwarded nodes. The declaration counts array items, not divs.
                    if not part:
                        raise ValueError(
                            "An asynchronous array item must contain message nodes."
                        )
                    position = append_nodes(part, page_path, page, position)
                    del part, fragment_root
                del fragments
            if async_items != declared["numberOfMsgs"]:
                raise ValueError(
                    "Asynchronous message count differs from the declared count."
                )
            counts = Counter()
            messages, top_level = _attach_forwarded(records, counts, warnings)
            peer_evidence = (
                _reassigned_peer_evidence(
                    index_member,
                    member,
                    identity,
                    top_level,
                    conflicting_reference=reference_counts[
                        unicodedata.normalize("NFC", index_member).casefold()
                    ]
                    > 1,
                )
                if resolution is not None
                else None
            )
            for record in top_level:
                if record["direction"] == "sent" and record["sender_native_id"]:
                    owners[record["sender_native_id"]] += 1
            counts["retained_top_level_text"] = len(messages)
            counts["all_message_nodes"] = ordinal
            counts["async_array_items"] = async_items
            all_counts.update(counts)
            peer = identity["index_avatar_native_id"]
            conversation = {
                "member_path": member,
                "title": title_text,
                **identity,
                "conversation_kind": "group"
                if peer.endswith("@chatroom")
                else "direct"
                if peer
                else "unknown",
                "body_uid": body_uid,
                "body_sid": body_sid,
                "messages": messages,
                "counts": dict(counts),
                "declared_async_counts": declared,
            }
            if resolution is not None:
                conversation["index_member_path"] = index_member
                conversation["reviewed_resolution"] = {
                    **resolution,
                    "declared_data_path": declared["data_path"],
                    "native_peer_evidence": peer_evidence,
                }
            metadata = None
            if conversation_sink is not None or node_audit_sink is not None:
                metadata = deepcopy(
                    {
                        key: value
                        for key, value in conversation.items()
                        if key != "messages"
                    }
                )
            if node_audit_sink is not None:
                consumed = 0

                def audit_rows(rows=records, all_top=top_level, kept_top=messages):
                    nonlocal consumed
                    for row in _node_audit_rows(rows, all_top, kept_top):
                        consumed += 1
                        yield row

                node_audit_sink(deepcopy(metadata), audit_rows())
                if consumed != len(records):
                    raise ValueError(
                        "The node audit sink must consume every node in its callback."
                    )
                audited_nodes += consumed
            if conversation_sink is None:
                conversations.append(conversation)
            else:
                conversation_sink(conversation)
                conversations.append(
                    {**metadata, "messages": [], "messages_omitted": True}
                )
            records.clear()
            del top_level, messages, conversation
        all_js = {
            name
            for name in members
            if name.lower().endswith(".js") and not name.startswith("__MACOSX/")
        }
        if all_js - used_js:
            raise ValueError("Unreferenced JavaScript members require format review.")
    after = path.stat()
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise ValueError("The source archive changed during parsing.")
    return {
        "parser_version": PARSER_VERSION,
        "archive_sha256": archive_sha256,
        "archive_size": before.st_size,
        "archive_members": len(infos),
        "archive_structure": structure,
        "selected_members": selected,
        "owner_native_ids": sorted(owners),
        "owner_native_id_counts": dict(owners),
        "conversations": conversations,
        "counts": dict(all_counts),
        "warnings": sorted(set(warnings)),
        "effective_limits": asdict(limits),
        "conversation_sink_used": conversation_sink is not None,
        "node_audit_sink_used": node_audit_sink is not None,
        "audited_message_nodes": audited_nodes,
        "reviewed_session_plan_used": session_plan is not None,
        "unavailable_index_conversations": unavailable_sessions,
    }
