"""Read group membership events from dated WeChat system notices.

Code version: v1.3.0-codex.0
"""

from collections import defaultdict
from datetime import datetime
from itertools import groupby
import json
import re

from .wechat_completion_json_codec import load_json_cell
from .wechat_contacts import classify_wechat_identifier


_TABLE = re.compile(r"(?:(?:main|temp)\.)?[A-Za-z_][A-Za-z0-9_]*")
_SUBTYPES = (None, "", "sysmsgtemplate", "delchatroommember")
_MAX_EVENTS = 1000
_MAX_NOTICE = 2048
_QUOTED = r'(?:"[^"]+"|“[^”]+”|「[^」]+」)'
_NAMED = rf"(?:{_QUOTED}|[Yy]ou|你|您)"
_TEMPLATE_NAME = r"\S(?:.*?\S)?"
_GROUP = r"(?:the |this )?group(?: chat)?"
_ROOM = r"(?:本)?(?:群聊|群组|群組|群)"
_END = r"[.。]?"
_VIEWER = frozenset({"You", "you", "你", "您"})
_INVITATION_COMPLETION = "secondary_group_invitation_v1"
_CONTENT_SHA256 = re.compile(r"[0-9a-f]{64}")


def _patterns(name, member_list, chinese_members):
    subject = rf"(?P<subject>{name})"
    recipients = rf"(?P<subject>{member_list})"
    recipients_zh = rf"(?P<subject>{chinese_members})"
    actor = rf"(?P<actor>{name})"
    sharer = rf"(?P<actor>{name}|you)"
    return {
        "joined": (
            ("qr", r"(?P<viewer_subject>You)['’]ve joined the group chat via QR Code\. "
             r"Group chat members: .+"),
            ("invited", rf"{actor} invited (?P<viewer_subject>you) to a group chat with .+"),
            ("invited", rf"{actor} invited {recipients} to {_GROUP}{_END}"),
            ("qr", rf"{subject} (?:has )?joined {_GROUP} via (?:the )?QR [Cc]ode"
             rf"(?: shared by {sharer}| (?P<viewer_actor>you) shared)?{_END}"),
            ("unknown", rf"{subject} (?:has )?joined {_GROUP}{_END}"),
            ("invited", rf"{actor}邀[请請]{recipients_zh}(?:加入|进入|進入)了?{_ROOM}{_END}"),
            ("qr", rf"{subject}(?:(?:通过|通過|透過)?(?:扫描|掃描){actor}"
             r"分享的(?:二维码|二維碼)|扫码|掃碼)"
             rf"(?:加入|进入|進入)了?{_ROOM}{_END}"),
            ("unknown", rf"{subject}(?:加入|进入|進入)了?{_ROOM}{_END}"),
        ),
        "left": (
            ("left", rf"(?P<viewer_subject>You)['’]ve left {_GROUP}{_END}"),
            ("left", rf"{subject} (?:has )?(?:voluntarily )?left {_GROUP}{_END}"),
            ("left", rf"{subject}(?:已)?(?:主动|主動)?(?:退出|离开|離開)了?{_ROOM}{_END}"),
        ),
        "removed": (
            ("removed", rf"{actor} removed {recipients} from {_GROUP}{_END}"),
            ("removed", rf"{subject} (?:was |were |has been |have been )?"
             rf"removed from {_GROUP}"
             rf"(?: by (?:the )?(?:group )?(?:admin |owner )?{actor})?{_END}"),
            ("removed", rf"{actor}[将將]{recipients_zh}(?:移出|踢出)了?{_ROOM}{_END}"),
            ("removed", rf"{subject}(?:已)?(?:被{actor}?)?(?:移出|踢出)了?{_ROOM}{_END}"),
        ),
    }


def _compile(*forms):
    return {
        kind: tuple((method, re.compile(expression)) for method, expression in options)
        for kind, options in _patterns(*forms).items()
    }


_LITERAL = _compile(_NAMED, rf"{_NAMED}(?:, {_NAMED})*", rf"{_NAMED}(?:、{_NAMED})*")
_LITERAL_SUBJECT = re.compile(_NAMED)
_TEMPLATE = _compile(_TEMPLATE_NAME, _TEMPLATE_NAME, _TEMPLATE_NAME)
_CANDIDATES = (
    " invited ", " joined ", " left ", " removed ", "加入", "进入", "進入",
    "退出", "离开", "離開", "移出", "踢出", "<sysmsg",
)
_CANDIDATE_SQL = " OR ".join("instr(m.text,?)>0" for _ in _CANDIDATES)


