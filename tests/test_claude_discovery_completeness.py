"""Retain verified Claude sessions without converting bounded discovery into success."""

# Code version: v1.0.0-codex.0

from contextlib import nullcontext
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from PIL import Image

from app.core.claude_history import (
    ClaudeConversationDiscoveryIncompleteError,
    ClaudeConversationLink,
    ClaudeHistoryStore,
    claude_history_path,
    sync_claude_history,
)
from app.core.claude_history_service import ClaudeHistoryService
from app.core.claude_media import (
    ClaudeMediaCatalog,
    ClaudeMediaSyncResult,
    _candidate_from_rendered_image,
    claude_media_dir,
    sync_claude_media,
)
from app.core.config import CrawlConfig
from app.core.job_lock import CacheTaskLock
from app.core.resource_persistence import read_parquet_rows
from app.core.state import TaskSnapshot, TaskState


OLD = ClaudeConversationLink('old', 'https://claude.ai/chat/old', 'Old fixture')
NEW = ClaudeConversationLink('new', 'https://claude.ai/chat/new', 'New fixture')


def test_text_partial_discovery_commits_verified_session_and_preserves_old_rows(tmp_path: Path, macos_host) -> None:
    store = ClaudeHistoryStore(claude_history_path(tmp_path))
    store.replace_conversation(OLD, [{'role': 'user', 'content_text': 'Previously cached content'}], '2026-10-22T00:00:00Z')
    store.save()
    old_rows = read_parquet_rows(store.path)
    page = Mock()
    state = TaskState('test', snapshot_factory=TaskSnapshot)
    with patch('app.core.claude_history._open_claude_history_page', return_value=nullcontext(page)), patch(
        'app.core.claude_history.goto_with_retry',
    ), patch('app.core.claude_history._wait_for_claude_ready'), patch(
        'app.core.claude_history.discover_claude_conversations',
        side_effect=ClaudeConversationDiscoveryIncompleteError('Fixture discovery is incomplete.', [NEW]),
    ), patch('app.core.claude_history.load_claude_conversation_messages', return_value=(
        [{'role': 'assistant', 'content_text': 'New verified content'}], 'payload',
    )):
        result = sync_claude_history(state, CrawlConfig(claude_browser='safari'), lambda: False, tmp_path)
    rows = read_parquet_rows(store.path)
    assert [row for row in rows if row['conversation_id'] == OLD.conversation_id] == old_rows
    assert any(row['content_text'] == 'New verified content' for row in rows)
    assert result['incomplete'] and result['failed'] == 0 and result['sessions'] == 1
    assert state.snapshot()['phase'] == 'failed'
    assert state.snapshot()['discovery_complete'] is False


def _image(conversation):
    return _candidate_from_rendered_image({
        'source_url': f'https://assets.claude.ai/images/{conversation.conversation_id}.png',
        'message_index': 0, 'image_index': 0, 'role': 'assistant',
    }, conversation)


def test_media_partial_discovery_commits_verified_image_and_keeps_prior_file(tmp_path: Path, macos_host) -> None:
    output = BytesIO()
    Image.new('RGB', (2, 2), (0, 85, 204)).save(output, format='PNG')
    content = output.getvalue()
    catalog = ClaudeMediaCatalog(claude_media_dir(tmp_path))
    catalog.root.mkdir(parents=True)
    old = _image(OLD)
    new = _image(NEW)
    assert old is not None and new is not None
    relative = f'img_{old.asset_id}.png'
    (catalog.root / relative).write_bytes(content)
    catalog.register(old, relative_path=relative, content=content)
    page = Mock()

    def transfer(_url, path, _should_stop, **kwargs):
        path.write_bytes(content)
        return 'image/png', False

    page.download_to_path.side_effect = transfer
    context = Mock(primary_page=page)
    state = TaskState('test', snapshot_factory=TaskSnapshot)
    with patch('app.core.claude_media.SafariContext', return_value=nullcontext(context)), patch(
        'app.core.claude_media.goto_with_retry',
    ), patch('app.core.claude_media._wait_for_claude_ready'), patch(
        'app.core.claude_media.discover_claude_conversations',
        side_effect=ClaudeConversationDiscoveryIncompleteError('Fixture discovery is incomplete.', [NEW]),
    ), patch('app.core.claude_media.collect_claude_conversation_images', return_value=([new], 0)):
        result = sync_claude_media(state, CrawlConfig(claude_browser='safari'), lambda: False, tmp_path)
    assert result.incomplete and result.discovery_incomplete
    assert result.failed_images == result.failed_sessions == 0
    assert result.cached_images == 2 and result.downloaded_images == 1
    assert (catalog.root / relative).read_bytes() == content
    assert state.snapshot()['phase'] == 'failed'
    assert state.snapshot()['discovery_complete'] is False


@pytest.mark.parametrize('mode', ['text', 'media'])
def test_service_cannot_finish_successfully_after_partial_session_discovery(tmp_path: Path, mode: str) -> None:
    class ImmediateThread:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    state = TaskState('test', snapshot_factory=TaskSnapshot)
    service = ClaudeHistoryService(state, local_store_root=tmp_path, task_lock=CacheTaskLock(tmp_path / 'task.lock'))
    text = {'sessions': 1, 'messages': 1, 'added_or_changed': 1, 'unchanged': 0,
            'failed': 0, 'stopped': False, 'incomplete': True}
    media = ClaudeMediaSyncResult(sessions=1, cached_images=1, discovery_incomplete=True)
    with patch('app.core.claude_history_service.Thread', ImmediateThread), patch(
        'app.core.claude_history_service.sync_claude_history', return_value=text,
    ), patch('app.core.claude_history_service.sync_claude_media', return_value=media), patch(
        'app.core.claude_history_service.append_shadow_backup_completion',
    ) as backup:
        service.start(CrawlConfig(claude_browser='safari'), content_mode=mode)
    assert state.snapshot()['phase'] == 'failed'
    assert 'Session discovery did not reach the end' in state.snapshot()['message']
    backup.assert_not_called()
