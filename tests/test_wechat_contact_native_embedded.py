"""Native embedded author references retain roles, source clocks, and bounded hints.

Code version: v1.1.2-codex.0
"""

from __future__ import annotations

from contextlib import closing

from copy import deepcopy
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest

from app.core import wechat_contact_embedded as embedded
from app.core import wechat_contact_analysis as analysis
from app.core.config import CrawlConfig
from app.core.wechat_archive import wechat_archive_path
from app.core.wechat_contacts import contact_registry_path, initialize_contact_registry
from app.core.wechat_text import native_components_text
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from tests.test_wechat_contacts import bind, insert_contact, observe
from tests.test_wechat_reconcile import merge, record, source


PARENT = "a" * 64
OWNER = "wxid_native_owner"
TARGET = "wxid_native_target"
OTHER = "wxid_native_other"
FIRST = "Target original body; retain  two spaces"
NESTED = "Target nested original body"
REPLY = "Other author's own reply"
QUOTED = "Target quoted original body"


def _quoted_component(text=QUOTED, *, sender=TARGET, clock="0", chat=""):
    return {
        "kind": "quoted_text",
        "text": text,
        "original_message_type": 1,
        "source_sender_id": sender,
        "source_sender_label": "Original source label",
        "source_timestamp": clock,
        "source_chat_id": chat,
        "source_ids": {"svrid": "9223372036854775806", "localid": "37"},
    }


def _reference(component):
    return {
        key: value for key, value in component.items() if key not in {"kind", "text"}
    }


def _tree():
    quoted = _quoted_component()
    replies = [{"kind": "reply_text", "text": REPLY}, quoted]
    nested = [
        {
            "kind": "forwarded_text",
            "text": NESTED,
            "source_sender_id": TARGET,
            "source_create_time": "1546300800",
            "source_ids": {"srcMsgSvrId": "7234567890123456789"},
        },
        {
            "kind": "forwarded_quote",
            "text": native_components_text(replies),
            "text_components": replies,
            "quoted_reference": _reference(quoted),
            "appmsg_subtype": 57,
            "source_sender_id": OTHER,
            "source_timestamp": "2019-01-01 08:01:00",
        },
    ]
    return {
        "message_type": 49,
        "appmsg_subtype": 19,
        "text_extraction_kind": "forwarded_text",
        "source_time": "2026-10-30 12:13:14",
        "text_components": [
            {
                "kind": "forwarded_text",
                "text": FIRST,
                "source_sender_id": TARGET,
                "source_timestamp": "1970-01-01 08:00:00",
                "source_create_time": "0",
            },
            {
                "kind": "forwarded_text",
                "text": "Other original body",
                "source_sender_id": OTHER,
            },
            {
                "kind": "forwarded_resource",
                "text": "",
                "resource_kind": "image",
                "source_sender_id": TARGET,
            },
            {
                "kind": "forwarded_group",
                "text": native_components_text(nested),
                "text_components": nested,
            },
        ],
    }


def _direct_quote(*, chat="", clock="0"):
    quoted = _quoted_component(chat=chat, clock=clock)
    return {
        "message_type": 49,
        "appmsg_subtype": 57,
        "text_extraction_kind": "quoted_reply",
        "source_time": "2026-10-30 12:13:14",
        "text_components": [{"kind": "reply_text", "text": REPLY}, quoted],
        "quoted_reference": _reference(quoted),
    }


def _quoted_forwarded_proof(*, nested=False):
    leaf = {
        "kind": "forwarded_text",
        "text": NESTED,
        "source_sender_id": TARGET,
        "source_create_time": "0",
        "source_ids": {"srcMsgSvrId": "7234567890123456789"},
    }
    reference = {
        "original_message_type": 49,
        "source_sender_id": OTHER,
        "source_timestamp": "0",
    }
    bundle = {
        "kind": "quoted_forwarded_text",
        "text": NESTED,
        "text_components": [leaf],
        "appmsg_subtype": 19,
        **reference,
    }
    parts = [{"kind": "reply_text", "text": REPLY}, bundle]
    if not nested:
        return {
            "message_type": 49,
            "appmsg_subtype": 57,
            "text_extraction_kind": "quoted_reply",
            "text_components": parts,
            "quoted_reference": reference,
        }
    replying = {
        "kind": "forwarded_quote",
        "text": native_components_text(parts),
        "text_components": parts,
        "quoted_reference": reference,
        "appmsg_subtype": 57,
        "source_sender_id": OTHER,
    }
    return {
        "message_type": 49,
        "appmsg_subtype": 19,
        "text_extraction_kind": "forwarded_text",
        "text_components": [replying],
    }


def _details(proof):
    return list(embedded.native_details(proof, parent_id=PARENT, account_id=OWNER))


def test_native_tree_separates_authors_quotes_resources_and_nested_positions():
    proof = _tree()
    before = deepcopy(proof)
    details = {tuple(item["path"]): item for item in _details(proof)}
    assert set(details) == {
        ("native", 1),
        ("native", 2),
        ("native", 4, 1),
        ("native", 4, 2),
        ("native_quote", 4, 2, 2),
    }
    target = [item for item in details.values() if item["sender_id"] == TARGET]
    other = [item for item in details.values() if item["sender_id"] == OTHER]
    assert {item["text"] for item in target} == {FIRST, NESTED, QUOTED}
    assert {item["text"] for item in other} == {"Other original body", REPLY}
    assert QUOTED not in details[("native", 4, 2)]["text"]
    assert details[("native", 4, 2)]["id"] == PARENT + "_forwarded_4_forwarded_2"
    quote = details[("native_quote", 4, 2, 2)]
    assert quote["id"] == PARENT + "_forwarded_4_forwarded_2_quoted_2"
    assert quote["anchor_id"] == "wechat_message_" + PARENT + "_forwarded_4_forwarded_2"
    assert proof == before


