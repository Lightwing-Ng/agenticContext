"""Exercise capture selection and exact native app evidence.

Code version: v1.1.1-codex.0
"""

from collections import Counter
from contextlib import closing
import hashlib
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import wechat_mac_refresh_projection as projection
from app.core import wechat_secondary_completions as completions
from app.core.owner_only_files import ensure_owner_only_directory, owner_only_problem
from app.core.wechat_completion_json_codec import encode_json_cell, load_json_cell
from scripts import capture_wechat_resource_details as capture
from scripts import reparse_wechat_completion_app_cards as reparse


_SESSION = "wechat:" + "a" * 64
_OTHER_SESSION = "wechat:" + "b" * 64
_EXCLUDED_SESSION = "wechat:" + "c" * 64
_OWNER = "wxid_capture_owner"
_OTHER_OWNER = "wxid_other_owner"
_SENDER = "wxid_capture_sender"
_TYPE = (51 << 32) | 49


def _add_completion(
    connection, ordinal, *, session=_SESSION, resource=None, clock_skew=0,
    local_type=_TYPE, operation="replace", strategy="native_sender",
):
    identifier = f"{ordinal:064x}"
    provenance = encode_json_cell(json.dumps({
        "resource_reference": {"kind": "app"} if resource is None else resource,
        "secondary_resource_completion": {
            "strategy": "native_sender",
            "clock_skew_seconds": clock_skew,
            "primary": {"message_type": 49},
            "secondary_source": {
                "account_id": _OWNER,
                "local_type": local_type,
                "sender_id": _SENDER,
                "source": {
                    "account_directory": _OWNER + "_native",
                    "database_relative_path": "db_storage/message/message_5.db",
                },
            },
        },
    }))
    assert isinstance(provenance, bytes)
    connection.execute(
        "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (identifier, session, ordinal, _SENDER, "", "received", "App message",
         1.0, "", "", "archive", provenance, "[]", ""),
    )
    connection.execute(
        "INSERT INTO entries(id,conversation_id,operation,strategy,conversation_row,"
        "source_key,witness_key,projected_sha256) VALUES(?,?,?,?,?,?,?,?)",
        (identifier, session, operation, strategy, "{}", "source", "witness", "projection"),
    )
    return identifier


def test_inventory_filters_retained_app_evidence_by_target_and_strategy():
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.row_factory = sqlite3.Row
        connection.executescript(completions._SCHEMA)
        wanted = _add_completion(connection, 1)
        retained = _add_completion(connection, 2)
        quarantined = _add_completion(connection, 3)
        _add_completion(connection, 4, resource={"kind": "app", "safe_title": "Existing"})
        _add_completion(connection, 5, resource={"kind": "image"})
        _add_completion(connection, 6, local_type=43)
        _add_completion(connection, 7, clock_skew=1)
        _add_completion(connection, 8, session=_OTHER_SESSION)
        _add_completion(connection, 9, strategy="native_quote")
        _add_completion(connection, 10, operation="append")
        connection.execute(
            "INSERT INTO ambiguities VALUES(?,?,?)", (quarantined, _SESSION, "conflict"),
        )
        counts = Counter()
        selected, sources = capture._inventory(
            connection, {retained}, session=_SESSION, after="",
            identifiers=[f"{i:064x}" for i in range(1, 12)], limit=1, counts=counts,
        )
    assert [candidate.row["id"] for candidate in selected] == [wanted]
    assert counts == {
        "retained_replacements": 7, "candidates": 1, "already_repaired": 1,
        "quarantined": 1, "already_labeled": 1, "other_resource": 1,
        "unsupported_witness": 2, "requested_ids": 11, "requested_not_eligible": 10,
    }
    assert sources == [{
        "account_id": _OWNER, "account_directory": _OWNER + "_native",
        "database_relative_path": "db_storage/message/message_5.db", "candidates": 1,
    }]


