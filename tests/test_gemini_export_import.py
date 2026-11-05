"""Synthetic source, preservation, and crash-recovery tests for Takeout imports."""

# Code version: v2.0.0-codex.0

from __future__ import annotations

from html import escape
import errno
import json
import os
from pathlib import Path
import shutil

import pytest

from app.core import gemini_export_import as importer
from app.core.gemini_downloader import GeminiConversationLink, GeminiHistoryStore, gemini_history_path
from app.core.job_lock import CacheTaskLock, cache_task_resource_lock_path
from app.core.llm_sqlite import read_sqlite_schema, read_sqlite_summaries
from app.core.owner_only_files import (
    OwnerOnlyFileError,
    owner_only_directory_problem,
    owner_only_problem,
)
from app.core.resource_persistence import GEMINI_HISTORY_SCHEMA, read_resource_rows, write_parquet_rows_atomic


def _card(prompt="Question", reply="<p>Answer</p>", ids=("example-session",), kind="Prompted", date="Jan 2, 2026, 9:30:13 PM CST", attachments=""):
    caption = "".join(f'<a href="https://gemini.google.com/app/{value}">Session</a>' for value in ids)
    return (
        '<div class="outer-cell"><div class="mdl-grid"><div class="content-cell">'
        f'{kind}\u00a0{escape(prompt)}<br>{attachments}{date}<br>{reply}'
        '</div><div class="content-cell">Preview</div>'
        f'<div class="content-cell mdl-typography--caption">{caption}</div></div></div>'
    )


def _source(tmp_path, *cards):
    source = tmp_path / "export"
    source.mkdir()
    (source / "MyActivity.html").write_bytes(("<html><body>" + "".join(cards or [_card()]) + "</body></html>").encode())
    return source


def _plan(source, root):
    return importer.plan_import(source, timezone="Asia/Taipei", local_store_root=root)


def _seed(root, prompt="Question", reply="Answer", timestamp="2026-01-02T13:30:13.123456Z", storage_format="sqlite"):
    path = gemini_history_path(root).with_suffix(".parquet" if storage_format == "parquet" else ".sqlite3")
    store = GeminiHistoryStore(path)
    store.replace_conversation(
        GeminiConversationLink("example-session", "https://gemini.google.com/app/example-session", "Original title"),
        [{"role": role, "message_index": index, "turn_index": 0, "content_text": text,
          "content_html": f"<p>{text}</p>", "author_label": f"Original {role}",
          "model_label": "Original model", "source_links": ["https://example.invalid/source"],
          "message_timestamp": timestamp}
         for index, (role, text) in enumerate((("user", prompt), ("assistant", reply)))],
        "2026-02-01T00:00:00Z",
    )
    store.save()
    return store.rows


def _legacy_receipt(source, root, state):
    """Build the published v1 Parquet envelope without invoking the new writer."""
    original = _seed(root, storage_format="parquet")
    legacy = gemini_history_path(root).with_suffix(".parquet")
    plan = _plan(source, root)
    bundle = legacy.parent / "imports" / f"takeout-{plan.catalog['source_digest']}"
    bundle.mkdir(parents=True)
    shutil.copytree(source, bundle / "source")
    catalog = {**plan.catalog, "version": "v1.0.0-codex.0"}
    importer._atomic_json(bundle / "events.json", catalog)
    importer._atomic_json(bundle / "provenance.json", plan.provenance)
    shutil.copyfile(legacy, bundle / "history-before.parquet")
    candidate = bundle / "history-candidate.parquet"
    write_parquet_rows_atomic(candidate, plan.rows, GEMINI_HISTORY_SCHEMA)
    receipt = {
        "version": "v1.0.0-codex.0", "state": "prepared" if state != "committed" else "committed",
        "source_digest": catalog["source_digest"], "timezone": catalog["timezone"],
        "baseline_sha256": importer._file_sha(legacy), "candidate_sha256": importer._file_sha(candidate),
        "added_rows": plan.summary["added_rows"],
        "source_event_ids": [event["source_event_id"] for event in catalog["events"]],
        "provenance_sha256": importer._file_sha(bundle / "provenance.json"),
        "catalog_sha256": importer._file_sha(bundle / "events.json"),
    }
    importer._atomic_json(bundle / "receipt.json", receipt)
    if state in {"committed", "published"}:
        os.replace(candidate, legacy)
    return bundle, plan.rows, original


