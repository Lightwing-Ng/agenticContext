"""Keep immutable local member observations separate from membership causes.

Code version: v1.5.1-codex.0
"""

from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta, timezone
import hashlib
import heapq
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from .wechat_contacts import classify_wechat_identifier


MAX_MEMBERS = 10_000
MAX_SNAPSHOT_BYTES = 4 * 1024**2
_MAX_HISTORY_EVENTS = 1_000
_LEGACY_ADAPTER = "v1.0.0-codex.0"
_ORDERED_ADAPTER = "v1.1.0-codex.0"
_HEX = re.compile(r"[0-9a-f]{64}")
_ACCOUNT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,151}")
_ZONE = timezone(timedelta(hours=8))
_NOTE = (
    "Snapshots describe the local WeChat member list at the observation time. "
    "Server synchronization is not independently verified. "
    "A missing member does not establish why or exactly when they left."
)
_SCHEMA = """
CREATE TABLE roster_info(version INTEGER NOT NULL CHECK(version=1));
INSERT INTO roster_info VALUES(1);
CREATE TABLE snapshots(
 id TEXT PRIMARY KEY,account_id TEXT NOT NULL,peer_id TEXT NOT NULL,
 observed_at TEXT NOT NULL,stream_id TEXT NOT NULL,payload TEXT NOT NULL,
 UNIQUE(account_id,peer_id,stream_id,observed_at)
) WITHOUT ROWID;
CREATE INDEX roster_scope ON snapshots(account_id,peer_id,observed_at DESC,id);
"""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError("Member snapshots need an explicit observation time.")
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None or parsed.year < 2000:
        raise ValueError("Member snapshot times need a UTC offset.")
    return parsed.astimezone(UTC).isoformat(timespec="microseconds")


def _scope(account, peer):
    if (
        not isinstance(account, str) or _ACCOUNT.fullmatch(account) is None
        or classify_wechat_identifier(peer)["kind"] != "group"
    ):
        raise ValueError("Member snapshots need an exact account and group.")


