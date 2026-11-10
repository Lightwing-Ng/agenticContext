"""Derived completion catalogs preserve originals and serve one consistent timeline.

Code version: v1.0.5-codex.1
"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from app.core import wechat_secondary_completions as completions
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
    assert before[path] is None and after[path] is not None
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
    with completions._readonly(fixture["catalog"]) as catalog:
        original_row = dict(
            catalog.execute(
                "SELECT * FROM messages WHERE id=?", (fixture["image"]["id"],)
            ).fetchone()
        )
    assert manifest[1]["original_sha256"] == completions._sha(original_row)
    with (
        completions._readonly(compacted) as output,
        completions._readonly(fixture["candidate"]) as source,
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
