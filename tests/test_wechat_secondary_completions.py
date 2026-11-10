"""Derived completion catalogs preserve originals and serve one consistent timeline.

Code version: v1.0.7-codex.2
"""

from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

import pytest

from app.core import wechat_secondary_completions as completions
from app.core.owner_only_files import owner_only_problem
from app.core.wechat_archive import (
    WechatArchiveRepository,
    _file_identity,
    _identifier,
    wechat_history_partition_fingerprint,
)
from app.core.wechat_resource_index import _SCHEMA as RESOURCE_SCHEMA
from tests import test_wechat_resource_sender_evidence as sender
from tests import test_wechat_macos_recovery_bank as native
from tests import test_wechat_secondary_system_notices as systems


mac_archive = sender.mac_archive
bank_archive = sender.bank_archive
source_binding = sender.source_binding
witness = sender.witness
invitation_scope = systems.invitation_scope


@pytest.fixture
def completion_fixture(source_binding, witness, tmp_path):
    row, _conversation, packet = deepcopy(witness)
    history = source_binding.fixture.history
    root = source_binding.root
    owner = sender.PRIMARY
    hashed = hashlib.md5(owner.encode(), usedforsecurity=False).hexdigest()
    partition = "ios-" + hashed
    session = "wechat:" + _identifier(partition, native.ROOM)
    row.update(conversation_id=session, text="[Image]", search_text="[image]")
    proof = json.loads(row["provenance"])
    proof["message_type"] = 3
    proof["resource_reference"]["kind"] = "image"
    proof["locator"].update(database="message_2.sqlite", row_id=64)
    row["provenance"] = json.dumps(proof)
    packet["messages"][0].update(local_type=3, text="[Image]")
    packet["messages"][0]["resource"].update(kind="image", label="[Image]")
    quoted = native._message(
        10,
        49,
        sender=owner,
        raw="<msg><appmsg><title>Verified reply</title><type>57</type><refermsg>"
        "<type>3</type><svrid>504</svrid><fromusr>" + native.ROOM + "</fromusr>"
        "<chatusr>"
        + native.MEMBER
        + "</chatusr><displayname>Native quoted author</displayname>"
        "<createtime>1700000004</createtime><content>opaque image metadata</content>"
        "</refermsg></appmsg></msg>",
    )
    quoted.update(real_sender_id=9, sender_proof=native._proof(9, owner))
    packet["messages"].append(quoted)
    quote_row = deepcopy(row)
    quote_row.update(
        id=hashlib.sha256(b"original-quoted-app-placeholder").hexdigest(),
        ordinal=65,
        sender_id=owner,
        sender_label="Retained primary owner label",
        direction="sent",
        text="[App message]",
        search_text="[app message]",
        time_sort=quoted["create_time"] + 28800,
        timestamp_value="2023-11-15T06:13:30+08:00",
        timestamp_label="15 Nov 2023 06:13:30",
    )
    quote_proof = deepcopy(proof)
    quote_proof.update(
        record_id=quote_row["id"],
        server_id=quoted["server_id"],
        unix_timestamp=quoted["create_time"],
        message_type=49,
    )
    quote_proof["resource_reference"].update(
        kind="app", unix_timestamp=quoted["create_time"]
    )
    quote_proof["locator"].update(local_id=65, row_id=65)
    quote_row["provenance"] = json.dumps(quote_proof)
    conversation = {
        "id": session,
        "partition_id": partition,
        "account_key": "account:" + owner,
        "peer_id": native.ROOM,
        "title": "Existing primary group",
        "kind": "group",
        "message_count": 2,
        "first_sort": row["time_sort"],
        "last_sort": quote_row["time_sort"],
        "first_time": row["timestamp_value"],
        "last_time": quote_row["timestamp_value"],
        "latest_text": quote_row["text"],
    }
    with sqlite3.connect(history) as connection:
        connection.execute(
            "CREATE TABLE source_snapshots(id INTEGER PRIMARY KEY,snapshot_id TEXT,"
            "source_id TEXT,metadata TEXT)"
        )
        connection.execute(
            "INSERT INTO partitions VALUES(?,?,?,?,?,?,?,?)",
            (
                partition,
                "account:" + owner,
                owner,
                "Primary owner",
                "Verified archive ID",
                "iPhone backup",
                "synthetic",
                "",
            ),
        )
        empty = {**conversation, "message_count": 0}
        connection.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(empty[column] for column in completions.CONVERSATION_COLUMNS),
        )
        connection.execute(
            "INSERT INTO source_snapshots(snapshot_id,source_id,metadata) VALUES(?,?,?)",
            (
                proof["backup_id"],
                partition,
                json.dumps(
                    {
                        "source_account_id": owner,
                        "account_hash": hashed,
                        "account_hash_verified": True,
                        "input_databases": [
                            {
                                "path": "Documents/" + hashed + "/DB/message_2.sqlite",
                                "sha256": proof["source_sha256"],
                            }
                        ],
                    }
                ),
            ),
        )
    catalog = history.with_name("resource_references.sqlite3")
    with sqlite3.connect(catalog) as connection, sqlite3.connect(history) as primary:
        connection.executescript(RESOURCE_SCHEMA)
        connection.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(conversation[column] for column in completions.CONVERSATION_COLUMNS),
        )
        for original in [row, quote_row]:
            connection.execute(
                "INSERT INTO messages VALUES("
                + ",".join("?" for _ in completions.COLUMNS)
                + ")",
                tuple(original[column] for column in completions.COLUMNS),
            )
        connection.executemany(
            "INSERT INTO archive_info VALUES(?,?)",
            [
                ("schema_version", "1"),
                ("proof_version", "v1.0.0-codex.0"),
                ("history_file_identity", json.dumps(_file_identity(history))),
                (
                    "history_partitions_sha256",
                    wechat_history_partition_fingerprint(primary),
                ),
            ],
        )
    return {
        "root": root,
        "history": history,
        "catalog": catalog,
        "packet": packet,
        "image": row,
        "quote": quote_row,
        "conversation": conversation,
        "candidate": tmp_path / "candidate.sqlite3",
    }


def _stage(fixture):
    return completions.stage_secondary_message_completions(
        fixture["root"], fixture["candidate"], [fixture["packet"]], notice_plans=()
    )


def _publish(fixture, report):
    return completions.publish_secondary_message_completions(
        fixture["root"],
        fixture["candidate"],
        candidate_sha256=report["candidate_sha256"],
    )


def test_stage_is_review_only_and_retains_complete_original_rows(completion_fixture):
    fixture = completion_fixture
    before = {name: Path(fixture[name]).read_bytes() for name in ["history", "catalog"]}
    report = _stage(fixture)
    assert report["counts"] == {"native_sender": 1, "native_quote": 1}
    assert not completions.completion_path(fixture["root"]).exists()
    assert {name: Path(fixture[name]).read_bytes() for name in before} == before
    with completions._readonly(fixture["candidate"]) as derived:
        originals = {
            row["id"]: json.loads(row["original_row"])
            for row in derived.execute("SELECT id,original_row FROM entries")
        }
        assert originals[fixture["image"]["id"]] == fixture["image"]
        assert originals[fixture["quote"]["id"]] == fixture["quote"]
        quote = derived.execute(
            "SELECT * FROM messages WHERE id=?", (fixture["quote"]["id"],)
        ).fetchone()
        assert quote["text"] == "Verified reply"
        assert quote["timestamp_value"] == fixture["quote"]["timestamp_value"]
        proof = json.loads(quote["provenance"])
        assert "resource_reference" not in proof
        assert proof["quoted_reference"]["original_message_type"] == 3
        assert (
            proof["secondary_resource_completion"]["original_resource_reference"][
                "kind"
            ]
            == "app"
        )


def test_published_overlay_drives_sql_paging_search_and_complete_export(
    completion_fixture,
):
    fixture = completion_fixture
    report = _stage(fixture)
    publication = _publish(fixture, report)
    assert publication["published"]
    repository = WechatArchiveRepository(fixture["root"])
    first = repository.query(
        account="account:" + sender.PRIMARY,
        session=fixture["conversation"]["id"],
        page_size=1,
        persist_event_cache=False,
    )
    second = repository.query(
        account="account:" + sender.PRIMARY,
        session=fixture["conversation"]["id"],
        page=2,
        page_size=1,
        persist_event_cache=False,
    )
    assert first["resource_index_status"] == "ready"
    assert first["count"] == 2 and first["pages"] == 2
    assert first["messages"][0]["sender_id"] == native.MEMBER
    assert second["messages"][0]["text"] == "Verified reply"
    assert second["messages"][0]["id"] == fixture["quote"]["id"]
    searched = repository.query(
        account="account:" + sender.PRIMARY,
        session=fixture["conversation"]["id"],
        query="Verified reply",
        persist_event_cache=False,
    )
    assert searched["count"] == 1
    assert searched["messages"][0]["id"] == fixture["quote"]["id"]
    with completions._readonly(fixture["history"]) as connection:
        _, table, status = repository._timeline_tables(
            connection,
            _file_identity(fixture["history"]),
            conversation_id=fixture["conversation"]["id"],
            strict=True,
        )
        assert status == "ready"
        exported = list(
            connection.execute(
                "SELECT id,text,sender_id,timestamp_value FROM "
                + table
                + " WHERE conversation_id=? ORDER BY time_sort,ordinal",
                (fixture["conversation"]["id"],),
            )
        )
        assert len(exported) == 2 and exported[1]["text"] == "Verified reply"
        assert exported[1]["timestamp_value"] == fixture["quote"]["timestamp_value"]


@pytest.mark.parametrize("source", ["history", "catalog"])
def test_source_change_rejects_publication_without_mutating_the_target(
    completion_fixture, source
):
    fixture = completion_fixture
    report = _stage(fixture)
    with sqlite3.connect(fixture[source]) as connection:
        connection.execute(
            "INSERT INTO archive_info VALUES('unrelated-change','retained')"
        )
    with pytest.raises(ValueError, match="compare-and-swap"):
        _publish(fixture, report)
    assert not completions.completion_path(fixture["root"]).exists()
    assert fixture["candidate"].exists()


