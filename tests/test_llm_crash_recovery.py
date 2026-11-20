"""Verify real process death across SQLite history transaction boundaries.

Code version: v1.0.0-codex.0
"""

from contextlib import closing
import hashlib
import multiprocessing
from pathlib import Path
import sqlite3
import traceback

import pyarrow as pa
import pytest

from app.core.llm_sqlite import read_sqlite_rows, read_sqlite_schema, write_sqlite_rows


pytestmark = pytest.mark.slow

SCHEMA = pa.schema([
    ("conversation_id", pa.string()),
    ("message_key", pa.string()),
    ("content_text", pa.string()),
], metadata={b"provenance": b"synthetic crash recovery"})


def _rows(revision):
    return [
        {
            "conversation_id": conversation,
            "message_key": str(index),
            "content_text": hashlib.shake_256(
                f"{revision}:{index}".encode()
            ).hexdigest(16_384),
        }
        for index, conversation in enumerate(["a", "b", "a", "c"])
    ]


def _write_until_killed(path, boundary, notifications, release):
    from app.core import llm_sqlite

    try:
        if boundary == "transaction":
            original_connect = llm_sqlite._connect

            def connected(*arguments, **options):
                connection = original_connect(*arguments, **options)
                if not options.get("readonly"):
                    connection.execute("PRAGMA cache_size=1")

                    def after_update():
                        notifications.send("transaction")
                        release.wait()
                        return 0

                    connection.create_function("crash_boundary", 0, after_update)
                return connection

            # Instrument a real SQL trigger; the parent kills the actual writer.
            llm_sqlite._connect = connected
        write_sqlite_rows(Path(path), _rows("new"), SCHEMA)
        notifications.send("committed")
        release.wait()
    except BaseException:
        notifications.send(traceback.format_exc())
        raise
    finally:
        notifications.close()


@pytest.mark.parametrize("boundary", ["transaction", "committed"])
def test_killed_writer_reopens_as_one_complete_snapshot(tmp_path, boundary):
    path = tmp_path / "data.sqlite3"
    original = _rows("old")
    write_sqlite_rows(path, original, SCHEMA)
    if boundary == "transaction":
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(
                "CREATE TRIGGER crash_after_update AFTER UPDATE ON row_groups "
                "BEGIN SELECT crash_boundary(); END"
            )
            connection.commit()
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    release = context.Event()
    writer = context.Process(
        target=_write_until_killed,
        args=(str(path), boundary, sender, release),
    )
    writer.start()
    sender.close()
    try:
        assert receiver.poll(30), "The writer did not reach its SQL boundary."
        assert receiver.recv() == boundary
        if boundary == "transaction":
            journal = Path(str(path) + "-journal")
            assert journal.is_file() and journal.stat().st_size > 512
        writer.kill()
        writer.join(10)
        assert not writer.is_alive() and writer.exitcode != 0
    finally:
        if writer.is_alive():
            writer.kill()
            writer.join(10)
        receiver.close()
        writer.close()

    # Opening a fresh connection must recover a hot journal without our rollback code.
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    expected = original if boundary == "transaction" else _rows("new")
    assert read_sqlite_rows(path) == expected
    assert read_sqlite_schema(path).equals(SCHEMA, check_metadata=True)
    assert not Path(str(path) + "-journal").exists()
