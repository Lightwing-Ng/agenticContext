"""Media references remain visible and reveal only their mapped Mac destinations.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from io import BytesIO
import json
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode, urljoin

from PIL import Image, ImageChops
import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from app.core.config import CrawlConfig
from app.core.wechat_archive import (
    _time_fields,
    import_wechat_archive,
    wechat_archive_path,
)
from app.core.wechat_mac_resources import WechatMacResourceResolver
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from tests import test_sidebar_e2e
from tests.test_wechat_archive import make_archive
from tests.test_wechat_mac_resources import native_hash
from tests.test_wechat_native_system_notices_e2e import _assert_bubble_effects
from tests.test_wechat_resource_timeline import _reference, _session, _sidecar


disposable_browser = test_sidebar_e2e.disposable_browser
CHECKSUM = "0123456789abcdef0123456789abcdef"
LONG_FILENAME = (
    "Synthetic report with a long descriptive filename and 中文 source context.pdf"
)
MONTH = "2021-08"


def _resource_row(session, kind, second, **values):
    identifier = hashlib.sha256(f"{session['id']}:{kind}:{second}".encode()).hexdigest()
    row = list(_reference(session, kind.title(), second, identifier=identifier))
    timestamp = int(
        datetime(
            2021, 8, 7, 19, 0, second, tzinfo=timezone(timedelta(hours=8))
        ).timestamp()
    )
    row[7:11] = _time_fields(
        {"sent_at_utc_iso": datetime.fromtimestamp(timestamp, timezone.utc).isoformat()}
    )
    provenance = json.loads(row[11])
    provenance["message_type"] = {"image": 3, "video": 43, "file": 49}[kind]
    provenance["locator"] = {"database": "fixture-message.sqlite", "row_id": second}
    provenance["resource_reference"].update(
        kind=kind, unix_timestamp=timestamp, **values
    )
    row[11] = json.dumps(provenance)
    return tuple(row)


@pytest.fixture(scope="module")
def resource_server(tmp_path_factory):
    sandbox = tmp_path_factory.mktemp("wechat-resource-browser")
    store = sandbox / "store"
    import_wechat_archive(make_archive(sandbox / "archive", repeated=3), store)
    first = _session(store)
    other = _session(store, owner="wxid_account_b")
    rows = [
        _resource_row(first, "image", 20, checksum_md5=CHECKSUM),
        _resource_row(first, "video", 40),
        _resource_row(first, "file", 55, filename=LONG_FILENAME, byte_size=4),
    ]
    missing = _resource_row(other, "image", 25, checksum_md5=CHECKSUM)
    sidecar = _sidecar(store, [*((first, row) for row in rows), (other, missing)])
    home = sandbox / "home"
    modern = (
        home / "Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"
    )
    account = modern / "wxid_account_a_abcd"
    images = account / "msg/attach" / native_hash(first["peer_id"]) / MONTH / "Img"
    images.mkdir(parents=True)
    (images / f"{CHECKSUM}.dat").write_bytes(b"opaque synthetic image")
    video = account / "msg/video" / MONTH
    video.mkdir(parents=True)
    files = account / "msg/file" / MONTH
    files.mkdir(parents=True)
    (files / LONG_FILENAME).write_bytes(b"data")
    history = wechat_archive_path(store)
    before = history.read_bytes(), sidecar.read_bytes()

    def resolver(root):
        return WechatMacResourceResolver(root, home=home)

    with (
        patch(
            "app.core.wechat_history_browser.WechatMacResourceResolver",
            side_effect=resolver,
        ),
        patch(
            "app.core.wechat_resources.WechatMacResourceResolver", side_effect=resolver
        ),
        patch("app.web.local_resource_routes.reveal_media_path") as file_opener,
        patch("app.web.local_resource_routes.open_directory_path") as opener,
    ):
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
                session=first["id"],
                other_session=other["id"],
                rows=rows,
                history=history,
                sidecar=sidecar,
                before=before,
                home=home,
                account=account,
                parked=home / "parked-account",
                video=video,
                opener=opener,
                file_opener=file_opener,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def _open_session(page, fixture, *, other=False):
    query = {
        "view": "text",
        "source": "wechat",
        "account": "account:wxid_account_b" if other else "account:wxid_account_a",
        "session": fixture.other_session if other else fixture.session,
    }
    response = page.goto(
        fixture.url + "/browser?" + urlencode(query), wait_until="networkidle"
    )
    assert response.status == 200
    toggle = page.locator("#sidebar_toggle")
    if toggle.get_attribute("aria-expanded") == "true":
        toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "false")
    page.evaluate("() => document.fonts.ready")


def _assert_painted_svg(page, resource, asset):
    icon = resource.locator(".wechat-resource-icon")
    icon.scroll_into_view_if_needed()
    mask = icon.evaluate("node => getComputedStyle(node).maskImage")
    assert mask.endswith(asset + '")'), mask
    response = page.request.get(urljoin(page.url, "/static/images/" + asset))
    assert response.status == 200 and "<svg" in response.text()
    rectangle = icon.bounding_box()
    before = Image.open(BytesIO(page.screenshot(clip=rectangle))).convert("RGB")
    icon.evaluate("node => {node.style.visibility = 'hidden';}")
    try:
        after = Image.open(BytesIO(page.screenshot(clip=rectangle))).convert("RGB")
    finally:
        icon.evaluate("node => {node.style.visibility = '';}")
    difference = ImageChops.difference(before, after)
    pixels = difference.load()
    assert (
        sum(
            max(pixels[x, y]) >= 4
            for y in range(difference.height)
            for x in range(difference.width)
        )
        >= 16
    )


def _assert_resource_geometry(page):
    geometry = page.locator(".wechat-resource").evaluate_all("""items => ({
        rows: items.map(item => {
            const card = item.closest('.wechat-message'), row = card.parentElement;
            const body = item.getBoundingClientRect(), header = card.querySelector('summary').getBoundingClientRect();
            const clock = row.querySelector(':scope > time').getBoundingClientRect();
            const label = item.querySelector('.wechat-resource-label').getBoundingClientRect();
            const style = getComputedStyle(item);
            return {kind: item.dataset.wechatResourceKind, bodyHeight: body.height,
                bodyWidth: body.width, cardWidth: card.getBoundingClientRect().width,
                clockRight: clock.right, belowHeader: body.top >= header.bottom - 0.5,
                labelHeight: label.height, background: style.backgroundColor, border: style.borderTopWidth};
        }), documentOverflow: document.documentElement.scrollWidth - innerWidth,
        listOverflow: document.querySelector('[data-chat-scrollport]').scrollWidth
            - document.querySelector('[data-chat-scrollport]').clientWidth,
    })""")
    assert geometry["documentOverflow"] <= 1 and geometry["listOverflow"] <= 1, geometry
    rights = [row["clockRight"] for row in geometry["rows"]]
    assert max(rights) - min(rights) <= 1, geometry
    for row in geometry["rows"]:
        assert row["belowHeader"] and row["bodyWidth"] <= row["cardWidth"], row
        assert row["background"] in {"rgba(0, 0, 0, 0)", "transparent"}, row
        assert row["border"] == "0px", row
        if row["kind"] in {"image", "video"}:
            assert 44 <= row["bodyHeight"] <= 52, row
    return geometry


@pytest.mark.parametrize("width", [1280, 710, 390])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_resource_placeholders_preserve_mac_mapping_icons_and_finder_actions(
    disposable_browser, resource_server, tmp_path, width, color_scheme
):
    fixture = resource_server
    fixture.opener.reset_mock()
    fixture.file_opener.reset_mock()
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1109},
        color_scheme=color_scheme,
        reduced_motion="reduce",
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        _open_session(page, fixture)
        resources = page.locator(".wechat-resource")
        expect(resources).to_have_count(3)
        assert str(fixture.home) not in page.content()
        image = page.locator('.wechat-resource[data-wechat-resource-kind="image"]')
        video = page.locator('.wechat-resource[data-wechat-resource-kind="video"]')
        file = page.locator('.wechat-resource[data-wechat-resource-kind="file"]')
        for resource, status, asset in [
            (image, "file", "photo.stack.svg"),
            (video, "folder", "film.circle.svg"),
            (file, "candidate", "document.on.document.fill.svg"),
        ]:
            expect(resource).to_have_attribute("data-wechat-resource-status", status)
            assert resource.evaluate("node => node.tagName") == "BUTTON"
            _assert_painted_svg(page, resource, asset)
        expect(file.locator(".wechat-resource-label")).to_have_text(LONG_FILENAME)
        geometry = _assert_resource_geometry(page)
        if width == 390:
            assert (
                next(row for row in geometry["rows"] if row["kind"] == "file")[
                    "labelHeight"
                ]
                > 24
            )
        _assert_bubble_effects(page)
        expect(page.locator(".browser-chat-effect")).to_have_count(6)

        video.click()
        expect(video.locator("[data-wechat-resource-feedback]")).to_have_text(
            "Folder shown in Finder"
        )
        fixture.opener.assert_called_once_with(fixture.video)
        fixture.file_opener.assert_not_called()
        expect(video).to_be_enabled()
        fixture.account.rename(fixture.parked)
        try:
            image.click()
            expect(image.locator("[data-wechat-resource-feedback]")).to_contain_text(
                "no longer available"
            )
            expect(image).to_be_enabled()
            expect(resources).to_have_count(3)
            fixture.opener.assert_called_once_with(fixture.video)
        finally:
            fixture.parked.rename(fixture.account)
        page.screenshot(path=tmp_path / f"wechat-resources-{width}-{color_scheme}.png")

        _open_session(page, fixture, other=True)
        missing = page.locator(".wechat-resource")
        expect(missing).to_have_count(1)
        expect(missing).to_have_attribute("data-wechat-resource-status", "missing")
        assert missing.evaluate("node => node.tagName") == "DIV"
        assert missing.get_attribute("data-wechat-resource-reveal") is None
        expect(missing).to_contain_text("Not found on this Mac")
        _assert_painted_svg(page, missing, "photo.stack.svg")
        assert str(fixture.home) not in page.content()
        fixture.opener.assert_called_once_with(fixture.video)
        fixture.file_opener.assert_not_called()
        assert not errors
        assert (
            fixture.history.read_bytes(),
            fixture.sidecar.read_bytes(),
        ) == fixture.before
    finally:
        if fixture.parked.exists():
            fixture.parked.rename(fixture.account)
        context.close()
