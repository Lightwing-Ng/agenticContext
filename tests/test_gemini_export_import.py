"""Synthetic source, preservation, and crash-recovery tests for Takeout imports."""

# Code version: v1.0.0-codex.0

from __future__ import annotations

from html import escape
import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from app.core import gemini_export_import as importer
from app.core.gemini_downloader import GeminiConversationLink, GeminiHistoryStore, gemini_history_path
from app.core.job_lock import CacheTaskLock, cache_task_resource_lock_path
from app.core.resource_persistence import GEMINI_HISTORY_SCHEMA, write_parquet_rows_atomic


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


def _seed(root, prompt="Question", reply="Answer", timestamp="2026-01-02T13:30:13.123456Z"):
    store = GeminiHistoryStore(gemini_history_path(root))
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
    assert pq.read_table(gemini_history_path(root)).to_pylist() == original
    backup = Path(result["bundle"]) / "history-before.parquet"
    assert pq.read_table(backup).to_pylist() == original


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
    assert (bundle.stat().st_mode & 0o077) == 0


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
        assert pq.read_table(gemini_history_path(root)).to_pylist() == original
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
    assert pq.read_table(gemini_history_path(root)).to_pylist() == original
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
        if Path(incoming).name == "history-candidate.parquet":
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
    assert pq.read_table(gemini_history_path(root)).to_pylist() == expected_rows
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


@pytest.mark.parametrize("member", ["source/MyActivity.html", "events.json", "provenance.json", "history-before.parquet"])
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
def test_corrupt_existing_history_fails_closed_before_writes(tmp_path, damage):
    source = _source(tmp_path)
    root = tmp_path / "store"
    rows = _seed(root)
    if damage == "duplicate_key":
        rows.append(dict(rows[0]))
    elif damage == "duplicate_index":
        rows[1]["message_index"] = 0
        rows[1]["message_key"] = "example-session:0:assistant"
    elif damage == "hash":
        rows[0]["content_sha256"] = "incorrect"
    if damage == "schema":
        pq.write_table(pq.read_table(gemini_history_path(root)).drop(["model_label"]), gemini_history_path(root))
    else:
        write_parquet_rows_atomic(gemini_history_path(root), rows, GEMINI_HISTORY_SCHEMA)
    before = gemini_history_path(root).read_bytes()
    with pytest.raises(importer.TakeoutImportError):
        _plan(source, root)
    assert gemini_history_path(root).read_bytes() == before
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
