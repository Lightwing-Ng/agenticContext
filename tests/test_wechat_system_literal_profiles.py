"""Native system templates retain literal labels without inventing identities.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from html import escape

import pytest

from app.core.wechat_text import extract_wechat_text, project_system_notice_text


def _member(username, nickname):
    return (
        "<member><username>"
        + escape(username)
        + "</username><nickname>"
        + escape(nickname)
        + "</nickname></member>"
    )


def _template(template, field, members, separator=""):
    separator_leaf = (
        "<separator>" + escape(separator) + "</separator>" if separator else ""
    )
    return (
        '<sysmsg type="sysmsgtemplate"><sysmsgtemplate>'
        '<content_template type="tmpl_type_profile"><plain/>'
        "<template>"
        + escape(template)
        + '</template><link_list><link name="username" type="link_profile">'
        "<memberlist>"
        + _member("wxid_inviter", "Source member")
        + '</memberlist></link><link name="'
        + field
        + '" type="link_profile"><memberlist>'
        + members
        + "</memberlist>"
        + separator_leaf
        + "</link></link_list></content_template></sysmsgtemplate></sysmsg>"
    )


@pytest.mark.parametrize("message_type", [10000, 10002])
@pytest.mark.parametrize(
    ("template", "field", "expected"),
    [
        (
            "$username$ invited you to a group chat with $others$",
            "others",
            "Source member invited you to a group chat with Literal <b>& label",
        ),
        (
            '"$username$" changed the group name to "$remark$"',
            "remark",
            '"Source member" changed the group name to "Literal <b>& label"',
        ),
    ],
)
def test_explicit_empty_identity_keeps_only_its_literal_source_label(
    message_type, template, field, expected
):
    raw = _template(template, field, _member("", "Literal <b>& label"))
    prefixed = "123456789@chatroom:\n" + raw
    assert project_system_notice_text(prefixed, peer_id="123456789@chatroom") == expected
    assert project_system_notice_text(prefixed, peer_id="987654321@chatroom") is None
    result = extract_wechat_text(message_type, prefixed)
    assert result == {
        "text": expected,
        "text_extraction_kind": "system_text",
        "original_message_type": message_type,
        "appmsg_subtype": None,
        "flags": ["system_notice"],
        "system_message_subtype": "sysmsgtemplate",
        "system_source_ids": {},
    }


def test_multiple_unidentified_literal_members_use_the_observed_separator():
    raw = _template(
        "$username$ invited $others$",
        "others",
        _member("", "Member A") + _member("", "Member B"),
        "; ",
    )
    assert project_system_notice_text(raw) == "Source member invited Member A; Member B"


@pytest.mark.parametrize(
    "members",
    [
        "<member><nickname>Literal label</nickname></member>",
        _member(" ", "Literal label"),
        _member("", ""),
        _member("", " "),
        "<member><username/><username/><nickname>Literal label</nickname></member>",
        "<member><username/><nickname><b>Literal label</b></nickname></member>",
        '<member><username/><nickname>Literal label</nickname><extra/></member>',
        _member("wxid_repeated", "Member A") + _member("wxid_repeated", "Member B"),
    ],
)
def test_literal_labels_do_not_relax_missing_or_ambiguous_member_guards(members):
    raw = _template("$username$ invited $others$", "others", members, ", ")
    assert project_system_notice_text(raw) is None
    assert extract_wechat_text(10002, raw) is None
