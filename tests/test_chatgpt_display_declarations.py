"""Preserve uncertain declaration source across tolerant HTML parser runtimes.

Code version: v1.0.0-codex.0
"""

import pytest

from app.core.chatgpt_display import format_chatgpt_display_markdown


@pytest.mark.parametrize("source", [
    'Before <Cite ref="turn0search1"/>\n<![invalid]>\nAfter <Cite ref="turn0search2"/>',
    'Before 😀\r\n<![broken]>\r\nClaim <Cite ref="turn0search1"/>  \r\n',
    '  <![unsupported]>\n\nClaim <Cite ref="turn0search1"/>',
])
def test_malformed_declarations_preserve_all_citations_and_exact_source_bytes(source):
    assert format_chatgpt_display_markdown(source) == source
    assert format_chatgpt_display_markdown(format_chatgpt_display_markdown(source)) == source


@pytest.mark.parametrize("prefix", [
    '<!-- <![invalid]> -->\n',
    '<a title="<![invalid]>">Source attribute</a>\n',
    '`<![invalid]>`\n',
    '```text\n<![invalid]>\n```\n',
    '<!DOCTYPE html>\n',
    '<![CDATA[<Cite ref="turn0search1"/>]]>\n',
])
def test_literal_or_supported_declarations_do_not_block_unrelated_citation_cleanup(prefix):
    assert format_chatgpt_display_markdown(prefix + 'Claim <Cite ref="turn0search2"/>') == prefix + "Claim"
