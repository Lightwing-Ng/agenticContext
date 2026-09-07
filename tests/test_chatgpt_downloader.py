"""Focused tests for ChatGPT project image caching."""

# Code version: v1.41.0-codex.1

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path
from queue import Queue
from threading import Event, Lock
from unittest.mock import patch

import pytest
from PIL import Image

from app.core.browser_sessions import probe_browser_session
from app.core.chatgpt_downloader import (
    ChatGPTHistoryStore,
    ChatGPTImageSizeLimitError,
    ChatGPTImageCandidate,
    ChatGPTImageCatalog,
    ChatGPTCatalogEntry,
    chatgpt_conversation_id,
    chatgpt_target_dir,
    collect_chatgpt_project_index_images,
    collect_conversation_images,
    collect_project_conversation_urls,
    download_chatgpt_image,
    enrich_chatgpt_project_index_prompts,
    extract_chatgpt_file_id,
    infer_image_extension,
    is_chatgpt_conversation_url,
    looks_like_image,
    reset_chatgpt_state,
    should_cache_chatgpt_candidate,
    sync_chatgpt_images,
)
from app.core.resource_persistence import (
    CHATGPT_HISTORY_SCHEMA,
    CHATGPT_HISTORY_SCHEMA_VERSION,
    read_parquet_rows,
    write_parquet_rows_atomic,
)
from app.core.chatgpt_downloader import (
    ChatGPTConversationWorkResult,
    ChatGPTImageDownloadWorkResult,
    CHATGPT_HOME_URL,
    PlaywrightError,
    _chatgpt_file_download_url,
    _chatgpt_conversation_worker,
    _extract_chatgpt_conversation_messages,
    _chatgpt_index_image_worker,
    _collect_all_chatgpt_conversation_urls_via_api,
    _chatgpt_project_id,
    _extract_chatgpt_conversation_image_payloads,
    _get_chatgpt_api_json,
    _get_chatgpt_api_json_via_page,
    _is_unavailable_chatgpt_image_error,
    _is_retryable_chatgpt_image_error,
    _is_recoverable_chatgpt_page_error,
    _iter_chatgpt_index_image_results,
    _iter_chatgpt_conversation_results,
    _iter_parallel_safari_prompt_metadata_results,
    _load_chatgpt_session_request_headers,
    _merge_current_conversation_images,
    _merge_chatgpt_conversation_payload_images,
    _wait_for_project_conversation_links,
    cache_chatgpt_conversation_history,
)
from app.core.config import CrawlConfig, DEFAULT_CHATGPT_PROJECT_NAME, DEFAULT_CHATGPT_PROJECT_URL
from app.core.safari_automation import SafariContext, SafariPage
from app.core.state import TaskState


PNG_PAYLOAD = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360606060000000050001a5f645400000000049454e44ae426082"
)


def _visual_test_image_payload(image_format: str, *, quality: int | None = None) -> bytes:
    """Create one deterministic image payload for visual-deduplication coverage."""
    image = Image.new("RGB", (128, 96))
    image.putdata(
        [
            ((column * 37 + row * 11) % 256, (column * 7 + row * 29) % 256, (column * 19 + row * 5) % 256)
            for row in range(image.height)
            for column in range(image.width)
        ]
    )
    output = BytesIO()
    save_options = {"quality": quality} if quality is not None else {}
    image.save(output, format=image_format, **save_options)
    return output.getvalue()


@pytest.mark.parametrize("excluded", [
    {"channel": "analysis"},
    {"channel": "commentary"},
    {"channel": "summary"},
    {"metadata": {"channel": "commentary"}},
    {"recipient": "web.run"},
    {"recipient": "functions.search", "channel": "final"},
    {"end_turn": False},
    {"status": "in_progress"},
    {"status": "finished_partial"},
    {"metadata": {"is_visually_hidden_from_conversation": True}},
    {"content": {"content_type": "thoughts", "text": "Intermediate reasoning"}},
    {"content": {"content_type": "reasoning_recap", "text": "Reasoning summary"}},
    {"author": {"role": "tool"}},
])
def test_chatgpt_history_excludes_internal_messages_and_preserves_final_json(excluded: dict) -> None:
    final_text = '{"search_query": "This JSON is the requested final answer."}'
    payload = {"mapping": {
        "user": {"message": {
            "author": {"role": "user"},
            "content": {"content_type": "text", "parts": ["Return JSON"]},
        }},
        "internal": {"message": {
            "author": {"role": "assistant"},
            "content": {"content_type": "text", "parts": ["Checking sources"]},
            **excluded,
        }},
        "final": {"message": {
            "author": {"role": "assistant"},
            "recipient": "all", "channel": "final", "end_turn": True,
            "status": "finished_successfully",
            "content": {"content_type": "text", "parts": [final_text]},
        }},
    }}
    rows = _extract_chatgpt_conversation_messages(payload, "https://chatgpt.com/c/final-only", "now")
    assert [row["content_text"] for row in rows] == ["Return JSON", final_text]
    assert [row["message_index"] for row in rows] == [0, 1]
    assert [row["turn_index"] for row in rows] == [0, 0]


@pytest.mark.parametrize("content_type,hidden,expected", [
    ("text", False, 1),
    ("multimodal_text", False, 1),
    ("text", True, 0),
    ("user_editable_context", False, 0),
])
def test_chatgpt_history_preserves_user_json_but_excludes_hidden_context(
    content_type: str, hidden: bool, expected: int,
) -> None:
    payload = {"mapping": {"user": {"message": {
        "author": {"role": "user"},
        "metadata": {"is_visually_hidden_from_conversation": hidden},
        "content": {"content_type": content_type, "parts": ['{"search_query": []}']},
    }}}}
    rows = _extract_chatgpt_conversation_messages(payload, "https://chatgpt.com/c/user-json", "now")
    assert len(rows) == expected
    if expected:
        assert rows[0]["content_text"] == '{"search_query": []}'


@pytest.mark.parametrize("fetch_fails", [False, True])
def test_chatgpt_history_refilters_legacy_rows_even_with_matching_revision(
    tmp_path: Path, fetch_fails: bool,
) -> None:
    url = "https://chatgpt.com/c/legacy-filter"
    payload = {"mapping": {
        key: {"message": {
            "author": {"role": role}, "create_time": 1771059600,
            "content": {"parts": [key]},
        }}
        for key, role in (("user", "user"), ("progress", "assistant"), ("final", "assistant"))
    }}
    store = ChatGPTHistoryStore(tmp_path / "history.parquet")
    store.replace_conversation(url, payload, "2026-09-07T00:00:00Z", provider_revision="unchanged")
    store.save()
    legacy_rows = read_parquet_rows(store.path)
    for row in legacy_rows:
        row["schema_version"] = 2
    write_parquet_rows_atomic(store.path, legacy_rows, CHATGPT_HISTORY_SCHEMA)
    before = store.path.read_bytes()
    store = ChatGPTHistoryStore(store.path)
    payload["mapping"]["progress"]["message"]["channel"] = "commentary"
    payload["mapping"]["final"]["message"]["channel"] = "final"
    with patch(
        "app.core.chatgpt_downloader._get_chatgpt_api_json_via_page",
        return_value=payload,
        side_effect=RuntimeError("Unavailable") if fetch_fails else None,
    ) as fetch:
        result = cache_chatgpt_conversation_history(
            store, [url], object(), {}, TaskState("test"), lambda: False,
            conversation_revisions_by_id={"legacy-filter": "unchanged"},
        )
    fetch.assert_called_once()
    if fetch_fails:
        assert result == (0, 0, 0)
        assert store.path.read_bytes() == before
        return
    assert result == (1, 0, 0)
    rows = read_parquet_rows(store.path)
    assert [row["content_text"] for row in rows] == ["user", "final"]
    assert all(row["schema_version"] == CHATGPT_HISTORY_SCHEMA_VERSION for row in rows)
    assert all(row["first_seen_at"] == "2026-09-07T00:00:00Z" for row in rows)
    assert ChatGPTHistoryStore(store.path).conversation_revision_matches(url, "unchanged")


def test_chatgpt_history_cache_persists_visible_user_and_assistant_messages(tmp_path: Path) -> None:
    conversation_url = "https://chatgpt.com/c/session-complete"
    payload = {
        "title": "Complete session",
        "mapping": {
            "system-node": {
                "message": {"author": {"role": "system"}, "content": {"parts": ["ignored"]}}
            },
            "user-node": {
                "message": {
                    "author": {"role": "user"},
                    "create_time": 1771059600,
                    "content": {"parts": ["Please summarize this"]},
                }
            },
            "assistant-node": {
                "message": {
                    "author": {"role": "assistant"},
                    "create_time": 1771059900,
                    "content": {"parts": [{"text": "Here is the summary."}]},
                }
            },
        },
    }

    extracted = _extract_chatgpt_conversation_messages(
        payload,
        conversation_url,
        "2026-08-12T08:00:00Z",
    )
    assert [row["role"] for row in extracted] == ["user", "assistant"]
    assert [row["content_text"] for row in extracted] == [
        "Please summarize this",
        "Here is the summary.",
    ]
    assert [row["last_seen_at"] for row in extracted] == [
        "2026-02-14T09:00:00Z",
        "2026-02-14T09:05:00Z",
    ]

    history_store = ChatGPTHistoryStore(tmp_path / "llm" / "chatgpt" / "history.parquet")
    with patch(
        "app.core.chatgpt_downloader._get_chatgpt_api_json_via_page",
        return_value=payload,
    ):
        processed, new_messages, unchanged_sessions = cache_chatgpt_conversation_history(
            history_store,
            [conversation_url],
            object(),
            {},
            TaskState("test"),
            lambda: False,
        )

    rows = read_parquet_rows(history_store.path)
    assert processed == 1
    assert new_messages == 2
    assert unchanged_sessions == 0
    assert rows is not None
    assert [row["content_text"] for row in rows] == [
        "Please summarize this",
        "Here is the summary.",
    ]
    assert [row["last_seen_at"] for row in rows] == [
        "2026-02-14T09:00:00Z",
        "2026-02-14T09:05:00Z",
    ]


def test_chatgpt_history_cache_skips_only_matching_revisions(tmp_path: Path) -> None:
    conversation_url = "https://chatgpt.com/c/session-already-cached"
    history_store = ChatGPTHistoryStore(tmp_path / "llm" / "chatgpt" / "history.parquet")
    history_store.replace_conversation(
        conversation_url,
        {
            "mapping": {
                "user": {
                    "message": {
                        "author": {"role": "user"},
                        "create_time": 1771059600,
                        "content": {"parts": ["Already cached"]},
                    }
                }
            }
        },
        "2026-08-12T08:00:00Z",
        provider_revision="revision-1",
    )
    history_store.save()
    history_store = ChatGPTHistoryStore(history_store.path)

    with patch("app.core.chatgpt_downloader._get_chatgpt_api_json_via_page") as api_get:
        processed, new_messages, unchanged_sessions = cache_chatgpt_conversation_history(
            history_store,
            [conversation_url],
            object(),
            {},
            TaskState("test"),
            lambda: False,
            conversation_revisions_by_id={"session-already-cached": "revision-1"},
            conversation_titles_by_id={"session-already-cached": "Renamed cached session"},
        )

    assert (processed, new_messages, unchanged_sessions) == (1, 0, 1)
    api_get.assert_not_called()

    assert read_parquet_rows(history_store.path)[0]["conversation_title"] == "Renamed cached session"


def test_chatgpt_history_cache_refreshes_legacy_capture_times(tmp_path: Path) -> None:
    conversation_url = "https://chatgpt.com/c/session-legacy-time"
    captured_at = "2026-08-12T08:00:00Z"
    legacy_payload = {
        "mapping": {
            "user": {
                "message": {
                    "author": {"role": "user"},
                    "content": {"parts": ["Legacy message"]},
                }
            }
        }
    }
    refreshed_payload = {
        "mapping": {
            "user": {
                "message": {
                    "author": {"role": "user"},
                    "create_time": 1771059600,
                    "content": {"parts": ["Legacy message"]},
                }
            }
        }
    }
    history_store = ChatGPTHistoryStore(tmp_path / "llm" / "chatgpt" / "history.parquet")
    history_store.replace_conversation(conversation_url, legacy_payload, captured_at)
    history_store.save()

    with patch(
        "app.core.chatgpt_downloader._get_chatgpt_api_json_via_page",
        return_value=refreshed_payload,
    ) as api_get:
        processed, new_messages, unchanged_sessions = cache_chatgpt_conversation_history(
            history_store,
            [conversation_url],
            object(),
            {},
            TaskState("test"),
            lambda: False,
        )

    rows = read_parquet_rows(history_store.path)
    assert (processed, new_messages, unchanged_sessions) == (1, 0, 1)
    assert rows is not None
    assert rows[0]["first_seen_at"] == captured_at
    assert rows[0]["last_seen_at"] == "2026-02-14T09:00:00Z"
    api_get.assert_called_once()


