"""Focused tests for the provider-neutral Agent session source adapter.

Code version: v1.7.6-codex.1
"""

from __future__ import annotations

from unittest.mock import patch

from app.core.agent_session_sources import (
    _claude_page_status,
    _grok_page_status,
    _read_gemini_project_links,
    _read_grok_project_links,
    _read_grok_project_session_links,
    _read_project_session_links,
    chatgpt_project_id,
    claude_project_session_id,
    fetch_grok_conversation_history,
    list_agent_project_sessions,
    list_agent_sources,
    normalize_agent_conversation_url,
    normalize_agent_source_catalog_payload,
    normalize_agent_project_url,
    probe_and_collect_grok_sources,
)
from app.core.browser_sessions import visible_claude_composer_selector
from app.core.config import CrawlConfig
from app.core.gemini_downloader import GeminiConversationLink
from app.core.grok_history import GrokConversation


def test_agent_conversation_url_normalization_is_provider_specific() -> None:
    assert normalize_agent_conversation_url(
        "gemini",
        "https://gemini.google.com/app/session-1/?hl=en",
    ) == "https://gemini.google.com/app/session-1"
    assert normalize_agent_conversation_url(
        "grok",
        "https://www.grok.com/c/session-2/",
    ) == "https://grok.com/c/session-2"
    assert normalize_agent_conversation_url(
        "chatgpt",
        "https://www.chatgpt.com/c/session-3?messageId=ignored",
    ) == "https://chatgpt.com/c/session-3"
    assert normalize_agent_conversation_url("grok", "https://example.com/c/session") == ""
    assert normalize_agent_conversation_url(
        "grok",
        "https://grok.com/project/project-1?chat=session-1",
    ) == "https://grok.com/project/project-1?chat=session-1"
    assert normalize_agent_conversation_url(
        "claude",
        "https://www.claude.ai/chat/session-4/?utm_source=agent",
    ) == "https://claude.ai/chat/session-4"
    assert normalize_agent_conversation_url(
        "claude",
        "https://claude.ai/project/project-1/chat/session-4",
    ) == "https://claude.ai/project/project-1/chat/session-4"
    assert normalize_agent_conversation_url(
        "claude",
        "https://claude.ai/project/project-1?chat=session-4",
    ) == "https://claude.ai/project/project-1/chat/session-4"


def test_agent_project_url_normalization_hides_provider_specific_routes() -> None:
    assert normalize_agent_project_url(
        "chatgpt",
        "https://chatgpt.com/g/g-p-demo/project?tab=chat",
    ) == "https://chatgpt.com/g/g-p-demo/project"
    assert normalize_agent_project_url(
        "gemini",
        "https://gemini.google.com/notebook/notebook-1/?hl=en",
    ) == "https://gemini.google.com/app/notebook-1"
    assert normalize_agent_project_url(
        "gemini",
        "https://gemini.google.com/notebooks/notebook-1",
    ) == "https://gemini.google.com/app/notebook-1"
    assert normalize_agent_project_url(
        "gemini",
        "https://gemini.google.com/notebook/create",
    ) == ""
    assert normalize_agent_project_url(
        "gemini",
        "https://gemini.google.com/notebooks/NEW",
    ) == ""
    assert normalize_agent_project_url(
        "grok",
        "https://www.grok.com/project/project-1?tab=conversations",
    ) == "https://grok.com/project/project-1?tab=conversations"
    assert normalize_agent_project_url(
        "claude",
        "https://www.claude.ai/project/project-1/?tab=chats",
    ) == "https://claude.ai/project/project-1"
    assert normalize_agent_project_url("grok", "https://example.com/project/project-1") == ""


def test_cached_gemini_project_rows_are_revalidated_before_replay() -> None:
    payload = normalize_agent_source_catalog_payload(
        "gemini",
        {
            "platform": "gemini",
            "browser_label": "Edge",
            "recent_sessions": [{"id": "session-1"}],
            "projects": [
                {
                    "id": "create",
                    "title": "New notebook",
                    "url": "https://gemini.google.com/app/create",
                },
                {
                    "id": "notebook-1",
                    "title": "Research notebook",
                    "url": "https://gemini.google.com/app/notebook-1",
                },
            ],
            "cache": {"status": "hit", "layer": "parquet"},
        },
    )

    assert payload["recent_sessions"] == []
    assert payload["projects"] == [
        {
            "id": "notebook-1",
            "title": "Research notebook",
            "url": "https://gemini.google.com/app/notebook-1",
            "updated_at": "",
        }
    ]
    assert payload["cache"] == {"status": "hit", "layer": "parquet"}