def _notice(text, subtype):
    if (
        not text or len(text) > _MAX_NOTICE or text.startswith("<")
        or any(character in text for character in "\0\r\n\u2028\u2029")
    ):
        return None
    matches = []
    patterns = _TEMPLATE if subtype else _LITERAL
    for kind, expressions in patterns.items():
        for method, expression in expressions:
            if match := expression.fullmatch(text):
                matches.append((kind, method, match.groupdict()))
                break
    if len(matches) != 1:
        return None
    kind, method, roles = matches[0]
    if subtype == "delchatroommember" and kind != "removed" and not (
        method == "invited" and roles.get("actor") in _VIEWER
    ):
        return None
    return kind, method, roles


def _unquote(value):
    if value and (value[0], value[-1]) in {('"', '"'), ('“', '”'), ('「', '」')}:
        return value[1:-1]
    return value or ""


def _stable_username(value):
    return (
        isinstance(value, str) and len(value) <= 1024
        and not any(ord(character) < 32 for character in value)
        and classify_wechat_identifier(value)["kind"] not in {"group", "unresolved"}
    )


def _verified_account(conversation):
    account = conversation.get("account_id")
    if (
        _stable_username(account)
        and conversation.get("identity_status") == "Verified archive ID"
        and conversation.get("account_key") == "account:" + account
    ):
        return account
    return None


def _valid_fields(fields, notice):
    if not isinstance(fields, dict) or set(fields) != {
        "kind", "method", "actor_label", "subject_ids", "viewer_subject",
    }:
        return False
    kind, method, roles = notice
    actor = _unquote(roles.get("actor") or roles.get("viewer_actor"))
    subject = roles.get("subject") or roles.get("viewer_subject")
    identities = fields["subject_ids"]
    viewer = fields["viewer_subject"]
    return (
        fields["kind"] == kind and fields["method"] == method
        and isinstance(fields["actor_label"], str) and fields["actor_label"] == actor
        and isinstance(identities, list) and len(identities) <= _MAX_NOTICE
        and all(_stable_username(value) for value in identities)
        and len(set(identities)) == len(identities)
        and type(viewer) is bool and bool(identities) != viewer
        and (not viewer or subject in _VIEWER)
    )


def valid_native_membership(fields, text):
    """Verify the retained field shape against the archived system notice text."""
    if not isinstance(text, str):
        return False
    notice = _notice(text.strip(), "sysmsgtemplate")
    return notice is not None and _valid_fields(fields, notice)


def _template_evidence(root, text):
    from .wechat_text import (
        _child, _system_container, _system_hidden_action, _system_leaf,
        _SYSTEM_PLACEHOLDER, _system_link_text, _system_template_text,
    )

    template = _child(root, "sysmsgtemplate")
    if (
        root.tag != "sysmsg" or root.attrib != {"type": "sysmsgtemplate"}
        or len(root) != 1 or not _system_container(root, {"sysmsgtemplate"})
        or template is None or template.attrib or len(template) != 1
        or not _system_container(template, {"content_template"})
    ):
        return None
    content = _child(template, "content_template")
    rendered = _system_template_text(content)
    if rendered is None or rendered.strip() != text:
        return None

    template_type = content.get("type", "")
    links = {
        link.get("name"): link for link in content.findall("link_list/link")
        if not _system_hidden_action(link, template_type)
    }
    # Match the unexpanded grammar so a nickname cannot supply a role or action.
    raw_template = _system_leaf(content, "template")
    skeleton = _SYSTEM_PLACEHOLDER.sub(
        lambda match: match[0] if match["name"] in links else "", raw_template,
    ).strip()
    notice = _notice(skeleton, "sysmsgtemplate")
    if notice is None:
        return None
    kind, method, roles = notice
    subject = roles.get("subject") or roles.get("viewer_subject")
    subject_slot = _SYSTEM_PLACEHOLDER.fullmatch(_unquote(subject))
    subject_link = links.get(subject_slot["name"]) if subject_slot else None
    subjects = []
    if subject_link is not None and subject_link.get("type") == "link_profile":
        for member in subject_link.findall("memberlist/member"):
            username = member.findtext("username")
            subjects.append({
                "label": member.findtext("nickname"),
                "username": username if _stable_username(username) else "",
            })

    actor = roles.get("actor") or roles.get("viewer_actor")
    actor_slot = _SYSTEM_PLACEHOLDER.fullmatch(_unquote(actor))
    actor_link = links.get(actor_slot["name"]) if actor_slot else None
    if actor_link is not None:
        actor = _system_link_text(actor_link, template_type) or ""
    fields = {
        "kind": kind, "method": method, "actor_label": _unquote(actor),
        "subject_ids": [member["username"] for member in subjects if member["username"]],
        "viewer_subject": subject in _VIEWER,
    }
    return fields, tuple(subjects)