def test_chatgpt_history_cache_refreshes_rows_without_source_timestamps(tmp_path: Path) -> None:
    conversation_url = "https://chatgpt.com/c/session-missing-time"
    history_path = tmp_path / "llm" / "chatgpt" / "history.parquet"
    write_parquet_rows_atomic(
        history_path,
        [
            {
                "schema_version": 1,
                "platform": "chatgpt",
                "conversation_id": "session-missing-time",
                "conversation_url": conversation_url,
                "conversation_title": "Missing source time",
                "message_key": "session-missing-time:user",
                "turn_index": 0,
                "message_index": 0,
                "role": "user",
                "author_label": "You",
                "content_text": "Needs a source timestamp",
                "content_html": "",
                "content_sha256": "missing-time-hash",
                "source_links": [],
                "model_label": "",
                "first_seen_at": "2026-08-12T08:00:00Z",
                "last_seen_at": "",
            }
        ],
        CHATGPT_HISTORY_SCHEMA,
    )

    history_store = ChatGPTHistoryStore(history_path)

    assert history_store.conversation_needs_timestamp_refresh(conversation_url)


class _FakeResponse:
    ok = True
    status = 200
    headers = {"content-type": "image/png"}

    def body(self) -> bytes:
        return PNG_PAYLOAD


class _FakeRequest:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def get(self, url: str, **_kwargs) -> _FakeResponse:
        self.urls.append(url)
        return _FakeResponse()


class _FakeContext:
    def __init__(self) -> None:
        self.request = _FakeRequest()


def test_chatgpt_url_helpers_accept_original_estuary_assets() -> None:
    source_url = (
        "https://chatgpt.com/backend-api/estuary/content?"
        "id=file_0000000090648211ba8e0181b729f6b1&ts=496144&p=fs"
    )

    assert extract_chatgpt_file_id(source_url) == "file_0000000090648211ba8e0181b729f6b1"
    assert infer_image_extension(source_url, "image/png; charset=binary") == ".png"
    assert infer_image_extension(source_url, "image/png", b"\xff\xd8\xfflegacy-jpeg") == ".jpg"
    assert looks_like_image(PNG_PAYLOAD)
    assert not looks_like_image(b"<html>not an image</html>")
    assert chatgpt_target_dir(DEFAULT_CHATGPT_PROJECT_NAME).name == DEFAULT_CHATGPT_PROJECT_NAME
    assert _chatgpt_project_id(DEFAULT_CHATGPT_PROJECT_URL) == "g-p-demo-project"


def test_chatgpt_excludes_user_uploads_from_every_cache_mode() -> None:
    base = {
        "source_url": "https://chatgpt.com/backend-api/estuary/content?id=file_role",
        "file_id": "file_role",
        "conversation_url": "https://chatgpt.com/g/project/c/role",
    }

    assert not should_cache_chatgpt_candidate(ChatGPTImageCandidate(**base, message_role="user"))
    assert not should_cache_chatgpt_candidate(ChatGPTImageCandidate(**base, message_role=" User "))
    assert should_cache_chatgpt_candidate(ChatGPTImageCandidate(**base, message_role="assistant"))
    assert should_cache_chatgpt_candidate(ChatGPTImageCandidate(**base))

    known_upload = dict(base, file_id="file_000000000e6471fd89cf0af9b5bd16e5")
    assert not should_cache_chatgpt_candidate(ChatGPTImageCandidate(**known_upload, message_role="user"))
    assert not should_cache_chatgpt_candidate(ChatGPTImageCandidate(**dict(base, source_url="")))
    assert not should_cache_chatgpt_candidate(
        ChatGPTImageCandidate(
            **dict(
                base,
                source_url=(
                    "https://chatgpt.com/backend-api/estuary/content?"
                    "id=asset%23file_role%23thumbnail"
                ),
            )
        )
    )


def test_chatgpt_extracts_every_image_asset_and_prompt_from_all_conversation_branches() -> None:
    payload = {
        "current_node": "assistant-message",
        "mapping": {
            "user-message": {
                "parent": None,
                "message": {
                    "author": {"role": "user"},
                    "content": {
                        "parts": [
                            "**Keep the subject centered.**\n\n- Use soft studio light\n- Preserve the blue backdrop",
                            {
                                "asset_pointer": "file-service://file_user_original",
                                "content_type": "image/png",
                                "width": 1_024,
                                "height": 1_536,
                                "file_name": "user-reference.png",
                            },
                            {
                                "asset_pointer": "file-service://file_document",
                                "content_type": "application/pdf",
                            },
                        ]
                    },
                }
            },
            "assistant-message": {
                "parent": "user-message",
                "message": {
                    "author": {"role": "assistant"},
                    "content": {
                        "parts": [
                            {
                                "image_asset_pointer": "file-service://file_assistant_original",
                                "content_type": "image_asset_pointer",
                                "width": 1_536,
                                "height": 1_024,
                            },
                            {
                                "image_asset_pointer": "sediment://file_historical_missing",
                                "content_type": "image_asset_pointer",
                            }
                        ]
                    },
                }
            },
            "stale-branch-message": {
                "parent": "user-message",
                "message": {
                    "author": {"role": "assistant"},
                    "content": {
                        "parts": [
                            {
                                "asset_pointer": "sediment://file_stale_branch",
                                "content_type": "image_asset_pointer",
                            },
                            {
                                "asset_pointer": "file-service://file_branch_original",
                                "content_type": "image_asset_pointer",
                            }
                        ]
                    },
                },
            },
        }
    }

    candidates = _extract_chatgpt_conversation_image_payloads(payload)

    assert {candidate["fileId"] for candidate in candidates} == {
        "file_user_original",
        "file_assistant_original",
        "file_branch_original",
    }
    by_file_id = {candidate["fileId"]: candidate for candidate in candidates}
    assert by_file_id["file_user_original"]["messageRole"] == "user"
    assert by_file_id["file_user_original"]["width"] == 1_024
    assert by_file_id["file_assistant_original"]["messageRole"] == "assistant"
    expected_prompt = "**Keep the subject centered.**\n\n- Use soft studio light\n- Preserve the blue backdrop"
    assert by_file_id["file_user_original"]["promptMarkdown"] == expected_prompt
    assert by_file_id["file_assistant_original"]["promptMarkdown"] == expected_prompt
    assert by_file_id["file_branch_original"]["promptMarkdown"] == expected_prompt


def test_chatgpt_project_index_keeps_only_current_project_images() -> None:
    class _JsonResponse:
        ok = True
        status = 200

        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def text(self) -> str:
            return json.dumps(self.payload)

    class _IndexRequest:
        def __init__(self) -> None:
            self.urls: list[str] = []

        def get(self, url: str, **_kwargs) -> _JsonResponse:
            self.urls.append(url)
            if "after=next-page" in url:
                return _JsonResponse(
                    {
                        "items": [
                            {
                                "conversation_id": "project-second",
                                "asset_pointer": "file-service://file_project_second",
                                "url": "https://chatgpt.com/backend-api/estuary/content?id=file_project_second&sig=two",
                                "width": 1_536,
                                "height": 1_024,
                                "created_at": 1_786_362_721.382649,
                            }
                        ]
                    }
                )
            return _JsonResponse(
                {
                    "items": [
                        {
                            "conversation_id": "different-project",
                            "asset_pointer": "file-service://file_other_project",
                            "url": "https://chatgpt.com/backend-api/estuary/content?id=file_other_project&sig=other",
                        },
                        {
                            "conversation_id": "project-first",
                            "asset_pointer": "file-service://file_project_first",
                            "url": "https://chatgpt.com/backend-api/estuary/content?id=file_project_first&sig=one",
                            "encodings": {
                                "thumbnail": {
                                    "path": "https://chatgpt.com/backend-api/estuary/content?id=file_project_first&sig=thumb"
                                }
                            },
                            "width": 1_024,
                            "height": 1_536,
                            "created_at": 1_786_362_700.25,
                        },
                    ],
                    "cursor": "next-page",
                }
            )

    class _IndexContext:
        def __init__(self) -> None:
            self.request = _IndexRequest()

    context = _IndexContext()
    request_headers = {"authorization": "Bearer test-token", "oai-device-id": "device-id"}
    candidates = collect_chatgpt_project_index_images(
        context,
        DEFAULT_CHATGPT_PROJECT_URL,
        [
            "https://chatgpt.com/g/g-p-demo-project/c/project-first",
            "https://chatgpt.com/g/g-p-demo-project/c/project-second",
        ],
        request_headers,
        TaskState("test"),
        should_stop=lambda: False,
        conversation_titles_by_id={
            "project-first": "master 21",
            "project-second": "master 22",
        },
    )

    assert {candidate.file_id for candidate in candidates} == {
        "file_project_first",
        "file_project_second",
    }
    assert all(candidate.request_headers == request_headers for candidate in candidates)
    assert {candidate.file_id: candidate.created_at for candidate in candidates} == {
        "file_project_first": "1786362700.25",
        "file_project_second": "1786362721.382649",
    }
    assert {candidate.file_id: candidate.conversation_title for candidate in candidates} == {
        "file_project_first": "master 21",
        "file_project_second": "master 22",
    }
    assert any("after=next-page" in url for url in context.request.urls)


def test_chatgpt_project_index_rejects_partial_results_after_retries() -> None:
    class _JsonResponse:
        ok = True
        status = 200

        @staticmethod
        def text() -> str:
            return json.dumps(
                {
                    "items": [
                        {
                            "conversation_id": "project-first",
                            "asset_pointer": "file-service://file_project_first",
                            "url": "https://chatgpt.com/backend-api/estuary/content?id=file_project_first",
                        }
                    ],
                    "cursor": "next-page",
                }
            )

    class _InterruptedRequest:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, _url: str, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return _JsonResponse()
            raise RuntimeError("Safari request failed: Load failed")

    context = type("InterruptedContext", (), {"request": _InterruptedRequest()})()
    state = TaskState("test")

    with patch("app.core.chatgpt_downloader.time.sleep") as sleep, pytest.raises(
        RuntimeError,
        match="stopped before pagination completed",
    ):
        collect_chatgpt_project_index_images(
            context,
            DEFAULT_CHATGPT_PROJECT_URL,
            ["https://chatgpt.com/c/project-first"],
            {"authorization": "Bearer test-token"},
            state,
            should_stop=lambda: False,
        )

    assert context.request.calls == 4
    assert len(sleep.call_args_list) == 2
    assert state.snapshot()["discovered_images"] == 1


def test_chatgpt_project_index_backfills_prompts_from_conversation_mappings() -> None:
    conversation_url = "https://chatgpt.com/c/project-first"
    candidate = ChatGPTImageCandidate(
        source_url="https://chatgpt.com/backend-api/estuary/content?id=file_project_first&sig=one",
        file_id="file_project_first",
        conversation_url=conversation_url,
        request_headers={"authorization": "Bearer test-token"},
    )
    payload = {
        "title": "Prompt metadata conversation",
        "current_node": "assistant-message",
        "mapping": {
            "user-message": {
                "parent": None,
                "message": {
                    "author": {"role": "user"},
                    "content": {"parts": ["**Create this image.**\n\n- Preserve the framing"]},
                },
            },
            "assistant-message": {
                "parent": "user-message",
                "message": {
                    "author": {"role": "assistant"},
                    "content": {
                        "parts": [
                            {
                                "asset_pointer": "sediment://file_project_first",
                                "content_type": "image_asset_pointer",
                            }
                        ]
                    },
                },
            },
        },
    }
    state = TaskState("test")
    persisted: list[ChatGPTImageCandidate] = []

    def persist_batch(candidates) -> int:
        persisted.extend(candidates)
        return len(candidates)

    with patch(
        "app.core.chatgpt_downloader._get_chatgpt_api_json",
        return_value=payload,
    ) as api_get:
        enriched = enrich_chatgpt_project_index_prompts(
            object(),
            DEFAULT_CHATGPT_PROJECT_URL,
            [candidate],
            {"authorization": "Bearer test-token"},
            state,
            should_stop=lambda: False,
            persist_batch=persist_batch,
        )

    assert enriched[0].source_url == candidate.source_url
    assert enriched[0].request_headers == candidate.request_headers
    assert enriched[0].prompt_markdown == "**Create this image.**\n\n- Preserve the framing"
    assert enriched[0].conversation_title == "Prompt metadata conversation"
    assert [item.file_id for item in persisted] == [candidate.file_id]
    assert api_get.call_count == 1
    assert api_get.call_args.args[1] == "https://chatgpt.com/backend-api/conversation/project-first"
    assert any("matched 1/1 images" in event for event in state.snapshot()["recent_events"])


