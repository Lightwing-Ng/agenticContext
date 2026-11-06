"""Controlled disclosure state preserves nested motion and native cleanup.

Code version: v1.0.0-codex.0
"""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests import test_sidebar_e2e


disposable_browser = test_sidebar_e2e.disposable_browser
SCRIPT = Path(__file__).resolve().parents[1] / "app/web/static/disclosure-motion.js"
HTML = """
<!doctype html><html lang="en-US"><style>
* { box-sizing: border-box; }
body { margin: 20px; }
.card { width: 200px; max-width: 100%; }
.card:has(> details[open]) { width: 480px; }
summary { min-height: 32px; padding: 4px; }
.ui-collapse-body { padding: 8px; }
.copy { margin: 0; height: 120px; }
</style><body>
<div class="card" id="outer-card"><details class="ui-collapse" id="outer">
<summary>Outer transcript</summary><div class="ui-collapse-body" id="outer-body">
<p class="copy">Outer content</p>
<div class="card" id="inner-card"><details class="ui-collapse" id="inner">
<summary>Inner transcript</summary><div class="ui-collapse-body" id="inner-body">
<button id="inner-action">Nested action</button><p class="copy">Inner content</p>
</div></details></div></div></details></div>
<div class="card" id="sibling-card"><details class="ui-collapse" id="sibling">
<summary>Separate transcript</summary><div class="ui-collapse-body" id="sibling-body">
<p class="copy">Independent content</p></div></details></div>
<details id="unbound"><summary>Native disclosure</summary><p>Native content</p></details>
</body></html>
"""


@pytest.fixture
def motion_page(disposable_browser):
    contexts = []

    def create(width=1280, *, reduced_motion="no-preference", initially_open=False):
        context = disposable_browser.new_context(
            viewport={"width": width, "height": 1000},
            reduced_motion=reduced_motion,
        )
        contexts.append(context)
        page = context.new_page()
        html = (
            HTML.replace('id="outer">', 'id="outer" open>') if initially_open else HTML
        )
        page.set_content(html)
        page.add_script_tag(path=str(SCRIPT))
        page.evaluate("""() => {
            const original = Element.prototype.animate;
            window.controlledMotions = [];
            Element.prototype.animate = function (...args) {
                const animation = original.apply(this, args);
                animation.pause();
                window.controlledMotions.push({target: this, animation});
                return animation;
            };
            SharedDisclosureMotion.enhance(document, {
                selector: 'details.ui-collapse', widthTarget: details => details.parentElement,
            });
        }""")
        return page

    yield create
    for context in contexts:
        context.close()


def _set_open(page, identifier, value, *, animate=False):
    page.evaluate(
        """({identifier, value, animate}) => SharedDisclosureMotion.setOpen(
            document.getElementById(identifier), value, {animate})""",
        {"identifier": identifier, "value": value, "animate": animate},
    )


def _state(page, identifier):
    return page.locator("#" + identifier).evaluate("""details => ({
        open: details.open,
        height: details.getBoundingClientRect().height,
        motion: details.dataset.collapseMotion || null,
        inert: details.querySelector(':scope > .ui-collapse-body').inert,
        expanded: details.querySelector(':scope > summary').getAttribute('aria-expanded'),
        active: window.controlledMotions.filter(({target, animation}) =>
            [details, details.parentElement, details.querySelector(':scope > .ui-collapse-body')]
                .includes(target) && !['idle', 'finished'].includes(animation.playState)).length,
    })""")


def _sample(page, identifier, fraction):
    page.evaluate(
        """async ({identifier, fraction}) => {
            const details = document.getElementById(identifier);
            const targets = [details, details.parentElement,
                details.querySelector(':scope > .ui-collapse-body')];
            for (const {target, animation} of window.controlledMotions) {
                if (targets.includes(target) && !['idle', 'finished'].includes(animation.playState)) {
                    animation.currentTime = animation.effect.getTiming().duration * fraction;
                }
            }
            await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        }""",
        {"identifier": identifier, "fraction": fraction},
    )
    return _state(page, identifier)


@pytest.mark.parametrize("width", [1280, 390])
def test_controlled_open_cancels_closing_and_restores_native_state(motion_page, width):
    page = motion_page(width)
    _set_open(page, "outer", True)
    page.locator("#outer > summary").press("Space")
    expect(page.locator("#outer")).to_have_attribute("data-collapse-motion", "close")
    closing = _sample(page, "outer", 0.2)
    assert closing["open"] and closing["inert"] and closing["active"] == 3
    _set_open(page, "outer", True)
    reopened = _state(page, "outer")
    assert reopened["open"]
    assert not reopened["inert"]
    assert reopened["motion"] is reopened["expanded"] is None
    assert reopened["active"] == 0
    _set_open(page, "outer", False)
    closed = _state(page, "outer")
    assert not closed["open"]
    assert closed["active"] == 0
    assert closed["motion"] is closed["expanded"] is None


@pytest.mark.parametrize("width", [1280, 390])
def test_child_motion_settles_only_conflicting_ancestor(motion_page, width):
    page = motion_page(width)
    page.locator("#sibling > summary").press("Enter")
    page.locator("#outer > summary").press("Enter")
    _sample(page, "outer", 0.5)
    page.locator("#inner > summary").press("Enter")
    outer = _state(page, "outer")
    assert outer["open"] and outer["active"] == 0 and outer["motion"] is None
    assert _state(page, "inner")["active"] == 3
    assert _state(page, "sibling")["active"] == 3
    before = outer["height"]
    _sample(page, "inner", 0.3)
    assert _state(page, "outer")["height"] > before + 10
    _set_open(page, "inner", True)
    assert _state(page, "outer")["active"] == 0
    assert _state(page, "sibling")["active"] == 3


@pytest.mark.parametrize("width", [1280, 390])
def test_parent_closing_starts_before_descendant_settlement(motion_page, width):
    page = motion_page(width)
    _set_open(page, "outer", True)
    page.locator("#inner > summary").press("Enter")
    _sample(page, "inner", 0.15)
    before = _state(page, "outer")["height"]
    page.locator("#outer > summary").press("Space")
    start = _sample(page, "outer", 0)
    assert start["height"] == pytest.approx(before, abs=1)
    assert start["motion"] == "close"
    assert _state(page, "inner")["active"] == 0
    _set_open(page, "outer", False)
    assert not _state(page, "outer")["open"]
    assert _state(page, "inner")["open"]


def test_instant_close_moves_focus_out_of_hidden_body_and_keeps_native_fallback(
    motion_page,
):
    page = motion_page()
    _set_open(page, "outer", True)
    _set_open(page, "inner", True)
    page.locator("#inner-action").focus()
    _set_open(page, "outer", False)
    expect(page.locator("#outer > summary")).to_be_focused()
    _set_open(page, "unbound", True)
    expect(page.locator("#unbound")).to_have_attribute("open", "")
    _set_open(page, "unbound", False)
    expect(page.locator("#unbound")).not_to_have_attribute("open", "")


def test_reduced_motion_and_initial_open_state_settle_immediately(motion_page):
    page = motion_page(reduced_motion="reduce", initially_open=True)
    assert _state(page, "outer")["open"]
    _set_open(page, "outer", False, animate=True)
    assert not _state(page, "outer")["open"]
    _set_open(page, "outer", True, animate=True)
    current = _state(page, "outer")
    assert current["open"] and current["active"] == 0
    assert current["motion"] is current["expanded"] is None