def test_cached_non_chatgpt_project_ids_are_derived_from_validated_urls() -> None:
    cases = (
        (
            "gemini",
            "https://gemini.google.com/app/notebook-1",
            "notebook-1",
        ),
        (
            "grok",
            "https://grok.com/project/project-1?tab=conversations",
            "project-1",
        ),
        (
            "claude",
            "https://claude.ai/project/project-1",
            "project-1",
        ),
    )

    for platform, project_url, expected_id in cases:
        payload = normalize_agent_source_catalog_payload(
            platform,
            {
                "projects": [
                    {
                        "id": "foreign-cache-id",
                        "title": "Validated project",
                        "url": project_url,
                    }
                ]
            },
        )

        assert payload["projects"][0]["id"] == expected_id


def test_cached_source_catalog_sorts_current_sessions_and_projects() -> None:
    payload = normalize_agent_source_catalog_payload(
        "chatgpt",
        {
            "recent_sessions": [
                {
                    "id": "old-session",
                    "title": "Old title",
                    "url": "https://chatgpt.com/c/old-session",
                    "updated_at": "2026-08-13T10:00:00Z",
                },
                {
                    "id": "new-session",
                    "title": "Renamed session",
                    "url": "https://chatgpt.com/c/new-session",
                    "updated_at": "2026-09-02T01:00:00Z",
                },
            ],
            "projects": [
                {
                    "id": "old-project",
                    "title": "Old project",
                    "url": "https://chatgpt.com/g/g-p-old/project",
                    "updated_at": "2026-08-13T10:00:00Z",
                },
                {
                    "id": "new-project",
                    "title": "Renamed project",
                    "url": "https://chatgpt.com/g/g-p-new/project",
                    "updated_at": "2026-09-02T01:00:00Z",
                },
            ],
        },
    )

    assert [row["id"] for row in payload["recent_sessions"]] == ["new-session", "old-session"]
    assert payload["recent_sessions"][0]["title"] == "Renamed session"
    assert [row["url"] for row in payload["projects"]] == [
        "https://chatgpt.com/g/g-p-new/project",
        "https://chatgpt.com/g/g-p-old/project",
    ]
    assert [row["id"] for row in payload["projects"]] == ["g-p-new", "g-p-old"]
    assert payload["projects"][0]["title"] == "Renamed project"


def test_cached_chatgpt_project_icon_metadata_survives_revalidation() -> None:
    project_id = "g-p-6a978edb95308191a53d2bb113154c10"
    payload = normalize_agent_source_catalog_payload(
        "chatgpt",
        {
            "recent_sessions": [],
            "projects": [
                {
                    "id": "project",
                    "title": "worthward",
                    "url": f"https://chatgpt.com/g/{project_id}-worthward/project",
                    "icon": "currency-dollar",
                    "icon_color": "#53B559",
                }
            ],
        },
    )

    assert payload["projects"] == [
        {
            "id": project_id,
            "title": "worthward",
            "url": f"https://chatgpt.com/g/{project_id}-worthward/project",
            "updated_at": "",
            "icon": "currency-dollar",
            "icon_color": "#53B559",
        }
    ]


def test_cached_chatgpt_project_aliases_use_one_stable_identity() -> None:
    project_id = "g-p-6a978edb95308191a53d2bb113154c10"
    old_url = f"https://chatgpt.com/g/{project_id}-antigravity/project"
    current_url = f"https://chatgpt.com/g/{project_id}-worthward/project"

    payload = normalize_agent_source_catalog_payload(
        "chatgpt",
        {
            "projects": [
                {
                    "id": "project",
                    "title": "Old project name",
                    "url": old_url,
                    "updated_at": "2026-09-01T00:00:00Z",
                },
                {
                    "id": "project",
                    "title": "worthward",
                    "url": current_url,
                    "updated_at": "2026-09-08T00:00:00Z",
                },
            ]
        },
    )

    assert chatgpt_project_id(old_url) == project_id
    assert chatgpt_project_id(current_url) == project_id
    assert payload["projects"] == [
        {
            "id": project_id,
            "title": "worthward",
            "url": current_url,
            "updated_at": "2026-09-08T00:00:00Z",
        }
    ]