def test_modified_projected_row_is_rejected_even_with_a_fresh_file_hash(
    completion_fixture,
):
    fixture = completion_fixture
    _stage(fixture)
    with sqlite3.connect(fixture["candidate"]) as connection:
        connection.execute("UPDATE messages SET sender_id='wxid_fabricated_author'")
    with pytest.raises(ValueError, match="projected message"):
        completions.publish_secondary_message_completions(
            fixture["root"],
            fixture["candidate"],
            candidate_sha256=completions._file_sha(fixture["candidate"]),
        )
    assert not completions.completion_path(fixture["root"]).exists()


def test_existing_completion_generation_is_a_compare_and_swap_boundary(
    completion_fixture,
):
    fixture = completion_fixture
    report = _stage(fixture)
    fixture["history"].with_name(completions.FILENAME).write_bytes(
        b"other-writer-generation"
    )
    with pytest.raises(ValueError, match="compare-and-swap"):
        _publish(fixture, report)
    assert (
        fixture["history"].with_name(completions.FILENAME).read_bytes()
        == b"other-writer-generation"
    )


def test_original_catalog_drift_disables_the_overlay_without_hiding_the_original(
    completion_fixture,
):
    fixture = completion_fixture
    _publish(fixture, _stage(fixture))
    with sqlite3.connect(fixture["catalog"]) as connection:
        connection.execute(
            "UPDATE messages SET sender_label='Retained concurrent change' WHERE id=?",
            (fixture["image"]["id"],),
        )
    with completions._readonly(fixture["history"]) as connection:
        conversation, messages, status = (
            completions.attach_secondary_message_completions(
                connection, fixture["history"], "main.conversations", "main.messages"
            )
        )
        assert (conversation, messages, status) == (
            "main.conversations",
            "main.messages",
            "stale",
        )


def test_completion_metadata_changes_invalidate_the_browser_source_fingerprint(
    completion_fixture,
):
    from app.core.wechat_conversation_events import _inputs

    fixture = completion_fixture
    contacts = fixture["history"].with_name("contacts.sqlite3")
    with sqlite3.connect(contacts):
        pass
    repository = WechatArchiveRepository(fixture["root"])
    before = _inputs(repository)
    _publish(fixture, _stage(fixture))
    after = _inputs(repository)
    path = str(completions.completion_path(fixture["root"]).absolute())
    assert path not in before and after[path] is not None
    assert before != after


def test_packet_generator_is_consumed_incrementally_and_replay_is_idempotent(
    completion_fixture, monkeypatch
):
    fixture = completion_fixture
    projected = []
    original = completions._complete_resource

    def observe(*args):
        projected.append(args[0]["id"])
        return original(*args)

    monkeypatch.setattr(completions, "_complete_resource", observe)

    def packets():
        yield fixture["packet"]
        assert projected, "The first packet must be projected before reading the next."
        yield deepcopy(fixture["packet"])

    report = completions.stage_secondary_message_completions(
        fixture["root"], fixture["candidate"], packets(), notice_plans=()
    )
    assert report["packets"] == 2
    assert report["counts"] == {"native_quote": 1, "native_sender": 1}
    assert report["rejected"] == {}


def test_multiple_physical_witnesses_are_reported_without_selecting_the_first(
    completion_fixture,
):
    fixture = completion_fixture
    second = deepcopy(fixture["packet"])
    for message in second["messages"]:
        message["local_id"] += 100
    second["receipt_id"] += "-another-native-row"
    report = completions.stage_secondary_message_completions(
        fixture["root"],
        fixture["candidate"],
        iter([fixture["packet"], second]),
        notice_plans=(),
    )
    assert report["counts"] == {}
    assert report["rejected"]["ambiguous_original_events"] == 2
    with completions._readonly(fixture["candidate"]) as derived:
        assert derived.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert derived.execute("SELECT COUNT(*) FROM ambiguities").fetchone()[0] == 2


def test_resource_and_system_strategies_share_one_sql_catalog_and_publication(
    completion_fixture, invitation_scope
):
    fixture = completion_fixture
    source, conversation, packet, anchors = invitation_scope
    with sqlite3.connect(fixture["history"]) as history:
        history.row_factory = sqlite3.Row
        history.execute(
            "INSERT INTO partitions VALUES(?,?,?,?,?,?,?,?)",
            tuple(source.execute("SELECT * FROM partitions").fetchone()),
        )
        history.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(conversation[key] for key in completions.CONVERSATION_COLUMNS),
        )
        snapshot = dict(source.execute("SELECT * FROM source_snapshots").fetchone())
        cursor = history.execute(
            "INSERT INTO source_snapshots(snapshot_id,source_id,metadata) VALUES(?,?,?)",
            (snapshot["snapshot_id"], snapshot["source_id"], snapshot["metadata"]),
        )
        for anchor in anchors:
            anchor = dict(anchor)
            proof = json.loads(anchor["provenance"])
            proof["source_snapshot"] = cursor.lastrowid
            anchor["provenance"] = json.dumps(proof)
            history.execute(
                "INSERT INTO messages VALUES("
                + ",".join("?" for _ in completions.COLUMNS)
                + ")",
                tuple(anchor[key] for key in completions.COLUMNS),
            )
    with (
        sqlite3.connect(fixture["catalog"]) as catalog,
        sqlite3.connect(fixture["history"]) as history,
    ):
        catalog.execute(
            "UPDATE archive_info SET value=? WHERE key='history_file_identity'",
            (json.dumps(_file_identity(fixture["history"])),),
        )
        catalog.execute(
            "UPDATE archive_info SET value=? WHERE key='history_partitions_sha256'",
            (wechat_history_partition_fingerprint(history),),
        )
    with (
        completions._readonly(fixture["history"]) as history,
        completions._readonly(fixture["catalog"]) as catalog,
    ):
        plans = systems.notices.build_notice_plans(history, catalog, packet)
    assert len(plans) == 1
    base_report = _stage(fixture)
    base_report = completions.compact_secondary_message_completions(
        fixture["root"],
        fixture["candidate"],
        fixture["candidate"].with_name("encoded-resource-base.sqlite3"),
        candidate_sha256=base_report["candidate_sha256"],
    )
    base_path = Path(base_report["candidate"])
    base_bytes = base_path.read_bytes()
    fixture["candidate"] = base_path.with_name("extended-with-notices.sqlite3")
    report = completions.stage_secondary_message_completions(
        fixture["root"],
        fixture["candidate"],
        (),
        notice_plans=plans,
        base_candidate_path=base_path,
        base_candidate_sha256=base_report["candidate_sha256"],
    )
    assert base_path.read_bytes() == base_bytes
    assert report["packets"] == 0
    assert report["base_candidate"]["sha256"] == base_report["candidate_sha256"]
    assert report["counts"] == {
        "native_sender": 1,
        "native_quote": 1,
        "system_notice": 1,
    }
    encoded = completions.compact_secondary_message_completions(
        fixture["root"],
        fixture["candidate"],
        fixture["candidate"].with_name("encoded-notice-candidate.sqlite3"),
        candidate_sha256=report["candidate_sha256"],
    )
    fixture["candidate"] = Path(encoded["candidate"])
    report = encoded
    bad_plan = fixture["candidate"].with_name("corrupt-encoded-plan.sqlite3")
    completions._clonefile(fixture["candidate"], bad_plan)
    with sqlite3.connect(bad_plan) as output:
        rowid, frame = output.execute(
            "SELECT rowid,plan FROM entries WHERE operation='append'"
        ).fetchone()
        assert isinstance(frame, bytes)
        corrupted = bytearray(frame)
        corrupted[-1] ^= 1
        output.execute(
            "UPDATE entries SET plan=? WHERE rowid=?", (bytes(corrupted), rowid)
        )
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        completions.publish_secondary_message_completions(
            fixture["root"], bad_plan, candidate_sha256=completions._file_sha(bad_plan)
        )
    legacy = fixture["candidate"].with_name("legacy-complete-with-notice.sqlite3")
    completions._clonefile(fixture["candidate"], legacy)
    with sqlite3.connect(legacy) as output:
        output.execute("DROP TABLE append_event_heads")
        output.execute("DROP TABLE append_event_audit")
    indexed = completions.stage_secondary_message_completions(
        fixture["root"],
        legacy.with_name("indexed-complete-with-notice.sqlite3"),
        (),
        notice_plans=(),
        base_candidate_path=legacy,
        base_candidate_sha256=completions._file_sha(legacy),
    )
    assert indexed["counts"] == report["retained_counts"]
    with completions._readonly(indexed["candidate"]) as output:
        assert (
            output.execute("SELECT COUNT(*) FROM append_event_heads").fetchone()[0] == 1
        )
    _publish(fixture, report)
    repository = WechatArchiveRepository(fixture["root"])
    result = repository.query(
        session=conversation["id"], page_size=1, page=2, persist_event_cache=False
    )
    assert result["count"] == 3 and result["pages"] == 3
    notice = result["messages"][0]
    assert notice["id"] == plans[0]["message"]["id"]
    assert notice["sender_id"] == ""
    assert notice["text"].startswith("Source account " + native.OWNER + ": ")
    assert json.loads(notice["provenance"])["text_extraction_kind"] == "system_text"
    with completions._readonly(fixture["history"]) as connection:
        assert (
            repository._timeline_conversation_id(connection, notice["id"])
            == conversation["id"]
        )
    searched = repository.query(
        session=conversation["id"], query="Inviting member", persist_event_cache=False
    )
    assert searched["count"] == 1 and searched["messages"][0]["id"] == notice["id"]
    with completions._readonly(fixture["history"]) as connection:
        conversations, messages, status = repository._resource_timeline_tables(
            connection,
            _file_identity(fixture["history"]),
            conversation_id=conversation["id"],
        )
        assert status == "ready"
        connection.execute(
            "CREATE TEMP TABLE retained_notice_messages AS SELECT * FROM " + messages
        )
        retained = dict(plans[0]["message"])
        retained["id"] = hashlib.sha256(b"preexisting-native-bank-notice").hexdigest()
        connection.execute(
            "INSERT INTO retained_notice_messages VALUES("
            + ",".join("?" for _ in completions.COLUMNS)
            + ")",
            tuple(retained[key] for key in completions.COLUMNS),
        )
        projected_conversations, projected_messages, status = (
            completions.attach_secondary_message_completions(
                connection,
                fixture["history"],
                conversations,
                "temp.retained_notice_messages",
                conversation_id=conversation["id"],
            )
        )
        assert status == "ready"
        actual = list(
            connection.execute(
                "SELECT id FROM " + projected_messages + " WHERE conversation_id=?",
                (conversation["id"],),
            )
        )
        assert len(actual) == 3
        assert retained["id"] in [row[0] for row in actual]
        assert notice["id"] not in [row[0] for row in actual]


