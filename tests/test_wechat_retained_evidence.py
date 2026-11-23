"""Retired export references remain secondary during native restoration.

Code version: v1.0.4-codex.0
"""

from datetime import datetime
import hashlib
import json
import shutil
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from app.core import wechat_native_restore as restore
from app.core import wechat_export_merge
from app.core.wechat_archive import _identifier, wechat_archive_path
from app.core.wechat_derived_retirement import (
    _ALIAS_SCHEMA,
    _counters,
    logical_message_sha256,
)
from app.core.wechat_export_merge import merge_wechat_export
from app.core.wechat_reconcile import _json
from app.core.wechat_retained_evidence import _retained_native_observation
from tests.test_wechat_native_restore import merge, record, run, source


@pytest.fixture(autouse=True)
def fixture_clone_backend(monkeypatch):
    if sys.platform != "darwin":
        monkeypatch.setattr(restore, "_clonefile", shutil.copyfile)
        monkeypatch.setattr(wechat_export_merge, "_clonefile", shutil.copyfile)


def retired_export(store, *, verified_export=True):
    metadata = {**source(), "source_label": "iPhone archive"}
    native = record(metadata, "existing", peer="123456789@chatroom")
    merge(store, [metadata], [native])
    path = wechat_archive_path(store)
    native_id = _identifier(metadata["source_id"], "existing")
    with sqlite3.connect(path) as connection:
        native_clock = connection.execute(
            "SELECT timestamp_value FROM messages WHERE id=?", (native_id,)
        ).fetchone()[0]
    digest = "a" * 64
    exported = {
        "source_id": "html-" + digest[:24],
        "source_sha256": digest,
        "source_file": "synthetic.html",
        "source_label": "HTML export",
        "owner_verified": verified_export,
    }
    if verified_export:
        exported.update(
            source_account_id="wxid_independent_export_owner",
            owner_evidence={"method": "independent_synthetic_receipt"},
        )
    cross = {
        "kind": "same_text_native_sender_peer_and_wall_clock",
        "account_relation": "shared_group",
        "existing_source_id": source("wxid_different_native")["source_id"],
        "existing_account_key": "account:wxid_different_native",
        "global_message_identity_asserted": False,
        "time_zone_inferred": False,
        # A nested export identifier never becomes a native server key.
        "server_id": "777",
    }
    merge_wechat_export(
        store,
        source=exported,
        records=iter([{
            "extraction_record_id": "export-one",
            "conversation_key": native["conversation_id"],
            "peer_id": native["conversation_id"],
            "title": native["conversation_id"],
            "kind": "group",
            "sender_id": "legacy-export-author",
            "sender_label": "Synthetic export author",
            "direction": "unknown",
            "text": native["text"],
            "sent_at_raw": native_clock,
            "provenance": {"locator": {"export_msgtype": "1"}},
            "crossrefs": [cross],
        }]),
        snapshot_id="synthetic-export-run",
        expected_records=1,
    )
    native_id = _identifier(metadata["source_id"], "existing")
    old_id = _identifier(exported["source_id"], "export-one")
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        old = dict(connection.execute("SELECT * FROM messages WHERE id=?", (old_id,)).fetchone())
        primary = connection.execute("SELECT * FROM messages WHERE id=?", (native_id,)).fetchone()
        original = json.loads(old["provenance"])
        partition = dict(connection.execute("SELECT * FROM partitions WHERE id=?", (exported["source_id"],)).fetchone())
        conversation = dict(connection.execute("SELECT * FROM conversations WHERE id=?", (old["conversation_id"],)).fetchone())
        body_sha256 = hashlib.sha256(old["text"].encode()).hexdigest()
        native_proof = json.loads(primary["provenance"])
        alias_metadata = {
            "original_sha256": logical_message_sha256(old),
            "body_sha256": body_sha256,
            "row": {k: v for k, v in old.items() if k not in {"text", "search_text", "provenance"}},
            "provenance_sha256": hashlib.sha256(old["provenance"].encode()).hexdigest(),
            "provenance": {key: original[key] for key in ("record_id", "source_snapshot", "source_time", "locator")},
            "source": partition,
            "conversation": {k: v for k, v in conversation.items() if k != "latest_text"},
        }
        proof = {
            "method": "exact_group_author_body_clock_order_v1",
            "anchor_count": 3,
            "derived_source_id": exported["source_id"],
            "native_source_id": metadata["source_id"],
            "derived_conversation_id": old["conversation_id"],
            "native_conversation_id": primary["conversation_id"],
            "native_source_snapshot_id": native_proof["source_snapshot"],
            "native_server_id": native_proof["server_id"],
            "room_id": native["conversation_id"],
            "wall_clock": datetime.fromisoformat(primary["timestamp_value"]).replace(tzinfo=None).isoformat(timespec="seconds"),
            "body_sha256": body_sha256,
            "derived_owner_unconfirmed": not verified_export,
        }
        connection.execute(_ALIAS_SCHEMA)
        connection.execute(
            "INSERT INTO derived_message_aliases VALUES(?,?,?,?,?)",
            (old_id, native_id, exported["source_id"], _json(alias_metadata), _json(proof)),
        )
        connection.execute("UPDATE message_evidence SET message_id=? WHERE message_id=?", (native_id, old_id))
        connection.execute("DELETE FROM messages WHERE id=?", (old_id,))
        _counters(connection, [old["conversation_id"]])
        manifest = json.loads(connection.execute("SELECT value FROM archive_info WHERE key='manifest'").fetchone()[0])
        manifest["messages"] -= 1
        for item in manifest["sources"]:
            if item["source_id"] == exported["source_id"]:
                item["messages"] -= 1
        connection.execute("UPDATE archive_info SET value=? WHERE key='manifest'", (_json(manifest),))
        evidence = dict(connection.execute("SELECT * FROM message_evidence WHERE source_id=?", (exported["source_id"],)).fetchone())
    return SimpleNamespace(
        store=store, path=path, metadata=metadata, native=native,
        native_id=native_id, old_id=old_id, evidence=evidence,
    )


