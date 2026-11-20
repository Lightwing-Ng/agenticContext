"""Preserve read-service and history-page identity through the storage boundary.

Code version: v1.0.0-codex.0
"""

from app.core import chat_history_browser, storage, wechat_browser_read


def test_storage_preserves_the_history_page_class_identity():
    assert storage.ChatHistoryPage is chat_history_browser.ChatHistoryPage
    assert "ChatHistoryPage" in storage.__all__


def test_storage_preserves_the_optional_browser_read_service_class_identity():
    assert storage.WechatBrowserReadService is wechat_browser_read.WechatBrowserReadService
    assert "WechatBrowserReadService" in storage.__all__