def test_native_details_preserve_unknown_clocks_source_ids_and_literal_context():
    proof = _tree()
    details = {tuple(item["path"]): item for item in _details(proof)}
    first = details[("native", 1)]
    assert first["timestamp_value"] == ""
    assert first["time_basis"] == "Unavailable source time"
    assert first["source_clock"] == "1970-01-01 08:00:00"
    assert "possible_epoch_zero_placeholder" in first["quality_flags"]
    inner = details[("native", 4, 1)]
    assert inner["timestamp_value"] == "2019-01-01T08:00:00+08:00"
    assert inner["source_clock"] == "1546300800"
    assert inner["source_ids"] == {"srcMsgSvrId": "7234567890123456789"}
    assert all(item["original_conversation_unknown"] for item in details.values())
    assert not any(
        "conversation_id" in item or "peer_id" in item for item in details.values()
    )
    assert details[("native_quote", 4, 2, 2)]["source_ids"] == {
        "svrid": "9223372036854775806",
        "localid": "37",
    }


@pytest.mark.parametrize(
    "nested,path,suffix",
    [
        (False, ["native", "quote", 1], "_quote_forwarded_1"),
        (True, ["native", 1, "quote", 1], "_forwarded_1_quote_forwarded_1"),
    ],
)
def test_quoted_forwarded_originals_keep_authors_and_actual_transcript_anchors(
    nested,
    path,
    suffix,
):
    proof = _quoted_forwarded_proof(nested=nested)
    before = deepcopy(proof)
    details = _details(proof)
    original = next(item for item in details if item["sender_id"] == TARGET)
    assert original["text"] == NESTED
    assert original["path"] == path
    assert original["id"] == PARENT + suffix
    assert original["anchor_id"] == "wechat_message_" + PARENT + suffix
    assert original["timestamp_value"] == ""
    assert original["source_clock"] == "0"
    assert original["original_conversation_unknown"] is True
    assert (
        embedded.native_detail(
            proof,
            path,
            parent_id=PARENT,
            account_id=OWNER,
        )
        == original
    )
    if nested:
        reply = next(item for item in details if item["sender_id"] == OTHER)
        assert reply["text"] == REPLY and NESTED not in reply["text"]
    assert proof == before


@pytest.mark.parametrize("chat", ["", "explicit-original@chatroom"])
def test_direct_quote_is_original_authors_evidence_with_parent_anchor(chat):
    proof = _direct_quote(chat=chat, clock="1546300800")
    before = deepcopy(proof)
    (item,) = _details(proof)
    assert item["path"] == ["quote", 2]
    assert item["id"] == PARENT + "_quoted_2"
    assert item["anchor_id"] == "wechat_message_" + PARENT
    assert item["text"] == QUOTED and item["sender_id"] == TARGET
    assert REPLY not in item["text"]
    assert item["timestamp_value"] == "2019-01-01T08:00:00+08:00"
    assert item["original_conversation_unknown"] is (not bool(chat))
    assert item["original_chat_id"] == chat
    assert item["direction"] == "unknown"
    assert proof == before


@pytest.mark.parametrize("message_type", [3, 47, 49, True, "1"])
def test_quoted_nontext_or_invalid_type_is_not_an_author_statement(message_type):
    proof = _direct_quote()
    proof["text_components"][1]["original_message_type"] = message_type
    proof["quoted_reference"]["original_message_type"] = message_type
    assert _details(proof) == []


@pytest.mark.parametrize("present", [False, True])
def test_unknown_quoted_type_keeps_literal_text_without_inventing_type(present):
    proof = _direct_quote()
    for part in (proof["text_components"][1], proof["quoted_reference"]):
        if present:
            part["original_message_type"] = None
        else:
            part.pop("original_message_type")
    (item,) = _details(proof)
    assert item["text"] == QUOTED
    assert item["original_message_type"] is None


def test_quoted_reply_with_forwarding_subtype_cannot_create_native_author_details():
    proof = _direct_quote()
    proof["appmsg_subtype"] = 19
    assert _details(proof) == []


@pytest.mark.parametrize(
    "path",
    [
        ["native", True],
        ["native", 0],
        ["native", -1],
        ["native", "1"],
        ["native_quote", 4, 2, True],
        ["quote"],
        ["quote", 0],
        ["foreign", 1],
        ("native", 1),
        [],
        None,
    ],
)
def test_detail_lookup_rejects_noncanonical_component_paths(path):
    assert (
        embedded.native_detail(_tree(), path, parent_id=PARENT, account_id=OWNER)
        is None
    )


def test_detail_lookup_returns_only_the_exact_component():
    proof = _tree()
    assert (
        embedded.native_detail(
            proof, ["native", 4, 1], parent_id=PARENT, account_id=OWNER
        )["text"]
        == NESTED
    )
    assert (
        embedded.native_detail(
            proof, ["native", 4, 3], parent_id=PARENT, account_id=OWNER
        )
        is None
    )


@pytest.mark.parametrize(
    "proof", [None, [], "literal", {"message_type": True}, {"message_type": 1}]
)
def test_non_native_or_malformed_envelopes_do_not_create_author_references(proof):
    assert _details(proof) == []


@pytest.mark.parametrize(
    "parent,account", [("invalid", OWNER), (PARENT, ""), (PARENT, None)]
)
def test_author_references_require_valid_parent_and_viewer_identity(parent, account):
    assert (
        list(embedded.native_details(_tree(), parent_id=parent, account_id=account))
        == []
    )


@pytest.mark.parametrize(
    "damage", ["resource_body", "wrong_group_body", "over_budget", "invalid_quote_role"]
)
def test_invalid_native_metadata_cannot_be_promoted_to_author_statements(damage):
    proof = _tree()
    if damage == "resource_body":
        proof["text_components"][2]["text"] = "Invented resource caption"
    elif damage == "wrong_group_body":
        proof["text_components"][3]["text"] = "Conflicting wrapper body"
    elif damage == "over_budget":
        proof["text_components"][0]["text"] = "x" * (2 * 1024 * 1024 + 1)
    else:
        proof["text_components"][3]["text_components"][1]["appmsg_subtype"] = 19
    assert _details(proof) == []


@pytest.mark.parametrize(
    "damage",
    ["ambiguous_reply", "ambiguous_quote", "invalid_metadata", "wrong_subtype"],
)
def test_invalid_native_quote_roles_never_attribute_flattened_quote_to_forwarder(
    damage,
):
    proof = _direct_quote()
    if damage == "ambiguous_reply":
        proof["text_components"].append(
            {"kind": "reply_text", "text": "Conflicting reply"}
        )
    elif damage == "ambiguous_quote":
        proof["text_components"].append(deepcopy(proof["text_components"][1]))
    elif damage == "invalid_metadata":
        proof["text_components"][1]["source_ids"] = {"svrid": True}
    else:
        proof["appmsg_subtype"] = 19
    before = deepcopy(proof)
    assert (
        analysis._own_text(
            REPLY + "\n\n" + QUOTED,
            proof,
            account_id=OWNER,
            parent_id=PARENT,
        )
        == ""
    )
    assert proof == before


