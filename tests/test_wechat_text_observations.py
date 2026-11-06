"""Mac text overlays bind exact sources while leaving primary messages unchanged.

Code version: v1.0.2-codex.0
"""

from contextlib import closing
from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

import pytest

from app.core.wechat_archive import _identifier, _time_fields, wechat_archive_path
from app.core.wechat_resource_index import (
    _SCHEMA as RESOURCE_SCHEMA,
    resource_index_path,
)
from app.core.wechat_text_observations import (
    WechatTextObservations,
    canonical_provenance_sha256,
)
from tests.test_wechat_reconcile import merge, record, source


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def make_observation_fixture(store: Path) -> dict:
    """Build one unavailable native quote and independently pinned presentation sources."""
    store = Path(store)
    metadata = source()
    owner = metadata["source_account_id"]
    group, peer = "123456789@chatroom", "wxid_private_peer"
    original_text = "The quotation is exaggerated, but fine."
    new_reply = "The quotation is exaggerated，but fine."
    excerpt = "A Mac-copied first line\nThe same app message continues."
    canonical_text = "Original canonical text stays <b>literal</b>."
    parent = record(
        metadata,
        "unavailable-quoted-reply",
        peer=peer,
        text=original_text,
        message_type=49,
        text_extraction_kind="quoted_reply",
        server_id="111",
        text_components=[{"kind": "reply_text", "text": original_text}],
        quoted_reference={
            "original_message_type": 49,
            "relationship": "forwarded_conversation",
            "authority": "user_confirmation",
            "content_unavailable": True,
        },
        flags=["index_recovered", "punctuation_normalized", "missing_dictionary_5"],
    )
    canonical = record(
        metadata,
        "canonical-original",
        peer=group,
        text=canonical_text,
        sender_id=owner,
        sender_display_name="Original speaker",
        direction="sent",
        server_id="222",
    )
    merge(store, [metadata], [parent, canonical])
    history_path = wechat_archive_path(store)
    message_id = _identifier(metadata["source_id"], parent["extraction_record_id"])
    target_id = _identifier(metadata["source_id"], canonical["extraction_record_id"])
    with closing(sqlite3.connect(history_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        parent_row = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (message_id,)
            ).fetchone()
        )
        target_row = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (target_id,)
            ).fetchone()
        )
        conversation = tuple(
            connection.execute(
                "SELECT * FROM conversations WHERE id=?",
                (target_row["conversation_id"],),
            ).fetchone()
        )
    image_id, anchor_id = (
        _identifier("synthetic-image-resource"),
        _identifier("synthetic-app-anchor"),
    )
    resource_path = resource_index_path(store)
    source_provenance = {}
    with closing(sqlite3.connect(resource_path)) as connection, connection:
        connection.executescript(RESOURCE_SCHEMA)
        connection.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", conversation
        )
        for index, (identifier, message_type) in enumerate(
            ((image_id, 3), (anchor_id, 49))
        ):
            epoch = canonical["unix_timestamp"] - 50 + index * 20
            sort, timestamp, label, basis = _time_fields(
                {"sent_at_utc_iso": datetime.fromtimestamp(epoch, UTC).isoformat()}
            )
            provenance = {
                "record_id": "image" if message_type == 3 else "unread-app",
                "message_type": message_type,
                "text_extraction_kind": "resource_placeholder",
                "unix_timestamp": epoch,
                "locator": {
                    "database": "message_4.sqlite",
                    "table": "Chat_" + hashlib.md5(group.encode()).hexdigest(),
                    "local_id": 40 + index,
                },
                "resource_reference": {
                    "kind": "image" if message_type == 3 else "app",
                    "account_id": owner,
                    "peer_id": group,
                },
            }
            source_provenance[identifier] = provenance
            row = {
                **target_row,
                "id": identifier,
                "text": "[Image]" if message_type == 3 else "[App message]",
                "time_sort": sort,
                "timestamp_value": timestamp,
                "timestamp_label": label,
                "time_basis": basis,
                "provenance": json.dumps(provenance),
            }
            connection.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(row.values()),
            )
        connection.execute(
            "INSERT INTO archive_info VALUES('history_fingerprint','stale-index-is-metadata-only')"
        )
    artifact_path = history_path.parent / "repair_audits/synthetic-mac/artifact.json"
    artifact_path.parent.mkdir(parents=True)
    prefix = new_reply + "\n\nQuoted conversation\n" + excerpt + "\n"
    parts = [
        {"kind": "literal", "text": prefix},
        {
            "kind": "archive_message_body",
            "message_id": target_id,
            "utf8_sha256": sha(canonical_text),
        },
    ]
    write_json(
        artifact_path,
        {
            "version": "v1.0.0",
            "corroborated_reply_candidate_id": message_id,
            "native_forward_item_source_ids_observed": False,
            "round_trip_reconstruction_verified": True,
            "user_input_representation": parts,
            "user_input_utf8_sha256": sha(prefix + canonical_text),
        },
    )
    observation_id = _identifier("synthetic-mac-text-observation")
    observation = {
        "id": observation_id,
        "authority": "user_provided_mac_client_copied_text",
        "message_id": message_id,
        "account_id": owner,
        "peer_id": peer,
        "text_sha256": sha(original_text),
        "provenance_sha256": canonical_provenance_sha256(
            json.loads(parent_row["provenance"])
        ),
        "source": {
            "path": str(artifact_path.relative_to(history_path.parent)),
            "sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
        },
        "reply": {
            "part": 0,
            "start": 0,
            "end": len(new_reply),
            "sha256": sha(new_reply),
        },
        "items": [
            {
                "kind": "resource",
                "message_id": image_id,
                "peer_id": group,
                "provenance_sha256": canonical_provenance_sha256(
                    source_provenance[image_id]
                ),
            },
            {
                "kind": "source_excerpt",
                "anchor_resource_id": anchor_id,
                "peer_id": group,
                "provenance_sha256": canonical_provenance_sha256(
                    source_provenance[anchor_id]
                ),
                "excerpt": {
                    "part": 0,
                    "start": prefix.index(excerpt),
                    "end": prefix.index(excerpt) + len(excerpt),
                    "sha256": sha(excerpt),
                },
            },
            {
                "kind": "message",
                "message_id": target_id,
                "peer_id": group,
                "text_sha256": sha(canonical_text),
                "provenance_sha256": canonical_provenance_sha256(
                    json.loads(target_row["provenance"])
                ),
            },
        ],
    }
    registry_path = history_path.with_name("text_observations.json")
    write_json(registry_path, {"schema_version": 1, "observations": [observation]})
    return {
        "store": store,
        "registry_path": registry_path,
        "artifact_path": artifact_path,
        "message_id": message_id,
        "account_id": owner,
        "peer_id": peer,
        "session_id": parent_row["conversation_id"],
        "history_path": history_path,
        "resource_path": resource_path,
        "resource_id": image_id,
        "anchor_resource_id": anchor_id,
        "new_reply": new_reply,
        "excerpt_text": excerpt,
        "canonical_text": canonical_text,
        "original_text": original_text,
        "observation_id": observation_id,
        "provenance": json.loads(parent_row["provenance"]),
        "observation": observation,
        "canonical_id": target_id,
    }