def _project_native_evidence(payload, text, notice):
    from .wechat_text import _Budget, _parse_xml

    root = _parse_xml(payload, _Budget())
    evidence = _template_evidence(root, text) if root is not None else None
    if evidence is None:
        return None, ()
    fields, subjects = evidence
    if _valid_fields(fields, notice):
        return fields, subjects
    # An exact profile can carry only a nickname. Keep it if the rendered role agrees.
    kind, method, roles = notice
    actor = _unquote(roles.get("actor") or roles.get("viewer_actor"))
    if (
        not fields["subject_ids"] and not fields["viewer_subject"]
        and (fields["kind"], fields["method"], fields["actor_label"]) == (kind, method, actor)
    ):
        return None, subjects
    return None, ()


def native_membership_fields(raw, peer_id, message_type):
    """Read verified visible subject roles from a native XML notice before flattening."""
    from .wechat_text import _system_xml_payload, extract_wechat_text

    if not isinstance(raw, str) or classify_wechat_identifier(peer_id)["kind"] != "group":
        return None
    payload = _system_xml_payload(raw, peer_id)
    if payload is None or (projection := extract_wechat_text(message_type, payload)) is None:
        return None
    text = projection["text"].strip()
    if (
        projection.get("system_message_subtype") != "sysmsgtemplate"
        or (notice := _notice(text, "sysmsgtemplate")) is None
    ):
        return None
    fields, _ = _project_native_evidence(payload, text, notice)
    return fields


def _event(text, conversation, proof):
    """Keep source wording separate from any explicitly proven subject identities."""
    from .wechat_text import _system_xml_payload, extract_wechat_text

    if not isinstance(proof, dict):
        return None
    message_type = proof.get("message_type")
    subtype = proof.get("system_message_subtype")
    if (
        proof.get("text_extraction_kind") != "system_text"
        or type(message_type) is not int
        or message_type not in (10000, 10002)
        or subtype not in _SUBTYPES
    ):
        return None
    text = text.lstrip("\ufeff \t\r\n")
    text = text.removeprefix("System Message:").strip()
    account = _verified_account(conversation)
    macos = proof.get("macos_recovery")
    raw_fields = proof.get("system_membership") if (
        account and isinstance(macos, dict) and macos.get("account_id") == account
        and macos.get("peer_id") == conversation["peer_id"]
    ) else None
    subjects = ()
    payload = _system_xml_payload(text, conversation["peer_id"])
    if payload:
        projected = extract_wechat_text(message_type, payload)
        if projected is None:
            return None
        native_subtype = projected["system_message_subtype"]
        if native_subtype not in _SUBTYPES or (subtype and subtype != native_subtype):
            return None
        text, subtype = projected["text"].strip(), native_subtype
    notice = _notice(text, subtype)
    if notice is None:
        return None
    if subtype == "sysmsgtemplate" and payload:
        raw_fields, subjects = _project_native_evidence(payload, text, notice)
    kind, method, roles = notice
    actor = _unquote(roles.get("actor") or roles.get("viewer_actor"))
    subject = roles.get("subject") or roles.get("viewer_subject")
    subject_label = _unquote(subject) if _LITERAL_SUBJECT.fullmatch(subject) else subject
    subject_ids = ()
    viewer_subject = (
        subject in _VIEWER and (
            subtype in (None, "", "delchatroommember")
            or bool(roles.get("viewer_subject")) and not payload
        )
    )
    if subtype == "sysmsgtemplate" and _valid_fields(raw_fields, notice):
        subject_ids = tuple(raw_fields["subject_ids"])
        viewer_subject = raw_fields["viewer_subject"]
    if (
        viewer_subject and account and not proof.get("secondary_source_invitation")
    ):
        subject_ids = tuple(dict.fromkeys((*subject_ids, account)))
        subjects = ({"label": subject_label, "username": account},)
    if not subjects:
        if subject_ids:
            # Flattened metadata retains exact IDs without per-profile label boundaries.
            subjects = tuple({"label": "", "username": username} for username in subject_ids)
        elif subtype in (None, ""):
            subjects = tuple(
                {"label": _unquote(match[0]), "username": ""}
                for match in _LITERAL_SUBJECT.finditer(subject)
            )
        else:
            subjects = ({"label": subject_label, "username": ""},)
    return {
        "kind": kind, "text": text, "method": method, "actor_label": actor,
        "subject_ids": subject_ids, "subjects": subjects, "subject_label": subject_label,
    }