@pytest.fixture
def candidate_store(tmp_path):
    history = tmp_path / "history.sqlite3"
    with sqlite3.connect(history) as connection:
        connection.execute(
            "CREATE TABLE messages(id TEXT PRIMARY KEY,provenance TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO messages VALUES(?,?)",
            [
                (PARENT, json.dumps(_tree())),
                ("b" * 64, json.dumps(_direct_quote())),
                (
                    "c" * 64,
                    json.dumps(
                        {
                            "message_type": 1,
                            "text_components": [{"source_sender_id": TARGET}],
                        }
                    ),
                ),
            ],
        )
    return SimpleNamespace(history=history, directory=tmp_path / "candidates")


def _candidates(fixture, author=TARGET):
    return list(
        embedded.native_candidate_ids(fixture.history, fixture.directory, author)
    )


def test_shared_candidate_index_keeps_only_author_parent_hints(
    candidate_store, monkeypatch
):
    fixture = candidate_store
    before = fixture.history.read_bytes()
    assert _candidates(fixture) == [PARENT, "b" * 64]
    target = fixture.directory / "native-author-candidates.sqlite3"
    with sqlite3.connect(target) as connection:
        fields = {row[1] for row in connection.execute("PRAGMA table_info(candidates)")}
        assert fields == {"author", "parent_id"}
        dump = "\n".join(connection.iterdump())
        assert FIRST not in dump and QUOTED not in dump and "1970-01-01" not in dump
    assert FIRST.encode() not in target.read_bytes()
    assert before == fixture.history.read_bytes()
    original = embedded._readonly

    def warm_only(path):
        assert Path(path) != fixture.history, (
            "A warm author lookup must not rescan messages."
        )
        return original(path)

    monkeypatch.setattr(embedded, "_readonly", warm_only)
    assert _candidates(fixture, OTHER) == [PARENT]
    assert _candidates(fixture) == [PARENT, "b" * 64]


def test_changed_history_invalidates_shared_author_hints(candidate_store):
    fixture = candidate_store
    assert _candidates(fixture) == [PARENT, "b" * 64]
    with sqlite3.connect(fixture.history) as connection:
        connection.execute(
            "INSERT INTO messages VALUES(?,?)", ("d" * 64, json.dumps(_tree()))
        )
    assert _candidates(fixture) == [PARENT, "b" * 64, "d" * 64]


def test_invalid_candidate_rows_are_counted_without_blocking_valid_authors(
    candidate_store,
):
    fixture = candidate_store
    invalid = [
        "broken text_components {",
        json.dumps([{"text_components": []}]),
        json.dumps(
            {
                "message_type": 49,
                "text_extraction_kind": "forwarded_text",
                "text_components": [{}] * 20_001,
            }
        ),
    ]
    with sqlite3.connect(fixture.history) as connection:
        connection.executemany(
            "INSERT INTO messages VALUES(?,?)",
            [(str(number) * 64, value) for number, value in enumerate(invalid, 4)],
        )
    assert _candidates(fixture) == [PARENT, "b" * 64]
    with sqlite3.connect(
        fixture.directory / "native-author-candidates.sqlite3"
    ) as connection:
        assert (
            connection.execute(
                "SELECT value FROM info WHERE key='invalid_rows'"
            ).fetchone()[0]
            == "3"
        )


def test_source_changed_during_hint_build_cannot_publish_a_cache(
    candidate_store, monkeypatch
):
    fixture = candidate_store
    original = embedded._authors
    changed = []

    def change_source(value):
        yield from original(value)
        if not changed:
            changed.append(True)
            with sqlite3.connect(fixture.history) as connection:
                connection.execute(
                    "INSERT INTO messages VALUES(?,?)", ("d" * 64, json.dumps(_tree()))
                )

    monkeypatch.setattr(embedded, "_authors", change_source)
    with pytest.raises(ValueError, match="changed while indexing"):
        _candidates(fixture)
    assert not (fixture.directory / "native-author-candidates.sqlite3").exists()
    assert not list(fixture.directory.glob(".native-authors-*"))


def test_source_changed_while_streaming_hints_is_rejected(candidate_store):
    fixture = candidate_store
    iterator = embedded.native_candidate_ids(fixture.history, fixture.directory, TARGET)
    assert next(iterator) == PARENT
    with sqlite3.connect(fixture.history) as connection:
        connection.execute(
            "INSERT INTO messages VALUES(?,?)", ("d" * 64, json.dumps(_tree()))
        )
    with pytest.raises(ValueError, match="changed while reading"):
        list(iterator)


@pytest.mark.parametrize("target", ["source", "directory", "cache"])
def test_symlinked_hint_inputs_are_rejected_without_replacing_links(
    candidate_store, tmp_path, target
):
    fixture = candidate_store
    if target == "source":
        linked = tmp_path / "linked-history.sqlite3"
        linked.symlink_to(fixture.history)
        fixture = SimpleNamespace(history=linked, directory=fixture.directory)
    elif target == "directory":
        real = tmp_path / "real-candidates"
        real.mkdir()
        fixture.directory.symlink_to(real, target_is_directory=True)
        linked = fixture.directory
    else:
        _candidates(fixture)
        linked = fixture.directory / "native-author-candidates.sqlite3"
        original = fixture.directory / "retained-index.sqlite3"
        linked.rename(original)
        linked.symlink_to(original)
    with pytest.raises(ValueError, match="symbolic|local regular"):
        _candidates(fixture)
    assert linked.is_symlink()


def test_nonregular_source_path_is_rejected_before_cache_creation(tmp_path):
    source = tmp_path / "source-directory"
    source.mkdir()
    cache = tmp_path / "cache"
    with pytest.raises(ValueError, match="local regular"):
        list(embedded.native_candidate_ids(source, cache, TARGET))
    assert not cache.exists()


