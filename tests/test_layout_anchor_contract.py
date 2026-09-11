"""Static checks for the cross-project spatial layout contract.

Code version: v0.2.0-codex.1
"""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STYLE_PATH = PROJECT_ROOT / "app/web/static/style.css"
TEMPLATE_ROOT = PROJECT_ROOT / "app/web/templates"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_shared_dimensions_and_safe_area_anchors_are_tokenized() -> None:
    stylesheet = _read(STYLE_PATH)

    for fragment in (
        "--layout-content-width: 640px;",
        "--layout-control-width: 384px;",
        "--layout-physical-effect-bleed: 48px;",
        "--layout-glass-border-width: 1px;",
        "--page-edge-pad: 10px;",
        "--layout-edge-gap: var(--page-edge-pad);",
        "--layout-page-inset-top: max(var(--page-edge-pad), env(safe-area-inset-top, 0px));",
        "--layout-page-inset-right: max(var(--page-edge-pad), env(safe-area-inset-right, 0px));",
        "--layout-page-inset-bottom: max(var(--page-edge-pad), env(safe-area-inset-bottom, 0px));",
        "--layout-page-inset-left: max(var(--page-edge-pad), env(safe-area-inset-left, 0px));",
        "--layout-global-anchor-top: calc(var(--layout-page-inset-top) + var(--layout-edge-gap));",
        "--layout-global-anchor-right: calc(var(--layout-page-inset-right) + var(--layout-edge-gap));",
        "--layout-global-anchor-bottom: calc(var(--layout-page-inset-bottom) + var(--layout-edge-gap));",
        "--layout-global-anchor-left: calc(var(--layout-page-inset-left) + var(--layout-edge-gap));",
        "--layout-global-anchor-inset: calc(var(--layout-edge-gap) * 2);",
        "--global-quick-actions-top: var(--layout-global-anchor-top);",
        "--global-quick-actions-right: var(--layout-global-anchor-right);",
        "--settings-general-option-max-width: var(--layout-content-width);",
        "--settings-form-control-max-width: var(--layout-control-width);",
        "--style-token-demo-width: var(--layout-control-width);",
        "--sidebar-shell-width: 312px;",
        "--sidebar-width: var(--sidebar-shell-width);",
        "--sidebar-shell-padding:",
        "--sidebar-shell-overlay-padding:",
        "--sidebar-shell-background:",
        "--sidebar-shell-border:",
        "--sidebar-shell-shadow:",
        "--sidebar-shell-blur: saturate(160%) blur(18px);",
    ):
        assert fragment in stylesheet


def test_effect_hosts_and_explicit_scrollports_do_not_mix_ownership() -> None:
    stylesheet = _read(STYLE_PATH)

    chart_host_start = stylesheet.index(".chart-panel.workspace {")
    chart_host = stylesheet[chart_host_start : stylesheet.index("\n}", chart_host_start)]
    assert "overflow: visible;" in chart_host

    settings_start = stylesheet.index(".settings-content-scrollport {")
    settings_scrollport = stylesheet[settings_start : stylesheet.index("\n}", settings_start)]
    for fragment in (
        "margin-inline-start: calc(-1 * var(--layout-physical-effect-bleed));",
        "padding-inline-start: var(--layout-physical-effect-bleed);",
        "padding-block-end: var(--layout-physical-effect-bleed);",
        "overflow-x: hidden;",
        "overflow-y: auto;",
    ):
        assert fragment in settings_scrollport

    cache_start = stylesheet.index(".cache-workspace-content {")
    cache_scrollport = stylesheet[cache_start : stylesheet.index("\n}", cache_start)]
    for fragment in (
        "padding-block-end: var(--layout-physical-effect-bleed);",
        "overflow-x: hidden;",
        "overflow-y: auto;",
        "scroll-padding-block-end: var(--layout-physical-effect-bleed);",
    ):
        assert fragment in cache_scrollport

    for fragment in (
        ".settings-action-package,\n.cache-common-config {\n    overflow: visible;",
        ".settings-category-panel > .shadow-backup-section,",
        ".settings-action-package {\n    position: relative;",
    ):
        assert fragment in stylesheet

    browser_start = stylesheet.index(".browser-content-card {")
    browser_scrollport = stylesheet[browser_start : stylesheet.index("\n}", browser_start)]
    assert "overflow-x: hidden;" in browser_scrollport
    assert "overflow-y: auto;" in browser_scrollport


def test_production_templates_publish_shared_layout_roles() -> None:
    theme_toggle = _read(TEMPLATE_ROOT / "_theme_toggle.html")
    dock = _read(TEMPLATE_ROOT / "_sidebar_dock.html")

    for fragment in (
        'data-layout-role="global-action-column"',
        'data-layout-role="global-theme-anchor"',
    ):
        assert fragment in theme_toggle
    assert 'data-layout-role="sidebar-dock"' in dock

    for template_name in (
        "_cache_page.html",
        "agent.html",
        "beta.html",
        "browser.html",
        "settings.html",
        "settings_style_tokens.html",
    ):
        template = _read(TEMPLATE_ROOT / template_name)
        for fragment in (
            'data-layout-role="sidebar-shell"',
            'data-layout-role="sidebar-toggle"',
            'data-layout-role="sidebar-title"',
            'data-layout-role="title-rail"',
            'data-layout-role="title-heading"',
        ):
            assert fragment in template, (template_name, fragment)

    assert 'data-layout-role="result-container"' in _read(TEMPLATE_ROOT / "_cache_page.html")
    assert 'data-layout-role="content-scrollport"' in _read(TEMPLATE_ROOT / "_cache_page.html")
    assert 'data-layout-role="content-scrollport"' in _read(TEMPLATE_ROOT / "browser.html")
    assert 'data-layout-role="content-scrollport"' in _read(TEMPLATE_ROOT / "settings.html")
    assert 'data-layout-role="content-scrollport"' in _read(TEMPLATE_ROOT / "settings_style_tokens.html")
    assert 'data-layout-role="pagination"' in _read(TEMPLATE_ROOT / "_pagination.html")