def test_chatgpt_conversation_mapping_replaces_a_stale_project_index_prompt() -> None:
    conversation_url = "https://chatgpt.com/c/project-first"
    candidate = ChatGPTImageCandidate(
        source_url="https://chatgpt.com/backend-api/estuary/content?id=file_project_first&sig=one",
        file_id="file_project_first",
        conversation_url=conversation_url,
        prompt_markdown="Short stale project-index prompt",
        conversation_title="master 37",
        request_headers={"authorization": "Bearer test-token"},
    )
    authoritative_prompt = "**Keep this pose.**\n\n- Preserve the exact proportions\n- Use a white studio"
    payload = {
        "title": "master 37",
        "mapping": {
            "user-message": {
                "parent": None,
                "message": {
                    "author": {"role": "user"},
                    "content": {"parts": [authoritative_prompt]},
                },
            },
            "assistant-message": {
                "parent": "user-message",
                "message": {
                    "author": {"role": "assistant"},
                    "content": {
                        "parts": [
                            {
                                "asset_pointer": "sediment://file_project_first",
                                "content_type": "image_asset_pointer",
                            }
                        ]
                    },
                },
            },
        },
    }

    with patch(
        "app.core.chatgpt_downloader._get_chatgpt_api_json",
        return_value=payload,
    ) as api_get:
        enriched = enrich_chatgpt_project_index_prompts(
            object(),
            DEFAULT_CHATGPT_PROJECT_URL,
            [candidate],
            {"authorization": "Bearer test-token"},
            TaskState("test"),
            should_stop=lambda: False,
        )

    assert enriched[0].prompt_markdown == authoritative_prompt
    api_get.assert_called_once()


def test_chatgpt_authoritative_empty_prompt_clears_stale_catalog_metadata(tmp_path: Path) -> None:
    target_dir = tmp_path / "media" / "chatgpt" / "demo-project"
    conversation_url = "https://chatgpt.com/c/project-first"
    candidate = ChatGPTImageCandidate(
        source_url="https://chatgpt.com/backend-api/estuary/content?id=file_project_first",
        file_id="file_project_first",
        conversation_url=conversation_url,
        prompt_markdown="Stale prompt from a different image",
    )
    catalog = ChatGPTImageCatalog.build(target_dir)
    catalog.entries_by_file_id[candidate.file_id] = ChatGPTCatalogEntry(
        file_id=candidate.file_id,
        relative_path="img_file_project_first.png",
        content_sha256="",
        content_bytes=0,
        source_url=candidate.source_url,
        conversation_url=conversation_url,
        alt_text="",
        width=1_024,
        height=1_536,
        first_seen_at="2026-08-12T00:00:00Z",
        last_seen_at="2026-08-12T00:00:00Z",
        prompt_markdown=candidate.prompt_markdown,
    )

    cleared = replace(
        candidate,
        prompt_markdown="",
        prompt_metadata_authoritative=True,
    )

    assert catalog.update_metadata_batch((cleared,)) == 1
    assert catalog.entries_by_file_id[candidate.file_id].prompt_markdown == ""


def test_chatgpt_project_index_backfills_a_missing_session_title_even_with_a_prompt() -> None:
    conversation_url = "https://chatgpt.com/c/project-first"
    candidate = ChatGPTImageCandidate(
        source_url="https://chatgpt.com/backend-api/estuary/content?id=file_project_first",
        file_id="file_project_first",
        conversation_url=conversation_url,
        prompt_markdown="Already cached prompt",
    )
    payload = {
        "title": "master 21",
        "mapping": {},
    }

    with patch(
        "app.core.chatgpt_downloader._get_chatgpt_api_json",
        return_value=payload,
    ) as api_get:
        enriched = enrich_chatgpt_project_index_prompts(
            object(),
            DEFAULT_CHATGPT_PROJECT_URL,
            [candidate],
            {"authorization": "Bearer test-token"},
            TaskState("test"),
            should_stop=lambda: False,
        )

    assert enriched[0].prompt_markdown == "Already cached prompt"
    assert enriched[0].conversation_title == "master 21"
    api_get.assert_called_once()


def test_chatgpt_prompt_backfill_skips_conversations_with_complete_catalog_metadata() -> None:
    candidate = ChatGPTImageCandidate(
        source_url="https://chatgpt.com/backend-api/estuary/content?id=file_complete",
        file_id="file_complete",
        conversation_url="https://chatgpt.com/c/complete",
        prompt_markdown="Authoritative cached prompt",
        conversation_title="master 21",
    )
    state = TaskState("test")

    with patch("app.core.chatgpt_downloader._get_chatgpt_api_json") as api_get:
        enriched = enrich_chatgpt_project_index_prompts(
            object(),
            DEFAULT_CHATGPT_PROJECT_URL,
            [candidate],
            {"authorization": "Bearer test-token"},
            state,
            should_stop=lambda: False,
            skip_complete_conversations=True,
        )

    assert enriched == [candidate]
    api_get.assert_not_called()
    assert any(
        "Reused complete cached prompt metadata for 1 ChatGPT sessions" in event
        for event in state.snapshot()["recent_events"]
    )


def test_chatgpt_prompt_rate_limit_defers_metadata_without_blocking_image_sync() -> None:
    candidate = ChatGPTImageCandidate(
        source_url="https://chatgpt.com/backend-api/estuary/content?id=file_rate_limited",
        file_id="file_rate_limited",
        conversation_url="https://chatgpt.com/c/rate-limited",
        request_headers={"authorization": "Bearer test-token"},
    )
    state = TaskState("test")

    with patch(
        "app.core.chatgpt_downloader._get_chatgpt_api_json",
        side_effect=RuntimeError("ChatGPT API request returned HTTP 429."),
    ) as api_get, patch("app.core.chatgpt_downloader.time.sleep") as sleep:
        enriched = enrich_chatgpt_project_index_prompts(
            object(),
            DEFAULT_CHATGPT_PROJECT_URL,
            [candidate],
            {"authorization": "Bearer test-token"},
            state,
            should_stop=lambda: False,
        )

    assert enriched == [candidate]
    assert api_get.call_count == 1
    sleep.assert_not_called()
    assert any(
        "deferring the remaining prompt metadata and continuing the image sync" in event
        for event in state.snapshot()["recent_events"]
    )


def test_chatgpt_prompt_metadata_uses_three_isolated_safari_pages_concurrently() -> None:
    context = SafariContext(DEFAULT_CHATGPT_PROJECT_URL)

    class _Page:
        def __init__(self) -> None:
            self.context = context
            self.closed = False

        def close(self) -> None:
            self.closed = True

    pages = [_Page(), _Page(), _Page()]
    active_lock = Lock()
    all_started = Event()
    active_calls = 0
    maximum_active_calls = 0

    def fetch_mapping(_page, _url, _headers):
        nonlocal active_calls, maximum_active_calls
        with active_lock:
            active_calls += 1
            maximum_active_calls = max(maximum_active_calls, active_calls)
            if active_calls == 3:
                all_started.set()
        assert all_started.wait(timeout=1)
        with active_lock:
            active_calls -= 1
        return {"mapping": {}}

    conversation_urls = [f"https://chatgpt.com/c/parallel-{index}" for index in range(3)]
    with patch.object(context, "new_page", side_effect=pages[1:]), patch(
        "app.core.chatgpt_downloader.open_chatgpt_page"
    ), patch(
        "app.core.chatgpt_downloader._get_chatgpt_api_json_via_page",
        side_effect=fetch_mapping,
    ):
        results = list(
            _iter_parallel_safari_prompt_metadata_results(
                pages[0],
                DEFAULT_CHATGPT_PROJECT_URL,
                conversation_urls,
                {"authorization": "Bearer test-token"},
                should_stop=lambda: False,
                worker_count=3,
            )
        )

    assert maximum_active_calls == 3
    assert {result.conversation_url for result in results} == set(conversation_urls)
    assert pages[0].closed is False
    assert pages[1].closed is True
    assert pages[2].closed is True


@pytest.mark.parametrize("error_type", (PlaywrightError, RuntimeError))
def test_chatgpt_api_retries_transient_browser_connection_errors(error_type) -> None:
    class _JsonResponse:
        ok = True
        status = 200

        @staticmethod
        def text() -> str:
            return '{"items": []}'

    class _RetryingRequest:
        def __init__(self) -> None:
            self.attempts = 0

        def get(self, _url: str, **_kwargs):
            self.attempts += 1
            if self.attempts < 3:
                raise error_type("socket hang up")
            return _JsonResponse()

    class _RetryingContext:
        def __init__(self) -> None:
            self.request = _RetryingRequest()

    context = _RetryingContext()
    with patch("app.core.chatgpt_downloader.time.sleep") as sleep:
        payload = _get_chatgpt_api_json(context, "https://chatgpt.com/backend-api/test", {})

    assert payload == {"items": []}
    assert context.request.attempts == 3
    assert [call.args[0] for call in sleep.call_args_list] == [1.0, 2.0]


def test_chatgpt_api_retries_rate_limits_with_retry_after() -> None:
    class _RateLimitedResponse:
        ok = False
        status = 429
        headers = {"Retry-After": "4"}

    class _ReadyResponse:
        ok = True
        status = 200
        headers = {}

        @staticmethod
        def text() -> str:
            return '{"items": []}'

    class _RateLimitedRequest:
        def __init__(self) -> None:
            self.attempts = 0

        def get(self, _url: str, **_kwargs):
            self.attempts += 1
            return _RateLimitedResponse() if self.attempts == 1 else _ReadyResponse()

    class _RateLimitedContext:
        def __init__(self) -> None:
            self.request = _RateLimitedRequest()

    context = _RateLimitedContext()
    with patch("app.core.chatgpt_downloader.time.sleep") as sleep:
        payload = _get_chatgpt_api_json(context, "https://chatgpt.com/backend-api/test", {})

    assert payload == {"items": []}
    assert context.request.attempts == 2
    sleep.assert_called_once_with(4.0)


def test_chatgpt_browser_page_api_uses_authorized_fetch_headers() -> None:
    class _AuthorizedPage:
        def __init__(self) -> None:
            self.argument: dict[str, object] = {}

        def evaluate(self, _script: str, argument: dict[str, object]) -> dict[str, object]:
            self.argument = argument
            return {"status": 200, "payload": {"mapping": {"node": {}}}}

    page = _AuthorizedPage()
    payload = _get_chatgpt_api_json_via_page(
        page,
        "https://chatgpt.com/backend-api/conversation/project-first",
        {
            "authorization": "Bearer test-token",
            "oai-device-id": "device-id",
            "cookie": "must-not-be-forwarded",
        },
    )

    assert payload == {"mapping": {"node": {}}}
    assert page.argument["headers"] == {
        "authorization": "Bearer test-token",
        "oai-device-id": "device-id",
        "Accept": "application/json",
    }


def test_chatgpt_loads_and_reuses_safari_session_authorization() -> None:
    class _SessionResponse:
        ok = True
        status = 200

        @staticmethod
        def text() -> str:
            return '{"accessToken":"test-token"}'

    class _SessionRequest:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def get(self, url: str, **kwargs) -> _SessionResponse:
            self.calls.append((url, kwargs))
            return _SessionResponse()

    context = SafariContext("https://chatgpt.com/")
    context.request = _SessionRequest()

    first = _load_chatgpt_session_request_headers(context, "https://chatgpt.com/")
    second = _load_chatgpt_session_request_headers(context, "https://chatgpt.com/")

    assert first == second == {"authorization": "Bearer test-token"}
    assert len(context.request.calls) == 1
    assert context.request.calls[0][0] == "https://chatgpt.com/api/auth/session"


def test_chatgpt_session_authorization_retries_transient_tls_disconnect() -> None:
    class _SessionResponse:
        ok = True
        status = 200

        @staticmethod
        def text() -> str:
            return '{"accessToken":"test-token"}'

    class _SessionRequest:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, _url: str, **_kwargs: object) -> _SessionResponse:
            self.calls += 1
            if self.calls == 1:
                raise PlaywrightError(
                    "Client network socket disconnected before secure TLS connection was established"
                )
            return _SessionResponse()

    context = SafariContext("https://chatgpt.com/")
    context.request = _SessionRequest()

    with patch("app.core.chatgpt_downloader.time.sleep") as sleep:
        headers = _load_chatgpt_session_request_headers(context, "https://chatgpt.com/c/example")

    assert headers == {"authorization": "Bearer test-token"}
    assert context.request.calls == 2
    sleep.assert_called_once_with(1.0)


@pytest.mark.parametrize(
    "message",
    (
        "APIRequestContext.get: Client network socket disconnected before secure TLS connection was established",
        "APIRequestContext.get: socket hang up",
        "APIRequestContext.get: read ECONNRESET",
        "APIRequestContext.get: connect ETIMEDOUT",
    ),
)
def test_chatgpt_image_retries_platform_network_error_messages(message: str) -> None:
    assert _is_retryable_chatgpt_image_error(PlaywrightError(message))


