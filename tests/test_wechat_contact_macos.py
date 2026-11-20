"""Project authenticated Mac contact references without copying source messages.

Code version: v1.0.0-codex.0
"""

from copy import deepcopy
import sqlite3

from app.core import wechat_contact_analysis as analysis
from app.core.wechat_contacts import contact_registry_path, initialize_contact_registry
from tests import test_wechat_macos_recovery_bank as fixtures
from tests.test_wechat_contacts import bind, insert_contact


mac_archive = fixtures.mac_archive
bank_archive = fixtures.bank_archive


def test_mac_refresh_adds_primary_and_attributed_forwarded_text_to_contact(bank_archive):
    fixture = bank_archive
    registry = contact_registry_path(fixture.root)
    with sqlite3.connect(registry) as connection:
        initialize_contact_registry(connection)
        contact = insert_contact(connection, fixtures.MEMBER)
        with sqlite3.connect(fixture.history) as history:
            partition = history.execute(
                "SELECT partition_id FROM conversations WHERE id=?", (fixture.group_session,)
            ).fetchone()[0]
        bind(connection, contact, source=partition, sender=fixtures.MEMBER)
    original_sources = (fixture.history.read_bytes(), registry.read_bytes())
    old = analysis.query_contact_analysis(fixture.root, contact, sort="newest")
    group = deepcopy(fixture.group)
    group["messages"] = [
        fixtures._message(80, 1, raw="Authenticated newer statement"),
        fixtures._message(
            81, 49, sender=fixtures.OWNER,
            raw=(
                "<appmsg><type>19</type><recorditem><recordinfo><datalist>"
                '<dataitem datatype="1"><sourcename>Same screen name</sourcename>'
                f"<dataitemsource><fromusr>{fixtures.MEMBER}</fromusr></dataitemsource>"
                "<srcMsgCreateTime>1700000079</srcMsgCreateTime>"
                "<datadesc>Authenticated forwarded statement</datadesc></dataitem>"
                '<dataitem datatype="1"><sourcename>Same screen name</sourcename>'
                "<datadesc>Unverified namesake statement</datadesc></dataitem>"
                "</datalist></recordinfo></recorditem></appmsg>"
            ),
        ),
        fixtures._message(
            82, 49, sender=fixtures.OWNER,
            raw=(
                "<appmsg><type>57</type><title>Other user's reply</title><refermsg>"
                f"<type>1</type><fromusr>{fixtures.ROOM}</fromusr>"
                f"<chatusr>{fixtures.MEMBER}</chatusr><svrid>580</svrid>"
                "<createtime>1700000080</createtime><content>Authenticated newer statement</content>"
                "</refermsg></appmsg>"
            ),
        ),
    ]
    fixtures._publish(fixture, packets=[group], name="contact-bank")
    result = analysis.query_contact_analysis(fixture.root, contact, sort="newest")
    assert result["source_fingerprint"] != old["source_fingerprint"]
    primary, child = result["items"][:2]
    assert primary["content_text"] == "Authenticated newer statement"
    assert primary["observation_count"] == 2
    assert child["content_text"] == "Authenticated forwarded statement"
    assert child["reference_kind"] == "native_forwarded"
    assert child["timestamp_value"] == "2023-11-15T06:14:39+08:00"
    assert child["source_label"] == "WeChat for Mac local database"
    assert result["total_count"] == old["total_count"] + 2
    location = analysis.message_source_location(fixture.root, child["id"])
    assert location["conversation_id"] == fixture.group_session
    assert location["anchor_id"] == "wechat_message_" + child["id"]
    assert location["component_path"] == ["native", 1]
    assert [item["id"] for item in analysis.query_contact_analysis(
        fixture.root, contact, query="Authenticated forwarded statement",
    )["items"]] == [child["id"]]
    exported = list(analysis.export_contact_analysis_items(
        fixture.root, contact, source_fingerprint=result["source_fingerprint"], sort="newest",
    ))
    assert [item["id"] for item in exported] == [item["id"] for item in result["items"]]

    group["messages"].append(fixtures._message(83, 1, raw="Another verified refresh"))
    fixtures._publish(fixture, packets=[group], name="next-contact-bank")
    refreshed = analysis.query_contact_analysis(fixture.root, contact, sort="newest")
    assert refreshed["total_count"] == result["total_count"] + 1
    assert refreshed["items"][0]["content_text"] == "Another verified refresh"
    assert (fixture.history.read_bytes(), registry.read_bytes()) == original_sources
