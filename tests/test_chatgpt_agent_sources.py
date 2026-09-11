"""Focused tests for the Agent's ChatGPT Web source catalog.

Code version: v1.2.9-codex.1
"""

from __future__ import annotations

from contextlib import nullcontext
import json
from unittest.mock import patch

from app.core.chatgpt_agent_sources import (
    CHATGPT_PROJECT_API_ENDPOINTS,
    _collect_projects,
    _collect_projects_from_api,
    _collect_project_sessions,
    _collect_root_sessions,
    _conversation_history_items,
    _conversation_item,
    _fetch_conversation_history,
    humanize_agent_history_prompts,
    normalize_chatgpt_conversation_url,
    normalize_chatgpt_project_url,
    probe_and_collect_chatgpt_sources,
)
from app.core.config import CrawlConfig


class _Response:
    def __init__(self, payload: dict[str, object], status: int = 200) -> None:
        self.ok = status < 400
        self.status = status
        self._payload = payload

    def text(self) -> str:
        import json

        return json.dumps(self._payload)


class _Request:
    def __init__(self, responses: dict[str, dict[str, object]]) -> None:
        self.responses = responses

    def get(self, url: str, **_kwargs) -> _Response:
        for marker, payload in self.responses.items():
            if marker in url:
                return _Response(payload)
        return _Response({}, status=404)


class _Context:
    def __init__(self, responses: dict[str, dict[str, object]]) -> None:
        self.request = _Request(responses)


class _Page:
    def evaluate(self, _script: str):
        return []


class _InPageFetchPage:
    """Simulate a browser page whose in-page requests bypass a dead proxy."""

    def __init__(self, payloads: dict[str, dict[str, object]]) -> None:
        self._payloads = payloads
        self.fetched_urls: list[str] = []

    def evaluate(self, _script: str, argument: dict[str, object]):
        url = str(argument.get("url") or "")
        self.fetched_urls.append(url)
        for marker, payload in self._payloads.items():
            if marker in url:
                return {"status": 200, "payload": payload}
        return {"status": 404, "payload": None}


class _ProxyDeadContext:
    """Fail if production falls back to the Node request context."""

    def __init__(self) -> None:
        class _Request:
            def get(self, _url: str, **_kwargs):
                raise TimeoutError("Proxy connection timed out.")

        self.request = _Request()


class _BootstrapPage:
    def __init__(self) -> None:
        self.waits: list[int] = []

    def goto(self, *_args, **_kwargs) -> None:
        return None

    def evaluate(self, _script: str) -> dict[str, object]:
        return {
            "ok": True,
            "status": 200,
            "bodyText": json.dumps({"accessToken": "fixture-token"}),
            "error": "",
        }

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.waits.append(milliseconds)


class _BootstrapContext:
    def __init__(self, page: _BootstrapPage) -> None:
        self.pages = [page]


def test_chatgpt_source_urls_are_canonical_and_scoped() -> None:
    assert normalize_chatgpt_project_url(
        "https://www.chatgpt.com/g/g-p-demo/project/?utm_source=agent"
    ) == "https://chatgpt.com/g/g-p-demo/project"
    assert normalize_chatgpt_conversation_url(
        "https://www.chatgpt.com/g/g-p-demo/c/session-1?oai-dm=1"
    ) == "https://chatgpt.com/g/g-p-demo/c/session-1"
    assert normalize_chatgpt_project_url("https://chatgpt.com/c/not-a-project") == ""
    assert normalize_chatgpt_conversation_url("https://example.com/c/session-1") == ""
    assert normalize_chatgpt_project_url(
        "https://chatgpt.com:444/g/g-p-demo/project"
    ) == ""
    assert normalize_chatgpt_conversation_url(
        "https://chatgpt.com:444/c/session-1"
    ) == ""
    assert normalize_chatgpt_project_url(
        "https://chatgpt.com:443/g/g-p-demo/project"
    ) == "https://chatgpt.com/g/g-p-demo/project"