def test_chatgpt_missing_image_assets_are_skipped_without_repeat_attempts(tmp_path: Path) -> None:
    catalog = ChatGPTImageCatalog.build(tmp_path / "media" / "chatgpt" / "demo-project")
    candidate = ChatGPTImageCandidate(
        source_url=_chatgpt_file_download_url("file_historical_missing"),
        file_id="file_historical_missing",
        conversation_url="https://chatgpt.com/c/project-conversation",
    )

    assert _is_unavailable_chatgpt_image_error(
        candidate,
        RuntimeError("ChatGPT file metadata request returned HTTP 404."),
    )
    direct_candidate = ChatGPTImageCandidate(
        source_url="https://chatgpt.com/backend-api/estuary/content?id=file_historical_direct",
        file_id="file_historical_direct",
        conversation_url="https://chatgpt.com/c/project-conversation",
    )
    assert _is_unavailable_chatgpt_image_error(
        direct_candidate,
        RuntimeError("ChatGPT image request returned HTTP 404."),
    )
    catalog.mark_unavailable(candidate.file_id)
    assert not catalog.claim_download(candidate.file_id)


def test_chatgpt_resolves_api_assets_to_their_original_download_url(tmp_path: Path) -> None:
    file_id = "file_api_image"
    download_url = f"https://chatgpt.com/backend-api/estuary/content?id={file_id}&sig=temporary"

    class _DownloadMetadataResponse:
        ok = True
        status = 200

        def text(self) -> str:
            return json.dumps({"download_url": download_url})

    class _ResolvedImageResponse:
        ok = True
        status = 200
        headers = {"content-type": "image/png"}

        def body(self) -> bytes:
            return PNG_PAYLOAD

    class _ResolvingRequest:
        def __init__(self) -> None:
            self.requests: list[tuple[str, dict[str, object]]] = []

        def get(self, url: str, **kwargs):
            self.requests.append((url, kwargs))
            if url == _chatgpt_file_download_url(file_id):
                return _DownloadMetadataResponse()
            assert url == download_url
            return _ResolvedImageResponse()

    class _ResolvingContext:
        def __init__(self) -> None:
            self.request = _ResolvingRequest()

    candidate = ChatGPTImageCandidate(
        source_url=_chatgpt_file_download_url(file_id),
        file_id=file_id,
        conversation_url="https://chatgpt.com/c/conversation-api-image",
        request_headers={"authorization": "Bearer test-token", "oai-device-id": "device-id"},
    )
    context = _ResolvingContext()
    target_dir = tmp_path / "media" / "chatgpt" / "demo-project"
    catalog = ChatGPTImageCatalog.build(target_dir)

    assert download_chatgpt_image(context, catalog, target_dir, candidate)
    assert [request[0] for request in context.request.requests] == [
        _chatgpt_file_download_url(file_id),
        download_url,
    ]
    assert context.request.requests[0][1]["headers"]["authorization"] == "Bearer test-token"
    assert catalog.entries_by_file_id[file_id].source_url == download_url


def test_chatgpt_refreshes_an_expired_project_index_image_url(tmp_path: Path) -> None:
    file_id = "file_expired_index_image"
    direct_url = f"https://chatgpt.com/backend-api/estuary/content?id={file_id}&sig=expired"
    refreshed_url = f"https://chatgpt.com/backend-api/estuary/content?id={file_id}&sig=refreshed"
    conversation_url = "https://chatgpt.com/c/project-index-conversation"

    class _NotFoundResponse:
        ok = False
        status = 404

    class _DownloadMetadataResponse:
        ok = True
        status = 200

        def text(self) -> str:
            return json.dumps({"download_url": refreshed_url})

    class _ResolvedImageResponse:
        ok = True
        status = 200
        headers = {"content-type": "image/png"}

        def body(self) -> bytes:
            return PNG_PAYLOAD

    class _RefreshingRequest:
        def __init__(self) -> None:
            self.urls: list[str] = []

        def get(self, url: str, **_kwargs):
            self.urls.append(url)
            if url == direct_url:
                return _NotFoundResponse()
            if url == _chatgpt_file_download_url(file_id, conversation_url):
                return _DownloadMetadataResponse()
            assert url == refreshed_url
            return _ResolvedImageResponse()

    class _RefreshingContext:
        def __init__(self) -> None:
            self.request = _RefreshingRequest()

    candidate = ChatGPTImageCandidate(
        source_url=direct_url,
        file_id=file_id,
        conversation_url=conversation_url,
        request_headers={"authorization": "Bearer test-token", "oai-device-id": "device-id"},
    )
    context = _RefreshingContext()
    target_dir = tmp_path / "media" / "chatgpt" / "demo-project"
    catalog = ChatGPTImageCatalog.build(target_dir)

    assert download_chatgpt_image(context, catalog, target_dir, candidate)
    assert context.request.urls == [
        direct_url,
        _chatgpt_file_download_url(file_id, conversation_url),
        refreshed_url,
    ]
    assert catalog.entries_by_file_id[file_id].source_url == refreshed_url


def test_chatgpt_prefers_a_rendered_original_url_over_an_unresolved_api_asset() -> None:
    conversation_url = "https://chatgpt.com/c/conversation-direct-url"
    file_id = "file_direct_url"
    candidates_by_file_id = {
        file_id: ChatGPTImageCandidate(
            source_url=_chatgpt_file_download_url(file_id, conversation_url),
            file_id=file_id,
            conversation_url=conversation_url,
            conversation_title="master 30",
            prompt_markdown="Authoritative conversation-mapping prompt",
            width=2_048,
            height=2_048,
            request_headers={"authorization": "Bearer test-token"},
        )
    }
    raw_candidate = {
        "sourceUrl": f"https://chatgpt.com/backend-api/estuary/content?id={file_id}&sig=temporary",
        "fileId": file_id,
        "promptMarkdown": "Incorrect nearest rendered prompt",
        "width": 1_024,
        "height": 1_024,
    }

    with patch("app.core.chatgpt_downloader._extract_original_image_payloads", return_value=[raw_candidate]):
        _merge_current_conversation_images(
            object(),
            conversation_url,
            candidates_by_file_id,
            "Conversation title",
        )

    assert candidates_by_file_id[file_id].source_url == raw_candidate["sourceUrl"]
    assert candidates_by_file_id[file_id].conversation_title == "master 30"
    assert (
        candidates_by_file_id[file_id].prompt_markdown
        == "Authoritative conversation-mapping prompt"
    )


@pytest.mark.parametrize("roles", [("user", "assistant"), ("assistant", "user")])
def test_upload_provenance_survives_api_and_rendered_image_merging(roles) -> None:
    url = "https://chatgpt.com/c/upload-test"
    mapping = {
        str(index): {"message": {"author": {"role": role}, "content": {
            "parts": [{"content_type": "image_asset_pointer", "asset_pointer": "sediment://file_upload"}],
        }}}
        for index, role in enumerate(roles)
    }
    candidates = {}
    _merge_chatgpt_conversation_payload_images({"mapping": mapping}, {}, url, candidates, "")
    with patch("app.core.chatgpt_downloader._extract_original_image_payloads", return_value=[{
        "sourceUrl": "https://chatgpt.com/backend-api/estuary/content?id=file_upload",
        "fileId": "file_upload", "width": 4096, "height": 4096,
    }]):
        _merge_current_conversation_images(object(), url, candidates, "")
    assert candidates["file_upload"].message_role == "user"
    assert not should_cache_chatgpt_candidate(candidates["file_upload"])


def test_upload_download_guard_never_requests_or_registers_a_file(tmp_path: Path) -> None:
    target_dir = tmp_path / "media" / "chatgpt" / "project"
    catalog = ChatGPTImageCatalog.build(target_dir)
    context = _FakeContext()
    candidate = ChatGPTImageCandidate(
        source_url="https://chatgpt.com/backend-api/estuary/content?id=file_upload",
        file_id="file_upload", conversation_url="https://chatgpt.com/c/upload-test",
        message_role="user",
    )
    assert not download_chatgpt_image(context, catalog, target_dir, candidate)
    assert not context.request.urls
    assert catalog.summarize() == 0
    assert not target_dir.exists()


def test_chatgpt_honors_local_resources_exclusions_in_the_canonical_store(tmp_path: Path) -> None:
    from app.core.local_media_browser import LocalMediaCatalog

    root = tmp_path / "local-store"
    target_dir = root / "media" / "chatgpt" / "project"
    catalog = ChatGPTImageCatalog.build(target_dir)
    context = _FakeContext()
    candidate = ChatGPTImageCandidate(
        source_url="https://chatgpt.com/backend-api/estuary/content?id=file_excluded",
        file_id="file_excluded", conversation_url="https://chatgpt.com/c/upload-test",
        message_role="assistant",
    )
    assert download_chatgpt_image(context, catalog, target_dir, candidate)
    media_catalog = LocalMediaCatalog(root)
    item = media_catalog.snapshot(force_refresh=True)[0]
    media_catalog.delete(item.stable_id)
    assert not (root / item.relative_path).exists()
    assert not download_chatgpt_image(context, catalog, target_dir, candidate)
    assert len(context.request.urls) == 1


def test_chatgpt_catalog_registers_downloads_and_skips_complete_files(tmp_path: Path) -> None:
    target_dir = tmp_path / "media" / "chatgpt" / "demo-project"
    candidate = ChatGPTImageCandidate(
        source_url="https://chatgpt.com/backend-api/estuary/content?id=file_demo",
        file_id="file_demo",
        conversation_url=DEFAULT_CHATGPT_PROJECT_URL.replace("/project", "/c/demo"),
        alt_text="A generated image",
        prompt_markdown="**Initial** prompt",
        width=1_024,
        height=1_536,
        message_role="assistant",
    )
    context = _FakeContext()
    catalog = ChatGPTImageCatalog.build(target_dir)

    assert download_chatgpt_image(context, catalog, target_dir, candidate)
    assert not download_chatgpt_image(context, catalog, target_dir, candidate)
    assert len(context.request.urls) == 1
    assert catalog.summarize() == 1
    refreshed_candidate = replace(
        candidate,
        conversation_title="A refreshed session",
        created_at="1786362721.382649",
        prompt_markdown="**Updated** prompt\n\n- Keep the pose",
    )
    assert catalog.update_metadata_batch((refreshed_candidate,)) == 1
    assert catalog.update_metadata_batch((refreshed_candidate,)) == 0

    reloaded = ChatGPTImageCatalog.build(target_dir)
    entry = reloaded.entries_by_file_id["file_demo"]
    assert reloaded.complete_entry("file_demo") == entry
    assert entry.conversation_title == "A refreshed session"
    assert entry.created_at == "1786362721.382649"
    assert entry.prompt_markdown == "**Updated** prompt\n\n- Keep the pose"
    assert (target_dir / entry.relative_path).read_bytes() == PNG_PAYLOAD


def test_chatgpt_catalog_does_not_reuse_prompt_for_an_unindexed_candidate(tmp_path: Path) -> None:
    target_dir = tmp_path / "media" / "chatgpt" / "demo-project"
    catalog = ChatGPTImageCatalog.build(target_dir)
    cached_candidate = ChatGPTImageCandidate(
        source_url="https://chatgpt.com/backend-api/estuary/content?id=file_known",
        file_id="file_known",
        conversation_url="https://chatgpt.com/c/known",
        prompt_markdown="Cached prompt",
        conversation_title="Cached session title",
        created_at="2026-08-11T00:00:00Z",
    )
    assert download_chatgpt_image(_FakeContext(), catalog, target_dir, cached_candidate)

    current_candidate = replace(
        cached_candidate,
        source_url="https://chatgpt.com/backend-api/estuary/content?id=file_known&sig=current",
        prompt_markdown="",
        conversation_title="Current session title",
        created_at="",
    )
    merged = catalog.merge_known_metadata((current_candidate,))[0]

    assert merged.source_url == current_candidate.source_url
    assert merged.prompt_markdown == ""
    assert merged.conversation_title == "Current session title"
    assert merged.created_at == "2026-08-11T00:00:00Z"


