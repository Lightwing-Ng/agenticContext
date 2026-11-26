"""Same native bytes may gain bounded semantics without rewriting prior observations.

Code version: v1.3.2-codex.0
"""

from contextlib import closing
from copy import deepcopy
from html import escape
import json
import sqlite3

import pytest

from app.core import wechat_macos_recovery_bank as bank
from app.core import wechat_macos_reparse as repair
from app.core import wechat_macos_recovery_revalidation as revalidation
from app.core.wechat_mac_refresh_projection import _project_message
from app.core.wechat_archive import WechatArchiveRepository
from app.core.wechat_text import native_components_text
from tests import test_wechat_macos_recovery_bank as fixtures


mac_archive = fixtures.mac_archive
bank_archive = fixtures.bank_archive


def _packets(fixture, kind):
    if kind == "forward":
        record = '<recordinfo><datalist><dataitem datatype="17"><recordxml><recordinfo><datalist><dataitem datatype="1"><datadesc>Nested first\n\nLiteral • text</datadesc></dataitem></datalist></recordinfo></recordxml></dataitem><dataitem datatype="1"><datadesc>Retained neighbor</datadesc></dataitem></datalist></recordinfo>'
        raw = (
            "<msg><appmsg><type>19</type><title>Synthetic nested record</title><recorditem>"
            + record
            + "</recorditem></appmsg></msg>"
        )
    else:
        raw = "<msg><appmsg><type>57</type><title>Literal reply</title><refermsg><type>49</type><svrid>451</svrid><fromusr>wxid_synthetic_original</fromusr><displayname>Original source</displayname><createtime>1700000200</createtime><content>&lt;msg&gt;&lt;appmsg&gt;&lt;type&gt;57&lt;/type&gt;&lt;title&gt;Exact quoted body&lt;/title&gt;&lt;/appmsg&gt;&lt;/msg&gt;</content></refermsg></appmsg></msg>"
    new = deepcopy(fixture.group)
    message = fixtures._message(90, 49, raw=raw)
    new["messages"] = [message]
    old = deepcopy(new)
    native = old["messages"][0]["native_text"]
    native["text_components"] = (
        native["text_components"][1:]
        if kind == "forward"
        else native["text_components"][:1]
    )
    if kind == "forward":
        native["omitted_forwarded_items"] = 1
        native["flags"] = [
            "forwarded_text_only",
            "forwarded_non_text_or_unreadable_items_omitted",
            "forwarded_record_partially_unreadable",
        ]
    else:
        native["flags"] = ["quoted_reply_envelope", "quoted_non_text_content_omitted"]
    native["text"] = native_components_text(native["text_components"])
    old["messages"][0]["text"] = native["text"]
    return old, new


def _rows(path, table):
    with closing(sqlite3.connect(path)) as connection, connection:
        return connection.execute("SELECT * FROM " + table + " ORDER BY 1,2").fetchall()


