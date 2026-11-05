"""Reviewed preview projections preserve source observations and archive bytes.

Code version: v1.1.1-codex.0
"""

from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from app.core.wechat_archive import _identifier
from app.core.wechat_contacts import (
    contact_registry_path,
    initialize_contact_registry,
)
from app.core.wechat_forwarded_previews import WechatForwardedPreviews
from app.core.wechat_reconcile import _json
from tests.test_wechat_contacts import _make_forwarded_html_store


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _message(fixture, identifier):
    with closing(sqlite3.connect(fixture["history"])) as connection, connection:
        connection.row_factory = sqlite3.Row
        return dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (identifier,)
            ).fetchone()
        )


def _render(values, title="Forward history"):
    def body(node):
        parts = [item["text"] for item in node.get("quoted_components", [])]
        if node["text"] and not (
            node["text_kind"] == "forwarded_text" and node.get("forwarded_components")
        ):
            parts.append(node["text"])
        for nested in node.get("forwarded_components", []):
            parts.append(
                f"[Forwarded text: {nested['sender_label']} | "
                f"{nested['raw_timestamp'] or 'Unknown original time'}]\n{body(nested)}"
            )
        return "\n\n".join(parts)

    return (
        title
        + "\n\n"
        + "\n\n".join(
            f"[Forwarded text: {item['sender_label']} | "
            f"{item['raw_timestamp'] or 'Unknown original time'}]\n{body(item)}"
            for item in values
        )
    )


def _registry(fixture, *, source=True):
    target = _message(fixture, fixture["target_id"])
    declaration = {
        "target_parent_id": target["id"],
        "target_component_path": [3],
        "target_text_sha256": _sha(target["text"]),
        "target_provenance_sha256": _sha(_json(json.loads(target["provenance"]))),
        "authority": "user_confirmed_nested_forward",
    }
    if source:
        donor = _message(fixture, fixture["source_id"])
        declaration.update(
            source_parent_id=donor["id"],
            source_component_paths=[[1], [2], [3], [4]],
            source_text_sha256=_sha(donor["text"]),
            source_provenance_sha256=_sha(_json(json.loads(donor["provenance"]))),
        )
    unresolved = {
        **declaration,
        "target_component_path": [9],
    }
    for key in tuple(unresolved):
        if key.startswith("source_"):
            unresolved.pop(key)
    value = {"schema_version": 1, "previews": [declaration, unresolved]}
    fixture["registry"].write_text(_json(value), encoding="utf-8")
    return value


def _update_tree(fixture, identifier, change):
    row = _message(fixture, identifier)
    proof = json.loads(row["provenance"])
    change(proof)
    text = _render(proof["forwarded_components"])
    with closing(sqlite3.connect(fixture["history"])) as connection, connection:
        connection.execute(
            "UPDATE messages SET text=?,provenance=? WHERE id=?",
            (text, _json(proof), identifier),
        )


