"""Profile and administrator notices preserve explicit source literals safely.

Code version: v1.0.0-codex.0
"""

from html import escape

import pytest

from app.core import wechat_text
from app.core.wechat_text import extract_wechat_text, project_system_notice_text
from tests.test_wechat_text import _system_link_template, _system_profile_link


ROOM = "47679661767@chatroom"
REMARK_TEMPLATE = '"$username$" changed the group name to "$remark$"'
ADMIN_TEMPLATE = 'Group owner "$ownername$" has set you as group admin $adminexplain$'


def _remark_xml(*, plain="<![CDATA[]]>", nickname="Source member"):
    return _system_link_template(
        REMARK_TEMPLATE,
        _system_profile_link("username", [("wxid_member", nickname)])
        + '<link name="remark" type="link_plain"><plain>'
        + plain
        + "</plain></link>",
    )


def _admin_xml(*, nickname="Source owner", title="View Details"):
    return _system_link_template(
        ADMIN_TEMPLATE,
        _system_profile_link("ownername", [("wxid_owner", nickname)])
        + '<link name="adminexplain" type="link_admin_explain"><title>'
        + escape(title)
        + "</title></link>",
        content_type="tmpl_type_admin_explain",
    )


@pytest.mark.parametrize("message_type", [10000, 10002])
@pytest.mark.parametrize("plain", ["<![CDATA[]]>", "", " ", "&#32;&#10;"])
def test_explicit_empty_or_whitespace_rename_value_is_literal(message_type, plain):
    xml = _remark_xml(plain=plain)
    literal = {"<![CDATA[]]>": "", "": "", " ": " ", "&#32;&#10;": " \n"}[plain]
    expected = '"Source member" changed the group name to "' + literal + '"'
    raw = ROOM + ":\n" + xml
    assert project_system_notice_text(raw, peer_id=ROOM) == expected
    assert extract_wechat_text(message_type, raw)["text"] == expected
    assert extract_wechat_text(1, raw)["text"] == raw
    assert project_system_notice_text(raw, peer_id="999@chatroom") is None


def test_empty_plain_leaf_is_distinct_from_missing_leaf():
    xml = _remark_xml().replace("<plain><![CDATA[]]></plain></link>", "</link>")
    assert project_system_notice_text(xml) is None
    assert extract_wechat_text(10002, xml) is None


@pytest.mark.parametrize("message_type", [10000, 10002])
@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
def test_admin_notice_projects_only_recorded_owner_and_action_title(message_type, ending):
    xml = _admin_xml()
    raw = ROOM + ":" + ending + xml
    expected = 'Group owner "Source owner" has set you as group admin View Details'
    assert project_system_notice_text(raw, peer_id=ROOM) == expected
    result = extract_wechat_text(message_type, raw)
    assert result["text"] == expected
    assert result["system_message_subtype"] == "sysmsgtemplate"
    assert result["system_source_ids"] == {}
    assert "wxid_owner" not in result["text"]


def test_admin_title_and_owner_are_literal_without_recursive_substitution(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("System notices must not open an action or external file")

    nickname = '<script>owner</script> $adminexplain$ & name'
    title = '<a href="javascript:run()">literal</a> $ownername$'
    xml = _admin_xml(nickname=nickname, title=title)
    with monkeypatch.context() as scoped:
        scoped.setattr("builtins.open", forbidden)
        assert project_system_notice_text(xml) == (
            'Group owner "' + nickname + '" has set you as group admin ' + title
        )


@pytest.mark.parametrize(
    "old,new",
    [
        ('type="tmpl_type_admin_explain"', 'type="unknown_template"'),
        ('name="adminexplain"', 'name="unknown"'),
        ('name="ownername"', 'name="adminexplain"'),
        ('type="link_admin_explain"', 'type="link_unknown"'),
        ('type="link_admin_explain"', 'type="link_plain"'),
        ('type="link_admin_explain"', 'type="link_admin_explain" hidden="1"'),
        ('type="link_admin_explain"', 'type="link_admin_explain" href="javascript:run()"'),
        ('<title>View Details</title>', '<title/>'),
        ('<title>View Details</title>', '<title> </title>'),
        ('<title>View Details</title>', '<title>View Details</title><title>Other</title>'),
        ('<title>View Details</title>', '<title><a>View Details</a></title>'),
        ('<title>View Details</title>', '<title>View Details</title><url>https://example.invalid</url>'),
        ('<title>View Details</title>', '<title lang="en">View Details</title>'),
        ('$adminexplain$', '$missing$'),
        ('<link_list>', '<link_list><link name="extra" type="link_plain"><plain/></link>'),
        ('</sysmsgtemplate>', '</sysmsgtemplate><config/>'),
    ],
)
def test_unsupported_or_ambiguous_admin_shapes_remain_unprojected(old, new):
    xml = _admin_xml().replace(old, new)
    assert project_system_notice_text(xml) is None
    assert extract_wechat_text(10002, xml) is None


def test_admin_action_is_not_accepted_in_an_ordinary_profile_template():
    xml = _admin_xml().replace('type="tmpl_type_admin_explain"', 'type="tmpl_type_profile"')
    assert project_system_notice_text(xml) is None


@pytest.mark.parametrize("xml", [_remark_xml(), _admin_xml()])
def test_new_notice_shapes_keep_xml_byte_depth_node_and_declaration_guards(monkeypatch, xml):
    assert project_system_notice_text(xml) is not None
    for attribute, limit in [("MAX_XML_BYTES", 128), ("MAX_XML_DEPTH", 3), ("MAX_XML_NODES", 3)]:
        with monkeypatch.context() as scoped:
            scoped.setattr(wechat_text, attribute, limit)
            assert project_system_notice_text(xml) is None
    declaration = '<!DOCTYPE sysmsg [<!ENTITY leak SYSTEM "file:///private/secret">]>'
    assert project_system_notice_text(declaration + xml) is None


def test_admin_expansion_retains_output_byte_budget(monkeypatch):
    xml = _admin_xml(title="中" * 20).replace("$adminexplain$", "$adminexplain$" * 100)
    monkeypatch.setattr(wechat_text, "MAX_XML_BYTES", len(xml.encode()) + 1)
    assert project_system_notice_text(xml) is None
