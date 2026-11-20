"""Pin display output when separate ChatGPT components share a message.

Code version: v1.0.0-codex.0
"""

import sys

import pytest

from app.core.chatgpt_display import format_chatgpt_display_markdown


def test_component_boundaries_keep_mixed_line_endings_and_escaped_cells():
    source = (
        "Header 🧭\r\n\r\n  <box>\r\n<caption>Alpha &amp; beta &#9731;.</caption>\r\n</box>"
        "\r\n\r\n`<box>` stays literal.\r\n\r\n"
        "<BOX>\n<row><text>left | side</text><text>right</text></row>\n</BOX>\r\n\r\nTail."
    )

    assert format_chatgpt_display_markdown(source) == (
        "Header 🧭\r\n\r\nAlpha & beta ☃.\r\n\r\n`<box>` stays literal.\r\n\r\n"
        "| left \\| side | right |\n| --- | --- |\r\n\r\nTail."
    )


def test_later_declaration_and_code_do_not_change_component_fallback_boundaries():
    source = (
        "<box><caption>Before.</caption></box>\n\n<![invalid]>\n\n"
        "```text\n<box><caption>Literal.</caption></box>\n```\n\n"
        "<box><future>Keep source.</future></box>\n\n<box><caption>After.</caption></box>"
    )

    assert format_chatgpt_display_markdown(source) == (
        "Before.\n\n<![invalid]>\n\n```text\n<box><caption>Literal.</caption></box>\n```\n\n"
        "```text\n<box><future>Keep source.</future></box>\n```\n\nAfter."
    )


def test_unclosed_component_after_a_complete_one_keeps_plain_prose():
    source = (
        "<box><caption>Start.</caption></box>\n\n"
        "<box><caption>Unclosed.\n\nKeep following **prose**."
    )

    assert format_chatgpt_display_markdown(source) == (
        "Start.\n\n```text\n<box><caption>Unclosed.\n```\n\nKeep following **prose**."
    )


def test_self_closing_component_keeps_legacy_delimiter_boundary():
    source = ' <box title="early > delimiter"/>\n\n<box><caption>After.</caption></box>'

    assert format_chatgpt_display_markdown(source) == (
        '```text\n <box title="early >\n``` delimiter"/>\n\nAfter.'
    )


@pytest.mark.parametrize("suffix", ("{}", '<span title="{}">Tail.</span>'))
def test_oversized_decimal_reference_after_a_complete_component_still_raises(suffix):
    previous_limit = sys.get_int_max_str_digits()
    limit = sys.int_info.str_digits_check_threshold
    reference = "&#" + "9" * (limit + 1) + ";"
    source = "<box><caption>Before.</caption></box>\n\n" + suffix.format(reference)

    try:
        sys.set_int_max_str_digits(limit)
        with pytest.raises(ValueError, match="Exceeds the limit"):
            format_chatgpt_display_markdown(source)
    finally:
        sys.set_int_max_str_digits(previous_limit)