def test_chatgpt_prompt_backfill_rechecks_sibling_images_when_one_lacks_prompt(tmp_path: Path) -> None:
    conversation_url = "https://chatgpt.com/c/project-first"
    old_candidate = ChatGPTImageCandidate(
        source_url="https://chatgpt.com/backend-api/estuary/content?id=file_old",
        file_id="file_old",
        conversation_url=conversation_url,
        prompt_markdown="Stale index prompt",
        conversation_title="Session title",
    )
    new_candidate = ChatGPTImageCandidate(
        source_url="https://chatgpt.com/backend-api/estuary/content?id=file_new",
        file_id="file_new",
        conversation_url=conversation_url,
        conversation_title="Session title",
    )
    catalog = ChatGPTImageCatalog.build(tmp_path / "media" / "chatgpt" / "demo-project")
    merged = catalog.merge_known_metadata((old_candidate, new_candidate))
    assert [candidate.prompt_markdown for candidate in merged] == [
        "Stale index prompt",
        "",
    ]
    payload = {
        "title": "Session title",
        "mapping": {
            "old-user": {
                "parent": None,
                "message": {
                    "author": {"role": "user"},
                    "content": {"parts": ["Original image prompt"]},
                },
            },
            "old-assistant": {
                "parent": "old-user",
                "message": {
                    "author": {"role": "assistant"},
                    "content": {
                        "parts": [
                            {
                                "asset_pointer": "sediment://file_old",
                                "content_type": "image_asset_pointer",
                            }
                        ]
                    },
                },
            },
            "new-user": {
                "parent": "old-assistant",
                "message": {
                    "author": {"role": "user"},
                    "content": {"parts": ["Latest image correction"]},
                },
            },
            "new-assistant": {
                "parent": "new-user",
                "message": {
                    "author": {"role": "assistant"},
                    "content": {
                        "parts": [
                            {
                                "asset_pointer": "sediment://file_new",
                                "content_type": "image_asset_pointer",
                            }
                        ]
                    },
                },
            },
        },
    }

    with patch(
        "app.core.chatgpt_downloader._get_chatgpt_api_json",
        return_value=payload,
    ) as api_get:
        enriched = enrich_chatgpt_project_index_prompts(
            object(),
            conversation_url,
            list(merged),
            {"authorization": "Bearer test-token"},
            TaskState("test"),
            should_stop=lambda: False,
        )

    assert [candidate.prompt_markdown for candidate in enriched] == [
        "Original image prompt",
        "Latest image correction",
    ]
    api_get.assert_called_once()


def test_chatgpt_catalog_prunes_missing_entries_during_load(tmp_path: Path) -> None:
    target_dir = tmp_path / "media" / "chatgpt" / "demo-project"
    candidate = ChatGPTImageCandidate(
        source_url="https://chatgpt.com/backend-api/estuary/content?id=file_valid",
        file_id="file_valid",
        conversation_url="https://chatgpt.com/c/valid",
    )
    catalog = ChatGPTImageCatalog.build(target_dir)
    assert download_chatgpt_image(_FakeContext(), catalog, target_dir, candidate)
    catalog.entries_by_file_id["file_missing"] = ChatGPTCatalogEntry(
        file_id="file_missing",
        relative_path="img_file_missing.png",
        content_sha256="missing",
        content_bytes=1_024,
        source_url="https://chatgpt.com/backend-api/estuary/content?id=file_missing",
        conversation_url="https://chatgpt.com/c/missing",
        alt_text="",
        width=1_024,
        height=1_024,
        first_seen_at="2026-08-11T00:00:00Z",
        last_seen_at="2026-08-11T00:00:00Z",
    )
    catalog.save()

    repaired = ChatGPTImageCatalog.build(target_dir)

    assert repaired.repair_result.removed_file_ids == ("file_missing",)
    assert repaired.summarize() == 1
    assert set(repaired.entries_by_file_id) == {"file_valid"}
    persisted = read_parquet_rows(repaired.catalog_path)
    assert persisted is not None
    assert {str(row["file_id"]) for row in persisted} == {"file_valid"}


def test_chatgpt_catalog_prunes_signature_only_corrupt_images_during_load(tmp_path: Path) -> None:
    target_dir = tmp_path / "media" / "chatgpt" / "demo-project"
    candidate = ChatGPTImageCandidate(
        source_url="https://chatgpt.com/backend-api/estuary/content?id=file_corrupt",
        file_id="file_corrupt",
        conversation_url="https://chatgpt.com/c/corrupt",
    )
    catalog = ChatGPTImageCatalog.build(target_dir)
    corrupt_path = target_dir / "img_file_corrupt.png"
    corrupt_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt_path.write_bytes(b"\x89PNG\r\n\x1a\n" + (b"damaged" * 32))
    catalog.entries_by_file_id[candidate.file_id] = ChatGPTCatalogEntry(
        file_id=candidate.file_id,
        relative_path=corrupt_path.name,
        content_sha256="corrupt",
        content_bytes=corrupt_path.stat().st_size,
        source_url=candidate.source_url,
        conversation_url=candidate.conversation_url,
        alt_text="",
        width=0,
        height=0,
        first_seen_at="",
        last_seen_at="",
    )
    catalog.save()

    reloaded = ChatGPTImageCatalog.build(target_dir)

    assert candidate.file_id not in reloaded.entries_by_file_id
    assert not corrupt_path.exists()


def test_chatgpt_catalog_prunes_cached_thumbnail_encodings_during_load(tmp_path: Path) -> None:
    target_dir = tmp_path / "media" / "chatgpt" / "demo-project"
    thumbnail_path = target_dir / "img_file_thumbnail.webp"
    thumbnail_path.parent.mkdir(parents=True)
    thumbnail_payload = _visual_test_image_payload("WEBP")
    thumbnail_path.write_bytes(thumbnail_payload)
    catalog = ChatGPTImageCatalog(
        target_dir,
        {
            "file_thumbnail": ChatGPTCatalogEntry(
                file_id="file_thumbnail",
                relative_path=thumbnail_path.name,
                content_sha256="thumbnail",
                content_bytes=len(thumbnail_payload),
                source_url=(
                    "https://chatgpt.com/backend-api/estuary/content?"
                    "id=asset%23file_thumbnail%23thumbnail"
                ),
                conversation_url="https://chatgpt.com/c/thumbnail",
                alt_text="",
                width=512,
                height=512,
                first_seen_at="2026-08-11T00:00:00Z",
                last_seen_at="2026-08-11T00:00:00Z",
            )
        },
    )
    catalog.save()

    repaired = ChatGPTImageCatalog.build(target_dir)

    assert repaired.repair_result.removed_file_ids == ("file_thumbnail",)
    assert repaired.repair_result.removed_local_files == 1
    assert not thumbnail_path.exists()
    assert repaired.summarize() == 0


def test_chatgpt_skips_images_above_the_universal_cache_size_limit(tmp_path: Path) -> None:
    target_dir = tmp_path / "media" / "chatgpt" / "demo-project"
    candidate = ChatGPTImageCandidate(
        source_url="https://chatgpt.com/backend-api/estuary/content?id=file_oversized",
        file_id="file_oversized",
        conversation_url="https://chatgpt.com/c/oversized",
    )
    catalog = ChatGPTImageCatalog.build(target_dir)

    with pytest.raises(ChatGPTImageSizeLimitError, match="cache limit"):
        download_chatgpt_image(
            _FakeContext(),
            catalog,
            target_dir,
            candidate,
            max_file_size_bytes=len(PNG_PAYLOAD) - 1,
        )

    assert catalog.summarize() == 0
    assert not list(target_dir.glob("img_*"))


def test_chatgpt_catalog_removes_lower_quality_visual_duplicates(tmp_path: Path) -> None:
    target_dir = tmp_path / "media" / "chatgpt" / "demo-project"
    target_dir.mkdir(parents=True)
    high_content = _visual_test_image_payload("JPEG", quality=90)
    low_content = _visual_test_image_payload("JPEG", quality=20)
    assert len(high_content) > len(low_content)
    high_path = target_dir / "img_high.jpg"
    low_path = target_dir / "img_low.jpg"
    high_path.write_bytes(high_content)
    low_path.write_bytes(low_content)
    conversation_url = "https://chatgpt.com/g/project/c/visual-duplicate"
    catalog = ChatGPTImageCatalog(
        target_dir,
        {
            "file-high": ChatGPTCatalogEntry(
                file_id="file-high",
                relative_path=high_path.name,
                content_sha256="high",
                content_bytes=len(high_content),
                source_url="https://chatgpt.com/backend-api/estuary/content?id=file-high",
                conversation_url=conversation_url,
                alt_text="High quality copy",
                width=0,
                height=0,
                first_seen_at="2026-08-10T00:00:00Z",
                last_seen_at="2026-08-10T00:00:00Z",
            ),
            "file-low": ChatGPTCatalogEntry(
                file_id="file-low",
                relative_path=low_path.name,
                content_sha256="low",
                content_bytes=len(low_content),
                source_url="https://chatgpt.com/backend-api/estuary/content?id=file-low",
                conversation_url=conversation_url,
                alt_text="Citation thumbnail",
                width=0,
                height=0,
                first_seen_at="2026-08-10T00:00:00Z",
                last_seen_at="2026-08-10T00:00:00Z",
            ),
        },
    )

    result = catalog.deduplicate_visual_duplicates()

    assert result.removed_file_ids == ("file-low",)
    assert result.reclaimed_bytes == len(low_content)
    assert high_path.exists()
    assert not low_path.exists()
    assert set(catalog.entries_by_file_id) == {"file-high"}


def test_chatgpt_catalog_does_not_keep_an_incoming_lower_quality_duplicate(tmp_path: Path) -> None:
    target_dir = tmp_path / "media" / "chatgpt" / "demo-project"
    target_dir.mkdir(parents=True)
    high_content = _visual_test_image_payload("JPEG", quality=90)
    low_content = _visual_test_image_payload("JPEG", quality=20)
    assert len(high_content) > len(low_content)
    high_path = target_dir / "img_high.jpg"
    low_path = target_dir / "img_low.jpg"
    high_path.write_bytes(high_content)
    low_path.write_bytes(low_content)
    conversation_url = "https://chatgpt.com/g/project/c/visual-duplicate"
    catalog = ChatGPTImageCatalog.build(target_dir)
    high_candidate = ChatGPTImageCandidate(
        source_url="https://chatgpt.com/backend-api/estuary/content?id=file-high",
        file_id="file-high",
        conversation_url=conversation_url,
    )
    low_candidate = ChatGPTImageCandidate(
        source_url="https://chatgpt.com/backend-api/estuary/content?id=file-low",
        file_id="file-low",
        conversation_url=conversation_url,
    )

    assert catalog.register_download(
        high_candidate,
        high_path.name,
        "high",
        len(high_content),
        "2026-08-10T00:00:00Z",
    )
    assert not catalog.register_download(
        low_candidate,
        low_path.name,
        "low",
        len(low_content),
        "2026-08-10T00:00:01Z",
    )
    assert high_path.exists()
    assert not low_path.exists()
    assert set(catalog.entries_by_file_id) == {"file-high"}


def test_chatgpt_retries_a_transient_direct_image_failure(tmp_path: Path) -> None:
    class _TransientResponse:
        ok = False
        status = 503
        headers: dict[str, str] = {}

    class _RetryRequest:
        def __init__(self) -> None:
            self.urls: list[str] = []
            self.responses = [_TransientResponse(), _FakeResponse()]

        def get(self, url: str, **_kwargs):
            self.urls.append(url)
            return self.responses.pop(0)

    class _RetryContext:
        def __init__(self) -> None:
            self.request = _RetryRequest()

    target_dir = tmp_path / "media" / "chatgpt" / "demo-project"
    candidate = ChatGPTImageCandidate(
        source_url="https://chatgpt.com/backend-api/estuary/content?id=file_retry",
        file_id="file_retry",
        conversation_url="https://chatgpt.com/g/project/c/retry",
    )
    context = _RetryContext()
    catalog = ChatGPTImageCatalog.build(target_dir)

    with patch("app.core.chatgpt_downloader.time.sleep") as sleep:
        assert download_chatgpt_image(context, catalog, target_dir, candidate)

    assert len(context.request.urls) == 2
    sleep.assert_called_once_with(0.5)
    assert catalog.summarize() == 1


def test_chatgpt_streams_first_party_original_through_safari(tmp_path: Path) -> None:
    target_dir = tmp_path / "media" / "chatgpt" / "demo-project"
    candidate = ChatGPTImageCandidate(
        source_url="https://chatgpt.com/backend-api/estuary/content?id=file_safari",
        file_id="file_safari",
        conversation_url="https://chatgpt.com/c/safari",
        request_headers={"authorization": "Bearer test-token"},
    )
    context = SafariContext(candidate.conversation_url)
    page = SafariPage(context, window_id=123)
    context.pages.append(page)
    catalog = ChatGPTImageCatalog.build(target_dir)
    streamed_headers: list[dict[str, str]] = []

    def stream_to_path(_url, destination_path, _should_stop, headers=None):
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path.write_bytes(PNG_PAYLOAD)
        streamed_headers.append(dict(headers or {}))
        return "image/png", False

    with patch.object(page, "download_to_path", side_effect=stream_to_path):
        assert download_chatgpt_image(context, catalog, target_dir, candidate)

    assert streamed_headers[0]["authorization"] == "Bearer test-token"
    assert (target_dir / "img_file_safari.png").read_bytes() == PNG_PAYLOAD
    assert catalog.summarize() == 1