def project(fixture, **changes):
    values = {
        key: fixture[key]
        for key in ("message_id", "account_id", "peer_id", "provenance")
    }
    values["text"] = fixture["original_text"]
    values.update(changes)
    return WechatTextObservations(fixture["store"]).project(**values)


def test_observation_projects_existing_sources_without_changing_primary_or_reading_media(
    tmp_path,
):
    fixture = make_observation_fixture(tmp_path)
    before = fixture["history_path"].read_bytes(), fixture["resource_path"].read_bytes()
    with patch(
        "app.core.wechat_mac_resources.WechatMacResourceResolver.resolve",
        side_effect=AssertionError("No media reads"),
    ):
        result = project(fixture)
    assert result["display_text"] == fixture["new_reply"]
    assert result["quoted_reply"]["text"] == ""
    image, excerpt, canonical = result["quoted_reply"]["forwarded_record"]["items"]
    assert image.messaging["resource"]["can_reveal"] is False
    assert image.content_text == ""
    assert excerpt.content_text == fixture["excerpt_text"]
    assert (
        excerpt.messaging["provenance"]["authority"]
        == "user_provided_mac_client_copied_text"
    )
    assert "canonical_message_id" not in excerpt.messaging
    assert "source_ids" not in excerpt.messaging["provenance"]
    assert canonical.content_text == fixture["canonical_text"]
    assert canonical.content_html == ""
    assert canonical.messaging["canonical_message_id"] == fixture["canonical_id"]
    assert all(
        item.last_seen_at and item.messaging["timestamp_label"]
        for item in (image, excerpt, canonical)
    )
    assert len({item.stable_id for item in (image, excerpt, canonical)}) == 3
    assert result["text_observation"] == {
        "id": fixture["observation_id"],
        "label": "Mac client text supplied by user; native reference IDs unavailable.",
    }
    assert before == (
        fixture["history_path"].read_bytes(),
        fixture["resource_path"].read_bytes(),
    )
    registry = fixture["registry_path"].read_text(encoding="utf-8")
    assert (
        fixture["new_reply"] not in registry
        and fixture["excerpt_text"] not in registry
        and fixture["canonical_text"] not in registry
    )


