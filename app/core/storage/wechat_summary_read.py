"""Publish and read authenticated, account-scoped WeChat conversation summaries.

Code version: v1.2.1-codex.0
"""

from contextlib import closing
import hashlib
import hmac
import math
import sqlite3

from .. import wechat_browser_snapshot as snapshot
from .. import wechat_conversation_events as events
from ..chat_history_browser import CHAT_HISTORY_PAGE_SIZE, ChatHistoryPage, ChatHistorySession
from ..wechat_archive import (
    WechatArchiveRepository, _HTML_COMPLETION_SUFFIX, _file_identity, _html_group_supplements,
)
from ..wechat_contacts import classify_wechat_identifier
from ..wechat_display_names import WechatDisplayNames
from ..wechat_mac_resources import WechatMacResourceResolver
from ..wechat_secondary_completions import FILENAME as COMPLETIONS_FILENAME


SUMMARY_VERSION = 2
MANIFEST = "direct-summary-manifest.snapshot"
ACCOUNT_SUMMARY_KINDS = frozenset({"all", "direct", "group", "unclassified"})
_ASCII_NOCASE = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_KIND_LABELS = {"direct": "Private chat", "group": "Group chat", "unclassified": "Unclassified chat"}
_PARTITION_FIELDS = (
    "key", "name", "identity_status", "account_id", "partition_id", "source_label", "source_file",
)


def _account_filename(account):
    digest = hashlib.sha256(account.encode("utf-8")).hexdigest()
    return f"direct-summary-{digest}.snapshot"


def _read(directory, filename, key):
    raw, _ = snapshot._read_private(directory / filename, snapshot.MAX_SNAPSHOT_BYTES)
    return snapshot._decode_snapshot(raw, key)


def _write(directory, filename, key, value, unchanged):
    payload = snapshot._json(value)
    signature = hmac.new(key, payload, hashlib.sha256).hexdigest().encode("ascii")
    encoded = snapshot._MAGIC + signature + b"\n" + payload
    if len(encoded) > snapshot.MAX_SNAPSHOT_BYTES:
        return False
    return snapshot._write_private_cache(
        directory / filename, encoded, unchanged, prefix=".direct-summary-",
    )


def publish_account_summaries(repository, history_identity):
    """Split an already verified overview; publish the manifest only after every shard."""
    pin = snapshot._pin(repository, history_identity)
    directory, key, key_identity = snapshot._storage(repository)

    def unchanged():
        return snapshot._unchanged(repository, history_identity, pin, directory, key_identity)

    overview = snapshot._read_snapshot(directory, key, pin)
    if overview is None:
        return False
    header = {"summary_version": SUMMARY_VERSION, "pin": pin}
    with closing(sqlite3.connect(
        repository.path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True,
    )) as connection:
        partitions = [list(row) for row in connection.execute(
            "SELECT account_key,account_label,identity_status,account_id,id,source_label,source_file "
            "FROM partitions ORDER BY account_label COLLATE NOCASE,id"
        )]
    if any(len(row) != len(_PARTITION_FIELDS) for row in partitions) or not unchanged():
        return False

    accounts = {row[0]: [] for row in partitions}
    for row in overview["rows"]:
        accounts.setdefault(row[2], []).append(row)
    manifest = {
        **header,
        "partitions": partitions,
        "accounts": sorted(accounts),
        "resource_status": overview["resource_status"],
        "resource_index_accounts": overview["resource_index_accounts"],
    }
    try:
        if _read(directory, MANIFEST, key) == manifest and all(
            _read(directory, _account_filename(account), key)
            == {**header, "account": account, "rows": rows}
            for account, rows in accounts.items()
        ):
            return unchanged()
    except (OSError, ValueError):
        pass
    for account, rows in accounts.items():
        if not _write(directory, _account_filename(account), key, {
            **header, "account": account, "rows": rows,
        }, unchanged):
            return False
    return _write(directory, MANIFEST, key, manifest, unchanged) and unchanged()


