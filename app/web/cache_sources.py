"""Presentation registry for cache source pages."""

# Code version: v1.6.0-codex.1

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from app.core.foundation import PRODUCT_NAME


@dataclass(frozen=True, slots=True)
class CacheSourceView:
    """Describe one cache source without coupling the shared page to its service."""

    key: str
    label: str
    view_endpoint: str
    template_name: str
    icon_filename: str
    document_title: str
    overview_title: str
    browser_panel_label: str
    browser_empty_message: str
    browser_config_field: str
    require_browser_ready: bool
    start_form_id: str
    start_button_label: str
    start_wait_title: str
    start_wait_copy: str
    stop_wait_title: str
    stop_wait_copy: str
    progress_strategy: str
    progress_aria_label: str
    group_key: str = "media"
    group_label: str = "Cache"
    include_in_llm_switcher: bool = False
    preserve_icon_color: bool = False
    show_shared_settings_link: bool = True
    show_content_mode: bool = False
    show_progress_audit: bool = False
    show_progress_value: bool = False
    show_progress_detail: bool = False
    settings_category: str = ""
    settings_link_label: str = ""
    supported_browsers: tuple[str, ...] = ()

    @property
    def dock_label(self) -> str:
        """Return the source label used by the dock menu."""
        return f"{self.label} cache"

    @property
    def browser_input_id(self) -> str:
        """Return the hidden browser input identifier."""
        return f"{self.key}_browser_input"

    @property
    def browser_storage_key(self) -> str:
        """Return the session-scoped browser selection key."""
        return f"cachelikes:browser-selection:{self.key}"

    @property
    def resolved_settings_category(self) -> str:
        """Return the existing Settings category owned by this source."""

        if self.settings_category:
            return self.settings_category
        if self.group_key == "llm" or self.include_in_llm_switcher:
            return "llm"
        return "downloads"

    @property
    def resolved_settings_link_label(self) -> str:
        """Return source-specific Settings copy without template conditionals."""

        if self.settings_link_label:
            return self.settings_link_label
        if self.resolved_settings_category == "llm":
            return f"Open {self.label} settings"
        return "Open shared cache settings"


