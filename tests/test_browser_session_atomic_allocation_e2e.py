"""Session allocations and header reservation change in one rendered frame.

Code version: v1.0.0-codex.0
"""

import pytest

from tests import test_browser_session_columns_e2e as session_columns


disposable_browser = session_columns.disposable_browser
session_column_browser_server_url = session_columns.session_column_browser_server_url


@pytest.mark.parametrize("source", ["chatgpt", "zhihu"])
def test_reduced_motion_container_allocation_and_header_offset_commit_together(
    disposable_browser, session_column_browser_server_url, source
):
    """Sample real controller updates before a later frame can hide interpolation."""
    context = disposable_browser.new_context(
        viewport={"width": 390, "height": 844},
        has_touch=True,
        is_mobile=True,
        reduced_motion="reduce",
    )
    try:
        page = context.new_page()
        page.goto(
            session_column_browser_server_url
            + f"/browser?view=text&source={source}&session_view=1",
            wait_until="networkidle",
        )
        page.evaluate("""async () => {
            await document.fonts.ready;
            const shell = document.querySelector('.browser-session-index-shell');
            const header = shell.querySelector('[data-table-header]');
            const body = shell.querySelector('[data-table-body]');
            window.sessionAllocationSamples = [];
            window.sessionAllocationObserver = new MutationObserver(() => {
                const headerBox = header.getBoundingClientRect();
                const cells = [...body.rows[0].cells];
                const style = getComputedStyle(body);
                window.sessionAllocationSamples.push({
                    bodyWidth: body.getBoundingClientRect().width,
                    bodyTop: body.getBoundingClientRect().top,
                    firstCellTop: cells[0].getBoundingClientRect().top,
                    headerBottom: headerBox.bottom,
                    reservedHeaderBottom: headerBox.top + header.offsetHeight,
                    margin: style.marginTop,
                    tokens: ['number', 'title', 'source', 'count', 'updated'].map(name =>
                        style.getPropertyValue(`--browser-session-${name}-width`)),
                    cells: cells.map(cell => cell.getBoundingClientRect().width),
                });
            });
            window.sessionAllocationObserver.observe(header, {
                attributes: true, subtree: true, attributeFilter: ['style'],
            });
        }""")
        for width in (1006, 700, 390, 1006):
            page.set_viewport_size({"width": width, "height": 791})
            page.evaluate("""() => new Promise(resolve =>
                requestAnimationFrame(() => requestAnimationFrame(resolve)))""")
        samples = page.evaluate("""() => {
            window.sessionAllocationObserver.disconnect();
            return window.sessionAllocationSamples;
        }""")
        assert len({round(sample["bodyWidth"]) for sample in samples}) >= 3, samples
        for sample in samples:
            width = sample["bodyWidth"]
            # These allocations are the documented product contract, independent of CSS tokens.
            if width > 800:
                allocation = (4, 66, 10, 20) if source == "zhihu" else (4, 56, 10, 10, 20)
            elif width > 500:
                allocation = (5, 60, 15, 20) if source == "zhihu" else (5, 48, 12, 15, 20)
            else:
                allocation = (8, 50, 18, 24) if source == "zhihu" else (8, 28, 18, 22, 24)
            assert sample["cells"] == pytest.approx(
                [width * percentage / 100 for percentage in allocation], abs=1
            ), sample
            assert sample["bodyTop"] == pytest.approx(
                sample["reservedHeaderBottom"], abs=1
            ), sample
            assert sample["firstCellTop"] == pytest.approx(
                sample["headerBottom"], abs=1
            ), sample
    finally:
        context.close()
