"""Responsive sidebar contract tests.

Code version: v1.1.1-codex.1
"""

from __future__ import annotations

from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STYLE_PATH = PROJECT_ROOT / "app/web/static/style.css"
RESPONSIVE_SCRIPT_PATH = PROJECT_ROOT / "app/web/static/responsive.js"
SIDEBAR_SCRIPT_PATH = PROJECT_ROOT / "app/web/static/sidebar.js"
STATIC_SCRIPT_ROOT = PROJECT_ROOT / "app/web/static"
TEMPLATE_ROOT = PROJECT_ROOT / "app/web/templates"
EXPECTED_BREAKPOINTS = {
    "compact-content-max": 600,
    "sidebar-overlay-max": 900,
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract_block(source: str, marker: str) -> str:
    start = source.index(marker)
    opening_brace = source.index("{", start)
    depth = 0
    for index in range(opening_brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"Unclosed CSS block: {marker}")


def test_css_and_javascript_share_one_semantic_sidebar_registry() -> None:
    stylesheet = _read(STYLE_PATH)
    responsive_script = _read(RESPONSIVE_SCRIPT_PATH)
    css_registry = {
        name: int(value)
        for name, value in re.findall(
            r"--responsive-breakpoint-([a-z0-9-]+):\s*([0-9]+)px;",
            stylesheet,
        )
    }

    assert {name: css_registry[name] for name in EXPECTED_BREAKPOINTS} == EXPECTED_BREAKPOINTS
    assert "compactContentMax: 600," in responsive_script
    assert "sidebarOverlayMax: 900," in responsive_script
    assert '"--responsive-breakpoint-compact-content-max"' in responsive_script
    assert '"--responsive-breakpoint-sidebar-overlay-max"' in responsive_script
    assert "window.CACHELIKES_RESPONSIVE = Object.freeze" in responsive_script

    direct_width_query = re.compile(r"matchMedia\(\s*[\"'`][^\"'`]*(?:min|max)-width")
    for script_path in STATIC_SCRIPT_ROOT.glob("*.js"):
        if script_path == RESPONSIVE_SCRIPT_PATH:
            continue
        assert direct_width_query.search(_read(script_path)) is None, script_path


def test_sidebar_overlay_and_compact_content_are_independent() -> None:
    stylesheet = _read(STYLE_PATH)
    overlay_block = _extract_block(stylesheet, "@media (max-width: 900px)")
    compact_block = _extract_block(stylesheet, "@media (max-width: 600px)")

    for fragment in (
        ".sidebar {",
        "z-index: auto;",
        "position: fixed;",
        "top: var(--sidebar-overlay-inset-top);",
        "bottom: var(--sidebar-overlay-inset-bottom);",
        ".sidebar-backdrop {",
        ".sidebar-backdrop:not([hidden]) {",
        "pointer-events: auto;",
        ".page > .sidebar-toggle {",
        "width: var(--settings-round-icon-button-size);",
        "height: var(--settings-round-icon-button-size);",
        "z-index: var(--layer-sidebar-toggle);",
    ):
        assert fragment in overlay_block

    assert "--sidebar-toggle-x: calc(" in overlay_block
    assert "--sidebar-overlay-toggle-inset" in overlay_block
    assert "--layout-global-action-inline-size: var(--settings-round-icon-button-size);" in overlay_block
    assert "--layout-sidebar-overlay-inline-size: min(" in overlay_block
    assert "top: var(--global-quick-actions-top);" in overlay_block

    for mobile_content_fragment in (
        ".workspace-grid {",
        ".metric-grid {",
        ".progress-metric-grid {",
    ):
        assert mobile_content_fragment not in overlay_block
        assert mobile_content_fragment in compact_block

    workspace_grid = _extract_block(compact_block, ".workspace-grid {")
    assert "grid-template-columns: 1fr;" in workspace_grid
    assert "grid-template-rows: auto auto minmax(0, 1fr);" in workspace_grid
    for selector in (".metric-grid {", ".progress-metric-grid {"):
        metric_grid = _extract_block(compact_block, selector)
        assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in metric_grid

    assert "@media (max-width: 720px)" not in stylesheet


def test_hidden_and_pointer_event_contracts_are_authoritative() -> None:
    stylesheet = _read(STYLE_PATH)

    assert "[hidden] {\n    display: none !important;\n}" in stylesheet
    assert ".sidebar-backdrop {\n    display: none;\n    visibility: hidden;\n    pointer-events: none;\n}" in stylesheet
    assert "html.sidebar-memory-collapsed .app-shell .sidebar {" in stylesheet
    assert "html.sidebar-memory-collapsed .sidebar-backdrop {" in stylesheet


def test_reduced_motion_makes_sidebar_geometry_synchronous() -> None:
    """Keep touch geometry deterministic when the browser requests reduced motion."""
    stylesheet = _read(STYLE_PATH)
    reduced_motion_marker = "@media (prefers-reduced-motion: reduce) {\n    :root"
    reduced_motion_start = stylesheet.index(reduced_motion_marker)
    reduced_motion_block = _extract_block(
        stylesheet[reduced_motion_start:],
        reduced_motion_marker,
    )

    assert "--sidebar-motion-duration: 0ms;" in reduced_motion_block
    assert ".app-shell," in reduced_motion_block
    assert ".sidebar," in reduced_motion_block
    assert ".sidebar-toggle {" in reduced_motion_block
    assert "transition: none !important;" in reduced_motion_block
    assert "[data-sidebar-gel-content] {" in reduced_motion_block
    assert "animation: none !important;" in reduced_motion_block
    assert "transform: none !important;" in reduced_motion_block


def test_sidebar_gel_contract_uses_shared_physics_without_layout_mutation() -> None:
    """Pin the cross-project content boundary, lifecycle, and soft-body curve."""
    stylesheet = _read(STYLE_PATH)
    sidebar_script = _read(SIDEBAR_SCRIPT_PATH)

    for token in (
        '"workspace-sidebar-gel-open"',
        '"workspace-sidebar-gel-close"',
        'const sidebarGelTargetSelector = "[data-sidebar-gel-content]";',
        'target.setAttribute("data-sidebar-gel-content", "")',
        "sidebarOverlayMedia.matches",
        "reducedMotionMedia.matches",
        'appShell.addEventListener("animationend", sidebarMotionEndHandler);',
        "clearSidebarMotionState();",
    ):
        assert token in sidebar_script

    for token in (
        ".app-shell.is-sidebar-animating [data-sidebar-gel-content] {",
        "animation-duration: var(--sidebar-motion-duration);",
        "animation-timing-function: var(--motion-bouncy);",
        "transform-origin: left top;",
        "@keyframes workspace-sidebar-gel-open",
        "@keyframes workspace-sidebar-gel-close",
        "translate3d(12px, 0, 0) scale3d(0.984, 1.024, 1)",
        "translate3d(-5px, 0, 0) scale3d(1.01, 0.992, 1)",
        "translate3d(2px, 0, 0) scale3d(0.997, 1.004, 1)",
    ):
        assert token in stylesheet

    gel_target_block = stylesheet.split(
        "/*\n * Apply shared soft-body physics only to registered content layers",
        1,
    )[1].split(".hero,", 1)[0]
    for forbidden_layout_rule in (
        "grid-template-columns",
        "padding:",
        "position: absolute",
        "transform-origin: left center",
        "width:",
    ):
        assert forbidden_layout_rule not in gel_target_block

    assert ".app-shell.is-sidebar-animating .workspace-mobile-summary-shell" not in stylesheet


def test_sidebar_pages_load_the_responsive_contract_before_bootstrap() -> None:
    for template_name in (
        "_cache_page.html",
        "browser.html",
        "settings.html",
        "settings_style_tokens.html",
    ):
        source = _read(TEMPLATE_ROOT / template_name)
        assert "viewport-fit=cover" in source
        assert "responsive-v1.0.0-codex.1" in source
        assert '{% include "_sidebar_bootstrap.html" %}' in source
        assert source.index("responsive-v1.0.0-codex.1") < source.index(
            '{% include "_sidebar_bootstrap.html" %}'
        )

    bootstrap_source = _read(TEMPLATE_ROOT / "_sidebar_bootstrap.html")
    assert 'window.sessionStorage.getItem("cachelikes:sidebar-open")' in bootstrap_source
    assert 'window.CACHELIKES_RESPONSIVE?.media?.("sidebarOverlayMax")' in bootstrap_source
    assert 'document.documentElement.classList.add("sidebar-memory-collapsed")' in bootstrap_source


def test_sidebar_pages_render_a_closed_accessibility_state_before_bootstrap() -> None:
    for template_name in (
        "_cache_page.html",
        "agent.html",
        "browser.html",
        "settings.html",
        "settings_style_tokens.html",
    ):
        source = _read(TEMPLATE_ROOT / template_name)
        toggle_start = source.index('id="sidebar_toggle"')
        toggle_end = source.index(">", toggle_start)
        toggle_markup = source[toggle_start:toggle_end]
        assert 'aria-expanded="false"' in toggle_markup, template_name

        backdrop_start = source.index('id="sidebar_backdrop"')
        backdrop_end = source.index(">", backdrop_start)
        backdrop_markup = source[backdrop_start:backdrop_end]
        assert "hidden" in backdrop_markup, template_name


def test_sidebar_toggle_is_outside_the_shell_stacking_context() -> None:
    stylesheet = _read(STYLE_PATH)
    for template_name in (
        "_cache_page.html",
        "browser.html",
        "settings.html",
        "settings_style_tokens.html",
    ):
        source = _read(TEMPLATE_ROOT / template_name)
        toggle_index = source.index('id="sidebar_toggle"')
        shell_index = source.index('class="app-shell')
        assert toggle_index < shell_index, template_name

    assert ".page > .sidebar-toggle" in stylesheet


def test_coarse_pointer_sidebar_toggle_keeps_its_hit_target_stationary() -> None:
    stylesheet = _read(STYLE_PATH)
    touch_block = _extract_block(
        stylesheet,
        "@media (hover: none) and (pointer: coarse)",
    )

    assert ".page > .sidebar-toggle:hover" in touch_block
    assert ".page > .sidebar-toggle:focus-visible" in touch_block
    assert ".page > .sidebar-toggle:active" in touch_block
    assert "width: 44px;" in touch_block
    assert "min-width: 44px;" in touch_block
    assert "height: 44px;" in touch_block
    assert "min-height: 44px;" in touch_block
    assert "touch-action: manipulation;" in touch_block
    assert "z-index: var(--layer-sidebar-toggle);" in touch_block
    assert "transform: translate3d(var(--sidebar-toggle-x), 0, 0);" in touch_block
    assert "transition: background 160ms var(--motion-standard), box-shadow 160ms var(--motion-standard), color 160ms var(--motion-standard);" in touch_block
