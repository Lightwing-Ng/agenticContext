"""Regression coverage for ChatGPT interactive-message tokens rendered as Markdown."""

# Code version: v1.0.0-claude.0

import hashlib
import json

import pytest

from app.core.chatgpt_downloader import _extract_chatgpt_conversation_messages
from app.core.chatgpt_markdown import normalize_chatgpt_markdown


def token(kind: str, payload: str | None = None) -> str:
    """Build a ChatGPT private-use token; payload fields may contain the field separator."""
    if payload is None:
        return f"\ue200{kind}\ue201"
    return f"\ue200{kind}\ue202{payload}\ue201"


def genui(widget: dict) -> str:
    return token("genui", json.dumps(widget, ensure_ascii=False))


def test_text_without_provider_tokens_is_returned_unchanged():
    text = "Line one  \r\nLine two with `code` and a [link](https://example.com).\n\n- item"
    assert normalize_chatgpt_markdown(text) == text


def test_citations_and_highlight_markers_leave_only_the_readable_sentence():
    text = f"\ue203Prices fell 60%\ue204 {token('cite', 'turn0search15')}。\ue206\n{token('filecite', 'turn0file0')}\nDone"
    assert normalize_chatgpt_markdown(text) == "Prices fell 60%。\n\nDone"


def test_inline_entity_keeps_its_name_inside_the_sentence():
    entity = token("entity", json.dumps(["place", "Sample Airport", "international gateway"]))
    assert normalize_chatgpt_markdown(f"**{entity}（TLV）** is near.") == "**Sample Airport（TLV）** is near."


def test_standalone_entity_becomes_a_bold_line_and_its_metadata_card_is_dropped():
    text = (
        "Visit:\n"
        + token("entity", json.dumps(["turn1business0", "Sample Lookout"])) + "\n"
        + token("entity_metadata", json.dumps(["turn1business0", "one-line", "Sample Lookout"])) + "\n\n"
        + "It opens at 22:30."
    )
    assert normalize_chatgpt_markdown(text) == "Visit:\n\n**Sample Lookout**\n\nIt opens at 22:30."


def test_titled_urls_link_only_plain_http_addresses():
    linked = normalize_chatgpt_markdown(f"See {token('url', 'Example Portal\ue202https://example.com/claim')}.")
    unsafe = normalize_chatgpt_markdown(token("url", "Unsafe\ue202javascript:alert(1)"))
    assert linked == "See [Example Portal](https://example.com/claim)."
    assert unsafe == "Unsafe"


def test_map_widget_lists_named_points_with_category_and_address_and_keeps_the_answer():
    widget = {"map_widget": {
        "points": [
            {"ref": "turn0business0", "name": "Sample Hotel", "category": "Lodging", "address": "1 Example Road"},
            {"ref": "turn0business1"},
        ],
        "answer": "Search around the central station.",
    }}
    rendered = normalize_chatgpt_markdown(f"Intro.\n{genui(widget)}")
    assert rendered == (
        "Intro.\n\n*Map*\n\n- **Sample Hotel** (Lodging) — 1 Example Road\n\nSearch around the central station."
    )
    assert "map_widget" not in rendered


def test_chart_widget_becomes_a_markdown_table_with_units():
    widget = {"charts_widget_v2": {"content": {
        "chartType": "line",
        "meta": {"title": "Sample deposits", "description": "Synthetic values"},
        "xKey": "year",
        "xAxisLabel": "Year",
        "series": [{"dataKey": "deposit", "label": "Deposits", "axisLabel": "Units"}],
        "data": [{"year": "2010", "deposit": 3149}, {"year": "2011", "deposit": 3500}],
    }}}
    assert normalize_chatgpt_markdown(genui(widget)) == (
        "**Sample deposits**\n\nSynthetic values\n\n"
        "| Year | Deposits (Units) |\n| --- | --- |\n| 2010 | 3149 |\n| 2011 | 3500 |"
    )


@pytest.mark.parametrize(("widget", "expected"), [
    (
        {"math_block_widget_always_prefetch_v2": {"content": "y=1-e^{-0.8x}"}},
        "```latex\ny=1-e^{-0.8x}\n```",
    ),
    (
        {"pronunciation_block_widget_v4_audio_fix_v2": {"content": "NDX", "pronunciation_hint": "en dee ex"}},
        "**NDX** (en dee ex)",
    ),
    (
        {"ask_user_input": {"questions": [{"question": "Which time?", "options": ["09:00", "08:00"]}]}},
        "**Which time?**\n- 09:00\n- 08:00",
    ),
    (
        {"suggest_automation": {"label": "Review in 30 days"}},
        "*Suggested automation: Review in 30 days*",
    ),
])
def test_text_bearing_widgets_keep_their_words(widget, expected):
    assert normalize_chatgpt_markdown(genui(widget)) == expected