def test_newer_upstream_bank_projection_wins_over_a_legacy_resource_completion(
    completion_fixture,
):
    fixture = completion_fixture
    _publish(fixture, _stage(fixture))
    repository = WechatArchiveRepository(fixture["root"])
    with completions._readonly(fixture["history"]) as connection:
        conversations, messages, status = repository._resource_timeline_tables(
            connection,
            _file_identity(fixture["history"]),
            conversation_id=fixture["conversation"]["id"],
        )
        assert status == "ready"
        connection.execute(
            "CREATE TEMP TABLE retained_bank_messages AS SELECT * FROM " + messages
        )
        connection.execute(
            "UPDATE retained_bank_messages SET sender_id=?,sender_label=? WHERE id=?",
            (
                "wxid_independently_verified_newer_author",
                "Newer verified author",
                fixture["image"]["id"],
            ),
        )
        _, projected, status = completions.attach_secondary_message_completions(
            connection,
            fixture["history"],
            conversations,
            "temp.retained_bank_messages",
            conversation_id=fixture["conversation"]["id"],
        )
        assert status == "ready"
        image = connection.execute(
            "SELECT * FROM " + projected + " WHERE id=?", (fixture["image"]["id"],)
        ).fetchone()
        quote = connection.execute(
            "SELECT * FROM " + projected + " WHERE id=?", (fixture["quote"]["id"],)
        ).fetchone()
        assert image["sender_id"] == "wxid_independently_verified_newer_author"
        assert image["sender_label"] == "Newer verified author"
        assert quote["text"] == "Verified reply"
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM " + projected + " WHERE conversation_id=?",
                (fixture["conversation"]["id"],),
            ).fetchone()[0]
            == 2
        )


def test_authenticated_irrelevant_packets_are_counted_without_creating_completions(
    completion_fixture, source_binding
):
    fixture = completion_fixture
    report = completions.stage_secondary_message_completions(
        fixture["root"],
        fixture["candidate"],
        iter([fixture["packet"], source_binding.packet]),
        notice_plans=(),
    )
    assert report["counts"] == {"native_sender": 1, "native_quote": 1}
    assert report["ignored"] == {"authenticated_non_group_packets": 1}
    assert report["packets"] == 2
    with completions._readonly(fixture["candidate"]) as derived:
        assert (
            dict(derived.execute("SELECT key,value FROM archive_info"))["staging_state"]
            == "complete"
        )


def test_invalid_packet_keeps_prior_verified_evidence_but_cannot_be_published(
    completion_fixture,
):
    fixture = completion_fixture
    unverified = {
        "conversation_kind": "direct",
        "peer_id": "wxid_unrelated",
        "messages": [],
    }
    with pytest.raises(ValueError, match="account binding"):
        completions.stage_secondary_message_completions(
            fixture["root"],
            fixture["candidate"],
            iter([fixture["packet"], unverified]),
            notice_plans=(),
        )
    with completions._readonly(fixture["candidate"]) as derived:
        assert derived.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 2
        assert (
            dict(derived.execute("SELECT key,value FROM archive_info"))["staging_state"]
            == "in_progress"
        )
    with pytest.raises(ValueError, match="not completed staging"):
        completions.publish_secondary_message_completions(
            fixture["root"],
            fixture["candidate"],
            candidate_sha256=completions._file_sha(fixture["candidate"]),
        )
    assert not completions.completion_path(fixture["root"]).exists()


def _retain_primary_image(fixture):
    original = dict(fixture["image"])
    original.update(
        sender_id=native.MEMBER, sender_label="Already verified primary author"
    )
    with sqlite3.connect(fixture["history"]) as connection:
        connection.execute(
            "INSERT INTO messages VALUES("
            + ",".join("?" for _ in completions.COLUMNS)
            + ")",
            tuple(original[key] for key in completions.COLUMNS),
        )
        connection.execute(
            "UPDATE conversations SET message_count=1,first_sort=?,last_sort=?,first_time=?,last_time=?,latest_text=? WHERE id=?",
            (
                original["time_sort"],
                original["time_sort"],
                original["timestamp_value"],
                original["timestamp_value"],
                original["text"],
                original["conversation_id"],
            ),
        )
    with sqlite3.connect(fixture["catalog"]) as connection:
        connection.execute(
            "UPDATE archive_info SET value=? WHERE key='history_file_identity'",
            (json.dumps(_file_identity(fixture["history"])),),
        )


def test_private_compaction_prunes_only_upstream_precedence_and_keeps_cross_owner_quote(
    completion_fixture, tmp_path
):
    import gzip

    fixture = completion_fixture
    _retain_primary_image(fixture)
    stage = _stage(fixture)
    source_bytes = fixture["candidate"].read_bytes()
    dry = completions.compact_secondary_message_completions(
        fixture["root"],
        fixture["candidate"],
        candidate_sha256=stage["candidate_sha256"],
        dry_run=True,
    )
    assert dry["pruned_counts"] == {"native_sender": 1}
    assert dry["retained_counts"] == {"native_quote": 1}
    compacted = tmp_path / "compacted.sqlite3"
    report = completions.compact_secondary_message_completions(
        fixture["root"],
        fixture["candidate"],
        compacted,
        candidate_sha256=stage["candidate_sha256"],
    )
    assert report["pruned_reasons"] == {"upstream_precedence": 1}
    assert fixture["candidate"].read_bytes() == source_bytes
    assert not completions.completion_path(fixture["root"]).exists()
    with gzip.open(report["manifest"], "rt") as stream:
        manifest = [json.loads(line) for line in stream]
    assert len(manifest) == 2
    assert manifest[1]["id"] == fixture["image"]["id"]
    with closing(completions._readonly(fixture["catalog"])) as catalog:
        original_row = dict(
            catalog.execute(
                "SELECT * FROM messages WHERE id=?", (fixture["image"]["id"],)
            ).fetchone()
        )
    assert manifest[1]["original_sha256"] == completions._sha(original_row)
    with (
        closing(completions._readonly(compacted)) as output,
        closing(completions._readonly(fixture["candidate"])) as source,
    ):
        assert output.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 1
        assert output.execute("SELECT COUNT(*) FROM witnesses").fetchone()[0] == 1
        assert completions._decoded_message(
            output.execute("SELECT * FROM messages").fetchone()
        ) == dict(
            source.execute(
                "SELECT * FROM messages WHERE id=?", (fixture["quote"]["id"],)
            ).fetchone()
        )
    publication = completions.publish_secondary_message_completions(
        fixture["root"], compacted, candidate_sha256=report["candidate_sha256"]
    )
    assert publication["published"]
    page = WechatArchiveRepository(fixture["root"]).query(
        session=fixture["conversation"]["id"], persist_event_cache=False
    )
    assert page["count"] == 2
    messages = {row["id"]: row for row in page["messages"]}
    assert (
        messages[fixture["image"]["id"]]["sender_label"]
        == "Already verified primary author"
    )
    assert messages[fixture["quote"]["id"]]["text"] == "Verified reply"


def test_compaction_excludes_the_already_published_secondary_generation(
    completion_fixture, tmp_path
):
    fixture = completion_fixture
    _publish(fixture, _stage(fixture))
    fixture["candidate"] = tmp_path / "extended.sqlite3"
    stage = _stage(fixture)
    report = completions.compact_secondary_message_completions(
        fixture["root"],
        fixture["candidate"],
        candidate_sha256=stage["candidate_sha256"],
        dry_run=True,
    )
    assert report["pruned_counts"] == {}
    assert report["retained_counts"] == {"native_sender": 1, "native_quote": 1}


def test_compaction_and_publication_reject_upstream_overlay_drift(
    completion_fixture, tmp_path, monkeypatch
):
    fixture = completion_fixture
    _retain_primary_image(fixture)
    stage = _stage(fixture)
    compacted = tmp_path / "compacted.sqlite3"
    report = completions.compact_secondary_message_completions(
        fixture["root"],
        fixture["candidate"],
        compacted,
        candidate_sha256=stage["candidate_sha256"],
    )
    overlay = fixture["history"].with_name("macos_recovery.json")
    overlay.write_text("{}")
    with pytest.raises(ValueError, match="compare-and-swap"):
        completions.publish_secondary_message_completions(
            fixture["root"], compacted, candidate_sha256=report["candidate_sha256"]
        )
    assert not completions.completion_path(fixture["root"]).exists()


def test_compaction_builds_upstream_once_and_reports_progress(
    completion_fixture, tmp_path, monkeypatch
):
    fixture = completion_fixture
    _retain_primary_image(fixture)
    stage = _stage(fixture)
    calls = []
    original = WechatArchiveRepository._timeline_tables

    def observe(self, *args, **kwargs):
        calls.append(kwargs)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(WechatArchiveRepository, "_timeline_tables", observe)
    progress = []
    report = completions.compact_secondary_message_completions(
        fixture["root"],
        fixture["candidate"],
        tmp_path / "compact-once.sqlite3",
        candidate_sha256=stage["candidate_sha256"],
        progress=progress.append,
    )
    assert len(calls) == 1 and calls[0]["include_secondary"] is False
    phases = [item["phase"] for item in progress]
    assert phases[0] == "checking_source_candidate" and phases[-1] == "complete"
    assert "validating_upstream_timeline" in phases and "vacuuming_candidate" in phases
    assert report["phase_seconds"]["validating_upstream_timeline"] >= 0
    assert report["elapsed_seconds"] >= 0


def test_compaction_rejects_an_input_change_during_cloning(
    completion_fixture, tmp_path, monkeypatch
):
    fixture = completion_fixture
    _retain_primary_image(fixture)
    stage = _stage(fixture)
    original_bytes = fixture["candidate"].read_bytes()
    clone = completions._clonefile

    def concurrent_overlay(source, target):
        clone(source, target)
        fixture["history"].with_name("macos_recovery.json").write_text("{}")

    monkeypatch.setattr(completions, "_clonefile", concurrent_overlay)
    output = tmp_path / "changed-input.sqlite3"
    with pytest.raises(ValueError, match="compare-and-swap"):
        completions.compact_secondary_message_completions(
            fixture["root"],
            fixture["candidate"],
            output,
            candidate_sha256=stage["candidate_sha256"],
        )
    assert fixture["candidate"].read_bytes() == original_bytes
    with completions._readonly(output) as derived:
        assert (
            dict(derived.execute("SELECT key,value FROM archive_info"))["staging_state"]
            == "in_progress"
        )
    assert not completions.completion_path(fixture["root"]).exists()