def _identity_fields(source, message_type):
    content = source.get("content_sha256")
    created = source.get("create_time")
    local_type = source.get("local_type")
    if (
        isinstance(content, str) and _CONTENT_SHA256.fullmatch(content)
        and type(created) is int and created > 0
        and type(local_type) is int and local_type & 0xFFFFFFFF == message_type
    ):
        return message_type, created, content
    return None


def _native_notice_identity(proof, conversation, account):
    source = proof.get("macos_recovery")
    if (
        not isinstance(source, dict) or source.get("account_id") != account
        or source.get("peer_id") != conversation["peer_id"]
        or source.get("session_id") != conversation["id"]
        or source.get("conversation_kind") != "group"
        or source.get("authenticated_pages") is not True
        or source.get("integrity_check") != "ok"
        or str(source.get("server_id")) != str(proof.get("server_id"))
        or source.get("create_time") != proof.get("unix_timestamp")
    ):
        return None
    return _identity_fields(source, proof["message_type"])


def _retained_membership(connection, row, proof, conversation, account):
    """Reuse only subject fields from the exact authenticated canonical occurrence."""
    if (
        proof.get("system_message_subtype") != "sysmsgtemplate"
        or _native_notice_identity(proof, conversation, account) is None
    ):
        return None
    native = proof["macos_recovery"]
    local_id = native.get("local_id")
    if type(local_id) is not int or any(
        not isinstance(native.get(key), str) or not native[key]
        for key in ("database_relative_path", "table")
    ):
        return None
    fields = None
    for source_id, source_proof, message_proof in connection.execute(
        "SELECT s.source_id,s.proof,o.proof FROM macos_recovery_bank.sources s "
        "CROSS JOIN macos_recovery_bank.occurrences o ON o.source_id=s.id "
        "WHERE s.account_id=? AND s.peer_id=? AND o.conversation_id=? "
        "AND o.local_id=? AND o.server_id=? AND o.content_sha256=? AND o.canonical_id=? "
        "AND o.canonical_store IN ('macos_bank','macos_bank_shadow')",
        (
            account, conversation["peer_id"], conversation["id"],
            local_id, str(native["server_id"]), native["content_sha256"], row["id"],
        ),
    ):
        try:
            packet, message = load_json_cell(source_proof), load_json_cell(message_proof)
        except (TypeError, ValueError):
            continue
        if not isinstance(packet, dict) or not isinstance(message, dict):
            continue
        source = packet.get("source")
        projected = message.get("native_text")
        if (
            not isinstance(source, dict)
            or not isinstance(projected, dict)
            or any(packet.get(key) != native[key] for key in (
                "account_id", "peer_id", "session_id", "conversation_kind",
            ))
            or not source_id or source.get("source_id") != source_id
            or any(source.get(key) != native.get(key) for key in (
                "database_relative_path", "table",
            ))
            or source.get("authenticated_pages") is not True
            or source.get("integrity_check") != "ok"
            or any(message.get(key) != native.get(key) for key in (
                "local_id", "local_type", "create_time", "content_sha256", "real_sender_id",
            ))
            or str(message.get("server_id")) != str(native["server_id"])
            or message.get("text") != row["text"] or projected.get("text") != row["text"]
        ):
            continue
        candidate = projected.get("system_membership")
        if not valid_native_membership(candidate, row["text"]) or not candidate["subject_ids"]:
            continue
        if fields is not None and fields != candidate:
            return None
        fields = candidate
    return fields


