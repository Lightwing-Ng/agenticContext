"""Conservatively supplement Gemini history from an extracted official Takeout.

Run with ``python -m app.core.gemini_export_import SOURCE --timezone AREA/CITY``.
The default is a read-only preview; ``--apply`` explicitly publishes the plan.
"""

# Code version: v1.0.0-codex.0

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
from html import escape
from html.parser import HTMLParser
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import unicodedata
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq

from .config import LOCAL_STORE_ROOT
from .gemini_downloader import gemini_history_path, normalize_gemini_conversation_url
from .job_lock import CACHE_TASK_LOCK_NAME, CacheMaintenanceLock, CacheTaskLock
from .resource_persistence import (
    GEMINI_HISTORY_SCHEMA,
    GEMINI_HISTORY_SCHEMA_VERSION,
    write_parquet_rows_atomic,
)


VERSION = "v1.0.0-codex.0"
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_DATE = re.compile(
    r"^([A-Z][a-z]{2}) (\d{1,2}), (\d{4}), (\d{1,2}):(\d{2}):(\d{2})\s*([AP]M)(?:\s+([A-Za-z]{2,6}))?$"
)
_MONTHS = {name: index for index, name in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1
)}


class TakeoutImportError(RuntimeError):
    """Refuse an import whose source, state, or publication is not verifiable."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha(path: Path) -> str | None:
    return _sha(path.read_bytes()) if path.is_file() else None


def _reject_symlink_ancestors(path: Path) -> None:
    for candidate in (path.absolute(), *path.absolute().parents):
        if candidate.is_symlink():
            raise TakeoutImportError("Symlinks are not permitted in source or output paths.")


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _sync_directory(path: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _sync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())
    _sync_directory(path.parent)


def _atomic_json(path: Path, value: object) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass
class _Text:
    value: str
    start: int
    end: int


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str | None]
    start: int
    end: int
    children: list[_Node | _Text] = field(default_factory=list)

    def text(self) -> str:
        return "".join(child.text() if isinstance(child, _Node) else child.value for child in self.children)


def _walk(node: _Node):
    yield node
    for child in node.children:
        if isinstance(child, _Node):
            yield from _walk(child)


class _Cards(HTMLParser):
    """Keep exact character offsets while parsing only outer activity cards."""

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source = source
        self.lines = [0, *(match.end() for match in re.finditer("\n", source))]
        self.stack: list[_Node] = []
        self.cards: list[_Node] = []

    def _position(self) -> int:
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if not self.stack and "outer-cell" not in (attributes.get("class") or "").split():
            return
        start = self._position()
        node = _Node(tag, attributes, start, start + len(self.get_starttag_text()))
        if self.stack:
            self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if not self.stack:
            return
        if self.stack[-1].tag != tag:
            raise TakeoutImportError("Malformed activity HTML: mismatched closing tag.")
        node = self.stack.pop()
        node.end = self.source.find(">", self._position()) + 1
        if not self.stack:
            self.cards.append(node)

    def handle_data(self, data):
        if self.stack:
            start = self._position()
            # Exact fragment boundaries use the following element, not decoded length.
            end = self.source.find("<", start)
            self.stack[-1].children.append(_Text(data, start, end if end >= 0 else len(self.source)))


class _RenderedText(HTMLParser):
    _BLOCK = {"p", "div", "pre", "li", "tr", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._BLOCK or tag == "br":
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append("\t")
        if tag == "a":
            href = dict(attrs).get("href", "")
            if urlsplit(href).scheme in {"http", "https"}:
                self.links.append(href)

    def handle_endtag(self, tag):
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def _timestamp(value: str, zone: ZoneInfo) -> str:
    match = _DATE.fullmatch(" ".join(value.split()))
    if not match or match[1] not in _MONTHS:
        raise TakeoutImportError("Unrecognized activity timestamp.")
    month, day, year, hour, minute, second, meridiem, _label = match.groups()
    local = datetime(
        int(year), _MONTHS[month], int(day), int(hour) % 12 + (12 if meridiem == "PM" else 0),
        int(minute), int(second),
    )
    if int(hour) not in range(1, 13):
        raise TakeoutImportError("Invalid activity clock hour.")
    first, second_fold = local.replace(tzinfo=zone, fold=0), local.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second_fold.utcoffset():
        raise TakeoutImportError("Ambiguous or nonexistent local activity time; an exact instant is required.")
    return first.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _relative_reference(value: str) -> str | None:
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or value.startswith("#") or not parsed.path:
        return None
    decoded = unquote(parsed.path)
    relative = PurePosixPath(decoded)
    if relative.is_absolute() or ".." in relative.parts or "\\" in decoded or "\x00" in decoded:
        raise TakeoutImportError("Unsafe local attachment reference.")
    return relative.as_posix()


def _source_files(source: Path) -> tuple[Path, list[dict], str]:
    _reject_symlink_ancestors(source)
    html_path = source / "MyActivity.html" if source.is_dir() else source
    if html_path.name != "MyActivity.html" or not html_path.is_file():
        raise TakeoutImportError("Supply an extracted Gemini Apps directory or MyActivity.html.")
    directory = html_path.parent
    files = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise TakeoutImportError("Symlinks are not permitted in the source bundle.")
        if path.is_file():
            files.append({"path": path.relative_to(directory).as_posix(), "sha256": _file_sha(path), "bytes": path.stat().st_size})
        elif not path.is_dir():
            raise TakeoutImportError("Only regular files and directories are permitted in the source bundle.")
    return html_path, files, _sha(_json_bytes(files))


def parse_takeout(source: Path | str, *, timezone: str) -> dict:
    """Parse every card, preserving source fragments without executing attachments."""
    zone = ZoneInfo(timezone)
    html_path, files, digest = _source_files(Path(source))
    data = html_path.read_bytes()
    html_sha256 = _sha(data)
    if next(item["sha256"] for item in files if item["path"] == html_path.name) != html_sha256:
        raise TakeoutImportError("The activity source changed while being parsed.")
    document = data.decode("utf-8")
    parser = _Cards(document)
    parser.feed(document)
    parser.close()
    if parser.stack or not parser.cards:
        raise TakeoutImportError("The activity export is empty or structurally incomplete.")
    events = []
    for ordinal, card in enumerate(parser.cards):
        grids = [node for node in card.children if isinstance(node, _Node)
                 and "mdl-grid" in (node.attrs.get("class") or "").split()]
        if len(grids) != 1:
            raise TakeoutImportError(f"Activity card {ordinal} does not have one supported grid wrapper.")
        cells = [node for node in grids[0].children if isinstance(node, _Node)
                 and "content-cell" in (node.attrs.get("class") or "").split()]
        if len(cells) != 3 or not any("caption" in item for item in (cells[-1].attrs.get("class") or "").split()):
            raise TakeoutImportError(f"Activity card {ordinal} does not have the supported three-cell layout.")
        primary, _preview, caption = cells
        label_parts = []
        for child in primary.children:
            if isinstance(child, _Node) and child.tag == "br":
                break
            label_parts.append(child.text() if isinstance(child, _Node) else child.value)
        label = "".join(label_parts)
        match = re.match(r"^(Prompted|Branched)\s", label)
        kind = match[1] if match else label.strip()
        prompt = label[match.end():] if match else ""
        dates = [(index, child) for index, child in enumerate(primary.children)
                 if isinstance(child, _Text) and _DATE.fullmatch(" ".join(child.value.split()))]
        if len(dates) != 1:
            raise TakeoutImportError(f"Activity card {ordinal} must contain exactly one direct timestamp.")
        date_index, date = dates[0]
        stamp = _timestamp(date.value, zone)
        response = primary.children[date_index + 1:]
        while response and (isinstance(response[0], _Node) and response[0].tag == "br" or isinstance(response[0], _Text) and not response[0].value.strip()):
            response = response[1:]
        while response and (isinstance(response[-1], _Node) and response[-1].tag == "br" or isinstance(response[-1], _Text) and not response[-1].value.strip()):
            response = response[:-1]
        body = document[response[0].start:response[-1].end] if response else ""
        rendered = _RenderedText()
        rendered.feed(body)
        urls = list(dict.fromkeys(
            normalized for node in _walk(caption) if node.tag == "a"
            for normalized in [normalize_gemini_conversation_url(node.attrs.get("href") or "")] if normalized
        ))
        if kind == "Prompted" and not urls:
            raise TakeoutImportError(f"Prompted card {ordinal} has no dedicated conversation metadata.")
        references = []
        for node in _walk(card):
            for attr in ("href", "src"):
                value = node.attrs.get(attr)
                if value:
                    relative = _relative_reference(value)
                    if relative:
                        references.append({"tag": node.tag, "attribute": attr, "reference": value, "relative_path": relative})
        raw = document[card.start:card.end]
        events.append({
            "source_event_id": f"takeout:{html_sha256}:{ordinal}", "source_order": ordinal,
            "source_kind": kind, "conversation_urls": urls, "prompt": prompt,
            "timestamp_utc": stamp, "timestamp_precision": "second", "display_timestamp": date.value,
            "assistant_html": body, "assistant_text": "".join(rendered.parts).strip(),
            "assistant_source_links": list(dict.fromkeys(rendered.links)),
            "references": references, "raw_html": raw, "raw_html_sha256": _sha(raw.encode()),
            "source_char_start": card.start, "source_char_end": card.end,
        })
    return {"version": VERSION, "timezone": timezone, "source_html": html_path.name,
            "source_html_sha256": html_sha256, "source_files": files,
            "source_digest": digest, "events": events}


def _baseline(path: Path) -> list[dict]:
    if not path.exists():
        return []
    table = pq.read_table(path)
    if not table.schema.equals(GEMINI_HISTORY_SCHEMA, check_metadata=False):
        raise TakeoutImportError("Existing Gemini history has an unsupported schema.")
    rows = table.to_pylist()
    keys = set()
    positions = defaultdict(list)
    for row in rows:
        cid, index, role = row["conversation_id"], row["message_index"], row["role"]
        if row["message_key"] in keys or row["message_key"] != f"{cid}:{index}:{role}" or role not in {"user", "assistant"}:
            raise TakeoutImportError("Existing Gemini history contains invalid or duplicate keys.")
        if _sha(row["content_text"].encode()) != row["content_sha256"]:
            raise TakeoutImportError("Existing Gemini history has an invalid content hash.")
        keys.add(row["message_key"])
        positions[cid].append(index)
    if any(sorted(indexes) != list(range(len(indexes))) for indexes in positions.values()):
        raise TakeoutImportError("Existing Gemini history contains duplicate or noncontiguous positions.")
    return rows


def _normalized(text: str) -> str:
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n")).strip()


def _second(value: str) -> str | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(UTC).replace(microsecond=0).isoformat()
    except (ValueError, AttributeError):
        return None


def _matches_for_event(originals: list[dict], event: dict, aggregate: bool, used: set[str]) -> dict[str, dict]:
    candidates = {}
    for role, text in (("user", event["prompt"]), ("assistant", event["assistant_text"])):
        candidates[role] = sorted((row for row in originals
            if row["message_key"] not in used and row["role"] == role
            and _normalized(row["content_text"]) == _normalized(text)
            and (aggregate or row["first_seen_at"] != row["last_seen_at"]
                 and _second(row["last_seen_at"]) is not None
                 and _second(row["last_seen_at"]) == _second(event["timestamp_utc"]))),
            key=lambda row: row["message_index"])
    users, assistants = candidates["user"], candidates["assistant"]
    if aggregate:
        return {"user": users[0]} if len(users) == 1 else {}
    if event["prompt"].strip() and event["assistant_text"].strip():
        for user in users:
            coherent = [row for row in assistants if row["turn_index"] == user["turn_index"]]
            if coherent:
                return {"user": user, "assistant": coherent[0]}
        return {"user": users[0]} if len(users) == 1 else {}
    if event["prompt"].strip():
        return {"user": users[0]} if users else {}
    return {"assistant": assistants[0]} if len(assistants) == 1 else {}


def _verify_receipt(bundle: Path, catalog: dict, history_sha256: str | None) -> dict:
    receipt = json.loads((bundle / "receipt.json").read_text())
    pending = receipt.get("state") == "prepared" and receipt.get("baseline_sha256") == history_sha256
    if (receipt.get("version") != VERSION or receipt.get("state") not in {"prepared", "committed"}
            or receipt.get("source_digest") != catalog["source_digest"]
            or receipt.get("timezone") != catalog["timezone"]
            or not isinstance(receipt.get("candidate_sha256"), str)
            or receipt["candidate_sha256"] != history_sha256 and not pending):
        raise TakeoutImportError("This source has a receipt for a different timezone or changed history; refusing replay.")
    for name, key in (("events.json", "catalog_sha256"), ("provenance.json", "provenance_sha256")):
        _reject_symlink_ancestors(bundle / name)
        if not receipt.get(key) or _file_sha(bundle / name) != receipt[key]:
            raise TakeoutImportError("Published import provenance is missing or changed.")
    if _source_files(bundle / "source" / "MyActivity.html")[2] != catalog["source_digest"]:
        raise TakeoutImportError("Published source bundle is missing or changed.")
    if receipt.get("baseline_sha256") is not None:
        backup = bundle / "history-before.parquet"
        _reject_symlink_ancestors(backup)
        if _file_sha(backup) != receipt["baseline_sha256"]:
            raise TakeoutImportError("Published import backup is missing or changed.")
    if pending and receipt["candidate_sha256"] != history_sha256:
        candidate = bundle / "history-candidate.parquet"
        _reject_symlink_ancestors(candidate)
        if _file_sha(candidate) != receipt["candidate_sha256"]:
            raise TakeoutImportError("The prepared import candidate is missing or changed.")
        _baseline(candidate)
    return receipt


@dataclass
class ImportPlan:
    """Hold an immutable-source preview and compare-and-swap publication evidence."""

    source: Path
    local_store_root: Path
    catalog: dict
    baseline_sha256: str | None
    rows: list[dict]
    provenance: list[dict]
    summary: dict
    existing_receipt: dict | None = None


def plan_import(source: Path | str, *, timezone: str, local_store_root: Path | str = LOCAL_STORE_ROOT) -> ImportPlan:
    """Prepare a read-only import, retaining every existing row and repeated event."""
    source = Path(source).absolute()
    root = Path(local_store_root).absolute()
    _reject_symlink_ancestors(root)
    catalog = parse_takeout(source, timezone=timezone)
    source_dir = source if source.is_dir() else source.parent
    if root == source_dir or source_dir in root.parents or root in source_dir.parents:
        raise TakeoutImportError("The output store and source bundle must have separate directory trees.")
    history = gemini_history_path(root)
    _reject_symlink_ancestors(history)
    before_sha = _file_sha(history)
    rows = _baseline(history)
    receipt_path = history.parent / "imports" / f"takeout-{catalog['source_digest']}" / "receipt.json"
    _reject_symlink_ancestors(receipt_path)
    if receipt_path.exists():
        receipt = _verify_receipt(receipt_path.parent, catalog, before_sha)
        pending = receipt["candidate_sha256"] != before_sha
        if pending:
            rows = _baseline(receipt_path.parent / "history-candidate.parquet")
        return ImportPlan(source, root, catalog, before_sha, rows, [], {
            "version": VERSION, "dry_run": True, "already_imported": not pending,
            "resuming_prepared_import": pending, "added_rows": receipt["added_rows"] if pending else 0,
            "history_rows": len(rows), "source_cards": len(catalog["events"]),
        }, receipt)
    originals = defaultdict(list)
    for row in rows:
        originals[row["conversation_id"]].append(row)
    positions = {cid: len(items) for cid, items in originals.items()}
    turns = {cid: max(item["turn_index"] for item in items) for cid, items in originals.items()}
    used = set()
    provenance = []
    captured = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    projections = []
    for event in catalog["events"]:
        if event["source_kind"] != "Prompted":
            continue
        aggregate = len(event["conversation_urls"]) != 1
        for url in event["conversation_urls"]:
            cid = url.rsplit("/", 1)[-1]
            projections.append((cid, url, event, aggregate))
    projections.sort(key=lambda item: (item[0], item[2]["timestamp_utc"], -item[2]["source_order"]))
    for cid, url, event, aggregate in projections:
        matches = _matches_for_event(originals[cid], event, aggregate, used)
        event_turn = None
        roles = [("user", event["prompt"], f"<p>{escape(event['prompt'])}</p>")]
        if not aggregate:
            roles.append(("assistant", event["assistant_text"], event["assistant_html"]))
        for role, text, body in roles:
            text = text.replace("\x00", "").strip()
            if not text:
                continue
            # A unique content anchor is sufficient for an aggregate prompt fact;
            # individual aggregate times and response ownership remain unknown.
            matched = matches.get(role)
            if matched:
                used.add(matched["message_key"])
                provenance.append({"source_event_id": event["source_event_id"], "conversation_id": cid,
                                   "role": role, "message_key": matched["message_key"], "status": "existing_exact_source_anchor"})
                continue
            index = positions.get(cid, 0)
            if event_turn is None:
                event_turn = turns.get(cid, -1) + 1
            turn = event_turn
            uncertain = bool(originals[cid]) or aggregate
            author = "You" if role == "user" else "Gemini"
            if uncertain:
                author += " (Takeout; alignment uncertain)"
            row = {
                "schema_version": GEMINI_HISTORY_SCHEMA_VERSION, "platform": "gemini",
                "conversation_id": cid, "conversation_url": url,
                "conversation_title": originals[cid][0]["conversation_title"] if originals[cid] else event["prompt"].strip()[:120] or "Gemini Takeout session",
                "message_key": f"{cid}:{index}:{role}", "message_index": index,
                "turn_index": turn,
                "role": role, "author_label": author, "content_text": text, "content_html": body,
                "content_sha256": _sha(text.encode()),
                "source_links": event["assistant_source_links"] if role == "assistant" else [],
                "model_label": "", "first_seen_at": captured,
                "last_seen_at": captured if aggregate else event["timestamp_utc"],
            }
            rows.append(row)
            positions[cid] = index + 1
            turns[cid] = turn
            provenance.append({"source_event_id": event["source_event_id"], "conversation_id": cid, "role": role,
                               "message_key": row["message_key"], "status": "added_uncertain_tail" if uncertain else "added_source_turn",
                               "individual_timestamp_known": not aggregate,
                               "row_nonpositional_sha256": _sha(_json_bytes({key: value for key, value in row.items() if key not in {"message_key", "message_index", "turn_index"}}))})
    added = sum(item["status"].startswith("added") for item in provenance)
    summary = {"version": VERSION, "dry_run": True, "already_imported": False, "added_rows": added,
               "history_rows": len(rows), "source_cards": len(catalog["events"]),
               "aggregate_prompt_cards": sum(event["source_kind"] == "Prompted" and len(event["conversation_urls"]) > 1 for event in catalog["events"]),
               "nonprompt_cards_preserved_without_role_inference": sum(event["source_kind"] != "Prompted" for event in catalog["events"]),
               "source_digest": catalog["source_digest"], "baseline_sha256": before_sha}
    return ImportPlan(source, root, catalog, before_sha, rows, provenance, summary)


def apply_import(plan: ImportPlan) -> dict:
    """Publish a reviewed plan under a full cache lock and source/history CAS."""
    root = plan.local_store_root
    history = gemini_history_path(root)
    _reject_symlink_ancestors(history)
    _reject_symlink_ancestors(root / CACHE_TASK_LOCK_NAME)
    bundle = history.parent / "imports" / f"takeout-{plan.catalog['source_digest']}"
    for relative in ("source", "receipt.json", "events.json", "provenance.json", "history-before.parquet", "history-candidate.parquet"):
        _reject_symlink_ancestors(bundle / relative)
    lock = CacheMaintenanceLock(CacheTaskLock(root / CACHE_TASK_LOCK_NAME), root)
    if not lock.acquire("Gemini Takeout import"):
        raise TakeoutImportError("A cache task is active; Takeout import requires exclusive maintenance access.")
    try:
        if _file_sha(history) != plan.baseline_sha256:
            raise TakeoutImportError("Gemini history changed after preview; make a new plan.")
        baseline_rows = _baseline(history)
        if plan.rows[:len(baseline_rows)] != baseline_rows:
            raise TakeoutImportError("The plan does not preserve every existing row, field, and relative position.")
        _html_path, files, digest = _source_files(plan.source)
        if digest != plan.catalog["source_digest"] or files != plan.catalog["source_files"]:
            raise TakeoutImportError("The source changed after preview; make a new plan.")
        bundle = history.parent / "imports" / f"takeout-{digest}"
        receipt_path = bundle / "receipt.json"
        if plan.existing_receipt is not None:
            receipt = _verify_receipt(bundle, plan.catalog, plan.baseline_sha256)
            if receipt != plan.existing_receipt:
                raise TakeoutImportError("The import receipt changed after preview.")
            if receipt.get("state") == "prepared":
                if receipt["candidate_sha256"] != plan.baseline_sha256:
                    os.replace(bundle / "history-candidate.parquet", history)
                    _sync_directory(history.parent)
                    if _file_sha(history) != receipt["candidate_sha256"] or _baseline(history) != plan.rows:
                        raise TakeoutImportError("Resumed publication readback failed; preserve the verified backup.")
                receipt["state"] = "committed"
                _atomic_json(receipt_path, receipt)
            return {**plan.summary, "dry_run": False}
        if receipt_path.exists():
            raise TakeoutImportError("An import receipt appeared after preview; make a new plan.")
        bundle.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(bundle, 0o700)
        raw_root = bundle / "source"
        raw_root.mkdir(mode=0o700, exist_ok=True)
        source_dir = plan.source if plan.source.is_dir() else plan.source.parent
        for item in files:
            destination = raw_root / item["path"]
            _reject_symlink_ancestors(destination)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if destination.exists() and _file_sha(destination) != item["sha256"]:
                raise TakeoutImportError("An existing source bundle has conflicting bytes.")
            if not destination.exists():
                with destination.open("xb") as output:
                    os.chmod(destination, 0o600)
                    with (source_dir / item["path"]).open("rb") as incoming:
                        shutil.copyfileobj(incoming, output)
                    output.flush()
                    os.fsync(output.fileno())
                _sync_directory(destination.parent)
            if _file_sha(destination) != item["sha256"]:
                raise TakeoutImportError("Source bundle verification failed.")
        _atomic_json(bundle / "events.json", plan.catalog)
        _atomic_json(bundle / "provenance.json", plan.provenance)
        backup = bundle / "history-before.parquet"
        if plan.baseline_sha256 is not None:
            if backup.exists() and _file_sha(backup) != plan.baseline_sha256:
                raise TakeoutImportError("An existing backup has conflicting bytes.")
            if not backup.exists():
                shutil.copyfile(history, backup)
                os.chmod(backup, 0o600)
                _sync_file(backup)
            if _file_sha(backup) != plan.baseline_sha256 or _baseline(backup) != _baseline(history):
                raise TakeoutImportError("History backup verification failed.")
        candidate = bundle / "history-candidate.parquet"
        write_parquet_rows_atomic(candidate, plan.rows, GEMINI_HISTORY_SCHEMA)
        os.chmod(candidate, 0o600)
        if _baseline(candidate) != plan.rows:
            raise TakeoutImportError("Typed candidate readback differs from the plan.")
        _sync_file(candidate)
        receipt = {"version": VERSION, "state": "prepared", "source_digest": digest,
                   "timezone": plan.catalog["timezone"], "baseline_sha256": plan.baseline_sha256,
                   "candidate_sha256": _file_sha(candidate), "added_rows": plan.summary["added_rows"],
                   "source_event_ids": [event["source_event_id"] for event in plan.catalog["events"]],
                   "provenance_sha256": _file_sha(bundle / "provenance.json"),
                   "catalog_sha256": _file_sha(bundle / "events.json")}
        _atomic_json(receipt_path, receipt)
        if _file_sha(history) != plan.baseline_sha256 or _source_files(plan.source)[2] != digest:
            raise TakeoutImportError("Source or history changed during staging; publication refused.")
        os.replace(candidate, history)
        _sync_directory(history.parent)
        if _file_sha(history) != receipt["candidate_sha256"] or _baseline(history) != plan.rows:
            raise TakeoutImportError("Published history verification failed; preserve the verified backup for recovery.")
        receipt["state"] = "committed"
        _atomic_json(receipt_path, receipt)
        return {**plan.summary, "dry_run": False, "bundle": str(bundle)}
    finally:
        lock.release()


def main(argv: list[str] | None = None) -> int:
    """Expose a read-only preview unless publication is explicitly requested."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--timezone", required=True, help="Explicit IANA timezone for displayed activity times")
    parser.add_argument("--local-store-root", type=Path, default=LOCAL_STORE_ROOT)
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args(argv)
    plan = plan_import(arguments.source, timezone=arguments.timezone, local_store_root=arguments.local_store_root)
    print(json.dumps(apply_import(plan) if arguments.apply else plan.summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