def test_chatgpt_does_not_cache_index_thumbnail_when_safari_original_is_gone(tmp_path: Path) -> None:
    target_dir = tmp_path / "media" / "chatgpt" / "demo-project"
    direct_url = "https://chatgpt.com/backend-api/estuary/content?id=file_safari_fallback"
    fallback_url = direct_url + "&encoding=thumbnail"
    candidate = ChatGPTImageCandidate(
        source_url=direct_url,
        file_id="file_safari_fallback",
        conversation_url="https://chatgpt.com/c/safari-fallback",
        request_headers={"authorization": "Bearer test-token"},
    )
    context = SafariContext(candidate.conversation_url)
    page = SafariPage(context, window_id=123)
    context.pages.append(page)
    catalog = ChatGPTImageCatalog.build(target_dir)
    streamed_urls: list[str] = []

    class _NotFoundResponse:
        ok = False
        status = 404

    def stream_to_path(url, destination_path, _should_stop, headers=None):
        streamed_urls.append(url)
        raise RuntimeError("Safari media request returned HTTP 404 with 0 bytes.")

    with patch.object(page, "download_to_path", side_effect=stream_to_path), patch.object(
        context.request,
        "get",
        return_value=_NotFoundResponse(),
    ):
        with pytest.raises(RuntimeError, match="HTTP 404"):
            download_chatgpt_image(context, catalog, target_dir, candidate)

    assert streamed_urls == [direct_url]
    assert fallback_url not in streamed_urls
    assert catalog.summarize() == 0


def test_chatgpt_reset_removes_only_the_dedicated_cache(tmp_path: Path) -> None:
    target_dir = tmp_path / "media" / "chatgpt" / "demo-project"
    candidate = ChatGPTImageCandidate(
        source_url="https://chatgpt.com/backend-api/estuary/content?id=file_reset",
        file_id="file_reset",
        conversation_url="https://chatgpt.com/g/project/c/reset",
        message_role="assistant",
    )
    catalog = ChatGPTImageCatalog.build(target_dir)
    download_chatgpt_image(_FakeContext(), catalog, target_dir, candidate)
    partial_dir = target_dir / ".chatgpt-partial"
    partial_dir.mkdir(exist_ok=True)
    (partial_dir / "orphan.part").write_bytes(b"partial")
    unrelated = tmp_path / "keep.txt"
    unrelated.write_text("keep", encoding="utf-8")

    result = reset_chatgpt_state(target_dir=target_dir)

    assert result.removed_media_files == 1
    assert result.removed_state_files == 1
    assert result.removed_partial_files == 1
    assert not target_dir.exists() or not any(target_dir.iterdir())
    assert unrelated.read_text(encoding="utf-8") == "keep"


def test_chatgpt_browser_probe_verifies_chromium_navigation_and_session() -> None:
    class _ProbeResponse:
        ok = True

        @staticmethod
        def text() -> str:
            return '{"accessToken":"test-token"}'

    class _ProbePage:
        def __init__(self) -> None:
            self.goto_calls: list[tuple[str, str, int]] = []

        def goto(self, url: str, wait_until: str, timeout: int) -> None:
            self.goto_calls.append((url, wait_until, timeout))

        @staticmethod
        def evaluate(_expression: str) -> dict[str, object]:
            return {
                "ok": True,
                "status": 200,
                "bodyText": '{"accessToken":"test-token"}',
                "error": "",
            }

    class _ProbeRequest:
        def get(self, _url: str, **_kwargs) -> _ProbeResponse:
            return _ProbeResponse()

    page = _ProbePage()
    context = type("ProbeContext", (), {"pages": [page], "request": _ProbeRequest()})()

    with patch(
        "app.core.browser_sessions.sync_playwright_or_error",
        return_value=nullcontext(object()),
    ), patch(
        "app.core.browser_sessions.launch_chromium_context",
        return_value=nullcontext(context),
    ):
        ready = probe_browser_session(
            "chatgpt",
            "edge",
            CrawlConfig(chatgpt_project_url="https://example.com/project"),
        )

    assert ready["can_download"] is True
    assert ready["account_name"] == "ChatGPT account"
    assert "account is ready" in ready["message"]
    assert page.goto_calls == [("https://chatgpt.com/", "domcontentloaded", 30_000)]


def test_chatgpt_browser_probe_verifies_safari_session_in_a_hidden_context(macos_host) -> None:
    class _ProbeResponse:
        ok = True
        status = 200

        @staticmethod
        def text() -> str:
            return '{"accessToken":"test-token"}'

    class _ProbePage:
        def __init__(self) -> None:
            self.goto_calls: list[tuple[str, str, int]] = []

        def goto(self, url: str, wait_until: str, timeout: int) -> None:
            self.goto_calls.append((url, wait_until, timeout))

        def wait_for_load_state(self, _state: str, _timeout: int) -> None:
            pass

    class _ProbeRequest:
        def get(self, _url: str, **_kwargs) -> _ProbeResponse:
            return _ProbeResponse()

    probe_page = _ProbePage()

    class _ProbeContext:
        primary_page = probe_page
        request = _ProbeRequest()

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _traceback) -> bool:
            return False

    with patch("app.core.browser_sessions.SafariContext", return_value=_ProbeContext()), patch(
        "app.core.browser_sessions.time.sleep"
    ):
        result = probe_browser_session(
            "chatgpt",
            "safari",
            CrawlConfig(chatgpt_browser="safari"),
        )

    assert result["logged_in"] is True
    assert result["can_download"] is True
    assert "account is ready" in result["message"]
    assert probe_page.goto_calls == [("https://chatgpt.com/", "domcontentloaded", 60_000)]


def test_chatgpt_accepts_a_single_chat_session_url() -> None:
    session_url = "https://chatgpt.com/g/g-p-demo-project/c/conversation-123"

    assert is_chatgpt_conversation_url(session_url)
    assert chatgpt_conversation_id(session_url) == "conversation-123"
    assert is_chatgpt_conversation_url("https://chatgpt.com/c/conversation-123?oai-dm=1")
    assert chatgpt_conversation_id("https://example.com/c/conversation-123") == ""
    assert not is_chatgpt_conversation_url(DEFAULT_CHATGPT_PROJECT_URL)
    assert not is_chatgpt_conversation_url("https://example.com/c/conversation-123")


def test_chatgpt_uses_a_single_chat_session_without_scanning_a_project() -> None:
    session_url = "https://chatgpt.com/c/conversation-123"
    state = TaskState("test")

    with patch("app.core.chatgpt_downloader.open_chatgpt_page") as open_page:
        conversation_urls = collect_project_conversation_urls(
            object(),
            session_url,
            state,
            should_stop=lambda: False,
        )

    assert conversation_urls == [session_url]
    open_page.assert_not_called()


def test_chatgpt_project_api_collects_authoritative_session_titles() -> None:
    conversation_id = "69523533-2780-8321-ac61-b6fd762cb455"

    class _ProjectResponse:
        ok = True
        status = 200

        @staticmethod
        def text() -> str:
            return json.dumps(
                {
                    "items": [
                        {
                            "id": conversation_id,
                            "title": "master 21",
                        }
                    ],
                    "cursor": "",
                }
            )

    class _ProjectRequest:
        @staticmethod
        def get(_url: str, **_kwargs):
            return _ProjectResponse()

    class _ProjectContext:
        request = _ProjectRequest()

    class _ProjectPage:
        context = _ProjectContext()

    titles_by_id: dict[str, str] = {}
    state = TaskState("test")
    with patch("app.core.chatgpt_downloader.open_chatgpt_page"):
        conversation_urls = collect_project_conversation_urls(
            _ProjectPage(),
            DEFAULT_CHATGPT_PROJECT_URL,
            state,
            should_stop=lambda: False,
            request_headers={"authorization": "Bearer test-token"},
            conversation_titles_by_id=titles_by_id,
        )

    assert conversation_urls == [
        "https://chatgpt.com/g/g-p-demo-project/"
        f"c/{conversation_id}"
    ]
    assert titles_by_id == {conversation_id: "master 21"}
    assert state.snapshot()["discovered_tweets"] == 1
    assert state.snapshot()["queued_tweets"] == 1


def test_chatgpt_history_api_collects_all_sessions_outside_the_media_project() -> None:
    class _HistoryResponse:
        ok = True
        status = 200

        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def text(self) -> str:
            return json.dumps(self._payload)

    class _HistoryRequest:
        calls: list[str] = []

        @classmethod
        def get(cls, url: str, **_kwargs):
            cls.calls.append(url)
            if "offset=0" in url:
                return _HistoryResponse(
                    {
                        "items": [
                            {"id": "studio-session", "title": "Studio image session"},
                            *[
                                {"id": f"filler-{index}", "title": f"Filler {index}"}
                                for index in range(99)
                            ],
                        ]
                    }
                )
            return _HistoryResponse(
                {
                    "items": [
                        {"id": "personal-session", "title": "Personal text session"},
                    ]
                }
            )

    class _HistoryContext:
        request = _HistoryRequest()

    titles: dict[str, str] = {}
    urls = _collect_all_chatgpt_conversation_urls_via_api(
        _HistoryContext(),
        DEFAULT_CHATGPT_PROJECT_URL,
        {"authorization": "Bearer test-token"},
        TaskState("test"),
        should_stop=lambda: False,
        conversation_titles_by_id=titles,
    )

    assert urls[0] == "https://chatgpt.com/c/studio-session"
    assert urls[-1] == "https://chatgpt.com/c/personal-session"
    assert len(urls) == 101
    assert titles["studio-session"] == "Studio image session"
    assert titles["personal-session"] == "Personal text session"
    assert any("/backend-api/conversations?" in url for url in _HistoryRequest.calls)


class _ClosableBrowserContext:
    def new_page(self) -> object:
        return object()

    def close(self) -> None:
        pass


def test_chatgpt_sync_launches_edge_offscreen_with_a_rendered_window(tmp_path: Path) -> None:
    state = TaskState("test")
    browser_context = _ClosableBrowserContext()

    with patch(
        "app.core.chatgpt_downloader.sync_playwright",
        return_value=nullcontext(object()),
    ), patch(
        "app.core.chatgpt_downloader.launch_chromium_context",
        return_value=nullcontext(browser_context),
    ) as launch_context, patch(
        "app.core.chatgpt_downloader.collect_project_conversation_urls",
        return_value=[],
    ):
        result = sync_chatgpt_images(
            state,
            config=CrawlConfig(),
            target_dir=tmp_path / "media" / "chatgpt" / DEFAULT_CHATGPT_PROJECT_NAME,
        )

    assert result.discovered_conversations == 0
    assert launch_context.call_args.kwargs["headless"] is False
    assert launch_context.call_args.kwargs["background_window"] is True


def test_chatgpt_sync_accepts_safari_without_playwright(tmp_path: Path, macos_host) -> None:
    state = TaskState("test")
    browser_context = _ClosableBrowserContext()

    with patch("app.core.chatgpt_downloader.sync_playwright", None), patch(
        "app.core.chatgpt_downloader._launch_chatgpt_browser_context",
        return_value=nullcontext(browser_context),
    ) as launch_context, patch(
        "app.core.chatgpt_downloader.collect_project_conversation_urls",
        return_value=[],
    ):
        result = sync_chatgpt_images(
            state,
            config=CrawlConfig(chatgpt_browser="safari"),
            target_dir=tmp_path / "media" / "chatgpt" / DEFAULT_CHATGPT_PROJECT_NAME,
        )

    assert result.discovered_conversations == 0
    assert launch_context.call_args.args[0].engine == "safari"
    assert launch_context.call_args.args[1] == DEFAULT_CHATGPT_PROJECT_URL
    assert any("offscreen Safari" in event for event in state.snapshot()["recent_events"])


def test_chatgpt_text_sync_uses_safari_home_and_skips_media_pipeline(
    tmp_path: Path, macos_host
) -> None:
    state = TaskState("test")
    browser_context = _ClosableBrowserContext()
    conversation_urls = [
        "https://chatgpt.com/c/session-1",
        "https://chatgpt.com/c/session-2",
    ]

    with patch("app.core.chatgpt_downloader.sync_playwright", None), patch(
        "app.core.chatgpt_downloader._launch_chatgpt_browser_context",
        return_value=nullcontext(browser_context),
    ) as launch_context, patch(
        "app.core.chatgpt_downloader.open_chatgpt_page",
    ) as open_page, patch(
        "app.core.chatgpt_downloader._load_chatgpt_session_request_headers",
        return_value={"authorization": "Bearer demo"},
    ), patch(
        "app.core.chatgpt_downloader._collect_all_chatgpt_conversation_urls_via_api",
        return_value=conversation_urls,
    ) as collect_all, patch(
        "app.core.chatgpt_downloader.cache_chatgpt_conversation_history",
        return_value=(2, 4, 0),
    ) as cache_history, patch(
        "app.core.chatgpt_downloader.collect_project_conversation_urls",
    ) as collect_project, patch(
        "app.core.chatgpt_downloader.collect_chatgpt_project_index_images",
    ) as collect_media:
        result = sync_chatgpt_images(
            state,
            config=CrawlConfig(chatgpt_browser="safari", chatgpt_text_startup_timeout_seconds=32.0,
                               cache_scan_waits={"chatgpt_text": 1.3}),
            target_dir=tmp_path / "media" / "chatgpt" / DEFAULT_CHATGPT_PROJECT_NAME,
            content_mode="text",
        )

    assert result.discovered_conversations == 2
    assert launch_context.call_args.args[0].engine == "safari"
    assert launch_context.call_args.args[1] == CHATGPT_HOME_URL
    open_page.assert_called_once()
    collect_all.assert_called_once()
    cache_history.assert_called_once()
    assert cache_history.call_args.kwargs["scan_wait_seconds"] == 1.3
    assert open_page.call_args.kwargs["startup_timeout_seconds"] == 32.0
    collect_project.assert_not_called()
    collect_media.assert_not_called()
    snapshot = state.snapshot()
    assert snapshot["processed_tweets"] == 2
    assert snapshot["progress_unit"] == "sessions"
    assert snapshot["discovery_complete"] is True


