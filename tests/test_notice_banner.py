"""Regression coverage for the shared floating-banner contract. Code version: v0.1.1-codex.1."""

from pathlib import Path

from app.web.app import create_app


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = PROJECT_ROOT / "app/web/templates"
STATIC_ROOT = PROJECT_ROOT / "app/web/static"


def test_cache_status_stays_in_the_inline_progress_panel() -> None:
    with create_app().test_client() as client:
        response = client.get("/cache/chatgpt")

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    cache_template = (TEMPLATE_ROOT / "_cache_page.html").read_text(encoding="utf-8")
    assert "render_notice_banner" not in cache_template
    assert "notice-banner.js" not in cache_template
    assert 'id="status_banner"' not in body
    assert 'id="banner_message"' not in body
    assert 'id="message" data-status-field="message"' in body


def test_browser_refresh_banner_reuses_the_shared_macro_and_controller() -> None:
    with create_app().test_client() as client:
        response = client.get("/browser")

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert '{% call render_notice_banner(' in (TEMPLATE_ROOT / "browser.html").read_text(encoding="utf-8")
    assert 'data-chatgpt-session-refresh-banner' in body
    assert 'data-session-refresh-dismiss' in body
    assert 'data-session-refresh-title' in body
    assert 'data-session-refresh-copy' in body
    assert 'src="/static/notice-banner.js?v=notice-banner-v0.1.0-codex.1"' in body


def test_banner_styles_follow_the_sibling_top_aligned_contract() -> None:
    stylesheet = (STATIC_ROOT / "style.css").read_text(encoding="utf-8")

    for token in (
        ".notice-floating-banner {",
        "display: grid !important;",
        "var(--workspace-modal-close-size)",
        "var(--workspace-modal-icon-size)",
        "minmax(0, 1fr)",
        "align-items: start !important;",
        ".notice-floating-banner-content {",
        ".notice-floating-banner-copy {",
        "list-style-position: outside;",
        ".notice-floating-banner .notice-close {",
        ".icon-dismiss-control {",
        "transform: translate3d(-50%, 0, 0);",
    ):
        assert token in stylesheet


def test_banner_surface_reuses_the_modal_dialog_material_and_close_contract() -> None:
    """Keep Cache banners on the sibling Style tokens material and close treatment."""
    stylesheet = (STATIC_ROOT / "style.css").read_text(encoding="utf-8")
    floating_start = stylesheet.index(".notice-floating {")
    floating_rule = stylesheet[floating_start:stylesheet.index("\n}", floating_start)]
    dismiss_start = stylesheet.index(".dismiss-button {")
    dismiss_rule = stylesheet[dismiss_start:stylesheet.index("\n}", dismiss_start)]
    dismiss_state_start = stylesheet.index(".dismiss-button:hover,")
    dismiss_state_rule = stylesheet[dismiss_state_start:stylesheet.index("\n}", dismiss_state_start)]

    assert "background: var(--notice-floating-material);" in floating_rule
    assert "box-shadow: var(--frosted-glass-shadow);" in floating_rule
    assert "backdrop-filter: var(--frosted-glass-blur);" in floating_rule
    assert "border: var(--frosted-glass-border);" in floating_rule
    assert "border: 0;" in dismiss_rule
    assert "background: transparent;" in dismiss_state_rule
    assert "box-shadow: none;" in dismiss_state_rule


def test_shared_notice_controller_persists_only_opt_in_notice_state() -> None:
    script = (STATIC_ROOT / "notice-banner.js").read_text(encoding="utf-8")

    assert 'document.querySelectorAll("[data-dismissible-notice]")' in script
    assert 'window.sessionStorage.getItem(storageKey)' in script
    assert 'window.sessionStorage.setItem(storageKey, String(notice.hidden))' in script