@pytest.fixture
def previews(tmp_path):
    contacts = contact_registry_path(tmp_path)
    contacts.parent.mkdir(parents=True)
    with closing(sqlite3.connect(contacts)) as connection, connection:
        initialize_contact_registry(connection)
    fixture = _make_forwarded_html_store(tmp_path)
    target_id = fixture["request"]["id"]
    parent = _message(fixture, target_id)
    proof = json.loads(parent["provenance"])
    prototype = deepcopy(proof["forwarded_components"][0])

    def child(number, text, *, native="wxid_forwarded", clock=""):
        node = deepcopy(prototype)
        node.update(
            export_msgid=f"42_component_{number}",
            component_ordinal=number,
            ordinal=number + 1,
            text=text,
            sender_native_id=native,
            sender_label=("Author: A" if native == "wxid_forwarded" else "Author B"),
            avatar_path=f"Room_files/Portrait/{native}.jpg",
            raw_timestamp=clock,
        )
        node["source_locator"].update(
            message_ordinal=number + 1, page_message_ordinal=number + 1
        )
        return node

    donor_children = [
        child(1, "First text: preserve every colon", clock="2021-08-08 12:34:56"),
        child(2, "Second text\nLiteral multiline body", native="wxid_author_b"),
        child(3, "Third 😀 <img src=x>", clock="1970-01-01 00:00:00"),
        child(4, "Fourth text", native="wxid_author_b", clock="2021-08-08 12:37"),
        child(5, "Unmatched donor tail must stay outside the preview"),
    ]
    preview = "\n".join(
        f"{item['sender_label']}: {item['text']}" for item in donor_children[:4]
    )
    target_children = [child(number, f"Sibling {number}") for number in range(1, 10)]
    target_children[2]["text"] = preview
    target_children[8]["text"] = (
        "An unresolved label: literal preview\n  • Keep spacing  "
    )
    for component in (target_children[2], target_children[8]):
        component["type"] = "49"
    proof.update(
        export_msgid_namespace="MesLocalID",
        primary_text_sha256=_sha("Forward history"),
        forwarded_components=target_children,
    )
    source_proof = deepcopy(proof)
    locator = source_proof["locator"]
    locator.update(export_msgid="43", message_ordinal=20, page_message_ordinal=20)
    source_record_id = _identifier(
        "html-text-v1", fixture["source"]["source_sha256"], _json(locator)
    )
    source_id = _identifier(fixture["source"]["source_id"], source_record_id)
    source_proof.update(
        record_id=source_record_id,
        forwarded_components=donor_children,
        sender_native_id="wxid_forwarded",
        avatar_path_metadata="Room_files/Portrait/wxid_forwarded.jpg",
    )
    donor = {
        **parent,
        "id": source_id,
        "ordinal": 20,
        "text": _render(donor_children),
        "provenance": _json(source_proof),
    }
    with closing(sqlite3.connect(fixture["history"])) as connection, connection:
        connection.execute(
            "UPDATE messages SET text=?,provenance=? WHERE id=?",
            (_render(target_children), _json(proof), target_id),
        )
        connection.execute(
            "INSERT INTO messages ("
            + ",".join(donor)
            + ") VALUES ("
            + ",".join("?" for _ in donor)
            + ")",
            tuple(donor.values()),
        )
    fixture.update(
        target_id=target_id,
        source_id=source_id,
        contacts=contacts,
        registry=contacts.with_name("forwarded_previews.json"),
        preview=preview,
        donor_children=donor_children,
    )
    _registry(fixture)
    return fixture


def _project(fixture):
    return WechatForwardedPreviews(fixture["store"]).project(
        [_message(fixture, fixture["target_id"])]
    )


def test_exact_reviewed_excerpt_reuses_only_declared_source_observations(previews):
    fixture = previews
    before = {
        key: fixture[key].read_bytes() for key in ("history", "contacts", "registry")
    }
    supplied = _message(fixture, fixture["target_id"])
    result = _project(fixture)[fixture["target_id"]]
    match = result[(3,)]
    assert match["status"] == "matched_excerpt"
    assert match["record"]["preview_text"] == ""
    assert match["preview_text"] == fixture["preview"]
    children = match["record"]["items"]
    assert len(children) == 4
    assert [item.content_text for item in children] == [
        item["text"] for item in fixture["donor_children"][:4]
    ]
    assert all(
        item.messaging["provenance"]["parent_message_id"] == fixture["source_id"]
        for item in children
    )
    assert all(
        item.messaging["contact"]["identity_status"] == "verified" for item in children
    )
    assert len({item.messaging["contact"]["id"] for item in children}) == 2
    for number, child in enumerate(children, start=1):
        proof = child.messaging["preview_reference_proof"]
        assert proof["relationship"] == "matching_indexed_excerpt"
        assert proof["source_component_path"] == [number]
        assert proof["original_bundle_identity_asserted"] is False
        assert child.messaging["original_conversation_unknown"] is True
        assert fixture["donor_children"][number - 1]["text"] not in _json(proof)
    assert children[0].messaging["timestamp_label"] == "8 Aug 2021 12:34:56"
    assert children[0].messaging["time_basis"] == (
        "Matching indexed source observation; Source clock; timezone unknown"
    )
    assert children[0].messaging["preview_reference_proof"]["source_time_basis"] == (
        "Source clock; timezone unknown"
    )
    for child in children[1:3]:
        assert child.first_seen_at == ""
        assert child.messaging["timestamp_label"] == ""
        assert child.messaging["time_basis"] == (
            "Matching indexed source observation; Unavailable source time"
        )
    assert "precision: minute" in children[3].messaging["time_basis"]
    unresolved = result[(9,)]
    assert unresolved["status"] == "preview_only"
    assert unresolved["record"]["items"] == ()
    assert (
        unresolved["preview_text"]
        == "An unresolved label: literal preview\n  • Keep spacing  "
    )
    assert all(fixture[key].read_bytes() == value for key, value in before.items())
    assert _message(fixture, fixture["target_id"]) == supplied
    assert not any(
        fixture["history"].with_name(fixture["history"].name + suffix).exists()
        for suffix in ("-wal", "-shm", "-journal")
    )


