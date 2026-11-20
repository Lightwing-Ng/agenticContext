"""Pin the exact output of the public Zhihu rich-text normalizer.

Code version: v1.0.0-codex.0
"""

import pytest

from app.core.zhihu_answers import ZhihuRichText, normalize_zhihu_rich_text


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            '<div class="RichContent-inner"><p>Outer</p>'
            '<div id="content"><p>Content</p><div class="RichText ztext"><p>Rich</p>'
            '<div itemprop="TEXT"><p>First text</p></div>'
            '<div itemprop="text"><p>Second text</p></div></div></div></div>',
            ZhihuRichText("<p>First text</p>", "First text", (), ()),
        ),
        (
            '<p>Before</p><div class="richcontent-inner"><p>Low</p></div>'
            '<div id="CONTENT"><p>Middle</p></div>'
            '<div class="ZTEXT RichText"><p>First rich</p></div>'
            '<div class="richtext ztext"><p>Second rich</p></div>',
            ZhihuRichText("<p>First rich</p>", "First rich", (), ()),
        ),
        (
            '<div class="RichContent-inner"><p>Lower rank</p></div>'
            '<div id="content"><p>Content</p></div>',
            ZhihuRichText("<p>Content</p>", "Content", (), ()),
        ),
        (
            '<p>Before</p><div class="RichContent-inner"><p>First inner</p></div>'
            '<div class="richcontent-inner"><p>Later inner</p></div>',
            ZhihuRichText("<p>First inner</p>", "First inner", (), ()),
        ),
        (
            '<div itemprop="text"/><div itemprop="text"><p>Second text</p></div>',
            ZhihuRichText("", "", (), ()),
        ),
        (
            '<div id="content"><p>Content</p></div>'
            '<button><span itemprop="text"><b>Selected body</b></span></button>',
            ZhihuRichText("<b>Selected body</b>", "**Selected body**", (), ()),
        ),
        (
            '<img itemprop="text" src="https://pic1.zhimg.com/chosen.jpg"/>'
            '<p>After void root</p><p>Second</p>',
            ZhihuRichText("<p>After void root</p><p>Second</p>", "After void root\n\nSecond", (), ()),
        ),
    ],
    ids=(
        "highest-rank-nested", "first-rich-root", "content-before-inner",
        "first-inner-root", "self-closing-root", "root-inside-skipped-parent", "void-root",
    ),
)
def test_body_root_selection_preserves_priority_and_first_match(source, expected):
    assert normalize_zhihu_rich_text(source) == expected


@pytest.mark.parametrize("source_length", (131_072, 131_073))
def test_large_answer_preserves_selected_root_and_entity_output(source_length):
    prefix = '<div id="content"><p>Lower rank</p></div><!--'
    suffix = '--><div itemprop="text"><p>First &amp; only</p></div><p>Outside</p>'
    comment = "中" * (source_length - len(prefix) - len(suffix))
    source = f" \x00{prefix}{comment}{suffix} "

    assert normalize_zhihu_rich_text(source) == ZhihuRichText(
        "<p>First &amp; only</p>", "First & only", (), (),
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            '<div itemprop="text"><p>  A&nbsp;<custom>  &amp; B</custom> '
            '<strong>&#x4E2D;&lt;tail&gt;</p>  C\\D<script><p>Hide</p></script></div><p>Outside</p>',
            ZhihuRichText(
                '<p>  A\xa0  &amp; B <strong>中&lt;tail&gt;</strong></p>  C\\D',
                'A & B **中\\<tail\\>**\n\n C\\\\D',
                (),
                (),
            ),
        ),
        (
            '<p>A&amp;<custom/>lt; B   <custom>  C</custom>\u3000D</p>',
            ZhihuRichText('<p>A&amp;lt; B     C\u3000D</p>', 'A&lt; B C D', (), ()),
        ),
        (
            '<div itemprop="text"><p>Body</p><ol start="²"><li>Malformed order</li></ol></div>',
            ZhihuRichText("", "", (), ()),
        ),
        (
            '<p>\x00A\ud800\u00a0B</p>',
            ZhihuRichText('<p>A\ud800\xa0B</p>', 'A\ud800 B', (), ()),
        ),
    ],
    ids=("misnested-tags", "adjacent-entity-and-text", "invalid-list-start", "nul-and-surrogate"),
)
def test_malformed_markup_and_entities_keep_exact_output(source, expected):
    assert normalize_zhihu_rich_text(source) == expected