def test_private_base_rejects_unreviewed_incomplete_or_stale_candidates(
    completion_fixture, tmp_path
):
    fixture = completion_fixture
    _stage(fixture)
    base = fixture["candidate"]
    destination = tmp_path / "extended.sqlite3"
    with pytest.raises(ValueError, match="reviewed private base candidate changed"):
        completions.stage_secondary_message_completions(
            fixture["root"],
            destination,
            (),
            notice_plans=(),
            base_candidate_path=base,
            base_candidate_sha256="0" * 64,
        )
    assert not destination.exists()
    with sqlite3.connect(base) as output:
        output.execute(
            "UPDATE archive_info SET value='in_progress' WHERE key='staging_state'"
        )
    with pytest.raises(ValueError, match="not completed staging"):
        completions.stage_secondary_message_completions(
            fixture["root"],
            destination,
            (),
            notice_plans=(),
            base_candidate_path=base,
            base_candidate_sha256=completions._file_sha(base),
        )
    assert not destination.exists()


def test_legacy_complete_private_base_preserves_rows_without_packet_replay(
    completion_fixture, tmp_path
):
    fixture = completion_fixture
    _stage(fixture)
    base = fixture["candidate"]
    with sqlite3.connect(base) as output:
        output.execute("DELETE FROM archive_info WHERE key='staging_state'")
    reviewed = completions._file_sha(base)
    report = completions.stage_secondary_message_completions(
        fixture["root"],
        tmp_path / "extended-legacy.sqlite3",
        (),
        notice_plans=(),
        base_candidate_path=base,
        base_candidate_sha256=reviewed,
    )
    assert report["counts"] == {"native_sender": 1, "native_quote": 1}
    assert report["packets"] == 0
    with (
        completions._readonly(base) as before,
        completions._readonly(report["candidate"]) as after,
    ):
        assert list(before.execute("SELECT * FROM messages ORDER BY id")) == list(
            after.execute("SELECT * FROM messages ORDER BY id")
        )
    assert completions._file_sha(base) == reviewed


def test_private_base_cannot_ignore_a_concurrently_published_target(
    completion_fixture, tmp_path
):
    fixture = completion_fixture
    stage = _stage(fixture)
    target = completions.completion_path(fixture["root"])
    target.write_bytes(b"preserved-external-generation")
    output = tmp_path / "extended-concurrent.sqlite3"
    with pytest.raises(ValueError, match="publication target changed"):
        completions.stage_secondary_message_completions(
            fixture["root"],
            output,
            (),
            notice_plans=(),
            base_candidate_path=fixture["candidate"],
            base_candidate_sha256=stage["candidate_sha256"],
        )
    assert not output.exists()
    assert target.read_bytes() == b"preserved-external-generation"


def _append_projection_case(witness):
    """Exercise storage ordering over already validated projection-shaped inputs."""
    row, conversation, packet = deepcopy(witness)
    native_event = native._message(20, 10000, raw="System Message: A member joined")
    header = {key: value for key, value in packet.items() if key != "messages"}
    event = {**header, **native_event}
    row.update(
        id=hashlib.sha256(b"foreign-bridge-row").hexdigest(),
        sender_id="",
        sender_label="",
        direction="unknown",
        text="A verified notice",
        provenance="{}",
        quality_flags="[]",
        search_text="a verified notice",
    )
    foreign = (row, conversation, event, "secondary_group_invitation_v1")
    own_row, own_conversation, own_event, _ = deepcopy(foreign)
    own_row["id"] = hashlib.sha256(b"native-own-row").hexdigest()
    own_event.update(
        account_id=conversation["account_key"][8:], session_id=conversation["id"]
    )
    return foreign, (
        own_row,
        own_conversation,
        own_event,
        "native_owner_system_notice_v1",
    )


def _store_projection(output, projection):
    message, conversation, event, strategy = projection
    completions._store_entry(
        output,
        message,
        conversation,
        event,
        strategy=strategy,
        plan={"strategy": strategy, "anchors": [], "event": event},
    )


@pytest.mark.parametrize("native_first", [False, True])
def test_native_own_append_precedence_is_order_independent_and_audited(
    witness, native_first
):
    foreign, own = _append_projection_case(witness)
    with sqlite3.connect(":memory:") as output:
        output.executescript(completions._SCHEMA)
        completions._append_tables(output)
        for projection in [own, foreign] if native_first else [foreign, own]:
            _store_projection(output, projection)
        assert output.execute("SELECT id FROM entries").fetchall() == [(own[0]["id"],)]
        audit = output.execute(
            "SELECT kept_id,dropped_id,dropped_row_sha256,dropped_source_key,dropped_witness_key FROM append_event_audit"
        ).fetchone()
        assert audit[0] == own[0]["id"] and audit[1] == foreign[0]["id"]
        assert all(isinstance(value, str) and len(value) == 64 for value in audit[2:])
        actual = dict(
            zip(
                completions.COLUMNS, output.execute("SELECT * FROM messages").fetchone()
            )
        )
        assert actual == own[0]


def test_agreeing_append_copies_keep_existing_evidence_and_count_the_duplicate(witness):
    foreign, _ = _append_projection_case(witness)
    another = deepcopy(foreign)
    another[0]["id"] = hashlib.sha256(b"another-validated-foreign-copy").hexdigest()
    another[2]["local_id"] += 100
    with sqlite3.connect(":memory:") as output:
        output.executescript(completions._SCHEMA)
        completions._append_tables(output)
        _store_projection(output, foreign)
        _store_projection(output, another)
        assert output.execute("SELECT id FROM entries").fetchall() == [
            (foreign[0]["id"],)
        ]
        assert output.execute(
            "SELECT decision,COUNT(*) FROM append_event_audit GROUP BY decision"
        ).fetchall() == [("same_rank_agreeing_duplicate", 1)]


def test_conflicting_append_witnesses_cannot_resurrect_at_the_same_rank(witness):
    foreign, own = _append_projection_case(witness)
    another = deepcopy(foreign)
    another[0].update(
        id=hashlib.sha256(b"conflicting-foreign-copy").hexdigest(),
        text="Conflicting verified source text",
    )
    another[2]["local_id"] += 100
    with sqlite3.connect(":memory:") as output:
        output.executescript(completions._SCHEMA)
        completions._append_tables(output)
        for projection in [foreign, another, foreign]:
            _store_projection(output, projection)
        assert output.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
        assert (
            output.execute("SELECT state FROM append_event_heads").fetchone()[0]
            == "ambiguous"
        )
        _store_projection(output, own)
        assert output.execute("SELECT id FROM entries").fetchall() == [(own[0]["id"],)]
        assert (
            output.execute(
                "SELECT COUNT(*) FROM append_event_audit WHERE decision='native_owner_resolves_foreign_ambiguity'"
            ).fetchone()[0]
            == 1
        )


def test_append_event_precedence_cannot_cross_a_target_conversation(witness):
    foreign, own = _append_projection_case(witness)
    own[0]["conversation_id"] = own[1]["id"] = own[2]["session_id"] = (
        "wechat:" + hashlib.sha256(b"another-target-conversation").hexdigest()
    )
    with sqlite3.connect(":memory:") as output:
        output.executescript(completions._SCHEMA)
        completions._append_tables(output)
        _store_projection(output, foreign)
        _store_projection(output, own)
        assert output.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 2


def test_conflicting_native_own_rows_remain_quarantined_after_replay(witness):
    _, own = _append_projection_case(witness)
    another = deepcopy(own)
    another[0].update(
        id=hashlib.sha256(b"another-native-own-row").hexdigest(),
        text="A conflicting owner-visible notice",
    )
    another[2]["local_id"] += 1
    with sqlite3.connect(":memory:") as output:
        output.executescript(completions._SCHEMA)
        completions._append_tables(output)
        for projection in [own, another, own]:
            _store_projection(output, projection)
        assert output.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
        assert output.execute(
            "SELECT rank,state FROM append_event_heads"
        ).fetchone() == (2, "ambiguous")


def test_append_deduplication_does_not_cross_base_message_types(witness):
    foreign, own = _append_projection_case(witness)
    own[2]["local_type"] = 10002
    with sqlite3.connect(":memory:") as output:
        output.executescript(completions._SCHEMA)
        completions._append_tables(output)
        _store_projection(output, foreign)
        _store_projection(output, own)
        assert output.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 2


@pytest.mark.parametrize(
    "table,column",
    [
        ("messages", "provenance"),
        ("entries", "original_row"),
        ("entries", "conversation_row"),
        ("sources", "header"),
        ("witnesses", "message"),
    ],
)
def test_encoded_cells_remain_hash_checked_at_publication(
    completion_fixture, tmp_path, table, column
):
    fixture = completion_fixture
    stage = _stage(fixture)
    compacted = tmp_path / "encoded.sqlite3"
    completions.compact_secondary_message_completions(
        fixture["root"],
        fixture["candidate"],
        compacted,
        candidate_sha256=stage["candidate_sha256"],
    )
    with sqlite3.connect(compacted) as output:
        rowid, frame = output.execute(
            f'SELECT rowid,"{column}" FROM {table} WHERE typeof("{column}")=? LIMIT 1',
            ("blob",),
        ).fetchone()
        corrupt = bytearray(frame)
        corrupt[-1] ^= 1
        output.execute(
            "UPDATE " + table + ' SET "' + column + '"=? WHERE rowid=?',
            (bytes(corrupt), rowid),
        )
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        completions.publish_secondary_message_completions(
            fixture["root"],
            compacted,
            candidate_sha256=completions._file_sha(compacted),
        )
    assert not completions.completion_path(fixture["root"]).exists()