def _quoted_card_packets(fixture, *, nested=False, prior_title=False):
    card = '<msg><appmsg><type>76</type><title>广岛之恋</title><sourcedisplayname>Literal publisher</sourcedisplayname><url>https://example.invalid/private-token</url></appmsg><appinfo><appname>QQ音乐</appname></appinfo></msg>'
    raw = '<msg><appmsg><type>57</type><title>好歌</title><refermsg><type>49</type><fromusr>wxid_synthetic_original</fromusr><svrid>451</svrid><createtime>1700000200</createtime><content>' + escape(card) + '</content></refermsg></appmsg></msg>'
    if nested:
        raw = '<msg><appmsg><type>19</type><recorditem><recordinfo><datalist><dataitem datatype="49"><sourcename>Literal forwarder</sourcename>' + raw + '</dataitem></datalist></recordinfo></recorditem></appmsg></msg>'
    new = {**deepcopy(fixture.group), "messages": [fixtures._message(96, 49, raw=raw)]}
    old = deepcopy(new)

    def strip(value):
        if isinstance(value, dict):
            if "app_card_metadata" in value:
                if prior_title:
                    metadata = value["app_card_metadata"]
                    value["app_card_metadata"] = {key: metadata[key] for key in ("schema_version", "appmsg_subtype", "safe_title")}
                else:
                    value.pop("app_card_metadata")
            for child in value.values():
                strip(child)
        elif isinstance(value, list):
            for child in value:
                strip(child)

    strip(old["messages"][0]["native_text"])
    return old, new


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("prior_title", [False, True])
@pytest.mark.parametrize("later_source", [False, True])
def test_same_raw_inline_quote_labels_are_journaled_without_changing_body_or_receipt(
    bank_archive, nested, prior_title, later_source
):
    fixture = bank_archive
    old, new = _quoted_card_packets(fixture, nested=nested, prior_title=prior_title)
    assert old["messages"][0]["text"] == new["messages"][0]["text"] == "好歌"
    if later_source:
        new["source"].update(source_id="macos-later-quoted-card", snapshot_id="later-quoted-card-capture", main_sha256="3" * 64)
    fixtures._publish(fixture, packets=[old], name="before-quoted-labels")
    previous = _rows(fixture.bank_path, "messages")[0]
    occurrences = _rows(fixture.bank_path, "occurrences")
    original_bank = fixture.bank_path.read_bytes()
    candidate, receipt = fixtures._stage(fixture, packets=[new], name="with-quoted-labels")
    assert receipt["reparsed"] == 1 and receipt["added"] == receipt["upgraded"] == receipt["conflicts"] == 0
    assert fixture.bank_path.read_bytes() == original_bank
    current = _rows(candidate, "messages")[0]
    assert current[:11] == previous[:11]
    proof, old_proof = json.loads(current[11]), json.loads(previous[11])
    assert proof["macos_recovery"] == old_proof["macos_recovery"]
    origin = proof["quoted_app_card_metadata_origin"]
    assert set(origin) == set(repair._METADATA_SOURCE_FIELDS)
    for field in repair._RAW_FIELDS:
        assert origin[field] == proof["macos_recovery"][field]
    for field in ("source_id", "snapshot_id", "main_sha256", "wal_sha256"):
        assert origin[field] == new["source"][field]
    assert "private-token" not in current[11]
    current_occurrences = _rows(candidate, "occurrences")
    assert set(occurrences) <= set(current_occurrences)
    assert len(current_occurrences) == len(occurrences) + int(later_source)
    with closing(sqlite3.connect(candidate)) as connection, connection:
        reason, prior = connection.execute("SELECT reason,prior_row FROM projection_revisions").fetchone()
        assert reason == "quoted_card_metadata_completion"
        assert json.loads(prior)["provenance"] == previous[11]
        bank._validate_bank(connection, fixture.history)
    bank.publish_mac_recovery_bank(fixture.root, candidate, receipt)
    for label, packet in (("old", old), ("new", new)):
        replay = fixtures._publish(fixture, packets=[packet], name="quoted-replay-" + label)
        assert replay["reparsed"] == replay["added"] == 0
    assert _rows(fixture.bank_path, "messages")[0] == current
    assert len(_rows(fixture.bank_path, "projection_revisions")) == 1


@pytest.mark.parametrize("change", ["title", "reply", "quote_sender", "quote_clock", "quote_server", "flags", "raw_pin"])
def test_quote_label_completion_cannot_change_existing_source_evidence(bank_archive, change):
    fixture = bank_archive
    old, new = _quoted_card_packets(fixture, prior_title=True)
    fixtures._publish(fixture, packets=[old], name="known-quoted-labels")
    message = new["messages"][0]
    native = message["native_text"]
    reference = native["quoted_reference"]
    if change == "title":
        reference["app_card_metadata"]["safe_title"] = "Conflicting known title"
    elif change == "reply":
        native["text_components"][0]["text"] = "Changed reply"
        native["text"] = message["text"] = "Changed reply"
    elif change == "quote_sender":
        reference["source_sender_id"] = "wxid_other_original"
    elif change == "quote_clock":
        reference["source_timestamp"] = "1700000999"
    elif change == "quote_server":
        reference["source_ids"]["svrid"] = "452"
    elif change == "flags":
        native["flags"].append("unexpected_new_semantics")
    else:
        message["content_sha256"] = "9" * 64
    before = fixture.bank_path.read_bytes()
    try:
        _candidate, receipt = fixtures._stage(fixture, packets=[new], name="conflicting-quoted-labels")
    except ValueError:
        pass
    else:
        assert receipt["reparsed"] == 0 and receipt["conflicts"] > 0
    assert fixture.bank_path.read_bytes() == before