def _secondary_invitation_identity(connection, event, proof, conversation, account):
    secondary = proof.get("secondary_source_invitation")
    if (
        not isinstance(secondary, dict) or secondary.get("strategy") != _INVITATION_COMPLETION
        or secondary.get("target_conversation_id") != conversation["id"]
        or secondary.get("peer_id") != conversation["peer_id"]
        or secondary.get("source_viewer_account_id") == account
        or not _stable_username(secondary.get("source_viewer_account_id"))
    ):
        return None
    row = connection.execute(
        "SELECT plan FROM secondary_completions.entries "
        "WHERE id=? AND conversation_id=? AND operation='append' AND strategy=?",
        (event["message_id"], conversation["id"], _INVITATION_COMPLETION),
    ).fetchone()
    if row is None:
        return None
    try:
        plan = load_json_cell(row[0])
    except (ValueError, TypeError):
        return None
    source = plan.get("event") if isinstance(plan, dict) and plan.get("proof") == secondary else None
    if (
        not isinstance(source, dict) or source.get("peer_id") != conversation["peer_id"]
        or source.get("account_id") != secondary["source_viewer_account_id"]
        or str(source.get("server_id")) != str(proof.get("server_id"))
        or str(source.get("server_id")) != str(secondary.get("server_id"))
    ):
        return None
    return _identity_fields(source, proof["message_type"])


def _invitation_copies(connection, observations, conversation, account):
    """Fold only a unique authenticated original and its exact completion copy."""
    native, copies = defaultdict(list), defaultdict(list)
    display_keys = set()
    for index, (event, proof, stamp) in enumerate(observations):
        if event["method"] != "invited" or proof.get("secondary_source_invitation"):
            continue
        identity = _native_notice_identity(proof, conversation, account)
        if identity is not None:
            key = stamp, event["text"]
            display_keys.add(key)
            native[key, identity].append(index)
    for index, (event, proof, stamp) in enumerate(observations):
        key = stamp, event["text"]
        if event["method"] != "invited" or key not in display_keys:
            continue
        identity = _secondary_invitation_identity(connection, event, proof, conversation, account)
        if identity is not None:
            copies[key, identity].append(index)
    return {
        indexes[0]: native[key][0] for key, indexes in copies.items()
        if len(indexes) == 1 and len(native.get(key, ())) == 1
    }


def group_membership_history(connection, message_table, conversation):
    """Count the selected room's retained notices and return its latest events."""
    conversation = dict(conversation)
    if (
        conversation["kind"] != "group"
        or classify_wechat_identifier(conversation["peer_id"])["kind"] != "group"
    ):
        return None
    if _TABLE.fullmatch(message_table) is None:
        raise ValueError("Group membership history needs a validated timeline table.")
    events = []
    total = 0
    rows = connection.execute(
        "SELECT m.id,m.text,m.timestamp_value,m.timestamp_label,m.provenance,m.time_sort "
        f"FROM {message_table} m WHERE m.conversation_id=? AND m.time_sort>0 "
        "AND instr(m.quality_flags,'\"system_notice\"')>0 "
        f"AND ({_CANDIDATE_SQL}) ORDER BY m.time_sort DESC,m.ordinal DESC,m.id",
        (conversation["id"], *_CANDIDATES),
    )
    account = _verified_account(conversation)
    attachments = {row[1] for row in connection.execute("PRAGMA database_list")} if account else set()
    for _, batch in groupby(rows, key=lambda row: row["time_sort"]):
        observations = []
        for row in batch:
            try:
                stamp = datetime.fromisoformat(row["timestamp_value"])
                proof = json.loads(row["provenance"])
            except (TypeError, ValueError):
                continue
            if stamp.utcoffset() is None:
                continue
            event = _event(row["text"], conversation, proof)
            if event is None:
                continue
            if "macos_recovery_bank" in attachments and not event["subject_ids"]:
                fields = _retained_membership(connection, row, proof, conversation, account)
                if fields is not None:
                    event = _event(row["text"], conversation, {**proof, "system_membership": fields})
            event.update(
                timestamp_value=row["timestamp_value"],
                timestamp_label=row["timestamp_label"],
                message_id=row["id"],
            )
            observations.append((event, proof, stamp))
        copies = (
            _invitation_copies(connection, observations, conversation, account)
            if "secondary_completions" in attachments and len(observations) > 1 else {}
        )
        for dropped, retained in copies.items():
            observations[retained][0]["duplicate_message_ids"] = (observations[dropped][0]["message_id"],)
        total += len(observations) - len(copies)
        for index, (event, _, _) in enumerate(observations):
            if index not in copies and len(events) < _MAX_EVENTS:
                events.append(event)
    return {"events": tuple(events), "total": total}