@pytest.mark.parametrize("reviewed", [True, False])
def test_history_browser_applies_only_reviewed_component_paths_without_source_writes(
    previews, reviewed
):
    from app.core.wechat_history_browser import query_wechat_history

    fixture = previews
    if not reviewed:
        fixture["registry"].unlink()
    before = {key: fixture[key].read_bytes() for key in ("history", "contacts")}
    session = fixture["request"]["conversation_id"]
    page = query_wechat_history(fixture["store"], session=session)
    by_id = {item.stable_id: item for item in page.items}
    target = by_id[fixture["target_id"]]
    target_children = target.messaging["forwarded_record"]["items"]
    assert len(target_children) == 9
    assert target.content_text == _message(fixture, fixture["target_id"])["text"]
    assert target_children[2].content_text == fixture["preview"]
    assert target_children[2].first_seen_at == ""
    assert target_children[2].messaging["timestamp_label"] == ""
    assert [
        number
        for number, item in enumerate(target_children, start=1)
        if item.messaging.get("forwarded_record")
    ] == ([3, 9] if reviewed else [])
    if reviewed:
        matched = target_children[2].messaging["forwarded_record"]
        assert matched["presentation_label"] == "Matched excerpt"
        assert matched["preview_text"] == ""
        assert len(matched["items"]) == 4
        assert all(
            item.messaging["provenance"]["parent_message_id"] == fixture["source_id"]
            for item in matched["items"]
        )
        assert target_children[2].messaging["display_text"] == ""
        assert (
            target_children[2].messaging["forwarded_preview_proof"]["preview_text"]
            == (fixture["preview"])
        )
        unresolved = target_children[8].messaging["forwarded_record"]
        assert unresolved["presentation_label"] == "Preview only"
        assert unresolved["items"] == ()
        assert unresolved["preview_text"] == target_children[8].content_text
    else:
        assert target_children[2].messaging["display_text"] == fixture["preview"]
    donor_children = by_id[fixture["source_id"]].messaging["forwarded_record"]["items"]
    assert len(donor_children) == 5
    assert not any(
        child.messaging.get("preview_reference_proof") for child in donor_children
    )
    assert donor_children[0].messaging["time_basis"] == "Source clock; timezone unknown"
    other_account = query_wechat_history(
        fixture["store"], session=session, account="account:wxid_other"
    )
    assert other_account.items == ()
    assert all(fixture[key].read_bytes() == value for key, value in before.items())


def test_missing_registry_has_no_effect_and_does_not_open_databases(
    previews, monkeypatch
):
    import app.core.wechat_forwarded_previews as module

    previews["registry"].unlink()
    supplied = _message(previews, previews["target_id"])
    monkeypatch.setattr(
        module, "_read_only", lambda path: pytest.fail("Unexpected database read")
    )
    assert WechatForwardedPreviews(previews["store"]).project([supplied]) == {}


@pytest.mark.parametrize("field", ["target_text_sha256", "target_provenance_sha256"])
def test_changed_target_pin_never_projects_a_reviewed_preview(previews, field):
    value = _registry(previews)
    for declaration in value["previews"]:
        declaration[field] = "0" * 64
    previews["registry"].write_text(_json(value))
    assert _project(previews) == {}


@pytest.mark.parametrize("field", ["source_text_sha256", "source_provenance_sha256"])
def test_changed_source_pin_retains_only_the_literal_preview(previews, field):
    value = _registry(previews)
    value["previews"][0][field] = "0" * 64
    previews["registry"].write_text(_json(value))
    result = _project(previews)[previews["target_id"]][(3,)]
    assert result["status"] == "preview_only"
    assert result["record"]["items"] == ()
    assert result["preview_text"] == previews["preview"]


@pytest.mark.parametrize("field,value", [("text", "Forged body"), ("provenance", "{}")])
def test_supplied_page_cannot_replace_the_published_target(previews, field, value):
    supplied = _message(previews, previews["target_id"])
    supplied[field] = value
    assert WechatForwardedPreviews(previews["store"]).project([supplied]) == {}


def test_body_free_registry_rejects_copied_bodies_and_duplicate_declarations(previews):
    value = _registry(previews)
    encoded = previews["registry"].read_text()
    assert previews["preview"] not in encoded
    assert all(item["text"] not in encoded for item in previews["donor_children"])
    value["previews"][0]["body"] = "Copying message bodies is forbidden"
    previews["registry"].write_text(_json(value))
    assert _project(previews) == {}
    value = _registry(previews)
    value["previews"].append(deepcopy(value["previews"][0]))
    previews["registry"].write_text(_json(value))
    assert _project(previews) == {}


