"""Rendered Jury readiness, review evidence, and one-question session behavior.

Code version: v1.1.4-codex.1
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
        "max_rounds": 3,
        "round": 1,
        "providers": [{"key": key, "ready": True} for key in providers],
        "rounds": [{
            "round": 1,
            "opinions": [{
                "provider": key,
                "verdict": "supported" if key == "chatgpt" else "unverified",
                "conclusion": "Primary evidence requires closer inspection.",
                "response": "Original provider response.",
                "conversation_url": f"https://{key}.com/c/one",
                "evidence": [{"url": "https://example.com/original", "supports": "Original wording differs from the claim."}],
                "unresolved": ["Publication date does not establish an agreement."],
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
        page.locator('label[for="jury_provider_gemini"]').click()
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
        expect(page.locator("[data-jury-ready-check]")).to_be_visible()
        assert checks[-1]["providers"] == list(providers)
        if width <= 900:
            page.locator("#sidebar_toggle").click()
        page.locator("[data-jury-prompt]").fill(current["question"])
        page.locator("[data-jury-submit]").click()
        expect(page.locator("[data-jury-submit]")).to_have_attribute("aria-label", "Stop Jury")
        expect(page.locator(".jury-opinion")).to_have_count(len(providers))
        expect(page.locator(".jury-evidence").first).to_contain_text("Original wording differs")
        expect(page.locator(".jury-unresolved").first).to_contain_text("Publication date")
        expect(page.locator(".jury-evidence a").first).to_have_attribute("href", "https://example.com/original")
        assert page.locator("[data-jury-provider]").evaluate_all("inputs => inputs.every(input => input.disabled)")
        next_round = deepcopy(current["rounds"][0])
        next_round["round"] = 2
        next_round["opinions"][0]["conclusion"] = '<img src=x onerror="window.juryInjected=true">'
        next_round["opinions"][0]["evidence"].append({"url": "javascript:alert(1)", "supports": "Unsafe source is plain text."})
        current.update(
            running=False, phase="inconclusive", round=2,
            rounds=[current["rounds"][0], next_round],
            response="The available evidence does not establish the claim.",
        )
        expect(page.locator(".jury-round")).to_have_count(2, timeout=7000)
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
        assert page.locator(".jury-round img").count() == 0
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
        assert starts[0]["max_rounds"] == 3
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
        expect(page.locator("[data-jury-ready-check]")).to_be_visible()
        assert checks[-1]["browser"] == selected_key
        page.locator('[data-dock-section="settings"]').click()
        expect(page.locator('[data-dock-section="agent"]')).to_have_attribute("href", f"/jury/{selected_key}")
        page.locator('[data-dock-section="agent"]').click()
        expect(page).to_have_url(f"{sidebar_server_url}/jury/{selected_key}")
        expect(page.locator('[aria-label="Agent modes"] [aria-current="page"]')).to_have_text("Jurors")
        page.get_by_role("radio", name="Agentic", exact=True).click()
        expect(page).to_have_url(re.compile(rf"{re.escape(sidebar_server_url)}/agent/{selected_key}/[^/?]+"))
        expect(page.locator('[aria-label="Agent modes"]')).to_have_class(
            re.compile(r"\bsegmented-control\b")
        )
        expect(page.locator('[aria-label="Agent modes"] [aria-current="page"]')).to_have_text("Agentic")
        page.get_by_role("radio", name="Jurors", exact=True).click()
        expect(page).to_have_url(f"{sidebar_server_url}/jury/{selected_key}")
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
    finally:
        context.close()