def test_chatgpt_blank_media_url_scans_all_sessions_as_assistant_only(
    tmp_path: Path, macos_host
) -> None:
    state = TaskState("test")
    browser_context = _ClosableBrowserContext()
    conversation_urls = [
        "https://chatgpt.com/c/session-1",
        "https://chatgpt.com/c/session-2",
    ]

    with patch("app.core.chatgpt_downloader.sync_playwright", None), patch(
        "app.core.chatgpt_downloader._launch_chatgpt_browser_context",
        return_value=nullcontext(browser_context),
    ) as launch_context, patch(
        "app.core.chatgpt_downloader.open_chatgpt_page",
    ) as open_page, patch(
        "app.core.chatgpt_downloader._load_chatgpt_session_request_headers",
        return_value={"authorization": "Bearer demo"},
    ), patch(
        "app.core.chatgpt_downloader._collect_all_chatgpt_conversation_urls_via_api",
        return_value=conversation_urls,
    ) as collect_all, patch(
        "app.core.chatgpt_downloader.collect_project_conversation_urls",
    ) as collect_project, patch(
        "app.core.chatgpt_downloader.collect_chatgpt_project_index_images",
    ) as collect_project_media, patch(
        "app.core.chatgpt_downloader._iter_chatgpt_conversation_results",
        return_value=iter(()),
    ) as conversation_results:
        result = sync_chatgpt_images(
            state,
            config=CrawlConfig(
                chatgpt_browser="safari",
                chatgpt_project_url="",
            ),
            target_dir=tmp_path / "media" / "chatgpt" / DEFAULT_CHATGPT_PROJECT_NAME,
        )

    assert result.discovered_conversations == 2
    assert launch_context.call_args.args[1] == CHATGPT_HOME_URL
    open_page.assert_called_once()
    collect_all.assert_called_once()
    collect_project.assert_not_called()
    collect_project_media.assert_not_called()
    conversation_results.assert_called_once()
    assert conversation_results.call_args.kwargs["assistant_only"] is True
    assert any(
        "User-uploaded media will be excluded" in event
        for event in state.snapshot()["recent_events"]
    )


def test_chatgpt_direct_session_refresh_skips_the_global_project_image_index(
    tmp_path: Path, macos_host
) -> None:
    session_url = "https://chatgpt.com/g/g-p-demo/c/session-123"
    state = TaskState("test")
    browser_context = _ClosableBrowserContext()

    with patch("app.core.chatgpt_downloader.sync_playwright", None), patch(
        "app.core.chatgpt_downloader._launch_chatgpt_browser_context",
        return_value=nullcontext(browser_context),
    ), patch(
        "app.core.chatgpt_downloader.collect_project_conversation_urls",
        return_value=[session_url],
    ), patch(
        "app.core.chatgpt_downloader.collect_chatgpt_project_index_images",
    ) as project_index, patch(
        "app.core.chatgpt_downloader._iter_chatgpt_conversation_results",
        return_value=iter(()),
    ) as conversation_results:
        result = sync_chatgpt_images(
            state,
            config=CrawlConfig(
                chatgpt_browser="safari",
                chatgpt_project_url=session_url,
            ),
            target_dir=tmp_path / "media" / "chatgpt" / DEFAULT_CHATGPT_PROJECT_NAME,
        )

    assert result.discovered_conversations == 1
    project_index.assert_not_called()
    conversation_results.assert_called_once()
    assert conversation_results.call_args.args[7] == 1
    assert any(
        "skipping the global project image index" in event
        for event in state.snapshot()["recent_events"]
    )
    assert any(
        "Starting 1 ChatGPT worker" in event
        for event in state.snapshot()["recent_events"]
    )


def test_chatgpt_sync_uses_the_project_image_index_without_legacy_page_scans(tmp_path: Path) -> None:
    state = TaskState("test")
    browser_context = _ClosableBrowserContext()
    candidate = ChatGPTImageCandidate(
        source_url="https://chatgpt.com/backend-api/estuary/content?id=file_index_only",
        file_id="file_index_only",
        conversation_url="https://chatgpt.com/c/project-index-only",
    )

    with patch(
        "app.core.chatgpt_downloader.sync_playwright",
        return_value=nullcontext(object()),
    ), patch(
        "app.core.chatgpt_downloader.launch_chromium_context",
        return_value=nullcontext(browser_context),
    ), patch(
        "app.core.chatgpt_downloader.collect_project_conversation_urls",
        return_value=[candidate.conversation_url],
    ), patch(
        "app.core.chatgpt_downloader.collect_chatgpt_project_index_images",
        return_value=[candidate],
    ), patch(
        "app.core.chatgpt_downloader._iter_chatgpt_index_image_results",
        return_value=iter([ChatGPTImageDownloadWorkResult(candidate.file_id, skipped=True)]),
    ), patch("app.core.chatgpt_downloader._iter_chatgpt_conversation_results") as conversation_results:
        result = sync_chatgpt_images(
            state,
            config=CrawlConfig(),
            target_dir=tmp_path / "media" / "chatgpt" / DEFAULT_CHATGPT_PROJECT_NAME,
        )

    assert result.discovered_conversations == 1
    assert result.discovered_images == 1
    snapshot = state.snapshot()
    assert snapshot["queued_tweets"] == 1
    assert snapshot["processed_tweets"] == 1
    assert snapshot["progress_unit"] == "images"
    conversation_results.assert_not_called()


class _ConversationPage:
    def __init__(self) -> None:
        self.waits: list[int] = []

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.waits.append(milliseconds)


def test_chatgpt_scans_the_nested_message_view_in_both_directions() -> None:
    page = _ConversationPage()
    directions: list[str] = []
    raw_candidate = {
        "sourceUrl": "https://chatgpt.com/backend-api/estuary/content?id=file_user_image",
        "fileId": "file_user_image",
        "messageRole": "user",
    }

    def scroll_message_view(_page, direction: str) -> dict[str, object]:
        directions.append(direction)
        return {"height": 1_200, "atBoundary": True, "moved": False}

    with patch("app.core.chatgpt_downloader.open_chatgpt_page"), patch(
        "app.core.chatgpt_downloader._extract_original_image_payloads",
        return_value=[raw_candidate],
    ), patch(
        "app.core.chatgpt_downloader._scroll_chatgpt_message_view",
        side_effect=scroll_message_view,
    ):
        candidates = collect_conversation_images(
            page,
            "https://chatgpt.com/c/conversation-123",
            should_stop=lambda: False,
            scan_wait_seconds=0.2,
        )

    assert candidates == []
    assert set(directions) == {"top", "bottom"}
    assert page.waits
    assert set(page.waits) == {200}


def test_chatgpt_recycles_tabs_after_recoverable_page_failures() -> None:
    assert _is_recoverable_chatgpt_page_error(RuntimeError("Page crashed"))
    assert _is_recoverable_chatgpt_page_error(RuntimeError("frame was detached"))
    assert _is_recoverable_chatgpt_page_error(RuntimeError("ERR_ABORTED"))
    assert _is_recoverable_chatgpt_page_error(RuntimeError("net::ERR_SSL_BAD_RECORD_MAC_ALERT"))
    assert _is_recoverable_chatgpt_page_error(RuntimeError("net::ERR_SSL_VERSION_OR_CIPHER_MISMATCH"))
    assert _is_recoverable_chatgpt_page_error(RuntimeError("net::ERR_CONNECTION_RESET"))
    assert _is_recoverable_chatgpt_page_error(RuntimeError("chrome-error://chromewebdata/"))
    assert _is_recoverable_chatgpt_page_error(RuntimeError("ChatGPT startup timed out after 60 seconds"))
    assert not _is_recoverable_chatgpt_page_error(RuntimeError("HTTP 403"))


def test_chatgpt_parallel_iterator_partitions_conversations_across_bounded_workers(tmp_path: Path) -> None:
    assignments_seen: list[list[int]] = []

    def fake_worker(
        assignments,
        _descriptor,
        _catalog,
        _target_dir,
        _startup_timeout_seconds,
        _scan_wait_seconds,
        _should_stop,
        result_queue,
    ) -> None:
        assignments_seen.append([index for index, _url in assignments])
        for index, url in assignments:
            result_queue.put(ChatGPTConversationWorkResult(index, url))

    urls = [f"https://chatgpt.com/c/conversation-{index}" for index in range(5)]
    with patch("app.core.chatgpt_downloader._chatgpt_conversation_worker", side_effect=fake_worker):
        results = list(
            _iter_chatgpt_conversation_results(
                urls,
                object(),
                ChatGPTImageCatalog.build(tmp_path / "demo-project"),
                tmp_path / "demo-project",
                60,
                0.5,
                lambda: False,
                worker_count=3,
            )
        )

    assert sorted(result.conversation_index for result in results) == [1, 2, 3, 4, 5]
    assert len(assignments_seen) == 3
    assert sorted(index for assignment in assignments_seen for index in assignment) == [1, 2, 3, 4, 5]


def test_chatgpt_assistant_only_worker_excludes_uploaded_and_unknown_media(
    tmp_path: Path,
) -> None:
    conversation_url = "https://chatgpt.com/c/conversation-123"
    candidates = [
        ChatGPTImageCandidate(
            source_url="https://chatgpt.com/backend-api/estuary/content?id=file_generated",
            file_id="file_generated",
            conversation_url=conversation_url,
            message_role="assistant",
        ),
        ChatGPTImageCandidate(
            source_url="https://chatgpt.com/backend-api/estuary/content?id=file_uploaded",
            file_id="file_uploaded",
            conversation_url=conversation_url,
            message_role="user",
        ),
        ChatGPTImageCandidate(
            source_url="https://chatgpt.com/backend-api/estuary/content?id=file_unknown",
            file_id="file_unknown",
            conversation_url=conversation_url,
        ),
    ]
    result_queue: Queue[ChatGPTConversationWorkResult] = Queue()
    page = object()

    with patch(
        "app.core.chatgpt_downloader._launch_chatgpt_browser_context",
        return_value=nullcontext(object()),
    ), patch(
        "app.core.chatgpt_downloader._chatgpt_context_page",
        return_value=page,
    ), patch(
        "app.core.chatgpt_downloader._collect_chatgpt_conversation_with_recovery",
        return_value=(page, candidates, None),
    ), patch(
        "app.core.chatgpt_downloader.download_chatgpt_image",
        return_value=True,
    ) as download_image:
        _chatgpt_conversation_worker(
            [(1, conversation_url)],
            object(),
            object(),
            tmp_path,
            60,
            0.5,
            lambda: False,
            result_queue,
            assistant_only=True,
        )

    result = result_queue.get_nowait()
    assert result.candidate_file_ids == ("file_generated",)
    assert result.downloaded_count == 1
    assert download_image.call_count == 1
    assert download_image.call_args.args[3].file_id == "file_generated"


def test_chatgpt_project_index_iterator_partitions_direct_image_downloads(tmp_path: Path) -> None:
    assignments_seen: list[list[str]] = []

    def fake_worker(candidates, _descriptor, _catalog, _target_dir, _should_stop, result_queue) -> None:
        assignments_seen.append([candidate.file_id for candidate in candidates])
        for candidate in candidates:
            result_queue.put(ChatGPTImageDownloadWorkResult(candidate.file_id, downloaded=True))

    candidates = [
        ChatGPTImageCandidate(
            source_url=f"https://chatgpt.com/backend-api/estuary/content?id=file_index_{index}",
            file_id=f"file_index_{index}",
            conversation_url="https://chatgpt.com/c/project-conversation",
        )
        for index in range(5)
    ]
    with patch("app.core.chatgpt_downloader._chatgpt_index_image_worker", side_effect=fake_worker):
        results = list(
            _iter_chatgpt_index_image_results(
                candidates,
                object(),
                ChatGPTImageCatalog.build(tmp_path / "demo-project"),
                tmp_path / "demo-project",
                lambda: False,
                worker_count=3,
            )
        )

    assert {result.candidate_file_id for result in results} == {candidate.file_id for candidate in candidates}
    assert len(assignments_seen) == 3
    assert {file_id for assignment in assignments_seen for file_id in assignment} == {
        candidate.file_id for candidate in candidates
    }


