"""Verify source-preserving, inert ChatGPT component display fallbacks.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from markdown_it import MarkdownIt
import pytest

from app.core.chatgpt_display import format_chatgpt_display_markdown


COMPONENT = '''<box border radius="xl" padding={3} gap={3}>
  <row align="center" justify="between">
    **Sample marker**
    <text color="secondary" size="xs">units</text>
  </row>
  <row align="end" justify="between">
    <title size="xl" color="default" tabularNums>2.8</title>
    <text color="secondary" size="xs">Reference: 0–3.4</text>
  </row>
  <box height="48px">
    <svg viewBox="0 0 320 48" width="100%" xmlns="http://www.w3.org/2000/svg">
      <rect x="4" y="29" width="312" height="9" rx="4.5" fill="#E5E7EB"/>
      <path d="M209 6 V41" stroke="#0055cc" strokeWidth="2.4"/>
      <circle cx="209" cy="33.5" r="4.5"/>
      <text x="4" y="47" fontSize="9">0</text>
      <text x="316" y="47" textAnchor="end">4.0</text>
    </svg>
  </box>
  <caption>Illustration only; retain the supplied wording.</caption>
</box>'''


def test_multiline_observed_component_keeps_rows_together_and_extracts_only_supplied_words():
    original = "Before.\n\n" + COMPONENT + '\n\nAfter. <Cite ref="turn390054search0"/>'
    formatted = format_chatgpt_display_markdown(original)
    assert formatted == (
        "Before.\n\n| **Sample marker** | units |\n| --- | --- |\n"
        "| 2.8 | Reference: 0–3.4 |\n\nDiagram labels: 0; 4.0\n\n"
        "Illustration only; retain the supplied wording.\n\nAfter."
    )
    assert original.endswith('<Cite ref="turn390054search0"/>')
    assert "strokeWidth" in original


def test_nested_boxes_and_svg_groups_keep_title_description_and_axis_text():
    original = (
        '<box><box><svg><title>Sample diagram</title><desc>Source description</desc>'
        '<g><text>0<tspan>.0</tspan></text><text>10</text></g></svg></box>'
        '<caption>Caption.</caption></box>'
    )
    assert format_chatgpt_display_markdown(original) == (
        "Diagram labels: Sample diagram; Source description; 0.0; 10\n\nCaption."
    )


@pytest.mark.parametrize("original", [
    "Ordinary **Markdown** and `code`  \r\n\r\n- item\r\n",
    '<div class="example"><span>Ordinary HTML text</span></div>',
    '<svg><text>Ordinary SVG source</text></svg>',
    'The literal <box> token is discussed in a sentence.',
    '<cite>Visible source attribution</cite>',
    '<cite ref="turn0">Visible source attribution</cite>',
    '<cite ref="turn0" title="Attribution"/>',
    '<cite ref="ordinary-reference"/>',
    '<a title=\'<Cite ref="turn0search1"/>\'>Literal attribute</a>',
    '<!-- <Cite ref="turn0search1"/> -->',
])
def test_unrelated_markup_and_normal_markdown_remain_byte_identical(original):
    assert format_chatgpt_display_markdown(original) == original


@pytest.mark.parametrize("original,expected", [
    ('Claim <Cite ref="turn0search1"/>.', 'Claim.'),
    ('Hello <cite ref="turn1"/>world', 'Hello world'),
    ('Hello<CITE ref="turn1"/>world', 'Hello world'),
    ("Hello<cite ref='turn12search3'> \n </CITE>world", 'Hello world'),
    ('甲<Cite ref="turn0search1"/>乙。', '甲乙。'),
    ('<Cite ref="turn0search1"/>', ''),
])
def test_only_empty_provider_citations_are_removed_without_joining_words(original, expected):
    assert format_chatgpt_display_markdown(original) == expected


@pytest.mark.parametrize("original", [
    '`<Cite ref="turn0search1"/>`',
    '``literal `<Cite ref="turn0search1"/>` source``',
    '\\``<Cite ref="turn0search1"/>`',
    '\\```<Cite ref="turn0search1"/>``',
    '```text\n' + COMPONENT + '\n<Cite ref="turn0search1"/>\n```\n',
    '~~~~text\n' + COMPONENT + '\n<Cite ref="turn0search1"/>\n~~~~\n',
    '    <Cite ref="turn0search1"/>\n    <box><text>Code</text></box>\n',
    '> ```text\n> <Cite ref="turn0search1"/>\n> ```\n',
    '- ```text\n  <Cite ref="turn0search1"/>\n  ```\n',
])
def test_inline_fenced_indented_quoted_and_list_code_are_byte_preserved(original):
    assert format_chatgpt_display_markdown(original) == original


def test_code_block_does_not_hide_the_following_component_or_citation():
    prefix = '```text\n<Cite ref="turn0search1"/>\n```\n'
    original = prefix + '<box><caption>Outside.</caption></box>\n<Cite ref="turn1"/>'
    assert format_chatgpt_display_markdown(original) == prefix + 'Outside.\n'


@pytest.mark.parametrize("component", [
    '<box><future-widget value={danger()}>Keep this.</future-widget></box>',
    '<box><script>unsafe()</script><caption>Keep this.</caption></box>',
    '<box><svg><foreignObject><div>Keep this.</div></foreignObject></svg></box>',
    '<box><row>Only one cell</row></box>',
    '<box><row><text>One</text><text>Two</text></box>',
    '<box><!-- Keep comment --><caption>Keep this.</caption></box>',
])
def test_unknown_or_mismatched_components_are_inert_source_and_keep_following_prose(component):
    formatted = format_chatgpt_display_markdown(component + '\n\nAfter **normal prose**.')
    assert component in formatted
    tokens = MarkdownIt('commonmark', {'html': False}).parse(formatted)
    fences = [token for token in tokens if token.type == 'fence']
    assert len(fences) == 1
    assert fences[0].content == component + '\n'
    assert tokens[-2].content == 'After **normal prose**.'


def test_unclosed_component_falls_back_before_the_next_plain_paragraph():
    component = '<box padding={3}>\n  <row><text>Keep this.</text></row>'
    formatted = format_chatgpt_display_markdown(component + '\n\nAfter **normal prose**.')
    tokens = MarkdownIt('commonmark', {'html': False}).parse(formatted)
    assert tokens[0].type == 'fence'
    assert tokens[0].content == component + '\n'
    assert tokens[-2].content == 'After **normal prose**.'


def test_fallback_fence_cannot_be_closed_by_backticks_restored_from_inline_code():
    component = '<box><future>````nested ``` backticks````</future></box>'
    formatted = format_chatgpt_display_markdown(component + '\n\nAfter.')
    tokens = MarkdownIt('commonmark', {'html': False}).parse(formatted)
    assert tokens[0].type == 'fence'
    assert tokens[0].markup == '`````'
    assert tokens[0].content == component + '\n'
    assert tokens[-2].content == 'After.'


def test_expressions_and_event_attributes_are_never_evaluated_or_emitted():
    component = '<box onClick={unsafe()}><caption>Source caption.</caption></box>'
    assert format_chatgpt_display_markdown(component) == 'Source caption.'


def test_later_declarations_do_not_invalidate_a_complete_component():
    original = '<box><caption>Caption.</caption></box>\n\n<!DOCTYPE example>\n<?example?>'
    assert format_chatgpt_display_markdown(original) == 'Caption.\n\n<!DOCTYPE example>\n<?example?>'


def test_malformed_later_declaration_keeps_the_complete_component_and_following_source():
    original = '<box><caption>Caption.</caption></box>\n\n<![invalid]>'
    assert format_chatgpt_display_markdown(original) == 'Caption.\n\n<![invalid]>'


def test_malformed_component_declaration_falls_back_to_its_complete_source():
    original = '<box><![invalid]></box>'
    formatted = format_chatgpt_display_markdown(original)
    assert MarkdownIt('commonmark').parse(formatted)[0].content == original + '\n'


def test_citation_scan_preserves_source_when_a_malformed_declaration_blocks_parsing():
    original = '<![invalid]>\n\nClaim <Cite ref="turn0search1"/>'
    assert format_chatgpt_display_markdown(original) == original


def test_deep_component_source_falls_back_without_recursive_render_failure():
    original = '<box>' * 80 + '<caption>Keep words.</caption>' + '</box>' * 80
    formatted = format_chatgpt_display_markdown(original)
    assert MarkdownIt('commonmark').parse(formatted)[0].content == original + '\n'


@pytest.mark.parametrize("code", ['`left | right`', '`first\nsecond`'])
def test_complex_protected_code_cannot_change_table_columns_or_rows(code):
    original = '<box><row><text>' + code + '</text><text>Value</text></row></box>'
    formatted = format_chatgpt_display_markdown(original)
    tokens = MarkdownIt('commonmark').parse(formatted)
    assert len(tokens) == 1
    assert tokens[0].type == 'fence'
    assert tokens[0].content == original + '\n'


@pytest.mark.parametrize("original", [
    COMPONENT, COMPONENT + '\n\n<Cite ref="turn0search1"/>',
    '<box><future>Keep source.</future></box>\n\nAfter.',
    '<box><row>Keep partial source.</row>\n\nAfter.',
])
def test_display_formatting_is_idempotent(original):
    once = format_chatgpt_display_markdown(original)
    assert format_chatgpt_display_markdown(once) == once
