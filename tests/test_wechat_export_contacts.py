"""Export aliases, paired native evidence, and atomic publication regressions.

Code version: v1.0.1-codex.0
"""

from contextlib import closing
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3

import pytest

from app.core import wechat_export_contacts as exports
from app.core.wechat_contacts import (
    MESSAGE_BINDING_AUTHORITY,
    WechatContactRepository,
    canonical_contact_id,
    contact_observation,
    contact_registry_path,
    initialize_contact_registry,
    source_contact_id,
)


SOURCE = "new-export"
SNAPSHOT = "snapshot-a"
NATIVE = "wxid_native"
ALIAS = "public.alias"


def paired(export_id=ALIAS, native=NATIVE, **updates):
    return {
        "native_username": native,
        "evidence": {
            "sender_export_id": export_id,
            "sender_native_id": native,
            "avatar_path": f"Portrait/{native}.jpg",
            "source_locator": {"html_member": "chat.html", "frame_index": 7},
        },
        **updates,
    }


def row(export_id=ALIAS, label="Export label", **updates):
    return {
        "row_number": 1,
        "export_id": export_id,
        "label": label,
        "avatar_url": "https://example.invalid/avatar",
        "tag_raw": "",
        "csv_sha256": "a" * 64,
        **updates,
    }


def proof(**updates):
    return {
        "source_id": SOURCE,
        "message_id": "message-a",
        "text_sha256": "b" * 64,
        "timestamp_value": "2026-10-31T10:00:00+08:00",
        "source_record_id": "frame:7",
        "native_username": NATIVE,
        "evidence": {"html_member": "chat.html", "frame_index": 7},
        **updates,
    }


def publish(root, **updates):
    return exports.publish_export_contacts(
        root,
        **{
            "source_id": SOURCE,
            "snapshot_id": SNAPSHOT,
            "rows": [row()],
            "sender_identities": {ALIAS: paired()},
            "message_proofs": [],
            **updates,
        },
    )