@pytest.mark.skipif(os.open not in os.supports_dir_fd, reason="requires POSIX directory handles")
def test_cli_resumes_after_last_attempt_and_keeps_the_lookahead(tmp_path, monkeypatch, capsys):
    history = capture.WechatArchiveRepository(tmp_path).path
    history.parent.mkdir(parents=True)
    for path in (history, history.with_name("resource_references.sqlite3")):
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE fixture(id INTEGER)")
    with closing(sqlite3.connect(history)) as connection:
        connection.execute("CREATE TABLE conversations(id TEXT, account_key TEXT, kind TEXT)")
        connection.executemany("INSERT INTO conversations VALUES(?,?,?)", [
            (_SESSION, "account:" + _OWNER, "group"),
            (_OTHER_SESSION, "account:" + _OTHER_OWNER, "group"),
            (_EXCLUDED_SESSION, "account:wxid_excluded_owner", "group"),
        ])
        connection.commit()
    with closing(sqlite3.connect(history.with_name(completions.FILENAME))) as connection:
        connection.executescript(completions._SCHEMA)
        last = _add_completion(connection, 40)
        first = _add_completion(connection, 10)
        second = _add_completion(connection, 30, session=_OTHER_SESSION)
        _add_completion(connection, 5, session=_EXCLUDED_SESSION)
        _add_completion(connection, 20, resource={"kind": "video"})
        connection.executemany("INSERT INTO archive_info VALUES(?,?)", {
            "schema_version": "1", "version": completions.VERSION,
            "input_identities": json.dumps(completions._pins(history)),
        }.items())
        connection.commit()

    workstation = (tmp_path / "private").resolve()
    workstation.mkdir(mode=0o700)
    ensure_owner_only_directory(workstation)
    monkeypatch.setattr(capture, "WORKSTATIONS", workstation)
    monkeypatch.setattr(capture, "_inputs", lambda repository: {})
    monkeypatch.setattr(
        capture, "_active_targets",
        lambda repository, derived, selected, counts: selected,
    )

    def simulated_capture(local_store, history_connection, selected, counts):
        return [], [{"message_id": candidate.row["id"]} for candidate in selected]

    monkeypatch.setattr(capture, "_capture", simulated_capture)
    arguments = [
        "--local-store", str(tmp_path), "--target-account", _OWNER,
        "--target-account", _OTHER_OWNER, "--limit", "2",
    ]
    first_output = workstation / "first.json"
    assert capture.main([*arguments, "--output", str(first_output)]) == 0
    assert owner_only_problem(first_output) == ""
    report = json.loads(capsys.readouterr().out)
    assert report["target_accounts"] == sorted([_OWNER, _OTHER_OWNER])
    assert report["next_after_message_id"] == second
    assert json.loads(first_output.read_text())["manifest"] == [
        {"message_id": first}, {"message_id": second},
    ]

    second_output = workstation / "second.json"
    assert capture.main([
        *arguments, "--after-message-id", report["next_after_message_id"],
        "--output", str(second_output),
    ]) == 0
    assert "next_after_message_id" not in json.loads(capsys.readouterr().out)
    assert json.loads(second_output.read_text())["manifest"] == [{"message_id": last}]


@pytest.fixture
def native_app():
    scope = projection.ConversationScope(_SESSION, _OWNER, "12345@chatroom", "group")
    table = projection._table(scope.peer_id)
    sender = {"rowid": 8, "user_name": _SENDER, "is_session": 0}
    body = (
        f"{_SENDER}:\n<msg><appmsg><type>51</type><finderFeed>"
        "<nickname>Card author</nickname><desc>Native video caption</desc>"
        "</finderFeed></appmsg></msg>"
    )
    witness = {
        "local_id": 847, "local_type": _TYPE, "server_id": "971234",
        "create_time": 1700000011, "real_sender_id": 8,
        "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "sender_id": _SENDER, "sender_proof": projection._name_proof(sender),
        "direction": "received",
    }
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(f"""
            CREATE TABLE {table} (
                local_id INTEGER PRIMARY KEY, local_type INTEGER, server_id INTEGER,
                create_time INTEGER, real_sender_id INTEGER, message_content TEXT,
                WCDB_CT_message_content INTEGER
            )
        """)
        connection.execute(
            f"INSERT INTO {table} VALUES(?,?,?,?,?,?,?)",
            (847, _TYPE, 971234, 1700000011, 8, body, 0),
        )
        context = SimpleNamespace(
            database=SimpleNamespace(connection=connection), names={8: sender},
        )
        yield context, scope, witness


def test_capture_preserves_native_labels_and_rejects_retained_label_conflicts(native_app):
    context, scope, witness = native_app
    counts = Counter()
    message = capture._native_message(context, scope, witness, {"kind": "app"}, counts)
    assert message["resource"]["channels_description"] == "Native video caption"
    assert message["resource"]["channels_nickname"] == "Card author"
    assert message["content_sha256"] == witness["content_sha256"]
    assert not counts

    assert capture._native_message(context, scope, witness, {
        "kind": "app", "channels_description": "Different retained description",
    }, counts) is None
    assert counts == {"native_label_conflict": 1}


@pytest.mark.parametrize(("column", "replacement"), [
    ("local_type", (5 << 32) | 49),
    ("server_id", 971235),
    ("create_time", 1700000012),
    ("real_sender_id", 9),
    ("message_content", "Different raw text at the same coordinates"),
])
def test_capture_refuses_any_native_coordinate_or_body_change(native_app, column, replacement):
    context, scope, witness = native_app
    table = projection._table(scope.peer_id)
    context.database.connection.execute(
        f"UPDATE {table} SET {column}=? WHERE local_id=?", (replacement, witness["local_id"]),
    )
    counts = Counter()
    assert capture._native_message(context, scope, witness, {"kind": "app"}, counts) is None
    assert counts == {"native_coordinate_conflict": 1}


