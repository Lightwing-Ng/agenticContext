"""Regression coverage for complete virtualized Claude media collection.

Code version: v1.0.1-codex.0
"""

import hashlib
import io
import json
import shutil
import subprocess
from unittest.mock import MagicMock, Mock

import pytest
from PIL import Image

from app.core import claude_media as media
from app.core.claude_history import ClaudeConversationLink, ClaudeSecurityVerificationError
from app.core.config import CrawlConfig
from app.core.state import TaskState


CONVERSATION = ClaudeConversationLink("fixture", "https://claude.ai/chat/fixture", "Fixture")


class VirtualMediaPage:
    """Mount three image-bearing rows while preserving absolute transcript indices."""

    def __init__(self, total=14, hidden=6):
        self.total = total
        self.first_loaded = hidden
        self.position = total - 3
        self.downloads = 0
        self.scrolls = 0

    def wait_for_timeout(self, milliseconds):
        pass

    def evaluate(self, script, arg=None):
        if ".some(button => /load earlier messages/" in script:
            return self.first_loaded > 0
        assert "const images = []" in script
        return [
            {"source_url": f"https://assets.claude.ai/images/image-{row}.png",
             "message_index": row, "image_index": 0, "role": "assistant"}
            for row in range(self.position, min(self.total, self.position + 3))
        ]

    def expand(self, should_stop):
        while self.first_loaded and not should_stop():
            self.first_loaded = max(0, self.first_loaded - 4)

    def top(self):
        self.position = self.first_loaded

    def scroll(self):
        self.scrolls += 1
        before = self.position
        self.position = min(self.total - 3, self.position + 2)
        return {"moved": before != self.position, "atBottom": self.position >= self.total - 3}

    def download_to_path(self, url, path, should_stop, **kwargs):
        self.downloads += 1
        path.write_bytes(png_bytes())
        return "image/png", False


def png_bytes():
    stream = io.BytesIO()
    Image.new("RGB", (2, 2), (0, 85, 204)).save(stream, format="PNG")
    return stream.getvalue()


@pytest.fixture
def virtual_helpers(monkeypatch):
    monkeypatch.setattr(media, "_raise_if_claude_security_verification", lambda page: None)
    monkeypatch.setattr(media, "_expand_earlier_claude_messages", lambda page, stop: page.expand(stop))
    monkeypatch.setattr(media, "_prepare_claude_conversation_for_rendering", lambda page: page.top())
    monkeypatch.setattr(media, "_scroll_claude_transcript", lambda page: page.scroll())


def test_complete_virtual_transcript_finds_all_images_and_stable_indices(virtual_helpers):
    page = VirtualMediaPage()
    found, skipped = media.collect_claude_conversation_images(page, CONVERSATION, lambda: False)
    assert len(found) == 14
    assert [item.message_index for item in found] == list(range(14))
    assert len({item.asset_id for item in found}) == 14
    assert skipped == 0


def test_incomplete_traversal_is_an_error_instead_of_success(virtual_helpers):
    with pytest.raises(RuntimeError, match="did not finish"):
        media.collect_claude_conversation_images(
            VirtualMediaPage(), CONVERSATION, lambda: False, timeout_seconds=0,
        )


def test_stop_interrupts_traversal_without_claiming_completion(virtual_helpers):
    page = VirtualMediaPage()
    found, skipped = media.collect_claude_conversation_images(
        page, CONVERSATION, lambda: page.scrolls > 0,
    )
    assert len(found) == 3
    assert skipped == 0
    assert page.scrolls == 1


def test_later_security_challenge_stops_traversal(virtual_helpers, monkeypatch):
    page = VirtualMediaPage()
    def inspect(current):
        if current.scrolls:
            raise RuntimeError("Human verification required")
    monkeypatch.setattr(media, "_raise_if_claude_security_verification", inspect)
    with pytest.raises(RuntimeError, match="Human verification"):
        media.collect_claude_conversation_images(page, CONVERSATION, lambda: False)


