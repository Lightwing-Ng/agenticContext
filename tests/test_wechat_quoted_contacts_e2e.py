"""Native quoted authors reuse exact contacts while retaining source labels.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

import csv
from io import StringIO
import json
import sqlite3
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from app.core.chat_history_browser import (
    build_chat_history_markdown,
    query_chat_history,
)
from app.core.config import CrawlConfig
from app.core.wechat_archive import wechat_archive_path
from app.core.wechat_contacts import contact_registry_path, initialize_contact_registry
from app.core.wechat_display_names import display_names_path
from app.core.wechat_text import native_components_text
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from tests import test_sidebar_e2e
from tests.test_wechat_contacts import bind, insert_contact, observe
from tests.test_wechat_quoted_contacts import (
    OLD_LABEL,
    OTHER,
    OWNER,
    ROOM,
    SNAPSHOT,
    SOURCE,
    TARGET,
    VIEWER,
    _item,
)
from tests.test_wechat_reconcile import merge, record, source


disposable_browser = test_sidebar_e2e.disposable_browser
QUOTED_BODIES = (
    '  Original <img src=x onerror="window.quotedContactExecuted = true"> & literal\n'
    + "https://example.invalid/"
    + "original/" * 18,
    "An independent namesake's original statement.",
)
REPLIES = ("First reply <b>stays literal</b>", "Second reply stays independent")
CANONICAL_NAMES = ("Canonical target", "Canonical namesake")


def _assert_sources_preserved(fixture):
    assert tuple(path.read_bytes() for path in fixture.paths) == fixture.preserved


@pytest.fixture(scope="module")
def quoted_contact_server(tmp_path_factory):
    sandbox = tmp_path_factory.mktemp("wechat-quoted-contact-browser")
    store = sandbox / "store"
    metadata = source(OWNER)
    observations = []
    for index, native in enumerate((TARGET, OTHER)):
        item = _item(sender=native, chat=ROOM, number="ab"[index])
        provenance = item.messaging["provenance"]
        components = provenance["text_components"]
        components[0]["text"] = REPLIES[index]
        components[1]["text"] = QUOTED_BODIES[index]
        observations.append(
            record(
                metadata,
                f"quoted-contact-{index}",
                peer=ROOM,
                text=native_components_text(components),
                message_type=49,
                text_extraction_kind="quoted_reply",
                appmsg_subtype=57,
                text_components=components,
                quoted_reference=provenance["quoted_reference"],
                server_id=str(9223372036854775806 - index),
            )
        )
    merge(store, [metadata], observations, SNAPSHOT)
    registry = contact_registry_path(store)
    registry.parent.mkdir(parents=True, exist_ok=True)
    contacts = {}
    with sqlite3.connect(registry) as connection:
        initialize_contact_registry(connection)
        for native, name in zip((TARGET, OTHER), CANONICAL_NAMES, strict=True):
            identifier = insert_contact(connection, native)
            contacts[native] = identifier
            bind(
                connection,
                identifier,
                source=SOURCE,
                sender=native,
                snapshot=SNAPSHOT,
                room=ROOM,
            )
            observe(
                connection,
                identifier,
                "nickname",
                name,
                source=SOURCE,
                snapshot=SNAPSHOT,
                owner=OWNER,
            )
    names = display_names_path(store)
    names.write_text(
        json.dumps({"schema_version": 1, "contacts": {contacts[TARGET]: VIEWER}})
    )
    paths = (wechat_archive_path(store), registry, names)
    preserved = tuple(path.read_bytes() for path in paths)
    session = query_chat_history(store, source="wechat").sessions[0].stable_id
    page = query_chat_history(
        store, source="wechat", account=f"account:{OWNER}", session=session
    )
    messages = {item.messaging["provenance"]["record_id"]: item for item in page.items}
    identifiers = []
    for index, native in enumerate((TARGET, OTHER)):
        message = messages[f"quoted-contact-{index}"]
        quote = message.messaging["quoted_reply"]
        assert quote["speaker"] == OLD_LABEL
        assert quote["viewer_display_name"] == (
            VIEWER if index == 0 else CANONICAL_NAMES[1]
        )
        assert quote["contact"]["id"] == contacts[native]
        assert quote["reference"] == observations[index]["quoted_reference"]
        assert quote["text"] == QUOTED_BODIES[index]
        assert message.content_text == observations[index]["text"]
        identifiers.append(message.stable_id)
    assert contacts[TARGET] != contacts[OTHER]
    markdown = build_chat_history_markdown(page)
    assert all(observation["text"] in markdown for observation in observations)
    assert VIEWER not in markdown
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
    with app.test_client() as client:
        response = client.get(
            f"/browser/wechat/session/{session}/export.csv",
            query_string={"account": f"account:{OWNER}", "chat_kind": "group"},
        )
        assert response.status_code == 200
        reader = csv.DictReader(StringIO(response.data.decode("utf-8-sig"), newline=""))
        assert sorted(row["Statement"] for row in reader) == sorted(
            observation["text"] for observation in observations
        )
    fixture = SimpleNamespace(
        identifiers=identifiers,
        paths=paths,
        preserved=preserved,
        session=session,
    )
    _assert_sources_preserved(fixture)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fixture.url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield fixture
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_native_quoted_contacts_use_viewer_names_without_merging_namesakes(
    disposable_browser, quoted_contact_server, tmp_path, width, color_scheme
):
    fixture = quoted_contact_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1000},
        color_scheme=color_scheme,
        reduced_motion="reduce",
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(
            fixture.url
            + "/browser?"
            + urlencode(
                {
                    "view": "text",
                    "source": "wechat",
                    "account": f"account:{OWNER}",
                    "session": fixture.session,
                }
            ),
            wait_until="domcontentloaded",
        )
        toggle = page.locator("#sidebar_toggle")
        if toggle.get_attribute("aria-expanded") == "true":
            toggle.click()
        page.evaluate("() => document.fonts.ready")
        for index, identifier in enumerate(fixture.identifiers):
            article = page.locator("#wechat_message_" + identifier)
            quote = article.get_by_role("blockquote", name="Quoted message")
            expect(quote).to_have_count(1)
            expect(quote.locator(".wechat-quoted-speaker")).to_have_text(
                VIEWER if index == 0 else CANONICAL_NAMES[1]
            )
            expect(quote.locator(".wechat-quoted-text")).to_have_text(
                QUOTED_BODIES[index]
            )
            assert OLD_LABEL not in quote.inner_text()
            expect(
                quote.locator("img, script, iframe, video, audio, viewer")
            ).to_have_count(0)
            expect(article.locator(":scope > .wechat-message-text")).to_have_text(
                REPLIES[index]
            )
        assert page.evaluate("typeof window.quotedContactExecuted") == "undefined"
        geometry = page.evaluate("""() => {
            const port = document.querySelector('[data-chat-scrollport]');
            return {
                documentOverflow: document.documentElement.scrollWidth - innerWidth,
                listOverflow: port.scrollWidth - port.clientWidth,
                quotes: [...document.querySelectorAll('.wechat-message-quote')].map(quote => {
                    const box = quote.getBoundingClientRect();
                    const parent = quote.closest('.wechat-message').getBoundingClientRect();
                    return {insideBubble: box.left >= parent.left && box.right <= parent.right,
                        textFits: [...quote.children].every(child => child.scrollWidth <= child.clientWidth)};
                }),
            };
        }""")
        assert geometry["documentOverflow"] == geometry["listOverflow"] == 0
        assert len(geometry["quotes"]) == 2
        assert all(
            quote["insideBubble"] and quote["textFits"] for quote in geometry["quotes"]
        )
        (tmp_path / "quoted-contact-geometry.json").write_text(
            json.dumps(geometry, indent=2) + "\n"
        )
        page.screenshot(path=tmp_path / f"quoted-contact-{width}-{color_scheme}.png")
        assert not errors
        _assert_sources_preserved(fixture)
    finally:
        context.close()