@pytest.mark.parametrize("shared,verified_export", [(False, True), (True, True), (False, False)])
def test_proved_export_reference_preserves_source_and_does_not_seed_native_sid(
    tmp_path, shared, verified_export
):
    fixture = retired_export(tmp_path, verified_export=verified_export)
    if shared:
        from app.core.wechat_shared_storage import _install
        with sqlite3.connect(fixture.path) as connection:
            _install(connection)
    incoming = record(
        fixture.metadata, "incoming", peer=fixture.native["conversation_id"],
        server_id="777", text="Independent new native body",
    )
    result = run(tmp_path, [fixture.metadata], [incoming])
    assert result["inserted_messages"] == 1 and result["conflict_variants"] == 0
    assert result["secondary_export_cross_references"] == 1
    assert result["old_messages_unchanged"] and result["old_evidence_unchanged"]
    with sqlite3.connect(fixture.path) as connection:
        connection.row_factory = sqlite3.Row
        retained = dict(connection.execute("SELECT * FROM message_evidence WHERE id=?", (fixture.evidence["id"],)).fetchone())
        assert retained == fixture.evidence
        assert _retained_native_observation(connection, retained, fixture.metadata["source_id"]) is None
    replay = run(tmp_path, [fixture.metadata], [incoming])
    assert replay["inserted_messages"] == replay["new_evidence"] == 0
    assert replay["replayed_observations"] == 1