def test_claude_sources_use_the_shared_chromium_source_collection() -> None:
    with patch(
        "app.core.agent_session_sources._run_chromium_source_collection",
        return_value={
            "recent_sessions": [
                {
                    "id": "claude-1",
                    "title": "Claude session",
                    "url": "https://claude.ai/chat/claude-1",
                }
            ],
            "projects": [
                {
                    "id": "project-1",
                    "title": "Claude project",
                    "url": "https://claude.ai/project/project-1",
                }
            ],
        },
    ) as collector:
        payload = list_agent_sources("claude", "edge", CrawlConfig())

    assert payload["platform"] == "claude"
    assert payload["recent_sessions"][0]["url"] == "https://claude.ai/chat/claude-1"
    assert payload["projects"][0]["url"] == "https://claude.ai/project/project-1"
    assert collector.call_args.args[:3] == ("edge", CrawlConfig(), "https://claude.ai/new")


def test_claude_status_exposes_account_restriction_without_attempting_login_bypass() -> None:
    class _Body:
        def inner_text(self, **_kwargs: object) -> str:
            return "Your account has been disabled for violating the usage policy."

    class _Page:
        def wait_for_timeout(self, _milliseconds: int) -> None:
            return None

        def locator(self, selector: str) -> _Body:
            assert selector == "body"
            return _Body()

    status = _claude_page_status(_Page(), "Edge")

    assert status["can_download"] is False
    assert status["account_name"] == "Claude account restricted"
    assert "restricted or unavailable" in status["message"]


def test_claude_status_requires_one_shared_semantic_composer() -> None:
    class _Composer:
        @property
        def first(self) -> "_Composer":
            return self

        def count(self) -> int:
            return 1

        def wait_for(self, **_kwargs: object) -> None:
            return None

    class _Body:
        def inner_text(self, **_kwargs: object) -> str:
            return "Claude"

    class _Page:
        def wait_for_timeout(self, _milliseconds: int) -> None:
            return None

        def locator(self, selector: str) -> _Body | _Composer:
            if selector == "body":
                return _Body()
            assert selector == visible_claude_composer_selector()
            return _Composer()

    status = _claude_page_status(_Page(), "Edge")

    assert status["logged_in"] is True
    assert status["can_download"] is True


def test_claude_status_rejects_ambiguous_semantic_composers() -> None:
    class _Composer:
        @property
        def first(self) -> "_Composer":
            raise AssertionError("An ambiguous composer set must not be awaited.")

        def count(self) -> int:
            return 2

    class _Body:
        def inner_text(self, **_kwargs: object) -> str:
            return "Claude"

    class _Page:
        def wait_for_timeout(self, _milliseconds: int) -> None:
            return None

        def locator(self, selector: str) -> _Body | _Composer:
            if selector == "body":
                return _Body()
            assert selector == visible_claude_composer_selector()
            return _Composer()

    status = _claude_page_status(_Page(), "Edge")

    assert status["logged_in"] is False
    assert status["can_download"] is False
    assert "could not verify" in status["message"]


def test_grok_agent_status_uses_the_message_composer_instead_of_files() -> None:
    class _Composer:
        @property
        def first(self) -> "_Composer":
            return self

        def wait_for(self, **_kwargs: object) -> None:
            return None

    class _Body:
        def inner_text(self, **_kwargs: object) -> str:
            return "What do you want to know?"

    class _Page:
        def wait_for_timeout(self, _milliseconds: int) -> None:
            return None

        def title(self) -> str:
            return "Grok"

        def content(self) -> str:
            return "<html><body><textarea></textarea></body></html>"

        def evaluate(
            self,
            expression: str,
            argument: dict[str, object] | None = None,
        ) -> object:
            if argument is None:
                assert "authAction" in expression
                return False
            assert "/rest/app-chat/conversations?" in str(argument["url"])
            return {"status": 200, "body": {"conversations": []}}

        def locator(self, selector: str) -> object:
            if selector == "body":
                return _Body()
            assert selector == "textarea"
            return _Composer()

    status = _grok_page_status(_Page(), "Edge")

    assert status["logged_in"] is True
    assert status["can_download"] is True
    assert status["message"] == "Edge verified an authenticated Grok Web session."