def test_dry_run_has_no_files_locks_or_backups(tmp_path):
    source = _source(tmp_path)
    root = tmp_path / "store"
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    plan = _plan(source, root)
    assert plan.summary["added_rows"] == 2
    assert plan.rows[0]["last_seen_at"] == "2026-01-02T13:30:13Z"
    assert not root.exists()
    assert before == {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}


def test_exact_source_second_anchors_preserve_all_original_fields(tmp_path):
    source = _source(tmp_path)
    root = tmp_path / "store"
    original = _seed(root)
    plan = _plan(source, root)
    assert plan.summary["added_rows"] == 0
    assert plan.rows == original
    result = importer.apply_import(plan)
    assert not result["dry_run"]
    assert read_resource_rows(gemini_history_path(root)) == original
    backup = Path(result["bundle"]) / "history-before.sqlite3"
    assert read_resource_rows(backup) == original


def test_unmatched_source_appends_visible_tail_without_reindexing_old_rows(tmp_path):
    source = _source(tmp_path, _card(reply="<p>A genuinely different response.</p>"))
    root = tmp_path / "store"
    original = _seed(root)
    plan = _plan(source, root)
    assert plan.rows[:len(original)] == original
    assert plan.summary["added_rows"] == 1
    assert plan.rows[-1]["message_index"] == 2
    assert plan.rows[-1]["content_text"] == "A genuinely different response."
    assert "alignment uncertain" in plan.rows[-1]["author_label"]
    assert plan.rows[-1]["turn_index"] == 1
    importer.apply_import(plan)
    first = gemini_history_path(root).read_bytes()
    repeated = _plan(source, root)
    assert repeated.summary["already_imported"]
    assert importer.apply_import(repeated)["added_rows"] == 0
    assert gemini_history_path(root).read_bytes() == first


def test_repeated_identical_source_events_are_not_collapsed(tmp_path):
    source = _source(tmp_path, _card(), _card())
    root = tmp_path / "store"
    original = _seed(root)
    plan = _plan(source, root)
    assert plan.rows[:2] == original
    assert plan.summary["added_rows"] == 2
    assert [row["content_text"] for row in plan.rows] == ["Question", "Answer", "Question", "Answer"]
    importer.apply_import(plan)
    assert importer.apply_import(_plan(source, root))["added_rows"] == 0


def test_same_second_answer_cannot_anchor_an_unrelated_user_turn(tmp_path):
    source = _source(tmp_path, _card(prompt="Different question"))
    root = tmp_path / "store"
    original = _seed(root)
    plan = _plan(source, root)
    assert plan.rows[:2] == original
    assert plan.summary["added_rows"] == 2
    assert [row["turn_index"] for row in plan.rows[-2:]] == [1, 1]


@pytest.mark.parametrize("timestamp", ["2026-01-02T13:30:13", "2026-02-01T00:00:00Z"])
def test_naive_or_capture_fallback_baseline_time_never_proves_source_identity(tmp_path, timestamp):
    source = _source(tmp_path)
    root = tmp_path / "store"
    _seed(root, timestamp=timestamp)
    assert _plan(source, root).summary["added_rows"] == 2


def test_assistant_only_tail_has_its_own_turn_even_after_unrelated_later_rows(tmp_path):
    source = _source(tmp_path, _card(reply="<p>Different response</p>"))
    root = tmp_path / "store"
    old = _seed(root)
    later = [dict(row) for row in old]
    for row in later:
        row["message_index"] += 2
        row["message_key"] = f"example-session:{row['message_index']}:{row['role']}"
        row["turn_index"] = 7
        row["last_seen_at"] = "2026-01-03T13:30:13Z"
    write_parquet_rows_atomic(gemini_history_path(root), old + later, GEMINI_HISTORY_SCHEMA)
    plan = _plan(source, root)
    assert plan.rows[:4] == old + later
    assert plan.rows[-1]["turn_index"] == 8


def test_aggregate_prompt_is_attributed_but_response_and_individual_time_are_unknown(tmp_path):
    source = _source(tmp_path, _card(ids=("session-one", "session-two"), reply="<p>Unassigned combined reply.</p>"))
    plan = _plan(source, tmp_path / "store")
    assert len(plan.rows) == 2
    assert {row["conversation_id"] for row in plan.rows} == {"session-one", "session-two"}
    assert all(row["role"] == "user" and row["last_seen_at"] == row["first_seen_at"] for row in plan.rows)
    assert all(not item["individual_timestamp_known"] for item in plan.provenance)
    assert "combined reply" in plan.catalog["events"][0]["assistant_html"]