def test_dataless_source_is_rejected_without_attempting_hydration(
    candidate_store, monkeypatch
):
    fixture = candidate_store
    original_stat = Path.stat
    original_readonly = embedded._readonly

    def dataless(path, *args, **kwargs):
        value = original_stat(path, *args, **kwargs)
        if path == fixture.history and kwargs.get("follow_symlinks", True):
            return SimpleNamespace(
                st_dev=value.st_dev,
                st_ino=value.st_ino,
                st_size=value.st_size,
                st_mtime_ns=value.st_mtime_ns,
                st_ctime_ns=value.st_ctime_ns,
                st_mode=value.st_mode,
                st_flags=0x40000000,
            )
        return value

    def no_cloud_read(path):
        assert path != fixture.history, "A dataless source must not be opened."
        return original_readonly(path)

    monkeypatch.setattr(Path, "stat", dataless)
    monkeypatch.setattr(embedded, "_readonly", no_cloud_read)
    with pytest.raises(ValueError, match="local regular"):
        _candidates(fixture)
    assert not fixture.directory.exists()


@pytest.mark.parametrize("suffix", ["-wal", "-journal", "-shm"])
def test_unpublished_source_journal_is_rejected(candidate_store, suffix):
    fixture = candidate_store
    Path(str(fixture.history) + suffix).write_bytes(b"unpublished source state")
    with pytest.raises(ValueError, match="unpublished database journal"):
        _candidates(fixture)


@pytest.fixture
def native_activity_store(tmp_path):
    metadata = source(OWNER)
    tree, quote = _tree(), _direct_quote()
    records = [
        record(
            metadata,
            "native-forward",
            peer="444111222@chatroom",
            sender_id=OTHER,
            text=native_components_text(tree["text_components"]),
            **{key: value for key, value in tree.items() if key != "source_time"},
        ),
        record(
            metadata,
            "native-reply",
            peer="444111222@chatroom",
            sender_id=OTHER,
            text=native_components_text(quote["text_components"]),
            **{key: value for key, value in quote.items() if key != "source_time"},
        ),
        record(
            metadata,
            "target-primary",
            peer="444111222@chatroom",
            sender_id=TARGET,
            text="Target primary body",
        ),
    ]
    merge(tmp_path, [metadata], records)
    history, registry = wechat_archive_path(tmp_path), contact_registry_path(tmp_path)
    with sqlite3.connect(registry) as connection:
        initialize_contact_registry(connection)
        target_id, other_id = (
            insert_contact(connection, TARGET),
            insert_contact(connection, OTHER),
        )
        for identifier, sender in ((target_id, TARGET), (other_id, OTHER)):
            bind(connection, identifier, source=metadata["source_id"], sender=sender)
    with sqlite3.connect(history) as connection:
        parents = dict(
            connection.execute(
                "SELECT json_extract(provenance,'$.record_id'),id FROM messages"
            )
        )
    return SimpleNamespace(
        store=tmp_path,
        history=history,
        registry=registry,
        target_id=target_id,
        other_id=other_id,
        parents=parents,
    )


def _publish_native_forwarded_proof(fixture, proof):
    identifier = fixture.parents["native-forward"]
    with sqlite3.connect(fixture.history) as connection:
        provenance = json.loads(
            connection.execute(
                "SELECT provenance FROM messages WHERE id=?",
                (identifier,),
            ).fetchone()[0]
        )
        provenance.update(proof)
        text = native_components_text(proof["text_components"])
        connection.execute(
            "UPDATE messages SET text=?,search_text=?,provenance=? WHERE id=?",
            (text, text.casefold(), json.dumps(provenance), identifier),
        )


def _render_native_forwarded_parent(fixture):
    from app.core.chat_history_browser import query_chat_history

    identifier = fixture.parents["native-forward"]
    with sqlite3.connect(fixture.history) as connection:
        (conversation,) = connection.execute(
            "SELECT conversation_id FROM messages WHERE id=?",
            (identifier,),
        ).fetchone()
    page = query_chat_history(fixture.store, source="wechat", session=conversation)
    return next(item for item in page.items if item.stable_id == identifier)


@pytest.mark.parametrize("viewer_name", ["", "My preferred target"])
def test_native_forwarded_transcript_resolves_registered_authors_without_changing_source(
    native_activity_store,
    viewer_name,
):
    fixture = native_activity_store
    proof = _tree()
    proof["text_components"][0]["source_sender_label"] = "Flora"
    proof["text_components"][1]["source_sender_label"] = "Flora"
    proof["text_components"][3]["text_components"][0]["source_sender_label"] = "Flora"
    proof["text_components"].append(
        {
            "kind": "forwarded_text",
            "text": "Unknown author's own body",
            "source_sender_id": "wxid_unregistered",
            "source_sender_label": "Flora",
        }
    )
    _publish_native_forwarded_proof(fixture, proof)
    with sqlite3.connect(fixture.registry) as connection:
        observe(connection, fixture.target_id, "nickname", "Canonical target")
        observe(connection, fixture.other_id, "nickname", "Canonical other")
    if viewer_name:
        from app.core.wechat_display_names import display_names_path

        display_names_path(fixture.store).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "contacts": {fixture.target_id: viewer_name},
                }
            )
        )
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    parent = _render_native_forwarded_parent(fixture)
    first, other, _, group, unknown = parent.messaging["forwarded_record"]["items"]
    nested = group.messaging["forwarded_record"]["items"][0]
    for child in (first, nested):
        contact = child.messaging["contact"]
        assert contact["id"] == fixture.target_id
        assert contact["identity_status"] == "verified"
        assert contact["display_name"] == "Canonical target"
        assert contact["source_display_name"] == child.author_label == "Flora"
        assert child.messaging["viewer_display_name"] == (
            viewer_name or "Canonical target"
        )
        assert child.messaging["sender_id"] == TARGET
        assert child.messaging["account_id"] == OWNER
    assert first.content_text == FIRST and nested.content_text == NESTED
    assert first.last_seen_at == ""
    assert nested.last_seen_at == "2019-01-01T08:00:00+08:00"
    assert first.messaging["provenance"]["parent_message_id"] == parent.stable_id
    assert nested.messaging["provenance"]["parent_message_id"] == parent.stable_id
    assert other.messaging["contact"]["id"] == fixture.other_id
    assert other.messaging["contact"]["source_display_name"] == "Flora"
    assert other.messaging["contact"]["id"] != first.messaging["contact"]["id"]
    assert unknown.messaging["contact"]["identity_status"] == "unresolved"
    assert unknown.messaging["sender_id"] == "wxid_unregistered"
    assert unknown.author_label == "Flora"
    assert parent.messaging["contact"]["id"] == fixture.other_id
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