def test_chatgpt_project_index_iterator_does_not_start_workers_after_stop(tmp_path: Path) -> None:
    candidate = ChatGPTImageCandidate(
        source_url="https://chatgpt.com/backend-api/estuary/content?id=file_stopped",
        file_id="file_stopped",
        conversation_url="https://chatgpt.com/c/stopped",
    )

    with patch("app.core.chatgpt_downloader._chatgpt_index_image_worker") as worker:
        results = list(
            _iter_chatgpt_index_image_results(
                [candidate],
                object(),
                ChatGPTImageCatalog.build(tmp_path / "demo-project"),
                tmp_path / "demo-project",
                should_stop=lambda: True,
                worker_count=3,
            )
        )

    assert results == []
    worker.assert_not_called()


def test_chatgpt_project_index_iterator_skips_complete_files_before_worker_start(tmp_path: Path) -> None:
    target_dir = tmp_path / "demo-project"
    target_dir.mkdir()
    existing_path = target_dir / "img_file_existing.png"
    existing_path.write_bytes(PNG_PAYLOAD)
    catalog = ChatGPTImageCatalog(
        target_dir,
        {
            "file_existing": ChatGPTCatalogEntry(
                file_id="file_existing",
                relative_path=existing_path.name,
                content_sha256="",
                content_bytes=len(PNG_PAYLOAD),
                source_url="https://example.com/existing.png",
                conversation_url="https://chatgpt.com/c/project-conversation",
                alt_text="",
                width=0,
                height=0,
                first_seen_at="",
                last_seen_at="",
            )
        },
    )
    candidates = [
        ChatGPTImageCandidate(
            source_url="https://example.com/existing.png",
            file_id="file_existing",
            conversation_url="https://chatgpt.com/c/project-conversation",
        ),
        ChatGPTImageCandidate(
            source_url="https://example.com/missing.png",
            file_id="file_missing",
            conversation_url="https://chatgpt.com/c/project-conversation",
        ),
    ]
    assignments_seen: list[list[str]] = []

    def fake_worker(worker_candidates, _descriptor, _catalog, _target_dir, _should_stop, result_queue) -> None:
        assignments_seen.append([candidate.file_id for candidate in worker_candidates])
        for candidate in worker_candidates:
            result_queue.put(ChatGPTImageDownloadWorkResult(candidate.file_id, downloaded=True))

    with patch("app.core.chatgpt_downloader._chatgpt_index_image_worker", side_effect=fake_worker):
        results = list(
            _iter_chatgpt_index_image_results(
                candidates,
                object(),
                catalog,
                target_dir,
                lambda: False,
                worker_count=3,
            )
        )

    assert assignments_seen == [["file_missing"]]
    assert {result.candidate_file_id for result in results} == {"file_existing", "file_missing"}
    existing_result = next(result for result in results if result.candidate_file_id == "file_existing")
    assert existing_result.skipped is True


def test_chatgpt_project_index_worker_refreshes_authorization_before_download(tmp_path: Path) -> None:
    candidate = ChatGPTImageCandidate(
        source_url="https://chatgpt.com/backend-api/estuary/content?id=file_index_refresh",
        file_id="file_index_refresh",
        conversation_url="https://chatgpt.com/c/project-conversation",
        request_headers={
            "authorization": "Bearer stale-token",
            "oai-device-id": "device-id",
        },
    )
    context = object()
    result_queue: Queue[ChatGPTImageDownloadWorkResult] = Queue()

    with patch(
        "app.core.chatgpt_downloader._launch_chatgpt_browser_context",
        return_value=nullcontext(context),
    ), patch(
        "app.core.chatgpt_downloader._load_chatgpt_session_request_headers",
        return_value={"authorization": "Bearer fresh-token"},
    ) as load_headers, patch(
        "app.core.chatgpt_downloader.download_chatgpt_image",
        return_value=True,
    ) as download:
        _chatgpt_index_image_worker(
            [candidate],
            object(),
            ChatGPTImageCatalog.build(tmp_path / "demo-project"),
            tmp_path / "demo-project",
            lambda: False,
            result_queue,
        )

    effective_candidate = download.call_args.args[3]
    assert effective_candidate.request_headers == {
        "authorization": "Bearer fresh-token",
        "oai-device-id": "device-id",
    }
    load_headers.assert_called_once_with(context, candidate.conversation_url)
    assert result_queue.get_nowait().downloaded is True


def test_chatgpt_project_index_worker_retries_transient_startup_failure(tmp_path: Path) -> None:
    candidate = ChatGPTImageCandidate(
        source_url="https://chatgpt.com/backend-api/estuary/content?id=file_index_retry",
        file_id="file_index_retry",
        conversation_url="https://chatgpt.com/c/project-conversation",
    )
    context = object()
    result_queue: Queue[ChatGPTImageDownloadWorkResult] = Queue()

    with patch(
        "app.core.chatgpt_downloader._launch_chatgpt_browser_context",
        side_effect=[RuntimeError("Safari request failed: Fetch is aborted"), nullcontext(context)],
    ) as launch_context, patch(
        "app.core.chatgpt_downloader._load_chatgpt_session_request_headers",
        return_value={"authorization": "Bearer fresh-token"},
    ), patch(
        "app.core.chatgpt_downloader.download_chatgpt_image",
        return_value=True,
    ), patch("app.core.chatgpt_downloader.time.sleep") as sleep:
        _chatgpt_index_image_worker(
            [candidate],
            object(),
            ChatGPTImageCatalog.build(tmp_path / "demo-project"),
            tmp_path / "demo-project",
            lambda: False,
            result_queue,
        )

    assert launch_context.call_count == 2
    sleep.assert_called_once()
    assert result_queue.get_nowait().downloaded is True


def test_chatgpt_project_index_iterator_bounds_worker_cleanup_wait(tmp_path: Path) -> None:
    release_worker = Event()
    candidate = ChatGPTImageCandidate(
        source_url="https://chatgpt.com/backend-api/estuary/content?id=file_index_cleanup",
        file_id="file_index_cleanup",
        conversation_url="https://chatgpt.com/c/project-conversation",
    )

    def lingering_worker(candidates, _descriptor, _catalog, _target_dir, _should_stop, result_queue) -> None:
        result_queue.put(ChatGPTImageDownloadWorkResult(candidates[0].file_id, downloaded=True))
        release_worker.wait(timeout=1)

    try:
        with patch(
            "app.core.chatgpt_downloader._chatgpt_index_image_worker",
            side_effect=lingering_worker,
        ), patch(
            "app.core.chatgpt_downloader.CHATGPT_WORKER_JOIN_TIMEOUT_SECONDS",
            0.01,
        ):
            results = list(
                _iter_chatgpt_index_image_results(
                    [candidate],
                    object(),
                    ChatGPTImageCatalog.build(tmp_path / "demo-project"),
                    tmp_path / "demo-project",
                    lambda: False,
                    worker_count=1,
                )
            )
    finally:
        release_worker.set()

    assert [result.candidate_file_id for result in results] == [candidate.file_id]


def test_chatgpt_project_startup_timeout_is_explicit() -> None:
    class _ProjectPage:
        def wait_for_timeout(self, _milliseconds: int) -> None:
            pass

    with patch(
        "app.core.chatgpt_downloader._extract_project_links",
        return_value=[],
    ), patch(
        "app.core.chatgpt_downloader._has_load_more_conversations",
        return_value=False,
    ):
        with pytest.raises(RuntimeError, match="after 1 seconds"):
            _wait_for_project_conversation_links(
                _ProjectPage(),
                DEFAULT_CHATGPT_PROJECT_URL,
                should_stop=lambda: False,
                startup_timeout_seconds=1,
                scan_wait_seconds=0.1,
            )


def test_chatgpt_default_runtime_history_root_and_reset(tmp_path: Path) -> None:
    from app.core.chatgpt_downloader import chatgpt_history_counts

    target = tmp_path / "local_store" / "media" / "chatgpt" / "demo"
    history = tmp_path / "local_store" / "llm" / "chatgpt" / "history.parquet"
    store = ChatGPTHistoryStore(history)
    store.replace_conversation("https://chatgpt.com/c/demo", {
        "mapping": {"user": {"message": {
            "author": {"role": "user"}, "content": {"parts": ["Canonical history"]},
        }}},
    }, "2026-09-05T00:00:00Z")
    store.save()
    with patch("app.core.chatgpt_downloader.chatgpt_target_dir", return_value=target):
        with patch("app.core.chatgpt_downloader.ChatGPTImageCatalog.build") as build_media:
            result = sync_chatgpt_images(TaskState("test"), should_stop=lambda: True, content_mode="text")
            build_media.assert_not_called()
        assert result.cached_messages == 1
        assert chatgpt_history_counts(tmp_path / "local_store") == {"cached_sessions": 1, "cached_messages": 1}
        reset_chatgpt_state()
    assert not history.exists()
    assert not (tmp_path / "local_store" / "media" / "llm").exists()


@pytest.mark.parametrize("revision", ["", "new-revision"])
def test_chatgpt_history_refreshes_changed_or_unknown_revision(tmp_path: Path, revision: str) -> None:
    url = "https://chatgpt.com/c/freshness"
    payload = {"mapping": {"user": {"message": {
        "author": {"role": "user"}, "create_time": 1771059600,
        "content": {"parts": ["Original"]},
    }}}}
    store = ChatGPTHistoryStore(tmp_path / "history.parquet")
    store.replace_conversation(url, payload, "2026-09-05T00:00:00Z", provider_revision="old-revision")
    store.save()
    payload["mapping"]["assistant"] = {"message": {
        "author": {"role": "assistant"}, "create_time": 1771059900,
        "content": {"parts": ["New reply"]},
    }}
    store = ChatGPTHistoryStore(store.path)
    with patch("app.core.chatgpt_downloader._get_chatgpt_api_json_via_page", return_value=payload) as api_get:
        result = cache_chatgpt_conversation_history(
            store, [url], object(), {}, TaskState("test"), lambda: False,
            conversation_revisions_by_id={"freshness": revision},
        )
    api_get.assert_called_once()
    assert result == (1, 1, 0)
    assert ChatGPTHistoryStore(store.path).cached_messages == 2
    assert {row["provider_revision"] for row in read_parquet_rows(store.path)} == {revision}


def test_chatgpt_text_service_does_not_report_partial_sync_as_success() -> None:
    from app.core.chatgpt_downloader import ChatGPTSyncResult
    from app.core.chatgpt_service import ChatGPTDownloadService

    state = TaskState("test")
    service = ChatGPTDownloadService(state)
    service._content_mode = "text"
    with patch("app.core.chatgpt_service.sync_chatgpt_images", return_value=ChatGPTSyncResult(incomplete=True)):
        service._run()
    assert state.snapshot()["phase"] == "failed"
    assert "incomplete" in state.snapshot()["message"]


def test_chatgpt_discovery_records_provider_update_time() -> None:
    from app.core.chatgpt_downloader import _collect_all_chatgpt_conversation_urls_via_api

    revisions = {}
    with patch("app.core.chatgpt_downloader._get_chatgpt_api_json", side_effect=[
        {"items": [{"id": "known", "update_time": 12345}, {"id": "unknown"}]},
        {"items": []},
    ]):
        urls = _collect_all_chatgpt_conversation_urls_via_api(
            object(), "https://chatgpt.com/", {"Authorization": "fixture"},
            TaskState("test"), lambda: False, conversation_revisions_by_id=revisions,
        )
    assert len(urls) == 2
    assert revisions == {"known": "12345"}


@pytest.mark.parametrize("attempted_failures", [0, 1])
def test_stopped_text_sync_counts_only_attempted_failures(tmp_path: Path, attempted_failures: int) -> None:
    state = TaskState("test")
    store = ChatGPTHistoryStore(tmp_path / "history.parquet")
    stops = iter([False] * attempted_failures + [True])
    with patch(
        "app.core.chatgpt_downloader._get_chatgpt_api_json_via_page",
        side_effect=RuntimeError("Unavailable session"),
    ) as fetch:
        result = cache_chatgpt_conversation_history(
            store, ["https://chatgpt.com/c/first", "https://chatgpt.com/c/pending"],
            object(), {}, state, lambda: next(stops),
        )
    assert result == (0, 0, 0)
    assert fetch.call_count == attempted_failures
    assert state.snapshot()["failed_tweets"] == attempted_failures