def _app_card_packets(fixture, *, prior_title=False):
    raw = (
        "<msg><appmsg><type>5</type><title>Original source title</title>"
        "<sourcedisplayname>Original publisher</sourcedisplayname></appmsg>"
        "<appinfo><appname>Source application</appname></appinfo></msg>"
    )
    message = _project_message(
        {
            "local_id": 94,
            "local_type": (5 << 32) | 49,
            "server_id": 594,
            "create_time": 1700000094,
            "real_sender_id": 3,
            "message_content": raw,
            "compression_marker": 0,
        },
        fixtures.OWNER,
        "group",
        {3: {"rowid": 3, "user_name": fixtures.MEMBER, "is_session": 0}},
    )
    new = {**deepcopy(fixture.group), "messages": [message]}
    old = deepcopy(new)
    old["messages"][0]["resource"] = {
        key: value
        for key, value in message["resource"].items()
        if key in {"kind", "label", "native_time", "app_subtype"}
    }
    if prior_title:
        old["messages"][0]["resource"]["safe_title"] = "Original source title"
    return old, new


@pytest.mark.parametrize("prior_title", [False, True])
def test_same_raw_card_labels_are_monotone_and_keep_historical_replays(
    bank_archive, prior_title
):
    fixture = bank_archive
    old, new = _app_card_packets(fixture, prior_title=prior_title)
    fixtures._publish(fixture, packets=[old], name="card-before-labels")
    prior_rows = _rows(fixture.bank_path, "messages")
    prior_occurrences = _rows(fixture.bank_path, "occurrences")
    original = fixture.bank_path.read_bytes()
    candidate, receipt = fixtures._stage(fixture, packets=[new], name="card-with-labels")
    assert receipt["reparsed"] == 1 and receipt["added"] == 0
    assert fixture.bank_path.read_bytes() == original
    assert _rows(candidate, "occurrences") == prior_occurrences
    with closing(sqlite3.connect(candidate)) as connection, connection:
        connection.row_factory = sqlite3.Row
        info = dict(connection.execute("SELECT key,value FROM archive_info"))
        assert info["code_version"] == bank.WRITER_VERSION
        row = connection.execute("SELECT * FROM messages").fetchone()
        assert tuple(row)[:11] == prior_rows[0][:11]
        proof = json.loads(row["provenance"])
        assert proof["resource_reference"]["safe_title"] == "Original source title"
        assert proof["resource_reference"]["publisher_name"] == "Original publisher"
        assert proof["resource_reference"]["app_name"] == "Source application"
        assert proof["app_card_metadata"]["schema_version"] == 1
        assert proof["macos_recovery"] == json.loads(prior_rows[0][11])["macos_recovery"]
        reason = connection.execute(
            "SELECT reason,prior_row,parser_revision FROM projection_revisions"
        ).fetchone()
        assert reason[0] == "app_card_metadata_completion"
        assert json.loads(reason[1])["provenance"] == prior_rows[0][11]
        assert reason[2] == bank.CODE_VERSION
        bank._validate_bank(connection, fixture.history)
    bank.publish_mac_recovery_bank(fixture.root, candidate, receipt)
    for label, packet in (("new", new), ("old", old)):
        replay = fixtures._publish(fixture, packets=[packet], name="card-replay-" + label)
        assert replay["reparsed"] == replay["added"] == 0
    assert _rows(fixture.bank_path, "occurrences") == prior_occurrences
    assert len(_rows(fixture.bank_path, "projection_revisions")) == 1


@pytest.mark.parametrize("legacy_label_paths", [False, True])
def test_new_card_metadata_records_complete_writer_policy_without_a_revision_journal(
    bank_archive, legacy_label_paths,
):
    fixture = bank_archive
    _old, new = _app_card_packets(fixture)
    if legacy_label_paths:
        resource = new["messages"][0]["resource"]
        del resource["app_name_source"], resource["publisher_name_source"]
    candidate, receipt = fixtures._stage(fixture, packets=[new], name="new-card-writer-tag")
    with closing(sqlite3.connect(candidate)) as connection, connection:
        info = dict(connection.execute("SELECT key,value FROM archive_info"))
        assert info["schema_version"] == "3"
        assert info["code_version"] == bank.WRITER_VERSION
        assert not repair._has_table(connection)
        bank._validate_bank(connection, fixture.history)
    with bank.mac_recovery_publication_guard(fixture.root) as guard:
        bank.publish_mac_recovery_bank(
            fixture.root, candidate, receipt, publication_guard=guard
        )
    before = _rows(fixture.bank_path, "messages")
    # A later legacy-only batch retains the complete provenance-policy tag.
    candidate, _receipt = fixtures._stage(
        fixture, packets=[fixture.direct], name="legacy-batch-retains-card-writer-tag"
    )
    with sqlite3.connect(candidate) as connection:
        info = dict(connection.execute("SELECT key,value FROM archive_info"))
        assert info["code_version"] == bank.WRITER_VERSION
        bank._validate_bank(connection, fixture.history)
    assert set(before) <= set(_rows(candidate, "messages"))