def test_link_and_image_order_survives_escaping_and_deduplication():
    source = (
        '<a href="/entry?q=one&amp;p=two" title="&quot;Quoted&quot;">First</a>'
        '<figure><img data-original-token="same" data-original="//pic1.zhimg.com/b.jpg" '
        'src="https://pic1.zhimg.com/fallback.jpg" alt="B &amp; label">'
        '<img data-original-token="same" src="https://pic1.zhimg.com/ignored.jpg">'
        '<figcaption>Caption</figcaption></figure>'
        '<a href="https://pic1.zhimg.com/b.jpg">Same image</a>'
        '<img src="https://example.test/rejected.jpg" alt="No remote load">'
        '<a href="javascript:alert(1)">Unsafe text</a><a href="https://example.test/final">Last</a>'
        '<figure><img data-original-token="same" data-actualsrc="https://pic2.zhimg.com/a.jpg"></figure>'
    )

    assert normalize_zhihu_rich_text(source) == ZhihuRichText(
        content_html=(
            '<a href="https://www.zhihu.com/entry?q=one&amp;p=two" title="&quot;Quoted&quot;">First</a>'
            '<figure><img alt="B &amp; label" data-original="https://pic1.zhimg.com/b.jpg">'
            '<figcaption>Caption</figcaption></figure>'
            '<a href="https://pic1.zhimg.com/b.jpg">Same image</a>'
            '<img alt="No remote load">Unsafe text<a href="https://example.test/final">Last</a>'
            '<figure><img data-original="https://pic2.zhimg.com/a.jpg"></figure>'
        ),
        content_markdown=(
            '[First](<https://www.zhihu.com/entry?q=one&p=two>)\n\n'
            '[Image omitted from cached Zhihu answer](<https://pic1.zhimg.com/b.jpg>)\n'
            '*Caption*\n\n[Same image](<https://pic1.zhimg.com/b.jpg>)\n'
            '[Image omitted from cached Zhihu answer]\nUnsafe text[Last](<https://example.test/final>)\n\n'
            '[Image omitted from cached Zhihu answer](<https://pic2.zhimg.com/a.jpg>)'
        ),
        media_urls=("https://pic1.zhimg.com/b.jpg", "https://pic2.zhimg.com/a.jpg"),
        resource_urls=(
            "https://www.zhihu.com/entry?q=one&p=two", "https://pic1.zhimg.com/b.jpg",
            "https://example.test/final", "https://pic2.zhimg.com/a.jpg",
        ),
    )


def test_input_truthiness_precedes_string_conversion():
    calls = []

    class Value:
        def __init__(self, truthy):
            self.truthy = truthy

        def __bool__(self):
            calls.append("bool")
            return self.truthy

        def __str__(self):
            calls.append("str")
            return "  <p>A\x00 &amp; B</p>  "

    assert normalize_zhihu_rich_text(Value(False)) == ZhihuRichText("", "", (), ())
    assert normalize_zhihu_rich_text(Value(True)) == ZhihuRichText("<p>A &amp; B</p>", "A & B", (), ())
    assert calls == ["bool", "bool", "str"]


def test_string_subclass_does_not_require_a_length():
    class Source(str):
        def __bool__(self):
            return True

        def __len__(self):
            raise AssertionError("synthetic length failure")

        def replace(self, old, new, count=-1):
            return self

        def strip(self, chars=None):
            return self

    class Value:
        def __str__(self):
            return Source("<p>Body &amp; text</p>")

    assert normalize_zhihu_rich_text(Value()) == ZhihuRichText(
        "<p>Body &amp; text</p>", "Body & text", (), (),
    )


def test_string_conversion_errors_are_not_parser_fallbacks():
    class InvalidValue:
        def __str__(self):
            raise ValueError("synthetic conversion failure")

    with pytest.raises(ValueError, match="^synthetic conversion failure$"):
        normalize_zhihu_rich_text(InvalidValue())
