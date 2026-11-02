"""HTML forwarding uses explicit source trees and preserves literal evidence.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape
import pytest

from app.core import wechat_forwarded as forwarding
from app.core.wechat_contacts import canonical_contact_id
from app.core.wechat_display_names import WechatDisplayNames, display_names_path


OWNER = "wxid_html_forward_owner"
FRIEND = "wxid_html_original_friend"
OTHER = "wxid_html_original_other"
PARENT = hashlib.sha256(b"html-forward-parent").hexdigest()
ARCHIVE = hashlib.sha256(b"synthetic-reviewed-html-export").hexdigest()
PRIMARY = "Source forwarding title"
FIRST_TEXT = "First literal <img src=https://invalid.example/track> & original body"
SECOND_TEXT = "Second original statement with emoji 🌹"
THIRD_TEXT = "Third original statement <script>literal only</script>"


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _component(index=1, **updates):
    native = FRIEND if index < 3 else OTHER
    return {
        "component_ordinal": index,
        "ordinal": index + 10,
        "export_msgid": f"parent_component_{index}",
        "type": "" if index == 1 else "1",
        "direction": "received",
        "sender_export_id": f"public.alias.{native[-6:]}",
        "sender_native_id": native,
        "sender_label": "Source friend" if index < 3 else "Other source friend",
        "avatar_path": f"Chat_files/Portrait/{native}.jpg",
        "raw_timestamp": f"2021-08-05 21:{36 + index}:44",
        "text": (FIRST_TEXT, SECOND_TEXT, THIRD_TEXT)[min(index, 3) - 1],
        "text_kind": "rendered_forwarded_app_metadata"
        if index == 1
        else "forwarded_rendered_text",
        "source_locator": {
            "member_path": "Export/Chat.html",
            "page": 0,
            "message_ordinal": index + 10,
            "page_message_ordinal": index + 10,
        },
        "flags": ["export_time_zone_unavailable"],
        "forwarded_components": [],
        **updates,
    }


def _contact(native, label):
    return {
        "id": canonical_contact_id(native, authority="native_message_sender"),
        "platform": "wechat",
        "namespace": "wechat",
        "kind": "wechat_account",
        "identity_status": "verified",
        "display_name": label,
        "source_display_name": label,
    }


def _resolved(raw):
    result = deepcopy(raw)
    pending = list(result)
    while pending:
        item = pending.pop()
        item["contact"] = _contact(item["sender_native_id"], item["sender_label"])
        pending.extend(item["forwarded_components"])
    return result


def _render(record):
    # This is the documented persisted export envelope, not a marker-text parser.
    parts = [
        item["text"] for item in record.get("quoted_components", []) if item.get("text")
    ]
    children = record.get("forwarded_components", [])
    if record["text"] and not (record["text_kind"] == "forwarded_text" and children):
        parts.append(record["text"])
    for child in children:
        label = child.get("sender_label") or "Unknown original sender"
        clock = child.get("raw_timestamp") or "Unknown original time"
        parts.append(f"[Forwarded text: {label} | {clock}]\n{_render(child)}")
    return "\n\n".join(parts)


def _evidence(*, children=None, primary=PRIMARY, kind="rendered_app_text"):
    raw = [_component(index) for index in (1, 2, 3)] if children is None else children
    if kind == "forwarded_text":
        primary = "\n\n".join(item["text"] for item in raw)
    source = {
        "text": primary,
        "text_kind": kind,
        "quoted_components": [],
        "forwarded_components": raw,
    }
    provenance = {
        "text_extraction_kind": kind,
        "export_msgid_namespace": "MesLocalID",
        "primary_text_sha256": _sha(primary),
        "synthetic_forwarded_fallback_displayed": False,
        "quoted_components": [],
        "forwarded_components": raw,
        "locator": {
            "archive_sha256": ARCHIVE,
            "member_path": "Export/Chat.html",
            "message_ordinal": 10,
            "page_message_ordinal": 10,
            "page": 0,
            "export_msgid": "10",
            "export_msgtype": "49" if kind != "plain_text" else "1",
            "export_msgid_namespace": "MesLocalID",
        },
        "source_time": "2021-08-05 21:42:49",
    }
    return provenance, _render(source), _resolved(raw)


def _project(evidence=None, **updates):
    provenance, text, components = evidence or _evidence()
    arguments = {
        "parent_id": PARENT,
        "account_id": OWNER,
        "text": text,
        "components": components,
        "source_proof_status": "source_proof_verified",
        **updates,
    }
    return forwarding.project_html_forwarded(provenance, **arguments)


def test_html_components_keep_original_people_clocks_and_literal_text():
    evidence = _evidence()
    before = deepcopy(evidence)
    result = _project(evidence)
    assert result["title"] == PRIMARY
    assert result["authority"] == "source_verified_html_export_tree"
    assert result["status"] == "embedded_html_text"
    assert len(result["items"]) == 3
    for index, child in enumerate(result["items"]):
        original = evidence[0]["forwarded_components"][index]
        assert child.content_text == original["text"]
        assert child.content_html == ""
        assert child.author_label == original["sender_label"]
        assert child.messaging["contact"] == evidence[2][index]["contact"]
        assert child.messaging["direction"] == original["direction"]
        assert child.last_seen_at == f"2021-08-05T21:{37 + index}:44"
        assert child.messaging["timestamp_label"] == f"5 Aug 2021 21:{37 + index}:44"
        assert child.messaging["time_basis"] == "Source clock; timezone unknown"
    first, second, third = result["items"]
    assert first.messaging["contact"]["id"] == second.messaging["contact"]["id"]
    assert first.messaging["contact"]["id"] != third.messaging["contact"]["id"]
    identifiers = [item.stable_id for item in result["items"]]
    assert len(set(identifiers)) == 3
    assert identifiers == [item.stable_id for item in _project(evidence)["items"]]
    assert set(identifiers).isdisjoint(
        item.stable_id for item in _project(evidence, parent_id="f" * 64)["items"]
    )
    assert evidence == before


def test_viewer_names_require_an_existing_verified_contact_and_preserve_preferences(
    tmp_path,
):
    evidence = _evidence()
    friend_id = evidence[2][0]["contact"]["id"]
    preference = display_names_path(tmp_path)
    preference.parent.mkdir(parents=True)
    preference.write_text(
        json.dumps({"schema_version": 1, "contacts": {friend_id: "Local <viewer>"}})
    )
    before = preference.read_bytes()
    names = WechatDisplayNames(tmp_path)
    result = _project(evidence, display_names=names)
    assert [item.messaging["viewer_display_name"] for item in result["items"]] == [
        "Local <viewer>",
        "Local <viewer>",
        "",
    ]
    unverified = deepcopy(evidence)
    unverified[2][0]["contact"]["identity_status"] = "unresolved"
    child = _project(unverified, display_names=names)["items"][0]
    assert child.messaging["viewer_display_name"] == ""
    assert child.messaging["contact"]["identity_status"] == "unresolved"
    assert preference.read_bytes() == before


def test_native_looking_sender_hint_does_not_create_a_verified_contact():
    evidence = _evidence(children=[_component(sender_native_id=OWNER)])
    evidence[2][0]["contact"] = {
        "id": "source-only-contact",
        "platform": "wechat",
        "identity_status": "unresolved",
        "display_name": "Source friend",
    }
    child = _project(evidence)["items"][0]
    assert child.messaging["contact"] == evidence[2][0]["contact"]
    assert child.messaging["viewer_display_name"] == ""
    assert child.messaging["direction"] == "received"


@pytest.mark.parametrize(
    "status",
    [
        None,
        "",
        True,
        "unverified_source_proof",
        "history_changed",
        "component_resource_limit",
    ],
)
def test_unverified_or_changed_parent_keeps_the_literal_fallback(status):
    assert _project(source_proof_status=status) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("message_type", 49),
        ("unix_timestamp", 1628199769),
        ("appmsg_subtype", 19),
        ("server_id", "not-a-local-export-id"),
        ("text_components", [{"kind": "forwarded_text", "text": "Native claim"}]),
        ("forwarded_references", {"items": []}),
    ],
)
def test_html_projection_rejects_mixed_native_declarations(field, value):
    evidence = _evidence()
    evidence[0][field] = value
    assert _project(evidence) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("message_type", 49),
        ("unix_timestamp", 1628199769),
        ("appmsg_subtype", 19),
        ("server_id", "not-a-local-export-id"),
        ("text_components", [{"kind": "forwarded_text", "text": "Native claim"}]),
        ("forwarded_references", {"items": []}),
    ],
)
def test_native_declarations_inside_the_html_locator_also_fail_closed(field, value):
    evidence = _evidence()
    evidence[0]["locator"][field] = value
    assert _project(evidence) is None


@pytest.mark.parametrize("namespace", [None, "ServerId"])
def test_html_source_namespace_cannot_be_missing_or_reinterpreted(namespace):
    evidence = _evidence()
    evidence[0]["export_msgid_namespace"] = namespace
    assert _project(evidence) is None


def test_untrusted_contact_inside_raw_source_cannot_override_resolved_contact():
    evidence = _evidence()
    canonical = deepcopy(evidence[2][0]["contact"])
    injected = _contact(OWNER, "An injected source contact")
    evidence[0]["forwarded_components"][0]["contact"] = injected
    child = _project(evidence)["items"][0]
    assert child.messaging["contact"] == canonical
    assert child.messaging["contact"]["id"] != injected["id"]


@pytest.mark.parametrize("invalid", [None, [], "not provenance", {}])
def test_invalid_provenance_does_not_become_a_transcript(invalid):
    _provenance, text, components = _evidence()
    assert (
        forwarding.project_html_forwarded(
            invalid,
            parent_id=PARENT,
            account_id=OWNER,
            text=text,
            components=components,
            source_proof_status="source_proof_verified",
        )
        is None
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"parent_id": "not-a-message-id"},
        {"parent_id": "A" * 64},
        {"account_id": ""},
        {"account_id": None},
        {"text": b"not text"},
    ],
)
def test_invalid_message_scope_is_not_projected(changes):
    assert _project(**changes) is None


def test_literal_forwarded_marker_without_a_proven_tree_stays_plain_text():
    provenance, _text, _components = _evidence(children=[])
    text = "A typed example:\n[Forwarded text: Someone | 2021-08-05 21:37:44]\nLiteral content"
    provenance["primary_text_sha256"] = _sha(text)
    assert _project((provenance, text, [])) is None


@pytest.mark.parametrize(
    "failure",
    [
        "parent_hash",
        "parent_text",
        "trailing_body",
        "child_text",
        "child_clock",
        "child_name",
        "child_order",
        "missing_child",
        "nested_tree",
        "namespace",
    ],
)
def test_any_body_or_component_mismatch_preserves_the_raw_fallback(failure):
    evidence = _evidence()
    provenance, text, components = evidence
    if failure == "parent_hash":
        provenance["primary_text_sha256"] = "0" * 64
    elif failure == "parent_text":
        text = "Changed forwarding title" + text[len(PRIMARY) :]
    elif failure == "trailing_body":
        text += "\nAdditional unaccounted source content"
    elif failure == "child_text":
        components[0]["text"] += " changed"
    elif failure == "child_clock":
        components[0]["raw_timestamp"] = "2021-08-05 00:00:00"
    elif failure == "child_name":
        components[0]["sender_label"] = "A different source speaker"
    elif failure == "child_order":
        components.reverse()
    elif failure == "missing_child":
        components.pop()
    elif failure == "nested_tree":
        components[0]["forwarded_components"] = [_component()]
    else:
        provenance["locator"]["export_msgid_namespace"] = "ServerId"
    before = deepcopy((provenance, text, components))
    assert _project((provenance, text, components)) is None
    assert (provenance, text, components) == before


@pytest.mark.parametrize(
    "clock,flags",
    [
        ("", []),
        ("not a clock", ["unrecognized_export_timestamp"]),
        ("2021-08-05", ["unrecognized_export_timestamp"]),
    ],
)
def test_missing_or_unrecognized_original_clock_is_not_copied_from_the_forwarder(
    clock, flags
):
    evidence = _evidence(children=[_component(raw_timestamp=clock, flags=flags)])
    result = _project(evidence)
    child = result["items"][0]
    assert child.last_seen_at == child.messaging["timestamp_label"] == ""
    assert child.messaging["time_basis"] == "Unavailable source time"


def test_nested_forwarded_tree_keeps_order_and_original_parent_ambiguity():
    nested = _component(3, text="A nested original statement")
    parent = _component(
        2,
        text="Nested forwarding title",
        text_kind="rendered_app_text",
        forwarded_components=[nested],
    )
    evidence = _evidence(children=[_component(), parent])
    evidence[0]["text_completion_provenance"] = {
        "root_context_only": True,
        "original_nested_parent_unknown": True,
    }
    evidence[0]["forwarded_components"][1]["text_completion_parent_context"] = {
        "immediate_parent_known": False
    }
    evidence[2][1]["text_completion_parent_context"] = {"immediate_parent_known": False}
    before = deepcopy(evidence)
    result = _project(evidence)
    first, second = result["items"]
    assert first.content_text == FIRST_TEXT
    nested_record = second.messaging["forwarded_record"]
    assert nested_record["title"] == parent["text"]
    assert [item.content_text for item in nested_record["items"]] == [nested["text"]]
    identifiers = {
        first.stable_id,
        second.stable_id,
        nested_record["items"][0].stable_id,
    }
    assert len(identifiers) == 3
    assert evidence == before


def test_synthetic_forwarded_fallback_does_not_repeat_unattributed_bodies():
    evidence = _evidence(kind="forwarded_text")
    result = _project(evidence)
    assert result["title"] == "Forwarded conversation"
    assert [item.content_text for item in result["items"]] == [
        FIRST_TEXT,
        SECOND_TEXT,
        THIRD_TEXT,
    ]
    provenance, text, components = evidence
    synthetic = "\n\n".join(item["text"] for item in provenance["forwarded_components"])
    assert synthetic not in result["title"]
    assert _project((provenance, synthetic + "\n\n" + text, components)) is None


@pytest.mark.parametrize("failure", ["depth", "count", "bytes"])
def test_oversized_forwarded_trees_fail_closed(failure):
    if failure == "depth":
        root = _component()
        cursor = root
        for _ in range(40):
            nested = _component()
            cursor["forwarded_components"] = [nested]
            cursor = nested
        evidence = _evidence(children=[root])
    elif failure == "count":
        evidence = _evidence(children=[_component(index + 1) for index in range(5000)])
    else:
        evidence = _evidence(children=[_component(text="x" * (16 * 1024**2 + 1))])
    assert _project(evidence) is None


class _RenderedElements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forwarded = 0
        self.embedded = 0
        self.active_media = []
        self.open_details = 0
        self.text = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.forwarded += tag == "details" and "data-wechat-forwarded" in attributes
        self.embedded += "data-wechat-embedded-row" in attributes
        self.open_details += tag == "details" and "open" in attributes
        if tag in {"img", "script", "video", "audio", "iframe", "object"}:
            self.active_media.append(tag)

    def handle_data(self, data):
        self.text.append(data)


def test_existing_message_macro_renders_safe_collapsed_children_exactly_once():
    result = _project()
    environment = Environment(
        loader=FileSystemLoader(Path(__file__).parents[1] / "app/web/templates"),
        autoescape=select_autoescape(("html", "xml")),
    )
    template = environment.get_template("_wechat_browser.html")
    module = template.make_module(
        {"text_page": SimpleNamespace(messaging={}, session_detail=False, sessions=())}
    )
    parent = SimpleNamespace(
        stable_id=PARENT,
        content_text="Retained complete raw source text",
        author_label="Outer forwarding person",
        last_seen_at="2021-08-05T21:42:49",
        messaging={
            "display_text": "",
            "direction": "received",
            "sender_id": "outer.alias",
            "timestamp_label": "5 Aug 2021 21:42:49",
            "time_basis": "Source clock; timezone unknown",
            "source_file": "Synthetic export.zip",
            "peer_id": "123@chatroom",
            "account_id": OWNER,
            "provenance": {},
            "quality_flags": (),
            "system_notices": (),
            "forwarded_record": result,
        },
    )
    rendered = str(module.message_row(parent))
    document = _RenderedElements()
    document.feed(rendered)
    text = "".join(document.text)
    assert document.forwarded == 1
    assert document.embedded == 3
    assert document.open_details == 0
    assert document.active_media == []
    assert text.count(PRIMARY) == 1
    assert [text.count(body) for body in (FIRST_TEXT, SECOND_TEXT, THIRD_TEXT)] == [
        1,
        1,
        1,
    ]
    assert "[Forwarded text:" not in text
    assert "Retained complete raw source text" not in text