def test_branch_activity_does_not_invent_a_user_or_assistant_role(tmp_path):
    source = _source(tmp_path, _card(kind="Branched"))
    plan = _plan(source, tmp_path / "store")
    assert plan.rows == []
    assert plan.summary["nonprompt_cards_preserved_without_role_inference"] == 1


def test_full_source_bundle_is_preserved_without_executing_uploads(tmp_path):
    source = _source(tmp_path, _card(attachments='Attached 1 file.<a href="upload.py">file</a><br>'))
    (source / "upload.py").write_bytes(b"raise RuntimeError('must never run')\n")
    (source / "unreferenced.bin").write_bytes(b"\x00\x01")
    result = importer.apply_import(_plan(source, tmp_path / "store"))
    bundle = Path(result["bundle"])
    for name in ("MyActivity.html", "upload.py", "unreferenced.bin"):
        assert (bundle / "source" / name).read_bytes() == (source / name).read_bytes()
    catalog = json.loads((bundle / "events.json").read_text())
    assert catalog["events"][0]["raw_html_sha256"] == importer._sha(catalog["events"][0]["raw_html"].encode())
    assert owner_only_directory_problem(bundle) == ""
    for path in bundle.rglob("*"):
        if path.is_dir():
            assert owner_only_directory_problem(path) == ""
            if os.name == "posix":
                assert path.stat().st_mode & 0o777 == 0o700
        else:
            assert owner_only_problem(path) == ""
            if os.name == "posix":
                assert path.stat().st_mode & 0o777 == 0o600
    assert owner_only_problem(gemini_history_path(tmp_path / "store")) == ""
    if os.name == "posix":
        assert bundle.stat().st_mode & 0o777 == 0o700


def test_other_provider_resource_lock_refuses_publication(tmp_path):
    source = _source(tmp_path)
    root = tmp_path / "store"
    plan = _plan(source, root)
    resource = CacheTaskLock(cache_task_resource_lock_path(root, "other-provider"))
    assert resource.acquire("busy")
    try:
        with pytest.raises(importer.TakeoutImportError, match="exclusive maintenance"):
            importer.apply_import(plan)
        assert not gemini_history_path(root).exists()
        assert not (root / "llm").exists()
    finally:
        resource.release()


@pytest.mark.parametrize("change", ["history", "source"])
def test_compare_and_swap_rejects_changed_input(tmp_path, change):
    source = _source(tmp_path)
    root = tmp_path / "store"
    plan = _plan(source, root)
    if change == "history":
        original = _seed(root)
    else:
        (source / "new-upload.bin").write_bytes(b"changed source tree")
    with pytest.raises(importer.TakeoutImportError, match="changed after preview"):
        importer.apply_import(plan)
    if change == "history":
        assert read_resource_rows(gemini_history_path(root)) == original
    else:
        assert not gemini_history_path(root).exists()


@pytest.mark.parametrize("damage", ["content", "order", "multiplicity"])
def test_under_lock_guard_rejects_a_plan_that_does_not_preserve_exact_baseline(tmp_path, damage):
    source = _source(tmp_path)
    root = tmp_path / "store"
    original = _seed(root)
    plan = _plan(source, root)
    if damage == "content":
        plan.rows[0] = {**plan.rows[0], "author_label": "Changed old field"}
    elif damage == "order":
        plan.rows.reverse()
    else:
        plan.rows.pop()
    with pytest.raises(importer.TakeoutImportError, match="preserve every existing"):
        importer.apply_import(plan)
    assert read_resource_rows(gemini_history_path(root)) == original
    assert not (gemini_history_path(root).parent / "imports").exists()


def test_crash_after_history_replace_is_idempotently_reconciled(tmp_path, monkeypatch):
    source = _source(tmp_path)
    root = tmp_path / "store"
    write = importer._atomic_json

    def fail_final_receipt(path, value):
        if path.name == "receipt.json" and value.get("state") == "committed":
            raise OSError("Synthetic final receipt failure")
        write(path, value)

    monkeypatch.setattr(importer, "_atomic_json", fail_final_receipt)
    with pytest.raises(OSError, match="Synthetic"):
        importer.apply_import(_plan(source, root))
    original = gemini_history_path(root).read_bytes()
    monkeypatch.setattr(importer, "_atomic_json", write)
    replay = _plan(source, root)
    assert replay.summary["already_imported"]
    assert importer.apply_import(replay)["added_rows"] == 0
    assert gemini_history_path(root).read_bytes() == original
    assert json.loads(next((root / "llm" / "gemini" / "imports").glob("*/receipt.json")).read_text())["state"] == "committed"


