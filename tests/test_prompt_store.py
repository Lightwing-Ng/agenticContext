"""Tests for snapshot-backed prompt bookmarks and browser controls."""

# Code version: v1.2.0-claude.0

import json
from pathlib import Path

import pytest

from app.core.local_media_browser import stable_media_id
from app.core.prompt_store import PromptStore, prompt_content_key
from app.core.resource_persistence import (
    GEMINI_HISTORY_SCHEMA,
    PROMPT_LEGACY_SCHEMA,
    PROMPT_SCHEMA,
    write_parquet_rows_atomic,
)
from app.web.app import create_app


def _history_row(
    conversation_id: str,
    message_key: str,
    content_text: str,
    *,
    role: str = "user",
    last_seen_at: str = "2026-08-20T06:00:00Z",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "platform": "chatgpt",
        "conversation_id": conversation_id,
        "conversation_url": f"https://chatgpt.com/c/{conversation_id}",
        "conversation_title": "Prompt demo",
        "message_key": message_key,
        "turn_index": 0,
        "message_index": 0,
        "role": role,
        "author_label": "You" if role == "user" else "ChatGPT",
        "content_text": content_text,
        "content_html": "",
        "content_sha256": "hash",
        "source_links": [],
        "model_label": "",
        "first_seen_at": last_seen_at,
        "last_seen_at": last_seen_at,
    }


def _write_history(root: Path, content_text: str) -> None:
    write_parquet_rows_atomic(
        root / "llm" / "chatgpt" / "history.parquet",
        [_history_row("demo", "demo:user:0", content_text)],
        GEMINI_HISTORY_SCHEMA,
    )


def test_prompt_store_captures_a_snapshot_and_survives_history_deletion(tmp_path: Path) -> None:
    _write_history(tmp_path, "First prompt")
    store = PromptStore(tmp_path)

    saved, created = store.add_pointer(
        source="chatgpt",
        conversation_id="demo",
        message_key="demo:user:0",
    )
    duplicate, duplicate_created = store.add_pointer(
        source="chatgpt",
        conversation_id="demo",
        message_key="demo:user:0",
    )

    assert created is True
    assert duplicate_created is False
    assert saved.stable_id == duplicate.stable_id
    assert saved.content_text == "First prompt"
    assert saved.remarks == ()
    assert store.catalog_path == tmp_path / "prompt" / "prompts.parquet"

    import pyarrow.parquet as parquet

    table = parquet.read_table(store.catalog_path)
    assert table.num_rows == 1
    assert "content_text" in table.column_names
    assert table.column("content_text").to_pylist() == ["First prompt"]

    _write_history(tmp_path, "Updated prompt")
    refreshed = PromptStore(tmp_path).query().items[0]
    assert refreshed.content_text == "First prompt"

    (tmp_path / "llm" / "chatgpt" / "history.parquet").unlink()
    offline_store = PromptStore(tmp_path)
    deleted_remote_history = offline_store.query().items[0]
    assert deleted_remote_history.content_text == "First prompt"
    assert deleted_remote_history.conversation_url == "https://chatgpt.com/c/demo"
    remarked, created = offline_store.add_remark(deleted_remote_history.stable_id, "offline")
    assert created is True
    assert remarked.remarks == ("offline",)
    assert PromptStore(tmp_path).query().items[0].remarks == ("offline",)


def test_legacy_pointer_rows_are_upgraded_before_history_disappears(tmp_path: Path) -> None:
    _write_history(tmp_path, "Legacy prompt")
    write_parquet_rows_atomic(
        tmp_path / "prompt" / "prompts.parquet",
        [
            {
                "schema_version": 1,
                "source": "chatgpt",
                "conversation_id": "demo",
                "message_key": "demo:user:0",
                "added_at": "2026-08-20T06:00:00Z",
            }
        ],
        PROMPT_LEGACY_SCHEMA,
    )

    upgraded = PromptStore(tmp_path)
    assert upgraded.query().items[0].content_text == "Legacy prompt"

    import pyarrow.parquet as parquet

    table = parquet.read_table(upgraded.catalog_path)
    assert table.column("content_text").to_pylist() == ["Legacy prompt"]
    (tmp_path / "llm" / "chatgpt" / "history.parquet").unlink()
    assert PromptStore(tmp_path).query().items[0].content_text == "Legacy prompt"