@pytest.mark.parametrize(
    "paths", [[[1], [3], [4]], [[True], [2], [3]], [[1], [2]], [[0], [1], [2]]]
)
def test_malformed_or_noncontiguous_source_paths_fail_closed(previews, paths):
    value = _registry(previews)
    value["previews"][0]["source_component_paths"] = paths
    previews["registry"].write_text(_json(value))
    assert _project(previews) == {}


@pytest.mark.parametrize(
    "mutation",
    ["single_author", "duplicate", "unverified", "different_text", "quoted"],
)
def test_ambiguous_or_unverified_donor_nodes_do_not_create_events(previews, mutation):
    def change(proof):
        nodes = proof["forwarded_components"]
        if mutation == "single_author":
            for node in nodes:
                node.update(
                    sender_native_id="wxid_forwarded",
                    sender_label="Author: A",
                    avatar_path="Room_files/Portrait/wxid_forwarded.jpg",
                )
        elif mutation == "duplicate":
            nodes[2].update(
                text=nodes[0]["text"], raw_timestamp=nodes[0]["raw_timestamp"]
            )
        elif mutation == "unverified":
            nodes[1]["sender_native_id"] = ""
        elif mutation == "quoted":
            nodes[1]["quoted_components"] = [
                {"kind": "rendered_quote", "text": "An undeclared quoted body"}
            ]
        else:
            nodes[3]["text"] += " An indexed extension"

    _update_tree(previews, previews["source_id"], change)
    if mutation in {"single_author", "duplicate"}:
        source = json.loads(_message(previews, previews["source_id"])["provenance"])
        text = "\n".join(
            f"{item['sender_label']}: {item['text']}"
            for item in source["forwarded_components"][:4]
        )
        _update_tree(
            previews,
            previews["target_id"],
            lambda proof: proof["forwarded_components"][2].update(text=text),
        )
    _registry(previews)
    result = _project(previews)[previews["target_id"]][(3,)]
    assert result["status"] == "preview_only"
    assert result["record"]["items"] == ()


@pytest.mark.parametrize(
    "mutation", ["sender", "avatar", "member", "snapshot", "account"]
)
def test_source_scope_and_outer_forwarder_must_agree(previews, mutation):
    if mutation == "account":
        with closing(sqlite3.connect(previews["history"])) as connection, connection:
            connection.execute("UPDATE partitions SET account_id='wxid_other'")
    else:

        def change(proof):
            if mutation == "sender":
                proof["sender_native_id"] = "wxid_other"
            elif mutation == "avatar":
                proof["avatar_path_metadata"] = "Room_files/Portrait/wxid_other.jpg"
            elif mutation == "member":
                proof["locator"]["member_path"] = "Export/Other.html"
            else:
                proof["source_snapshot"] = 900

        _update_tree(previews, previews["source_id"], change)
    _registry(previews)
    result = _project(previews)
    assert not result or result[previews["target_id"]][(3,)]["status"] == "preview_only"


def test_plain_preview_like_text_has_no_implicit_nested_record(previews):
    _update_tree(
        previews,
        previews["target_id"],
        lambda proof: proof["forwarded_components"][2].update(text_kind="plain_text"),
    )
    _registry(previews)
    result = _project(previews)[previews["target_id"]]
    assert (3,) not in result
    assert result[(9,)]["record"]["items"] == ()


def test_existing_nested_structure_does_not_gain_a_second_preview_record(previews):
    source = json.loads(_message(previews, previews["source_id"])["provenance"])
    child = source["forwarded_components"][0]
    _update_tree(
        previews,
        previews["target_id"],
        lambda proof: proof["forwarded_components"][2].update(
            forwarded_components=[child]
        ),
    )
    _registry(previews)
    result = _project(previews)[previews["target_id"]]
    assert (3,) not in result
    assert result[(9,)]["status"] == "preview_only"


def test_reviewed_literal_preview_headers_never_become_authors_or_times(previews):
    literal = (
        "Sender: A: 2021-08-08 12:34:56\n"
        "[Forwarded text: Apparent author | 2021-08-08 12:35:00]\n"
        "  • Literal source line\n\n"
    )
    _update_tree(
        previews,
        previews["target_id"],
        lambda proof: proof["forwarded_components"][2].update(text=literal),
    )
    _registry(previews, source=False)
    result = _project(previews)[previews["target_id"]][(3,)]
    assert result["record"]["items"] == ()
    assert result["record"]["preview_text"] == literal
    assert "timestamp_label" not in result["record"]


