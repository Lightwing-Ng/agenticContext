"""Copy already validated active completions into a bounded, encoded private catalog.

Code version: v1.0.1-codex.0
The caller owns source validation, the exact upstream-active selection, and final
proof/CAS validation. This helper never modifies an attached source database.
"""

from collections import Counter
from pathlib import Path
import os
import sqlite3
import stat
import tempfile

from .wechat_completion_json_codec import decode_json_cell, encode_json_cell
from . import owner_only_files

CODE_VERSION = "v1.0.1-codex.0"
_TABLES = {
    "messages",
    "entries",
    "sources",
    "witnesses",
    "ambiguities",
    "archive_info",
    "append_event_heads",
    "append_event_audit",
    "incremental_resource_conflicts",
}
_OPTIONAL = {
    "append_event_heads",
    "append_event_audit",
    "incremental_resource_conflicts",
}
_JSON_COLUMNS = {
    "messages": {"provenance"},
    "entries": {"original_row", "conversation_row", "plan"},
    "sources": {"header"},
    "witnesses": {"message"},
}


def stream_file_owned(owner, path):
    """Require the exact newly created private inode before any path operation."""
    try:
        value = Path(path).lstat()
    except FileNotFoundError:
        return False
    return (
        stat.S_ISREG(value.st_mode) and (value.st_dev, value.st_ino) == owner["inode"]
    )


def stream_directory_owned(owner):
    try:
        current = owner["directory"].lstat()
    except FileNotFoundError:
        return False
    return (
        stat.S_ISDIR(current.st_mode)
        and (current.st_dev, current.st_ino) == owner["directory_inode"]
    )


def sync_stream_file(owner):
    """Flush only the anchored writable inode, including on Windows."""
    descriptor = os.open(owner["path"], os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        value = os.fstat(descriptor)
        if (value.st_dev, value.st_ino) != owner["inode"]:
            raise ValueError("The private streamed inode changed before flushing.")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def reset_stream_marker(owner):
    """Keep failed output incomplete without touching a replaced public name."""
    if not stream_directory_owned(owner) or not stream_file_owned(owner, owner["path"]):
        return
    connection = sqlite3.connect(owner["path"].as_uri() + "?mode=rw", uri=True)
    try:
        if not stream_file_owned(owner, owner["path"]):
            raise ValueError(
                "The private streamed inode changed before its failure marker."
            )
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive_info'"
        ).fetchone():
            connection.execute(
                "INSERT OR REPLACE INTO archive_info VALUES('staging_state','in_progress')"
            )
            connection.commit()
    finally:
        connection.close()