def test_prompts_mode_renders_add_and_copy_controls(tmp_path: Path) -> None:
    _write_history(tmp_path, "Create a concise local summary.")
    app = create_app(tmp_path)
    client = app.test_client()

    response = client.post(
        "/api/browser/prompts",
        json={
            "source": "chatgpt",
            "conversation_id": "demo",
            "message_key": "demo:user:0",
        },
    )
    assert response.status_code == 200
    assert response.get_json()["created"] is True
    assert client.post(
        "/api/browser/prompts",
        json={
            "source": "chatgpt",
            "conversation_id": "demo",
            "message_key": "demo:user:0",
        },
    ).get_json()["created"] is False

    prompt_body = client.get("/browser?view=prompts").get_data(as_text=True)
    text_body = client.get(
        "/browser?view=text&source=chatgpt&session_view=1&session=chatgpt:demo"
    ).get_data(as_text=True)

    assert 'id="browser_view_prompts"' in prompt_body
    assert 'data-option-count="3"' in prompt_body
    assert "Saved prompts" in prompt_body
    assert 'class="browser-prompt-col-added">Saved</th>' not in prompt_body
    assert 'class="browser-prompt-col-source">Source</th>' in prompt_body
    assert 'class="browser-prompt-col-remarks">Remarks</th>' in prompt_body
    assert 'foundation-metric-card' in prompt_body
    assert 'data-prompt-copy' in prompt_body
    assert 'data-prompt-remark-add' not in prompt_body
    assert 'data-prompt-remark-options' in prompt_body
    assert 'list="browser_prompt_remark_options"' in prompt_body
    assert 'aria-autocomplete="list"' in prompt_body
    assert 'enterkeyhint="done"' in prompt_body
    assert 'data-prompt-remark-select' not in prompt_body
    assert 'data-prompt-text="Create a concise local summary."' in prompt_body
    assert 'data-prompt-add' in text_body
    assert client.get("/static/images/text.bubble.fill.svg").status_code == 200
    assert client.get("/static/images/text.bubble.badge.clock.fill.svg").status_code == 200
    assert 'aria-label="Added as prompt"' in text_body


def test_prompt_remarks_are_persisted_and_exposed_as_shared_options(tmp_path: Path) -> None:
    _write_history(tmp_path, "Remarkable prompt")
    app = create_app(tmp_path)
    client = app.test_client()

    saved_response = client.post(
        "/api/browser/prompts",
        json={
            "source": "chatgpt",
            "conversation_id": "demo",
            "message_key": "demo:user:0",
        },
    )
    prompt_id = saved_response.get_json()["item"]["id"]

    added_response = client.post(
        f"/api/browser/prompts/{prompt_id}/remarks",
        json={"remark": "  favorite  "},
    )
    assert added_response.status_code == 200
    assert added_response.get_json()["created"] is True
    assert added_response.get_json()["item"]["remarks"] == ["favorite"]
    assert added_response.get_json()["remark_options"] == ["favorite"]

    duplicate_response = client.post(
        f"/api/browser/prompts/{prompt_id}/remarks",
        json={"remark": "FAVORITE"},
    )
    assert duplicate_response.status_code == 200
    assert duplicate_response.get_json()["created"] is False

    prompt_body = client.get("/browser?view=prompts").get_data(as_text=True)
    assert 'data-prompt-remark="favorite"' in prompt_body
    assert '<datalist id="browser_prompt_remark_options" data-prompt-remark-options>' in prompt_body
    assert '<option value="favorite">favorite</option>' in prompt_body
    assert "favorite" in prompt_body

    removed_response = client.delete(
        f"/api/browser/prompts/{prompt_id}/remarks",
        json={"remark": "favorite"},
    )
    assert removed_response.status_code == 200
    assert removed_response.get_json()["item"]["remarks"] == []
    assert removed_response.get_json()["remark_options"] == []

    reloaded = PromptStore(tmp_path)
    assert reloaded.query().items[0].remarks == ()


