"""Exercise Zhihu body selection in tag-dense source documents.

Code version: v1.0.0-codex.0
"""

import pytest

from app.core.zhihu_answers import ZhihuRichText, normalize_zhihu_rich_text


@pytest.mark.parametrize("filler_tags", (4_080, 4_081, 24_000))
def test_tag_dense_source_still_selects_late_higher_priority_body(filler_tags):
    source = (
        '<div id="content"><p>Discard <a href="/old">Old</a></p></div>'
        + "<x/>" * filler_tags
        + '<section itemprop="text"><p>Keep <a href="/new">New</a></p></section>'
    )

    assert normalize_zhihu_rich_text(source) == ZhihuRichText(
        '<p>Keep <a href="https://www.zhihu.com/new">New</a></p>',
        'Keep [New](<https://www.zhihu.com/new>)',
        (),
        ("https://www.zhihu.com/new",),
    )


def test_tag_dense_source_keeps_empty_result_on_markdown_conversion_error():
    source = (
        '<div itemprop="text"><p>Body</p>'
        + "<x/>" * 4_096
        + '<ol start="²"><li>Invalid list start</li></ol></div>'
    )

    assert normalize_zhihu_rich_text(source) == ZhihuRichText("", "", (), ())