def test_grok_agent_status_rejects_a_signed_out_page_even_with_a_composer() -> None:
    class _Body:
        def inner_text(self, **_kwargs: object) -> str:
            return "Ask anything\nSign in"

    class _Page:
        def wait_for_timeout(self, _milliseconds: int) -> None:
            return None

        def title(self) -> str:
            return "Grok"

        def content(self) -> str:
            return "<html><body><textarea></textarea><button>Sign in</button></body></html>"

        def locator(self, selector: str) -> object:
            if selector == "body":
                return _Body()
            raise AssertionError("A signed-out page must fail before the composer is trusted.")

    status = _grok_page_status(_Page(), "Edge")

    assert status["logged_in"] is False
    assert status["can_download"] is False
    assert status["message"] == "Edge is not signed in to Grok."


def test_grok_agent_status_uses_visible_auth_controls_as_signed_out_evidence() -> None:
    class _Body:
        def inner_text(self, **_kwargs: object) -> str:
            return "Ask anything"

    class _Page:
        def wait_for_timeout(self, _milliseconds: int) -> None:
            return None

        def title(self) -> str:
            return "Grok"

        def content(self) -> str:
            return "<html><body><textarea></textarea><button>Sign in to Grok</button></body></html>"

        def evaluate(self, expression: str) -> bool:
            assert "authAction" in expression
            assert "!composer" not in expression
            assert "authAction && !account" not in expression
            assert "sign in|log in|sign up|create account" in expression
            return True

        def locator(self, selector: str) -> object:
            if selector == "body":
                return _Body()
            raise AssertionError("Visible authentication controls must fail before composer access.")

    status = _grok_page_status(_Page(), "Edge")

    assert status["logged_in"] is False
    assert status["can_download"] is False


def test_grok_agent_status_requires_positive_authenticated_api_evidence() -> None:
    class _Composer:
        @property
        def first(self) -> "_Composer":
            return self

        def wait_for(self, **_kwargs: object) -> None:
            return None

    class _Body:
        def inner_text(self, **_kwargs: object) -> str:
            return "Ask anything"

    class _Page:
        def wait_for_timeout(self, _milliseconds: int) -> None:
            return None

        def title(self) -> str:
            return "Grok"

        def content(self) -> str:
            return "<html><body><textarea></textarea></body></html>"

        def evaluate(
            self,
            expression: str,
            argument: dict[str, object] | None = None,
        ) -> object:
            if argument is None:
                return False
            return {"status": 401, "body": {"error": "Unauthorized"}}

        def locator(self, selector: str) -> object:
            if selector == "body":
                return _Body()
            assert selector == "textarea"
            return _Composer()

    status = _grok_page_status(_Page(), "Edge")

    assert status["logged_in"] is False
    assert status["can_download"] is False
    assert status["message"] == "Edge could not verify an authenticated Grok account."


def test_grok_agent_status_rejects_a_schema_invalid_success_payload() -> None:
    class _Composer:
        @property
        def first(self) -> "_Composer":
            return self

        def wait_for(self, **_kwargs: object) -> None:
            return None

    class _Body:
        def inner_text(self, **_kwargs: object) -> str:
            return "Ask anything"

    class _Page:
        def wait_for_timeout(self, _milliseconds: int) -> None:
            return None

        def title(self) -> str:
            return "Grok"

        def content(self) -> str:
            return "<html><body><textarea></textarea></body></html>"

        def evaluate(
            self,
            expression: str,
            argument: dict[str, object] | None = None,
        ) -> object:
            if argument is None:
                return False
            return {"status": 200, "body": {"error": "not authenticated"}}

        def locator(self, selector: str) -> object:
            if selector == "body":
                return _Body()
            return _Composer()

    status = _grok_page_status(_Page(), "Edge")

    assert status["can_download"] is False
    assert "could not verify an authenticated Grok account" in status["message"]