_CACHE_SOURCE_VIEWS = (
    CacheSourceView(
        key="claude",
        label="Claude",
        view_endpoint="claude",
        template_name="claude.html",
        icon_filename="images/claude.svg",
        document_title=f"{PRODUCT_NAME} Claude",
        overview_title="Claude history cache overview",
        browser_panel_label="Authorized browser",
        browser_empty_message="No signed-in Claude account detected",
        browser_config_field="claude_browser",
        require_browser_ready=True,
        start_form_id="start_form_claude",
        start_button_label="Start",
        start_wait_title="Starting Claude history sync",
        start_wait_copy="Preparing the selected browser session and caching Claude sessions to Parquet.",
        stop_wait_title="Stopping Claude history sync",
        stop_wait_copy="Requesting a safe stop after the current Claude session.",
        progress_strategy="queue",
        progress_aria_label="Claude history sync progress",
        group_key="llm",
        group_label="Chats",
        show_content_mode=True,
        show_progress_value=True,
        show_progress_detail=True,
    ),
    CacheSourceView(
        key="gemini",
        label="Gemini",
        view_endpoint="gemini",
        template_name="gemini.html",
        icon_filename="images/Google_Gemini_logo_2025_symbol.svg",
        document_title=f"{PRODUCT_NAME} Gemini",
        overview_title="Gemini history cache overview",
        browser_panel_label="Authorized browser",
        browser_empty_message="No authorized Gemini account detected",
        browser_config_field="gemini_browser",
        require_browser_ready=True,
        start_form_id="start_form_gemini",
        start_button_label="Start",
        start_wait_title="Starting Gemini history sync",
        start_wait_copy="Preparing the selected browser session and caching Gemini sessions to Parquet.",
        stop_wait_title="Stopping Gemini history sync",
        stop_wait_copy="Requesting a safe stop after the current Gemini session.",
        progress_strategy="queue",
        progress_aria_label="Gemini history sync progress",
        group_key="llm",
        group_label="Chats",
        preserve_icon_color=True,
        show_content_mode=True,
        show_progress_value=True,
        show_progress_detail=True,
    ),
    CacheSourceView(
        key="x",
        label="X",
        view_endpoint="index",
        template_name="index.html",
        icon_filename="images/x.svg",
        document_title=PRODUCT_NAME,
        overview_title="Execution overview",
        browser_panel_label="Browser",
        browser_empty_message="No signed-in account detected",
        browser_config_field="x_browser",
        require_browser_ready=False,
        start_form_id="start_form",
        start_button_label="Start",
        start_wait_title="Starting X cache",
        start_wait_copy="Preparing the signed-in X session and starting the local cache task.",
        stop_wait_title="Stopping X cache",
        stop_wait_copy="Requesting a safe stop for the active X cache task.",
        progress_strategy="discovery",
        progress_aria_label="Download progress",
    ),
    CacheSourceView(
        key="grok",
        label="Grok",
        view_endpoint="grok",
        template_name="grok.html",
        icon_filename="images/grok.svg",
        document_title=f"{PRODUCT_NAME} Grok",
        overview_title="Grok library overview",
        browser_panel_label="Authorized browser",
        browser_empty_message="No signed-in account detected",
        browser_config_field="grok_browser",
        require_browser_ready=True,
        start_form_id="start_form_grok",
        start_button_label="Start",
        start_wait_title="Starting Grok sync",
        start_wait_copy="Preparing the selected browser session and starting the local Grok media sync.",
        stop_wait_title="Stopping Grok sync",
        stop_wait_copy="Requesting a safe stop for the active Grok sync.",
        progress_strategy="grok-audit",
        progress_aria_label="Grok sync progress",
        include_in_llm_switcher=True,
        show_content_mode=True,
        show_progress_audit=True,
        show_progress_detail=True,
    ),
    CacheSourceView(
        key="chatgpt",
        label="ChatGPT",
        view_endpoint="chatgpt",
        template_name="chatgpt.html",
        icon_filename="images/ChatGPT-Logo.svg",
        document_title=f"{PRODUCT_NAME} ChatGPT",
        overview_title="ChatGPT cache overview",
        browser_panel_label="Authorized browser",
        browser_empty_message="The ChatGPT account in the selected browser is ready.",
        browser_config_field="chatgpt_browser",
        require_browser_ready=True,
        start_form_id="start_form_chatgpt",
        start_button_label="Start",
        start_wait_title="Starting ChatGPT sync",
        start_wait_copy="Preparing the selected browser session and starting the original-resolution image sync.",
        stop_wait_title="Stopping ChatGPT sync",
        stop_wait_copy="Requesting a safe stop for the active ChatGPT sync.",
        progress_strategy="queue",
        progress_aria_label="ChatGPT sync progress",
        include_in_llm_switcher=True,
        show_content_mode=True,
        show_progress_audit=True,
        show_progress_value=True,
        show_progress_detail=True,
    ),
    CacheSourceView(
        key="zhihu",
        label="Zhihu",
        view_endpoint="zhihu",
        template_name="zhihu.html",
        icon_filename="images/zhihu.svg",
        document_title=f"{PRODUCT_NAME} Zhihu",
        overview_title="Zhihu answers cache overview",
        browser_panel_label="Authorized browser",
        browser_empty_message="No signed-in Zhihu account detected",
        browser_config_field="zhihu_browser",
        require_browser_ready=True,
        start_form_id="start_form_zhihu",
        start_button_label="Start",
        start_wait_title="Starting Zhihu answer cache",
        start_wait_copy="Preparing the authenticated browser and reading Zhihu answer text.",
        stop_wait_title="Stopping Zhihu answer cache",
        stop_wait_copy="Requesting a safe stop before the next activity page or final write.",
        progress_strategy="queue",
        progress_aria_label="Zhihu answer cache progress",
        group_key="llm",
        group_label="Cache",
        include_in_llm_switcher=True,
        preserve_icon_color=True,
        show_content_mode=True,
        show_progress_value=True,
        show_progress_detail=True,
        settings_category="downloads",
        settings_link_label="Open Zhihu settings",
        supported_browsers=("edge", "chrome"),
    ),
)


CACHE_SOURCE_VIEWS = tuple(sorted(_CACHE_SOURCE_VIEWS, key=lambda source: source.label.casefold()))
MEDIA_CACHE_SOURCE_VIEWS = tuple(source for source in CACHE_SOURCE_VIEWS if source.group_key == "media")
LLM_CACHE_SOURCE_VIEWS = tuple(source for source in CACHE_SOURCE_VIEWS if source.group_key == "llm")
LLM_SWITCHER_SOURCE_VIEWS = tuple(
    source
    for source in CACHE_SOURCE_VIEWS
    if source.group_key == "llm" or source.include_in_llm_switcher
)
CACHE_SOURCE_BY_KEY = MappingProxyType({source.key: source for source in CACHE_SOURCE_VIEWS})


def cache_source_views_for_group(group_key: str) -> tuple[CacheSourceView, ...]:
    """Return the source switcher options for one dock group."""
    normalized_group = str(group_key or "").strip().lower()
    if normalized_group == "llm":
        return LLM_SWITCHER_SOURCE_VIEWS
    return MEDIA_CACHE_SOURCE_VIEWS


def cache_source_views_for_page(source_key: str) -> tuple[CacheSourceView, ...]:
    """Return the complete registry for every rendered cache-page switcher."""
    if get_cache_source_view(source_key) is None:
        return ()
    return CACHE_SOURCE_VIEWS


def get_cache_source_view(source_key: str) -> CacheSourceView | None:
    """Return one registered source using a normalized source key."""
    return CACHE_SOURCE_BY_KEY.get(str(source_key or "").strip().lower())


def get_cache_source_label(source_key: str) -> str:
    """Return a registered label with a safe title-cased fallback."""
    source = get_cache_source_view(source_key)
    return source.label if source is not None else str(source_key or "").title()