def test_prepared_candidate_is_verified_and_resumed_after_precommit_failure(tmp_path, monkeypatch):
    source = _source(tmp_path, _card(reply="<p>New response</p>"))
    root = tmp_path / "store"
    _seed(root)
    before = gemini_history_path(root).read_bytes()
    replace = importer.os.replace

    def fail_history_replace(incoming, destination):
        if Path(incoming).name == "history-candidate.sqlite3":
            raise OSError("Synthetic precommit failure")
        return replace(incoming, destination)

    monkeypatch.setattr(importer.os, "replace", fail_history_replace)
    with pytest.raises(OSError, match="Synthetic"):
        importer.apply_import(_plan(source, root))
    assert gemini_history_path(root).read_bytes() == before
    monkeypatch.setattr(importer.os, "replace", replace)
    resumed = _plan(source, root)
    assert resumed.summary["resuming_prepared_import"]
    expected_rows = resumed.rows
    assert importer.apply_import(resumed)["added_rows"] == 1
    assert read_resource_rows(gemini_history_path(root)) == expected_rows
    assert importer.apply_import(_plan(source, root))["added_rows"] == 0


def test_changed_asset_cannot_be_silently_treated_as_same_import(tmp_path):
    source = _source(tmp_path)
    (source / "upload.bin").write_bytes(b"first")
    root = tmp_path / "store"
    first = importer.apply_import(_plan(source, root))
    (source / "upload.bin").write_bytes(b"second")
    plan = _plan(source, root)
    assert not plan.summary["already_imported"]
    second = importer.apply_import(plan)
    assert second["bundle"] != first["bundle"]
    assert (Path(first["bundle"]) / "source" / "upload.bin").read_bytes() == b"first"
    assert (Path(second["bundle"]) / "source" / "upload.bin").read_bytes() == b"second"


@pytest.mark.parametrize("member", ["source/MyActivity.html", "events.json", "provenance.json", "history-before.sqlite3"])
def test_replay_refuses_damaged_durable_source_provenance_or_backup(tmp_path, member):
    source = _source(tmp_path)
    root = tmp_path / "store"
    _seed(root)
    result = importer.apply_import(_plan(source, root))
    (Path(result["bundle"]) / member).write_bytes(b"damaged")
    before = gemini_history_path(root).read_bytes()
    with pytest.raises(importer.TakeoutImportError):
        _plan(source, root)
    assert gemini_history_path(root).read_bytes() == before


@pytest.mark.parametrize("damage", ["duplicate_key", "duplicate_index", "hash", "schema"])
@pytest.mark.parametrize("storage_format", ["sqlite", "parquet"])
def test_corrupt_existing_history_fails_closed_before_writes(tmp_path, damage, storage_format):
    source = _source(tmp_path)
    root = tmp_path / "store"
    rows = _seed(root, storage_format=storage_format)
    history = gemini_history_path(root).with_suffix(".sqlite3" if storage_format == "sqlite" else ".parquet")
    if damage == "duplicate_key":
        rows.append(dict(rows[0]))
    elif damage == "duplicate_index":
        rows[1]["message_index"] = 0
        rows[1]["message_key"] = "example-session:0:assistant"
    elif damage == "hash":
        rows[0]["content_sha256"] = "incorrect"
    if damage == "schema":
        write_parquet_rows_atomic(history, rows, GEMINI_HISTORY_SCHEMA.remove(GEMINI_HISTORY_SCHEMA.get_field_index("model_label")))
    else:
        write_parquet_rows_atomic(history, rows, GEMINI_HISTORY_SCHEMA)
    before = history.read_bytes()
    with pytest.raises(importer.TakeoutImportError):
        _plan(source, root)
    assert history.read_bytes() == before
    assert not (gemini_history_path(root).parent / "imports").exists()


@pytest.mark.parametrize("reference", ["../private.txt", "%2e%2e/private.txt", "/private.txt", "folder\\private.txt"])
def test_attachment_traversal_is_rejected(tmp_path, reference):
    source = _source(tmp_path, _card(attachments=f'<a href="{reference}">file</a>'))
    with pytest.raises(importer.TakeoutImportError, match="Unsafe local"):
        _plan(source, tmp_path / "store")