@pytest.mark.parametrize(
    "field", ["message_id", "account_id", "peer_id", "text", "provenance"]
)
def test_caller_scope_and_primary_evidence_must_match(tmp_path, field):
    fixture = make_observation_fixture(tmp_path)
    changed = {
        "message_id": "f" * 64,
        "account_id": "wxid_other",
        "peer_id": "other-peer",
        "text": "Changed body",
        "provenance": {**fixture["provenance"], "server_id": "999"},
    }
    assert project(fixture, **{field: changed[field]}) is None


@pytest.mark.parametrize(
    "failure",
    [
        "account",
        "peer",
        "text_hash",
        "provenance_hash",
        "target_hash",
        "target_provenance",
        "target_peer",
        "resource_peer",
        "resource_provenance",
        "anchor_hash",
        "slice_hash",
        "slice_bounds",
        "slice_bool",
        "slice_nonliteral",
        "authority",
        "artifact_absolute",
        "artifact_parent",
        "duplicate",
        "unknown_field",
    ],
)
def test_invalid_registry_bindings_fail_closed(tmp_path, failure):
    fixture = make_observation_fixture(tmp_path)
    observation = deepcopy(fixture["observation"])
    if failure == "account":
        observation["account_id"] = "wxid_other"
    elif failure == "peer":
        observation["peer_id"] = "different-peer"
    elif failure == "text_hash":
        observation["text_sha256"] = "f" * 64
    elif failure == "provenance_hash":
        observation["provenance_sha256"] = "f" * 64
    elif failure == "target_hash":
        observation["items"][2]["text_sha256"] = "f" * 64
    elif failure == "target_provenance":
        observation["items"][2]["provenance_sha256"] = "f" * 64
    elif failure == "target_peer":
        observation["items"][2]["peer_id"] = "different@chatroom"
    elif failure == "resource_peer":
        observation["items"][0]["peer_id"] = "different@chatroom"
    elif failure == "resource_provenance":
        observation["items"][0]["provenance_sha256"] = "f" * 64
    elif failure == "anchor_hash":
        observation["items"][1]["provenance_sha256"] = "f" * 64
    elif failure == "slice_hash":
        observation["reply"]["sha256"] = "f" * 64
    elif failure == "slice_bounds":
        observation["reply"]["end"] = 1_000_000
    elif failure == "slice_bool":
        observation["reply"]["part"] = False
    elif failure == "slice_nonliteral":
        observation["reply"]["part"] = 1
    elif failure == "authority":
        observation["authority"] = "native_xml_decoded"
    elif failure == "artifact_absolute":
        observation["source"]["path"] = str(fixture["artifact_path"])
    elif failure == "artifact_parent":
        observation["source"]["path"] = "repair_audits/../outside.json"
    elif failure == "unknown_field":
        observation["html"] = "<script>not allowed</script>"
    values = [observation, observation] if failure == "duplicate" else [observation]
    write_json(fixture["registry_path"], {"schema_version": 1, "observations": values})
    assert project(fixture) is None


def test_artifact_full_hash_symlink_and_size_guards(tmp_path):
    fixture = make_observation_fixture(tmp_path)
    original = fixture["artifact_path"].read_bytes()
    fixture["artifact_path"].write_bytes(original + b"\n")
    assert project(fixture) is None
    fixture["artifact_path"].write_bytes(original)
    duplicate = fixture["artifact_path"].with_name("original.json")
    fixture["artifact_path"].rename(duplicate)
    fixture["artifact_path"].symlink_to(duplicate)
    assert project(fixture) is None
    fixture["artifact_path"].unlink()
    fixture["artifact_path"].write_bytes(b"x" * (1024 * 1024 + 1))
    assert project(fixture) is None


def test_registry_override_is_managed_and_does_not_create_files(tmp_path):
    fixture = make_observation_fixture(tmp_path / "store")
    candidate = fixture["artifact_path"].parent / "candidate-registry.json"
    candidate.write_bytes(fixture["registry_path"].read_bytes())
    viewer = WechatTextObservations(fixture["store"], registry_path=candidate)
    assert viewer.project(
        fixture["message_id"],
        fixture["account_id"],
        fixture["peer_id"],
        fixture["original_text"],
        fixture["provenance"],
    )
    outside = tmp_path / "outside.json"
    outside.write_bytes(fixture["registry_path"].read_bytes())
    assert (
        WechatTextObservations(fixture["store"], registry_path=outside).observations
        == {}
    )
    absent = tmp_path / "absent-store"
    assert WechatTextObservations(absent).observations == {}
    assert not absent.exists()


