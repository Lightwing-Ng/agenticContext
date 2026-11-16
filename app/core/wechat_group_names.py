"""Project time-stamped group names from notices in the validated WeChat timeline.

Code version: v1.0.0-codex.0
"""

from collections import defaultdict
from datetime import datetime
import json
import re

from .wechat_contacts import classify_wechat_identifier
from .wechat_text import _system_xml_payload, extract_wechat_text


_ENGLISH_CHANGE = re.compile(
    r'(?:You|"[^"\r\n]+") changed the group name to "(?P<title>[^\r\n]+)"'
)
_CHINESE_CHANGE = re.compile(
    r'(?:你|“[^”\r\n]+”|「[^」\r\n]+」|"[^"\r\n]+")'
    r'(?:已)?(?:'
    r'(?:修改|更改)(?:群名|群聊名称|群組名稱)'
    r'|(?:将|將)(?:群名|群聊名称|群組名稱)(?:修改|更改|變更)'
    r')(?:为|為)(?P<quoted>[^\r\n]+)'
)
_QUOTES = {'"': '"', '“': '”', '「': '」'}
_TABLE = re.compile(r"(?:(?:main|temp)\.)?[A-Za-z_][A-Za-z0-9_]*")


def _renamed_title(text, peer, message_type):
    if text.startswith("System Message:"):
        text = text.removeprefix("System Message:").lstrip(" \t")
    if payload := _system_xml_payload(text, peer):
        projection = extract_wechat_text(message_type, payload)
        if projection is None or projection.get("system_message_subtype") != "sysmsgtemplate":
            return ""
        text = projection["text"]
    if match := _ENGLISH_CHANGE.fullmatch(text):
        return match["title"]
    if match := _CHINESE_CHANGE.fullmatch(text):
        quoted = match["quoted"]
        if _QUOTES.get(quoted[0]) == quoted[-1]:
            return quoted[1:-1]
    return ""


def group_name_projections(connection, message_table, conversations):
    """Read dated system evidence for the requested conversation IDs only."""
    if _TABLE.fullmatch(message_table) is None:
        raise ValueError("Group names need a validated timeline table.")
    peers = {
        row["id"]: row["peer_id"]
        for row in conversations
        if row["kind"] == "group"
        and classify_wechat_identifier(row["peer_id"])["kind"] == "group"
    }
    if not peers:
        return {}
    changes = defaultdict(dict)
    for row in connection.execute(
        f"SELECT m.conversation_id,m.id,m.text,m.timestamp_value,m.timestamp_label,m.provenance "
        f"FROM {message_table} m "
        "WHERE m.conversation_id IN (SELECT value FROM json_each(?)) AND m.time_sort>0 "
        "AND instr(m.quality_flags,'\"system_notice\"')>0 "
        "AND (instr(m.text,'changed the group name to ')>0 OR instr(m.text,'群名')>0 "
        "OR instr(m.text,'群聊名称')>0 OR instr(m.text,'群組名稱')>0)",
        (json.dumps(list(peers)),),
    ):
        try:
            proof = json.loads(row["provenance"])
            stamp = datetime.fromisoformat(row["timestamp_value"])
        except (TypeError, ValueError):
            continue
        if not isinstance(proof, dict):
            continue
        message_type = proof.get("message_type")
        if (
            stamp.utcoffset() is None
            or proof.get("text_extraction_kind") != "system_text"
            or type(message_type) is not int
            or message_type not in (10000, 10002)
            or proof.get("system_message_subtype") not in (None, "", "sysmsgtemplate")
        ):
            continue
        session = row["conversation_id"]
        title = _renamed_title(row["text"], peers[session], message_type)
        if not title.strip() or "\0" in title:
            continue
        observations = changes[session]
        key = (stamp, title)
        prior = observations.get(key)
        if prior is None or row["id"] < prior["message_id"]:
            observations[key] = {
                "title": title,
                "timestamp_value": row["timestamp_value"],
                "timestamp_label": row["timestamp_label"],
                "message_id": row["id"],
            }

    projections = {}
    for session, observations in changes.items():
        ordered = sorted(observations, reverse=True)
        stamp, title = ordered[0]
        if len(ordered) > 1 and ordered[1][0] == stamp:
            title = ""
        projections[session] = {
            "title": title,
            "changes": tuple(observations[key] for key in ordered),
        }
    return projections


def latest_group_titles(connection, message_table, conversations):
    return {
        session: projection["title"]
        for session, projection in group_name_projections(
            connection, message_table, conversations
        ).items()
        if projection["title"]
    }