def _count_deltas(repository, rows, pin):
    """Read only already published event counts for the selected account's rows."""
    directory = events._directory(repository)
    if directory.is_symlink() or not directory.is_dir():
        return {}
    fingerprint = events._fingerprint(pin["inputs"])
    counts, scanned = {}, 0
    for row in sorted(rows, key=lambda row: row[0]):
        session = row[0]
        try:
            path = events._cache_path(repository, session)
            identity = events._identity(path)
            size = identity["size"]
            if (
                size > max(events.MAX_CACHE_BYTES, events.MAX_COMPRESSED_CACHE_BYTES)
                or scanned + size + 1 > events.MAX_SUMMARY_SCAN_BYTES
            ):
                continue
            scanned += size + 1
            summary = events._cached_summary(path, fingerprint, identity)
            if summary is not None and summary[0] == session:
                counts[session] = summary[1]
        except (OSError, ValueError):
            continue
    return counts


def _group_supplements(repository, account, rows, manifest):
    """Open the primary archive only when the signed group summaries can pair."""
    scopes = {(row[1], row[3]) for row in rows}
    if not any(
        partition.endswith(_HTML_COMPLETION_SUFFIX)
        and peer and peer.endswith("@chatroom")
        and (partition.removesuffix(_HTML_COMPLETION_SUFFIX), peer) in scopes
        for partition, peer in scopes
    ):
        return {}
    with closing(sqlite3.connect(
        repository.path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True,
    )) as connection:
        connection.row_factory = sqlite3.Row
        table, _, _ = snapshot._install(connection, {
            "rows": rows,
            "resource_status": manifest["resource_status"],
            "resource_index_accounts": manifest["resource_index_accounts"],
        })
        return _html_group_supplements(connection, table, account)


def _page(root, account, chat_kind, sort, page, rows, manifest, deltas, supplements, *, previous):
    partitions = {
        row[4]: dict(zip(_PARTITION_FIELDS, row, strict=True)) for row in manifest["partitions"]
    }
    rows = [row for row in rows if row[1] in partitions]
    if sort == "name":
        rows.sort(key=lambda row: (row[4].translate(_ASCII_NOCASE), row[0]))
    else:
        direction = 1 if sort == "oldest" else -1

        def order(row):
            last = max(row[8], supplements[row[0]]["last_sort"]) if row[0] in supplements else row[8]
            return last == 0, direction * last, row[0]

        rows.sort(key=order)
    size = CHAT_HISTORY_PAGE_SIZE
    pages = max(1, math.ceil(len(rows) / size))
    current = max(1, min(pages, int(page)))

    def count(row):
        return (
            row[6] - deltas.get(row[0], 0)
            + supplements.get(row[0], {}).get("additional_count", 0)
        )

    accounts = [
        {key: value for key, value in partition.items() if key not in {"source_label", "source_file"}}
        for partition in partitions.values()
    ]
    display_names = WechatDisplayNames(root)
    mac_resources = WechatMacResourceResolver(root)
    projected, options = display_names.project_accounts(accounts)
    sessions = []
    for row in rows[(current - 1) * size : current * size]:
        raw = dict(zip(snapshot._COLUMNS, row, strict=True))
        kind = raw["kind"]
        partition = partitions[raw["partition_id"]]
        messaging = {
            "account_key": raw["account_key"], "account_id": partition["account_id"],
            "account_label": partition["name"], "identity_status": partition["identity_status"],
            "partition_id": raw["partition_id"], "source_label": partition["source_label"],
            "source_file": partition["source_file"], "kind": kind, "peer_id": raw["peer_id"],
        }
        official = kind != "group" and classify_wechat_identifier(raw["peer_id"])["kind"] == "official_account"
        messaging.update(
            conversation_kind_label="Official account" if official else _KIND_LABELS.get(kind, "Unclassified chat"),
            viewer_account_label=display_names.account_label(messaging, verified_accounts=accounts),
            mac_account_mapping=mac_resources.account_mapping(partition["account_id"] or ""),
        )
        if supplement := supplements.get(raw["id"]):
            messaging["html_supplement"] = supplement
        sessions.append(ChatHistorySession(
            stable_id=raw["id"], source="wechat", conversation_id=raw["id"], conversation_url="",
            conversation_title=raw["title"], message_count=count(row), first_seen_at=raw["first_time"],
            last_seen_at=raw["last_time"], latest_message="" if previous else raw["latest_text"], latest_role="",
            latest_author_label="", model_label="", source_links=(), messaging=messaging,
        ))
    resource_accounts = None if previous else manifest["resource_index_accounts"]
    return ChatHistoryPage(
        items=(), total_count=len(rows), conversation_count=len(rows), current_page=current,
        total_pages=pages, page_size=size, session_view=True, sessions=tuple(sessions),
        messaging={
            "accounts": projected, "account_options": options, "selected_account": account,
            "chat_kind": chat_kind, "event_projection": {}, "requested_session": "",
            "message_count": 0 if previous else sum(map(count, rows)), "source_snapshots": {},
            "resource_index_status": manifest["resource_status"],
            "resource_index_accounts": tuple(resource_accounts) if resource_accounts is not None else None,
        },
    )