def test_chatgpt_status_and_sources_share_one_chromium_context() -> None:
    page = _BootstrapPage()
    context = _BootstrapContext(page)
    source_payload = {
        "browser_label": "Edge",
        "recent_sessions": [{"id": "recent-1"}],
        "projects": [],
        "limit": 20,
    }
    with patch(
        "app.core.chatgpt_agent_sources.sync_playwright_or_error",
        return_value=nullcontext(object()),
    ) as playwright_factory, patch(
        "app.core.chatgpt_agent_sources.launch_chromium_context",
        return_value=nullcontext(context),
    ) as launch_context, patch(
        "app.core.chatgpt_agent_sources._collect_sources",
        return_value=source_payload,
    ) as collect_sources, patch(
        "app.core.chatgpt_agent_sources._discover_chatgpt_agent_efforts",
        return_value={
            "model_verified": True,
            "actual_model": "GPT-5.6 Sol",
            "thinking_effort": "Landing proof",
            "available_efforts": ["Launch brief", "Landing proof"],
            "effort_catalog_complete": True,
        },
    ) as discover_efforts:
        status, sources = probe_and_collect_chatgpt_sources("edge", CrawlConfig())

    assert status["can_download"] is True
    assert status["available_efforts"] == ["Launch brief", "Landing proof"]
    assert status["thinking_effort"] == "Landing proof"
    assert status["effort_catalog_complete"] is True
    assert sources == {**source_payload, "platform": "chatgpt"}
    playwright_factory.assert_called_once()
    launch_context.assert_called_once()
    assert launch_context.call_args.kwargs["headless"] is False
    assert launch_context.call_args.kwargs["background_window"] is True
    collect_sources.assert_called_once_with(context, page, "Edge")
    discover_efforts.assert_called_once_with(page)


def test_root_sessions_filter_project_sessions_and_limit_to_twenty() -> None:
    items = [
        {"id": "root-1", "title": "Root session", "update_time": "2026-08-13T10:00:00Z"},
        {"id": "project-1", "title": "Project session", "gizmo_id": "g-p-demo"},
    ]
    context = _Context({"/backend-api/conversations": {"items": items}})

    sessions = _collect_root_sessions(context, {"authorization": "Bearer test"})

    assert sessions == [
        {
            "id": "root-1",
            "title": "Root session",
            "url": "https://chatgpt.com/c/root-1",
            "updated_at": "2026-08-13T10:00:00Z",
        }
    ]


def test_root_sessions_are_sorted_by_provider_update_time() -> None:
    context = _Context(
        {
            "/backend-api/conversations": {
                "items": [
                    {"id": "older", "title": "Old title", "update_time": "2026-08-13T10:00:00Z"},
                    {"id": "newer", "title": "Renamed title", "update_time": "2026-09-02T01:00:00Z"},
                ]
            }
        }
    )

    sessions = _collect_root_sessions(context, {"authorization": "Bearer test"})

    assert [session["id"] for session in sessions] == ["newer", "older"]
    assert sessions[0]["title"] == "Renamed title"


def test_project_api_merges_all_catalog_endpoints_and_keeps_newest_projects() -> None:
    responses = {
        "/backend-api/gizmos/snorlax/sidebar?owned_only=true&conversations_per_gizmo=5&limit=20": {
            "items": [
                {
                    "id": "g-p-old",
                    "name": "Old project title",
                    "updated_at": "2026-08-13T10:00:00Z",
                }
            ]
        },
        "/backend-api/projects?offset=0&limit=100&order=updated": {
            "items": [
                {
                    "id": "g-p-new",
                    "name": "Newest project",
                    "updated_at": "2026-09-02T01:00:00Z",
                }
            ]
        },
    }

    class _OrderedRequest:
        def __init__(self) -> None:
            self.urls: list[str] = []

        def get(self, url: str, **_kwargs: object) -> _Response:
            endpoint = url.split("chatgpt.com", 1)[-1]
            self.urls.append(endpoint)
            return _Response(responses.get(endpoint, {}))

    class _OrderedContext:
        def __init__(self) -> None:
            self.request = _OrderedRequest()

    context = _OrderedContext()
    projects = _collect_projects_from_api(context, {"authorization": "Bearer test"})

    assert [project["id"] for project in projects] == ["g-p-new", "g-p-old"]
    assert len(context.request.urls) == len(CHATGPT_PROJECT_API_ENDPOINTS) + 2


