"""Inspect the ChatGPT tab inside a project debug Chromium over CDP.

Windows verified this against a live debug Chrome. Pass the current CDP HTTP
endpoint from that browser's `debug_port` or `DevToolsActivePort`. Do not reuse
a previous session's port.
"""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright


def main(argv: list[str]) -> int:
    endpoint = str(argv[1] if len(argv) > 1 else "").strip()
    if not endpoint:
        raise SystemExit(
            "Usage: python scripts/tmp_probe_chrome_tab.py http://127.0.0.1:<port>"
        )

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(endpoint)
        try:
            context = browser.contexts[0]
            page = context.pages[0]
            print("url:", page.url)
            print("title:", page.title())
            try:
                result = page.evaluate(
                    'async () => { const resp = await fetch("/api/auth/session", '
                    '{credentials:"include", cache:"no-store"}); '
                    "return {ok: resp.ok, status: resp.status}; }"
                )
                print("auth:", result)
            except Exception as exc:  # noqa: BLE001 - diagnostic script
                print("auth error:", exc)
        finally:
            browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