def test_grok_agent_status_rejects_cloudflare_before_composer_access() -> None:
    class _Body:
        def inner_text(self, **_kwargs: object) -> str:
            return "Performing security verification\nRay ID: abc123"

    class _Page:
        def wait_for_timeout(self, _milliseconds: int) -> None:
            return None

        def title(self) -> str:
            return "Just a moment..."

        def content(self) -> str:
            return "<html><body>Cloudflare security verification</body></html>"

        def locator(self, selector: str) -> object:
            if selector == "body":
                return _Body()
            raise AssertionError("A security challenge must fail before composer access.")

    status = _grok_page_status(_Page(), "Edge")

    assert status["logged_in"] is False
    assert status["can_download"] is False
    assert status["account_name"] == "Security verification required"


def test_grok_agent_bootstrap_collects_readiness_and_sources_in_one_context() -> None:
    source_snapshot = {
        "recent_sessions": [
            {
                "id": "session-1",
                "title": "Grok session",
                "url": "https://grok.com/c/session-1",
            }
        ],
        "projects": [],
    }

    def run_collection(
        _browser: str,
        _config: CrawlConfig,
        home_url: str,
        collector: object,
        **_kwargs: object,
    ) -> object:
        assert home_url == "https://grok.com/"
        with patch(
            "app.core.agent_session_sources._grok_page_status",
            return_value={
                "platform": "grok",
                "browser_label": "Edge",
                "logged_in": True,
                "can_download": True,
                "account_name": "Grok account",
                "message": "Ready",
            },
        ), patch(
            "app.core.agent_session_sources._collect_grok_sources",
            return_value=source_snapshot,
        ):
            return collector(object())

    with patch(
        "app.core.agent_session_sources._run_chromium_source_collection",
        side_effect=run_collection,
    ) as collection:
        status, sources = probe_and_collect_grok_sources("edge", CrawlConfig())

    assert status["can_download"] is True
    assert sources is not None
    assert sources["recent_sessions"][0]["url"] == "https://grok.com/c/session-1"
    collection.assert_called_once()


def test_gemini_sources_reuse_the_existing_history_link_collector() -> None:
    links = [
        GeminiConversationLink(
            conversation_id="gemini-1",
            url="https://gemini.google.com/app/gemini-1",
            title="Gemini session",
        )
    ]
    with patch(
        "app.core.agent_session_sources._run_chromium_source_collection",
        return_value=links,
    ) as collector:
        payload = list_agent_sources("gemini", "edge", CrawlConfig())

    assert payload["platform"] == "gemini"
    assert payload["projects"] == []
    assert payload["recent_sessions"] == [
        {
            "id": "gemini-1",
            "title": "Gemini session",
            "url": "https://gemini.google.com/app/gemini-1",
            "updated_at": "",
        }
    ]
    assert collector.call_args.args[0] == "edge"
    assert collector.call_args.args[1].gemini_max_conversations == 20
    assert collector.call_args.args[2] == "https://gemini.google.com/app"


def test_grok_sources_reuse_the_existing_authenticated_conversations_api() -> None:
    conversations = [
        GrokConversation(
            conversation_id="grok-1",
            title="Grok session",
            created_at="2026-08-14T01:00:00Z",
            updated_at="2026-08-14T02:00:00Z",
            url="https://grok.com/c/grok-1",
        )
    ]
    with patch(
        "app.core.agent_session_sources._run_chromium_source_collection",
        return_value=conversations,
    ) as collector:
        payload = list_agent_sources("grok", "chrome", CrawlConfig())

    assert payload["platform"] == "grok"
    assert payload["recent_sessions"][0] == {
        "id": "grok-1",
        "title": "Grok session",
        "url": "https://grok.com/c/grok-1",
        "updated_at": "2026-08-14T02:00:00Z",
    }
    assert collector.call_args.args[:3] == ("chrome", CrawlConfig(), "https://grok.com/")


def test_chatgpt_sources_still_use_the_existing_project_capable_adapter() -> None:
    existing_payload = {"recent_sessions": [], "projects": [{"url": "project"}]}
    with patch(
        "app.core.agent_session_sources.list_chatgpt_agent_sources",
        return_value=existing_payload,
    ) as sources:
        payload = list_agent_sources("chatgpt", "edge", CrawlConfig())

    assert payload == {**existing_payload, "platform": "chatgpt"}
    sources.assert_called_once()