def test_completion_reparse_skips_witnesses_also_used_by_another_account(monkeypatch):
    peer = "12345@chatroom"
    source = {
        "account_directory": _OWNER + "_native",
        "database_relative_path": "db_storage/message/message_0.db",
        "table": projection._table(peer),
    }
    header = dict(
        source=source, peer_id=peer, session_id=_SESSION,
        account_id=_OWNER, conversation_kind="group", account_binding={},
    )
    source_key = completions._sha(header)
    monkeypatch.setattr(capture.bank, "_validate_account_binding", lambda *_: None)
    with (
        closing(sqlite3.connect(":memory:")) as history,
        closing(sqlite3.connect(":memory:")) as catalog,
    ):
        history.row_factory = catalog.row_factory = sqlite3.Row
        history.execute("CREATE TABLE conversations(id TEXT,account_key TEXT,kind TEXT,peer_id TEXT)")
        history.executemany("INSERT INTO conversations VALUES(?,?,?,?)", [
            (_SESSION, "account:" + _OWNER, "group", peer),
            (_OTHER_SESSION, "account:" + _OTHER_OWNER, "group", peer),
        ])
        catalog.executescript(completions._SCHEMA)
        catalog.execute("INSERT INTO sources VALUES(?,?,?)", (
            source_key, completions._json(header), source_key,
        ))
        witness_keys = []
        for ordinal in (1, 2, 3):
            witness = {
                "local_id": ordinal, "local_type": _TYPE, "content_sha256": str(ordinal) * 64,
                "resource": {"kind": "app"},
            }
            witness_key = completions._sha([source_key, ordinal, witness["content_sha256"]])
            catalog.execute("INSERT INTO witnesses VALUES(?,?,?,?)", (
                witness_key, source_key, completions._json(witness), completions._sha(witness),
            ))
            witness_keys.append(witness_key)
        for ordinal, session, witness_key in (
            (1, _SESSION, witness_keys[0]),
            (2, _SESSION, witness_keys[1]),
            (3, _OTHER_SESSION, witness_keys[1]),
            (4, _OTHER_SESSION, witness_keys[2]),
        ):
            identifier = _add_completion(catalog, ordinal, session=session)
            catalog.execute("UPDATE messages SET text='[App message]' WHERE id=?", (identifier,))
            catalog.execute("UPDATE entries SET source_key=?,witness_key=? WHERE id=?", (
                source_key, witness_key, identifier,
            ))
        counts = Counter()
        selected = reparse._target_witnesses(catalog, history, {_OWNER}, counts)
        assert [target[0] for targets in selected.values() for target in targets] == [witness_keys[0]]
        assert counts["shared_or_quarantined_witnesses"] == 1


def test_completion_reparse_appends_only_native_labels(native_app, monkeypatch):
    context, scope, witness = native_app
    witness = {**witness, "resource": {"kind": "app", "app_subtype": "51"}}
    digest = completions._sha(witness)
    directory = scope.account_id + "_native"
    path = "db_storage/message/message_0.db"
    reference = SimpleNamespace(account_directory=directory, relative_path=path)
    database = SimpleNamespace(close=lambda: None)
    context.require_current = lambda *_: None
    monkeypatch.setattr(reparse.native, "KeyRegistry", lambda: SimpleNamespace(
        references=lambda *_args, **_kwargs: [reference],
    ))
    monkeypatch.setattr(reparse.native, "read_database", lambda _: database)
    monkeypatch.setattr(reparse.projection, "prepare_source_context", lambda *_: context)
    with closing(sqlite3.connect(":memory:")) as catalog:
        catalog.executescript(completions._SCHEMA)
        catalog.execute("INSERT INTO witnesses VALUES(?,?,?,?)", (
            "witness", "source", completions._json(witness), digest,
        ))
        counts = Counter()
        targets = {(_OWNER, directory, path): [
            ("witness", digest, scope.session_id, scope.peer_id, witness),
        ]}
        reparse._enrich_witnesses(catalog, targets, counts)
        cell, current_digest = catalog.execute("SELECT message,sha256 FROM witnesses").fetchone()
        current = load_json_cell(cell)
        resource = current.pop("resource")
        assert current == {key: value for key, value in witness.items() if key != "resource"}
        assert resource == {
            "kind": "app", "app_subtype": "51", "app_metadata_version": "1",
            "channels_description": "Native video caption", "channels_nickname": "Card author",
        }
        assert current_digest == completions._sha({**current, "resource": resource})
        assert counts["enriched_witnesses"] == 1
