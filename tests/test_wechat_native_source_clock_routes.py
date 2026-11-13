"""A native child's source clock reaches chat, contact ordering, and CSV output.

Code version: v1.0.2-codex.0
"""

from __future__ import annotations

import csv
from html.parser import HTMLParser
import io
import sqlite3
from unittest.mock import patch

import pytest

from app.core.config import CrawlConfig
from app.core.wechat_contact_analysis import query_contact_analysis
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from tests import test_wechat_contact_native_embedded as native
from tests.wechat_browser_markup import get_verified_browser_page


native_activity_store = native.native_activity_store


class _RenderedTimes(HTMLParser):
    def __init__(self):
        super().__init__()
        self.row = ""
        self.current = None
        self.times = {}

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "tr":
            self.row = values.get("data-contact-message-id", "")
        elif tag == "time":
            self.current = [values.get("id") or self.row, values, []]

    def handle_data(self, data):
        if self.current is not None:
            self.current[2].append(data)

    def handle_endtag(self, tag):
        if tag == "time" and self.current is not None:
            key, attrs, parts = self.current
            self.times.setdefault(key, []).append((attrs, "".join(parts)))
            self.current = None
        elif tag == "tr":
            self.row = ""


@pytest.mark.parametrize(
    "raw,stamp,label",
    [
        ("2021-09-17 11:12:56", "2021-09-17T11:12:56", "17 Sep 2021 11:12:56"),
        ("2021-03-10 01:49:57", "2021-03-10T01:49:57", "10 Mar 2021 01:49:57"),
    ],
)
def test_child_source_clock_is_consistent_across_chat_contact_and_csv(
    native_activity_store, tmp_path, raw, stamp, label
):
    fixture = native_activity_store
    proof = native._tree()
    proof["text_components"][0]["source_timestamp"] = raw
    native._publish_native_forwarded_proof(fixture, proof)
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    analysis = query_contact_analysis(fixture.store, fixture.target_id, sort="newest")
    parent = fixture.parents["native-forward"]
    child = parent + "_forwarded_1"
    item = next(item for item in analysis["items"] if item["id"] == child)
    assert item["timestamp_value"] == stamp
    assert item["time_basis"] == "Source clock; timezone unknown"
    assert item["account_key"] == "account:" + native.OWNER
    assert item["source_clock"] == raw
    known = [row["time_sort"] for row in analysis["items"] if row["timestamp_value"]]
    assert known == sorted(known, reverse=True)
    assert analysis["items"][-1]["timestamp_value"] == ""
    with sqlite3.connect(fixture.history) as connection:
        (conversation,) = connection.execute(
            "SELECT conversation_id FROM messages WHERE id=?", (parent,)
        ).fetchone()
    with patch(
        "app.web.app.SavedConfigStore.load",
        return_value=SavedConfigStore(CrawlConfig()),
    ):
        app = create_app(
            fixture.store,
            computer_use_settings_path=tmp_path / "settings.json",
            computer_use_runtime_root=tmp_path / "runtime",
            agent_external_operations_enabled=False,
        )
    app.config.update(TESTING=True)
    client = app.test_client()
    chat = get_verified_browser_page(
        client,
        "/browser",
        query_string={
            "view": "text",
            "source": "wechat",
            "account": "account:" + native.OWNER,
            "chat_kind": "group",
            "session": conversation,
        },
    )
    assert chat.status_code == 200
    rendered = _RenderedTimes()
    rendered.feed(chat.get_data(as_text=True))
    attrs, text = rendered.times["wechat_time_" + child][0]
    assert attrs["datetime"] == stamp and text == label
    contact = client.get(
        "/browser/wechat/contacts/" + fixture.target_id,
        query_string={"q": native.FIRST},
    )
    assert contact.status_code == 200
    rendered = _RenderedTimes()
    rendered.feed(contact.get_data(as_text=True))
    attrs, text = rendered.times[child][0]
    assert attrs["datetime"] == stamp and text == label
    exported = client.get(
        "/browser/wechat/contacts/" + fixture.target_id + "/export.csv",
        query_string={"q": native.FIRST},
    )
    assert exported.status_code == 200
    rows = list(csv.DictReader(io.StringIO(exported.data.decode("utf-8-sig"))))
    assert rows == [{"Time": raw, "Statement": native.FIRST}]
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())