def dump(path):
    with closing(sqlite3.connect(path)) as connection:
        return {
            table: connection.execute("SELECT * FROM " + table + " ORDER BY 1").fetchall()
            for (table,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        }


@pytest.fixture
def registry(tmp_path, monkeypatch):
    target = contact_registry_path(tmp_path)
    target.parent.mkdir(parents=True)
    identifier = canonical_contact_id(ALIAS, authority="native_contact_username")
    with closing(sqlite3.connect(target)) as connection, connection:
        initialize_contact_registry(connection)
        connection.execute(
            "INSERT INTO contacts VALUES(?,?,?,?,?,?)",
            (identifier, "wechat", "", ALIAS, "wechat_identity", "verified"),
        )
        connection.execute(
            "INSERT INTO contact_identifiers VALUES(?,?,?,?,?)",
            (identifier, "wechat", ALIAS, "", "native_contact_username"),
        )
        observation = contact_observation(
            identifier,
            source_id="old-native-source",
            owner_account_id="wxid_old_owner",
            kind="remark",
            value="Protected old remark",
            evidence={"table": "SyntheticNativeContact"},
        )
        connection.execute(
            "INSERT INTO contact_observations VALUES(?,?,?,?,?,?,?,?,?)",
            tuple(observation.values()),
        )
    # Synthetic tests do not depend on the host filesystem supporting APFS.
    monkeypatch.setattr(exports, "_clone_database", shutil.copyfile)
    return tmp_path


def test_csv_is_headerless_and_preserves_first_record_and_raw_fields(tmp_path):
    path = tmp_path / "contacts.csv"
    payload = (
        '\ufeff"first.alias","A, B","https://example.invalid/p","raw"\r\n'
        '"wxid_second","Second","",""\r\n'
    ).encode()
    path.write_bytes(payload)
    result = exports.parse_contacts_csv(path)
    assert result["sha256"] == hashlib.sha256(payload).hexdigest()
    assert [item["export_id"] for item in result["rows"]] == ["first.alias", "wxid_second"]
    assert result["rows"][0] == row(
        export_id="first.alias", label="A, B", tag_raw="raw",
        avatar_url="https://example.invalid/p", csv_sha256=result["sha256"],
    )
    assert result["rows"][1]["row_number"] == 2
    assert path.read_bytes() == payload


@pytest.mark.parametrize(
    "payload",
    [b'a,b,c\n', b'a,b,c,d,e\n', b'a,"unterminated,c,d\n', b'a,b,\x00,d\n'],
)
def test_csv_rejects_malformed_structure(tmp_path, payload):
    path = tmp_path / "contacts.csv"
    path.write_bytes(payload)
    with pytest.raises(ValueError):
        exports.parse_contacts_csv(path)


def test_native_string_match_does_not_promote_alias_or_change_old_records(registry):
    path = contact_registry_path(registry)
    before = dump(path)
    report = publish(registry, sender_identities={})
    after = dump(path)
    for table, previous in before.items():
        assert set(previous) <= set(after[table])
    identifier = source_contact_id(SOURCE, ALIAS, source_snapshot=SNAPSHOT)
    assert report["csv_known_native_string_matches"] == 1
    scoped = next(item for item in after["contacts"] if item[0] == identifier)
    assert scoped[1:3] == ("source_sender_id", SOURCE)
    assert scoped[-1] == "source_scoped"
    binding = next(item for item in after["sender_bindings"] if item[0] == SOURCE)
    assert binding[4:6] == (identifier, "source_scoped_identifier")
    assert len(after["contact_observations"]) == len(before["contact_observations"]) + 1


def test_paired_avatar_maps_mutable_alias_to_different_native_identity(registry):
    report = publish(registry, owner_account_id="wxid_owner")
    after = dump(contact_registry_path(registry))
    identifier = canonical_contact_id(NATIVE, authority="corroborated_export_username")
    binding = next(item for item in after["sender_bindings"] if item[0] == SOURCE)
    assert binding[4:6] == (identifier, "corroborated_export_username")
    observation = next(item for item in after["contact_observations"] if item[2] == SOURCE)
    assert observation[1] == identifier
    assert observation[4] == "wxid_owner"
    assert observation[6:8] == ("source_sender_label", "Export label")
    evidence = json.loads(observation[8])
    assert evidence["csv_sha256"] == "a" * 64
    assert evidence["row_number"] == 1
    assert evidence["column_number"] == 2
    assert evidence["raw"]["export_id"] == ALIAS
    assert report["changed"] is True


def test_name_versions_and_source_owners_remain_separate(registry):
    publish(registry, owner_account_id="wxid_owner_a")
    publish(
        registry, snapshot_id="snapshot-b", owner_account_id="wxid_owner_b",
        rows=[row(label="New label", csv_sha256="c" * 64)],
    )
    observations = [
        item for item in dump(contact_registry_path(registry))["contact_observations"]
        if item[2] == SOURCE
    ]
    assert {(item[3], item[4], item[7]) for item in observations} == {
        (SNAPSHOT, "wxid_owner_a", "Export label"),
        ("snapshot-b", "wxid_owner_b", "New label"),
    }


def test_repeated_snapshot_is_idempotent_and_conflicting_snapshot_fails(registry):
    publish(registry)
    path = contact_registry_path(registry)
    before = path.read_bytes()
    identity = exports._database_identity(path)
    repeated = publish(registry)
    assert repeated["changed"] is False
    assert repeated["inserted"] == {}
    assert exports._database_identity(path) == identity
    with pytest.raises(ValueError, match="immutable"):
        publish(registry, rows=[row(label="Changed under the same snapshot")])
    assert path.read_bytes() == before


def test_message_proof_resolves_senderless_message_only_with_exact_digest(registry):
    publish(registry, message_proofs=[proof()])
    path = contact_registry_path(registry)
    stored = dump(path)["message_contact_bindings"]
    assert len(stored) == 1
    assert stored[0][6] == MESSAGE_BINDING_AUTHORITY
    message = {
        "id": "message-a", "source_id": SOURCE, "conversation_id": "conversation-a",
        "source_snapshot_id": SNAPSHOT, "sender_id": "", "sender_label": "Label",
        "text_sha256": "b" * 64, "timestamp_value": proof()["timestamp_value"],
        "source_record_id": "frame:7", "conversation_kind": "direct",
    }
    repository = WechatContactRepository(registry)
    assert repository.resolve_messages([message])[message["id"]]["identity_status"] == "verified"
    altered = {**message, "text_sha256": "d" * 64}
    assert repository.resolve_messages([altered])[message["id"]]["identity_status"] == "unresolved"
    other_source = {**message, "source_id": "other-source"}
    resolved = repository.resolve_messages([other_source])[message["id"]]
    assert resolved["identity_status"] == "unresolved"


@pytest.mark.parametrize(
    "updates",
    [{"source_id": "old-source"}, {"text_sha256": ""}, {"timestamp_value": ""},
     {"source_record_id": ""}, {"native_username": "wxid_wrong"}, {"evidence": {}}],
)
def test_invalid_proof_fails_atomically(registry, updates):
    path = contact_registry_path(registry)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        publish(registry, message_proofs=[proof(**updates)])
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".export-contacts-*"))


