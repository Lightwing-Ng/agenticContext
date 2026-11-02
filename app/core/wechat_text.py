"""Extract known WeChat textual envelopes without opening attachments or rendering HTML.

Code version: v1.3.0-codex.0
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any
from xml.etree import ElementTree


MAX_XML_BYTES = 2 * 1024 * 1024
MAX_XML_DEPTH = 32
MAX_XML_NODES = 20_000
MAX_FORWARD_DEPTH = 8
_XML_DECLARATION = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_CODE = re.compile(r"[0-9]{1,10}")
_INVITATION_HEADINGS = ("Group Chat Invitation", "邀请你加入群聊", "邀请你加入群组")
_INVITATION_LABEL = r'(?:"[^\r\n]+"|“[^\r\n]+”|「[^\r\n]+」)'
_INVITATION_GROUP_LABEL = r'(?:"[^\r\n]*"|“[^\r\n]*”|「[^\r\n]*」)'
_CHINESE_INVITATION_GROUP = r"(?:" + _INVITATION_GROUP_LABEL + r'|[^"“”「」\r\n]+)'
_ENGLISH_INVITATION = re.compile(
    r"Group Chat Invitation[ \t\r\n]+"
    + _INVITATION_LABEL
    + r" invited you to join the group chat "
    + _INVITATION_GROUP_LABEL
    + r"\. Enter to view details\."
)
_CHINESE_INVITATION = re.compile(
    r"邀请你加入群聊[ \t\r\n]+"
    + _INVITATION_LABEL
    + r"邀请你加入群聊"
    + _CHINESE_INVITATION_GROUP
    + r"[，,]进入可查看详情[。.]"
)
_LEGACY_CHINESE_INVITATION = re.compile(
    r"邀请你加入群组[ \t\r\n]+"
    + _INVITATION_LABEL
    + r"邀请你加入群组"
    + _INVITATION_LABEL
    + r"[，,]进入查看详情[。.]"
)

_FORWARDED_RESOURCES = {
    2: "image",
    3: "audio",
    4: "video",
    5: "app",
    6: "app",
    8: "file",
}


@dataclass
class _Budget:
    """Bound cumulative work when an envelope contains another XML document."""

    xml_bytes: int = 0
    xml_nodes: int = 0
    forward_depth: int = 0


def _parse_xml(raw: str, budget: _Budget) -> ElementTree.Element | None:
    if len(raw) > MAX_XML_BYTES or _XML_DECLARATION.search(raw):
        return None
    try:
        budget.xml_bytes += len(raw.encode("utf-8"))
    except UnicodeEncodeError:
        return None
    if budget.xml_bytes > MAX_XML_BYTES:
        return None
    parser = ElementTree.XMLPullParser(events=("start", "end"))
    root = None
    depth = 0
    try:
        value = raw.lstrip("\ufeff \t\r\n")
        for offset in range(0, len(value), 4096):
            parser.feed(value[offset : offset + 4096])
            for event, element in parser.read_events():
                if event == "start":
                    depth += 1
                    budget.xml_nodes += 1
                    if depth > MAX_XML_DEPTH or budget.xml_nodes > MAX_XML_NODES:
                        return None
                    if root is None:
                        root = element
                else:
                    depth -= 1
        parser.close()
    except (ElementTree.ParseError, ValueError):
        return None
    return root if depth == 0 else None


def _child(parent: ElementTree.Element | None, tag: str) -> ElementTree.Element | None:
    if parent is None:
        return None
    matches = parent.findall(tag)
    return matches[0] if len(matches) == 1 else None


def _leaf(parent: ElementTree.Element | None, path: str) -> str:
    node = parent
    for part in path.split("/"):
        node = _child(node, part)
    # Do not interpret a nested configuration or HTML fragment as a text field.
    return (node.text or "") if node is not None and len(node) == 0 else ""


def _code(value: str) -> int | None:
    value = value.strip()
    return int(value) if _CODE.fullmatch(value) else None


def _usable(value: str) -> bool:
    return bool(value.strip())


def is_group_invitation_text(text: str) -> bool:
    """Recognize complete standard card copy without rewriting it or naming people.

    This is a text-template check, not an identity or native-subtype assertion.
    Callers still need an independently proven message type and source event.
    """
    if not isinstance(text, str) or len(text) > 8192 or "\x00" in text:
        return False
    if text.count("Group Chat Invitation") == 1 and _ENGLISH_INVITATION.fullmatch(text):
        return True
    return bool(
        text.count("邀请你加入群聊") == 2
        and _CHINESE_INVITATION.fullmatch(text)
        or text.count("邀请你加入群组") == 2
        and _LEGACY_CHINESE_INVITATION.fullmatch(text)
    )


def _invitation(appmsg: ElementTree.Element) -> dict[str, Any] | None:
    fields = {}
    for tag, key in (("title", "title"), ("des", "description"), ("url", "url")):
        nodes = appmsg.findall(tag)
        if len(nodes) > 1 or (nodes and len(nodes[0]) != 0):
            return None
        if nodes:
            fields[key] = nodes[0].text or ""
    title, description = fields.get("title", ""), fields.get("description", "")
    if title not in _INVITATION_HEADINGS or not is_group_invitation_text(
        title + "\n" + description
    ):
        return None
    values = [title, description]
    url = fields.get("url", "")
    if _usable(url) and url not in values:
        values.append(url)
    return _result(
        49,
        "\n".join(values),
        "app_text",
        subtype=5,
        flags=["invitation_card_metadata"],
        invitation_card_fields=fields,
    )


def native_components_text(components: list[dict[str, Any]]) -> str:
    """Join source text once; metadata-only items never introduce separators."""
    return "\n\n".join(item["text"] for item in components if _usable(item["text"]))


def _result(
    message_type: int,
    text: str,
    kind: str,
    *,
    subtype: int | None = None,
    flags: list[str] | None = None,
    allow_empty: bool = False,
    **metadata: Any,
) -> dict[str, Any] | None:
    if not _usable(text) and not allow_empty:
        return None
    return {
        "text": text,
        "text_extraction_kind": kind,
        "original_message_type": message_type,
        "appmsg_subtype": subtype,
        "flags": list(dict.fromkeys(flags or [])),
        **metadata,
    }


def _source_fields(item: ElementTree.Element) -> dict[str, Any]:
    """Retain asserted forwarding metadata without assigning its original conversation."""
    source_ids = {}
    for path in (
        "srcMsgSvrId",
        "srcMsgLocalId",
        "dataitemsource/msgid",
        "dataitemsource/svrid",
        "svrid",
    ):
        value = _leaf(item, path)
        if value:
            source_ids[path] = value
    return {
        "source_sender_id": _leaf(item, "dataitemsource/realchatname")
        or _leaf(item, "dataitemsource/fromusr"),
        "source_sender_label": _leaf(item, "sourcename"),
        "source_timestamp": _leaf(item, "sourcetime"),
        "source_create_time": _leaf(item, "srcMsgCreateTime"),
        "source_data_id": item.get("dataid", ""),
        "source_ids": source_ids,
    }


def _record_root(
    node: ElementTree.Element | None, budget: _Budget
) -> ElementTree.Element | None:
    if node is None:
        return None
    if node.tag == "recordinfo" and _child(node, "datalist") is not None:
        return node
    direct = _child(node, "recordinfo")
    if direct is not None:
        return _record_root(direct, budget)
    if len(node) == 0 and _usable(node.text or ""):
        parsed = _parse_xml(node.text or "", budget)
        return parsed if parsed is not None and parsed.tag == "recordinfo" else None
    return None


def _forward_components(
    root: ElementTree.Element,
    budget: _Budget,
    *,
    depth: int = 0,
    prefix: tuple[int, ...] = (),
) -> tuple[list[dict[str, Any]], int, bool]:
    components = []
    omitted = 0
    incomplete = False
    datalist = _child(root, "datalist")
    if datalist is None:
        return [], 0, True
    for ordinal, item in enumerate(datalist, start=1):
        path = (*prefix, ordinal)
        if item.tag != "dataitem":
            omitted += 1
            incomplete = True
            continue
        kind = _code(item.get("datatype", ""))
        metadata = {"forwarded_item_path": list(path), **_source_fields(item)}
        # Only an explicit app envelope establishes quote roles; datadesc stays literal.
        appmsg = _child(item, "appmsg")
        wrapped = _child(item, "msg")
        if appmsg is None and wrapped is not None:
            appmsg = _child(wrapped, "appmsg")
        if (
            kind in {1, 49}
            and appmsg is not None
            and _code(_leaf(appmsg, "type")) == 57
        ):
            budget.forward_depth += depth
            try:
                quoted = _quoted(appmsg, budget)
            finally:
                budget.forward_depth -= depth
            literal = _leaf(item, "datadesc") if kind == 1 else ""
            if _usable(literal) and (quoted is None or literal != quoted["text"]):
                # Preserve the previously extracted body when two explicit fields disagree.
                components.append(
                    {"kind": "forwarded_text", "text": literal, **metadata}
                )
                incomplete = True
            elif quoted is not None:
                components.append(
                    {
                        "kind": "forwarded_quote",
                        "text": quoted["text"],
                        "text_components": quoted["text_components"],
                        "quoted_reference": quoted["quoted_reference"],
                        "appmsg_subtype": 57,
                        "flags": quoted["flags"],
                        **metadata,
                    }
                )
            else:
                omitted += 1
                incomplete = True
        elif kind == 1:
            text = _leaf(item, "datadesc")
            if _usable(text):
                components.append(
                    {
                        "kind": "forwarded_text",
                        "text": text,
                        **metadata,
                    }
                )
        elif kind == 17:
            if depth + budget.forward_depth > MAX_FORWARD_DEPTH:
                incomplete = True
                omitted += 1
                continue
            nested = _record_root(_child(item, "recordinfo"), budget)
            if nested is None:
                incomplete = True
                omitted += 1
                continue
            values, skipped, partial = _forward_components(
                nested, budget, depth=depth + 1, prefix=path
            )
            if values:
                components.append(
                    {
                        "kind": "forwarded_group",
                        "text": native_components_text(values),
                        "text_components": values,
                        "forwarded_record_title": _leaf(item, "datadesc"),
                        **metadata,
                    }
                )
            omitted += skipped
            incomplete = incomplete or partial
        else:
            # Preserve the occurrence and clock without reading a payload or its title.
            components.append(
                {
                    "kind": "forwarded_resource",
                    "text": "",
                    "resource_kind": _FORWARDED_RESOURCES.get(kind, "unknown"),
                    **({"forwarded_data_type": kind} if kind is not None else {}),
                    **metadata,
                }
            )
    return components, omitted, incomplete


def _forwarded(
    appmsg: ElementTree.Element, budget: _Budget, *, allow_metadata: bool = False
) -> dict[str, Any] | None:
    if budget.forward_depth > MAX_FORWARD_DEPTH:
        return None
    budget.forward_depth += 1
    try:
        root = _record_root(_child(appmsg, "recorditem"), budget)
        if root is None:
            return None
        components, omitted, incomplete = _forward_components(root, budget)
        flags = ["forwarded_text_only"]
        pending = list(components)
        while pending:
            component = pending.pop()
            if component["kind"] == "forwarded_resource":
                flags.append("forwarded_resource_metadata_only")
                break
            pending.extend(component.get("text_components", []))
        if omitted:
            flags.append("forwarded_non_text_or_unreadable_items_omitted")
        if incomplete:
            flags.append("forwarded_record_partially_unreadable")
        return _result(
            49,
            native_components_text(components),
            "forwarded_text",
            subtype=19,
            flags=flags,
            allow_empty=allow_metadata and bool(components),
            text_components=components,
            forwarded_record_title=_leaf(appmsg, "title"),
            omitted_forwarded_items=omitted,
        )
    finally:
        budget.forward_depth -= 1


def _quoted_forwarded(
    reference: ElementTree.Element, budget: _Budget
) -> dict[str, Any] | None:
    """Read only an explicit forwarded-text app inside a quoted app reference."""
    content = _child(reference, "content")
    if content is None:
        return None
    if len(content) == 0:
        root = _parse_xml(content.text or "", budget)
    elif (
        len(content) == 1
        and not (content.text or "").strip()
        and not (content[0].tail or "").strip()
    ):
        # Inline XML was already counted by the outer parser's shared budget.
        root = content[0]
    else:
        return None
    if root is None or root.tag not in {"msg", "appmsg"}:
        return None
    appmsg = root if root.tag == "appmsg" else _child(root, "appmsg")
    if appmsg is None or _code(_leaf(appmsg, "type")) != 19:
        return None
    return _forwarded(appmsg, budget, allow_metadata=True)


def _quoted(appmsg: ElementTree.Element, budget: _Budget) -> dict[str, Any] | None:
    components = []
    reply = _leaf(appmsg, "title")
    if _usable(reply):
        components.append({"kind": "reply_text", "text": reply})
    reference = _child(appmsg, "refermsg")
    reference_type = _code(_leaf(reference, "type"))
    flags = ["quoted_reply_envelope"]
    metadata = {
        "original_message_type": reference_type,
        "source_sender_id": _leaf(reference, "fromusr"),
        "source_sender_label": _leaf(reference, "displayname"),
        "source_chat_id": _leaf(reference, "chatusr"),
        "source_timestamp": _leaf(reference, "createtime"),
        "source_ids": {
            key: value
            for key in ("svrid", "msgid", "localid")
            if (value := _leaf(reference, key))
        },
    }
    content = _leaf(reference, "content") if reference_type == 1 else ""
    forwarded = _quoted_forwarded(reference, budget) if reference_type == 49 else None
    if _usable(content):
        components.append({"kind": "quoted_text", "text": content, **metadata})
    elif forwarded is not None:
        # The quote's forwarder and its embedded original authors are different roles.
        components.append(
            {
                "kind": "quoted_forwarded_text",
                "text": forwarded["text"],
                **metadata,
                "appmsg_subtype": 19,
                "forwarded_record_title": forwarded["forwarded_record_title"],
                "text_components": forwarded["text_components"],
                "omitted_forwarded_items": forwarded["omitted_forwarded_items"],
                "flags": forwarded["flags"],
            }
        )
        flags.extend("quoted_" + flag for flag in forwarded["flags"])
    elif reference_type is not None and reference_type != 1:
        flags.append("quoted_non_text_content_omitted")
    else:
        flags.append("quoted_text_unavailable")
    if not _usable(reply):
        flags.append("reply_text_unavailable")
    return _result(
        49,
        native_components_text(components),
        "quoted_reply",
        subtype=57,
        flags=flags,
        text_components=components,
        quoted_reference=metadata,
    )


def _system_text(message_type: int, raw: str, budget: _Budget) -> dict[str, Any] | None:
    stripped = raw.lstrip("\ufeff \t\r\n")
    if not stripped.startswith("<"):
        if (
            stripped.startswith(("{", "["))
            or "\x00" in raw
            or not any(character.isalpha() for character in raw)
        ):
            return None
        return _result(message_type, raw, "system_text", flags=["system_notice"])
    root = _parse_xml(raw, budget)
    if root is None:
        return None
    subtype = root.get("type", "")
    text = ""
    if root.tag == "sysmsg":
        paths = {
            "revokemsg": ("revokemsg/replacemsg", "replacemsg"),
            "sysmsgtemplate": ("sysmsgtemplate/content_template/plain",),
            "editrevokecontent": ("editrevokecontent/text",),
        }.get(subtype, ())
        for path in paths:
            candidate = _leaf(root, path)
            if _usable(candidate):
                text = candidate
                break
    elif root.tag == "revokemsg":
        subtype = "revokemsg"
        text = _leaf(root, "replacemsg")
    return _result(
        message_type,
        text,
        "system_text",
        flags=["system_notice"],
        system_message_subtype=subtype,
        system_source_ids={
            path: value
            for path in ("revokemsg/newmsgid", "revokemsg/msgid", "newmsgid", "msgid")
            if (value := _leaf(root, path))
        },
    )


def extract_wechat_text(message_type: int, raw: str) -> dict[str, Any] | None:
    """Return known text plus provenance, or None for unsupported or unsafe content.

    The caller removes a verified received-group sender prefix first. Forwarded and
    quoted content remains embedded source evidence, not independent conversations.
    XML work is bounded across nested documents; no external resources are opened.
    """
    if (
        isinstance(message_type, bool)
        or not isinstance(message_type, int)
        or not isinstance(raw, str)
        or not _usable(raw)
    ):
        return None
    if message_type == 1:
        return _result(1, raw, "plain_text")
    if message_type not in {49, 10000, 10002}:
        return None
    budget = _Budget()
    if message_type in {10000, 10002}:
        return _system_text(message_type, raw, budget)
    root = _parse_xml(raw, budget)
    if root is None:
        return None
    appmsg = root if root.tag == "appmsg" else _child(root, "appmsg")
    if appmsg is None or root.tag not in {"msg", "appmsg"}:
        return None
    subtype = _code(_leaf(appmsg, "type"))
    if subtype == 1:
        values = []
        for path in ("title", "des"):
            text = _leaf(appmsg, path)
            if _usable(text) and text not in values:
                values.append(text)
        return _result(49, "\n".join(values), "app_text", subtype=1)
    if subtype == 5:
        return _invitation(appmsg)
    if subtype == 19:
        return _forwarded(appmsg, budget)
    if subtype == 57:
        return _quoted(appmsg, budget)
    return None