def _snapshot(value):
    """Normalize only complete, nonempty, source-bound local roster captures."""
    if not isinstance(value, dict) or value.get("complete") is not True:
        raise ValueError("Incomplete member observations cannot replace a baseline.")
    account, peer = value.get("account_id"), value.get("peer_id")
    _scope(account, peer)
    stamp = _stamp(value.get("observed_at"))
    members, source = value.get("members"), value.get("source")
    if (
        not isinstance(members, (tuple, list))
        or not 1 <= len(members) <= MAX_MEMBERS
        or type(value.get("member_count")) is not int
        or value["member_count"] != len(members)
        or not isinstance(source, dict)
    ):
        raise ValueError("A member snapshot needs a complete bounded member list.")
    directory = source.get("account_directory")
    adapter = source.get("adapter_version")
    if (
        not isinstance(directory, str)
        or re.fullmatch(r"[A-Za-z0-9_.-]{1,256}", directory) is None
        or not (directory == account or directory.startswith(account + "_"))
        or source.get("database_relative_path") != "db_storage/contact/contact.db"
        or source.get("table") != "chat_room"
        or adapter not in (_LEGACY_ADAPTER, _ORDERED_ADAPTER)
        or source.get("integrity_check") != "ok"
        or source.get("authenticated_pages") is not True
        or any(
            not isinstance(source.get(key), str)
            or _HEX.fullmatch(source[key]) is None
            for key in ("main_sha256", "wal_sha256")
        )
    ):
        raise ValueError("Member snapshots need authenticated contact-source evidence.")
    if source.get("wal_tail_status") not in {
        "none", "uncommitted_frames", "zero_preallocation", "stale_salt_tail",
    }:
        raise ValueError("An incomplete WAL tail cannot establish member absence.")
    normalized, seen = [], set()
    for member in members:
        if not isinstance(member, dict):
            raise ValueError("A member needs a stable native username.")
        username, label = member.get("username"), member.get("display_name")
        if (
            not isinstance(username, str) or not username
            or len(username.encode()) > 1024
            or any(character.isspace() or ord(character) < 32 for character in username)
            or username in seen
            or classify_wechat_identifier(username)["kind"] in {"group", "unresolved"}
            or not isinstance(label, str) or "\0" in label
            or len(label.encode()) > 4096
        ):
            raise ValueError("Member identities must be exact, unique and bounded.")
        seen.add(username)
        normalized.append({"username": username, "display_name": label})
    if account not in seen:
        raise ValueError("A complete local group roster must include its source account.")
    membership_sha = hashlib.sha256(_json(sorted(seen)).encode()).hexdigest()
    if (
        source.get("member_set_cross_checked") is not True
        or source.get("completeness_scope") != "committed_local_roster"
        or type(source.get("roster_count")) is not int
        or source["roster_count"] != len(normalized)
        or source.get("owner_id") not in seen
        or source.get("membership_sha256") != membership_sha
        or not isinstance(source.get("room_ext_sha256"), str)
        or _HEX.fullmatch(source["room_ext_sha256"]) is None
        or source.get("wal_tail_complete") is not (
            source["wal_tail_status"] in {"none", "uncommitted_frames"}
        )
    ):
        raise ValueError("A complete roster needs matching member-set and committed-source proofs.")
    if adapter == _ORDERED_ADAPTER:
        order = [member["username"] for member in normalized]
        if source.get("member_order_sha256") != hashlib.sha256(_json(order).encode()).hexdigest():
            raise ValueError("An ordered member snapshot needs its matching source-order digest.")
    else:
        normalized.sort(key=lambda member: member["username"])
    result = {
        "account_id": account, "peer_id": peer, "observed_at": stamp,
        "members": normalized, "member_count": len(normalized),
        "complete": True, "source": source,
    }
    encoded = _json(result)
    if len(encoded.encode()) > MAX_SNAPSHOT_BYTES:
        raise ValueError("A member snapshot exceeds its bounded storage budget.")
    # Source ordering changes presentation without starting a new membership baseline.
    stream = _json([directory, source["database_relative_path"], source["table"], _LEGACY_ADAPTER])
    return result, encoded, hashlib.sha256(stream.encode()).hexdigest()


def _display(snapshot, snapshot_id):
    result = dict(snapshot)
    observed = datetime.fromisoformat(snapshot["observed_at"]).astimezone(_ZONE)
    result.update(
        id=snapshot_id,
        members=tuple(snapshot["members"]),
        observed_at=observed.isoformat(),
        observed_label=f"{observed.day} {observed.strftime('%b %Y %H:%M:%S')}",
    )
    return result


def _verified_observation(row, account_id, peer_id):
    identity, expected_stream, encoded, expected_time = row
    if not isinstance(encoded, str) or len(encoded.encode()) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Invalid member snapshot size.")
    value, normalized, stream = _snapshot(json.loads(encoded))
    if (
        value["account_id"] != account_id or value["peer_id"] != peer_id
        or stream != expected_stream or normalized != encoded
        or value["observed_at"] != expected_time
        or hashlib.sha256(encoded.encode()).hexdigest() != identity
    ):
        raise ValueError("Member snapshot evidence changed.")
    return _display(value, identity)


def _snapshot_changes(previous, current):
    before = {member["username"]: member for member in previous["members"]}
    after = {member["username"]: member for member in current["members"]}
    for kind, identities, members in (
        ("newly_listed", after.keys() - before.keys(), after),
        ("no_longer_listed", before.keys() - after.keys(), before),
    ):
        for username in sorted(identities):
            yield {
                **members[username], "kind": kind, "reason": "unknown",
                "interval_start": previous["observed_at"],
                "interval_end": current["observed_at"],
            }


