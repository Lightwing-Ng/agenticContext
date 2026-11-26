"""Verify Playwright's native Windows TCP compatibility in real Chromium.

Code version: v1.1.0-codex.0
"""

from playwright.sync_api import sync_playwright

def test_disposable_chromium_disables_tcp_port_randomization():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--enable-automation"])
        try:
            session = browser.new_browser_cdp_session()
            try:
                arguments = session.send("Browser.getBrowserCommandLine")["arguments"]
            finally:
                session.detach()
        finally:
            browser.close()

    feature_switches = [
        argument.partition("=")[2]
        for argument in arguments
        if argument.startswith("--disable-features=")
    ]
    assert len(feature_switches) == 1
    assert feature_switches[0].split(",").count("TcpPortRandomizationWin") == 1
