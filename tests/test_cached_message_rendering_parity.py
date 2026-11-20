"""Pin cached-message rendering and fallback compatibility.

Code version: v1.0.0-codex.0
"""

import sys

from markupsafe import Markup
import pytest

from app.web import presentation
from app.web.presentation import cached_message_source_links, render_cached_message


_BODY = (
    'Before.\n\n<box><text>From fallback.</text></box>\n\n'
    'Claim <Cite ref="turn0search1"/>.'
)


def test_chatgpt_keeps_exact_sanitized_html_and_clickable_destinations():
    content_html = (
        '<h3>Stored</h3><table><tr><td colspan="2">Cell</td></tr></table>'
        '<script>discard()</script><a href="javascript:discard()">Unsafe URL</a>'
        '<a href="https://example.invalid/source?a=1&amp;b=2">Source</a>'
    )

    rendered = render_cached_message(_BODY, content_html, provider=" ChatGPT ")

    assert type(rendered) is Markup
    assert rendered == (
        '<h3>Stored</h3><div class="agent-markdown-table-shell" role="region" '
        'tabindex="0" aria-label="Scrollable message table"><table><tr>'
        '<td colspan="2">Cell</td></tr></table></div><a>Unsafe URL</a>'
        '<a href="https://example.invalid/source?a=1&amp;b=2">Source</a>'
    )
    source = "https://example.invalid/source?a=1&b=2"
    other = f"{source}#different"
    assert cached_message_source_links((source, other, other), rendered) == (other,)


@pytest.mark.parametrize("content_html", ("", "<script>discard()</script><!-- ignored -->"))
def test_chatgpt_formats_markdown_when_no_rich_html_survives(content_html):
    rendered = render_cached_message(_BODY, content_html, provider=" ChatGPT ")

    assert type(rendered) is Markup
    assert rendered == "<p>Before.</p>\n<p>From fallback.</p>\n<p>Claim.</p>\n"


def test_chatgpt_adapter_runs_only_for_the_selected_ordinary_text_path(monkeypatch):
    calls = []
    format_display = presentation.format_chatgpt_display_markdown

    def record_format(value):
        calls.append(value)
        return format_display(value)

    monkeypatch.setattr(presentation, "format_chatgpt_display_markdown", record_format)

    render_cached_message(_BODY, "<p>Stored</p>", provider="chatgpt")
    assert calls == []

    render_cached_message(_BODY, "<script>discard()</script>", provider="chatgpt")
    assert calls == [_BODY]


def test_other_provider_keeps_chatgpt_component_text_literal():
    assert render_cached_message(_BODY, provider="grok") == (
        '<p>Before.</p>\n<p>&lt;box&gt;&lt;text&gt;From fallback.&lt;/text&gt;&lt;/box&gt;</p>\n'
        '<p>Claim &lt;Cite ref=&quot;turn0search1&quot;/&gt;.</p>\n'
    )


def test_rich_html_keeps_observable_provider_and_content_coercion():
    calls = []

    class Provider:
        def __str__(self):
            calls.append("provider")
            return " ChatGPT "

    class Content:
        def __bool__(self):
            calls.append("content truth value")
            return True

        def __str__(self):
            calls.append("content string")
            raise TypeError("synthetic content coercion error")

    with pytest.raises(TypeError, match="synthetic content coercion error"):
        render_cached_message(Content(), "<p>Stored</p>", provider=Provider())

    assert calls == ["provider", "content truth value", "content string"]


@pytest.mark.parametrize("prefix", ('&#', '&<Cite ref="turn0"/>#'))
def test_rich_html_preserves_chatgpt_numeric_reference_errors(prefix):
    minimum_limit = sys.int_info.str_digits_check_threshold
    content = f'<box><text>{prefix}{"9" * (minimum_limit + 1)};</text></box>'
    previous_limit = sys.get_int_max_str_digits()

    try:
        sys.set_int_max_str_digits(minimum_limit)
        with pytest.raises(ValueError, match="digits"):
            render_cached_message(content, "<p>Stored</p>", provider="chatgpt")
    finally:
        sys.set_int_max_str_digits(previous_limit)


@pytest.mark.parametrize("link", (
    "[link](https://example.invalid/\ud800\udc00)",
    "<https://example.invalid/\ud800\udc00>",
    "[link]: https://example.invalid/\ud800\udc00\n\n[link]",
))
def test_rich_html_preserves_chatgpt_markdown_link_encoding_errors(link):
    content = f"<box><text>From fallback.</text></box>\n\n{link}"

    with pytest.raises(UnicodeEncodeError):
        render_cached_message(content, "<p>Stored</p>", provider="chatgpt")
