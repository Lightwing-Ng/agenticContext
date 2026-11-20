"""Candidate-ID membership stays indexed through compound source timelines.

Code version: v1.0.1-codex.0
"""

import json
import sqlite3

import pytest

from app.core.wechat_archive import _file_identity, _identifier
from app.core.wechat_media_event_index import attach_media_event_index
from app.core.wechat_media_placeholders import attach_media_placeholders
from tests import test_wechat_media_event_global_timeline as media


global_media_store = media.global_media_store
placeholder_store = media.placeholder_store


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
def test_compound_timeline_membership_does_not_materialize_unrelated_archives(
    global_media_store, scoped, legacy
):
    fixture = global_media_store
    spec = fixture.specs[0]
    noise = "wechat:" + _identifier("Unrelated primary observations")
    scoped_resource_view = scoped and not legacy
    with sqlite3.connect(fixture.history) as primary:
        row = list(primary.execute("SELECT * FROM conversations LIMIT 1").fetchone())
        row[0], row[3] = noise, "wxid_unrelated_peer"
        primary.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", row
        )
        seed = list(primary.execute("SELECT * FROM messages LIMIT 1").fetchone())
        batch = []
        for number in range(100_000 if scoped_resource_view else 30_000):
            value = seed.copy()
            value[0], value[1], value[2] = f"unrelated-primary-{number}", noise, number
            batch.append(value)
        primary.executemany(
            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch
        )
        # The live primary uses a shared-body view. Preserve indexed storage
        # underneath an equivalent join so a whole-view scan is measurable.
        primary.execute("ALTER TABLE messages RENAME TO message_storage")
        primary.execute("CREATE TABLE message_body_ids(id TEXT PRIMARY KEY)")
        primary.execute("INSERT INTO message_body_ids SELECT id FROM message_storage")
        if scoped_resource_view:
            columns = [row[1] for row in primary.execute("PRAGMA table_info(message_storage)")]
            projected = ",".join(
                "CASE WHEN b.id IS NULL THEN m.text ELSE m.text END AS text"
                if column == "text" else "m." + column
                for column in columns
            )
            primary.execute(
                "CREATE VIEW messages AS SELECT " + projected
                + " FROM message_storage m LEFT JOIN message_body_ids b ON b.id=m.id"
            )
        else:
            primary.execute(
                "CREATE VIEW messages AS SELECT m.* FROM message_storage m JOIN message_body_ids b ON b.id=m.id"
            )
    with sqlite3.connect(fixture.index) as index:
        index.execute(
            "UPDATE archive_info SET value=? WHERE key='history_file_identity'",
            (json.dumps(_file_identity(fixture.history)),),
        )
    connection = sqlite3.connect(
        fixture.history.as_uri() + "?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA temp_store=MEMORY")
    try:
        # The resource index preserves the primary table's declared affinity.
        schema_source = "main.message_storage" if scoped_resource_view else "main.messages"
        connection.execute(
            f"CREATE TEMP TABLE native_input_messages AS SELECT * FROM {schema_source} WHERE 0"
        )
        connection.execute(
            "CREATE UNIQUE INDEX native_input_id ON native_input_messages(id)"
        )
        connection.execute(
            "CREATE INDEX native_input_scope ON native_input_messages(conversation_id,time_sort,ordinal)"
        )
        batch = []
        for number in range(20_000):
            value = seed.copy()
            value[0], value[1], value[2] = (
                f"unrelated-native-{number}", "outside-resource-scope", number
            )
            batch.append(value)
        # An input ID remains global even when its input conversation differs.
        # A conversation-equality shortcut would incorrectly add it a second time.
        overlap = list(spec.rows[0])
        overlap[1] = noise
        batch.append(overlap)
        connection.executemany(
            "INSERT INTO native_input_messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            batch,
        )
        native_scope = ""
        if scoped_resource_view:
            # The production resource view filters its second arm through a
            # single-row scope table and excludes IDs already in the primary.
            connection.execute("CREATE TEMP TABLE resource_scope(id TEXT PRIMARY KEY)")
            connection.execute("INSERT INTO resource_scope VALUES(?)", (noise,))
            native_scope = (
                " WHERE r.conversation_id=(SELECT id FROM resource_scope) "
                "AND NOT EXISTS(SELECT 1 FROM main.messages p WHERE p.id=r.id)"
            )
        connection.execute(
            "CREATE TEMP VIEW compound_input_messages AS SELECT * FROM main.messages "
            "UNION ALL SELECT r.* FROM native_input_messages r" + native_scope
        )
        tables = "main.conversations", "compound_input_messages"
        if legacy:
            *tables, status = attach_media_placeholders(
                connection,
                fixture.history,
                *tables,
                conversation_id=spec.conversation if scoped else None,
            )
            assert status == "ready"
        ticks = []
        connection.set_progress_handler(
            lambda: ticks.append(1) or int(len(ticks) > 600), 1000
        )
        conversations, messages, status = attach_media_event_index(
            connection,
            fixture.history,
            *tables,
            conversation_id=spec.conversation if scoped else None,
        )
        assert status == "ready"
        summary = connection.execute(
            f"SELECT message_count FROM {conversations} WHERE id=?",
            (spec.conversation,),
        ).fetchone()[0]
        actual = connection.execute(
            f"SELECT COUNT(*) FROM {messages} WHERE conversation_id=?",
            (spec.conversation,),
        ).fetchone()[0]
        assert summary == actual == 737
        rows = connection.execute(
            f"SELECT id FROM {messages} WHERE conversation_id=? ORDER BY time_sort,ordinal,id LIMIT 30",
            (spec.conversation,),
        ).fetchall()
        assert len(rows) == len({row[0] for row in rows}) == 30
        assert len(ticks) <= 600
    finally:
        connection.set_progress_handler(None, 0)
        connection.close()