def test_gemini_sources_expose_notebooks_as_shared_projects() -> None:
    with patch(
        "app.core.agent_session_sources._run_chromium_source_collection",
        return_value={
            "recent_sessions": [
                {
                    "conversation_id": "gemini-1",
                    "title": "Gemini session",
                    "url": "https://gemini.google.com/app/gemini-1",
                }
            ],
            "projects": [
                {
                    "id": "notebook-1",
                    "title": "Research notebook",
                    "url": "https://gemini.google.com/notebook/notebook-1",
                    "updated_at": "",
                },
                {
                    "id": "notebook-1",
                    "title": "Duplicate app alias",
                    "url": "https://gemini.google.com/app/notebook-1",
                    "updated_at": "",
                },
            ],
        },
    ):
        payload = list_agent_sources("gemini", "edge", CrawlConfig())

    assert payload["projects"] == [
        {
            "id": "notebook-1",
            "title": "Research notebook",
            "url": "https://gemini.google.com/app/notebook-1",
            "updated_at": "",
        }
    ]


def test_gemini_project_reader_ignores_recent_app_links_in_a_notebook_nav() -> None:
    rows = (
        {
            "href": "https://gemini.google.com/notebook/notebook-1",
            "title": "Research notebook",
        },
        {
            "href": "https://gemini.google.com/app/recent-chat",
            "title": "Ordinary recent chat",
        },
        {
            "href": "https://gemini.google.com/notebook/create",
            "title": "New notebook",
        },
        {
            "href": "https://gemini.google.com/notebooks/new",
            "title": "Create notebook",
        },
    )

    class _NotebookLinks:
        def __init__(self, selector: str) -> None:
            self.selector = selector

        def evaluate_all(self, script: str) -> list[dict[str, str]]:
            assert "parent.tagName === 'NAV'" not in script
            assert "context.join" not in script
            return [
                row
                for row in rows
                if "/notebook/" in row["href"] or "/notebooks/" in row["href"]
            ]

    class _Page:
        def locator(self, selector: str) -> _NotebookLinks:
            assert 'href*="/notebook/"' in selector
            assert 'href*="/notebooks/"' in selector
            assert 'href*="/app/"' not in selector
            return _NotebookLinks(selector)

    assert _read_gemini_project_links(_Page()) == [
        {
            "id": "notebook-1",
            "title": "Research notebook",
            "url": "https://gemini.google.com/app/notebook-1",
            "updated_at": "",
        }
    ]


def test_grok_sources_expose_projects_as_shared_projects() -> None:
    with patch(
        "app.core.agent_session_sources._run_chromium_source_collection",
        return_value={
            "recent_sessions": [],
            "projects": [
                {
                    "id": "project-1",
                    "title": "Research project",
                    "url": "https://grok.com/project/project-1?tab=conversations",
                    "updated_at": "",
                }
            ],
        },
    ):
        payload = list_agent_sources("grok", "edge", CrawlConfig())

    assert payload["projects"][0]["url"] == "https://grok.com/project/project-1?tab=conversations"


def test_grok_project_reader_uses_playwright_controls_for_button_rows() -> None:
    class _Toggle:
        def count(self) -> int:
            return 1

        def is_visible(self) -> bool:
            return True

        def get_attribute(self, _name: str) -> str:
            return "false"

        def click(self, **_kwargs: object) -> None:
            return None

    class _Locator:
        def evaluate_all(self, _script: str) -> list[dict[str, object]]:
            return [{"index": 3, "label": "Research project New chat Options"}]

        def nth(self, _index: int) -> "_Locator":
            return self

        def click(self, **_kwargs: object) -> None:
            return None

    class _Page:
        def get_by_role(self, _role: str, *, name: str, exact: bool) -> _Toggle:
            assert name == "Projects"
            assert exact is True
            return _Toggle()

        def wait_for_timeout(self, _milliseconds: int) -> None:
            return None

        def locator(self, selector: str) -> _Locator:
            if selector == 'a[href*="/project/"]':
                return _LocatorWithRows()
            return _Locator()

    class _LocatorWithRows(_Locator):
        def evaluate_all(self, _script: str) -> list[dict[str, str]]:
            return [{
                "href": "https://grok.com/project/project-1?chat=session-1",
                "title": "Research project",
            }]

    assert _read_grok_project_links(_Page()) == [{
        "id": "project-1",
        "title": "Research project",
        "url": "https://grok.com/project/project-1?tab=conversations",
        "updated_at": "",
    }]


