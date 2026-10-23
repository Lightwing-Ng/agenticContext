"""Read-only, session-wide ChatGPT media gallery contracts.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from PIL import Image

from app.web.app import create_app

INDEX_PATH = "/browser?view=media&source=chatgpt&session_view=1&session_index=1"
ALL_MEDIA_PATH = INDEX_PATH + "&media_view=all"


def seed_flat_media(root: Path) -> None:
    """Create 27 distinct images and two exact copies across three sessions."""
    project = root / "media/chatgpt/flat-fixture"
    project.mkdir(parents=True)
    entries = {}
    for number in range(1, 28):
        filename = f"image-{number:02d}.png"
        Image.new("RGB", (32 + number, 48), (number * 7, 80, 160)).save(project / filename)
        session_number = (number - 1) // 9 + 1
        entries[filename] = {
            "file_id": filename,
            "relative_path": filename,
            "conversation_url": f"https://chatgpt.com/c/flat-session-{session_number}",
            "conversation_title": f"Flat session {session_number}",
            "created_at": f"2026-10-{number:02d}T00:00:00Z",
            "width": 32 + number,
            "height": 48,
            "prompt_markdown": f"Fixture prompt {number}",
        }
    for number, session_number in ((1, 2), (2, 3)):
        filename = f"duplicate-{number}.png"
        (project / filename).write_bytes((project / "image-01.png").read_bytes())
        entries[filename] = {
            **entries["image-01.png"],
            "file_id": filename,
            "relative_path": filename,
            "conversation_url": f"https://chatgpt.com/c/flat-session-{session_number}",
            "conversation_title": f"Flat session {session_number}",
        }
    (project / ".chatgpt_catalog.json").write_text(json.dumps({"entries": entries}), encoding="utf-8")


class _PageElements(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.elements: list[dict[str, str | None]] = []
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append({"tag": tag, **dict(attrs)})


@pytest.fixture()
def flat_media_client(tmp_path: Path):
    root = tmp_path / "local-store"
    seed_flat_media(root)
    application = create_app(
        root,
        computer_use_settings_path=tmp_path / "settings.json",
        computer_use_runtime_root=tmp_path / "runtime",
        agent_external_operations_enabled=False,
    )
    application.config.update(TESTING=True)
    with application.test_client() as client:
        yield client, root


def _file_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_all_media_expands_sessions_deduplicates_before_paging_and_preserves_cache(flat_media_client):
    client, root = flat_media_client
    covers = client.get(INDEX_PATH)
    assert covers.status_code == 200
    assert covers.get_data(as_text=True).count("data-chatgpt-session-card") == 3
    # Warm the legacy catalog migration before checking the read-only display query.
    before = _file_hashes(root)
    first = client.get(ALL_MEDIA_PATH)
    second = client.get(ALL_MEDIA_PATH + "&page=2")
    assert first.status_code == second.status_code == 200
    first_elements = _PageElements(first.get_data(as_text=True)).elements
    second_elements = _PageElements(second.get_data(as_text=True)).elements
    first_ids = {element["data-media-id"] for element in first_elements if "data-media-id" in element}
    second_ids = {element["data-media-id"] for element in second_elements if "data-media-id" in element}
    assert len(first_ids) == 24
    assert len(second_ids) == 3
    assert not first_ids & second_ids
    assert 'data-numeric-display-value="27"' in first.get_data(as_text=True)
    assert 'data-view="all"' in first.get_data(as_text=True)
    assert "data-chatgpt-session-card" not in first.get_data(as_text=True)
    assert 'name="media_view" value="all"' in first.get_data(as_text=True)
    pagination = [element for element in first_elements if element.get("data-pagination-target") == "2"]
    assert pagination
    for element in pagination:
        query = parse_qs(urlsplit(element["href"]).query)
        assert query["media_view"] == ["all"]
        assert query["session_index"] == ["1"]
    # The normal cover view still contains every cached image in its resource total.
    assert 'data-numeric-display-value="29"' in client.get(INDEX_PATH).get_data(as_text=True)
    assert _file_hashes(root) == before


def test_all_media_keeps_search_sort_and_scopes_the_new_control(flat_media_client):
    client, _root = flat_media_client
    body = client.get(ALL_MEDIA_PATH + "&q=image-02.png&sort=oldest").get_data(as_text=True)
    assert body.count('data-media-id="') == 1
    assert 'name="media_view" value="all"' in body
    assert 'value="oldest" selected' in body
    control = next(element for element in _PageElements(body).elements if element.get("data-browser-view") == "all")
    assert control["aria-pressed"] == "true"
    for path in (
        "/browser?view=media&source=x&session_index=1&media_view=all",
        "/browser?view=text&source=chatgpt&session_index=1&media_view=all",
        "/browser?view=media&source=chatgpt&session_view=0&media_view=all",
    ):
        response = client.get(path)
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert 'data-browser-view="all"' not in html
        assert 'name="media_view"' not in html


def test_empty_all_sessions_still_offers_all_media_and_cover_modes(tmp_path: Path):
    application = create_app(
        tmp_path / "local-store",
        computer_use_settings_path=tmp_path / "settings.json",
        computer_use_runtime_root=tmp_path / "runtime",
        agent_external_operations_enabled=False,
    )
    with application.test_client() as client:
        for path in (INDEX_PATH, ALL_MEDIA_PATH):
            response = client.get(path)
            assert response.status_code == 200
            buttons = [
                element for element in _PageElements(response.get_data(as_text=True)).elements
                if "data-browser-view" in element
            ]
            assert {button["data-browser-view"] for button in buttons} == {"list", "grid", "all"}
            assert sum(button["aria-pressed"] == "true" for button in buttons) == 1