def test_channels_labels_complete_cached_original_and_quote_from_same_native_bytes(bank_archive):
    fixture = bank_archive
    fallback = "当前微信版本不支持展示该内容，请升级至最新版本。"
    card = (
        f"<appmsg><type>51</type><title>{fallback}</title><finderFeed>"
        "<desc>原帖文案\n保留换行</desc><nickname>示例发布者</nickname>"
        "<mediaList><media><url>https://example.invalid/private-media</url>"
        "</media></mediaList></finderFeed></appmsg>"
    )
    message = _project_message(
        {
            "local_id": 94,
            "local_type": (51 << 32) | 49,
            "server_id": 594,
            "create_time": 1700000094,
            "real_sender_id": 3,
            "message_content": card,
            "compression_marker": 0,
        },
        fixtures.OWNER,
        "group",
        {3: {"rowid": 3, "user_name": fixtures.MEMBER, "is_session": 0}},
    )
    reply = fixtures._message(
        96, 49,
        raw=(
            "<appmsg><type>57</type><title>原引用回复</title><refermsg>"
            f"<type>49</type><svrid>594</svrid><fromusr>{fixtures.MEMBER}</fromusr>"
            f"<createtime>1700000094</createtime><content>{escape(card)}</content>"
            "</refermsg></appmsg>"
        ),
    )
    new = {**deepcopy(fixture.group), "messages": [message, reply]}
    old = deepcopy(new)
    for metadata in (
        old["messages"][0]["resource"],
        old["messages"][1]["native_text"]["quoted_reference"]["app_card_metadata"],
    ):
        assert metadata.pop("channels_description") == "原帖文案\n保留换行"
        assert metadata.pop("channels_nickname") == "示例发布者"
    fixtures._publish(fixture, packets=[old], name="channels-legacy-labels")
    with closing(sqlite3.connect(fixture.bank_path)) as connection, connection:
        connection.execute(
            "UPDATE archive_info SET value='v1.4.3-codex.0' WHERE key='code_version'"
        )
    before = _rows(fixture.bank_path, "messages")
    receipt = fixtures._publish(fixture, packets=[new], name="channels-recovered-labels")
    assert (receipt["added"], receipt["reparsed"], receipt["conflicts"]) == (0, 2, 0)
    for previous, current in zip(before, _rows(fixture.bank_path, "messages"), strict=True):
        assert current[:11] == previous[:11]
        proof = json.loads(current[11])
        assert proof["macos_recovery"] == json.loads(previous[11])["macos_recovery"]
        labels = (
            proof["quoted_reference"]["app_card_metadata"]
            if current[6] == "原引用回复" else proof["resource_reference"]
        )
        assert labels["safe_title"] == fallback
        assert labels["channels_description"] == "原帖文案\n保留换行"
        assert labels["channels_nickname"] == "示例发布者"
        assert "private-media" not in current[11]


def test_card_labels_from_a_later_snapshot_keep_the_original_quote_receipt(bank_archive):
    fixture = bank_archive
    old, new = _app_card_packets(fixture)
    new["source"].update(
        source_id="macos-synthetic-card-label-refresh",
        snapshot_id="synthetic-later-native-capture",
        main_sha256="3" * 64,
    )
    fixtures._publish(fixture, packets=[old], name="quoted-original-card")
    before = _rows(fixture.bank_path, "messages")[0]
    original_occurrences = _rows(fixture.bank_path, "occurrences")
    receipt = fixtures._publish(fixture, packets=[new], name="later-card-labels")
    assert receipt["reparsed"] == 1 and receipt["added"] == 0
    current = _rows(fixture.bank_path, "messages")[0]
    proof = json.loads(current[11])
    assert current[:11] == before[:11]
    assert proof["macos_recovery"] == json.loads(before[11])["macos_recovery"]
    origin = proof["app_card_metadata_origin"]
    for field in ("source_id", "snapshot_id", "main_sha256", "wal_sha256"):
        assert origin[field] == new["source"][field]
    assert origin["content_sha256"] == new["messages"][0]["content_sha256"]
    assert set(original_occurrences) <= set(_rows(fixture.bank_path, "occurrences"))
    assert len(_rows(fixture.bank_path, "occurrences")) == len(original_occurrences) + 1
    for label, packet in (("old", old), ("new", new)):
        replay = fixtures._publish(fixture, packets=[packet], name="later-card-replay-" + label)
        assert replay["added"] == replay["reparsed"] == 0
    assert _rows(fixture.bank_path, "messages")[0] == current
    with sqlite3.connect(fixture.bank_path) as connection:
        bank._validate_bank(connection, fixture.history)