def test_grok_project_reader_prefers_workspace_repository_api() -> None:
    class _Page:
        def evaluate(self, _script: str, request: dict[str, object]) -> dict[str, object]:
            assert "/rest/workspaces?" in str(request["url"])
            return {
                "status": 200,
                "body": {
                    "workspaces": [
                        {
                            "workspaceId": "project-1",
                            "name": "Research project",
                            "lastUseTime": "2026-08-14T12:00:00Z",
                            "kind": "WORKSPACE_KIND_ALL",
                        },
                        {
                            "workspaceId": "imagine-1",
                            "name": "Generated images",
                            "kind": "WORKSPACE_KIND_IMAGINE",
                        },
                    ]
                },
            }

    assert _read_grok_project_links(_Page()) == [{
        "id": "project-1",
        "title": "Research project",
        "url": "https://grok.com/project/project-1?tab=conversations",
        "updated_at": "2026-08-14T12:00:00Z",
    }]


def test_grok_project_session_reader_uses_workspace_conversations_api() -> None:
    class _Page:
        def evaluate(self, _script: str, request: dict[str, object]) -> dict[str, object]:
            assert "workspaceId=project-1" in str(request["url"])
            return {
                "status": 200,
                "body": {
                    "conversations": [{
                        "conversationId": "session-1",
                        "title": "Research session",
                        "modifyTime": "2026-08-14T12:30:00Z",
                    }]
                },
            }

    assert _read_grok_project_session_links(
        _Page(),
        "https://grok.com/project/project-1?tab=conversations",
    ) == [{
        "id": "session-1",
        "title": "Research session",
        "url": "https://grok.com/project/project-1?chat=session-1",
        "updated_at": "2026-08-14T12:30:00Z",
    }]


def test_grok_project_session_fallback_keeps_only_same_project_chat_urls() -> None:
    class _Page:
        def evaluate(
            self,
            _script: str,
            request: dict[str, object] | None = None,
        ) -> object:
            if request is not None:
                raise RuntimeError("API unavailable")
            return [
                {
                    "href": "https://grok.com/project/project-1?chat=session-1",
                    "title": "Same project",
                },
                {
                    "href": "https://grok.com/c/root-session",
                    "title": "Root chat",
                },
                {
                    "href": "https://grok.com/project/project-2?chat=session-2",
                    "title": "Other project",
                },
            ]

    assert _read_grok_project_session_links(
        _Page(),
        "https://grok.com/project/project-1?tab=conversations",
    ) == [
        {
            "id": "session-1",
            "title": "Same project",
            "url": "https://grok.com/project/project-1?chat=session-1",
            "updated_at": "",
        }
    ]


def test_claude_project_session_reader_keeps_only_same_project_conversations() -> None:
    class _Page:
        def evaluate(self, _script: str) -> list[dict[str, str]]:
            return [
                {
                    "href": "https://claude.ai/project/project-1/chat/session-1",
                    "title": "Same project",
                },
                {
                    "href": "https://claude.ai/project/project-1/c/session-2",
                    "title": "Same project legacy route",
                },
                {
                    "href": "https://claude.ai/project/project-2/chat/session-3",
                    "title": "Other project",
                },
                {
                    "href": "https://claude.ai/chat/root-session",
                    "title": "Root chat",
                },
            ]

    assert claude_project_session_id(
        "https://claude.ai/project/project-1/c/session-2",
        "https://claude.ai/project/project-1",
    ) == "session-2"
    assert claude_project_session_id(
        "https://claude.ai/project/project-2/chat/session-3",
        "https://claude.ai/project/project-1",
    ) == ""
    assert _read_project_session_links(
        _Page(),
        "claude",
        "https://www.claude.ai/project/project-1/?tab=chats",
    ) == [
        {
            "id": "session-1",
            "title": "Same project",
            "url": "https://claude.ai/project/project-1/chat/session-1",
            "updated_at": "",
        },
        {
            "id": "session-2",
            "title": "Same project legacy route",
            "url": "https://claude.ai/project/project-1/c/session-2",
            "updated_at": "",
        },
    ]