def test_encoded_view_paging_search_export_and_extension_preserve_canonical_bytes(
    completion_fixture, tmp_path
):
    from app.core.wechat_completion_json_codec import decode_json_cell

    fixture = completion_fixture
    stage = _stage(fixture)
    compacted = tmp_path / "encoded.sqlite3"
    report = completions.compact_secondary_message_completions(
        fixture["root"],
        fixture["candidate"],
        compacted,
        candidate_sha256=stage["candidate_sha256"],
    )
    assert (
        report["json_cell_transport"]["stored_json_bytes"]
        < report["json_cell_transport"]["uncompressed_json_bytes"]
    )
    with (
        completions._readonly(compacted) as output,
        completions._readonly(fixture["candidate"]) as original,
    ):
        for entry in output.execute("SELECT * FROM messages ORDER BY id"):
            before = original.execute(
                "SELECT * FROM messages WHERE id=?", (entry["id"],)
            ).fetchone()
            assert (
                decode_json_cell(entry["provenance"]).encode()
                == before["provenance"].encode()
            )
            assert completions._sha(
                completions._decoded_message(entry)
            ) == completions._sha(dict(before))
    extension = completions.stage_secondary_message_completions(
        fixture["root"],
        tmp_path / "extension.sqlite3",
        (),
        notice_plans=(),
        base_candidate_path=compacted,
        base_candidate_sha256=report["candidate_sha256"],
    )
    publication = completions.publish_secondary_message_completions(
        fixture["root"],
        extension["candidate"],
        candidate_sha256=extension["candidate_sha256"],
    )
    assert publication["published"]
    repository = WechatArchiveRepository(fixture["root"])
    result = repository.query(
        session=fixture["conversation"]["id"],
        page=2,
        page_size=1,
        persist_event_cache=False,
    )
    assert result["count"] == 2 and result["messages"][0]["text"] == "Verified reply"
    searched = repository.query(
        session=fixture["conversation"]["id"],
        query="Verified reply",
        persist_event_cache=False,
    )
    assert searched["count"] == 1
    assert isinstance(searched["messages"][0]["provenance"], str)
    with completions._readonly(fixture["history"]) as connection:
        _, timeline, status = repository._timeline_tables(
            connection, _file_identity(fixture["history"]), strict=True
        )
        assert status == "ready"
        rows = list(
            connection.execute(
                "SELECT provenance FROM " + timeline + " WHERE conversation_id=?",
                (fixture["conversation"]["id"],),
            )
        )
        assert len(rows) == 2 and all(isinstance(row[0], str) for row in rows)


def test_active_guard_inflates_each_original_frame_once(
    completion_fixture, tmp_path, monkeypatch
):
    from app.core import wechat_completion_json_codec as codec

    fixture = completion_fixture
    stage = _stage(fixture)
    compacted = tmp_path / "encoded.sqlite3"
    completions.compact_secondary_message_completions(
        fixture["root"],
        fixture["candidate"],
        compacted,
        candidate_sha256=stage["candidate_sha256"],
    )
    repository = WechatArchiveRepository(fixture["root"])
    with completions._readonly(fixture["history"]) as connection:
        _, messages, _ = repository._resource_timeline_tables(
            connection, _file_identity(fixture["history"])
        )
        connection.execute(
            "ATTACH DATABASE ? AS encoded",
            (compacted.as_uri() + "?mode=ro&immutable=1",),
        )
        codec.clear_json_codec_cache()
        original = codec.zlib.decompressobj
        calls = []

        def observe(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        monkeypatch.setattr(codec.zlib, "decompressobj", observe)
        active = list(
            connection.execute(
                "SELECT e.id FROM encoded.entries e WHERE "
                + completions._active_predicate(messages)
            )
        )
        assert len(active) == 2 and len(calls) == 2


def test_verified_generation_cache_survives_rename_but_rejects_target_and_pin_drift(
    completion_fixture, tmp_path, monkeypatch
):
    fixture = completion_fixture
    stage = _stage(fixture)
    report = completions.compact_secondary_message_completions(
        fixture["root"],
        fixture["candidate"],
        tmp_path / "encoded-cached.sqlite3",
        candidate_sha256=stage["candidate_sha256"],
    )
    calls = []
    validate = completions._validate_entries

    def observe(*args, **kwargs):
        calls.append(1)
        return validate(*args, **kwargs)

    monkeypatch.setattr(completions, "_validate_entries", observe)
    publication = completions.publish_secondary_message_completions(
        fixture["root"],
        report["candidate"],
        candidate_sha256=report["candidate_sha256"],
    )
    assert publication["validation_cache_reused"] and calls == []
    repository = WechatArchiveRepository(fixture["root"])
    for scope in [None, fixture["conversation"]["id"]]:
        with completions._readonly(fixture["history"]) as connection:
            _, _, status = repository._timeline_tables(
                connection,
                _file_identity(fixture["history"]),
                conversation_id=scope,
                strict=True,
            )
            assert status == "ready"
    assert calls == []
    target = completions.completion_path(fixture["root"])
    with sqlite3.connect(target) as output:
        output.execute(
            "INSERT INTO archive_info VALUES('test-generation-change','retained')"
        )
    with completions._readonly(fixture["history"]) as connection:
        _, _, status = completions.attach_secondary_message_completions(
            connection, fixture["history"], "main.conversations", "main.messages"
        )
        assert status == "ready"
    assert len(calls) == 1
    with sqlite3.connect(fixture["catalog"]) as output:
        output.execute(
            "INSERT INTO archive_info VALUES('test-source-change','retained')"
        )
    with completions._readonly(fixture["history"]) as connection:
        assert (
            completions.attach_secondary_message_completions(
                connection, fixture["history"], "main.conversations", "main.messages"
            )[2]
            == "stale"
        )


def test_an_external_post_rename_replacement_cannot_seed_verified_cache(
    completion_fixture, tmp_path, monkeypatch
):
    fixture = completion_fixture
    stage = _stage(fixture)
    report = completions.compact_secondary_message_completions(
        fixture["root"],
        fixture["candidate"],
        tmp_path / "encoded-race.sqlite3",
        candidate_sha256=stage["candidate_sha256"],
    )
    replace = completions.os.replace
    target = completions.completion_path(fixture["root"])

    def external_write(source, destination):
        replace(source, destination)
        Path(destination).write_bytes(b"unverified external generation")

    monkeypatch.setattr(completions.os, "replace", external_write)
    with pytest.raises(ValueError, match="verified candidate identity"):
        completions.publish_secondary_message_completions(
            fixture["root"],
            report["candidate"],
            candidate_sha256=report["candidate_sha256"],
        )
    key = completions._validation_cache_key(
        target, completions._identity(target), completions._pins(fixture["history"])
    )
    assert key not in completions._CACHE


def _legacy_source_receipt(fixture, *, retain_declared_path=False):
    proof = json.loads(fixture["image"]["provenance"])
    owner = sender.PRIMARY
    hashed = hashlib.md5(owner.encode(), usedforsecurity=False).hexdigest()
    relative = "Documents/" + hashed + "/DB/message_2.sqlite"
    member = {
        "bytes": 512,
        "member_path": "Preserved archive/" + relative,
        "sha256": proof["source_sha256"],
    }
    with sqlite3.connect(fixture["history"]) as history:
        history.row_factory = sqlite3.Row
        snapshot = dict(
            history.execute(
                "SELECT * FROM source_snapshots WHERE source_id=?", ("ios-" + hashed,)
            ).fetchone()
        )
        metadata = json.loads(snapshot["metadata"])
        metadata.update(
            source_id=snapshot["source_id"],
            backup_id=snapshot["snapshot_id"],
            container_kind="legacy_ios",
            archive_sha256="a" * 64,
            identity_sha256="b" * 64,
        )
        if not retain_declared_path:
            metadata["input_databases"] = [member]
        raw = json.dumps(metadata)
        history.execute(
            "UPDATE source_snapshots SET metadata=? WHERE id=?", (raw, snapshot["id"])
        )
    with sqlite3.connect(fixture["catalog"]) as catalog:
        catalog.execute(
            "CREATE TABLE IF NOT EXISTS native_resource_sources(source_snapshot_id INTEGER,database_relative_path TEXT,database_sha256 TEXT,account_id TEXT,source_id TEXT,backup_id TEXT,metadata_sha256 TEXT,file_id TEXT,receipt_basis TEXT,proof TEXT)"
        )
        catalog.execute(
            "INSERT INTO native_resource_sources VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                snapshot["id"],
                relative,
                proof["source_sha256"],
                owner,
                snapshot["source_id"],
                snapshot["snapshot_id"],
                hashlib.sha256(raw.encode()).hexdigest(),
                hashlib.sha1(
                    ("AppDomain-com.tencent.xin-" + relative).encode()
                ).hexdigest(),
                "preservation_manifest",
                json.dumps(member),
            ),
        )
        catalog.execute(
            "UPDATE archive_info SET value=? WHERE key='history_file_identity'",
            (json.dumps(_file_identity(fixture["history"])),),
        )


def test_legacy_snapshot_receipt_recovers_new_rows_and_revalidates_at_publication(
    completion_fixture,
):
    fixture = completion_fixture
    _legacy_source_receipt(fixture)
    report = _stage(fixture)
    assert report["counts"] == {"native_sender": 1, "native_quote": 1}
    assert report["snapshot_adapter"]["rejected"] == {}
    with closing(completions._readonly(fixture["candidate"])) as candidate:
        for row in candidate.execute("SELECT provenance FROM messages"):
            receipt = json.loads(row[0])["secondary_resource_completion"][
                "snapshot_receipt_adapter"
            ]
            assert receipt["receipt_shape"] == "legacy_archive_member_path"
            assert len(receipt["catalog_source_receipt_sha256"]) == 64
    assert _publish(fixture, report)["published"]


def test_existing_successful_snapshot_rows_remain_byte_identical(completion_fixture):
    fixture = completion_fixture
    _legacy_source_receipt(fixture, retain_declared_path=True)
    events = completions.iter_secondary_native_events(fixture["packet"])
    events = list(events)
    with (
        completions._readonly(fixture["history"]) as history,
        completions._readonly(fixture["catalog"]) as catalog,
    ):
        declared = completions._snapshot_index(history)
        adapted = completions._snapshot_resolver(history, catalog)
        for row in catalog.execute("SELECT * FROM messages"):
            before = completions._complete_resource(
                dict(row), fixture["conversation"], events, declared
            )
            after = completions._complete_resource(
                dict(row), fixture["conversation"], events, adapted
            )
            assert before == after
            assert (
                "snapshot_receipt_adapter"
                not in json.loads(after[0]["provenance"])[
                    "secondary_resource_completion"
                ]
            )


