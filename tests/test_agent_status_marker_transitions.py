"""Agent lifecycle markers commit their reduced-motion geometry immediately.

Code version: v1.0.0-codex.0
"""

import pytest

from tests import test_sidebar_e2e as fixtures


disposable_browser = fixtures.disposable_browser
sidebar_server_url = fixtures.sidebar_server_url


@pytest.mark.parametrize("motion", ["no-preference", "reduce"])
def test_status_marker_switches_between_loading_and_running_without_geometry_transitions(
    disposable_browser, sidebar_server_url, motion
):
    context = disposable_browser.new_context(reduced_motion=motion)
    page = context.new_page()
    page.route(
        "**/__agent_marker_fixture__",
        lambda route: route.fulfill(
            content_type="text/html",
            body=(
                '<!doctype html><html lang="en"><head>'
                f'<link rel="stylesheet" href="{sidebar_server_url}/static/style.css">'
                '</head><body><article class="agent-response-card">'
                '<p class="agent-response-status" data-status="loading">'
                '<span class="agent-response-status-indicator">'
                '<span id="marker" class="agent-response-status-spinner '
                'suggestion-loading-spinner"></span>'
                '</span></p></article></body></html>'
            ),
        ),
    )
    try:
        page.goto(sidebar_server_url + "/__agent_marker_fixture__", wait_until="load")
        states = page.locator("#marker").evaluate(
            """marker => {
                const sample = () => {
                    const style = getComputedStyle(marker);
                    return {
                        width: Number.parseFloat(style.width),
                        height: Number.parseFloat(style.height),
                        flexBasis: Number.parseFloat(style.flexBasis),
                        transitions: marker.getAnimations().filter(animation =>
                            animation instanceof CSSTransition
                            && ['width', 'height', 'flex-basis'].includes(
                                animation.transitionProperty
                            )
                        ).map(animation => animation.transitionProperty),
                    };
                };
                const loading = sample();
                marker.closest('[data-status]').dataset.status = 'running';
                marker.classList.replace('suggestion-loading-spinner', 'cache-phase-live-marker');
                const running = sample();
                marker.closest('[data-status]').dataset.status = 'loading';
                marker.classList.replace('cache-phase-live-marker', 'suggestion-loading-spinner');
                return {loading, running, restoredLoading: sample()};
            }"""
        )
        for name, size in (("loading", 16), ("running", 6), ("restoredLoading", 16)):
            assert states[name] == {
                "width": size,
                "height": size,
                "flexBasis": size,
                "transitions": [],
            }
    finally:
        context.close()