@pytest.mark.parametrize("field", ["safe_title", "publisher_name", "app_name"])
def test_card_reparse_cannot_replace_a_known_source_label(bank_archive, field):
    fixture = bank_archive
    _old, new = _app_card_packets(fixture)
    fixtures._publish(fixture, packets=[new], name="known-card-labels")
    changed = deepcopy(new)
    changed["messages"][0]["resource"][field] = "Conflicting source label"
    with pytest.raises(ValueError, match="revision history"):
        fixtures._stage(fixture, packets=[changed], name="conflicting-card-label")


def test_card_metadata_reparse_requires_the_same_native_body_pin(bank_archive):
    fixture = bank_archive
    old, new = _app_card_packets(fixture)
    fixtures._publish(fixture, packets=[old], name="pinned-card")
    new["messages"][0]["content_sha256"] = "9" * 64
    with pytest.raises(ValueError, match="changed after"):
        fixtures._stage(fixture, packets=[new], name="changed-card-body")


@pytest.mark.parametrize(
    "change",
    ["nested_name", "long_name", "wrong_publisher_path", "missing_app_path", "unversioned", "secret"],
)
def test_card_metadata_packet_rejects_ambiguous_or_opaque_labels(bank_archive, change):
    _old, new = _app_card_packets(bank_archive)
    resource = new["messages"][0]["resource"]
    if change == "nested_name":
        resource["publisher_name"] = {"nested": "Publisher"}
    elif change == "long_name":
        resource["publisher_name"] = "x" * 1025
    elif change == "wrong_publisher_path":
        resource["publisher_name_source"] = "appmsg/url"
    elif change == "missing_app_path":
        resource.pop("app_name_source")
    elif change == "unversioned":
        resource.pop("app_metadata_version")
    else:
        resource["aeskey"] = "opaque-secret"
    with pytest.raises(ValueError):
        bank._validate_packet(new, (fixtures.OWNER,))


@pytest.mark.parametrize("kind", ["quote", "forward"])
def test_same_raw_upgrade_preserves_ids_source_occurrences_and_old_projection(
    bank_archive, kind
):
    fixture = bank_archive
    old, new = _packets(fixture, kind)
    fixtures._publish(fixture, packets=[old], name="old-parser")
    prior_messages = _rows(fixture.bank_path, "messages")
    prior_occurrences = _rows(fixture.bank_path, "occurrences")
    prior_sources = _rows(fixture.bank_path, "sources")
    original_bank = fixture.bank_path.read_bytes()
    candidate, receipt = fixtures._stage(fixture, packets=[new], name="new-parser")
    assert receipt["reparsed"] == 1 and receipt["added"] == receipt["upgraded"] == 0
    assert fixture.bank_path.read_bytes() == original_bank
    assert _rows(candidate, "occurrences") == prior_occurrences
    assert set(prior_sources) <= set(_rows(candidate, "sources"))
    with closing(sqlite3.connect(candidate)) as connection, connection:
        info = dict(connection.execute("SELECT key,value FROM archive_info"))
        assert info["schema_version"] == "4" and info["code_version"] == bank.VERSION
        current = connection.execute("SELECT * FROM messages").fetchone()
        assert current[:6] == prior_messages[0][:6]
        assert current[7:11] == prior_messages[0][7:11]
        assert current[6] == new["messages"][0]["text"]
        archived = json.loads(
            connection.execute("SELECT prior_row FROM projection_revisions").fetchone()[
                0
            ]
        )
        assert (
            tuple(
                archived[key]
                for key in [
                    row[1] for row in connection.execute("PRAGMA table_info(messages)")
                ]
            )
            == prior_messages[0]
        )
        bank._validate_bank(connection, fixture.history)
    bank.publish_mac_recovery_bank(fixture.root, candidate, receipt)
    result = WechatArchiveRepository(fixture.root).query(
        session=new["session_id"], persist_event_cache=False
    )
    assert any(row["text"] == new["messages"][0]["text"] for row in result["messages"])
    replay = fixtures._publish(fixture, packets=[new], name="replay-new")
    assert replay["reparsed"] == replay["added"] == 0
    stale = fixtures._publish(fixture, packets=[old], name="replay-old")
    assert stale["reparsed"] == stale["added"] == 0
    assert _rows(fixture.bank_path, "occurrences") == prior_occurrences
    assert len(_rows(fixture.bank_path, "projection_revisions")) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("content_sha256", "9" * 64),
        ("create_time", 1700000999),
        ("local_id", 100),
        ("real_sender_id", 2),
        ("sender_id", fixtures.OWNER),
    ],
)
def test_changed_native_identity_is_not_semantic_repair(bank_archive, field, value):
    fixture = bank_archive
    old, new = _packets(fixture, "quote")
    fixtures._publish(fixture, packets=[old], name="old-parser")
    before = fixture.bank_path.read_bytes()
    new["messages"][0][field] = value
    try:
        _candidate, receipt = fixtures._stage(
            fixture, packets=[new], name="wrong-native"
        )
    except ValueError:
        pass
    else:
        assert receipt["reparsed"] == 0 and receipt["conflicts"] > 0
    assert fixture.bank_path.read_bytes() == before