def _last_member_names(connection, account_id, peer_id, member_ids, known):
    """Find the last saved room label for each exact membership notice subject."""
    missing = set(member_ids)
    names = {}
    if not missing:
        return names
    for row in connection.execute(
        "SELECT id,stream_id,payload,observed_at FROM snapshots WHERE account_id=? AND peer_id=? "
        "ORDER BY observed_at DESC,id DESC",
        (account_id, peer_id),
    ):
        try:
            observation = known.get(row[0]) or _verified_observation(row, account_id, peer_id)
        except (TypeError, ValueError):
            continue
        for member in observation["members"]:
            username, display_name = member["username"], member["display_name"]
            if username in missing and display_name.strip():
                names[username] = display_name
                missing.remove(username)
        if not missing:
            break
    return names


class WechatGroupRosterRepository:
    """Append validated observations without changing any original archive."""

    def __init__(self, local_store_root):
        self.path = Path(local_store_root) / "messaging" / "wechat" / "group_rosters.sqlite3"

    def _check_path(self):
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise ValueError("Member snapshots need a regular local database.")

    def _ensure_database(self):
        """Publish a complete schema atomically even during simultaneous first captures."""
        self._check_path()
        if self.path.exists():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".group-rosters-", suffix=".sqlite3", dir=self.path.parent,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with closing(sqlite3.connect(temporary)) as connection:
                connection.executescript(_SCHEMA)
            try:
                os.link(temporary, self.path, follow_symlinks=False)
            except FileExistsError:
                pass
        finally:
            temporary.unlink()
        self._check_path()

    @staticmethod
    def _check_schema(connection):
        if connection.execute("SELECT version FROM roster_info").fetchall() != [(1,)]:
            raise ValueError("Unsupported member snapshot database.")
        for name in ("snapshots", "roster_info"):
            if connection.execute(
                "SELECT type FROM sqlite_schema WHERE name=?", (name,)
            ).fetchone() != ("table",):
                raise ValueError("Member snapshots need ordinary tables.")

    @contextmanager
    def _read(self):
        self._check_path()
        if not self.path.exists():
            yield None
            return
        with closing(sqlite3.connect(
            self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2,
        )) as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            self._check_schema(connection)
            connection.execute("BEGIN")
            yield connection

    def save(self, snapshot):
        value, encoded, stream = _snapshot(snapshot)
        snapshot_id = hashlib.sha256(encoded.encode()).hexdigest()
        self._ensure_database()
        with closing(sqlite3.connect(self.path, timeout=10)) as connection:
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA foreign_keys=ON")
            self._check_schema(connection)
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute(
                    "SELECT 1 FROM snapshots WHERE id=?", (snapshot_id,)
                ).fetchone():
                    return snapshot_id
                latest = connection.execute(
                    "SELECT observed_at FROM snapshots WHERE account_id=? AND peer_id=? "
                    "AND stream_id=? ORDER BY observed_at DESC LIMIT 1",
                    (value["account_id"], value["peer_id"], stream),
                ).fetchone()
                if latest and latest[0] >= value["observed_at"]:
                    raise ValueError("Member snapshot observations must advance within their source.")
                connection.execute(
                    "INSERT INTO snapshots VALUES(?,?,?,?,?,?)",
                    (
                        snapshot_id, value["account_id"], value["peer_id"],
                        value["observed_at"], stream, encoded,
                    ),
                )
        return snapshot_id

    def project(self, account_id, peer_id, *, member_ids=()):
        empty = {
            "status": "absent", "snapshot_count": 0, "current": None,
            "previous": None, "changes": (), "source_note": _NOTE,
        }
        _scope(account_id, peer_id)
        try:
            with self._read() as connection:
                if connection is None:
                    return empty
                count = connection.execute(
                    "SELECT count(*) FROM snapshots WHERE account_id=? AND peer_id=?",
                    (account_id, peer_id),
                ).fetchone()[0]
                rows = connection.execute(
                    "SELECT id,stream_id,payload,observed_at FROM snapshots WHERE account_id=? AND peer_id=? "
                    "ORDER BY observed_at DESC,id DESC LIMIT 1", (account_id, peer_id),
                ).fetchall()
                if not rows:
                    return empty
                rows += connection.execute(
                    "SELECT id,stream_id,payload,observed_at FROM snapshots WHERE account_id=? AND peer_id=? "
                    "AND stream_id=? AND id!=? ORDER BY observed_at DESC,id DESC LIMIT 1",
                    (account_id, peer_id, rows[0][1], rows[0][0]),
                ).fetchall()
                projected = [_verified_observation(row, account_id, peer_id) for row in rows]
                last_member_names = _last_member_names(
                    connection, account_id, peer_id, member_ids,
                    {value["id"]: value for value in projected},
                )
            current, *prior = projected
            previous = prior[0] if prior else None
            return {
                **empty, "status": "ready", "snapshot_count": count,
                "current": current, "previous": previous,
                "changes": tuple(_snapshot_changes(previous, current)) if previous else (),
                "last_member_names": last_member_names,
            }
        except (OSError, ValueError, TypeError, sqlite3.DatabaseError):
            return {**empty, "status": "invalid"}

    def history(self, peer_id, *, membership_notices=()):
        """Compare snapshots within each source and retain the first overlapping departure."""
        if classify_wechat_identifier(peer_id)["kind"] != "group":
            raise ValueError("Member snapshot history needs an exact group.")
        empty = {"status": "absent", "events": (), "total": 0, "source_note": _NOTE}
        try:
            events = []
            total = 0
            previous_by_source = {}
            first_absence_by_member = {}
            joins_by_member = {}
            for notice in membership_notices:
                if notice["kind"] == "joined" and notice["timestamp_value"]:
                    joined_at = _stamp(notice["timestamp_value"])
                    for username in notice["subject_ids"]:
                        joins_by_member.setdefault(username, []).append(joined_at)
            with self._read() as connection:
                if connection is None:
                    return empty
                for account, identity, stream, payload, observed_at in connection.execute(
                    "SELECT account_id,id,stream_id,payload,observed_at FROM snapshots WHERE peer_id=? "
                    "ORDER BY observed_at,account_id,stream_id,id", (peer_id,),
                ):
                    current = _verified_observation((identity, stream, payload, observed_at), account, peer_id)
                    scope = (account, stream)
                    prior = previous_by_source.get(scope)
                    previous_by_source[scope] = observed_at, current
                    for member in current["members"]:
                        username = member["username"]
                        first_absence = first_absence_by_member.get(username)
                        if first_absence is not None and first_absence < observed_at:
                            del first_absence_by_member[username]
                    if prior is None:
                        continue
                    previous_at, previous = prior
                    for change in _snapshot_changes(previous, current):
                        username = change["username"]
                        if change["kind"] == "no_longer_listed":
                            first_absence = first_absence_by_member.get(username)
                            # Each interval excludes its last observed presence. A
                            # later presence or exact join separates departures.
                            if (
                                first_absence is not None
                                and previous_at < first_absence
                                and not any(
                                    first_absence < joined_at <= observed_at
                                    for joined_at in joins_by_member.get(username, ())
                                )
                            ):
                                continue
                            first_absence_by_member[username] = observed_at
                        event = {
                            **change, "account_id": account,
                            "interval_start_label": previous["observed_label"],
                            "interval_end_label": current["observed_label"],
                            "previous_snapshot_id": previous["id"],
                            "current_snapshot_id": current["id"],
                        }
                        key = (observed_at, identity, username)
                        if len(events) < _MAX_HISTORY_EVENTS:
                            heapq.heappush(events, (key, event))
                        else:
                            heapq.heappushpop(events, (key, event))
                        total += 1
            if not previous_by_source:
                return empty
            return {
                **empty, "status": "ready", "total": total,
                "events": tuple(event for _, event in sorted(events, reverse=True)),
            }
        except (OSError, ValueError, TypeError, sqlite3.DatabaseError):
            return {**empty, "status": "invalid"}
