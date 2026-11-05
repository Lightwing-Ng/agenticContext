"""Contact statements fit their scrollport and keep shared header alignment.

Code version: v1.2.2-codex.0
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import PurePosixPath
import re
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from app.core.wechat_contact_analysis import query_contact_analysis
from tests import test_wechat_contact_analysis_e2e as fixtures


disposable_browser = fixtures.disposable_browser
contact_analysis_server = fixtures.contact_analysis_server
LONG_STATEMENT = (
    "A complete source statement keeps its original wording and remains readable. "
    "完整保留原文并在当前列内自然换行。 "
    "https://example.test/" + "unbrokentext" * 12
)
LONG_CONVERSATION = "A conversation with a long title 需要保留的完整群聊名称"
LONG_ACCOUNT = "Research account with a complete source identity · abc123"


def _static_resource_name(url):
    path = urlsplit(url).path
    return PurePosixPath(path).name[:128] if path.startswith("/static/") else ""


def _stylesheet_diagnostic(response):
    headers = response.headers
    length = headers.get("content-length", "")
    declared = int(length) if length.isdecimal() and len(length) <= 10 else None
    result = {
        "resource": _static_resource_name(response.url),
        "status": response.status,
        "mime": headers.get("content-type", "").split(";", 1)[0][:80],
        "declared_bytes": declared,
    }
    if declared is not None and 0 <= declared <= 1024 * 1024:
        try:
            body = response.body()
        except Exception as error:
            result["body_error_type"] = type(error).__name__
        else:
            result["bytes"] = len(body)
            if len(body) <= 1024 * 1024:
                result["sha256"] = hashlib.sha256(body).hexdigest()
            else:
                result["body_over_budget"] = True
    else:
        result["body_capture"] = "unknown_or_excessive_declared_size"
    return result


@contextmanager
def _observe_contact_table_failure(page):
    """Keep bounded static evidence and inspect bodies only after a failed assertion."""
    stylesheets, failed_requests, console_codes = [], [], []

    def response_received(response):
        name = _static_resource_name(response.url)
        if name.endswith(".css") and len(stylesheets) < 16:
            stylesheets.append(response)

    def request_failed(request):
        name = _static_resource_name(request.url)
        if name and len(failed_requests) < 16:
            failed_requests.append({
                "resource": name,
                "codes": re.findall(r"\bERR_[A-Z0-9_]+\b", str(request.failure))[:8],
            })

    def console_error(message):
        name = _static_resource_name(message.location.get("url", ""))
        if message.type == "error" and name and len(console_codes) < 16:
            console_codes.append({
                "resource": name,
                "codes": re.findall(r"\bERR_[A-Z0-9_]+\b", message.text)[:8],
            })

    page.on("response", response_received)
    page.on("requestfailed", request_failed)
    page.on("console", console_error)
    try:
        yield
    except AssertionError:
        try:
            state = page.evaluate("""() => {
                const properties = ['number', 'time', 'statement', 'conversation', 'account'];
                const tables = [...document.querySelectorAll('.wechat-contact-table')].slice(0, 2).map(table => {
                    const style = getComputedStyle(table);
                    return {
                        role: table.hasAttribute('data-table-body') ? 'body' : 'header',
                        layout: style.tableLayout, width: style.width,
                        variables: properties.map(name => style.getPropertyValue('--wechat-contact-' + name + '-width').trim().slice(0, 32)),
                        columns: [...table.querySelectorAll('col')].slice(0, 5).map(col => ({
                            inline: col.style.width.slice(0, 40), computed: getComputedStyle(col).width,
                        })),
                        cells: [...(table.rows[0]?.cells || [])].slice(0, 5).map(cell => ({
                            inline: cell.style.width.slice(0, 40), computed: getComputedStyle(cell).width,
                        })),
                    };
                });
                const links = [...document.querySelectorAll('link[rel=stylesheet]')].filter(link => {
                    return new URL(link.href).pathname.startsWith('/static/');
                }).slice(0, 8).map(link => {
                    let rules = null;
                    try { rules = link.sheet?.cssRules.length ?? null; } catch {}
                    return {
                        resource: new URL(link.href).pathname.split('/').pop().slice(0, 128),
                        sheet: Boolean(link.sheet), rules, disabled: link.disabled,
                        media: link.media.slice(0, 80),
                    };
                });
                return {readyState: document.readyState, fonts: document.fonts.status, tables, links};
            }""")
            diagnostics = {
                "state": state,
                "stylesheets": [_stylesheet_diagnostic(response) for response in stylesheets],
                "failed_static_requests": failed_requests,
                "static_console_codes": console_codes,
            }
            print("Contact table style failure: " + json.dumps(diagnostics, separators=(",", ":")))
        except Exception as error:
            print("Contact table style diagnostic unavailable: " + type(error).__name__)
        raise
    finally:
        page.remove_listener("response", response_received)
        page.remove_listener("requestfailed", request_failed)
        page.remove_listener("console", console_error)


def _responsive_results(fixture):
    original = query_contact_analysis(
        fixture.store, fixture.target_id, background=False
    )
    assert original["status"] == "ready" and len(original["items"]) == 50

    def results(*_args, **kwargs):
        result = deepcopy(original)
        result.update(
            total_count=80502,
            total_pages=1611,
            current_page=int(kwargs.get("page", 1)),
        )
        result["items"][1].update(
            content_text=LONG_STATEMENT,
            conversation_title=LONG_CONVERSATION,
            viewer_account_label=LONG_ACCOUNT,
        )
        if result["current_page"] == 1611:
            result["items"] = result["items"][:2]
            for item, timestamp in zip(
                result["items"],
                ("5 Apr 2019 19:40:17", "5 Apr 2019 19:38:52"),
            ):
                item["timestamp_label"] = timestamp
        # The real oldest-first query assigns authoritative ordinals to each row.
        offset = (result["current_page"] - 1) * result["page_size"]
        for position, item in enumerate(result["items"], offset + 1):
            item["sequence_number"] = position
        return result

    return results


def _table_geometry(page):
    return page.locator(".wechat-contact-table-shell").evaluate(
        """shell => {
        const port = shell.querySelector('[data-table-scroll]');
        const header = shell.querySelector('[data-table-header]');
        const body = shell.querySelector('[data-table-body]');
        const firstRow = body.rows[0];
        const bounds = node => {
            const box = node.getBoundingClientRect();
            return {left: box.left, right: box.right, width: box.width,
                top: box.top, height: box.height};
        };
        function textMetrics(cell, node) {
            const range = document.createRange();
            range.selectNodeContents(node);
            const rects = [...range.getClientRects()].filter(rect => rect.width && rect.height);
            const lineTops = [...new Set(rects.map(rect => Math.round(rect.top * 100) / 100))];
            const cellStyle = getComputedStyle(cell);
            const textStyle = getComputedStyle(node);
            const probe = document.createElement('span');
            probe.textContent = node.textContent;
            Object.assign(probe.style, {
                position: 'fixed', left: '-100000px', visibility: 'hidden',
                whiteSpace: 'pre', width: 'max-content', font: textStyle.font,
                fontFamily: textStyle.fontFamily, fontSize: textStyle.fontSize,
                fontWeight: textStyle.fontWeight, fontStretch: textStyle.fontStretch,
                fontStyle: textStyle.fontStyle, letterSpacing: textStyle.letterSpacing,
                fontFeatureSettings: textStyle.fontFeatureSettings,
                fontVariationSettings: textStyle.fontVariationSettings,
            });
            document.body.append(probe);
            const intrinsicWidth = probe.getBoundingClientRect().width;
            probe.remove();
            const contentWidth = cell.getBoundingClientRect().width
                - parseFloat(cellStyle.paddingLeft) - parseFloat(cellStyle.paddingRight)
                - parseFloat(cellStyle.borderLeftWidth) - parseFloat(cellStyle.borderRightWidth);
            return {lineCount: lineTops.length, intrinsicWidth, contentWidth,
                intrinsicFits: intrinsicWidth <= contentWidth + 0.5};
        }
        const numberIndexes = [...new Set([0, Math.min(9, body.rows.length - 1), body.rows.length - 1])];
        const numbers = numberIndexes.map(index => {
            const cell = body.rows[index].cells[0];
            const range = document.createRange();
            range.selectNodeContents(cell);
            const cellBox = cell.getBoundingClientRect();
            const textBox = range.getBoundingClientRect();
            return {text: cell.textContent.trim(), align: getComputedStyle(cell).textAlign,
                ...textMetrics(cell, cell),
                renderedFits: textBox.left >= cellBox.left - 1
                    && textBox.right <= cellBox.right + 1,
                centerError: Math.abs((cellBox.left + cellBox.right
                    - textBox.left - textBox.right) / 2)};
        });
        const times = [...body.rows].slice(0, 2).map(row => {
            const cell = row.cells[1];
            const time = cell.querySelector('time');
            return {text: time.textContent.trim(), ...textMetrics(cell, time)};
        });
        const longCells = [...body.rows[1].cells].slice(1).map(cell => {
            const range = document.createRange();
            range.selectNodeContents(cell);
            const box = cell.getBoundingClientRect();
            const textBox = range.getBoundingClientRect();
            return {width: box.width, textWidth: textBox.width,
                textFits: textBox.left >= box.left - 1 && textBox.right <= box.right + 1,
                lines: range.getClientRects().length, overflowX: getComputedStyle(cell).overflowX,
                textOverflow: getComputedStyle(cell).textOverflow};
        });
        return {viewport: innerWidth, documentOverflow: document.documentElement.scrollWidth - innerWidth,
            port: bounds(port), portOverflow: port.scrollWidth - port.clientWidth,
            scrollbarWidth: port.offsetWidth - port.clientWidth,
            body: bounds(body), header: bounds(header),
            columns: [...firstRow.cells].map(bounds),
            headers: [...header.rows[0].cells].map(bounds), numbers, times, longCells};
    }"""
    )


def _assert_table_fits(geometry):
    assert geometry["documentOverflow"] <= 1, geometry
    assert geometry["portOverflow"] <= 1, geometry
    assert geometry["body"]["left"] >= geometry["port"]["left"] - 1, geometry
    assert geometry["body"]["right"] <= geometry["port"]["right"] + 1, geometry
    assert len(geometry["columns"]) == len(geometry["headers"]) == 5, geometry
    percentages = []
    for column, heading in zip(geometry["columns"], geometry["headers"]):
        assert column["width"] > 0, geometry
        assert abs(column["left"] - heading["left"]) <= 1, geometry
        assert abs(column["width"] - heading["width"]) <= 1, geometry
        ratio = column["width"] / geometry["body"]["width"] * 100
        percentage = round(ratio)
        assert abs(ratio - percentage) <= 0.15, geometry
        assert percentage % 2 == 0 or percentage % 5 == 0, geometry
        percentages.append(percentage)
    assert sum(percentages) == 100, geometry
    assert percentages == [10, 20, 40, 15, 15], geometry
    assert all(
        number["align"] == "center"
        and number["centerError"] <= 1
        and number["renderedFits"]
        for number in geometry["numbers"]
    ), geometry
    assert all(
        number["lineCount"] == 1
        for number in geometry["numbers"]
        if number["intrinsicFits"]
    ), geometry
    assert all(cell["textFits"] for cell in geometry["longCells"]), geometry
    assert all(cell["textOverflow"] != "ellipsis" for cell in geometry["longCells"])
    assert geometry["longCells"][1]["lines"] > 1, geometry


def _wait_for_table_alignment(page):
    page.evaluate(
        """() => new Promise(resolve => requestAnimationFrame(
            () => requestAnimationFrame(resolve)))"""
    )
    page.wait_for_function(
        """() => {
        const header = document.querySelector('[data-table-header]');
        const body = document.querySelector('[data-table-body]');
        return [...body.rows[0].cells].every((cell, index) => {
            const source = cell.getBoundingClientRect();
            const target = header.rows[0].cells[index].getBoundingClientRect();
            return Math.abs(source.left - target.left) <= 1
                && Math.abs(source.width - target.width) <= 1;
        });
    }""",
        timeout=5000,
    )


@pytest.mark.parametrize("width", [831, 700, 390, 1280])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_contact_columns_fit_without_horizontal_scroll_and_keep_centered_numbers(
    disposable_browser, contact_analysis_server, tmp_path, width, color_scheme
):
    fixture = contact_analysis_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1307},
        reduced_motion="reduce",
        color_scheme=color_scheme,
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    measurements = []
    url = fixture.url + "/browser/wechat/contacts/" + fixture.target_id
    try:
        with _observe_contact_table_failure(page), patch(
            "app.core.wechat_contact_analysis.query_contact_analysis",
            side_effect=_responsive_results(fixture),
        ):
            for number_page in (1, 1610):
                page.goto(url + f"?page={number_page}", wait_until="networkidle")
                page.evaluate("() => document.fonts.ready")
                rows = page.locator("[data-table-body] tbody tr")
                expect(rows).to_have_count(50)
                expect(rows.nth(1).locator("td").nth(2)).to_have_text(LONG_STATEMENT)
                expect(rows.nth(1).locator("td").nth(3)).to_contain_text(
                    LONG_CONVERSATION
                )
                expect(rows.nth(1).locator("td").nth(4)).to_contain_text(LONG_ACCOUNT)
                measured = _table_geometry(page)
                measurements.append(measured)
                (tmp_path / "table-geometry.json").write_text(
                    json.dumps(measurements, indent=2) + "\n"
                )
                _assert_table_fits(measured)
            page.screenshot(path=tmp_path / f"contact-table-{width}-{color_scheme}.png")
            page.set_viewport_size(
                {"width": 831 if width != 831 else 700, "height": 1307}
            )
            _wait_for_table_alignment(page)
            resized = _table_geometry(page)
            measurements.append(resized)
            _assert_table_fits(resized)
            (tmp_path / "table-geometry.json").write_text(
                json.dumps(measurements, indent=2) + "\n"
            )
        assert not errors
        assert fixture.before == (
            fixture.history.read_bytes(),
            fixture.registry.read_bytes(),
        )
    finally:
        context.close()


@pytest.mark.parametrize(("width", "height"), [(855, 1247), (831, 1307), (700, 1247)])
def test_contact_last_page_keeps_numbers_and_timestamps_on_one_line_when_they_fit(
    disposable_browser, contact_analysis_server, tmp_path, width, height
):
    fixture = contact_analysis_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": height}, reduced_motion="reduce"
    )
    page = context.new_page()
    url = fixture.url + "/browser/wechat/contacts/" + fixture.target_id + "?page=1611"
    try:
        with patch(
            "app.core.wechat_contact_analysis.query_contact_analysis",
            side_effect=_responsive_results(fixture),
        ):
            page.goto(url, wait_until="networkidle")
            page.evaluate("() => document.fonts.ready")
            _wait_for_table_alignment(page)
            rows = page.locator("[data-table-body] tbody tr")
            expect(rows).to_have_count(2)
            measured = _table_geometry(page)
            (tmp_path / "last-page-geometry.json").write_text(
                json.dumps(measured, indent=2) + "\n"
            )
            page.screenshot(path=tmp_path / f"contact-last-page-{width}.png")
            if width == 855:
                assert measured["body"]["width"] > 800, measured
            elif width == 831:
                assert measured["body"]["width"] <= 800, measured
            assert [number["text"] for number in measured["numbers"]] == [
                "80,501",
                "80,502",
            ]
            assert [time["text"] for time in measured["times"]] == [
                "5 Apr 2019 19:40:17",
                "5 Apr 2019 19:38:52",
            ]
            assert all(
                number["lineCount"] == 1 and number["intrinsicFits"]
                for number in measured["numbers"]
            ), measured
            if width >= 831:
                assert all(
                    time["lineCount"] == 1 and time["intrinsicFits"]
                    for time in measured["times"]
                ), measured
            _assert_table_fits(measured)
        assert fixture.before == (
            fixture.history.read_bytes(),
            fixture.registry.read_bytes(),
        )
    finally:
        context.close()


@pytest.mark.parametrize("width", [831, 700])
def test_contact_header_tracks_body_with_a_classic_scrollbar_gutter(
    disposable_browser, contact_analysis_server, tmp_path, width
):
    fixture = contact_analysis_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1307}, reduced_motion="reduce"
    )
    page = context.new_page()
    measurements = []
    url = fixture.url + "/browser/wechat/contacts/" + fixture.target_id
    try:
        with patch(
            "app.core.wechat_contact_analysis.query_contact_analysis",
            side_effect=_responsive_results(fixture),
        ):
            for number_page in (1, 1610):
                page.goto(url + f"?page={number_page}", wait_until="networkidle")
                page.evaluate("() => document.fonts.ready")
                # A styled scrollbar allocates a real classic gutter in Chromium.
                page.add_style_tag(
                    content="""
                    .wechat-contact-table-scroll {
                        overflow-y: scroll;
                        scrollbar-gutter: stable;
                        scrollbar-width: auto;
                    }
                    .wechat-contact-table-scroll::-webkit-scrollbar { width: 15px; }
                    """
                )
                page.wait_for_function(
                    """() => {
                    const port = document.querySelector('[data-table-scroll]');
                    return port.offsetWidth - port.clientWidth === 15;
                }""",
                    timeout=5000,
                )
                _wait_for_table_alignment(page)
                measured = _table_geometry(page)
                measurements.append(measured)
                (tmp_path / "classic-gutter-geometry.json").write_text(
                    json.dumps(measurements, indent=2) + "\n"
                )
                assert measured["scrollbarWidth"] == 15, measured
                assert measured["body"]["height"] > measured["port"]["height"]
                _assert_table_fits(measured)
            page.screenshot(path=tmp_path / f"contact-classic-gutter-{width}.png")
        assert fixture.before == (
            fixture.history.read_bytes(),
            fixture.registry.read_bytes(),
        )
    finally:
        context.close()