def test_forward_completion_cannot_change_a_retained_leaf(bank_archive):
    fixture = bank_archive
    old, new = _packets(fixture, "forward")
    fixtures._publish(fixture, packets=[old], name="old-parser")
    native = new["messages"][0]["native_text"]
    native["text_components"][-1]["text"] = "Changed existing source statement"
    native["text"] = native_components_text(native["text_components"])
    new["messages"][0]["text"] = native["text"]
    with pytest.raises(ValueError, match="revision history"):
        fixtures._stage(fixture, packets=[new], name="wrong-leaf")


@pytest.mark.parametrize(
    "defect", ["digest", "prior", "current", "truncate", "sequence", "source"]
)
def test_resealed_payload_cannot_hide_a_broken_revision_chain(bank_archive, defect):
    fixture = bank_archive
    old, new = _packets(fixture, "quote")
    fixtures._publish(fixture, packets=[old], name="old-parser")
    candidate, _receipt = fixtures._stage(fixture, packets=[new], name="repaired")
    with sqlite3.connect(candidate) as connection:
        if defect == "digest":
            connection.execute("UPDATE projection_revisions SET digest=?", ("0" * 64,))
        elif defect == "prior":
            connection.execute("UPDATE projection_revisions SET prior_row='{}'")
        elif defect == "current":
            connection.execute("UPDATE messages SET text='Changed current body'")
        elif defect == "truncate":
            connection.execute("DELETE FROM projection_revisions")
        elif defect == "sequence":
            connection.execute("UPDATE projection_revisions SET sequence=2")
        elif defect == "source":
            connection.execute(
                "UPDATE projection_revisions SET source_key=?", ("0" * 64,)
            )
        connection.execute(
            "UPDATE archive_info SET value=? WHERE key='payload_sha256'",
            (bank._rows_digest(connection),),
        )
        connection.commit()
        with pytest.raises((ValueError, KeyError)):
            bank._validate_bank(connection, fixture.history)


def test_revalidation_accepts_unchanged_historical_occurrence_signatures(bank_archive):
    fixture = bank_archive
    old, new = _packets(fixture, "quote")
    fixtures._publish(fixture, packets=[old], name="old-parser")
    fixtures._publish(fixture, packets=[new], name="new-parser")
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            "ATTACH DATABASE ? AS macos_recovery_bank", (str(fixture.bank_path),)
        )
        assert revalidation._retained_events(connection) == (1, 1)


def test_later_stage_preserves_revisions_and_cas_still_rejects_drift(bank_archive):
    fixture = bank_archive
    old, new = _packets(fixture, "forward")
    fixtures._publish(fixture, packets=[old], name="old-parser")
    fixtures._publish(fixture, packets=[new], name="new-parser")
    revisions = _rows(fixture.bank_path, "projection_revisions")
    candidate, receipt = fixtures._stage(
        fixture, packets=[fixture.direct], name="another-source"
    )
    assert _rows(candidate, "projection_revisions") == revisions
    fixture.history.touch()
    with pytest.raises(ValueError, match="Canonical inputs changed before reuse or publication"):
        bank.publish_mac_recovery_bank(fixture.root, candidate, receipt)