def test_gemini_project_session_listing_fails_closed_without_collection() -> None:
    with patch(
        "app.core.agent_session_sources._run_chromium_source_collection"
    ) as collector:
        payload = list_agent_project_sessions(
            "gemini",
            "edge",
            "https://gemini.google.com/app/notebook-1",
            CrawlConfig(),
        )

    collector.assert_not_called()
    assert payload["sessions"] == []
    assert payload["project_url"] == "https://gemini.google.com/app/notebook-1"
    assert payload["message"] == (
        "Gemini Notebook session ownership cannot be verified; "
        "use New session in project."
    )


def test_gemini_project_session_dom_fallback_never_reads_page_links() -> None:
    class _Page:
        def evaluate(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("Gemini Project sessions must not inspect page links.")

    assert _read_project_session_links(
        _Page(),
        "gemini",
        "https://gemini.google.com/app/notebook-1",
    ) == []


def test_grok_project_session_listing_uses_the_shared_contract() -> None:
    project_url = "https://grok.com/project/project-1?tab=conversations"
    session_url = "https://grok.com/project/project-1?chat=grok-session"
    with patch(
        "app.core.agent_session_sources._run_chromium_source_collection",
        return_value=[
            {
                "id": "selected-session",
                "title": "Selected session",
                "url": session_url,
                "updated_at": "",
            }
        ],
    ) as collector:
        payload = list_agent_project_sessions(
            "grok",
            "edge",
            project_url,
            CrawlConfig(),
        )

    assert payload["platform"] == "grok"
    assert payload["project_url"] == project_url
    assert payload["sessions"][0]["url"] == session_url
    assert collector.call_args.args[2] == project_url


def test_grok_conversation_history_fetch_pairs_project_messages() -> None:
    class _Page:
        def evaluate(self, _script: str, request: dict[str, object]) -> dict[str, object]:
            url = str(request["url"])
            if "workspaceId=project-1" in url:
                return {
                    "status": 200,
                    "body": {
                        "conversations": [{
                            "conversationId": "session-1",
                            "title": "Renamed project session",
                        }]
                    },
                }
            if url.endswith("/response-node"):
                return {
                    "status": 200,
                    "body": {
                        "responseNodes": [
                            {"responseId": "user-1"},
                            {"responseId": "assistant-1"},
                        ]
                    },
                }
            if url.endswith("/load-responses"):
                return {
                    "status": 200,
                    "body": {
                        "responses": [
                            {
                                "responseId": "user-1",
                                "sender": "human",
                                "message": "What changed?",
                                "createTime": "2026-09-02T01:00:00Z",
                            },
                            {
                                "responseId": "assistant-1",
                                "sender": "assistant",
                                "message": "The selected session is now visible.",
                                "createTime": "2026-09-02T01:00:02Z",
                            },
                        ]
                    },
                }
            raise AssertionError(f"Unexpected Grok history request: {url}")

    def run_collector(_browser: str, _config: CrawlConfig, _url: str, collector, **_kwargs):
        return collector(_Page())

    with patch(
        "app.core.agent_session_sources._run_chromium_source_collection",
        side_effect=run_collector,
    ):
        payload = fetch_grok_conversation_history(
            "edge",
            "https://grok.com/project/project-1?chat=session-1",
            CrawlConfig(),
        )

    assert payload["title"] == "Renamed project session"
    assert payload["history"] == [{
        "prompt": "What changed?",
        "response": "The selected session is now visible.",
        "started_at": "2026-09-02T01:00:00Z",
        "finished_at": "2026-09-02T01:00:02Z",
    }]


def test_chatgpt_project_session_listing_keeps_existing_adapter() -> None:
    with patch(
        "app.core.agent_session_sources.list_chatgpt_project_sessions",
        return_value={"project_url": "https://chatgpt.com/g/g-p-demo/project", "sessions": []},
    ) as sessions:
        payload = list_agent_project_sessions(
            "chatgpt",
            "edge",
            "https://chatgpt.com/g/g-p-demo/project",
            CrawlConfig(),
        )

    assert payload["platform"] == "chatgpt"
    sessions.assert_called_once()
