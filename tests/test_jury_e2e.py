"""Rendered Jury readiness, review evidence, and one-question session behavior.

Code version: v1.3.6-codex.0
"""

from copy import deepcopy
from pathlib import Path
import re

import pytest
from playwright.sync_api import expect, sync_playwright

from tests import test_sidebar_e2e as fixtures

sidebar_server_url = fixtures.sidebar_server_url


@pytest.fixture(scope="module")
def jury_browser():
    """Use isolated Edge on the requested host without opening its signed-in profile."""
    with sync_playwright() as playwright:
        if Path("/Applications/Microsoft Edge.app").exists():
            browser = playwright.chromium.launch(channel="msedge", headless=True)
        else:
            browser = fixtures._launch_disposable_browser(playwright)
        yield browser
        browser.close()


def account_check(route, calls, *, unavailable=()):
    payload = route.request.post_data_json
    calls.append(payload)
    records = [
        {
            "key": key,
            "label": {"chatgpt": "ChatGPT", "grok": "Grok", "gemini": "Gemini", "claude": "Claude"}[key],
            "ready": key not in unavailable,
            "message": "Signed in" if key not in unavailable else "Provider unavailable",
            "model": {
                "chatgpt-latest-extra-high": "Latest · Extra High",
                "grok-auto": "Auto",
                "gemini-3.1-pro": "3.1 Pro",
                "gemini-3.8-flash": "3.8 Flash",
                "claude-auto": "Auto",
            }[payload["models"][key]],
        }
        for key in payload["providers"]
    ]
    ready = all(item["ready"] for item in records)
    route.fulfill(json={
        "ready": ready,
        "providers": records,
        "message": (
            "All selected jurors are signed in."
            if ready
            else "Some selected jurors are unavailable. Recheck or adjust the selection."
        ),
    })


def session_payload(*, running=True, phase="reviewing", providers=("chatgpt", "grok")):
    return {
        "session_id": "jury-one",
        "browser": "edge",
        "question": "Check the original claim against the primary evidence.",
        "running": running,
        "phase": phase,
        "message": "Cross-checking the evidence." if running else "No consensus. Objections remain.",
        "convergence_mode": "automatic",
        "termination_reason": "",
        "round": 1,
        "providers": [{"key": key, "ready": True} for key in providers],
        "rounds": [{
            "round": 1,
            "opinions": [{
                "provider": key,
                "valid": key != "grok",
                "verdict": "supported" if key == "chatgpt" else "unverified",
                "conclusion": "Primary evidence requires closer inspection.",
                "response": "Original provider response.",
                "conversation_url": f"https://{key}.com/c/one",
                "evidence": [{"url": "https://example.com/original", "supports": "Original wording differs from the claim."}],
                "unresolved": [
                    "Publication date does not establish an agreement."
                    if key != "grok"
                    else "The juror did not return a structured vote."
                ],
            } for key in providers],
        }],
        "response": "" if running else "The available evidence does not establish the claim.",
        "consensus": False,
    }