@pytest.mark.parametrize("nested", [False, True])
def test_native_quoted_forwarded_transcript_uses_the_nested_authors_contact(
    native_activity_store,
    nested,
):
    fixture = native_activity_store
    proof = _quoted_forwarded_proof(nested=nested)
    if nested:
        leaf = proof["text_components"][0]["text_components"][1]["text_components"][0]
    else:
        leaf = proof["text_components"][1]["text_components"][0]
    leaf["source_sender_label"] = "Flora"
    _publish_native_forwarded_proof(fixture, proof)
    with sqlite3.connect(fixture.registry) as connection:
        observe(connection, fixture.target_id, "nickname", "Canonical target")
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    parent = _render_native_forwarded_parent(fixture)
    if nested:
        (replying,) = parent.messaging["forwarded_record"]["items"]
        (child,) = replying.messaging["quoted_reply"]["forwarded_record"]["items"]
        assert replying.messaging["contact"]["id"] == fixture.other_id
    else:
        (child,) = parent.messaging["quoted_reply"]["forwarded_record"]["items"]
    assert child.messaging["contact"]["id"] == fixture.target_id
    assert child.messaging["contact"]["identity_status"] == "verified"
    assert child.messaging["contact"]["display_name"] == "Canonical target"
    assert child.messaging["contact"]["source_display_name"] == "Flora"
    assert child.author_label == "Flora" and child.content_text == NESTED
    assert child.messaging["sender_id"] == TARGET
    assert child.messaging["account_id"] == OWNER
    assert child.last_seen_at == ""
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


@pytest.mark.parametrize(
    "damage",
    [
        "owner",
        "account_key",
        "partition",
        "unverified_owner",
        "snapshot_source",
        "snapshot_metadata",
        "snapshot_metadata_owner",
        "snapshot_metadata_source",
        "snapshot_missing",
    ],
)
def test_native_forwarded_transcript_cannot_bind_authors_under_invalid_owner_scope(
    native_activity_store,
    damage,
):
    fixture = native_activity_store
    proof = _tree()
    proof["text_components"][0]["source_sender_label"] = "Flora"
    _publish_native_forwarded_proof(fixture, proof)
    with sqlite3.connect(fixture.history) as connection:
        if damage == "owner":
            connection.execute("UPDATE partitions SET account_id='wxid_wrong_owner'")
        elif damage == "account_key":
            connection.execute(
                "UPDATE conversations SET account_key='account:wxid_wrong_owner'"
            )
        elif damage == "partition":
            connection.execute("UPDATE partitions SET id='source-wrong-partition'")
            connection.execute(
                "UPDATE conversations SET partition_id='source-wrong-partition'"
            )
        elif damage == "unverified_owner":
            connection.execute(
                "UPDATE partitions SET identity_status='Source account unconfirmed'"
            )
        elif damage == "snapshot_source":
            connection.execute(
                "UPDATE source_snapshots SET source_id='source-wrong-snapshot'"
            )
        elif damage == "snapshot_metadata":
            connection.execute("UPDATE source_snapshots SET metadata='{}'")
        elif damage in {"snapshot_metadata_owner", "snapshot_metadata_source"}:
            for snapshot_id, raw in connection.execute(
                "SELECT id,metadata FROM source_snapshots"
            ).fetchall():
                metadata = json.loads(raw)
                key = "source_account_id" if damage.endswith("owner") else "source_id"
                metadata[key] = (
                    "wxid_wrong_owner"
                    if damage.endswith("owner")
                    else "source-wrong-metadata"
                )
                connection.execute(
                    "UPDATE source_snapshots SET metadata=? WHERE id=?",
                    (json.dumps(metadata), snapshot_id),
                )
        else:
            connection.execute("DELETE FROM source_snapshots")
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    parent = _render_native_forwarded_parent(fixture)
    child = parent.messaging["forwarded_record"]["items"][0]
    assert child.messaging["contact"]["identity_status"] == "unresolved"
    assert child.messaging["sender_id"] == TARGET
    assert child.author_label == "Flora"
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def test_native_forwarded_transcript_never_borrows_an_outer_room_sender_binding(
    native_activity_store,
):
    fixture = native_activity_store
    unregistered = "wxid_room_scoped_unregistered"
    proof = _tree()
    proof["text_components"][0].update(
        {
            "source_sender_id": unregistered,
            "source_sender_label": "Flora",
        }
    )
    _publish_native_forwarded_proof(fixture, proof)
    with sqlite3.connect(fixture.history) as connection:
        source_id, outer_room = connection.execute(
            "SELECT c.partition_id,c.peer_id FROM messages m "
            "JOIN conversations c ON c.id=m.conversation_id WHERE m.id=?",
            (fixture.parents["native-forward"],),
        ).fetchone()
    with sqlite3.connect(fixture.registry) as connection:
        bind(
            connection,
            fixture.target_id,
            source=source_id,
            sender=unregistered,
            room=outer_room,
        )
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    parent = _render_native_forwarded_parent(fixture)
    child = parent.messaging["forwarded_record"]["items"][0]
    assert child.messaging["contact"]["identity_status"] == "unresolved"
    assert child.messaging["contact"].get("id") != fixture.target_id
    assert child.messaging["sender_id"] == unregistered
    assert child.author_label == "Flora" and child.content_text == FIRST
    assert child.messaging["peer_id"] == ""
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def test_contact_index_includes_native_authors_without_copying_or_misattributing(
    native_activity_store,
):
    fixture = native_activity_store
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    page = analysis.query_contact_analysis(fixture.store, fixture.target_id)
    assert page["status"] == "ready"
    assert page["total_count"] == 5
    assert page["observation_count"] == 5
    assert {item["content_text"] for item in page["items"]} == {
        FIRST,
        NESTED,
        QUOTED,
        "Target primary body",
    }
    assert sum(item["content_text"] == QUOTED for item in page["items"]) == 2
    assert {item["sender_id"] for item in page["items"]} == {TARGET}
    first = next(item for item in page["items"] if item["content_text"] == FIRST)
    assert first["component_path"] == ["native", 1]
    assert first["timestamp_value"] == ""
    assert first["provenance"]["source_time"] == "1970-01-01 08:00:00"
    assert first["original_conversation_unknown"] is True
    nested = next(item for item in page["items"] if item["content_text"] == NESTED)
    assert nested["provenance"]["original_source_ids"] == {
        "srcMsgSvrId": "7234567890123456789",
    }
    other = analysis.query_contact_analysis(fixture.store, fixture.other_id)
    assert other["total_count"] == 3
    assert {item["content_text"] for item in other["items"]} == {
        "Other original body",
        REPLY,
    }
    assert all(QUOTED not in item["content_text"] for item in other["items"])
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())
    with sqlite3.connect(
        analysis.contact_analysis_cache_path(fixture.store, fixture.target_id)
    ) as cache:
        dump = "\n".join(cache.iterdump())
        assert FIRST not in dump and QUOTED not in dump and REPLY not in dump
        assert cache.execute("SELECT COUNT(*) FROM refs").fetchone()[0] == 5


