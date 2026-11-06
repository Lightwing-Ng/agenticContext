"""Regression tests for the observed ChatGPT project landing redirect."""

# Code version: v1.0.0-codex.0

from unittest.mock import patch

import pytest

from app.core.safari_automation import (
    SafariContext,
    SafariPage,
    safari_navigation_matches,
)


PROJECT_COMPONENT = "g-p-0123456789abcdef0123456789abcdef-studio-fixture"
BARE_PATH = f"/g/{PROJECT_COMPONENT}"


@pytest.mark.parametrize("host", ("chatgpt.com", "www.chatgpt.com"))
@pytest.mark.parametrize("slug", ("", "-studio-fixture"))
@pytest.mark.parametrize("reverse", (False, True))
def test_project_redirect_accepts_only_the_same_complete_project_component(
    host: str, slug: str, reverse: bool,
) -> None:
    bare = f"https://{host}/g/g-p-0123456789abcdef0123456789abcdef{slug}"
    landing = f"{bare}/project"
    target, current = (landing, bare) if reverse else (bare, landing)

    assert safari_navigation_matches(target, current)


@pytest.mark.parametrize("reverse", (False, True))
def test_project_redirect_preserves_existing_query_and_fragment_tolerance(
    reverse: bool,
) -> None:
    bare = f"https://chatgpt.com{BARE_PATH}?view=recent#top"
    landing = f"https://chatgpt.com{BARE_PATH}/project?view=all#history"
    target, current = (landing, bare) if reverse else (bare, landing)

    assert safari_navigation_matches(target, current)


@pytest.mark.parametrize(
    "target_path,current_path",
    (
        (BARE_PATH, "/g/g-p-fedcba9876543210fedcba9876543210-studio-fixture/project"),
        (BARE_PATH, "/g/g-p-0123456789abcdef0123456789abcdef-renamed/project"),
        (BARE_PATH, "/g/g-p-0123456789abcdef0123456789abcdef/project"),
        (BARE_PATH, f"{BARE_PATH}/c/session-one"),
        (BARE_PATH, "/c/session-one"),
        (BARE_PATH, f"{BARE_PATH}/project/c/session-one"),
        (BARE_PATH, f"{BARE_PATH}/project/extra"),
        (BARE_PATH, f"{BARE_PATH}//project"),
        (BARE_PATH, f"{BARE_PATH}/project/"),
        (f"{BARE_PATH}/", f"{BARE_PATH}/project"),
        (f"{BARE_PATH}//", f"{BARE_PATH}/project"),
        (BARE_PATH, f"{BARE_PATH}%2Fproject"),
        (BARE_PATH, f"{BARE_PATH}/%70roject"),
        (BARE_PATH, f"{BARE_PATH}/../{PROJECT_COMPONENT}/project"),
        ("/g/g-p-0123456789abcdef0123456789abcde", "/g/g-p-0123456789abcdef0123456789abcde/project"),
        ("/g/g-p-0123456789abcdef0123456789abcdef0", "/g/g-p-0123456789abcdef0123456789abcdef0/project"),
        ("/g/g-p-0123456789abcdef0123456789abcdeg", "/g/g-p-0123456789abcdef0123456789abcdeg/project"),
        ("/g/g-p-demo", "/g/g-p-demo/project"),
        ("/g/g-p-0123456789abcdef0123456789abcdef-%2Fother", "/g/g-p-0123456789abcdef0123456789abcdef-%2Fother/project"),
        (BARE_PATH, f"/g/{PROJECT_COMPONENT.upper()}/project"),
    ),
)
@pytest.mark.parametrize("reverse", (False, True))
def test_project_redirect_rejects_other_projects_chats_and_noncanonical_paths(
    target_path: str, current_path: str, reverse: bool,
) -> None:
    target = f"https://chatgpt.com{target_path}"
    current = f"https://chatgpt.com{current_path}"
    if reverse:
        target, current = current, target

    assert not safari_navigation_matches(target, current)


@pytest.mark.parametrize(
    "target_origin,current_origin",
    (
        ("https://chatgpt.com", "https://www.chatgpt.com"),
        ("https://chatgpt.com", "https://example.com"),
        ("https://chatgpt.com", "https://chatgpt.com.example.com"),
        ("https://chatgpt.com", "http://chatgpt.com"),
        ("http://chatgpt.com", "http://chatgpt.com"),
        ("https://chatgpt.com", "https://chatgpt.com:443"),
        ("https://chatgpt.com", "https://chatgpt.com:444"),
        ("https://chatgpt.com:443", "https://chatgpt.com:443"),
        ("https://chatgpt.com:444", "https://chatgpt.com:444"),
        ("https://chatgpt.com", "https://chatgpt.com:invalid"),
        ("https://chatgpt.com", "https://chatgpt.com:"),
        ("https://chatgpt.com", "https://user:pass@chatgpt.com"),
        ("https://user:pass@chatgpt.com", "https://user:pass@chatgpt.com"),
        ("https://chatgpt.com", "https://@chatgpt.com"),
    ),
)
@pytest.mark.parametrize("reverse", (False, True))
def test_project_redirect_rejects_origin_port_and_userinfo_drift(
    target_origin: str, current_origin: str, reverse: bool,
) -> None:
    target = f"{target_origin}{BARE_PATH}"
    current = f"{current_origin}{BARE_PATH}/project"
    if reverse:
        target, current = current, target

    assert not safari_navigation_matches(target, current)


@pytest.mark.parametrize("reverse", (False, True))
def test_project_redirect_ready_landing_does_not_retry_navigation(reverse: bool) -> None:
    bare = f"https://chatgpt.com{BARE_PATH}"
    landing = f"{bare}/project"
    target, current = (landing, bare) if reverse else (bare, landing)
    page = SafariPage(SafariContext(target), window_id=123)

    with (
        patch.object(
            page,
            "_read_navigation_state",
            return_value={"href": current, "readyState": "complete"},
        ),
        patch.object(page, "_keep_in_background") as background,
        patch.object(page, "_run_in_window") as run,
    ):
        page.goto(target, timeout=1_000)

    background.assert_called_once_with()
    run.assert_not_called()