def test_prompt_collection_is_limited_to_user_messages(tmp_path: Path) -> None:
    write_parquet_rows_atomic(
        tmp_path / "llm" / "chatgpt" / "history.parquet",
        [
            _history_row("demo", "demo:user:0", "User prompt", role="user"),
            _history_row("demo", "demo:assistant:1", "Assistant response", role="assistant"),
        ],
        GEMINI_HISTORY_SCHEMA,
    )
    store = PromptStore(tmp_path)

    with pytest.raises(ValueError, match="Only user messages"):
        store.add_pointer(
            source="chatgpt",
            conversation_id="demo",
            message_key="demo:assistant:1",
        )

    app = create_app(tmp_path)
    client = app.test_client()
    response = client.post(
        "/api/browser/prompts",
        json={
            "source": "chatgpt",
            "conversation_id": "demo",
            "message_key": "demo:assistant:1",
        },
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "Only user messages can be saved as prompts."

    write_parquet_rows_atomic(
        store.catalog_path,
        [
            {
                "schema_version": 1,
                "source": "chatgpt",
                "conversation_id": "demo",
                "message_key": "demo:assistant:1",
                "content_text": "",
                "conversation_title": "",
                "conversation_url": "",
                "author_label": "",
                "captured_at": "",
                "added_at": "2026-08-20T06:00:00Z",
            }
        ],
        PROMPT_SCHEMA,
    )
    assert PromptStore(tmp_path).query().items == ()

    text_body = client.get(
        "/browser?view=text&source=chatgpt&session_view=1&session=chatgpt:demo"
    ).get_data(as_text=True)
    assert text_body.count('class="browser-prompt-add-button') == 1
    assert 'Assistant response' in text_body


def _write_chatgpt_media(root: Path, images: dict[str, tuple[str, str]]) -> None:
    """Seed ChatGPT images as {filename: (conversation id, prompt)}."""
    project_dir = root / "media" / "chatgpt" / "demo-project"
    project_dir.mkdir(parents=True)
    entries: dict[str, dict[str, str]] = {}
    for image_number, (filename, (conversation_id, prompt)) in enumerate(images.items(), start=1):
        (project_dir / filename).write_bytes(filename.encode("utf-8"))
        entries[f"file-{filename}"] = {
            "file_id": f"file-{filename}",
            "relative_path": filename,
            "conversation_url": f"https://chatgpt.com/c/{conversation_id}",
            "conversation_title": f"Media {conversation_id}",
            "prompt_markdown": prompt,
            "created_at": f"2026-08-{image_number:02d}T00:00:00Z",
        }
    (project_dir / ".chatgpt_catalog.json").write_text(
        json.dumps({"entries": entries}),
        encoding="utf-8",
    )


def _media_id(filename: str) -> str:
    return stable_media_id(f"media/chatgpt/demo-project/{filename}")


def test_media_prompt_saves_the_cached_user_turn_when_the_history_has_it(tmp_path: Path) -> None:
    _write_history(tmp_path, "Draw a lighthouse.")
    store = PromptStore(tmp_path)

    saved, created = store.add_media_prompt(
        source="chatgpt",
        conversation_id="demo",
        prompt_text="Draw  a lighthouse.\n",
        conversation_title="Media title",
        conversation_url="https://chatgpt.com/c/demo",
        captured_at="2026-08-21T00:00:00Z",
    )
    again, created_again = store.add_media_prompt(
        source="chatgpt", conversation_id="demo", prompt_text="Draw a lighthouse.",
    )
    from_text, created_from_text = store.add_pointer(
        source="chatgpt", conversation_id="demo", message_key="demo:user:0",
    )

    # The media prompt is that conversation's own user turn, so both entry points share one row.
    assert created is True
    assert (created_again, created_from_text) == (False, False)
    assert saved.stable_id == again.stable_id == from_text.stable_id
    assert saved.message_key == "demo:user:0"
    assert saved.content_text == "Draw a lighthouse."
    assert saved.conversation_title == "Prompt demo"
    assert store.saved_content_keys() == {prompt_content_key("chatgpt", "demo", "Draw a lighthouse.")}
    assert len(store.query().items) == 1


def test_media_prompt_without_cached_history_keeps_a_durable_snapshot(tmp_path: Path) -> None:
    store = PromptStore(tmp_path)

    saved, created = store.add_media_prompt(
        source="chatgpt",
        conversation_id="uncached",
        prompt_text="Draw a lighthouse.",
        conversation_title="Lighthouse session",
        conversation_url="https://chatgpt.com/c/uncached",
        captured_at="2026-08-21T00:00:00Z",
    )
    again, created_again = store.add_media_prompt(
        source="chatgpt", conversation_id="uncached", prompt_text=" Draw a  lighthouse. ",
    )
    other, created_other = store.add_media_prompt(
        source="chatgpt", conversation_id="another", prompt_text="Draw a lighthouse.",
    )

    assert (created, created_again, created_other) == (True, False, True)
    assert saved.stable_id == again.stable_id != other.stable_id
    assert saved.message_key.startswith("media-prompt:")
    assert (saved.content_text, saved.author_label) == ("Draw a lighthouse.", "You")
    assert saved.conversation_title == "Lighthouse session"
    assert saved.conversation_url == "https://chatgpt.com/c/uncached"
    assert saved.captured_at == "2026-08-21T00:00:00Z"
    # The same text saved from another conversation is a second prompt, flagged on both rows.
    assert other.duplicate_count == 2

    reloaded = PromptStore(tmp_path)
    assert {item.stable_id: item.duplicate_count for item in reloaded.query().items} == {
        saved.stable_id: 2,
        other.stable_id: 2,
    }
    remarked, _ = reloaded.add_remark(saved.stable_id, "snapshot")
    assert remarked.remarks == ("snapshot",)

    with pytest.raises(ValueError, match="no indexed prompt"):
        store.add_media_prompt(source="chatgpt", conversation_id="uncached", prompt_text="  ")
    with pytest.raises(ValueError, match="source conversation"):
        store.add_media_prompt(source="chatgpt", conversation_id="", prompt_text="Draw a lighthouse.")


def test_removed_prompt_drops_its_remarks_and_is_restorable_by_the_same_process(tmp_path: Path) -> None:
    write_parquet_rows_atomic(
        tmp_path / "llm" / "chatgpt" / "history.parquet",
        [
            _history_row("first", "first:user:0", "Shared prompt"),
            _history_row("second", "second:user:0", "Shared  prompt"),
            _history_row("third", "third:user:0", "Unique prompt"),
        ],
        GEMINI_HISTORY_SCHEMA,
    )
    store = PromptStore(tmp_path)
    first, second, third = (
        store.add_pointer(source="chatgpt", conversation_id=name, message_key=f"{name}:user:0")[0]
        for name in ("first", "second", "third")
    )
    store.add_remark(first.stable_id, "keep")

    assert {item.stable_id: item.duplicate_count for item in store.query().items} == {
        first.stable_id: 2,
        second.stable_id: 2,
        third.stable_id: 1,
    }
    assert first.text_key == second.text_key != third.text_key

    assert store.remove(first.stable_id) == 1
    assert {item.stable_id: item.duplicate_count for item in store.query().items} == {
        second.stable_id: 1,
        third.stable_id: 1,
    }
    assert store.remark_options() == ()
    persisted = PromptStore(tmp_path)
    assert len(persisted.query().items) == 2
    assert persisted.remark_options() == ()
    with pytest.raises(LookupError, match="was not found"):
        store.remove(first.stable_id)
    # Another process never saw the removal, so it cannot undo it.
    with pytest.raises(LookupError, match="can no longer be restored"):
        persisted.restore(first.stable_id)

    restored = store.restore(first.stable_id)
    assert restored.stable_id == first.stable_id
    assert restored.added_at == first.added_at
    assert (restored.remarks, restored.duplicate_count) == (("keep",), 2)
    assert {item.stable_id for item in PromptStore(tmp_path).query().items} == {
        first.stable_id,
        second.stable_id,
        third.stable_id,
    }
    assert PromptStore(tmp_path).remark_options() == ("keep",)
    with pytest.raises(LookupError, match="can no longer be restored"):
        store.restore(first.stable_id)


def test_media_cards_save_their_prompt_once_per_conversation(tmp_path: Path) -> None:
    _write_history(tmp_path, "Draw a lighthouse.")
    _write_chatgpt_media(
        tmp_path,
        {
            "cached-a.png": ("demo", "Draw a lighthouse."),
            "cached-b.png": ("demo", "Draw  a lighthouse.\n"),
            "uncached.png": ("other", "Draw a lighthouse."),
            "unindexed.png": ("other", ""),
        },
    )
    app = create_app(tmp_path)
    client = app.test_client()
    gallery_url = "/browser?view=media&source=chatgpt&session_view=0"

    body = client.get(gallery_url).get_data(as_text=True)
    assert body.count("data-prompt-add\n") == 3
    assert body.count('aria-label="Add as prompt"') == 3
    assert "browser-prompt-add-button is-added" not in body
    assert body.count("Not indexed") == 1
    # The save action is the standard Circular icon button inside the prompt heading.
    assert (
        'class="circular-icon-button browser-media-round-action browser-prompt-add-button"\n'
        "                                            data-prompt-add\n"
        f'                                            data-prompt-media-id="{_media_id("uncached.png")}"'
    ) in body
    shared_key = prompt_content_key("chatgpt", "demo", "Draw a lighthouse.")
    assert body.count(f'data-prompt-content-key="{shared_key}"') == 2

    cached = client.post("/api/browser/prompts", json={"media_id": _media_id("cached-a.png")})
    sibling = client.post("/api/browser/prompts", json={"media_id": _media_id("cached-b.png")})
    uncached = client.post("/api/browser/prompts", json={"media_id": _media_id("uncached.png")})
    unindexed = client.post("/api/browser/prompts", json={"media_id": _media_id("unindexed.png")})
    missing = client.post("/api/browser/prompts", json={"media_id": "media-missing"})

    assert (cached.status_code, cached.get_json()["created"]) == (200, True)
    assert cached.get_json()["item"]["message_key"] == "demo:user:0"
    assert (sibling.status_code, sibling.get_json()["created"]) == (200, False)
    assert sibling.get_json()["item"]["id"] == cached.get_json()["item"]["id"]
    uncached_item = uncached.get_json()["item"]
    assert (uncached.status_code, uncached.get_json()["created"]) == (200, True)
    assert uncached_item["message_key"].startswith("media-prompt:")
    assert uncached_item["conversation_title"] == "Media other"
    assert uncached_item["conversation_url"] == "https://chatgpt.com/c/other"
    assert uncached_item["duplicate_count"] == 2
    assert (unindexed.status_code, unindexed.get_json()["error"]) == (
        400,
        "This media item has no indexed prompt.",
    )
    assert (missing.status_code, missing.get_json()["error"]) == (404, "Cached media was not found.")

    saved_body = client.get(gallery_url).get_data(as_text=True)
    assert saved_body.count("browser-prompt-add-button is-added") == 3
    assert saved_body.count('aria-label="Added as prompt"') == 3
    # The Text view shows the same user turn as saved.
    text_body = client.get(
        "/browser?view=text&source=chatgpt&session_view=1&session=chatgpt:demo"
    ).get_data(as_text=True)
    assert 'aria-label="Added as prompt"' in text_body


def test_saved_prompt_rows_flag_duplicates_and_toggle_saving(tmp_path: Path) -> None:
    write_parquet_rows_atomic(
        tmp_path / "llm" / "chatgpt" / "history.parquet",
        [
            _history_row("first", "first:user:0", "Shared prompt"),
            _history_row("second", "second:user:0", "Shared prompt"),
            _history_row("third", "third:user:0", "Unique prompt"),
        ],
        GEMINI_HISTORY_SCHEMA,
    )
    app = create_app(tmp_path)
    client = app.test_client()
    prompt_ids = [
        client.post(
            "/api/browser/prompts",
            json={"source": "chatgpt", "conversation_id": name, "message_key": f"{name}:user:0"},
        ).get_json()["item"]["id"]
        for name in ("first", "second", "third")
    ]
    client.post(f"/api/browser/prompts/{prompt_ids[0]}/remarks", json={"remark": "keep"})

    def duplicate_marks(body: str) -> dict[str, str]:
        """Return each row's duplicate label by prompt, or an empty string when its mark is hidden."""
        marks = {}
        for row in body.split('<tr data-prompt-id="')[1:]:
            attributes = row.split("data-prompt-duplicate\n", 1)[1].split(">", 1)[0]
            label = attributes.split('aria-label="', 1)[1].split('"', 1)[0]
            marks[row.split('"', 1)[0]] = "" if "hidden" in attributes.split() else label
        return marks

    body = client.get("/browser?view=prompts").get_data(as_text=True)
    assert body.count("data-prompt-unsave\n") == 3
    assert body.count('aria-label="Remove from saved prompts"') == 3
    assert duplicate_marks(body) == {
        prompt_ids[0]: "Duplicate prompt: saved 2 times",
        prompt_ids[1]: "Duplicate prompt: saved 2 times",
        prompt_ids[2]: "",
    }
    assert '<strong data-prompt-total data-numeric-display-value="3">3</strong>' in body
    assert body.count("data-prompt-text-key=") == 3
    for asset in ("bookmark.slash.fill.svg", "rectangle.on.rectangle.svg"):
        assert client.get(f"/static/images/{asset}").status_code == 200

    removed = client.delete(f"/api/browser/prompts/{prompt_ids[0]}")
    assert removed.status_code == 200
    assert removed.get_json() == {"removed": True, "duplicate_count": 1, "remark_options": []}
    assert client.delete(f"/api/browser/prompts/{prompt_ids[0]}").status_code == 404
    after_removal = client.get("/browser?view=prompts").get_data(as_text=True)
    assert duplicate_marks(after_removal) == {prompt_ids[1]: "", prompt_ids[2]: ""}
    assert '<strong data-prompt-total data-numeric-display-value="2">2</strong>' in after_removal

    restored = client.post(f"/api/browser/prompts/{prompt_ids[0]}/restore")
    assert restored.status_code == 200
    assert restored.get_json()["item"]["id"] == prompt_ids[0]
    assert restored.get_json()["item"]["remarks"] == ["keep"]
    assert restored.get_json()["item"]["duplicate_count"] == 2
    assert restored.get_json()["remark_options"] == ["keep"]
    again = client.post(f"/api/browser/prompts/{prompt_ids[0]}/restore")
    assert (again.status_code, again.get_json()["error"]) == (
        404,
        "The removed prompt can no longer be restored.",
    )
    assert len(PromptStore(tmp_path).query().items) == 3