def test_native_contact_search_keeps_quoted_and_replying_authors_separate(
    native_activity_store,
):
    fixture = native_activity_store
    quoted = analysis.query_contact_analysis(
        fixture.store, fixture.target_id, query=QUOTED
    )
    assert quoted["total_count"] == 2
    assert (
        analysis.query_contact_analysis(fixture.store, fixture.other_id, query=QUOTED)[
            "total_count"
        ]
        == 0
    )
    assert (
        analysis.query_contact_analysis(fixture.store, fixture.target_id, query=REPLY)[
            "total_count"
        ]
        == 0
    )


def test_native_contact_references_link_to_the_source_bubble_not_an_invented_chat(
    native_activity_store,
):
    fixture = native_activity_store
    page = analysis.query_contact_analysis(fixture.store, fixture.target_id)
    parents = fixture.parents
    for item in page["items"]:
        location = analysis.message_source_location(fixture.store, item["id"])
        assert location is not None
        assert location["conversation_id"] == item["conversation_id"]
        assert location["anchor_id"] == item["anchor_id"]
    quoted = next(
        item for item in page["items"] if item["component_path"] == ["quote", 2]
    )
    assert quoted["id"] == parents["native-reply"] + "_quoted_2"
    assert quoted["anchor_id"] == "wechat_message_" + parents["native-reply"]
    nested = next(
        item
        for item in page["items"]
        if item["component_path"] == ["native_quote", 4, 2, 2]
    )
    assert (
        nested["anchor_id"]
        == "wechat_message_" + parents["native-forward"] + "_forwarded_4_forwarded_2"
    )


def test_native_contact_warm_query_does_not_rescan_source_authors(
    native_activity_store,
    monkeypatch,
):
    fixture = native_activity_store
    first = analysis.query_contact_analysis(fixture.store, fixture.target_id)

    def no_scan(*args, **kwargs):
        raise AssertionError("A warm contact query must not scan native author hints.")

    monkeypatch.setattr(analysis, "_candidate_native_batches", no_scan)
    result = analysis.query_contact_analysis(
        fixture.store, fixture.target_id, page_size=1, page=5
    )
    assert result["total_count"] == first["total_count"] == 5
    assert result["current_page"] == result["total_pages"] == 5


def test_legacy_misattributed_primary_quote_is_removed_only_from_derived_references(
    native_activity_store,
    monkeypatch,
):
    fixture = native_activity_store
    identifier = fixture.parents["native-reply"]
    with closing(sqlite3.connect(fixture.history)) as connection, connection:
        provenance = json.loads(
            connection.execute(
                "SELECT provenance FROM messages WHERE id=?",
                (identifier,),
            ).fetchone()[0]
        )
        provenance["text_components"].append(
            {
                "kind": "reply_text",
                "text": "Ambiguous extra reply role",
            }
        )
        flattened = native_components_text(provenance["text_components"])
        connection.execute(
            "UPDATE messages SET text=?,search_text=?,provenance=? WHERE id=?",
            (flattened, flattened.casefold(), json.dumps(provenance), identifier),
        )
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    current_own_text = analysis._own_text

    def legacy_raw_fallback(text, provenance, **kwargs):
        replies = [
            part
            for part in provenance.get("text_components", [])
            if part.get("kind") == "reply_text"
        ]
        if (
            provenance.get("text_extraction_kind") == "quoted_reply"
            and len(replies) > 1
        ):
            return text
        return current_own_text(text, provenance, **kwargs)

    with monkeypatch.context() as legacy:
        legacy.setattr(analysis, "_own_text", legacy_raw_fallback)
        retained = analysis.query_contact_analysis(fixture.store, fixture.other_id)
    old = next(item for item in retained["items"] if item["id"] == identifier)
    assert old["content_text"] == flattened
    assert QUOTED in old["content_text"] and old["sender_id"] == OTHER
    old_version = "v1.5.0-codex.0"
    identities = {
        str(path.resolve()): analysis._identity(path)
        for path in (fixture.history, fixture.registry)
    }
    fingerprint = hashlib.sha256(
        analysis._json([old_version, identities]).encode()
    ).hexdigest()
    cache_path = analysis.contact_analysis_cache_path(fixture.store, fixture.other_id)
    with closing(sqlite3.connect(cache_path)) as cache, cache:
        cache.executemany(
            "UPDATE info SET value=? WHERE key=?",
            [
                (old_version, "version"),
                (fingerprint, "fingerprint"),
            ],
        )

    def no_primary_rescan(*args, **kwargs):
        raise AssertionError(
            "An unchanged legacy primary cache must be revalidated during migration."
        )

    with monkeypatch.context() as upgrading:
        upgrading.setattr(analysis, "_candidate_batches", no_primary_rescan)
        repaired = analysis.query_contact_analysis(fixture.store, fixture.other_id)
    assert repaired["total_count"] == repaired["observation_count"] == 2
    assert {item["content_text"] for item in repaired["items"]} == {
        "Other original body",
        REPLY,
    }
    assert not any(QUOTED in item["content_text"] for item in repaired["items"])
    with closing(sqlite3.connect(cache_path)) as cache, cache:
        assert (
            cache.execute("SELECT 1 FROM refs WHERE id=?", (identifier,)).fetchone()
            is None
        )
    target = analysis.query_contact_analysis(
        fixture.store, fixture.target_id, query=QUOTED
    )
    assert target["total_count"] == 2
    assert {item["sender_id"] for item in target["items"]} == {TARGET}
    assert {item["content_text"] for item in target["items"]} == {QUOTED}
    with closing(sqlite3.connect(fixture.history)) as history, history:
        assert (
            history.execute(
                "SELECT text FROM messages WHERE id=?", (identifier,)
            ).fetchone()[0]
            == flattened
        )
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