@pytest.mark.parametrize(
    "bad_avatar",
    ["Portrait/wxid_wrong.jpg", "https://x/wxid_native.jpg", "../wxid_native.jpg"],
)
def test_unpaired_avatar_or_missing_openim_realm_never_creates_identity(registry, bad_avatar):
    identity = paired()
    identity["evidence"]["avatar_path"] = bad_avatar
    before = contact_registry_path(registry).read_bytes()
    with pytest.raises(ValueError, match="same-frame"):
        publish(registry, sender_identities={ALIAS: identity})
    with pytest.raises(ValueError, match="application realm"):
        publish(registry, sender_identities={ALIAS: paired(native="peer@openim")})
    assert contact_registry_path(registry).read_bytes() == before


def test_clone_failure_and_live_sidecars_preserve_registry(registry, monkeypatch):
    path = contact_registry_path(registry)
    before = path.read_bytes()

    def unsupported(*args):
        raise OSError(errno.ENOTSUP, "APFS clone unavailable")

    monkeypatch.setattr(exports, "_clone_database", unsupported)
    with pytest.raises(OSError, match="APFS"):
        publish(registry)
    assert path.read_bytes() == before
    sidecar = Path(str(path) + "-wal")
    sidecar.write_bytes(b"user-owned-sidecar")
    with pytest.raises(ValueError, match="sidecars"):
        publish(registry)
    assert sidecar.read_bytes() == b"user-owned-sidecar"
    assert path.read_bytes() == before


def test_live_registry_change_during_clone_is_detected(registry, monkeypatch):
    path = contact_registry_path(registry)

    def concurrent_change(source, destination):
        shutil.copyfile(source, destination)
        replacement = source.with_name("other-writer.sqlite3")
        shutil.copyfile(source, replacement)
        with closing(sqlite3.connect(replacement)) as connection, connection:
            connection.execute(
                "INSERT INTO contact_registry_info VALUES('other-writer','preserve')"
            )
        os.replace(replacement, source)

    monkeypatch.setattr(exports, "_clone_database", concurrent_change)
    with pytest.raises(ValueError, match="during cloning"):
        publish(registry)
    after = dump(path)
    assert ("other-writer", "preserve") in after["contact_registry_info"]
    assert not after["sender_bindings"]
    assert not list(path.parent.glob(".export-contacts-*"))


def test_unsupported_host_cannot_silently_fall_back_to_whole_file_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(exports.sys, "platform", "unsupported")
    with pytest.raises(OSError, match="APFS"):
        exports._clone_database(tmp_path / "source", tmp_path / "target")


def test_fresh_registry_can_be_initialized_without_cloning(tmp_path):
    report = publish(tmp_path)
    assert report["changed"] is True
    assert len(dump(contact_registry_path(tmp_path))["contacts"]) == 1


def test_publish_replace_failure_preserves_original_file(registry, monkeypatch):
    path = contact_registry_path(registry)
    before = path.read_bytes()

    def fail_replace(*args):
        raise OSError("Synthetic atomic publication failure")

    monkeypatch.setattr(exports.os, "replace", fail_replace)
    with pytest.raises(OSError, match="atomic publication"):
        publish(registry)
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".export-contacts-*"))


def test_openim_native_realms_stay_separate(registry):
    identities = {
        "alias.a": paired("alias.a", "peer@openim", realm="app-a"),
        "alias.b": paired("alias.b", "peer@openim", realm="app-b"),
    }
    publish(
        registry,
        rows=[row("alias.a"), row("alias.b", row_number=2)],
        sender_identities=identities,
    )
    contacts = [
        item for item in dump(contact_registry_path(registry))["contacts"]
        if item[1] == "openim"
    ]
    assert len(contacts) == 2
    assert {item[2] for item in contacts} == {"app-a", "app-b"}
    assert len({item[0] for item in contacts}) == 2


def test_existing_native_namespace_authority_and_kind_are_preserved(registry):
    path = contact_registry_path(registry)
    identifier = canonical_contact_id(ALIAS, authority="native_contact_username")
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE contacts SET kind='historical_native_kind' WHERE id=?", (identifier,),
        )
    before = dump(path)
    publish(
        registry,
        sender_identities={ALIAS: paired(native=ALIAS)},
        message_proofs=[proof(native_username=ALIAS)],
    )
    after = dump(path)
    assert before["contacts"] == after["contacts"]
    old_identifier = (identifier, "wechat", ALIAS, "", "native_contact_username")
    corroborated = (identifier, "wechat", ALIAS, "", "corroborated_export_username")
    assert old_identifier in before["contact_identifiers"]
    assert old_identifier in after["contact_identifiers"]
    assert corroborated in after["contact_identifiers"]
    assert not any(item[1] == "native_username" for item in after["contact_identifiers"])
    assert after["message_contact_bindings"][0][5] == identifier