def test_project_sessions_are_sorted_and_renamed_rows_are_reconciled() -> None:
    project_url = "https://chatgpt.com/g/g-p-demo/project"
    context = _Context(
        {
            "/api/auth/session": {"accessToken": "fixture-token"},
            "/backend-api/gizmos/g-p-demo/conversations": {
                "items": [
                    {"id": "older", "title": "Old", "update_time": "2026-08-13T10:00:00Z"},
                    {"id": "newer", "title": "New renamed title", "update_time": "2026-09-02T01:00:00Z"},
                ]
            }
        }
    )

    sessions = _collect_project_sessions(context, project_url)

    assert [session["id"] for session in sessions] == ["newer", "older"]
    assert sessions[0]["title"] == "New renamed title"


def test_project_api_parser_supports_nested_gizmo_items() -> None:
    context = _Context(
        {
            "/backend-api/gizmos": {
                "items": [
                    {
                        "gizmo": {
                            "id": "g-p-demo-project",
                            "name": "Demo project",
                            "updated_at": "2026-08-13T09:00:00Z",
                        }
                    }
                ]
            }
        }
    )

    projects = _collect_projects_from_api(context, {"authorization": "Bearer test"})

    assert projects == [
        {
            "id": "g-p-demo-project",
            "title": "Demo project",
            "url": "https://chatgpt.com/g/g-p-demo-project/project",
            "updated_at": "2026-08-13T09:00:00Z",
        }
    ]


def test_project_api_enriches_live_icon_metadata_from_project_detail() -> None:
    context = _Context(
        {
            "/backend-api/gizmos/g-p-demo-project": {
                "gizmo": {
                    "id": "g-p-demo-project",
                    "display": {"name": "Demo project", "emoji": "currency-dollar", "theme": "#53B559"},
                }
            },
            "/backend-api/gizmos": {
                "items": [
                    {
                        "id": "g-p-demo-project",
                        "name": "Demo project",
                        "updated_at": "2026-08-13T09:00:00Z",
                    }
                ]
            },
        }
    )

    projects = _collect_projects_from_api(context, {"authorization": "Bearer test"})

    assert projects[0]["icon"] == "currency-dollar"
    assert projects[0]["icon_color"] == "#53B559"


def test_project_api_parser_supports_sidebar_resource_records() -> None:
    context = _Context(
        {
            "/backend-api/gizmos/snorlax/sidebar": {
                "items": [
                    {
                        "gizmo": {
                            "gizmo": {
                            "id": "g-p-11111111111111111111111111111111",
                            "short_url": "g-p-11111111111111111111111111111111-sidebar-project",
                                "display": {"name": "Sidebar project"},
                                "last_interacted_at": "2026-08-13T11:00:00Z",
                            },
                            "conversations": {"items": []},
                        }
                    }
                ]
            }
        }
    )

    projects = _collect_projects(context, None, {"authorization": "Bearer test"})

    assert projects == [
        {
            "id": "g-p-11111111111111111111111111111111",
            "title": "Sidebar project",
            "url": "https://chatgpt.com/g/g-p-11111111111111111111111111111111-sidebar-project/project",
            "updated_at": "2026-08-13T11:00:00Z",
        }
    ]


def test_conversation_item_can_build_project_session_url() -> None:
    assert _conversation_item(
        {"id": "session-1", "title": "Project chat"},
        "https://chatgpt.com/g/g-p-demo-project/c/",
    )["url"] == "https://chatgpt.com/g/g-p-demo-project/c/session-1"