@pytest.mark.parametrize(
    "nested,suffix",
    [
        (False, "_quote_forwarded_1"),
        (True, "_forwarded_1_quote_forwarded_1"),
    ],
)
def test_quoted_forwarded_contact_reference_navigates_to_rendered_original(
    native_activity_store,
    nested,
    suffix,
):
    fixture = native_activity_store
    identifier = fixture.parents["native-forward"]
    proof = _quoted_forwarded_proof(nested=nested)
    with sqlite3.connect(fixture.history) as connection:
        original = json.loads(
            connection.execute(
                "SELECT provenance FROM messages WHERE id=?",
                (identifier,),
            ).fetchone()[0]
        )
        original.update(proof)
        text = native_components_text(proof["text_components"])
        connection.execute(
            "UPDATE messages SET text=?,search_text=?,provenance=? WHERE id=?",
            (text, text.casefold(), json.dumps(original), identifier),
        )
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    page = analysis.query_contact_analysis(
        fixture.store, fixture.target_id, query=NESTED
    )
    (item,) = page["items"]
    assert item["content_text"] == NESTED
    assert item["id"] == identifier + suffix
    assert item["anchor_id"] == "wechat_message_" + identifier + suffix
    location = analysis.message_source_location(fixture.store, item["id"])
    assert location is not None
    assert location["anchor_id"] == item["anchor_id"]
    assert location["parent_message_id"] == identifier
    assert location["conversation_id"] == item["conversation_id"]
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


@pytest.mark.parametrize("forwarded", [False, True])
@pytest.mark.parametrize("reply_state", ["missing", "empty"])
def test_quote_only_native_parent_renders_and_keeps_source_jump_anchor(
    native_activity_store,
    tmp_path,
    forwarded,
    reply_state,
):
    fixture = native_activity_store
    identifier = fixture.parents["native-reply"]
    proof = _quoted_forwarded_proof() if forwarded else _direct_quote()
    if reply_state == "missing":
        proof["text_components"] = [
            part for part in proof["text_components"] if part["kind"] != "reply_text"
        ]
    else:
        proof["text_components"][0]["text"] = ""
    text = native_components_text(proof["text_components"])
    with sqlite3.connect(fixture.history) as connection:
        provenance = json.loads(
            connection.execute(
                "SELECT provenance FROM messages WHERE id=?",
                (identifier,),
            ).fetchone()[0]
        )
        provenance.update(proof)
        connection.execute(
            "UPDATE messages SET text=?,search_text=?,provenance=? WHERE id=?",
            (text, text.casefold(), json.dumps(provenance), identifier),
        )
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    page = analysis.query_contact_analysis(fixture.store, fixture.target_id)
    reference = next(
        item for item in page["items"] if item["parent_message_id"] == identifier
    )
    with patch(
        "app.web.app.SavedConfigStore.load",
        return_value=SavedConfigStore(CrawlConfig()),
    ):
        app = create_app(
            fixture.store,
            computer_use_settings_path=tmp_path / "agent.json",
            computer_use_runtime_root=tmp_path / "runtime",
            agent_external_operations_enabled=False,
        )
    app.config.update(TESTING=True)
    client = app.test_client()
    redirect = client.get("/browser/wechat/messages/" + reference["id"])
    assert redirect.status_code == 302
    destination = urlsplit(redirect.headers["Location"])
    assert destination.fragment == reference["anchor_id"]
    response = client.get(destination.path + "?" + destination.query)
    assert response.status_code == 200

    class SourceBubble(HTMLParser):
        def __init__(self):
            super().__init__()
            self.depth = 0
            self.found = False
            self.ids = set()
            self.text = []
            self.quote = False
            self.forwarded = False

        def handle_starttag(self, tag, attributes):
            attributes = dict(attributes)
            if (
                tag == "article"
                and attributes.get("id") == "wechat_message_" + identifier
            ):
                self.found = True
                self.depth = 1
            elif tag == "article" and self.depth:
                self.depth += 1
            if self.depth:
                self.ids.add(attributes.get("id", ""))
                self.quote |= (
                    tag == "blockquote"
                    and attributes.get("aria-label") == "Quoted message"
                )
                self.forwarded |= (
                    tag == "details" and "data-wechat-forwarded" in attributes
                )

        def handle_endtag(self, tag):
            if tag == "article" and self.depth:
                self.depth -= 1

        def handle_data(self, value):
            if self.depth:
                self.text.append(value)

    document = SourceBubble()
    document.feed(response.get_data(as_text=True))
    assert document.found
    assert reference["anchor_id"] in document.ids
    assert reference["content_text"] in "".join(document.text)
    assert document.forwarded if forwarded else document.quote
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def _original_conversation(connection, message_id, peer):
    connection.row_factory = sqlite3.Row
    old = connection.execute(
        "SELECT c.* FROM conversations c JOIN messages m ON m.conversation_id=c.id WHERE m.id=?",
        (message_id,),
    ).fetchone()
    copied = dict(old)
    copied.update(
        id="wechat:" + hashlib.sha256(peer.encode()).hexdigest(),
        peer_id=peer,
        title="Explicit original source room",
        message_count=1,
    )
    connection.execute(
        "INSERT INTO conversations("
        + ",".join(copied)
        + ") VALUES("
        + ",".join("?" for _ in copied)
        + ")",
        tuple(copied.values()),
    )
    return copied["id"]