def test_revision_chains_are_bounded_and_cannot_be_silently_reset(bank_archive):
    fixture = bank_archive
    record = (
        "<recordinfo><datalist>"
        + "".join(
            f'<dataitem datatype="1"><datadesc>Literal {index}</datadesc></dataitem>'
            for index in range(35)
        )
        + "</datalist></recordinfo>"
    )
    raw = (
        "<msg><appmsg><type>19</type><title>Bounded source</title><recorditem>"
        + record
        + "</recorditem></appmsg></msg>"
    )
    full = deepcopy(fixture.group)
    full["messages"] = [fixtures._message(90, 49, raw=raw)]

    def partial(count):
        packet = deepcopy(full)
        native = packet["messages"][0]["native_text"]
        native["text_components"] = (
            native["text_components"][:count] + native["text_components"][-1:]
        )
        native["omitted_forwarded_items"] = 34 - count
        native["flags"] = [
            "forwarded_text_only",
            "forwarded_non_text_or_unreadable_items_omitted",
            "forwarded_record_partially_unreadable",
        ]
        native["text"] = native_components_text(native["text_components"])
        packet["messages"][0]["text"] = native["text"]
        return packet

    fixtures._publish(fixture, packets=[partial(0)], name="baseline")
    for index in range(1, repair.MAX_REVISIONS + 1):
        receipt = fixtures._publish(
            fixture, packets=[partial(index)], name=f"revision-{index}"
        )
        assert receipt["reparsed"] == 1
    before = fixture.bank_path.read_bytes()
    with pytest.raises(ValueError, match="bounded revision"):
        fixtures._stage(fixture, packets=[partial(33)], name="excess-revision")
    assert fixture.bank_path.read_bytes() == before
    assert len(_rows(fixture.bank_path, "projection_revisions")) == repair.MAX_REVISIONS


def test_deleting_one_history_cannot_hide_behind_another_valid_chain(bank_archive):
    fixture = bank_archive
    old, new = _packets(fixture, "quote")
    for packet in (old, new):
        another = deepcopy(packet["messages"][0])
        another.update(local_id=91, server_id="591")
        packet["messages"].append(another)
    fixtures._publish(fixture, packets=[old], name="old-parser")
    candidate, receipt = fixtures._stage(fixture, packets=[new], name="new-parser")
    assert receipt["reparsed"] == 2
    with sqlite3.connect(candidate) as connection:
        identifier = connection.execute(
            "SELECT message_id FROM projection_revisions LIMIT 1"
        ).fetchone()[0]
        connection.execute(
            "DELETE FROM projection_revisions WHERE message_id=?", (identifier,)
        )
        connection.execute(
            "UPDATE archive_info SET value=? WHERE key='payload_sha256'",
            (bank._rows_digest(connection),),
        )
        with pytest.raises(ValueError, match="lost its revision history"):
            bank._validate_bank(connection, fixture.history)


def test_neutral_pat_requires_the_exact_known_profile_and_chat(bank_archive):
    fixture = bank_archive
    packet = deepcopy(fixture.group)
    native = {
        "text": "Original actor tickled Original target",
        "text_extraction_kind": "system_text",
        "original_message_type": 49,
        "appmsg_subtype": 62,
        "flags": ["system_notice"],
        "system_message_subtype": "pat",
        "system_source_ids": {
            "patinfo/fromusername": "wxid_synthetic_actor",
            "patinfo/chatusername": fixtures.ROOM,
            "patinfo/pattedusername": "wxid_synthetic_target",
        },
    }
    message = {
        "local_id": 92,
        "local_type": (62 << 32) | 49,
        "server_id": "592",
        "create_time": 1700000092,
        "content_sha256": "8" * 64,
        "real_sender_id": 18,
        "sender_id": "",
        "sender_label": "System Message",
        "direction": "unknown",
        "text": native["text"],
        "native_text": native,
    }
    packet["messages"] = [message]
    receipt = fixtures._publish(fixture, packets=[packet], name="pat")
    assert receipt["added"] == 1 and receipt["reparsed"] == 0
    for defect in ("chat", "subtype", "flags"):
        malformed = deepcopy(packet)
        if defect == "chat":
            malformed["messages"][0]["native_text"]["system_source_ids"][
                "patinfo/chatusername"
            ] = "999@chatroom"
        elif defect == "subtype":
            malformed["messages"][0]["native_text"]["appmsg_subtype"] = 57
        else:
            malformed["messages"][0]["native_text"]["flags"].append("unverified_role")
        with pytest.raises(ValueError):
            bank._validate_packet(malformed, (fixtures.OWNER,))