def test_citation_pointers_and_malformed_payloads_add_no_text():
    assert normalize_chatgpt_markdown(genui({"citation": {"ref": "turn0view1"}})) == ""
    assert normalize_chatgpt_markdown(genui({"refs": ["turn35search2"]})) == ""
    assert normalize_chatgpt_markdown(token("genui", "eFbD")) == ""
    assert normalize_chatgpt_markdown(token("memcite")) == ""


def test_unknown_widgets_are_preserved_as_a_fenced_json_block():
    rendered = normalize_chatgpt_markdown(genui({"future_widget": {"title": "Sample", "rows": [1]}}))
    assert rendered.startswith("```json\n")
    assert rendered.endswith("\n```")
    body = rendered.removeprefix("```json\n").removesuffix("\n```")
    assert json.loads(body) == {"future_widget": {"title": "Sample", "rows": [1]}}


def test_titled_urls_keep_parentheses_and_encode_trailing_backslashes():
    wikipedia = "https://en.wikipedia.org/wiki/Python_(programming_language)"
    assert normalize_chatgpt_markdown(token("url", f"Python\ue202{wikipedia}")) == (
        f"[Python](<{wikipedia}>)"
    )
    assert normalize_chatgpt_markdown(token("url", "Ref\ue202https://example.com/a\\")) == (
        "[Ref](https://example.com/a%5C)"
    )


def test_map_category_is_not_repeated_when_the_name_already_contains_it():
    widget = {"map_widget": {"points": [
        {"name": "Hanoi Train Street", "category": "Train Street"},
        {"name": "Barcelona Tapas", "category": "Bar"},
    ]}}
    assert normalize_chatgpt_markdown(genui(widget)) == (
        "*Map*\n\n- **Hanoi Train Street**\n- **Barcelona Tapas** (Bar)"
    )


def test_dropped_markers_keep_the_space_that_separates_words_and_drop_it_before_punctuation():
    assert normalize_chatgpt_markdown(f"Hello {genui({'citation': {}})}world") == "Hello world"
    assert normalize_chatgpt_markdown(f"亲民 {token('cite', 'turn0search1')}。") == "亲民。"


def test_a_block_widget_inside_a_sentence_does_not_leave_a_leading_space():
    widget = token("navlist", "Recent items\ue202turn1news1")
    assert normalize_chatgpt_markdown(f"The top items {widget} are listed.") == (
        "The top items\n\n**Recent items**\n\nare listed."
    )


def test_widget_with_a_changed_inner_shape_keeps_its_text_as_json():
    widget = {"ask_user_input": {"items": [{"prompt": "Which time?", "choices": ["09:00"]}]}}
    rendered = normalize_chatgpt_markdown(genui(widget))
    assert rendered.startswith("```json\n")
    assert json.loads(rendered.removeprefix("```json\n").removesuffix("\n```")) == widget


def test_truncated_token_ends_at_its_line_and_leaves_the_following_prose():
    text = "结论如下。" + "\ue200cite\ue202turn0search4\ue202turn0news18" + "\n\n后续正文。"
    assert normalize_chatgpt_markdown(text) == "结论如下。\n\n后续正文。"


def test_lone_surrogates_are_replaced_so_the_message_can_be_hashed():
    text = normalize_chatgpt_markdown(token("entity", json.dumps(["place", "Bad\ud800Name"])) + " today.")
    assert text == "Bad�Name today."
    text.encode("utf-8")


def test_message_made_only_of_provider_markers_has_no_readable_text():
    assert normalize_chatgpt_markdown(f"{token('cite', 'turn0search1')}{token('map')}") == ""


def test_normalization_is_idempotent_for_every_supported_token():
    text = "\n".join([
        f"\ue203Claim{token('cite', 'turn0search1')}\ue204",
        token("entity", json.dumps(["company", "Sample Co"])),
        token("url", "Portal\ue202https://example.com/x"),
        genui({"map_widget": {"points": [{"name": "A"}], "answer": "B"}}),
        token("navlist", "Recent items\ue202turn1news1"),
        token("image_group", json.dumps({"query": ["Sample harbor"]})),
    ])
    once = normalize_chatgpt_markdown(text)
    assert normalize_chatgpt_markdown(once) == once


def test_synchronization_stores_the_rendered_markdown_and_hashes_it():
    message = {
        "author": {"role": "assistant"},
        "content": {"content_type": "text", "parts": [f"Rates rose {token('cite', 'turn0search2')}."]},
        "status": "finished_successfully",
    }
    marker = {
        "author": {"role": "assistant"},
        "content": {"content_type": "text", "parts": [token("cite", "turn0search3")]},
        "status": "finished_successfully",
    }
    payload = {"title": "Sample", "mapping": {"answer": {"message": message}, "marker": {"message": marker}}}
    rows = _extract_chatgpt_conversation_messages(payload, "https://chatgpt.com/c/sample", "2026-10-01T00:00:00Z")
    assert [row["content_text"] for row in rows] == ["Rates rose."]
    assert rows[0]["content_sha256"] == hashlib.sha256("Rates rose.".encode("utf-8")).hexdigest()