def test_existing_file_identity_and_index_are_reused(tmp_path):
    raw = {"source_url": "https://assets.claude.ai/images/legacy.png", "message_index": 0,
           "image_index": 0, "role": "assistant"}
    old = media._candidate_from_rendered_image(raw, CONVERSATION)
    current = media._candidate_from_rendered_image({**raw, "message_index": 42}, CONVERSATION)
    catalog = media.ClaudeMediaCatalog(media.claude_media_dir(tmp_path))
    catalog.root.mkdir(parents=True)
    path = catalog.root / f"img_{old.asset_id}.png"
    content = png_bytes()
    path.write_bytes(content)
    catalog.register(old, relative_path=path.name, content=content)
    before = (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
    restored = media.ClaudeMediaCatalog(catalog.root)
    reused = restored.reuse_candidate(current)
    assert reused.asset_id == old.asset_id
    assert reused.message_index == old.message_index
    assert restored.contains(reused)
    assert before == (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js executes the rendered DOM extractor")
def test_dom_extractor_uses_absolute_transcript_row_indices():
    class CapturePage:
        script = ""
        def evaluate(self, script):
            self.script = script
            return []
    page = CapturePage()
    media.discover_claude_rendered_images(page, CONVERSATION)
    script = """
const article = row => ({
    querySelector: selector => selector.includes('user-message') ? null : {
        querySelectorAll: () => [{currentSrc: 'https://assets.claude.ai/image-' + row + '.png',
                                 getAttribute: () => ''}],
    },
    closest: () => ({getAttribute: () => String(row)}),
});
global.document = {querySelectorAll: () => [article(42), article(43)]};
const extract = EXTRACT;
const first = extract();
document.querySelectorAll = () => [article(43)];
console.log(JSON.stringify([first, extract()]));
""".replace("EXTRACT", page.script)
    response = subprocess.run(["node", "-e", script], check=True, text=True, capture_output=True)
    first, remounted = json.loads(response.stdout)
    assert [row["message_index"] for row in first] == [42, 43]
    assert remounted[0]["message_index"] == 43
    assert media._candidate_from_rendered_image(first[1], CONVERSATION).asset_id == (
        media._candidate_from_rendered_image(remounted[0], CONVERSATION).asset_id
    )


def test_full_sync_persists_every_image_then_reuses_it(tmp_path, virtual_helpers, monkeypatch, macos_host):
    page = VirtualMediaPage()
    context = MagicMock()
    context.primary_page = page
    context.__enter__.return_value = context
    monkeypatch.setattr(media, "SafariContext", lambda *args, **kwargs: context)
    monkeypatch.setattr(media, "goto_with_retry", lambda *args, **kwargs: None)
    monkeypatch.setattr(media, "_wait_for_claude_ready", lambda page: None)
    monkeypatch.setattr(media, "discover_claude_conversations", lambda *args, **kwargs: [CONVERSATION])
    first = media.sync_claude_media(
        TaskState("test"), CrawlConfig(claude_browser="safari"), lambda: False, tmp_path,
    )
    assert first.downloaded_images == first.cached_images == first.discovered_images == 14
    assert not first.incomplete
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in media.claude_media_dir(tmp_path).glob("img_*")}
    second = media.sync_claude_media(
        TaskState("test"), CrawlConfig(claude_browser="safari"), lambda: False, tmp_path,
    )
    assert second.downloaded_images == 0
    assert second.skipped_known == second.cached_images == 14
    assert page.downloads == 14
    assert before == {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in media.claude_media_dir(tmp_path).glob("img_*")}
    assert context.__exit__.call_count == 2


def test_load_earlier_limit_cannot_report_the_transcript_complete(monkeypatch):
    class NeverFinishesLoadingPage:
        clicks = 0

        def wait_for_timeout(self, milliseconds):
            pass

        def evaluate(self, script, arg=None):
            if "control.click()" in script:
                self.clicks += 1
                return True
            assert ".some(button => /load earlier messages/" in script
            return True

    monkeypatch.setattr(media, "_raise_if_claude_security_verification", lambda page: None)
    page = NeverFinishesLoadingPage()
    with pytest.raises(RuntimeError, match="load-earlier limit"):
        media.collect_claude_conversation_images(page, CONVERSATION, lambda: False)
    assert page.clicks == 200


def test_human_verification_aborts_before_the_next_conversation(tmp_path, monkeypatch, macos_host):
    page = MagicMock()
    context = MagicMock()
    context.primary_page = page
    context.__enter__.return_value = context
    sessions = [
        ClaudeConversationLink(f"fixture-{index}", f"https://claude.ai/chat/fixture-{index}", "Fixture")
        for index in range(2)
    ]
    challenge = ClaudeSecurityVerificationError("Fixture human verification challenge")
    navigate = Mock()
    inspect = Mock(side_effect=challenge)
    monkeypatch.setattr(media, "SafariContext", lambda *args, **kwargs: context)
    monkeypatch.setattr(media, "goto_with_retry", navigate)
    monkeypatch.setattr(media, "_wait_for_claude_ready", lambda page: None)
    monkeypatch.setattr(media, "discover_claude_conversations", lambda *args, **kwargs: sessions)
    monkeypatch.setattr(media, "collect_claude_conversation_images", inspect)

    with pytest.raises(ClaudeSecurityVerificationError) as caught:
        media.sync_claude_media(
            TaskState("test"), CrawlConfig(claude_browser="safari"), lambda: False, tmp_path,
        )

    assert caught.value is challenge
    assert inspect.call_count == 1
    assert [call.args[1] for call in navigate.call_args_list] == [
        media.CLAUDE_HOME_URL, sessions[0].url,
    ]
    context.__exit__.assert_called_once()
    assert not list(media.claude_media_dir(tmp_path).glob("img_*"))
