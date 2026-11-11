"""Bound private compaction probes to candidate IDs and scalar notice membership.

Code version: v1.0.0-codex.0
The validated upstream view remains authoritative. Candidate rows and duplicate
upstream observations retain their existing SQLite comparison semantics.
"""

from __future__ import annotations

from heapq import merge


ENTRY_BATCH_SIZE = 256
_BATCH = "temp.completion_selection_batch"
_ROWS = "temp.completion_selection_rows"
_MEMBERS = "temp.completion_selection_members"
_NOTICE_KEYS = "temp.completion_selection_notice_keys"
_ENTRY_COLUMNS = (
    "id", "conversation_id", "operation", "strategy", "original_sha256",
    "projected_sha256", "source_key", "witness_key",
)


class BoundedActiveSelection:
    """Use indexed, uncorrelated upstream input filters before exact predicates."""

    def __init__(self, connection, upstream):
        from . import wechat_secondary_completions as common

        if not isinstance(upstream, str) or common._SQL.fullmatch(upstream) is None:
            raise ValueError("Unsafe completion selection timeline.")
        self.connection = connection
        self.upstream = upstream
        self.common = common
        self.columns = ",".join('b."' + column + '"' for column in common.COLUMNS)
        connection.execute(
            "CREATE TEMP TABLE completion_selection_batch(id TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        connection.execute(
            "CREATE TEMP TABLE completion_selection_rows AS SELECT "
            + self.columns + " FROM " + upstream + " b WHERE 0"
        )
        # This index is deliberately nonunique: differing same-ID observations
        # must all remain eligible for the original existential comparison.
        connection.execute(
            "CREATE INDEX completion_selection_row_id ON completion_selection_rows(id)"
        )
        connection.execute(
            "CREATE TEMP TABLE completion_selection_members AS "
            "SELECT b.id,b.conversation_id,json_extract(b.provenance,'$.server_id') AS server_id "
            "FROM " + upstream + " b WHERE 0"
        )
        connection.execute(
            "CREATE INDEX completion_selection_member_id "
            "ON completion_selection_members(conversation_id,id)"
        )
        connection.execute(
            "CREATE INDEX completion_selection_member_server "
            "ON completion_selection_members(conversation_id,server_id)"
        )
        connection.execute(
            "CREATE TEMP TABLE completion_selection_notice_keys("
            "id TEXT,conversation_id TEXT,server_id)"
        )
        connection.execute(
            "CREATE INDEX completion_selection_notice_key "
            "ON completion_selection_notice_keys(conversation_id,server_id)"
        )

    def __enter__(self):
        return self

    def __exit__(self, *_exception):
        for table in (_NOTICE_KEYS, _MEMBERS, _ROWS, _BATCH):
            self.connection.execute("DROP TABLE " + table)

    def _replacement_query(self, count):
        return (
            "INSERT INTO " + _ROWS + " SELECT " + self.columns + " FROM "
            + self.upstream + " b WHERE b.id IN (" + ",".join("?" for _ in range(count)) + ")"
        )

    def _membership_query(self, count):
        return (
            "INSERT INTO " + _MEMBERS
            + " SELECT b.id,b.conversation_id,json_extract(b.provenance,'$.server_id') "
            "FROM " + self.upstream + " b WHERE b.conversation_id IN ("
            + ",".join("?" for _ in range(count)) + ") AND ("
            "b.id IN (SELECT id FROM " + _NOTICE_KEYS + ") OR EXISTS(SELECT 1 FROM "
            + _NOTICE_KEYS + " k WHERE k.conversation_id=b.conversation_id "
            "AND k.server_id=json_extract(b.provenance,'$.server_id')))"
        )

    def _scope(self, identifiers, entry_table=None):
        connection = self.connection
        for table in (_BATCH, _ROWS, _MEMBERS, _NOTICE_KEYS):
            connection.execute("DELETE FROM " + table)
        connection.executemany(
            "INSERT INTO " + _BATCH + " VALUES(?)", [(value,) for value in identifiers]
        )
        replacements = identifiers
        if entry_table is not None:
            replacements = [row[0] for row in connection.execute(
                "SELECT e.id FROM " + entry_table + " e WHERE e.operation='replace' "
                "AND e.id IN (SELECT id FROM " + _BATCH + ")"
            )]
        if replacements:
            # Bound parameters also push through older SQLite compound views
            # whose mixed expression affinities prevent an IN-subquery pushdown.
            connection.execute(
                self._replacement_query(len(replacements)), replacements
            )
        if entry_table is not None:
            connection.execute(
                "INSERT INTO " + _NOTICE_KEYS
                + " SELECT e.id,e.conversation_id,json_extract("
                "_wechat_completion_json_v1(e.plan),'$.event.server_id') FROM "
                + entry_table + " e WHERE e.operation='append' "
                "AND e.id IN (SELECT id FROM " + _BATCH + ")"
            )
            conversations = [row[0] for row in connection.execute(
                "SELECT DISTINCT conversation_id FROM " + _NOTICE_KEYS
            )]
            if conversations:
                connection.execute(self._membership_query(len(conversations)), conversations)

    def entries(self, entry_table, *, conversation_id=None):
        """Yield the existing ordered audit fields and exact active decision."""
        if entry_table not in {
            "main.entries", "completion_source.entries", "secondary_completions.entries"
        }:
            raise ValueError("Unsafe completion selection entries.")
        yield from merge(
            self._entries_by_operation(entry_table, "replace", conversation_id),
            self._entries_by_operation(entry_table, "append", conversation_id),
            key=lambda entry: entry["id"],
        )

    def _entries_by_operation(self, entry_table, operation, conversation_id):
        previous = None
        while True:
            query = "SELECT id FROM " + entry_table
            conditions, parameters = ["operation='" + operation + "'"], []
            if conversation_id is not None:
                conditions.append("conversation_id=?")
                parameters.append(conversation_id)
            if previous is not None:
                conditions.append("id>?")
                parameters.append(previous)
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            rows = self.connection.execute(
                query + " ORDER BY id LIMIT ?", (*parameters, ENTRY_BATCH_SIZE)
            ).fetchall()
            if not rows:
                return
            identifiers = [row[0] for row in rows]
            self._scope(identifiers, entry_table)
            fields = ",".join("e." + name for name in _ENTRY_COLUMNS)
            cursor = self.connection.execute(
                "SELECT " + fields + ",CASE WHEN "
                + self.common._active_predicate(_ROWS, membership_table=_MEMBERS)
                + " THEN 1 ELSE 0 END AS is_active FROM " + entry_table
                + " e WHERE e.id IN (SELECT id FROM " + _BATCH + ") ORDER BY e.id"
            )
            # Both operation iterators share the same bounded SQL workspace.
            # Materialize only this batch's small audit fields before merging;
            # the other iterator can then safely prepare its next batch.
            for row in cursor.fetchall():
                yield dict(zip((*_ENTRY_COLUMNS, "is_active"), row, strict=True))
            previous = identifiers[-1]

    def original_is_active(self, original):
        """Check a replayed prior row without rebuilding a global upstream index."""
        self._scope([original["id"]])
        return self.connection.execute(
            "SELECT " + self.common._active_predicate(_ROWS, membership_table=_MEMBERS)
            + " FROM (SELECT ? AS id,? AS conversation_id,'replace' AS operation,"
            "? AS original_row,NULL AS plan) e",
            (original["id"], original["conversation_id"], self.common._json(original)),
        ).fetchone()[0]
