"""Recovered native authors retain source bytes and use existing contact links.

Code version: v1.0.2-codex.0
"""

from copy import deepcopy
import json
import sqlite3

import pytest

from app.core import wechat_contact_embedded as embedded
from app.core import wechat_history_browser as browser
from tests import test_wechat_contact_native_embedded as native_fixtures
from tests.test_wechat_contact_native_embedded import (
    FIRST,
    OTHER,
    OWNER,
    PARENT,
    TARGET,
    _publish_native_forwarded_proof,
    _render_native_forwarded_parent,
    _tree,
)
from tests.test_wechat_contacts import observe
from tests import test_wechat_forwarded_authors as proof_fixtures


native_author_store = proof_fixtures.native_author_store
native_activity_store = native_fixtures.native_activity_store
forwarded_origin_store = proof_fixtures.forwarded_origin_store
native_html_store = proof_fixtures.native_html_store
statement_store = proof_fixtures.statement_store


def _resolution(sender=TARGET):
    return {
        "sender_id": sender,
        "identity_basis": "verified_complete_native_transcript_copy",
        "evidence": {
            "native_message_id": "b" * 64,
            "native_reference_id": "b" * 64 + "_forwarded_1",
            "native_component_path": ["native", 1],
            "target_component_path": [1],
        },
    }


def _missing_author_tree():
    proof = _tree()
    proof["text_components"][0].update(
        source_sender_id="", source_sender_label="Fixture speaker"
    )
    proof["text_components"][1]["source_sender_label"] = "Fixture speaker"
    return proof


def test_recovered_detail_uses_exact_component_path_without_rewriting_source():
    proof = _missing_author_tree()
    before = deepcopy(proof)
    assert embedded.native_detail(
        proof, ["native", 1], parent_id=PARENT, account_id=OWNER
    ) is None
    recovered = embedded.native_detail(
        proof,
        ["native", 1],
        parent_id=PARENT,
        account_id=OWNER,
        author_resolutions={(1,): _resolution()},
    )
    assert recovered["sender_id"] == TARGET
    assert recovered["sender_label"] == "Fixture speaker"
    assert recovered["text"] == FIRST
    assert recovered["direction"] == "received"
    assert recovered["identity_basis"] == "verified_complete_native_transcript_copy"
    assert recovered["author_recovery"] == _resolution()["evidence"]
    assert proof == before


def test_recovery_cannot_replace_a_literal_author_or_use_another_position():
    proof = _missing_author_tree()
    details = list(
        embedded.native_details(
            proof,
            parent_id=PARENT,
            account_id=OWNER,
            author_resolutions={(2,): _resolution()},
        )
    )
    by_path = {tuple(detail["path"]): detail for detail in details}
    assert ("native", 1) not in by_path
    assert by_path[("native", 2)]["sender_id"] == OTHER
    assert "author_recovery" not in by_path[("native", 2)]


def test_browser_reuses_canonical_contact_for_recovered_author_and_keeps_namesake(
    native_activity_store, monkeypatch
):
    fixture = native_activity_store
    _publish_native_forwarded_proof(fixture, _missing_author_tree())
    parent_id = fixture.parents["native-forward"]
    with sqlite3.connect(fixture.registry) as connection:
        observe(connection, fixture.target_id, "nickname", "Canonical target")
        observe(connection, fixture.other_id, "nickname", "Canonical namesake")
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    seen = []

    def verified_projection(self, rows):
        seen.extend(row["id"] for row in rows)
        return {parent_id: {(1,): _resolution()}}

    monkeypatch.setattr(browser.WechatForwardedAuthors, "project", verified_projection)
    parent = _render_native_forwarded_parent(fixture)
    recovered, namesake, *_ = parent.messaging["forwarded_record"]["items"]
    assert parent_id in seen
    assert recovered.messaging["contact"]["id"] == fixture.target_id
    assert recovered.messaging["contact"]["identity_status"] == "verified"
    assert recovered.messaging["viewer_display_name"] == "Canonical target"
    assert recovered.messaging["contact"]["resolution_reason"] == (
        "verified_complete_native_transcript_copy"
    )
    assert recovered.author_label == "Fixture speaker"
    assert recovered.messaging["sender_id"] == TARGET
    assert recovered.messaging["direction"] == "received"
    proof = recovered.messaging["provenance"]
    assert proof["embedded_forwarded_component"]["source_sender_id"] == ""
    assert proof["native_forwarded_author_recovery"] == _resolution()["evidence"]
    assert namesake.messaging["contact"]["id"] == fixture.other_id
    assert namesake.messaging["viewer_display_name"] == "Canonical namesake"
    assert "native_forwarded_author_recovery" not in namesake.messaging["provenance"]
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


@pytest.mark.parametrize("recovery", [None, {(2,): _resolution()}])
def test_browser_without_exact_recovery_keeps_nameless_author_unresolved(
    native_activity_store, monkeypatch, recovery
):
    fixture = native_activity_store
    _publish_native_forwarded_proof(fixture, _missing_author_tree())
    parent_id = fixture.parents["native-forward"]
    monkeypatch.setattr(
        browser.WechatForwardedAuthors,
        "project",
        lambda self, rows: {parent_id: recovery} if recovery else {},
    )
    parent = _render_native_forwarded_parent(fixture)
    recovered, namesake, *_ = parent.messaging["forwarded_record"]["items"]
    assert recovered.messaging["contact"]["identity_status"] == "unresolved"
    assert recovered.messaging["sender_id"] == ""
    assert recovered.author_label == "Fixture speaker"
    assert "native_forwarded_author_recovery" not in recovered.messaging["provenance"]
    assert namesake.messaging["contact"]["id"] == fixture.other_id


def test_native_copy_proof_drives_browser_and_removal_restores_unresolved_author(
    native_author_store,
):
    fixture = native_author_store
    with sqlite3.connect(fixture.history) as connection:
        conversation, raw_proof = connection.execute(
            "SELECT conversation_id,provenance FROM messages WHERE id=?",
            (fixture.target_parent,),
        ).fetchone()
        donor = json.loads(
            connection.execute(
                "SELECT provenance FROM messages WHERE id=?", (fixture.native_parent,)
            ).fetchone()[0]
        )
    position = next(
        number
        for number, part in enumerate(donor["text_components"])
        if part["source_sender_id"] == proof_fixtures.TARGET
    )
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()

    def render():
        page = browser.query_wechat_history(fixture.store, session=conversation)
        return next(
            item for item in page.items if item.stable_id == fixture.target_parent
        ).messaging["forwarded_record"]["items"][position]

    child = render()
    assert child.messaging["contact"]["id"] == proof_fixtures.CID
    assert child.messaging["contact"]["identity_status"] == "verified"
    assert child.messaging["sender_id"] == proof_fixtures.TARGET
    assert child.messaging["provenance"]["embedded_forwarded_component"][
        "source_sender_id"
    ] == ""
    assert json.loads(raw_proof)["text_components"][position]["source_sender_id"] == ""
    assert child.messaging["provenance"]["native_forwarded_author_recovery"][
        "native_message_id"
    ] == fixture.native_parent
    fixture.author_registry.unlink()
    unresolved = render()
    assert unresolved.messaging["contact"]["identity_status"] == "unresolved"
    assert unresolved.messaging["sender_id"] == ""
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())
