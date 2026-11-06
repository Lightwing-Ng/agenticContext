"""Contact analysis uses verified identity and preserves same-tab chat navigation.

Code version: v1.4.1-codex.0
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import sqlite3
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from app.core.config import CrawlConfig
from app.core.wechat_archive import import_wechat_archive, wechat_archive_path
from app.core.wechat_contact_analysis import query_contact_analysis
from app.core.wechat_contacts import contact_registry_path, initialize_contact_registry
from app.core.wechat_display_names import display_names_path
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from tests import test_sidebar_e2e
from tests.test_wechat_archive import make_archive
from tests.test_wechat_contacts import bind, insert_contact


disposable_browser = test_sidebar_e2e.disposable_browser
VIEWER_NAME = "Research Contact"
TARGET = "wxid_peer"
OTHER = "wxid_other_person"


@pytest.fixture(scope="module")
def contact_analysis_server(tmp_path_factory):
    sandbox = tmp_path_factory.mktemp("wechat-contact-analysis-browser")
    archive = make_archive(sandbox / "archive", repeated=108)
    store = sandbox / "store"
    import_wechat_archive(archive, store)
    history = wechat_archive_path(store)
    with sqlite3.connect(history) as connection:
        connection.row_factory = sqlite3.Row
        sessions = {
            (row["account_key"], row["kind"]): dict(row)
            for row in connection.execute(
                "SELECT * FROM conversations WHERE account_key LIKE 'account:%'"
            )
        }
        source_session = sessions[("account:wxid_account_a", "group")]
        for (account, _kind), session in sessions.items():
            rows = connection.execute(
                "SELECT id,sender_id,ordinal FROM messages WHERE conversation_id=? ORDER BY ordinal",
                (session["id"],),
            ).fetchall()
            for index, row in enumerate(rows):
                target = row["sender_id"] == TARGET
                marker = "A" if account.endswith("account_a") else "B"
                text = (
                    f"{marker} target statement {row['ordinal']}"
                    if target
                    else "Owner statement"
                )
                label = "Flora" if target else "Source account owner"
                sender = row["sender_id"]
                if session["id"] == source_session["id"] and index == 64:
                    sender, label, text = (
                        OTHER,
                        "Flora",
                        "OTHER FLORA excluded statement",
                    )
                connection.execute(
                    "UPDATE messages SET sender_id=?,sender_label=?,text=?,search_text=? WHERE id=?",
                    (sender, label, text, text.casefold(), row["id"]),
                )
    registry = contact_registry_path(store)
    with sqlite3.connect(registry) as connection:
        initialize_contact_registry(connection)
        target_id = insert_contact(connection, TARGET)
        other_id = insert_contact(connection, OTHER)
        for owner in ("wxid_account_a", "wxid_account_b"):
            source = "ios-" + hashlib.md5(owner.encode()).hexdigest()
            bind(connection, target_id, source=source, sender=TARGET)
            bind(connection, other_id, source=source, sender=OTHER)
    names = display_names_path(store)
    names.write_text(
        json.dumps({"schema_version": 1, "contacts": {target_id: VIEWER_NAME}})
    )
    prepared = query_contact_analysis(store, target_id, background=False)
    assert prepared["status"] == "ready"
    assert prepared["total_count"] == 215
    before = (history.read_bytes(), registry.read_bytes())
    with patch(
        "app.web.app.SavedConfigStore.load",
        return_value=SavedConfigStore(CrawlConfig()),
    ):
        app = create_app(
            store,
            computer_use_settings_path=sandbox / "agent.json",
            computer_use_runtime_root=sandbox / "runtime",
            agent_external_operations_enabled=False,
        )
    app.config.update(TESTING=True)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            url=f"http://127.0.0.1:{server.server_port}",
            store=store,
            history=history,
            registry=registry,
            before=before,
            session=source_session["id"],
            target_id=target_id,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _duplicate_contact_server(tmp_path_factory, *, conflicting_times=False):
    sandbox = tmp_path_factory.mktemp("wechat-contact-duplicate-browser")
    archive = make_archive(sandbox / "archive", repeated=2)
    store = sandbox / "store"
    import_wechat_archive(archive, store)
    history = wechat_archive_path(store)
    originals = {}
    shared_text = "Shared native statement observed by two accounts."
    with sqlite3.connect(history) as connection:
        connection.row_factory = sqlite3.Row
        sessions = connection.execute(
            "SELECT * FROM conversations WHERE account_key LIKE 'account:%'"
        ).fetchall()
        for session in sessions:
            row = connection.execute(
                "SELECT id,provenance FROM messages WHERE conversation_id=? "
                "AND sender_id=? ORDER BY ordinal LIMIT 1",
                (session["id"], TARGET),
            ).fetchone()
            if session["kind"] != "group":
                text = session["account_key"] + " direct statement"
                connection.execute(
                    "UPDATE messages SET text=?,search_text=? WHERE id=?",
                    (text, text.casefold(), row["id"]),
                )
                continue
            connection.execute(
                "UPDATE conversations SET peer_id=?,title=? WHERE id=?",
                ("fixture-shared@chatroom", "Shared group", session["id"]),
            )
            provenance = json.loads(row["provenance"])
            provenance.update(
                message_type=1,
                server_id="7210500216981401697",
                unix_timestamp=1793339123
                + int(
                    conflicting_times and session["account_key"].endswith("account_b")
                ),
            )
            connection.execute(
                "UPDATE messages SET text=?,search_text=?,provenance=?,"
                "timestamp_value=?,timestamp_label=?,time_sort=? WHERE id=?",
                (
                    shared_text,
                    shared_text.casefold(),
                    json.dumps(provenance),
                    "2026-10-30T13:45:23+08:00",
                    "30 Oct 2026 13:45:23",
                    1793339123,
                    row["id"],
                ),
            )
            originals[row["id"]] = session["account_key"]
    registry = contact_registry_path(store)
    with sqlite3.connect(registry) as connection:
        initialize_contact_registry(connection)
        target_id = insert_contact(connection, TARGET)
        for owner in ("wxid_account_a", "wxid_account_b"):
            source = "ios-" + hashlib.md5(owner.encode()).hexdigest()
            bind(connection, target_id, source=source, sender=TARGET)
    prepared = query_contact_analysis(
        store, target_id, query=shared_text, background=False
    )
    assert prepared["status"] == "ready"
    assert prepared["total_count"] == 1
    assert prepared["counts"]["source_accounts"] == 2
    assert prepared["items"][0]["time_conflict"] is conflicting_times
    before = (history.read_bytes(), registry.read_bytes())
    with patch(
        "app.web.app.SavedConfigStore.load",
        return_value=SavedConfigStore(CrawlConfig()),
    ):
        app = create_app(
            store,
            computer_use_settings_path=sandbox / "agent.json",
            computer_use_runtime_root=sandbox / "runtime",
            agent_external_operations_enabled=False,
        )
    app.config.update(TESTING=True)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            url=f"http://127.0.0.1:{server.server_port}",
            store=store,
            history=history,
            registry=registry,
            before=before,
            target_id=target_id,
            originals=originals,
            shared_text=shared_text,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def duplicate_contact_analysis_server(tmp_path_factory):
    yield from _duplicate_contact_server(tmp_path_factory)


@pytest.fixture(scope="module")
def conflicting_duplicate_contact_analysis_server(tmp_path_factory):
    yield from _duplicate_contact_server(tmp_path_factory, conflicting_times=True)


@pytest.mark.parametrize(("width", "scheme"), [(788, "light"), (390, "dark")])
def test_merged_statement_shows_one_primary_link_and_retains_original_accounts(
    disposable_browser, duplicate_contact_analysis_server, tmp_path, width, scheme
):
    fixture = duplicate_contact_analysis_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1000},
        reduced_motion="reduce",
        color_scheme=scheme,
    )
    page = context.new_page()
    try:
        analysis_url = (
            fixture.url
            + "/browser/wechat/contacts/"
            + fixture.target_id
            + "?"
            + urlencode({"q": fixture.shared_text, "sort": "oldest"})
        )
        page.goto(analysis_url, wait_until="networkidle")
        if page.locator("#sidebar_toggle").get_attribute("aria-expanded") == "true":
            page.locator("#sidebar_toggle").click()
        rows = page.locator("tr[data-contact-statement-id]")
        expect(rows).to_have_count(1)
        expect(rows.locator(".wechat-contact-statement")).to_have_text(
            fixture.shared_text
        )
        metrics = page.locator('[aria-label="Contact statement totals"] strong')
        expect(metrics).to_have_text(["1", "1", "2"])
        sources = rows.locator("details[data-wechat-contact-sources]")
        expect(sources).to_have_count(1)
        expect(sources).not_to_have_attribute("open", "")
        expect(sources.locator(".ui-collapse-body")).not_to_be_visible()
        expect(
            rows.locator(".wechat-contact-statement-entry.has-sources")
        ).to_have_count(1)
        observations = sources.locator("[data-wechat-contact-observation]")
        expect(observations).to_have_count(2)
        expect(rows).not_to_contain_text("2 sources")
        expect(sources.locator("summary")).to_have_attribute(
            "title", "Show original sources · 2 sources"
        )
        closed_height = rows.bounding_box()["height"]
        prepared = query_contact_analysis(
            fixture.store,
            fixture.target_id,
            query=fixture.shared_text,
            background=False,
        )
        (primary,) = prepared["items"]
        assert primary["observation_count"] == 2
        assert {item["id"] for item in primary["observations"]} == set(
            fixture.originals
        )
        assert {item["account_key"] for item in primary["observations"]} == set(
            fixture.originals.values()
        )
        expect(rows.locator("a[data-wechat-contact-statement]")).to_have_attribute(
            "href", f"/browser/wechat/messages/{primary['id']}"
        )
        sources.locator("summary").click()
        expect(sources).to_have_attribute("open", "")
        for origin in primary["observations"]:
            observation = sources.locator(
                f'[data-wechat-contact-observation="{origin["id"]}"]'
            )
            expect(observation).to_be_visible()
            expect(observation).to_have_attribute(
                "data-contact-account-key", origin["account_key"]
            )
            expect(
                observation.locator("a[data-wechat-contact-source]")
            ).to_have_attribute("href", f"/browser/wechat/messages/{origin['id']}")
        assert rows.bounding_box()["height"] > closed_height + 10
        sources.locator("summary").click()
        expect(sources).not_to_have_attribute("open", "")
        expect(sources.locator(".ui-collapse-body")).not_to_be_visible()
        assert abs(rows.bounding_box()["height"] - closed_height) <= 1
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=tmp_path / f"contact-unique-sources-{width}-{scheme}.png")
        rows.locator("a[data-wechat-contact-statement]").click()
        page.wait_for_url("**/browser?**", wait_until="networkidle")
        assert len(context.pages) == 1
        assert parse_qs(urlsplit(page.url).query)["account"] == [primary["account_key"]]
        expect(page.locator(f"#wechat_message_{primary['id']}")).to_be_visible()
        assert fixture.before == (
            fixture.history.read_bytes(),
            fixture.registry.read_bytes(),
        )
    finally:
        context.close()


def _chat_position(page):
    return page.locator("[data-chat-scrollport]").evaluate(
        """port => {
        const bounds = port.getBoundingClientRect();
        const anchor = [...port.querySelectorAll('[data-chat-message-id][id]')]
            .find(message => message.getBoundingClientRect().bottom > bounds.top + port.clientTop + 1);
        return {top: port.scrollTop, left: port.scrollLeft, anchor: anchor.id,
            offset: anchor.getBoundingClientRect().top - bounds.top};
    }"""
    )


def _assert_analysis_summary_and_pagination_geometry(page, evidence_path):
    geometry = page.locator("[data-contact-analysis]").evaluate(
        """analysis => {
        const cards = [...analysis.querySelectorAll('.browser-metric-grid > .metric-card')]
            .map(card => {const box = card.getBoundingClientRect();
                return {top: box.top, left: box.left, right: box.right, width: box.width};});
        const pagination = analysis.querySelector('.local-store-pagination');
        const active = pagination.querySelector('[aria-current="page"]');
        const indicator = pagination.querySelector('.local-store-pagination-indicator');
        const activeBox = active.getBoundingClientRect();
        const indicatorBox = indicator.getBoundingClientRect();
        return {cards,
            indicatorCenterError: Math.abs((activeBox.left + activeBox.right
                - indicatorBox.left - indicatorBox.right) / 2),
            indicatorVerticalError: Math.abs((activeBox.top + activeBox.bottom
                - indicatorBox.top - indicatorBox.bottom) / 2)};
    }"""
    )
    evidence_path.write_text(json.dumps(geometry, indent=2) + "\n")
    cards = geometry["cards"]
    assert len(cards) == 3, geometry
    assert (
        max(card["top"] for card in cards) - min(card["top"] for card in cards) <= 1
    ), geometry
    assert all(card["width"] > 0 for card in cards), geometry
    assert all(
        left["right"] <= right["left"] + 1 for left, right in zip(cards, cards[1:])
    ), geometry
    assert geometry["indicatorCenterError"] <= 1, geometry
    assert geometry["indicatorVerticalError"] <= 1, geometry


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_contact_link_aggregates_verified_accounts_and_back_restores_chat(
    disposable_browser, contact_analysis_server, tmp_path, width, color_scheme
):
    fixture = contact_analysis_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1000},
        reduced_motion="reduce",
        color_scheme=color_scheme,
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    source_url = (
        fixture.url
        + "/browser?"
        + urlencode(
            {
                "view": "text",
                "source": "wechat",
                "account": "account:wxid_account_a",
                "chat_kind": "group",
                "session": fixture.session,
                "session_page": 7,
                "q": "statement",
                "sort": "oldest",
            }
        )
    )
    try:
        page.goto(source_url, wait_until="networkidle")
        toggle = page.locator("#sidebar_toggle")
        if toggle.get_attribute("aria-expanded") == "true":
            toggle.click()
        expect(toggle).to_have_attribute("aria-expanded", "false")
        expect(page.locator(".wechat-message")).to_have_count(100)
        page.evaluate("() => document.fonts.ready")
        link = (
            page.locator("a[data-wechat-contact-link]")
            .filter(has_text=VIEWER_NAME)
            .nth(30)
        )
        expect(link).not_to_have_attribute("target", "_blank")
        link.scroll_into_view_if_needed()
        article_id = link.evaluate("link => link.closest('article').id")
        summary = page.locator(f"#{article_id} > details > summary")
        summary.focus()
        summary.press("Enter")
        expect(page.locator(f"#{article_id} > details")).to_have_attribute("open", "")
        link.scroll_into_view_if_needed()
        page.evaluate(
            "history.replaceState({unrelated: {preserve: 'existing state'}}, '')"
        )
        position = _chat_position(page)
        assert position["top"] > 1000
        original_url = page.url
        link.click()
        page.wait_for_url("**/browser/wechat/contacts/**", wait_until="networkidle")
        assert len(context.pages) == 1
        expect(page.locator("[data-contact-analysis]")).to_be_visible()
        expect(
            page.get_by_role("heading", name=VIEWER_NAME, exact=True)
        ).to_be_visible()
        rows = page.locator(".wechat-contact-table tbody tr[data-contact-message-id]")
        expect(rows).to_have_count(50)
        assert "OTHER FLORA" not in "\n".join(rows.all_text_contents())
        assert (
            page.locator("[data-contact-analysis] .browser-chat-message").count() == 0
        )
        assert page.locator(".wechat-contact-table thead th").count() >= 4
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        _assert_analysis_summary_and_pagination_geometry(
            page, tmp_path / "contact-analysis-geometry.json"
        )
        page.screenshot(path=tmp_path / f"contact-analysis-{width}-{color_scheme}.png")

        page.go_back(wait_until="networkidle")
        expect(page).to_have_url(original_url)
        page.wait_for_function(
            """expected => Math.abs(document.querySelector('[data-chat-scrollport]').scrollTop - expected) <= 2""",
            arg=position["top"],
        )
        restored = _chat_position(page)
        assert restored["anchor"] == position["anchor"]
        assert abs(restored["offset"] - position["offset"]) <= 2
        expect(page.locator(f"#{article_id} > details")).to_have_attribute("open", "")
        assert page.evaluate("history.state.unrelated.preserve") == "existing state"
        # Keyboard activation follows the same native navigation and return path.
        link = page.locator(f"#{article_id} a[data-wechat-contact-link]")
        link.focus()
        link.press("Enter")
        page.wait_for_url("**/browser/wechat/contacts/**", wait_until="networkidle")
        assert len(context.pages) == 1
        search = page.get_by_role("searchbox", name="Search contact statements")
        search.fill("B target statement")
        search.press("Enter")
        page.wait_for_load_state("networkidle")
        rows = page.locator(".wechat-contact-table tbody tr[data-contact-message-id]")
        expect(rows.first).to_contain_text("B target statement")
        assert set(
            rows.evaluate_all("rows => rows.map(row => row.dataset.contactAccountKey)")
        ) == {"account:wxid_account_b"}
        assert not errors
        assert fixture.before == (
            fixture.history.read_bytes(),
            fixture.registry.read_bytes(),
        )
    finally:
        context.close()


def _large_contact_page_result(fixture):
    """Exercise distant page controls without manufacturing 80,000 source records."""
    original = query_contact_analysis(
        fixture.store,
        fixture.target_id,
        query="target statement",
        sort="oldest",
        background=False,
    )
    assert original["status"] == "ready" and len(original["items"]) == 50

    def large_page(*_args, **kwargs):
        result = deepcopy(original)
        selected = min(1611, max(1, int(kwargs.get("page", 1))))
        result.update(total_count=80502, total_pages=1611, current_page=selected)
        if selected == 1611:
            result["items"] = result["items"][:2]
        return result

    return large_page


def _contact_pagination_geometry(page):
    return page.get_by_role("navigation", name="Contact statement pages").evaluate(
        """pagination => {
        const active = pagination.querySelector('[aria-current="page"]');
        const indicator = pagination.querySelector('.local-store-pagination-indicator');
        const neutral = pagination.querySelector('.local-store-page-button:not(.is-active):not(.local-store-page-nav)');
        const activeBox = active.getBoundingClientRect();
        const indicatorBox = indicator.getBoundingClientRect();
        const textRange = document.createRange();
        textRange.selectNodeContents(active);
        const textBox = textRange.getBoundingClientRect();
        const activeStyle = getComputedStyle(active);
        return {
            activePage: active.textContent.trim(),
            activeCount: pagination.querySelectorAll('[aria-current="page"]').length,
            activeBounds: {left: activeBox.left, right: activeBox.right,
                top: activeBox.top, bottom: activeBox.bottom, width: activeBox.width},
            textBounds: {left: textBox.left, right: textBox.right,
                top: textBox.top, bottom: textBox.bottom, width: textBox.width},
            activeFont: {size: activeStyle.fontSize, weight: activeStyle.fontWeight,
                family: activeStyle.fontFamily},
            indicatorCenterError: Math.abs((activeBox.left + activeBox.right
                - indicatorBox.left - indicatorBox.right) / 2),
            indicatorVerticalError: Math.abs((activeBox.top + activeBox.bottom
                - indicatorBox.top - indicatorBox.bottom) / 2),
            activeColor: activeStyle.color,
            activeBackground: activeStyle.backgroundColor,
            indicatorBackground: getComputedStyle(indicator).backgroundColor,
            indicatorOpacity: Number(getComputedStyle(indicator).opacity),
            neutralColor: getComputedStyle(neutral).color,
            arrows: [...pagination.querySelectorAll('.local-store-page-nav')]
                .map(button => ({color: getComputedStyle(button).color,
                    background: getComputedStyle(button).backgroundColor})),
            textFits: textBox.left >= activeBox.left - 1
                && textBox.right <= activeBox.right + 1
                && textBox.top >= activeBox.top - 1
                && textBox.bottom <= activeBox.bottom + 1,
            documentFits: document.documentElement.scrollWidth <= innerWidth,
            isAnimated: pagination.classList.contains('is-animated'),
        };
    }"""
    )


def _assert_contact_pagination_geometry(page, current, *, animated):
    # Browser Back preserves the previous arrow's pointer position and hover state.
    page.mouse.move(0, 0)
    page.get_by_role("navigation", name="Contact statement pages").evaluate(
        """async pagination => {
        const transitions = pagination.getAnimations({subtree: true}).filter(animation => {
            const end = animation.effect?.getComputedTiming().endTime;
            return Number.isFinite(end) && end <= 2000;
        });
        await Promise.allSettled(transitions.map(animation => animation.finished));
    }"""
    )
    if animated:
        page.wait_for_function(
            """() => {
            const pagination = document.querySelector('[aria-label="Contact statement pages"]');
            const active = pagination?.querySelector('[aria-current="page"]');
            const indicator = pagination?.querySelector('.local-store-pagination-indicator');
            if (!active || !indicator || !pagination.classList.contains('is-animated')) return false;
            const target = active.getBoundingClientRect();
            const painted = indicator.getBoundingClientRect();
            return Math.abs(target.left + target.right - painted.left - painted.right) <= 2
                && Math.abs(target.top + target.bottom - painted.top - painted.bottom) <= 2
                && Number(getComputedStyle(indicator).opacity) > 0.99;
        }"""
        )
    geometry = _contact_pagination_geometry(page)
    assert geometry["activePage"] == str(current), geometry
    assert geometry["activeCount"] == 1, geometry
    if not animated:
        assert not geometry["isAnimated"], geometry
        assert geometry["indicatorOpacity"] == 0, geometry
        assert geometry["activeBackground"] != "rgba(0, 0, 0, 0)", geometry
        assert geometry["activeBackground"] == geometry["indicatorBackground"], geometry
    assert geometry["textFits"] and geometry["documentFits"], json.dumps(
        geometry, indent=2
    )
    assert geometry["activeColor"] != geometry["neutralColor"], geometry
    assert all(
        arrow["color"] == geometry["neutralColor"]
        and arrow["background"] == "rgba(0, 0, 0, 0)"
        for arrow in geometry["arrows"]
    ), geometry
    if animated:
        assert geometry["indicatorCenterError"] <= 1, geometry
        assert geometry["indicatorVerticalError"] <= 1, geometry
    return geometry


def _assert_contact_page_filters(page, expected_return):
    for href in (
        page.get_by_role("navigation", name="Contact statement pages")
        .locator("a")
        .evaluate_all("links => links.map(link => link.href)")
    ):
        parameters = parse_qs(urlsplit(href).query)
        assert parameters["q"] == ["target statement"]
        assert parameters["sort"] == ["oldest"]
        assert parameters["return_to"] == [expected_return]


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_contact_large_page_controls_track_current_page_and_resize(
    disposable_browser, contact_analysis_server, tmp_path, width, color_scheme
):
    fixture = contact_analysis_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1000},
        reduced_motion="reduce",
        color_scheme=color_scheme,
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    return_url = (
        "/browser?view=text&source=wechat&account=account%3Awxid_account_a"
        "&chat_kind=group&session=" + fixture.session + "&sort=oldest"
        "#wechat_message_preserved"
    )
    parameters = {"q": "target statement", "sort": "oldest", "return_to": return_url}
    url = fixture.url + "/browser/wechat/contacts/" + fixture.target_id
    evidence = []
    try:
        with patch(
            "app.core.wechat_contact_analysis.query_contact_analysis",
            side_effect=_large_contact_page_result(fixture),
        ):
            for current in (1, 806, 1611):
                page.goto(
                    url + "?" + urlencode({**parameters, "page": current}),
                    wait_until="networkidle",
                )
                page.mouse.move(0, 0)
                page.evaluate("() => document.fonts.ready")
                evidence.append(
                    _assert_contact_pagination_geometry(page, current, animated=True)
                )
                _assert_contact_page_filters(page, return_url)
                expect(page.locator("tr[data-contact-message-id]")).to_have_count(
                    2 if current == 1611 else 50
                )
            page.get_by_role("link", name="Previous statement page", exact=True).click()
            page.wait_for_load_state("networkidle")
            _assert_contact_pagination_geometry(page, 1610, animated=True)
            _assert_contact_page_filters(page, return_url)
            page.go_back(wait_until="networkidle")
            _assert_contact_pagination_geometry(page, 1611, animated=True)
            page.set_viewport_size(
                {"width": 390 if width == 1280 else 1280, "height": 850}
            )
            evidence.append(
                _assert_contact_pagination_geometry(page, 1611, animated=True)
            )
            page.set_viewport_size({"width": width, "height": 1000})
            _assert_contact_pagination_geometry(page, 1611, animated=True)
            page.screenshot(
                path=tmp_path / f"contact-page-1611-{width}-{color_scheme}.png"
            )
        assert not errors
        assert fixture.before == (
            fixture.history.read_bytes(),
            fixture.registry.read_bytes(),
        )
        (tmp_path / "large-page-geometry.json").write_text(
            json.dumps(evidence, indent=2) + "\n"
        )
    finally:
        context.close()


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_contact_last_page_remains_highlighted_without_motion_asset(
    disposable_browser, contact_analysis_server, tmp_path, width, color_scheme
):
    fixture = contact_analysis_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1000}, color_scheme=color_scheme
    )
    context.route("**/pagination-motion.js*", lambda route: route.abort())
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        with patch(
            "app.core.wechat_contact_analysis.query_contact_analysis",
            side_effect=_large_contact_page_result(fixture),
        ):
            page.goto(
                fixture.url
                + "/browser/wechat/contacts/"
                + fixture.target_id
                + "?"
                + urlencode({"page": 1611, "sort": "oldest", "q": "target statement"}),
                wait_until="networkidle",
            )
            page.mouse.move(0, 0)
            page.evaluate("() => document.fonts.ready")
            assert (
                page.evaluate("typeof window.CACHELIKES_PAGINATION_MOTION")
                == "undefined"
            )
            geometry = _assert_contact_pagination_geometry(page, 1611, animated=False)
            (tmp_path / "motion-fallback-geometry.json").write_text(
                json.dumps(geometry, indent=2) + "\n"
            )
            page.screenshot(
                path=tmp_path / f"contact-page-fallback-{width}-{color_scheme}.png"
            )
        assert not errors
        assert fixture.before == (
            fixture.history.read_bytes(),
            fixture.registry.read_bytes(),
        )
    finally:
        context.close()