def test_source_symlink_and_output_recursion_are_rejected(tmp_path):
    source = _source(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("private")
    (source / "linked").symlink_to(outside)
    with pytest.raises(importer.TakeoutImportError, match="Symlinks"):
        _plan(source, tmp_path / "store")
    (source / "linked").unlink()
    with pytest.raises(importer.TakeoutImportError, match="separate directory"):
        _plan(source, source / "store")
    with pytest.raises(importer.TakeoutImportError, match="separate directory"):
        _plan(source, tmp_path)


@pytest.mark.parametrize("target", ["root", "ancestor", "imports", "bundle", "raw", "raw_file", "lock"])
def test_output_symlinks_are_rejected_before_lock_or_writes(tmp_path, target):
    source = _source(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "store"
    if target in {"root", "ancestor"}:
        root.symlink_to(outside, target_is_directory=True)
        requested = root if target == "root" else root / "nested"
        with pytest.raises(importer.TakeoutImportError, match="Symlinks"):
            _plan(source, requested)
        assert list(outside.iterdir()) == []
        return
    plan = _plan(source, root)
    bundle = gemini_history_path(root).parent / "imports" / f"takeout-{plan.catalog['source_digest']}"
    links = {"imports": bundle.parent, "bundle": bundle, "raw": bundle / "source",
             "raw_file": bundle / "source" / "MyActivity.html", "lock": root / ".cache_task.lock"}
    link = links[target]
    link.parent.mkdir(parents=True, exist_ok=True)
    if target in {"raw_file", "lock"}:
        sentinel = outside / "sentinel"
        sentinel.write_bytes(b"untouched")
        link.symlink_to(sentinel)
    else:
        link.symlink_to(outside, target_is_directory=True)
    before = {path.relative_to(outside): path.read_bytes() for path in outside.rglob("*") if path.is_file()}
    with pytest.raises(importer.TakeoutImportError, match="Symlinks"):
        importer.apply_import(plan)
    assert before == {path.relative_to(outside): path.read_bytes() for path in outside.rglob("*") if path.is_file()}
    assert not gemini_history_path(root).exists()


def test_source_ancestor_symlink_is_rejected(tmp_path):
    source = _source(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(source, target_is_directory=True)
    with pytest.raises(importer.TakeoutImportError, match="Symlinks"):
        _plan(alias / "MyActivity.html", tmp_path / "store")


@pytest.mark.parametrize("card", [
    _card(ids=(), prompt="Quoted https://gemini.google.com/app/quoted-session"),
    _card(date="unparseable clock"),
    _card().replace('</div></div>', '</section></div>'),
    _card(reply='<div class="content-cell mdl-typography--caption"><a href="https://gemini.google.com/app/body-session">quoted</a></div>'),
])
def test_unknown_or_forged_structure_fails_closed_or_uses_only_dedicated_caption(tmp_path, card):
    source = _source(tmp_path, card)
    if "body-session" in card:
        plan = _plan(source, tmp_path / "store")
        assert {row["conversation_id"] for row in plan.rows} == {"example-session"}
    else:
        with pytest.raises(importer.TakeoutImportError):
            _plan(source, tmp_path / "store")


def test_exact_crlf_raw_html_provenance_and_explicit_timezone(tmp_path):
    source = _source(tmp_path, _card(prompt="first\r\nsecond", reply="<pre>a\r\nb</pre>"))
    catalog = importer.parse_takeout(source, timezone="Asia/Taipei")
    event = catalog["events"][0]
    raw = (source / "MyActivity.html").read_bytes().decode()
    assert raw[event["source_char_start"]:event["source_char_end"]] == event["raw_html"]
    assert "\r\n" in event["raw_html"]
    assert event["timestamp_precision"] == "second"
    with pytest.raises(TypeError):
        importer.parse_takeout(source)


def test_ambiguous_dst_clock_is_not_guessed(tmp_path):
    source = _source(tmp_path, _card(date="Nov 1, 2026, 1:30:00 AM CST"))
    with pytest.raises(importer.TakeoutImportError, match="Ambiguous or nonexistent"):
        importer.parse_takeout(source, timezone="America/New_York")


def test_sync_file_uses_a_windows_syncable_handle_without_changing_backup_bytes(tmp_path, monkeypatch):
    backup = tmp_path / "history-before.sqlite3"
    original = b"exact backup bytes\x00\xff"
    backup.write_bytes(original)
    open_file = Path.open
    fsync = importer.os.fsync
    handles = []
    directories = []

    def tracked_open(path, *args, **kwargs):
        handle = open_file(path, *args, **kwargs)
        if path == backup:
            handles.append(handle)
        return handle

    def windows_fsync(descriptor):
        owned = next(handle for handle in handles if not handle.closed and handle.fileno() == descriptor)
        if not owned.writable():
            raise OSError(errno.EBADF, "Windows cannot fsync this read-only file descriptor")
        return fsync(descriptor)

    def sync_directory(path):
        assert all(handle.closed for handle in handles)
        directories.append(path)

    monkeypatch.setattr(Path, "open", tracked_open)
    monkeypatch.setattr(importer.os, "fsync", windows_fsync)
    monkeypatch.setattr(importer, "_sync_directory", sync_directory)
    importer._sync_file(backup)
    assert handles and all(handle.closed for handle in handles)
    assert directories == [backup.parent]
    assert backup.read_bytes() == original


def test_backup_fsync_failure_propagates_without_publishing_or_changing_source(tmp_path, monkeypatch):
    source = _source(tmp_path, _card(reply="<p>Supplemental answer</p>"))
    root = tmp_path / "store"
    _seed(root)
    history = gemini_history_path(root)
    before = history.read_bytes()
    source_before = {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()}
    plan = _plan(source, root)
    open_file = Path.open
    fsync = importer.os.fsync
    handles = []

    def tracked_open(path, *args, **kwargs):
        handle = open_file(path, *args, **kwargs)
        if path.name == "history-before.sqlite3":
            handles.append(handle)
        return handle

    def failed_backup_fsync(descriptor):
        if any(not handle.closed and handle.fileno() == descriptor for handle in handles):
            raise OSError(errno.EIO, "Synthetic backup sync failure")
        return fsync(descriptor)

    monkeypatch.setattr(Path, "open", tracked_open)
    monkeypatch.setattr(importer.os, "fsync", failed_backup_fsync)
    with pytest.raises(OSError, match="Synthetic backup sync failure"):
        importer.apply_import(plan)
    assert handles and all(handle.closed for handle in handles)
    assert history.read_bytes() == before
    bundle = history.parent / "imports" / f"takeout-{plan.catalog['source_digest']}"
    assert (bundle / "history-before.sqlite3").read_bytes() == before
    assert not (bundle / "receipt.json").exists()
    assert source_before == {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()}




def test_private_bundle_boundary_is_verified_before_source_bytes_are_copied(tmp_path, monkeypatch):
    source = _source(tmp_path)
    root = tmp_path / "store"
    plan = _plan(source, root)
    copied = []
    copy = importer.shutil.copyfileobj

    def checked_copy(incoming, output, *args, **kwargs):
        target = Path(output.name)
        assert owner_only_problem(target) == ""
        assert owner_only_directory_problem(target.parent) == ""
        copied.append(target)
        return copy(incoming, output, *args, **kwargs)

    monkeypatch.setattr(importer.shutil, "copyfileobj", checked_copy)
    result = importer.apply_import(plan)
    assert copied
    assert all(owner_only_problem(path) == "" for path in copied)
    assert owner_only_directory_problem(Path(result["bundle"])) == ""


def test_unverifiable_private_bundle_fails_before_private_content_is_written(tmp_path, monkeypatch):
    source = _source(tmp_path)
    root = tmp_path / "store"
    plan = _plan(source, root)
    source_before = {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()}

    def reject_directory(_path):
        raise OwnerOnlyFileError("Synthetic native directory boundary failure")

    monkeypatch.setattr(importer, "ensure_owner_only_directory", reject_directory)
    with pytest.raises(OwnerOnlyFileError, match="Synthetic native directory boundary failure"):
        importer.apply_import(plan)
    bundle = gemini_history_path(root).parent / "imports" / f"takeout-{plan.catalog['source_digest']}"
    assert bundle.is_dir()
    assert list(bundle.iterdir()) == []
    assert not gemini_history_path(root).exists()
    assert source_before == {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()}


def test_legacy_parquet_preview_and_sqlite_publication_preserve_exact_source_and_backup(tmp_path):
    source = _source(tmp_path, _card(reply="<p>New recovered response</p>"))
    root = tmp_path / "store"
    old_rows = _seed(root, storage_format="parquet")
    history = gemini_history_path(root)
    legacy = history.with_suffix(".parquet")
    original = legacy.read_bytes()
    plan = _plan(source, root)
    assert plan.rows[:len(old_rows)] == old_rows
    assert not history.exists()
    assert legacy.read_bytes() == original
    result = importer.apply_import(plan)
    bundle = Path(result["bundle"])
    assert legacy.read_bytes() == (bundle / "history-before.parquet").read_bytes() == original
    assert history.read_bytes().startswith(b"SQLite format 3\x00")
    assert read_resource_rows(history) == plan.rows
    assert read_sqlite_schema(history).equals(GEMINI_HISTORY_SCHEMA, check_metadata=True)
    assert read_sqlite_summaries(history)
    receipt = json.loads((bundle / "receipt.json").read_text())
    assert receipt["version"] == "v2.0.0-codex.0"
    assert receipt["baseline_format"] == "parquet"
    assert receipt["candidate_format"] == "sqlite"
    assert receipt["canonical_baseline_sha256"] is None
    before_history = history.read_bytes()
    before_receipt = (bundle / "receipt.json").read_bytes()
    assert importer.apply_import(_plan(source, root))["added_rows"] == 0
    assert history.read_bytes() == before_history
    assert (bundle / "receipt.json").read_bytes() == before_receipt


@pytest.mark.parametrize("change", ["legacy_bytes", "canonical_appeared"])
def test_legacy_fallback_compare_and_swap_checks_both_authorities(tmp_path, change):
    source = _source(tmp_path)
    root = tmp_path / "store"
    _seed(root, storage_format="parquet")
    plan = _plan(source, root)
    history = gemini_history_path(root)
    changed = history if change == "canonical_appeared" else history.with_suffix(".parquet")
    _seed(root, reply="Concurrent writer", storage_format="sqlite" if change == "canonical_appeared" else "parquet")
    before = changed.read_bytes()
    with pytest.raises(importer.TakeoutImportError, match="changed after preview"):
        importer.apply_import(plan)
    assert changed.read_bytes() == before
    assert not (history.parent / "imports").exists()


@pytest.mark.parametrize("change", ["legacy_bytes", "canonical_appeared"])
def test_legacy_and_canonical_changes_during_staging_refuse_publication(tmp_path, monkeypatch, change):
    source = _source(tmp_path)
    root = tmp_path / "store"
    _seed(root, storage_format="parquet")
    plan = _plan(source, root)
    write = importer.write_sqlite_rows
    changed_bytes = []

    def change_after_candidate(*args, **kwargs):
        write(*args, **kwargs)
        _seed(root, reply="Concurrent writer", storage_format="sqlite" if change == "canonical_appeared" else "parquet")
        path = gemini_history_path(root).with_suffix(".sqlite3" if change == "canonical_appeared" else ".parquet")
        changed_bytes.append((path, path.read_bytes()))

    monkeypatch.setattr(importer, "write_sqlite_rows", change_after_candidate)
    with pytest.raises(importer.TakeoutImportError, match="changed during staging"):
        importer.apply_import(plan)
    assert changed_bytes and all(path.read_bytes() == body for path, body in changed_bytes)
    if change == "legacy_bytes":
        assert not gemini_history_path(root).exists()


def test_absent_canonical_publication_does_not_clobber_a_last_instant_writer(tmp_path, monkeypatch):
    source = _source(tmp_path)
    root = tmp_path / "store"
    history = gemini_history_path(root)
    _seed(root, storage_format="parquet")
    plan = _plan(source, root)
    competitor = tmp_path / "competing-store"
    _seed(competitor, reply="Concurrent authoritative SQLite")
    competing_bytes = gemini_history_path(competitor).read_bytes()
    link = importer.os.link

    def race(incoming, destination, *args, **kwargs):
        if Path(destination) == history:
            history.write_bytes(competing_bytes)
        return link(incoming, destination, *args, **kwargs)

    monkeypatch.setattr(importer.os, "link", race)
    with pytest.raises(FileExistsError):
        importer.apply_import(plan)
    assert history.read_bytes() == competing_bytes


def test_interrupted_initial_publication_link_is_reconciled_without_rewriting_history(tmp_path, monkeypatch):
    source = _source(tmp_path)
    root = tmp_path / "store"
    unlink = Path.unlink

    def fail_candidate_unlink(path, *args, **kwargs):
        if path.name == "history-candidate.sqlite3":
            raise OSError("Synthetic link publication interruption")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_candidate_unlink)
    with pytest.raises(OSError, match="Synthetic link publication interruption"):
        importer.apply_import(_plan(source, root))
    history = gemini_history_path(root)
    before = history.read_bytes()
    candidate = next(history.parent.glob("imports/*/history-candidate.sqlite3"))
    assert candidate.samefile(history)
    monkeypatch.setattr(Path, "unlink", unlink)
    replay = _plan(source, root)
    assert replay.summary["already_imported"]
    assert importer.apply_import(replay)["added_rows"] == 0
    assert history.read_bytes() == before
    assert not candidate.exists()
    assert owner_only_problem(history) == ""


@pytest.mark.parametrize("state", ["committed", "prepared", "published"])
def test_legacy_receipts_replay_and_recover_in_their_original_format(tmp_path, state):
    source = _source(tmp_path, _card(reply="<p>Legacy recovered response</p>"))
    root = tmp_path / "store"
    bundle, expected, original = _legacy_receipt(source, root, state)
    receipt_path = bundle / "receipt.json"
    before_receipt = receipt_path.read_bytes()
    legacy = gemini_history_path(root).with_suffix(".parquet")
    before_history = legacy.read_bytes()
    preserved = {name: (bundle / name).read_bytes() for name in (
        "history-before.parquet", "events.json", "provenance.json", "source/MyActivity.html",
    )}
    plan = _plan(source, root)
    assert plan.summary["resuming_prepared_import"] == (state == "prepared")
    assert importer.apply_import(plan)["added_rows"] == (1 if state == "prepared" else 0)
    assert not gemini_history_path(root).exists()
    assert read_resource_rows(legacy) == expected
    assert read_resource_rows(bundle / "history-before.parquet") == original
    assert all((bundle / name).read_bytes() == body for name, body in preserved.items())
    if state == "committed":
        assert receipt_path.read_bytes() == before_receipt
        assert legacy.read_bytes() == before_history
    receipt = json.loads(receipt_path.read_text())
    assert receipt["version"] == "v1.0.0-codex.0"
    assert receipt["state"] == "committed"


def test_legacy_receipt_after_an_unrelated_sqlite_migration_fails_closed(tmp_path):
    source = _source(tmp_path, _card(reply="<p>Legacy recovered response</p>"))
    root = tmp_path / "store"
    bundle, rows, _original = _legacy_receipt(source, root, "committed")
    history = gemini_history_path(root)
    write_parquet_rows_atomic(history, rows, GEMINI_HISTORY_SCHEMA)
    before = history.read_bytes()
    before_receipt = (bundle / "receipt.json").read_bytes()
    with pytest.raises(importer.TakeoutImportError, match="changed history"):
        _plan(source, root)
    assert history.read_bytes() == before
    assert (bundle / "receipt.json").read_bytes() == before_receipt


def test_invalid_canonical_sqlite_never_falls_back_to_valid_legacy_parquet(tmp_path):
    source = _source(tmp_path)
    root = tmp_path / "store"
    _seed(root, storage_format="parquet")
    history = gemini_history_path(root)
    legacy_before = history.with_suffix(".parquet").read_bytes()
    history.write_bytes(b"Synthetic corrupt canonical SQLite")
    before = history.read_bytes()
    with pytest.raises(importer.TakeoutImportError, match="cannot be verified"):
        _plan(source, root)
    assert history.read_bytes() == before
    assert history.with_suffix(".parquet").read_bytes() == legacy_before
    assert not (history.parent / "imports").exists()


@pytest.mark.parametrize("sidecar", ["-wal", "-shm", "-journal"])
def test_sqlite_sidecar_state_is_never_silently_omitted_from_a_byte_backup(tmp_path, sidecar):
    source = _source(tmp_path)
    root = tmp_path / "store"
    _seed(root)
    history = gemini_history_path(root)
    before = history.read_bytes()
    Path(f"{history}{sidecar}").write_bytes(b"Synthetic pending SQLite state")
    with pytest.raises(importer.TakeoutImportError, match="active sidecar"):
        _plan(source, root)
    assert history.read_bytes() == before
    assert not (history.parent / "imports").exists()


@pytest.mark.parametrize("field,value", [("candidate_format", "parquet"), ("candidate_format", "../escape"), ("baseline_format", "unknown")])
def test_receipt_format_discriminators_are_a_closed_contract(tmp_path, field, value):
    source = _source(tmp_path)
    root = tmp_path / "store"
    result = importer.apply_import(_plan(source, root))
    receipt_path = Path(result["bundle"]) / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt[field] = value
    receipt_path.write_bytes(importer._json_bytes(receipt))
    before = gemini_history_path(root).read_bytes()
    with pytest.raises(importer.TakeoutImportError, match="receipt format"):
        _plan(source, root)
    assert gemini_history_path(root).read_bytes() == before