@pytest.mark.parametrize("failure", [
    "foreign-native", "native-envelope", "injected-native-field", "missing-alias",
    "alias-target", "alias-record", "native-source", "native-snapshot",
    "body", "source-snapshot", "export-receipt", "export-label",
    "export-owner", "export-owner-unverified", "export-sha",
    "unproved-anchors", "global-identity", "timezone",
])
def test_unproved_foreign_evidence_refuses_without_publication(tmp_path, failure):
    fixture = retired_export(tmp_path)
    with sqlite3.connect(fixture.path) as connection:
        connection.row_factory = sqlite3.Row
        evidence = dict(connection.execute("SELECT * FROM message_evidence WHERE id=?", (fixture.evidence["id"],)).fetchone())
        observation = json.loads(evidence["observation"])
        alias = connection.execute("SELECT * FROM derived_message_aliases WHERE old_id=?", (fixture.old_id,)).fetchone()
        metadata, proof = json.loads(alias["metadata"]), json.loads(alias["proof"])
        if failure == "foreign-native":
            connection.execute("UPDATE message_evidence SET source_id=?", (source("wxid_foreign_native")["source_id"],))
        elif failure == "native-envelope":
            observation = {"record_id": "other-native", "server_id": "777", "source_snapshot": 1}
        elif failure == "injected-native-field":
            observation["server_id"] = "777"
        elif failure == "missing-alias":
            connection.execute("DELETE FROM derived_message_aliases")
        elif failure == "alias-target":
            connection.execute("UPDATE derived_message_aliases SET native_id=?", ("f" * 64,))
        elif failure == "alias-record":
            metadata["provenance"]["record_id"] = "wrong"
        elif failure == "native-source":
            proof["native_source_id"] = source("wxid_foreign_native")["source_id"]
        elif failure == "native-snapshot":
            proof["native_source_snapshot_id"] = 9999
        elif failure == "body":
            observation["content_sha256"] = "0" * 64
        elif failure == "source-snapshot":
            connection.execute("UPDATE source_snapshots SET source_id='foreign-export' WHERE id=?", (metadata["provenance"]["source_snapshot"],))
        elif failure == "export-receipt":
            connection.execute("UPDATE export_imports SET metadata='{}'")
        elif failure in {"export-owner", "export-owner-unverified"}:
            sid = metadata["provenance"]["source_snapshot"]
            original_source = json.loads(connection.execute(
                "SELECT metadata FROM source_snapshots WHERE id=?", (sid,)
            ).fetchone()[0])
            if failure == "export-owner":
                original_source["source_account_id"] = "wxid_wrong_export_owner"
            else:
                original_source["owner_verified"] = False
            encoded_source = _json(original_source)
            connection.execute("UPDATE source_snapshots SET metadata=? WHERE id=?", (encoded_source, sid))
            connection.execute("UPDATE export_imports SET metadata=?", (encoded_source,))
        elif failure == "export-sha":
            connection.execute("UPDATE export_imports SET source_sha256=?", ("b" * 64,))
        elif failure == "export-label":
            connection.execute("UPDATE partitions SET source_label='iPad archive' WHERE id=?", (evidence["source_id"],))
        elif failure == "unproved-anchors":
            proof["anchor_count"] = 2
        elif failure == "global-identity":
            observation["cross_reference"]["global_message_identity_asserted"] = True
        elif failure == "timezone":
            observation["cross_reference"]["time_zone_inferred"] = True
        if failure in {"native-envelope", "injected-native-field", "body", "global-identity", "timezone"}:
            encoded = _json(observation)
            identifier = _identifier(evidence["source_id"], evidence["extraction_record_id"], encoded)
            connection.execute("UPDATE message_evidence SET id=?,observation=?", (identifier, encoded))
        if failure in {"alias-record", "native-source", "native-snapshot", "unproved-anchors"}:
            connection.execute("UPDATE derived_message_aliases SET metadata=?,proof=?", (_json(metadata), _json(proof)))
    before = fixture.path.read_bytes()
    incoming = record(fixture.metadata, "new", peer=fixture.native["conversation_id"], server_id="777")
    with pytest.raises(ValueError):
        run(tmp_path, [fixture.metadata], [incoming])
    assert fixture.path.read_bytes() == before


def test_generic_reconcile_preserves_proved_retired_export_reference(tmp_path):
    fixture = retired_export(tmp_path)
    incoming = record(
        fixture.metadata, "generic-incoming", peer=fixture.native["conversation_id"],
        server_id="777", text="Independent new native body",
    )
    result = merge(tmp_path, [fixture.metadata], [incoming], snapshot="after-retirement")
    assert result["inserted_messages"] == 1 and result["conflict_variants"] == 0
    with sqlite3.connect(fixture.path) as connection:
        connection.row_factory = sqlite3.Row
        retained = dict(connection.execute(
            "SELECT * FROM message_evidence WHERE id=?", (fixture.evidence["id"],)
        ).fetchone())
        assert retained == fixture.evidence
    replay = merge(tmp_path, [fixture.metadata], [incoming], snapshot="after-retirement")
    assert replay["inserted_messages"] == replay["new_evidence"] == 0
    assert replay["replayed_observations"] == 1