def test_distinct_verified_source_conversation_cannot_supply_an_excerpt(previews):
    with closing(sqlite3.connect(previews["history"])) as connection, connection:
        connection.row_factory = sqlite3.Row
        original = dict(connection.execute("SELECT * FROM conversations").fetchone())
        other = {
            **original,
            "id": original["id"] + "-other",
            "peer_id": "other-synthetic@chatroom",
        }
        connection.execute(
            "INSERT INTO conversations ("
            + ",".join(other)
            + ") VALUES ("
            + ",".join("?" for _ in other)
            + ")",
            tuple(other.values()),
        )
        connection.execute(
            "UPDATE messages SET conversation_id=? WHERE id=?",
            (other["id"], previews["source_id"]),
        )
        snapshot = connection.execute(
            "SELECT id,metadata FROM source_snapshots"
        ).fetchone()
        metadata = json.loads(snapshot["metadata"])
        additional = deepcopy(metadata["source_conversation_catalog"][0])
        additional["index_avatar_native_id"] = other["peer_id"]
        metadata["source_conversation_catalog"].append(additional)
        connection.execute(
            "UPDATE source_snapshots SET metadata=? WHERE id=?",
            (_json(metadata), snapshot["id"]),
        )
    _registry(previews)
    result = _project(previews)[previews["target_id"]][(3,)]
    assert result["status"] == "preview_only"
    assert result["record"]["items"] == ()


def test_same_content_in_a_distinct_snapshot_cannot_supply_an_excerpt(previews):
    with closing(sqlite3.connect(previews["history"])) as connection, connection:
        source = connection.execute(
            "SELECT snapshot_id,source_id,metadata FROM source_snapshots"
        ).fetchone()
        metadata = json.loads(source[2])
        metadata["observation_note"] = "Distinct receipt with otherwise equal metadata"
        snapshot = connection.execute(
            "INSERT INTO source_snapshots(snapshot_id,source_id,metadata) VALUES (?,?,?)",
            (source[0], source[1], _json(metadata)),
        ).lastrowid
    _update_tree(
        previews,
        previews["source_id"],
        lambda proof: proof.update(source_snapshot=snapshot),
    )
    _registry(previews)
    assert _project(previews)[previews["target_id"]][(3,)]["status"] == "preview_only"


@pytest.mark.parametrize("name", ["registry", "history", "contacts"])
def test_symlinked_inputs_are_never_read(previews, name):
    path = previews[name]
    resident = path.with_suffix(path.suffix + ".resident")
    path.rename(resident)
    path.symlink_to(resident.name)
    if name == "registry":
        with pytest.raises(ValueError, match="symbolic links"):
            WechatForwardedPreviews(previews["store"])
    else:
        assert _project(previews) == {}


def test_cloud_placeholder_is_rejected_before_reading_registry(previews, monkeypatch):
    import app.core.wechat_forwarded_previews as module

    lstat = Path.lstat

    def placeholder(path):
        state = lstat(path)
        if path == previews["registry"]:
            return SimpleNamespace(st_mode=state.st_mode, st_flags=0x40000000)
        return state

    projector = WechatForwardedPreviews(previews["store"])
    monkeypatch.setattr(Path, "lstat", placeholder)
    monkeypatch.setattr(
        module, "_read_json", lambda path: pytest.fail("Cloud hydration read")
    )
    assert projector.project([_message(previews, previews["target_id"])]) == {}


def test_active_journal_and_page_budget_fail_closed(previews, monkeypatch):
    import app.core.wechat_forwarded_previews as module

    supplied = _message(previews, previews["target_id"])
    journal = previews["history"].with_name("history.sqlite3-journal")
    journal.write_bytes(b"active source writer")
    assert WechatForwardedPreviews(previews["store"]).project([supplied]) == {}
    journal.unlink()
    monkeypatch.setattr(module, "_PAGE_BYTES", 1)
    assert _project(previews) == {}


def test_message_budget_stops_a_generator_without_consuming_the_tail(
    previews, monkeypatch
):
    import app.core.wechat_forwarded_previews as module

    monkeypatch.setattr(module, "_MAX_MESSAGES", 2)
    row = _message(previews, previews["target_id"])
    visited = []

    def messages():
        for number in range(50):
            visited.append(number)
            yield row

    assert WechatForwardedPreviews(previews["store"]).project(messages()) == {}
    assert visited == [0, 1, 2]