def test_incremental_replay_skips_exact_verified_witnesses_before_projection(
    completion_fixture, tmp_path, monkeypatch
):
    fixture = completion_fixture
    base = _stage(fixture)

    def unexpected(*_args):
        raise AssertionError("A completed physical witness must not be reprojected.")

    monkeypatch.setattr(completions, "_complete_resource", unexpected)
    report = completions.stage_secondary_message_completions(
        fixture["root"],
        tmp_path / "incremental.sqlite3",
        [fixture["packet"]],
        notice_plans=(),
        base_candidate_path=fixture["candidate"],
        base_candidate_sha256=base["candidate_sha256"],
        only_uncompleted=True,
    )
    assert report["incremental"]["same_stored_witness_skipped"] == 2
    assert report["counts"] == base["counts"]


def test_incremental_new_sender_conflict_is_quarantined_and_preserved(
    completion_fixture, tmp_path
):
    fixture = completion_fixture
    base = _stage(fixture)
    packet = deepcopy(fixture["packet"])
    packet["messages"][0].update(
        sender_id=native.OWNER,
        sender_label=native.OWNER,
        direction="sent",
        real_sender_id=9,
        sender_proof=native._proof(9, native.OWNER),
    )
    report = completions.stage_secondary_message_completions(
        fixture["root"],
        tmp_path / "incremental-conflict.sqlite3",
        [packet],
        notice_plans=(),
        base_candidate_path=fixture["candidate"],
        base_candidate_sha256=base["candidate_sha256"],
        only_uncompleted=True,
    )
    assert report["incremental"]["new_sender_conflicts"] == 1
    assert report["counts"] == {"native_quote": 1}
    with completions._readonly(report["candidate"]) as output:
        assert (
            output.execute("SELECT id FROM ambiguities").fetchone()[0]
            == fixture["image"]["id"]
        )
        conflict = output.execute(
            "SELECT * FROM incremental_resource_conflicts"
        ).fetchone()
        old = completions.load_json_cell(
            output.execute(
                "SELECT message FROM witnesses WHERE id=?",
                (conflict["prior_witness_key"],),
            ).fetchone()[0]
        )
        new = completions.load_json_cell(
            output.execute(
                "SELECT message FROM witnesses WHERE id=?", (conflict["witness_key"],)
            ).fetchone()[0]
        )
        assert old["sender_id"] != new["sender_id"]
    replay = completions.stage_secondary_message_completions(
        fixture["root"],
        tmp_path / "quarantine-replay.sqlite3",
        [fixture["packet"]],
        notice_plans=(),
        base_candidate_path=report["candidate"],
        base_candidate_sha256=report["candidate_sha256"],
        only_uncompleted=True,
    )
    assert replay["incremental"]["quarantined_rows_skipped"] == 1
    assert replay["counts"] == {"native_quote": 1}


def test_incremental_encoded_successful_rows_keep_storage_and_proof_bytes(
    completion_fixture, tmp_path
):
    fixture = completion_fixture
    base = _stage(fixture)
    encoded = completions.compact_secondary_message_completions(
        fixture["root"],
        fixture["candidate"],
        tmp_path / "encoded-only-base.sqlite3",
        candidate_sha256=base["candidate_sha256"],
    )
    packet = deepcopy(fixture["packet"])
    packet["messages"][0]["local_id"] += 100
    report = completions.stage_secondary_message_completions(
        fixture["root"],
        tmp_path / "incremental-encoded.sqlite3",
        [packet],
        notice_plans=(),
        base_candidate_path=encoded["candidate"],
        base_candidate_sha256=encoded["candidate_sha256"],
        only_uncompleted=True,
    )
    assert report["incremental"]["new_nonconflicting_witness_observations"] == 1
    with (
        completions._readonly(encoded["candidate"]) as before,
        completions._readonly(report["candidate"]) as after,
    ):
        for table, columns in [
            ("messages", "id,provenance"),
            ("entries", "id,original_row,projected_sha256"),
        ]:
            assert [
                tuple(row)
                for row in before.execute(
                    "SELECT " + columns + " FROM " + table + " ORDER BY id"
                )
            ] == [
                tuple(row)
                for row in after.execute(
                    "SELECT " + columns + " FROM " + table + " ORDER BY id"
                )
            ]


def test_incremental_typed_quote_body_conflict_is_not_hidden_by_existing_entry(
    completion_fixture, tmp_path
):
    fixture = completion_fixture
    base = _stage(fixture)
    packet = deepcopy(fixture["packet"])
    quote = native._message(
        10,
        49,
        sender=sender.PRIMARY,
        raw="<msg><appmsg><title>A changed literal reply</title><type>57</type><refermsg><type>3</type><svrid>504</svrid><fromusr>"
        + native.ROOM
        + "</fromusr><chatusr>"
        + native.MEMBER
        + "</chatusr><displayname>Native quoted author</displayname><createtime>1700000004</createtime><content>opaque image metadata</content></refermsg></appmsg></msg>",
    )
    quote.update(real_sender_id=9, sender_proof=native._proof(9, sender.PRIMARY))
    packet["messages"][1] = quote
    report = completions.stage_secondary_message_completions(
        fixture["root"],
        tmp_path / "incremental-body-conflict.sqlite3",
        [packet],
        notice_plans=(),
        base_candidate_path=fixture["candidate"],
        base_candidate_sha256=base["candidate_sha256"],
        only_uncompleted=True,
    )
    assert report["incremental"]["new_quote_body_conflicts"] == 1
    assert report["counts"] == {"native_sender": 1}


def test_incremental_mode_still_validates_every_packet_before_skipping(
    completion_fixture, tmp_path
):
    fixture = completion_fixture
    base = _stage(fixture)
    packet = deepcopy(fixture["packet"])
    packet.pop("account_binding")
    with pytest.raises(ValueError, match="account binding"):
        completions.stage_secondary_message_completions(
            fixture["root"],
            tmp_path / "invalid-incremental.sqlite3",
            [packet],
            notice_plans=(),
            base_candidate_path=fixture["candidate"],
            base_candidate_sha256=base["candidate_sha256"],
            only_uncompleted=True,
        )


def test_optional_completion_input_omits_absence_but_retains_unavailable_presence(
    tmp_path, monkeypatch
):
    history = tmp_path / "history.sqlite3"
    catalog = history.with_name(completions.FILENAME)
    path = str(catalog.absolute())
    assert completions.completion_input_identities(history) == {}
    catalog.write_bytes(b"Resident metadata probe; no database payload is read.")
    original_identity = completions._identity
    monkeypatch.setattr(
        Path,
        "open",
        lambda *_args, **_kwargs: pytest.fail(
            "Input identities must not read source payloads."
        ),
    )
    assert completions.completion_input_identities(history)[path] == original_identity(
        catalog
    )

    def unavailable(_path, *, optional=False):
        assert _path == catalog and optional
        raise ValueError("Synthetic resident source is temporarily unavailable.")

    monkeypatch.setattr(completions, "_identity", unavailable)
    state = catalog.lstat()
    assert completions.completion_input_identities(history) == {
        path: {
            "unavailable": True,
            "size": state.st_size,
            "mtime_ns": state.st_mtime_ns,
        }
    }


def _same_body_quote_witness(
    *,
    actor=native.MEMBER,
    server="504",
    typ=3,
    subtype=6,
    label="Native quoted author",
    title="Same inert title",
    quoted_body="Known quoted body",
):
    from xml.sax.saxutils import escape

    content = (
        quoted_body
        if typ == 1
        else escape(
            "<msg><appmsg><type>"
            + str(subtype)
            + "</type><title>"
            + title
            + "</title></appmsg></msg>"
        )
        if typ == 49
        else ""
    )
    quote = native._message(
        10,
        49,
        sender=sender.PRIMARY,
        raw="<msg><appmsg><title>Verified reply</title><type>57</type><refermsg><type>"
        + str(typ)
        + "</type><svrid>"
        + server
        + "</svrid><fromusr>"
        + native.ROOM
        + "</fromusr><chatusr>"
        + actor
        + "</chatusr><displayname>"
        + label
        + "</displayname><createtime>1700000004</createtime><content>"
        + content
        + "</content></refermsg></appmsg></msg>",
    )
    quote.update(real_sender_id=9, sender_proof=native._proof(9, sender.PRIMARY))
    if typ == 49:
        # The strategy consumes sealed parsed facts independently of extractor revisions.
        quote["native_text"]["quoted_reference"]["app_card_metadata"] = {
            "schema_version": 1,
            "appmsg_subtype": subtype,
            "safe_title": title,
        }
    return quote


@pytest.mark.parametrize(
    "initial,change",
    [
        ({}, {"actor": "wxid_different_quoted_actor"}),
        ({}, {"server": "999"}),
        ({}, {"typ": 43}),
        ({"typ": 49}, {"typ": 49, "subtype": 5}),
        ({"typ": 49}, {"typ": 49, "title": "Another explicit quoted title"}),
    ],
)
def test_incremental_same_reply_cannot_hide_conflicting_quoted_reference_facts(
    completion_fixture, tmp_path, initial, change
):
    fixture = completion_fixture
    fixture["packet"]["messages"][1] = _same_body_quote_witness(**initial)
    base = _stage(fixture)
    packet = deepcopy(fixture["packet"])
    packet["messages"][1] = _same_body_quote_witness(**change)
    assert packet["messages"][1]["text"] == fixture["packet"]["messages"][1]["text"]
    report = completions.stage_secondary_message_completions(
        fixture["root"],
        tmp_path / "reference-conflict.sqlite3",
        [packet],
        notice_plans=(),
        base_candidate_path=base["candidate"],
        base_candidate_sha256=base["candidate_sha256"],
        only_uncompleted=True,
    )
    assert report["incremental"]["new_quote_reference_conflicts"] == 1
    assert report["counts"] == {"native_sender": 1}
    with completions._readonly(report["candidate"]) as output:
        conflict = output.execute(
            "SELECT * FROM incremental_resource_conflicts"
        ).fetchone()
        assert conflict["prior_witness_key"] != conflict["witness_key"]
        assert "quoted-reference" in conflict["reason"]
    replay = completions.stage_secondary_message_completions(
        fixture["root"],
        tmp_path / "retained-reference-quarantine.sqlite3",
        [fixture["packet"]],
        notice_plans=(),
        base_candidate_path=report["candidate"],
        base_candidate_sha256=report["candidate_sha256"],
        only_uncompleted=True,
    )
    assert replay["incremental"]["quarantined_rows_skipped"] == 1
    assert replay["counts"] == {"native_sender": 1}