def open_jury(jury_browser, server_url, width, checks, *, height=900, unavailable=()):
    context = jury_browser.new_context(
        viewport={"width": width, "height": height},
        color_scheme="dark" if width < 600 else "light",
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.route("**/api/jury/check", lambda route: account_check(route, checks, unavailable=unavailable))
    page.route("**/api/jury/sessions**", lambda route: route.fulfill(json={"sessions": []}))
    page.goto(f"{server_url}/jury/edge")
    if width <= 900:
        page.locator("#sidebar_toggle").click()
    return page, context


def test_jury_model_menu_escapes_sidebar_clipping(jury_browser, sidebar_server_url):
    checks = []
    page, context = open_jury(
        jury_browser,
        sidebar_server_url,
        753,
        checks,
        height=1355,
    )
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        assert checks == []
        page.evaluate("""() => {
            const pagehide = new Event('pagehide');
            Object.defineProperty(pagehide, 'persisted', {value: true});
            window.dispatchEvent(pagehide);
            const pageshow = new Event('pageshow');
            Object.defineProperty(pageshow, 'persisted', {value: true});
            window.dispatchEvent(pageshow);
        }""")
        expect(page.locator("[data-jury-check-label]")).to_have_text("Not checked")
        assert checks == []
        trigger = page.locator('[data-jury-model-trigger="chatgpt"]')
        menu = page.locator('[data-jury-model-menu="chatgpt"]')
        trigger.click()
        expect(menu).to_be_visible()
        page.wait_for_function("""() => {
            const trigger = document.querySelector('[data-jury-model-trigger="chatgpt"]');
            const menu = document.querySelector('[data-jury-model-menu="chatgpt"]');
            return Math.abs(menu.getBoundingClientRect().right - trigger.getBoundingClientRect().right) <= 1;
        }""")
        geometry = menu.evaluate("""menu => {
            const trigger = document.querySelector('[data-jury-model-trigger="chatgpt"]');
            const providerList = document.querySelector('.jury-provider-list');
            const menuRect = menu.getBoundingClientRect();
            const triggerRect = trigger.getBoundingClientRect();
            const contentRect = providerList.getBoundingClientRect();
            const options = Array.from(menu.querySelectorAll('[role="option"]'));
            return {
                portalled: menu.parentElement?.matches('[data-shared-select-overlay]') === true,
                left: menuRect.left,
                right: menuRect.right,
                top: menuRect.top,
                bottom: menuRect.bottom,
                triggerRight: triggerRect.right,
                triggerBottom: triggerRect.bottom,
                contentLeft: contentRect.left,
                contentRight: contentRect.right,
                viewportWidth: innerWidth,
                viewportHeight: innerHeight,
                optionsHit: options.every(option => {
                    const box = option.getBoundingClientRect();
                    const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
                    return hit === option || option.contains(hit);
                }),
                optionLabelsFit: options.every(option => {
                    const text = option.querySelector('.trade-strategy-dropdown-text');
                    const canvas = document.createElement('canvas');
                    const context = canvas.getContext('2d');
                    context.font = getComputedStyle(text).font;
                    return context.measureText(text.textContent).width <= text.clientWidth + 1;
                }),
                horizontalOverflow: document.documentElement.scrollWidth > innerWidth + 1,
            };
        }""")
        assert geometry["portalled"]
        assert geometry["left"] >= geometry["contentLeft"] - 1
        assert geometry["right"] <= geometry["contentRight"] + 1
        assert abs(geometry["right"] - geometry["triggerRight"]) <= 1, geometry
        assert geometry["top"] >= geometry["triggerBottom"] + 3
        assert geometry["bottom"] <= geometry["viewportHeight"] - 9
        assert geometry["optionsHit"]
        assert geometry["optionLabelsFit"]
        assert not geometry["horizontalOverflow"]
        Path("test-results").mkdir(exist_ok=True)
        page.screenshot(path="test-results/jury-model-menu-753.png")
        page.keyboard.press("Escape")
        expect(menu).to_be_hidden()
        expect(trigger).to_have_attribute("aria-expanded", "false")
        assert menu.evaluate("menu => menu.parentElement?.dataset.juryModelPicker === 'chatgpt'")
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize(
    ("width", "height", "summary_columns"),
    ((1024, 900, "248px 34px"), (585, 1218, "240px 26px")),
)
def test_jury_recent_sessions_reuses_agent_disclosure_style(
    jury_browser,
    sidebar_server_url,
    width,
    height,
    summary_columns,
):
    """Keep the Jury disclosure on the rendered Agent Recent sessions contract."""
    context = jury_browser.new_context(
        viewport={"width": width, "height": height},
        color_scheme="light",
        reduced_motion="reduce",
    )
    agent_page = context.new_page()
    jury_page = context.new_page()
    jury_page.route("**/api/jury/sessions**", lambda route: route.fulfill(json={"sessions": []}))

    def disclosure_style(page, selector):
        return page.locator(selector).evaluate("""details => {
            const summary = details.querySelector(':scope > summary');
            const body = details.querySelector(':scope > .ui-collapse-body');
            const action = body.querySelector('.agent-new-session-button');
            const value = (node, names, pseudo = null) => {
                const style = getComputedStyle(node, pseudo);
                return Object.fromEntries(names.map(name => [name, style[name]]));
            };
            return {
                details: value(details, [
                    'backgroundColor', 'borderBottomStyle', 'borderLeftStyle',
                    'borderRightStyle', 'borderTopStyle', 'marginTop', 'minWidth',
                    'overflowX', 'overflowY', 'padding',
                ]),
                summary: value(summary, [
                    'alignItems', 'columnGap', 'cursor', 'display', 'fontSize',
                    'fontWeight', 'gridTemplateColumns', 'height', 'minHeight',
                    'padding',
                ]),
                chevron: value(summary, [
                    'backgroundColor', 'height', 'maskImage', 'transform', 'width',
                ], '::after'),
                body: value(body, ['display', 'minWidth', 'overflowX', 'overflowY', 'padding']),
                action: value(action, [
                    'borderRadius', 'fontSize', 'fontWeight', 'height', 'minHeight',
                    'paddingLeft',
                ]),
                horizontalOverflow: document.documentElement.scrollWidth > innerWidth + 1,
            };
        }""")

    try:
        agent_page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        jury_page.goto(f"{sidebar_server_url}/jury/edge")
        if width <= 900:
            agent_page.locator("#sidebar_toggle").click()
            jury_page.locator("#sidebar_toggle").click()

        agent_style = disclosure_style(
            agent_page,
            "aside#agent_sidebar > details.agent-session-collapse",
        )
        jury_style = disclosure_style(
            jury_page,
            "aside#jury_sidebar > details.agent-session-collapse",
        )

        assert jury_style == agent_style
        assert jury_style["details"] == {
            "backgroundColor": "rgba(0, 0, 0, 0)",
            "borderBottomStyle": "none",
            "borderLeftStyle": "none",
            "borderRightStyle": "none",
            "borderTopStyle": "none",
            "marginTop": "18px",
            "minWidth": "0px",
            "overflowX": "visible",
            "overflowY": "visible",
            "padding": "0px",
        }
        assert jury_style["summary"] == {
            "alignItems": "center",
            "columnGap": "8px",
            "cursor": "pointer",
            "display": "grid",
            "fontSize": "15px",
            "fontWeight": "400",
            "gridTemplateColumns": summary_columns,
            "height": "36px",
            "minHeight": "36px",
            "padding": "0px",
        }
        assert jury_style["chevron"]["width"] == "12px"
        assert jury_style["chevron"]["height"] == "8px"
        assert jury_style["chevron"]["transform"] == "matrix(-1, 0, 0, -1, 0, 0)"
        assert jury_style["body"] == {
            "display": "grid",
            "minWidth": "0px",
            "overflowX": "visible",
            "overflowY": "visible",
            "padding": "0px 0px 10px",
        }
        assert jury_style["horizontalOverflow"] is False
    finally:
        context.close()


@pytest.mark.parametrize("width", [1024, 390])
def test_jury_failed_session_delete_and_shadow_bleed(
    jury_browser, sidebar_server_url, width,
):
    context = jury_browser.new_context(
        viewport={"width": width, "height": 1000 if width > 900 else 844},
        color_scheme="light" if width > 900 else "dark",
        reduced_motion="reduce",
    )
    page = context.new_page()
    errors = []
    deleted = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    catalog = [
        {
            "session_id": "failed-0",
            "title": "Failed Jury zero",
            "phase": "failed",
            "running": False,
            "deletable": True,
        },
        {
            "session_id": "inconclusive-1",
            "title": "Inconclusive Jury",
            "phase": "inconclusive",
            "running": False,
            "deletable": False,
        },
        *[
            {
                "session_id": f"failed-{index}",
                "title": f"Failed Jury {index}",
                "phase": "failed",
                "running": False,
                "deletable": True,
            }
            for index in range(2, 8)
        ],
    ]

    def sessions(route):
        route.fulfill(json={"sessions": deepcopy(catalog)})

    def delete_session(route):
        payload = route.request.post_data_json
        deleted.append(payload)
        catalog[:] = [item for item in catalog if item["session_id"] != payload["session_id"]]
        route.fulfill(json={"deleted": True, "session_id": payload["session_id"]})

    page.route("**/api/jury/sessions**", sessions)
    page.route("**/api/jury/session", delete_session)
    try:
        page.goto(f"{sidebar_server_url}/jury/edge")
        if width <= 900:
            page.locator("#sidebar_toggle").click()
        rows = page.locator("[data-jury-session-list] > .agent-execution-session-row")
        expect(rows).to_have_count(8)
        failed = page.locator('[data-jury-session-id="failed-0"]')
        inconclusive = page.locator('[data-jury-session-id="inconclusive-1"]')
        expect(failed.locator("xpath=..").locator(".agent-execution-session-delete")).to_have_count(1)
        expect(inconclusive.locator("xpath=..").locator(".agent-execution-session-delete")).to_have_count(0)

        geometry = page.locator("[data-jury-session-list]").evaluate("""list => {
            const first = list.querySelector('.agent-execution-session').getBoundingClientRect();
            const listRect = list.getBoundingClientRect();
            const body = list.closest('.ui-collapse-body');
            list.scrollTop = list.scrollHeight;
            const last = list.querySelector(
                '.agent-execution-session-row:last-child .agent-execution-session'
            ).getBoundingClientRect();
            return {
                overflowY: getComputedStyle(list).overflowY,
                bodyOverflow: getComputedStyle(body).overflow,
                inlineStartBleed: first.left - listRect.left,
                inlineEndBleed: listRect.right - first.right,
                blockStartBleed: first.top - listRect.top,
                blockEndBleed: listRect.bottom - last.bottom,
                scrollable: list.scrollHeight > list.clientHeight,
                horizontalOverflow: document.documentElement.scrollWidth > innerWidth + 1,
            };
        }""")
        assert geometry["overflowY"] == "auto"
        assert geometry["bodyOverflow"] == "visible"
        assert geometry["inlineStartBleed"] >= 15
        assert geometry["inlineEndBleed"] >= 15
        assert geometry["blockStartBleed"] >= 5
        assert geometry["blockEndBleed"] >= 30
        assert geometry["scrollable"]
        assert not geometry["horizontalOverflow"]

        page.locator("[data-jury-session-list]").evaluate("list => { list.scrollTop = 0; }")
        failed.hover()
        delete_button = failed.locator("xpath=..").locator(".agent-execution-session-delete")
        expect(delete_button).to_be_visible()
        expect(delete_button).to_have_attribute(
            "aria-label", "Delete failed Jury session: Failed Jury zero",
        )
        centers = failed.locator("xpath=..").evaluate("""row => {
            const pill = row.querySelector('.agent-execution-session').getBoundingClientRect();
            const action = row.querySelector('.agent-execution-session-delete').getBoundingClientRect();
            return {
                pillRightCenterX: pill.right - pill.height / 2,
                pillCenterY: pill.top + pill.height / 2,
                actionCenterX: action.left + action.width / 2,
                actionCenterY: action.top + action.height / 2,
            };
        }""")
        assert centers["actionCenterX"] == pytest.approx(centers["pillRightCenterX"], abs=0.1)
        assert centers["actionCenterY"] == pytest.approx(centers["pillCenterY"], abs=0.1)

        delete_button.click()
        expect(page.locator('[data-jury-session-id="failed-0"]')).to_have_count(0)
        expect(inconclusive).to_be_focused()
        assert deleted == [{"session_id": "failed-0"}]
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("width,height", [(1028, 1355), (1007, 1232), (390, 844)])
def test_jury_runtime_preferences_restore_without_account_probe(
    jury_browser,
    sidebar_server_url,
    width,
    height,
):
    checks = []
    page, context = open_jury(jury_browser, sidebar_server_url, width, checks, height=height)
    storage_key = "cachelikes:jury-runtime-preferences:v1"
    try:
        page.locator('label[for="jury_provider_grok"]').click()
        page.locator('label[for="jury_provider_gemini"]').click()
        page.locator('label[for="jury_provider_claude"]').click()
        for provider, model in (
            ("chatgpt", "chatgpt-latest-medium"),
            ("grok", "grok-build"),
            ("gemini", "gemini-3.8-flash"),
        ):
            page.locator(f'[data-jury-model-trigger="{provider}"]').click()
            page.locator(f'[data-jury-model-option="{model}"]').click()
        page.locator("[data-jury-browser-trigger]").click()
        page.locator('[data-jury-browser-option="edge"]').click()

        expected = {
            "version": 1,
            "browser": "edge",
            "providers": ["chatgpt", "claude"],
            "models": {
                "chatgpt": "chatgpt-latest-medium",
                "grok": "grok-build",
                "gemini": "gemini-3.8-flash",
                "claude": "claude-auto",
            },
        }
        assert page.evaluate(
            "key => JSON.parse(window.localStorage.getItem(key))",
            storage_key,
        ) == expected
        expect(page.locator('[aria-label="Agent modes"] a').first).to_have_attribute(
            "href",
            "/agent/tunnel/chatgpt",
        )
        expect(page.locator("[data-jury-check-label]")).to_have_text("Not checked")
        assert checks == []

        page.goto(f"{sidebar_server_url}/jury/edge", wait_until="domcontentloaded")
        expect(page).to_have_url(f"{sidebar_server_url}/jury/edge")
        assert page.locator("[data-jury-provider]:checked").evaluate_all(
            "inputs => inputs.map(input => input.value)"
        ) == ["chatgpt", "claude"]
        expect(page.locator('[data-jury-model-label="chatgpt"]')).to_have_text("Latest · Medium")
        expect(page.locator('[data-jury-model-label="grok"]')).to_have_text("Build")
        expect(page.locator('[data-jury-model-label="gemini"]')).to_have_text("3.8 Flash")
        expect(page.locator('[data-jury-model-label="claude"]')).to_have_text("Auto")
        expect(page.locator("[data-jury-check-label]")).to_have_text("Not checked")
        expect(page.locator("[data-jury-ready-check]")).to_be_hidden()
        expect(page.locator("[data-jury-submit]")).to_be_disabled()
        assert checks == []

        page.locator("[data-jury-new-session]").click()
        assert page.evaluate(
            "key => JSON.parse(window.localStorage.getItem(key))",
            storage_key,
        ) == expected
        assert checks == []

        page.evaluate(
            "key => window.localStorage.setItem(key, '{')",
            storage_key,
        )
        page.goto(f"{sidebar_server_url}/jury/edge", wait_until="domcontentloaded")
        expect(page).to_have_url(f"{sidebar_server_url}/jury/edge")
        assert page.locator("[data-jury-provider]:checked").evaluate_all(
            "inputs => inputs.map(input => input.value)"
        ) == ["chatgpt", "grok", "gemini"]
        expect(page.locator('[data-jury-model-label="chatgpt"]')).to_have_text("Latest · Extra High")
        expect(page.locator('[data-jury-model-label="grok"]')).to_have_text("Auto")
        expect(page.locator('[data-jury-model-label="gemini"]')).to_have_text("3.1 Pro")
        expect(page.locator("[data-jury-check-label]")).to_have_text("Not checked")
        assert checks == []
    finally:
        context.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_jury_all_selected_accounts_gate_and_shared_responsive_sidebar(jury_browser, sidebar_server_url, width):
    checks = []
    page, context = open_jury(jury_browser, sidebar_server_url, width, checks, unavailable=("gemini",))
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        expect(page.locator('[aria-label="Agent modes"] a')).to_have_text(["Agentic", "Jurors"])
        expect(page.locator('[aria-label="Agent modes"] a[aria-current="page"]')).to_have_text("Jurors")
        expect(page.locator('[aria-label="Agent modes"]')).to_have_class(
            re.compile(r"\bsegmented-control\b")
        )
        expect(page.locator("[data-jury-provider]:checked")).to_have_count(3)
        expect(page.locator('[data-jury-provider][value="claude"]')).not_to_be_checked()
        expect(page.locator("[data-jury-check-label]")).to_have_text("Not checked")
        expect(page.locator("[data-jury-check-message]")).to_be_empty()
        expect(page.locator("[data-jury-submit]")).to_be_disabled()
        assert checks == []
        page.locator("[data-jury-check]").click()
        expect(page.locator("[data-jury-check-label]")).to_have_text("Not ready")
        expect(page.locator("[data-jury-check-message]")).to_have_text(
            "Unavailable juror: Gemini (3.1 Pro) — Provider unavailable. "
            "Sign in or deselect this juror, then check accounts again; at least two must remain selected."
        )
        unavailable_geometry = page.locator(".jury-account-status").evaluate("""card => ({
            cardFits: card.scrollWidth <= card.clientWidth,
            messageFits: card.querySelector('[data-jury-check-message]').scrollWidth
                <= card.querySelector('[data-jury-check-message]').clientWidth,
            pageFits: document.documentElement.scrollWidth <= innerWidth + 1,
        })""")
        assert all(unavailable_geometry.values())
        expect(page.locator("[data-jury-ready-check]")).to_be_hidden()
        expect(page.locator("#jury_sidebar")).not_to_contain_text("Terminal")
        expect(page.locator("#jury_sidebar")).not_to_contain_text("Current project")
        expect(page.locator('[data-jury-browser-trigger]')).to_have_attribute("aria-label", "Browser: Edge")
        row_geometry = page.locator("[data-jury-provider-row]").evaluate_all("""rows => rows.map(row => {
            const items = [
                row.querySelector('.jury-provider-selection-mark'),
                row.querySelector('.browser-picker-selected-icon-shell'),
                row.querySelector('.jury-provider-name'),
                row.querySelector('[data-jury-model-trigger]'),
            ].filter(item => item && item.getBoundingClientRect().width > 0);
            const boxes = items.map(item => item.getBoundingClientRect());
            const rowBox = row.getBoundingClientRect();
            const trigger = row.querySelector('[data-jury-model-trigger]');
            const name = row.querySelector('.jury-provider-name');
            const nameRange = document.createRange();
            nameRange.selectNodeContents(name);
            const triggerStyle = getComputedStyle(trigger);
            return {
                sameLine: Math.max(...boxes.map(box => box.top)) < Math.min(...boxes.map(box => box.bottom)),
                insideRow: boxes.every(box => box.left >= rowBox.left && box.right <= rowBox.right),
                nameFits: nameRange.getBoundingClientRect().width <= name.getBoundingClientRect().width + 0.01,
                rowHeight: rowBox.height,
                triggerHeight: trigger.getBoundingClientRect().height,
                triggerWidth: trigger.getBoundingClientRect().width,
                triggerRight: trigger.getBoundingClientRect().right,
                triggerRadius: triggerStyle.borderRadius,
            };
        })""")
        assert all(item["sameLine"] and item["insideRow"] for item in row_geometry)
        if width == 1280:
            assert all(item["nameFits"] for item in row_geometry)
        assert all(item["rowHeight"] == 36 and item["triggerHeight"] == 30 for item in row_geometry)
        assert all(item["triggerWidth"] < 150 and item["triggerRadius"] == "999px" for item in row_geometry)
        assert max(item["triggerRight"] for item in row_geometry) - min(
            item["triggerRight"] for item in row_geometry
        ) <= 1
        expect(page.locator("[data-jury-provider-readiness]")).to_have_count(0)
        expect(page.locator('[data-jury-model-picker="grok"]')).to_have_attribute(
            "data-shared-select-kind", "jury-model",
        )
        selected_mark = page.locator(
            '[data-jury-provider-row="chatgpt"] .jury-provider-selection-mark'
        )
        mark_style = selected_mark.evaluate(
            "element => ({color: getComputedStyle(element).backgroundColor, "
            "mask: getComputedStyle(element).maskImage})"
        )
        success_color = page.locator("[data-jury-root]").evaluate("""root => {
            const sample = document.createElement('span');
            sample.style.backgroundColor = 'var(--theme-success-strong)';
            root.appendChild(sample);
            const color = getComputedStyle(sample).backgroundColor;
            sample.remove();
            return color;
        }""")
        assert mark_style["color"] == success_color
        assert "checkmark.circle.fill.svg" in mark_style["mask"]
        Path("test-results").mkdir(exist_ok=True)
        if width == 1280:
            page.screenshot(path="test-results/jury-unavailable.png")
        gemini_model = page.locator('[data-jury-model-trigger="gemini"]')
        gemini_model.press("ArrowDown")
        expect(page.locator('[data-jury-model-option][data-provider="gemini"]').first).to_be_focused()
        page.locator('[data-jury-model-option][data-provider="gemini"]').first.press("End")
        expect(page.locator('[data-jury-model-option="gemini-3.8-flash"]')).to_be_focused()
        page.locator('[data-jury-model-option="gemini-3.8-flash"]').press("Enter")
        expect(gemini_model).to_have_attribute("aria-expanded", "false")
        expect(gemini_model).to_have_text("3.8 Flash")
        expect(page.locator("[data-jury-check-label]")).to_have_text("Not checked")
        expect(page.locator("[data-jury-submit]")).to_be_disabled()
        assert len(checks) == 1
        page.locator('label[for="jury_provider_gemini"]').click()
        expect(page.locator("[data-jury-check-label]")).to_have_text("Not checked")
        expect(page.locator("[data-jury-submit]")).to_be_disabled()
        assert len(checks) == 1
        page.locator("[data-jury-check]").click()
        expect(page.locator("[data-jury-ready-check]")).to_be_visible()
        status_geometry = page.locator(".jury-account-status").evaluate("""card => {
            const literal = card.querySelector('.browser-session-status-literal').getBoundingClientRect();
            const message = card.querySelector('[data-jury-check-message]');
            const messageRange = document.createRange();
            messageRange.selectNodeContents(message);
            const firstMessageLine = messageRange.getClientRects()[0];
            return {literalLeft: literal.left, messageTextLeft: firstMessageLine?.left};
        }""")
        assert abs(status_geometry["literalLeft"] - status_geometry["messageTextLeft"]) <= 1
        assert checks[-1] == {
            "browser": "edge",
            "providers": ["chatgpt", "grok"],
            "models": {
                "chatgpt": "chatgpt-latest-extra-high",
                "grok": "grok-auto",
            },
        }
        empty_mark = page.locator(
            '[data-jury-provider-row="gemini"] .jury-provider-selection-mark'
        ).evaluate("element => getComputedStyle(element).maskImage")
        assert empty_mark.endswith('/static/images/circle.svg\")')
        page.screenshot(path="test-results/jury-desktop.png" if width == 1280 else "test-results/jury-mobile.png")
        if width <= 900:
            page.locator("#sidebar_toggle").click()
            expect(page.locator(".sidebar-dock")).to_have_css("opacity", "0")
        page.locator("[data-jury-prompt]").fill("Check this claim.")
        expect(page.locator("[data-jury-submit]")).to_be_enabled()
        if width == 390:
            page.screenshot(path="test-results/jury-mobile-workspace.png")
        geometry = page.evaluate("""() => {
            const bounds = (selector) => document.querySelector(selector).getBoundingClientRect();
            const composer = bounds('#jury_prompt_form');
            const textarea = bounds('[data-jury-prompt]');
            return {
                overflow: document.documentElement.scrollWidth > innerWidth + 1,
                composerRight: composer.right, width: innerWidth, textareaHeight: textarea.height,
                composerBottom: composer.bottom, viewportHeight: innerHeight,
            };
        }""")
        assert not geometry["overflow"]
        assert geometry["composerRight"] <= geometry["width"] + 1
        assert geometry["composerBottom"] <= geometry["viewportHeight"] + 1
        assert geometry["textareaHeight"] >= 60
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("width", [1028, 390])
def test_jury_account_status_uses_compact_annotated_padding(
    jury_browser,
    sidebar_server_url,
    width,
):
    """Keep the Jury-only readiness card compact at desktop and narrow widths."""
    checks = []
    page, context = open_jury(
        jury_browser,
        sidebar_server_url,
        width,
        checks,
        height=1355,
    )
    try:
        padding = page.locator(".jury-account-status").evaluate("""card => {
            const style = getComputedStyle(card);
            return {
                top: style.paddingTop,
                right: style.paddingRight,
                bottom: style.paddingBottom,
                left: style.paddingLeft,
                pageFits: document.documentElement.scrollWidth <= innerWidth + 1,
            };
        }""")
        assert padding == {
            "top": "4px",
            "right": "0px",
            "bottom": "4px",
            "left": "10px",
            "pageFits": True,
        }
        assert checks == []
    finally:
        context.close()


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("providers", [("chatgpt", "grok"), ("chatgpt", "grok", "gemini")])
def test_jury_one_start_retains_rounds_evidence_and_dissent(jury_browser, sidebar_server_url, width, providers):
    checks = []
    page, context = open_jury(jury_browser, sidebar_server_url, width, checks)
    current = session_payload(providers=providers)
    starts = []
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def start(route):
        starts.append(route.request.post_data_json)
        route.fulfill(json=current)

    page.route("**/api/jury/start", start)
    page.route("**/api/jury/status**", lambda route: route.fulfill(json=current))
    try:
        if "gemini" not in providers:
            page.locator('label[for="jury_provider_gemini"]').click()
        else:
            expect(page.locator('[data-jury-model-label="gemini"]')).to_have_text("3.1 Pro")
        expect(page.locator(".jury-composer-note, .jury-round-limit, [data-jury-max-rounds]")).to_have_count(0)
        expect(page.locator(".agent-composer-footer > *")).to_have_count(1)
        expect(page.locator(".agent-composer-footer > .agent-composer-actions")).to_have_count(1)
        expect(page.locator("[data-jury-check-label]")).to_have_text("Not checked")
        assert checks == []
        page.locator("[data-jury-check]").click()
        expect(page.locator("[data-jury-ready-check]")).to_be_visible()
        assert checks[-1]["providers"] == list(providers)
        if width <= 900:
            page.locator("#sidebar_toggle").click()
        page.locator("[data-jury-prompt]").fill(current["question"])
        page.locator("[data-jury-submit]").click()
        expect(page.locator("[data-jury-submit]")).to_have_attribute("aria-label", "Stop Jury")
        expect(page.locator(".jury-opinion")).to_have_count(len(providers))
        for provider in providers:
            opinion = page.locator(
                f'.jury-opinion[data-provider="{provider}"]'
            ).first
            icon_geometry = opinion.evaluate("""(article, providerKey) => {
                const opinionIcon = article.querySelector(
                    '.jury-opinion-provider .browser-picker-option-icon'
                );
                const shell = article.querySelector(
                    '.jury-opinion-provider .browser-picker-selected-icon-shell'
                );
                const sidebarIcon = document.querySelector(
                    `[data-jury-provider-row="${providerKey}"] .browser-picker-option-icon`
                );
                const shellStyle = getComputedStyle(shell);
                const iconStyle = getComputedStyle(opinionIcon);
                return {
                    sameSource: opinionIcon.src === sidebarIcon.src,
                    shellSize: [shellStyle.width, shellStyle.height],
                    iconSize: [iconStyle.width, iconStyle.height],
                    decorative: shell.getAttribute('aria-hidden'),
                };
            }""", provider)
            assert icon_geometry == {
                "sameSource": True,
                "shellSize": ["24px", "24px"],
                "iconSize": ["22px", "22px"],
                "decorative": "true",
            }
        first_content = page.locator(
            ".jury-opinion .agent-response-answer-content"
        ).first
        expect(first_content).to_have_text(
            "Primary evidence requires closer inspection."
        )
        expect(first_content).not_to_contain_text("Original provider response.")
        expect(page.locator(".jury-evidence").first).to_contain_text("Original wording differs")
        expect(page.locator(".jury-unresolved").first).to_contain_text("Publication date")
        grok = page.locator('.jury-opinion[data-provider="grok"]').first
        expect(grok.locator(".jury-opinion-header > span")).to_have_text("invalid vote")
        expect(grok.locator(".jury-unresolved > p")).to_have_text(
            "Structured vote unavailable"
        )
        expect(grok.locator(".jury-unresolved")).to_contain_text(
            "The juror did not return a structured vote."
        )
        expect(page.locator(".jury-evidence a").first).to_have_attribute("href", "https://example.com/original")
        assert page.locator("[data-jury-provider]").evaluate_all("inputs => inputs.every(input => input.disabled)")
        continued_rounds = []
        for round_number in range(2, 5):
            continued_round = deepcopy(current["rounds"][0])
            continued_round["round"] = round_number
            continued_rounds.append(continued_round)
        continued_rounds[-1]["opinions"][0]["conclusion"] = (
            '<img src=x onerror="window.juryInjected=true">'
        )
        continued_rounds[-1]["opinions"][0]["evidence"].append({
            "url": "javascript:alert(1)",
            "supports": "Unsafe source is plain text.",
        })
        current.update(
            running=False, phase="inconclusive", round=4,
            termination_reason="evidence_stalled",
            rounds=[current["rounds"][0], *continued_rounds],
            response="The available evidence does not establish the claim.",
        )
        expect(page.locator(".jury-round")).to_have_count(4, timeout=7000)
        rounds = page.locator(".jury-round")
        expect(rounds.locator(".jury-round-number")).to_have_text(["1", "2", "3", "4"])
        expect(rounds.first.locator("summary")).to_have_attribute(
            "aria-label", "Round 1: Independent checks",
        )
        expect(rounds.nth(1).locator("summary")).to_have_attribute(
            "aria-label", "Round 2: Cross-review",
        )
        collapse_geometry = rounds.evaluate_all("""items => {
            const first = items[0];
            const second = items[1];
            const last = items.at(-1);
            const summary = first.querySelector('summary');
            const body = first.querySelector('.ui-collapse-body');
            const number = first.querySelector('.jury-round-number');
            const summaryStyle = getComputedStyle(summary);
            const bodyStyle = getComputedStyle(body);
            const numberStyle = getComputedStyle(number);
            const closedChevron = getComputedStyle(second.querySelector('summary'), '::after');
            const openChevron = getComputedStyle(last.querySelector('summary'), '::after');
            return {
                summaryDisplay: summaryStyle.display,
                summaryColumns: summaryStyle.gridTemplateColumns,
                summaryGap: summaryStyle.columnGap,
                summaryPadding: [summaryStyle.paddingTop, summaryStyle.paddingRight,
                    summaryStyle.paddingBottom, summaryStyle.paddingLeft],
                summaryFont: [summaryStyle.fontSize, summaryStyle.fontWeight],
                bodyPadding: [bodyStyle.paddingTop, bodyStyle.paddingRight,
                    bodyStyle.paddingBottom, bodyStyle.paddingLeft],
                siblingMargin: getComputedStyle(second).marginTop,
                borderStyle: getComputedStyle(first).borderBottomStyle,
                numberSize: [numberStyle.width, numberStyle.height],
                numberBackground: numberStyle.backgroundColor,
                numberBorderColor: numberStyle.borderColor,
                numberColor: numberStyle.color,
                closedChevron: [closedChevron.width, closedChevron.height,
                    closedChevron.transform],
                openChevron: [openChevron.width, openChevron.height,
                    openChevron.transform],
                pageFits: document.documentElement.scrollWidth <= innerWidth + 1,
            };
        }""")
        assert collapse_geometry["summaryDisplay"] == "grid"
        assert collapse_geometry["summaryColumns"].endswith(" 12px")
        assert collapse_geometry["summaryGap"] == "8px"
        assert collapse_geometry["summaryPadding"] == ["10px", "0px", "10px", "0px"]
        assert collapse_geometry["summaryFont"] == ["15px", "500"]
        assert collapse_geometry["bodyPadding"] == ["0px", "10px", "10px", "10px"]
        assert collapse_geometry["siblingMargin"] == "8px"
        assert collapse_geometry["borderStyle"] == "none"
        assert collapse_geometry["numberSize"] == ["20px", "20px"]
        assert collapse_geometry["numberBackground"] == "rgba(0, 0, 0, 0)"
        assert collapse_geometry["numberBorderColor"] == collapse_geometry["numberColor"]
        assert collapse_geometry["closedChevron"][:2] == ["12px", "8px"]
        assert collapse_geometry["closedChevron"][2] == "matrix(1, 0, 0, 1, 0, 0)"
        assert collapse_geometry["openChevron"][:2] == ["12px", "8px"]
        assert collapse_geometry["openChevron"][2] == "matrix(-1, 0, 0, -1, 0, 0)"
        assert collapse_geometry["pageFits"] is True
        expect(page.locator("[data-jury-conclusion-title]")).to_have_text("Review outcome")
        expect(page.locator("[data-jury-conclusion]")).to_contain_text("Agreement is not proof")
        if width <= 900:
            page.locator("#sidebar_toggle").click()
        expect(page.locator("[data-jury-ready-check]")).to_be_visible()
        colors = page.locator("[data-jury-root]").evaluate("""root => {
            const reference = document.createElement('span');
            reference.style.color = 'var(--theme-warning)';
            reference.style.backgroundColor = 'var(--theme-success-strong)';
            root.appendChild(reference);
            const warning = getComputedStyle(reference).color;
            const success = getComputedStyle(reference).backgroundColor;
            reference.remove();
            return {
                warning, success,
                status: getComputedStyle(root.querySelector('[data-jury-status] .agent-response-status-dot')).backgroundColor,
                account: getComputedStyle(root.querySelector('[data-jury-ready-check]')).backgroundColor,
            };
        }""")
        assert colors["status"] == colors["warning"]
        assert colors["account"] == colors["success"]
        assert colors["status"] != colors["account"]
        if width <= 900:
            page.locator("#sidebar_toggle").click()
        expect(page.locator(".jury-round").last).to_contain_text('<img src=x onerror="window.juryInjected=true">')
        assert page.locator(
            ".jury-round .agent-response-answer-content img"
        ).count() == 0
        assert page.locator('.jury-round a[href^="javascript:"]').count() == 0
        assert page.evaluate("window.juryInjected || false") is False
        assert len(starts) == 1
        assert starts[0]["providers"] == list(providers)
        assert starts[0]["models"] == {
            key: {
                "chatgpt": "chatgpt-latest-extra-high",
                "grok": "grok-auto",
                "gemini": "gemini-3.1-pro",
            }[key]
            for key in providers
        }
        assert "max_rounds" not in starts[0]
        assert starts[0]["question"] == current["question"]
        expect(page.locator("[data-jury-submit]")).to_be_disabled()
        page.screenshot(path=f"test-results/jury-discussion-{len(providers)}-{width}.png")
        assert not errors
    finally:
        context.close()


def test_jury_browser_keyboard_selection_and_dock_restore(jury_browser, sidebar_server_url):
    checks = []
    page, context = open_jury(jury_browser, sidebar_server_url, 1280, checks)
    try:
        expect(page.locator("[data-jury-check-label]")).to_have_text("Not checked")
        assert checks == []
        page.locator("[data-jury-check]").click()
        expect(page.locator("[data-jury-ready-check]")).to_be_visible()
        trigger = page.locator("[data-jury-browser-trigger]")
        trigger.press("ArrowDown")
        expect(page.locator('[data-jury-browser-option="edge"]')).to_be_focused()
        page.locator('[data-jury-browser-option="edge"]').press("End")
        expect(page.locator("[data-jury-browser-option]").last).to_be_focused()
        selected_key = page.locator("[data-jury-browser-option]").last.get_attribute("data-jury-browser-option")
        page.locator("[data-jury-browser-option]").last.press("Enter")
        expect(trigger).to_have_attribute("aria-expanded", "false")
        expect(trigger).to_be_focused()
        expect(page).to_have_url(f"{sidebar_server_url}/jury/{selected_key}")
        expect(page.locator("[data-jury-check-label]")).to_have_text("Not checked")
        expect(page.locator("[data-jury-ready-check]")).to_be_hidden()
        assert len(checks) == 1
        page.locator("[data-jury-check]").click()
        expect(page.locator("[data-jury-ready-check]")).to_be_visible()
        assert checks[-1]["browser"] == selected_key
        page.locator('[data-dock-section="settings"]').click()
        expect(page.locator('[data-dock-section="agent"]')).to_have_attribute("href", f"/jury/{selected_key}")
        page.locator('[data-dock-section="agent"]').click()
        expect(page).to_have_url(f"{sidebar_server_url}/jury/{selected_key}")
        expect(page.locator('[aria-label="Agent modes"] [aria-current="page"]')).to_have_text("Jurors")
        expect(page.locator("[data-jury-check-label]")).to_have_text("Not checked")
        assert len(checks) == 2
        page.get_by_role("radio", name="Agentic", exact=True).click()
        expect(page).to_have_url(f"{sidebar_server_url}/agent/tunnel/chatgpt")
        expect(page.locator('[aria-label="Agent modes"]')).to_have_class(
            re.compile(r"\bsegmented-control\b")
        )
        expect(page.locator('[aria-label="Agent modes"] [aria-current="page"]')).to_have_text("Agentic")
        page.get_by_role("radio", name="Jurors", exact=True).click()
        expect(page).to_have_url(f"{sidebar_server_url}/jury/{selected_key}")
        expect(page.locator("[data-jury-check-label]")).to_have_text("Not checked")
        assert len(checks) == 2
    finally:
        context.close()


def test_jury_mode_link_switches_into_and_out_of_safari_without_account_probe(
    jury_browser,
    sidebar_server_url,
):
    checks = []
    page, context = open_jury(jury_browser, sidebar_server_url, 1007, checks, height=1232)
    try:
        page.goto(f"{sidebar_server_url}/jury/safari", wait_until="domcontentloaded")
        agentic = page.locator('[aria-label="Agent modes"] a').first
        expect(agentic).to_have_attribute("href", "/agent/tunnel/chatgpt")

        page.locator("[data-jury-browser-trigger]").click()
        page.locator('[data-jury-browser-option="edge"]').click()
        expect(page).to_have_url(f"{sidebar_server_url}/jury/edge")
        expect(agentic).to_have_attribute("href", "/agent/tunnel/chatgpt")

        page.locator("[data-jury-browser-trigger]").click()
        page.locator('[data-jury-browser-option="safari"]').click()
        expect(page).to_have_url(f"{sidebar_server_url}/jury/safari")
        expect(agentic).to_have_attribute("href", "/agent/tunnel/chatgpt")
        claude_row = page.locator('[data-jury-provider-row="claude"]')
        expect(claude_row).to_be_visible()
        expect(claude_row).to_have_attribute("aria-disabled", "true")
        expect(claude_row).to_have_attribute(
            "title", "Claude Jury is available in Microsoft Edge or Google Chrome.",
        )
        expect(page.locator('[data-jury-provider][value="claude"]')).not_to_be_checked()
        expect(page.locator('[data-jury-provider][value="claude"]')).to_be_disabled()
        assert checks == []
    finally:
        context.close()


def test_safari_jury_shows_disabled_claude_and_migrates_stale_preferences(
    jury_browser,
    sidebar_server_url,
):
    checks = []
    page, context = open_jury(jury_browser, sidebar_server_url, 1007, checks, height=1232)
    storage_key = "cachelikes:jury-runtime-preferences:v1"
    try:
        if not page.locator('[data-jury-browser-option="safari"]').count():
            pytest.skip("Safari Jury is not exposed on this host.")
        expect(page.locator('[data-jury-provider-row="claude"]')).to_be_visible()
        page.locator('label[for="jury_provider_claude"]').click()
        expect(page.locator('[data-jury-provider][value="claude"]')).to_be_checked()

        page.locator("[data-jury-browser-trigger]").click()
        page.locator('[data-jury-browser-option="safari"]').click()
        expect(page).to_have_url(f"{sidebar_server_url}/jury/safari")
        expect(page.locator('[data-jury-provider-row="claude"]')).to_be_visible()
        expect(page.locator('[data-jury-provider-row="claude"]')).to_have_attribute(
            "aria-disabled", "true",
        )
        expect(page.locator('[data-jury-provider][value="claude"]')).not_to_be_checked()
        expect(page.locator('[data-jury-provider][value="claude"]')).to_be_disabled()
        remembered = page.evaluate(
            "key => JSON.parse(window.localStorage.getItem(key))",
            storage_key,
        )
        assert remembered["browser"] == "safari"
        assert "claude" not in remembered["providers"]
        assert checks == []

        page.evaluate(
            """({key, value}) => window.localStorage.setItem(key, JSON.stringify(value))""",
            {
                "key": storage_key,
                "value": {
                    "version": 1,
                    "browser": "safari",
                    "providers": ["chatgpt", "claude"],
                    "models": {
                        "chatgpt": "chatgpt-latest-medium",
                        "grok": "grok-auto",
                        "gemini": "gemini-3.1-pro",
                        "claude": "claude-auto",
                    },
                },
            },
        )
        page.goto(f"{sidebar_server_url}/jury/safari", wait_until="domcontentloaded")
        expect(page).to_have_url(f"{sidebar_server_url}/jury/safari")
        expect(page.locator('[data-jury-provider-row="claude"]')).to_be_visible()
        expect(page.locator('[data-jury-provider-row="claude"]')).to_have_attribute(
            "aria-disabled", "true",
        )
        expect(page.locator('[data-jury-provider][value="claude"]')).not_to_be_checked()
        expect(page.locator('[data-jury-provider][value="chatgpt"]')).to_be_checked()
        migrated = page.evaluate(
            "key => JSON.parse(window.localStorage.getItem(key))",
            storage_key,
        )
        assert migrated["browser"] == "safari"
        assert migrated["providers"] == ["chatgpt"]
        assert "claude" not in migrated["providers"]
        assert checks == []

        page.locator("[data-jury-browser-trigger]").click()
        page.locator('[data-jury-browser-option="edge"]').click()
        expect(page).to_have_url(f"{sidebar_server_url}/jury/edge")
        expect(page.locator('[data-jury-provider-row="claude"]')).to_be_visible()
        expect(page.locator('[data-jury-provider][value="claude"]')).not_to_be_checked()
        expect(page.locator('[data-jury-provider][value="claude"]')).to_be_enabled()
    finally:
        context.close()


def test_new_session_drops_disabled_claude_from_a_legacy_safari_record(
    jury_browser,
    sidebar_server_url,
):
    checks = []
    context = jury_browser.new_context(
        viewport={"width": 1007, "height": 1232},
        reduced_motion="reduce",
    )
    page = context.new_page()
    legacy = session_payload(
        running=False,
        phase="failed",
        providers=("chatgpt", "grok", "gemini", "claude"),
    )
    legacy.update(session_id="legacy-safari-four", browser="safari")
    summary = {
        "session_id": legacy["session_id"],
        "title": "Legacy Safari Jury",
        "phase": "failed",
        "running": False,
    }
    page.route(
        "**/api/jury/check",
        lambda route: account_check(route, checks),
    )
    page.route(
        "**/api/jury/sessions**",
        lambda route: route.fulfill(json={"sessions": [summary]}),
    )
    page.route(
        "**/api/jury/status?**",
        lambda route: route.fulfill(json=legacy),
    )
    try:
        page.goto(f"{sidebar_server_url}/jury/safari")
        if not page.locator('[data-jury-browser-option="safari"]').count():
            pytest.skip("Safari Jury is not exposed on this host.")
        page.get_by_role("button", name="Legacy Safari Jury").click()
        expect(page.locator('[data-jury-provider-row="claude"]')).to_be_visible()
        expect(page.locator('[data-jury-provider-row="claude"]')).to_have_attribute(
            "aria-disabled", "true",
        )
        expect(page.locator('[data-jury-provider][value="claude"]')).to_be_checked()

        page.locator("[data-jury-new-session]").click()
        expect(page.locator('[data-jury-provider][value="claude"]')).not_to_be_checked()
        page.locator("[data-jury-check]").click()
        expect(page.locator("[data-jury-check-label]")).to_have_text(
            "All selected signed in"
        )
        expect(page.locator("[data-jury-ready-check]")).to_be_visible()

        assert len(checks) == 1
        assert checks[0]["browser"] == "safari"
        assert checks[0]["providers"] == ["chatgpt", "grok", "gemini"]
        assert "claude" not in checks[0]["models"]
    finally:
        context.close()


def test_jury_restore_is_read_only_and_stop_preserves_discussion(jury_browser, sidebar_server_url):
    context = jury_browser.new_context(viewport={"width": 1280, "height": 900}, reduced_motion="reduce")
    page = context.new_page()
    current = session_payload()
    writes = []
    page.route("**/api/jury/status**", lambda route: route.fulfill(json=current))
    page.route("**/api/jury/sessions**", lambda route: route.fulfill(json={"sessions": [current]}))
    page.route("**/api/jury/check", lambda route: (writes.append("check"), route.fulfill(json={})))
    page.route("**/api/jury/start", lambda route: (writes.append("start"), route.fulfill(json={})))

    def stop(route):
        writes.append(route.request.post_data_json)
        current.update(running=False, phase="stopped", message="Jury stopped. No further rounds were sent.")
        route.fulfill(json=current)

    page.route("**/api/jury/stop", stop)
    try:
        page.goto(f"{sidebar_server_url}/jury/edge?session_id=jury-one")
        expect(page.locator(".jury-opinion")).to_have_count(2)
        expect(page.locator("[data-jury-submit]")).to_have_attribute("aria-label", "Stop Jury")
        assert writes == []
        page.locator("[data-jury-submit]").click()
        expect(page.locator("[data-jury-status-copy]")).to_contain_text("No further rounds")
        expect(page.locator(".jury-opinion")).to_have_count(2)
        assert writes == [{"session_id": "jury-one"}]
        page.locator("[data-jury-new-session]").click()
        expect(page.locator("[data-jury-check-label]")).to_have_text("Not checked")
        expect(page.locator("[data-jury-ready-check]")).to_be_hidden()
        assert writes == [{"session_id": "jury-one"}]
    finally:
        context.close()
