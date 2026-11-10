"""Published source-scoped image quotes keep exact authors and inert card bodies.

Code version: v1.1.0-codex.0
"""

from __future__ import annotations

import json
import sqlite3
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from app.core.chat_history_browser import query_chat_history
from app.core.config import CrawlConfig
from app.core.wechat_archive import _file_identity
from app.core.wechat_contacts import contact_registry_path, initialize_contact_registry
from app.core.wechat_quoted_resources import ORIGINAL_BASIS
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from tests import test_sidebar_e2e
from tests import test_wechat_secondary_completions as completion
from tests.test_wechat_contacts import bind, insert_contact, observe


disposable_browser = test_sidebar_e2e.disposable_browser
mac_archive = completion.mac_archive
bank_archive = completion.bank_archive
source_binding = completion.source_binding
witness = completion.witness
completion_fixture = completion.completion_fixture
VIEWER = "Primary account contact remark"


@pytest.fixture
def completed_quote_server(completion_fixture, tmp_path):
    fixture = completion_fixture
    partition = fixture["conversation"]["partition_id"]
    owner = completion.sender.PRIMARY
    peer = fixture["conversation"]["peer_id"]
    with sqlite3.connect(fixture["history"]) as history:
        snapshot_id, snapshot_name, encoded = history.execute(
            "SELECT id,snapshot_id,metadata FROM source_snapshots WHERE source_id=?",
            (partition,),
        ).fetchone()
        metadata = {**json.loads(encoded), "source_id": partition}
        history.execute(
            "UPDATE source_snapshots SET metadata=? WHERE id=?",
            (json.dumps(metadata), snapshot_id),
        )
    # The fixture independently declares the retained primary placeholder type.
    with sqlite3.connect(fixture["catalog"]) as catalog:
        proof = json.loads(fixture["image"]["provenance"])
        proof["text_extraction_kind"] = "resource_placeholder"
        catalog.execute(
            "UPDATE messages SET provenance=? WHERE id=?",
            (json.dumps(proof), fixture["image"]["id"]),
        )
        catalog.execute(
            "UPDATE archive_info SET value=? WHERE key='history_file_identity'",
            (json.dumps(_file_identity(fixture["history"])),),
        )
    registry = contact_registry_path(fixture["root"])
    with sqlite3.connect(registry) as contacts:
        initialize_contact_registry(contacts)
        author = insert_contact(contacts, completion.native.MEMBER)
        bind(
            contacts,
            author,
            source=partition,
            sender=completion.native.MEMBER,
            snapshot=snapshot_name,
            room=peer,
        )
        observe(
            contacts,
            author,
            "nickname",
            "Canonical native author",
            source=partition,
            snapshot=snapshot_name,
            owner=owner,
        )
        observe(
            contacts,
            author,
            "remark",
            VIEWER,
            source=partition,
            snapshot=snapshot_name,
            owner=owner,
            room=peer,
        )
    staged = completion._stage(fixture)
    compacted = completion.completions.compact_secondary_message_completions(
        fixture["root"],
        fixture["candidate"],
        tmp_path / "encoded-completions.sqlite3",
        candidate_sha256=staged["candidate_sha256"],
    )
    completion.completions.publish_secondary_message_completions(
        fixture["root"],
        compacted["candidate"],
        candidate_sha256=compacted["candidate_sha256"],
    )
    with completion.completions._readonly(
        completion.completions.completion_path(fixture["root"])
    ) as encoded:
        assert (
            encoded.execute(
                "SELECT COUNT(*) FROM messages WHERE typeof(provenance)='blob'"
            ).fetchone()[0]
            > 0
        )
    paths = (
        fixture["history"],
        fixture["catalog"],
        registry,
        completion.completions.completion_path(fixture["root"]),
    )
    before = tuple(path.read_bytes() for path in paths)
    model = query_chat_history(
        fixture["root"],
        source="wechat",
        account="account:" + owner,
        session=fixture["conversation"]["id"],
        query="Verified reply",
    )
    assert len(model.items) == 1
    quote = model.items[0].messaging["quoted_reply"]
    assert quote["resource_basis"] == ORIGINAL_BASIS
    assert quote["resource_original"]["message_id"] == fixture["image"]["id"]
    assert quote["speaker"] == "Native quoted author"
    assert quote["viewer_display_name"] == VIEWER
    assert quote["contact"]["id"] == author
    assert not quote["resource"]["can_reveal"]
    with patch(
        "app.web.app.SavedConfigStore.load",
        return_value=SavedConfigStore(CrawlConfig()),
    ):
        application = create_app(
            fixture["root"],
            computer_use_settings_path=tmp_path / "agent.json",
            computer_use_runtime_root=tmp_path / "runtime",
            agent_external_operations_enabled=False,
        )
    application.config.update(TESTING=True)
    server = make_server("127.0.0.1", 0, application, threaded=True)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield SimpleNamespace(
            url=f"http://127.0.0.1:{server.server_port}",
            session=fixture["conversation"]["id"],
            owner=owner,
            identifier=fixture["quote"]["id"],
            paths=paths,
            before=before,
        )
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        assert tuple(path.read_bytes() for path in paths) == before


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_off_page_completed_image_quote_keeps_body_and_exact_primary_contact(
    disposable_browser, completed_quote_server, width, color_scheme
):
    fixture = completed_quote_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 900},
        color_scheme=color_scheme,
        reduced_motion="reduce",
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        response = page.goto(
            fixture.url
            + "/browser?"
            + urlencode(
                {
                    "view": "text",
                    "source": "wechat",
                    "account": "account:" + fixture.owner,
                    "session": fixture.session,
                    "q": "Verified reply",
                }
            ),
            wait_until="networkidle",
        )
        assert response.status == 200
        expect(page.locator(".wechat-message")).to_have_count(1)
        quote = page.locator(
            "#wechat_message_" + fixture.identifier + " .wechat-message-quote"
        )
        expect(quote.locator(".wechat-quoted-speaker")).to_have_text(VIEWER)
        resource = quote.locator('.wechat-resource[data-wechat-resource-kind="image"]')
        expect(resource).to_be_visible()
        expect(resource.locator(".wechat-resource-label")).to_have_text("Image")
        expect(resource).to_have_attribute(
            "data-wechat-resource-status", "metadata_only"
        )
        expect(resource.locator(".wechat-resource-status")).to_have_text(
            "Metadata only"
        )
        expect(quote.locator("img, video, audio, script, a, button")).to_have_count(0)
        expect(quote.locator(".wechat-quoted-text")).to_have_count(0)
        expect(
            page.locator(
                "#wechat_message_" + fixture.identifier + " > .wechat-message-text"
            )
        ).to_have_text("Verified reply")
        assert not errors
    finally:
        context.close()