@pytest.mark.parametrize(
    "initial,change",
    [
        ({}, {"label": "Another account's alias"}),
        ({"actor": "", "server": ""}, {}),
    ],
)
def test_incremental_quote_labels_and_unknown_detail_enrichment_keep_encoded_proof(
    completion_fixture, tmp_path, initial, change
):
    fixture = completion_fixture
    fixture["packet"]["messages"][1] = _same_body_quote_witness(**initial)
    base = _stage(fixture)
    encoded = completions.compact_secondary_message_completions(
        fixture["root"],
        base["candidate"],
        tmp_path / "encoded-reference-base.sqlite3",
        candidate_sha256=base["candidate_sha256"],
    )
    packet = deepcopy(fixture["packet"])
    packet["messages"][1] = _same_body_quote_witness(**change)
    report = completions.stage_secondary_message_completions(
        fixture["root"],
        tmp_path / "compatible-reference.sqlite3",
        [packet],
        notice_plans=(),
        base_candidate_path=encoded["candidate"],
        base_candidate_sha256=encoded["candidate_sha256"],
        only_uncompleted=True,
    )
    assert report["counts"] == {"native_quote": 1, "native_sender": 1}
    assert report["incremental"]["new_nonconflicting_witness_observations"] == 1
    with (
        completions._readonly(encoded["candidate"]) as before,
        completions._readonly(report["candidate"]) as after,
    ):
        for table, columns in [
            ("messages", "id,provenance"),
            ("entries", "id,original_row,projected_sha256"),
        ]:
            statement = "SELECT " + columns + " FROM " + table + " ORDER BY id"
            assert list(map(tuple, before.execute(statement))) == list(
                map(tuple, after.execute(statement))
            )


def test_incremental_explicit_outer_app_type_conflict_is_not_plain_text_agreement(
    completion_fixture, tmp_path
):
    fixture = completion_fixture
    base = _stage(fixture)
    packet = deepcopy(fixture["packet"])
    novel = native._message(
        10,
        49,
        sender=sender.PRIMARY,
        raw="<msg><appmsg><type>1</type><title>Verified reply</title></appmsg></msg>",
    )
    novel.update(real_sender_id=9, sender_proof=native._proof(9, sender.PRIMARY))
    assert novel["text"] == packet["messages"][1]["text"]
    packet["messages"][1] = novel
    report = completions.stage_secondary_message_completions(
        fixture["root"],
        tmp_path / "outer-type-conflict.sqlite3",
        [packet],
        notice_plans=(),
        base_candidate_path=base["candidate"],
        base_candidate_sha256=base["candidate_sha256"],
        only_uncompleted=True,
    )
    assert report["incremental"]["new_quote_reference_conflicts"] == 1
    assert report["counts"] == {"native_sender": 1}


def _canonical_compaction_tables(path):
    json_columns = {
        "messages": {"provenance"},
        "entries": {"original_row", "conversation_row", "plan"},
        "sources": {"header"},
        "witnesses": {"message"},
    }
    result = {}
    with completions._readonly(path) as connection:
        for table in [
            "messages",
            "entries",
            "sources",
            "witnesses",
            "ambiguities",
            "archive_info",
            "append_event_heads",
            "append_event_audit",
            "incremental_resource_conflicts",
        ]:
            rows = []
            for row in connection.execute('SELECT * FROM "' + table + '" ORDER BY 1'):
                value = dict(row)
                for column in json_columns.get(table, ()):
                    value[column] = completions.decode_json_cell(value[column])
                rows.append(value)
            result[table] = rows
    return result


@pytest.mark.parametrize("selection", ["all", "partial", "none"])
def test_stream_compaction_matches_exact_clone_selection_and_canonical_cells(
    completion_fixture, tmp_path, monkeypatch, selection
):
    fixture = completion_fixture
    if selection in {"partial", "none"}:
        _retain_primary_image(fixture)
    if selection == "none":
        with sqlite3.connect(fixture["history"]) as history:
            history.execute(
                "INSERT INTO messages VALUES("
                + ",".join("?" for _ in completions.COLUMNS)
                + ")",
                tuple(fixture["quote"][column] for column in completions.COLUMNS),
            )
        with sqlite3.connect(fixture["catalog"]) as catalog:
            catalog.execute(
                "UPDATE archive_info SET value=? WHERE key='history_file_identity'",
                (json.dumps(_file_identity(fixture["history"])),),
            )
    stage = _stage(fixture)
    source_bytes = fixture["candidate"].read_bytes()
    normal = completions.compact_secondary_message_completions(
        fixture["root"],
        fixture["candidate"],
        tmp_path / "clone-result.sqlite3",
        candidate_sha256=stage["candidate_sha256"],
    )
    phases = []
    monkeypatch.setattr(
        completions,
        "_clonefile",
        lambda *_args: pytest.fail("Stream mode must not clone."),
    )
    monkeypatch.setattr(
        completions,
        "_encode_derived_json",
        lambda *_args: pytest.fail("Stream mode must encode during copy."),
    )
    streamed = completions.compact_secondary_message_completions(
        fixture["root"],
        fixture["candidate"],
        tmp_path / "stream-result.sqlite3",
        candidate_sha256=stage["candidate_sha256"],
        stream_compaction=True,
        progress=phases.append,
    )
    assert streamed["compaction_mode"] == "stream"
    assert normal["retained_counts"] == streamed["retained_counts"]
    assert (
        normal["manifest_uncompressed_sha256"]
        == streamed["manifest_uncompressed_sha256"]
    )
    assert _canonical_compaction_tables(
        normal["candidate"]
    ) == _canonical_compaction_tables(streamed["candidate"])
    assert fixture["candidate"].read_bytes() == source_bytes
    assert "vacuuming_candidate" not in [value["phase"] for value in phases]
    assert not list(tmp_path.glob(".completion-stream-*"))
    assert not owner_only_problem(Path(streamed["candidate"]))
    if sys.platform != "win32":
        assert Path(streamed["candidate"]).stat().st_mode & 0o777 == 0o600


def test_stream_compaction_keeps_quarantined_audit_only_native_witnesses(
    completion_fixture, tmp_path
):
    fixture = completion_fixture
    base = _stage(fixture)
    packet = deepcopy(fixture["packet"])
    packet["messages"][1] = _same_body_quote_witness(
        actor="wxid_conflicting_quote_actor"
    )
    stage = completions.stage_secondary_message_completions(
        fixture["root"],
        tmp_path / "quarantined-source.sqlite3",
        [packet],
        notice_plans=(),
        base_candidate_path=base["candidate"],
        base_candidate_sha256=base["candidate_sha256"],
        only_uncompleted=True,
    )
    assert stage["incremental"]["new_quote_reference_conflicts"] == 1
    normal = completions.compact_secondary_message_completions(
        fixture["root"],
        stage["candidate"],
        tmp_path / "audit-clone.sqlite3",
        candidate_sha256=stage["candidate_sha256"],
    )
    streamed = completions.compact_secondary_message_completions(
        fixture["root"],
        stage["candidate"],
        tmp_path / "audit-stream.sqlite3",
        candidate_sha256=stage["candidate_sha256"],
        stream_compaction=True,
    )
    assert _canonical_compaction_tables(
        normal["candidate"]
    ) == _canonical_compaction_tables(streamed["candidate"])
    with completions._readonly(streamed["candidate"]) as result:
        assert (
            result.execute(
                "SELECT COUNT(*) FROM incremental_resource_conflicts"
            ).fetchone()[0]
            == 1
        )
        assert result.execute("SELECT COUNT(*) FROM witnesses").fetchone()[0] == 3
        assert result.execute("SELECT COUNT(*) FROM ambiguities").fetchone()[0] == 1


def test_stream_compaction_partial_failure_is_not_publishable_and_preserves_source(
    completion_fixture, tmp_path, monkeypatch
):
    from app.core import wechat_completion_stream_copy as copier

    fixture = completion_fixture
    stage = _stage(fixture)
    source_bytes = fixture["candidate"].read_bytes()
    copy = copier._copy_table

    def stop_after_entries(output, table, *args):
        copy(output, table, *args)
        if table == "entries":
            raise ValueError("Injected interruption after a committed partial table.")

    monkeypatch.setattr(copier, "_copy_table", stop_after_entries)
    target = tmp_path / "partial-stream.sqlite3"
    with pytest.raises(ValueError, match="Injected interruption"):
        completions.compact_secondary_message_completions(
            fixture["root"],
            fixture["candidate"],
            target,
            candidate_sha256=stage["candidate_sha256"],
            stream_compaction=True,
        )
    with completions._readonly(target) as partial:
        assert (
            partial.execute(
                "SELECT value FROM archive_info WHERE key='staging_state'"
            ).fetchone()[0]
            == "in_progress"
        )
    with pytest.raises(ValueError, match="not completed staging"):
        completions.publish_secondary_message_completions(
            fixture["root"],
            target,
            candidate_sha256=completions._file_sha(target),
        )
    assert fixture["candidate"].read_bytes() == source_bytes
    assert not completions.completion_path(fixture["root"]).exists()


def test_stream_output_reservation_never_overwrites_a_racing_file(
    completion_fixture, tmp_path
):
    fixture = completion_fixture
    stage = _stage(fixture)
    target = tmp_path / "racing-stream.sqlite3"
    payload = b"Independent destination created after the initial absence check."

    def race(value):
        if value["phase"] == "creating_stream_candidate":
            target.write_bytes(payload)

    with pytest.raises(FileExistsError):
        completions.compact_secondary_message_completions(
            fixture["root"],
            fixture["candidate"],
            target,
            candidate_sha256=stage["candidate_sha256"],
            stream_compaction=True,
            progress=race,
        )
    assert target.read_bytes() == payload
    assert not list(tmp_path.glob(".completion-stream-*"))