def account_summary_pin(root):
    """Pin the exact archive publication without opening any message database."""
    repository = WechatArchiveRepository(root)
    return snapshot._pin(repository, _file_identity(repository.path))


def read_account_summary(root, *, account, sort, page, chat_kind="direct", previous=False):
    """Read only navigation metadata and one account shard; never build on a miss."""
    if chat_kind not in ACCOUNT_SUMMARY_KINDS:
        return None
    repository = WechatArchiveRepository(root)
    directory = repository.path.parent / "browser_cache"
    key_path = directory / "signing.key"
    try:
        directory_state = snapshot._directory_identity(directory)
        key, key_state = snapshot._read_signing_key(key_path)
        current_pin = account_summary_pin(root)
        manifest = _read(directory, MANIFEST, key)
        if (
            not isinstance(manifest, dict)
            or manifest.get("summary_version") != SUMMARY_VERSION
            or manifest.get("resource_status") not in {"ready", "absent"}
        ):
            return None
        pin = manifest.get("pin")
        if previous:
            completion = str(repository.path.with_name(COMPLETIONS_FILENAME).absolute())
            bank = str(repository.path.parent.resolve() / "macos_recovery.sqlite3")
            if not snapshot._preview_pin_matches(pin, current_pin, completion, bank):
                return None
        elif pin != current_pin:
            return None
        rows = []
        if account in manifest["accounts"]:
            shard = _read(directory, _account_filename(account), key)
            if (
                not isinstance(shard, dict)
                or shard.get("summary_version") != SUMMARY_VERSION
                or shard.get("pin") != pin
                or shard.get("account") != account
                or not isinstance(shard.get("rows"), list)
                or len(shard["rows"]) > snapshot.MAX_ROWS
                or any(not snapshot._valid_row(row) or row[2] != account for row in shard["rows"])
            ):
                return None
            rows = shard["rows"]
            if chat_kind != "all":
                rows = [row for row in rows if row[5] == chat_kind]
        supplements = _group_supplements(
            repository, account, rows, manifest,
        ) if chat_kind == "group" else {}
        if supplements:
            source_ids = {supplement["id"] for supplement in supplements.values()}
            rows = [row for row in rows if row[0] not in source_ids]
        result = _page(
            root, account, chat_kind, sort, page, rows, manifest,
            {} if previous else _count_deltas(repository, rows, pin), previous=previous,
            supplements=supplements,
        )
        if (
            account_summary_pin(root) == current_pin
            and snapshot._directory_identity(directory) == directory_state
            and snapshot._cache_file_state(key_path) == key_state
        ):
            return result
    except (OSError, KeyError, ValueError, TypeError, RecursionError, sqlite3.DatabaseError):
        pass
    return None