def test_known_pat_can_replace_only_the_same_raw_inert_app_role(bank_archive):
    fixture = bank_archive
    old = deepcopy(fixture.group)
    message = {
        "local_id": 93,
        "local_type": (62 << 32) | 49,
        "server_id": "593",
        "create_time": 1700000093,
        "content_sha256": "7" * 64,
        "real_sender_id": 18,
        "sender_id": "wxid_synthetic_internal",
        "sender_label": "wxid_synthetic_internal",
        "direction": "received",
        "sender_proof": fixtures._proof(18, "wxid_synthetic_internal"),
        "text": "[App message]",
        "resource": {
            "kind": "app",
            "label": "[App message]",
            "native_time": 1700000093,
            "app_subtype": "62",
        },
    }
    old["messages"] = [message]
    new = deepcopy(old)
    repaired = new["messages"][0]
    repaired.pop("resource")
    repaired.pop("sender_proof")
    repaired.update(
        sender_id="",
        sender_label="System Message",
        direction="unknown",
        text="Native actor tickled native target",
    )
    repaired["native_text"] = {
        "text": repaired["text"],
        "text_extraction_kind": "system_text",
        "original_message_type": 49,
        "appmsg_subtype": 62,
        "flags": ["system_notice"],
        "system_message_subtype": "pat",
        "system_source_ids": {
            "patinfo/fromusername": "wxid_synthetic_actor",
            "patinfo/chatusername": fixtures.ROOM,
            "patinfo/pattedusername": "wxid_synthetic_target",
        },
    }
    fixtures._publish(fixture, packets=[old], name="old-pat-resource")
    occurrences = _rows(fixture.bank_path, "occurrences")
    result = fixtures._publish(fixture, packets=[new], name="new-pat-system")
    assert result["reparsed"] == 1 and result["added"] == 0
    assert _rows(fixture.bank_path, "occurrences") == occurrences


def test_another_sources_improved_replay_preserves_its_own_historical_occurrence(
    bank_archive,
):
    fixture = bank_archive
    old_a, new_a = _packets(fixture, "quote")
    old_b, new_b = deepcopy(old_a), deepcopy(new_a)
    for packet in (old_b, new_b):
        packet["source"]["database_relative_path"] = "db_storage/message/message_9.db"
        packet["source"]["snapshot_id"] = "independent-capture"
        packet["source"]["main_sha256"] = "3" * 64
        packet["messages"][0]["local_id"] = 102
        packet["messages"][0]["content_sha256"] = "4" * 64
    fixtures._publish(fixture, packets=[old_a, old_b], name="two-old-observations")
    occurrences = _rows(fixture.bank_path, "occurrences")
    fixtures._publish(fixture, packets=[new_a], name="repaired-canonical")
    current = _rows(fixture.bank_path, "messages")
    receipt = fixtures._publish(fixture, packets=[new_b], name="other-improved-replay")
    assert receipt["existing"] == 1 and receipt["reparsed"] == receipt["added"] == 0
    assert _rows(fixture.bank_path, "messages") == current
    assert _rows(fixture.bank_path, "occurrences") == occurrences
    assert len(_rows(fixture.bank_path, "projection_revisions")) == 1
    forged = deepcopy(new_b)
    forged["messages"][0]["content_sha256"] = "5" * 64
    with pytest.raises(ValueError):
        fixtures._stage(fixture, packets=[forged], name="other-changed-raw")


def test_forward_repair_cannot_reverse_preserved_source_order(bank_archive):
    fixture = bank_archive
    old, new = _packets(fixture, "forward")
    native = new["messages"][0]["native_text"]
    retained = deepcopy(native["text_components"][-1])
    retained["text"] = "Third retained source item"
    retained["forwarded_item_path"] = [3]
    native["text_components"].append(retained)
    old_native = old["messages"][0]["native_text"]
    old_native["text_components"].append(deepcopy(retained))
    old_native["text"] = native_components_text(old_native["text_components"])
    old["messages"][0]["text"] = old_native["text"]
    # Reverse already observed paths 2 and 3 while adding the new nested path 1.
    native["text_components"] = [
        native["text_components"][2],
        *native["text_components"][:2],
    ]
    native["text"] = native_components_text(native["text_components"])
    new["messages"][0]["text"] = native["text"]
    fixtures._publish(fixture, packets=[old], name="ordered-old")
    with pytest.raises(ValueError, match="revision history"):
        fixtures._stage(fixture, packets=[new], name="reversed-new")