def test_stream_output_replacement_is_never_reset_or_modified_by_failure(
    completion_fixture, tmp_path
):
    fixture = completion_fixture
    stage = _stage(fixture)
    target = tmp_path / "replaced-stream.sqlite3"
    saved = tmp_path / "retained-partial-inode.sqlite3"
    payload = b"External file must remain untouched."

    def replace(value):
        if value["phase"] == "checking_input_pins":
            target.rename(saved)
            target.write_bytes(payload)

    with pytest.raises(ValueError, match="reserved public inode"):
        completions.compact_secondary_message_completions(
            fixture["root"],
            fixture["candidate"],
            target,
            candidate_sha256=stage["candidate_sha256"],
            stream_compaction=True,
            progress=replace,
        )
    assert target.read_bytes() == payload
    assert saved.exists()
    with completions._readonly(saved) as retained:
        assert (
            retained.execute(
                "SELECT value FROM archive_info WHERE key='staging_state'"
            ).fetchone()[0]
            == "in_progress"
        )
    assert list(tmp_path.glob(".completion-stream-*/catalog.sqlite3"))


def test_stream_compaction_rejects_current_overlay_drift(completion_fixture, tmp_path):
    fixture = completion_fixture
    stage = _stage(fixture)

    def drift(value):
        if value["phase"] == "streaming_retained_rows":
            fixture["history"].with_name("macos_recovery.json").write_text("{}")

    with pytest.raises(ValueError, match="compare-and-swap"):
        completions.compact_secondary_message_completions(
            fixture["root"],
            fixture["candidate"],
            tmp_path / "drift-stream.sqlite3",
            candidate_sha256=stage["candidate_sha256"],
            stream_compaction=True,
            progress=drift,
        )
    assert not completions.completion_path(fixture["root"]).exists()


@pytest.mark.parametrize(
    "body,expected_conflict", [("", False), ("A conflicting literal quote", True)]
)
def test_incremental_quoted_content_omission_is_enrichment_but_known_literal_conflicts(
    completion_fixture, tmp_path, body, expected_conflict
):
    fixture = completion_fixture
    fixture["packet"]["messages"][1] = _same_body_quote_witness(typ=1)
    base = _stage(fixture)
    packet = deepcopy(fixture["packet"])
    packet["messages"][1] = _same_body_quote_witness(typ=1, quoted_body=body)
    assert packet["messages"][1]["text"] != fixture["packet"]["messages"][1]["text"]
    report = completions.stage_secondary_message_completions(
        fixture["root"],
        tmp_path / "literal-enrichment.sqlite3",
        [packet],
        notice_plans=(),
        base_candidate_path=base["candidate"],
        base_candidate_sha256=base["candidate_sha256"],
        only_uncompleted=True,
    )
    if expected_conflict:
        assert report["incremental"]["new_quote_body_conflicts"] == 1
        assert report["counts"] == {"native_sender": 1}
    else:
        assert report["counts"] == {"native_quote": 1, "native_sender": 1}
        assert report["incremental"]["new_nonconflicting_witness_observations"] == 1
        assert (
            _canonical_compaction_tables(base["candidate"])["messages"]
            == _canonical_compaction_tables(report["candidate"])["messages"]
        )


def test_stream_compaction_crosses_commits_with_all_original_projection_hashes(
    completion_fixture, tmp_path
):
    fixture = completion_fixture
    initial = fixture["packet"]["messages"][0]
    rows, events = [], []
    for index in range(300):
        row, event = deepcopy(fixture["image"]), deepcopy(initial)
        row["id"] = hashlib.sha256(("streamed-row-" + str(index)).encode()).hexdigest()
        row["ordinal"] += index
        event["local_id"] += index
        event["server_id"] = str(int(event["server_id"]) + index)
        proof = json.loads(row["provenance"])
        proof["record_id"] = row["id"]
        proof["server_id"] = event["server_id"]
        proof["locator"]["row_id"] += index
        proof["locator"]["local_id"] = row["ordinal"]
        row["provenance"] = json.dumps(proof)
        rows.append(row)
        events.append(event)
    with sqlite3.connect(fixture["catalog"]) as catalog:
        catalog.execute("DELETE FROM messages")
        catalog.execute("UPDATE conversations SET message_count=300")
        catalog.executemany(
            "INSERT INTO messages VALUES("
            + ",".join("?" for _ in completions.COLUMNS)
            + ")",
            [tuple(row[column] for column in completions.COLUMNS) for row in rows],
        )
    fixture["packet"]["messages"] = events
    stage = _stage(fixture)
    assert stage["counts"] == {"native_sender": 300}
    result = completions.compact_secondary_message_completions(
        fixture["root"],
        stage["candidate"],
        tmp_path / "stream-300.sqlite3",
        candidate_sha256=stage["candidate_sha256"],
        stream_compaction=True,
    )
    with (
        completions._readonly(stage["candidate"]) as source,
        completions._readonly(result["candidate"]) as output,
    ):
        identifiers = [
            row[0] for row in source.execute("SELECT id FROM messages ORDER BY rowid")
        ]
        assert identifiers != sorted(identifiers)
        assert output.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 300
        assert list(
            map(
                tuple,
                source.execute("SELECT id,projected_sha256 FROM entries ORDER BY id"),
            )
        ) == list(
            map(
                tuple,
                output.execute("SELECT id,projected_sha256 FROM entries ORDER BY id"),
            )
        )
        assert [
            dict(row) for row in source.execute("SELECT * FROM messages ORDER BY id")
        ] == [
            completions._decoded_message(row)
            for row in output.execute("SELECT * FROM messages ORDER BY id")
        ]


def test_stream_compaction_mixed_cells_and_orphans_match_clone(
    completion_fixture, tmp_path
):
    fixture = completion_fixture
    stage = _stage(fixture)
    with sqlite3.connect(stage["candidate"]) as source:
        row = source.execute(
            "SELECT id,provenance FROM messages ORDER BY id LIMIT 1"
        ).fetchone()
        source.execute(
            "UPDATE messages SET provenance=? WHERE id=?",
            (completions.encode_json_cell(row[1]), row[0]),
        )
        source.execute(
            "INSERT INTO sources VALUES(?,?,?)", ("orphan", "{}", "unused-orphan")
        )
        source.execute(
            "INSERT INTO witnesses VALUES(?,?,?,?)",
            ("orphan", "orphan", "{}", "unused-orphan"),
        )
    reviewed = completions._file_sha(stage["candidate"])
    normal = completions.compact_secondary_message_completions(
        fixture["root"],
        stage["candidate"],
        tmp_path / "mixed-clone.sqlite3",
        candidate_sha256=reviewed,
    )
    streamed = completions.compact_secondary_message_completions(
        fixture["root"],
        stage["candidate"],
        tmp_path / "mixed-stream.sqlite3",
        candidate_sha256=reviewed,
        stream_compaction=True,
    )
    assert _canonical_compaction_tables(
        normal["candidate"]
    ) == _canonical_compaction_tables(streamed["candidate"])
    with completions._readonly(streamed["candidate"]) as output:
        assert (
            output.execute("SELECT COUNT(*) FROM sources WHERE id='orphan'").fetchone()[
                0
            ]
            == 0
        )
        assert (
            output.execute(
                "SELECT COUNT(*) FROM witnesses WHERE id='orphan'"
            ).fetchone()[0]
            == 0
        )


def test_stream_compaction_replays_native_witness_integrity_before_copy(
    completion_fixture, tmp_path
):
    fixture = completion_fixture
    stage = _stage(fixture)
    with sqlite3.connect(stage["candidate"]) as source:
        row = source.execute("SELECT id,message FROM witnesses LIMIT 1").fetchone()
        value = json.loads(row[1])
        value["sender_id"] = "wxid_unauthorized_native_sender"
        source.execute(
            "UPDATE witnesses SET message=? WHERE id=?", (json.dumps(value), row[0])
        )
    destination = tmp_path / "tampered-source-stream.sqlite3"
    with pytest.raises(ValueError, match="native witness changed"):
        completions.compact_secondary_message_completions(
            fixture["root"],
            stage["candidate"],
            destination,
            candidate_sha256=completions._file_sha(stage["candidate"]),
            stream_compaction=True,
        )
    assert not destination.exists()


@pytest.mark.parametrize(
    "failed_phase",
    ["sync", "validating_compacted_candidate", "hashing_compacted_candidate"],
)
def test_stream_output_stays_incomplete_if_final_flush_or_verification_fails(
    completion_fixture, tmp_path, monkeypatch, failed_phase
):
    from app.core import wechat_completion_stream_copy as copier

    fixture = completion_fixture
    stage = _stage(fixture)
    target = tmp_path / "failed-final-stream.sqlite3"
    if failed_phase == "sync":

        def fail_sync(_owner):
            raise ValueError("Injected final stream verification failure.")

        monkeypatch.setattr(copier, "sync_stream_file", fail_sync)

    def fail_phase(value):
        if value["phase"] == failed_phase:
            raise ValueError("Injected final stream verification failure.")

    with pytest.raises(ValueError, match="Injected final stream"):
        completions.compact_secondary_message_completions(
            fixture["root"],
            stage["candidate"],
            target,
            candidate_sha256=stage["candidate_sha256"],
            stream_compaction=True,
            progress=fail_phase,
        )
    with completions._readonly(target) as retained:
        assert (
            retained.execute(
                "SELECT value FROM archive_info WHERE key='staging_state'"
            ).fetchone()[0]
            == "in_progress"
        )
    with pytest.raises(ValueError, match="not completed staging"):
        completions.publish_secondary_message_completions(
            fixture["root"], target, candidate_sha256=completions._file_sha(target)
        )


def test_stream_private_permissions_are_enforced_before_opening_sqlite(
    tmp_path, monkeypatch
):
    from app.core import wechat_completion_stream_copy as copier

    observed = []
    original_connect = copier.sqlite3.connect
    original_owner = copier.owner_only_files.ensure_owner_only

    def directory_boundary(path):
        assert path.is_dir() and not list(path.iterdir())
        path.chmod(0o700)
        observed.append("directory")

    def file_boundary(path):
        assert path.stat().st_size == 0 and path.stat().st_nlink == 1
        original_owner(path)
        observed.append("file")

    def connect_after_boundaries(*args, **kwargs):
        assert observed == ["directory", "file"]
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(
        copier.owner_only_files,
        "ensure_owner_only_directory",
        directory_boundary,
        raising=False,
    )
    monkeypatch.setattr(copier.owner_only_files, "ensure_owner_only", file_boundary)
    monkeypatch.setattr(copier.sqlite3, "connect", connect_after_boundaries)
    target = tmp_path / "owner-only-stream.sqlite3"
    connection, owner = copier.create_stream_output(target)
    connection.close()
    copier.release_stream_link(owner, target)
    assert not copier.owner_only_files.owner_only_problem(target)