def test_conversation_history_pairs_ordered_user_and_assistant_messages() -> None:
    history = _conversation_history_items(
        [
            {
                "message_index": 2,
                "role": "assistant",
                "content_text": "The first answer.",
                "last_seen_at": "2026-08-14T01:02:00Z",
            },
            {
                "message_index": 1,
                "role": "user",
                "content_text": "The first question.",
                "last_seen_at": "2026-08-14T01:01:00Z",
            },
            {
                "message_index": 4,
                "role": "assistant",
                "content_text": "The second answer.",
                "last_seen_at": "2026-08-14T01:04:00Z",
            },
            {
                "message_index": 3,
                "role": "user",
                "content_text": "The second question.",
                "last_seen_at": "2026-08-14T01:03:00Z",
            },
        ]
    )

    assert history == [
        {
            "prompt": "The first question.",
            "response": "The first answer.",
            "started_at": "2026-08-14T01:01:00Z",
            "finished_at": "2026-08-14T01:02:00Z",
        },
        {
            "prompt": "The second question.",
            "response": "The second answer.",
            "started_at": "2026-08-14T01:03:00Z",
            "finished_at": "2026-08-14T01:04:00Z",
        },
    ]


def test_agent_history_prompts_recover_the_user_request_and_hide_transport_text() -> None:
    marker = "agent-turn-0123456789abcdef0123456789abcdef"
    initial_prompt = (
        "Controller transfer ID: agent-transfer-0123456789abcdef0123456789abcdef\n\n"
        "You are the reasoning component of a local Computer Use coding agent.\n\n"
        "User request: Explain why the history question is unreadable.\n\n"
        "Begin with the smallest useful read, search, or list JSON action."
    )
    observation_prompt = (
        f"Controller turn receipt: {marker}\n\n"
        "Controller observation for turn 303:\n"
        '{"ok":false,"action":"final","error":"limitations must be an array"}\n'
        "Return exactly one strict JSON controller action."
    )

    history = humanize_agent_history_prompts(
        [
            {"prompt": initial_prompt, "response": "First action"},
            {"prompt": observation_prompt, "response": "Final action"},
        ],
        session_title="Provider-generated title",
    )

    assert [item["prompt"] for item in history] == [
        "Explain why the history question is unreadable.",
        "Explain why the history question is unreadable.",
    ]
    assert [item["response"] for item in history] == ["First action", "Final action"]


def test_agent_history_prompt_uses_readable_title_when_cached_slice_omits_initial_turn() -> None:
    history = humanize_agent_history_prompts(
        [
            {
                "prompt": "Controller observation for turn 303:\n{}\nProtocol text",
                "response": "Final action",
            }
        ],
        session_title="Read Project Instructions",
    )

    assert history[0]["prompt"] == "Read Project Instructions"


def test_fetch_conversation_history_reads_authenticated_mapping_without_persistence() -> None:
    context = _Context(
        {
            "/api/auth/session": {"accessToken": "fixture-token"},
            "/backend-api/conversation/fixture-session": {
                "title": "Fixture session",
                "current_node": "assistant-node",
                "mapping": {
                    "root": {"message": None, "parent": None},
                    "user-node": {
                        "parent": "root",
                        "message": {
                            "author": {"role": "user"},
                            "content": {"parts": ["Check the fonts"]},
                            "create_time": "2026-08-14T01:01:00Z",
                        }
                    },
                    "progress-node": {
                        "parent": "user-node",
                        "message": {
                            "author": {"role": "assistant"},
                            "channel": "commentary",
                            "content": {"parts": ["Checking the font settings"]},
                        },
                    },
                    "tool-call-node": {
                        "parent": "progress-node",
                        "message": {
                            "author": {"role": "assistant"},
                            "recipient": "functions.inspect",
                            "content": {"parts": ['{"path": "styles.css"}']},
                        },
                    },
                    "assistant-node": {
                        "parent": "tool-call-node",
                        "message": {
                            "author": {"role": "assistant"},
                            "channel": "final",
                            "content": {"parts": ["The font stack is configured"]},
                            "create_time": "2026-08-14T01:02:00Z",
                        }
                    },
                    "alternate-assistant-node": {
                        "parent": "user-node",
                        "message": {
                            "author": {"role": "assistant"},
                            "content": {"parts": ["An alternate answer"]},
                            "create_time": "2026-08-14T01:03:00Z",
                        }
                    },
                },
            },
        }
    )

    payload = _fetch_conversation_history(context, "https://chatgpt.com/c/fixture-session")

    assert payload["title"] == "Fixture session"
    assert payload["history"] == [
        {
            "prompt": "Check the fonts",
            "response": "The font stack is configured",
            "started_at": "2026-08-14T01:01:00Z",
            "finished_at": "2026-08-14T01:02:00Z",
        }
    ]