def create_stream_output(destination):
    """Reserve an absent output name atomically; write through a private hard link."""
    destination = Path(destination)
    directory = Path(
        tempfile.mkdtemp(prefix=".completion-stream-", dir=destination.parent)
    )
    ensure_directory = getattr(owner_only_files, "ensure_owner_only_directory", None)
    if ensure_directory is not None:
        ensure_directory(directory)
    elif os.name == "nt":
        raise ValueError(
            "Stream compaction requires the Windows owner-only directory boundary."
        )
    else:
        directory.chmod(0o700)
    directory_identity = directory.stat()
    private = directory / "catalog.sqlite3"
    descriptor = os.open(
        private,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    state = os.fstat(descriptor)
    owner = {
        "path": private,
        "directory": directory,
        "inode": (state.st_dev, state.st_ino),
        "directory_inode": (directory_identity.st_dev, directory_identity.st_ino),
    }
    output = None
    try:
        owner_only_files.ensure_owner_only(private)
        if not stream_file_owned(owner, private) or not stream_directory_owned(owner):
            raise ValueError(
                "The owner-only streamed workspace changed before reservation."
            )
        os.link(private, destination)
        if not stream_file_owned(owner, private):
            raise ValueError(
                "The private streamed inode changed before opening SQLite."
            )
        output = sqlite3.connect(private.as_uri() + "?mode=rw", uri=True)
        if not stream_file_owned(owner, private) or not stream_file_owned(
            owner, destination
        ):
            raise ValueError("The reserved streamed candidate changed before writing.")
        return output, owner
    except BaseException:
        if output is not None:
            output.close()
        os.close(descriptor)
        descriptor = None
        # The reserved public candidate, if created, remains reviewable; never remove a racer.
        if stream_directory_owned(owner) and stream_file_owned(owner, private):
            private.unlink()
            directory.rmdir()
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def release_stream_link(owner, destination):
    """Drop only our private extra link after the public inode is verified."""
    directory = owner["directory"]
    current = directory.lstat()
    if (
        (current.st_dev, current.st_ino) != owner["directory_inode"]
        or not stat.S_ISDIR(current.st_mode)
        or not stream_file_owned(owner, owner["path"])
        or not stream_file_owned(owner, destination)
    ):
        raise ValueError("The streamed candidate lost its reserved private inode.")
    owner["path"].unlink()
    directory.rmdir()


def encode_stream_values(row, columns, json_fields, counts):
    values = list(row)
    for index, column in enumerate(columns):
        if column not in json_fields:
            continue
        text = decode_json_cell(values[index])
        value = encode_json_cell(text)
        if decode_json_cell(value) != text:
            raise ValueError("A streamed JSON cell changed its canonical text.")
        values[index] = value
        if text is not None:
            counts["uncompressed_json_bytes"] += len(text.encode("utf-8"))
            counts["stored_json_bytes"] += (
                len(value) if isinstance(value, bytes) else len(value.encode("utf-8"))
            )
            counts["encoded_json_cells"] += int(isinstance(value, bytes))
    return values


def _copy_table(output, table, columns, predicate, counts, copied, notify):
    statement = (
        'SELECT s.* FROM completion_stream_source."'
        + table
        + '" s'
        + (" WHERE " + predicate if predicate else "")
        + " ORDER BY s.rowid"
    )
    insert = (
        'INSERT INTO "' + table + '" VALUES(' + ",".join("?" for _ in columns) + ")"
    )
    json_fields = _JSON_COLUMNS.get(table, set())
    for row in output.execute(statement):
        values = encode_stream_values(row, columns, json_fields, counts)
        output.execute(insert, values)
        copied[table] += 1
        if json_fields:
            counts["processed_json_rows"] += 1
        if copied[table] % 256 == 0:
            output.commit()
        if copied[table] % 10_000 == 0:
            notify(table=table, copied_rows=copied[table], **dict(counts))
    output.commit()
    notify(table=table, copied_rows=copied[table], **dict(counts))


def stream_retained_catalog(output, source, notify):
    """Encode during copy; retain exact row/proof bytes after canonical decoding."""
    marker = output.execute(
        "SELECT value FROM archive_info WHERE key='staging_state'"
    ).fetchone()
    if marker != ("in_progress",):
        raise ValueError(
            "Stream compaction requires an incomplete private destination."
        )
    if any(
        output.execute('SELECT 1 FROM "' + table + '" LIMIT 1').fetchone()
        for table in _TABLES - {"archive_info"}
    ):
        raise ValueError("Stream compaction requires empty destination data tables.")
    output.execute(
        "ATTACH DATABASE ? AS completion_stream_source",
        (Path(source).absolute().as_uri() + "?mode=ro&immutable=1",),
    )
    tables = {
        row[0]
        for row in output.execute(
            "SELECT name FROM completion_stream_source.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if not _TABLES - _OPTIONAL <= tables or tables - _TABLES:
        raise ValueError(
            "The streamed source has an unsupported derived-catalog schema."
        )
    layouts = {}
    for table in tables:
        source_layout = [
            tuple(row)[1:]
            for row in output.execute(
                'PRAGMA completion_stream_source.table_info("' + table + '")'
            )
        ]
        destination_layout = [
            tuple(row)[1:]
            for row in output.execute('PRAGMA main.table_info("' + table + '")')
        ]
        if source_layout != destination_layout:
            raise ValueError("The streamed source changed a derived table layout.")
        layouts[table] = [row[0] for row in source_layout]
    if output.execute(
        "SELECT 1 FROM completion_stream_source.sqlite_master WHERE type IN ('trigger','view') LIMIT 1"
    ).fetchone():
        raise ValueError(
            "The streamed source has unsupported executable schema objects."
        )
    counts, copied = Counter(), Counter()
    # The source's complete marker must never enter a committed partial output.
    output.execute("DELETE FROM archive_info WHERE key<>'staging_state'")
    _copy_table(
        output,
        "archive_info",
        layouts["archive_info"],
        "s.key<>'staging_state'",
        counts,
        copied,
        notify,
    )
    for table in (
        "ambiguities",
        "append_event_audit",
        "incremental_resource_conflicts",
    ):
        if table in tables:
            _copy_table(output, table, layouts[table], "", counts, copied, notify)
    for table in ("entries", "messages"):
        _copy_table(
            output,
            table,
            layouts[table],
            "EXISTS(SELECT 1 FROM retained_ids r WHERE r.id=s.id)",
            counts,
            copied,
            notify,
        )
    expected = output.execute("SELECT COUNT(*) FROM retained_ids").fetchone()[0]
    if copied["entries"] != expected or copied["messages"] != expected:
        raise ValueError(
            "The streamed active-entry selection lost a message or proof row."
        )
    if "append_event_heads" in tables:
        _copy_table(
            output,
            "append_event_heads",
            layouts["append_event_heads"],
            "s.state<>'active' OR EXISTS(SELECT 1 FROM entries e WHERE e.id=s.message_id)",
            counts,
            copied,
            notify,
        )
    _copy_table(
        output,
        "witnesses",
        layouts["witnesses"],
        "EXISTS(SELECT 1 FROM entries e WHERE e.witness_key=s.id)"
        " OR EXISTS(SELECT 1 FROM incremental_resource_conflicts a WHERE a.witness_key=s.id OR a.prior_witness_key=s.id)"
        " OR EXISTS(SELECT 1 FROM append_event_audit a WHERE a.kept_witness_key=s.id OR a.dropped_witness_key=s.id)",
        counts,
        copied,
        notify,
    )
    _copy_table(
        output,
        "sources",
        layouts["sources"],
        "EXISTS(SELECT 1 FROM entries e WHERE e.source_key=s.id)"
        " OR EXISTS(SELECT 1 FROM incremental_resource_conflicts a WHERE a.source_key=s.id OR a.prior_source_key=s.id)"
        " OR EXISTS(SELECT 1 FROM append_event_audit a WHERE a.kept_source_key=s.id OR a.dropped_source_key=s.id)",
        counts,
        copied,
        notify,
    )
    output.execute("DETACH DATABASE completion_stream_source")
    return {"json_cell_transport": dict(counts), "copied_rows": dict(copied)}