def test_changed_native_quote_is_not_overlaid_even_if_registry_hashes_are_refreshed(
    tmp_path,
):
    fixture = make_observation_fixture(tmp_path)
    provenance = deepcopy(fixture["provenance"])
    provenance["quoted_reference"]["content_unavailable"] = False
    with closing(sqlite3.connect(fixture["history_path"])) as connection, connection:
        connection.execute(
            "UPDATE messages SET provenance=? WHERE id=?",
            (json.dumps(provenance), fixture["message_id"]),
        )
    fixture["observation"]["provenance_sha256"] = canonical_provenance_sha256(
        provenance
    )
    write_json(
        fixture["registry_path"],
        {"schema_version": 1, "observations": [fixture["observation"]]},
    )
    assert project(fixture, provenance=provenance) is None


def test_codepoint_slices_keep_non_bmp_characters_and_whitespace(tmp_path):
    fixture = make_observation_fixture(tmp_path)
    artifact = json.loads(fixture["artifact_path"].read_text(encoding="utf-8"))
    extra = "😀  exact e\u0301\n"
    artifact["user_input_representation"][0]["text"] += extra
    old_part = artifact["user_input_representation"][0]["text"]
    artifact.pop("user_input_utf8_sha256")
    write_json(fixture["artifact_path"], artifact)
    observation = fixture["observation"]
    observation["source"]["sha256"] = hashlib.sha256(
        fixture["artifact_path"].read_bytes()
    ).hexdigest()
    observation["reply"] = {
        "part": 0,
        "start": len(old_part) - len(extra),
        "end": len(old_part),
        "sha256": sha(extra),
    }
    write_json(
        fixture["registry_path"], {"schema_version": 1, "observations": [observation]}
    )
    assert project(fixture)["display_text"] == extra


def test_database_and_registry_changes_during_projection_invalidate_result(tmp_path):
    fixture = make_observation_fixture(tmp_path)
    viewer = WechatTextObservations(fixture["store"])
    fixture["registry_path"].write_text(fixture["registry_path"].read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert (
        viewer.project(
            fixture["message_id"],
            fixture["account_id"],
            fixture["peer_id"],
            fixture["original_text"],
            fixture["provenance"],
        )
        is None
    )


def test_artifact_parent_binding_is_enforced_even_when_its_hash_is_current(tmp_path):
    fixture = make_observation_fixture(tmp_path)
    artifact = json.loads(fixture["artifact_path"].read_text(encoding="utf-8"))
    artifact["corroborated_reply_candidate_id"] = "f" * 64
    write_json(fixture["artifact_path"], artifact)
    fixture["observation"]["source"]["sha256"] = hashlib.sha256(
        fixture["artifact_path"].read_bytes()
    ).hexdigest()
    write_json(
        fixture["registry_path"],
        {"schema_version": 1, "observations": [fixture["observation"]]},
    )
    assert project(fixture) is None


def test_file_identity_checks_work_without_the_platform_nofollow_flag(
    tmp_path, monkeypatch
):
    fixture = make_observation_fixture(tmp_path)
    monkeypatch.delattr(
        "app.core.wechat_text_observations.os.O_NOFOLLOW", raising=False
    )
    assert project(fixture)["display_text"] == fixture["new_reply"]


def test_same_named_text_in_another_account_cannot_supply_a_canonical_body(tmp_path):
    fixture = make_observation_fixture(tmp_path)
    other = source("wxid_other_account")
    target = record(
        other,
        "other-original",
        peer="123456789@chatroom",
        text=fixture["canonical_text"],
    )
    merge(tmp_path, [other], [target])
    target_id = _identifier(other["source_id"], target["extraction_record_id"])
    with closing(sqlite3.connect(fixture["history_path"])) as connection, connection:
        provenance = json.loads(
            connection.execute(
                "SELECT provenance FROM messages WHERE id=?", (target_id,)
            ).fetchone()[0]
        )
    artifact = json.loads(fixture["artifact_path"].read_text(encoding="utf-8"))
    artifact["user_input_representation"][1]["message_id"] = target_id
    write_json(fixture["artifact_path"], artifact)
    observation = fixture["observation"]
    observation["source"]["sha256"] = hashlib.sha256(
        fixture["artifact_path"].read_bytes()
    ).hexdigest()
    observation["items"][2]["message_id"] = target_id
    observation["items"][2]["provenance_sha256"] = canonical_provenance_sha256(
        provenance
    )
    write_json(
        fixture["registry_path"], {"schema_version": 1, "observations": [observation]}
    )
    assert project(fixture) is None


def test_concurrent_primary_write_discards_the_overlay(tmp_path):
    from app.core.wechat_contacts import WechatContactRepository

    fixture = make_observation_fixture(tmp_path)
    resolve = WechatContactRepository.resolve_messages

    def changed(repository, requests):
        result = resolve(repository, requests)
        with closing(sqlite3.connect(fixture["history_path"])) as connection, connection:
            connection.execute(
                "UPDATE messages SET sender_label='Concurrent edit' WHERE id=?",
                (fixture["message_id"],),
            )
        return result

    with patch.object(WechatContactRepository, "resolve_messages", changed):
        assert project(fixture) is None