def test_bootstrap_network_failure_does_not_claim_signed_out() -> None:
    with patch(
        "app.core.chatgpt_agent_sources.sync_playwright_or_error",
        side_effect=RuntimeError("net::ERR_CONNECTION_CLOSED"),
    ):
        status, sources = probe_and_collect_chatgpt_sources("edge", CrawlConfig())
    assert status["logged_in"] is None
    assert status["can_download"] is False
    assert status["probe_error"] is True
    assert "ERR_CONNECTION_CLOSED" in status["message"]
    assert sources is None


def test_fetch_conversation_history_uses_in_page_fetch_when_page_supplied() -> None:
    page = _InPageFetchPage(
        {
            "/api/auth/session": {"accessToken": "fixture-token"},
            "/backend-api/conversation/fixture-session": {
                "title": "Fixture session",
                "current_node": "assistant-node",
                "mapping": {
                    "root": {"message": None, "parent": None},
                    "user-node": {
                        "parent": "root",
                        "message": {
                            "author": {"role": "user"},
                            "content": {"parts": ["Check the fonts"]},
                            "create_time": "2026-08-14T01:01:00Z",
                        },
                    },
                    "assistant-node": {
                        "parent": "user-node",
                        "message": {
                            "author": {"role": "assistant"},
                            "channel": "final",
                            "content": {"parts": ["The font stack is configured"]},
                            "create_time": "2026-08-14T01:02:00Z",
                        },
                    },
                },
            },
        }
    )

    payload = _fetch_conversation_history(
        _ProxyDeadContext(),
        "https://chatgpt.com/c/fixture-session",
        page,
    )

    assert payload["title"] == "Fixture session"
    assert payload["history"] == [
        {
            "prompt": "Check the fonts",
            "response": "The font stack is configured",
            "started_at": "2026-08-14T01:01:00Z",
            "finished_at": "2026-08-14T01:02:00Z",
        }
    ]
    assert page.fetched_urls == [
        "https://chatgpt.com/api/auth/session",
        "https://chatgpt.com/backend-api/conversation/fixture-session",
    ]


def test_collect_root_sessions_uses_in_page_fetch_when_page_supplied() -> None:
    page = _InPageFetchPage(
        {
            "/backend-api/conversations": {
                "items": [
                    {
                        "id": "root-1",
                        "title": "Root session",
                        "update_time": "2026-08-13T10:00:00Z",
                    }
                ]
            },
        }
    )

    sessions = _collect_root_sessions(
        _ProxyDeadContext(),
        {"authorization": "Bearer test"},
        page=page,
    )

    assert sessions == [
        {
            "id": "root-1",
            "title": "Root session",
            "url": "https://chatgpt.com/c/root-1",
            "updated_at": "2026-08-13T10:00:00Z",
        }
    ]
    assert len(page.fetched_urls) == 1
    assert page.fetched_urls[0].startswith(
        "https://chatgpt.com/backend-api/conversations?"
    )
