"""Rendered Jury readiness, review evidence, and one-question session behavior.

Code version: v1.0.2-codex.1
"""

from copy import deepcopy
from pathlib import Path

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
        {"key": key, "ready": key not in unavailable, "message": "Signed in" if key not in unavailable else "Provider unavailable"}
        for key in payload["providers"]
    ]
    route.fulfill(json={"ready": all(item["ready"] for item in records), "providers": records})


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


def open_jury(jury_browser, server_url, width, checks, *, unavailable=()):
    context = jury_browser.new_context(
        viewport={"width": width, "height": 900},
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


@pytest.mark.parametrize("width", [1280, 390])
def test_jury_all_selected_accounts_gate_and_shared_responsive_sidebar(jury_browser, sidebar_server_url, width):
    checks = []
    page, context = open_jury(jury_browser, sidebar_server_url, width, checks, unavailable=("gemini",))
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        expect(page.locator('[aria-label="Agent modes"] a')).to_have_text(["Agentic", "Jurors"])
        expect(page.locator('[aria-label="Agent modes"] a[aria-current="page"]')).to_have_text("Jurors")
        expect(page.locator("[data-jury-provider]:checked")).to_have_count(3)
        expect(page.locator('[data-jury-provider][value="claude"]')).not_to_be_checked()
        expect(page.locator("[data-jury-check-label]")).to_have_text("Not ready")
        expect(page.locator("[data-jury-ready-check]")).to_be_hidden()
        expect(page.locator("#jury_sidebar")).not_to_contain_text("Terminal")
        expect(page.locator("#jury_sidebar")).not_to_contain_text("Current project")
        expect(page.locator('[data-jury-browser-trigger]')).to_have_attribute("aria-label", "Browser: Edge")
        Path("test-results").mkdir(exist_ok=True)
        if width == 1280:
            page.screenshot(path="test-results/jury-unavailable.png")
        page.locator('[data-jury-provider][value="gemini"]').uncheck()
        expect(page.locator("[data-jury-ready-check]")).to_be_visible()
        assert checks[-1] == {"browser": "edge", "providers": ["chatgpt", "grok"]}
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
            page.locator('[data-jury-provider][value="gemini"]').uncheck()
        else:
            expect(page.locator("#jury_model_gemini")).to_have_text("3.1 Pro")
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