def _bridge_original(fixture, *, body=QUOTED):
    identifier = fixture.parents["target-primary"]
    with sqlite3.connect(fixture.history) as connection:
        original = _original_conversation(
            connection, identifier, "original-room@chatroom"
        )
        connection.execute(
            "UPDATE messages SET conversation_id=?,text=?,search_text=? WHERE id=?",
            (original, body, body.casefold(), identifier),
        )
    return identifier


def _replace_quote_field(fixture, key, value, *, remove=False):
    def update(node):
        if isinstance(node, dict):
            targets = []
            if node.get("kind") == "quoted_text":
                targets.append(node)
            if isinstance(node.get("quoted_reference"), dict):
                targets.append(node["quoted_reference"])
            for target in targets:
                if remove:
                    target.pop(key, None)
                else:
                    target[key] = deepcopy(value)
            for part in node.values():
                if isinstance(part, (dict, list)):
                    update(part)
        elif isinstance(node, list):
            for part in node:
                update(part)

    with sqlite3.connect(fixture.history) as connection:
        rows = connection.execute("SELECT id,provenance FROM messages").fetchall()
        for identifier, raw in rows:
            provenance = json.loads(raw)
            update(provenance)
            connection.execute(
                "UPDATE messages SET provenance=? WHERE id=?",
                (json.dumps(provenance), identifier),
            )


def test_native_explicit_original_event_links_preserve_all_receiving_observations(
    native_activity_store,
):
    fixture = native_activity_store
    primary_id = _bridge_original(fixture)
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    page = analysis.query_contact_analysis(fixture.store, fixture.target_id)
    assert page["total_count"] == 3
    assert page["observation_count"] == 5
    event = next(item for item in page["items"] if item["content_text"] == QUOTED)
    assert event["id"] == primary_id
    assert event["is_embedded"] is False
    assert event["observation_count"] == 3
    assert event["peer_id"] == "original-room@chatroom"
    assert {origin["peer_id"] for origin in event["observations"]} == {
        "original-room@chatroom",
        "444111222@chatroom",
    }
    raw = list(
        analysis.export_contact_analysis_items(
            fixture.store,
            fixture.target_id,
            query=QUOTED,
            source_fingerprint=page["source_fingerprint"],
            source_occurrences=True,
        )
    )
    assert len(raw) == 3
    assert {item["statement_id"] for item in raw} == {event["statement_id"]}
    children = [item for item in raw if item["is_embedded"]]
    assert len(children) == 2
    assert all(item["timestamp_value"] == "" for item in children)
    assert all(item["original_conversation_unknown"] is True for item in children)
    assert all(item["original_peer_id"] == "" for item in children)
    assert all(item["peer_id"] == "444111222@chatroom" for item in children)
    assert all(item["provenance"]["source_time"] == "0" for item in children)
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def test_ambiguous_original_event_across_native_threads_keeps_references_separate(
    native_activity_store,
):
    fixture = native_activity_store
    primary = _bridge_original(fixture)
    with sqlite3.connect(fixture.history) as connection:
        other = _original_conversation(
            connection, primary, "second-original-room@chatroom"
        )
        row = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (primary,)
            ).fetchone()
        )
        row.update(id="e" * 64, conversation_id=other)
        connection.execute(
            "INSERT INTO messages("
            + ",".join(row)
            + ") VALUES("
            + ",".join("?" for _ in row)
            + ")",
            tuple(row.values()),
        )
    page = analysis.query_contact_analysis(
        fixture.store, fixture.target_id, query=QUOTED
    )
    assert page["total_count"] == page["observation_count"] == 4
    assert all(item["observation_count"] == 1 for item in page["items"])


@pytest.mark.parametrize(
    "identifier", ["0", "09223372036854775806", "9.223372036854775806e18"]
)
def test_noncanonical_native_original_id_does_not_link_events(
    native_activity_store, identifier
):
    fixture = native_activity_store
    _bridge_original(fixture)
    _replace_quote_field(fixture, "source_ids", {"svrid": identifier, "localid": "37"})
    page = analysis.query_contact_analysis(
        fixture.store, fixture.target_id, query=QUOTED
    )
    assert page["total_count"] == page["observation_count"] == 3


@pytest.mark.parametrize(
    "source_ids,expected",
    [
        ({"localid": "9223372036854775806"}, 3),
        ({"msgid": "9223372036854775806"}, 3),
        ({"svrid": "9223372036854775806", "srcMsgSvrId": "7777777777777777777"}, 3),
        ({"svrid": "9223372036854775806", "srcMsgSvrId": "9223372036854775806"}, 1),
    ],
)
def test_original_event_bridge_requires_one_consistent_server_id_namespace(
    native_activity_store,
    source_ids,
    expected,
):
    fixture = native_activity_store
    _bridge_original(fixture)
    _replace_quote_field(fixture, "source_ids", source_ids)
    page = analysis.query_contact_analysis(
        fixture.store, fixture.target_id, query=QUOTED
    )
    assert page["total_count"] == expected
    assert page["observation_count"] == 3


@pytest.mark.parametrize("body", [QUOTED.upper(), QUOTED + " "])
def test_native_original_event_body_must_match_without_normalization(
    native_activity_store, body
):
    fixture = native_activity_store
    _bridge_original(fixture, body=body)
    page = analysis.query_contact_analysis(fixture.store, fixture.target_id)
    assert page["total_count"] == page["observation_count"] == 5


@pytest.mark.parametrize("present", [False, True])
def test_unknown_native_quoted_type_does_not_link_to_known_original_event(
    native_activity_store, present
):
    fixture = native_activity_store
    _bridge_original(fixture)
    _replace_quote_field(fixture, "original_message_type", None, remove=not present)
    page = analysis.query_contact_analysis(
        fixture.store, fixture.target_id, query=QUOTED
    )
    assert page["total_count"] == page["observation_count"] == 3


@pytest.mark.parametrize(
    "peer,expected",
    [("original-room@chatroom", 1), ("contradictory-original@chatroom", 3)],
)
def test_explicit_native_original_chat_id_must_agree_with_event_bridge(
    native_activity_store, peer, expected
):
    fixture = native_activity_store
    _bridge_original(fixture)
    _replace_quote_field(fixture, "source_chat_id", peer)
    page = analysis.query_contact_analysis(
        fixture.store, fixture.target_id, query=QUOTED
    )
    assert page["total_count"] == expected
    assert page["observation_count"] == 3
