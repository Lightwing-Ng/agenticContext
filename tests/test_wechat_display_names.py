"""Local display aliases require verified identity and never rewrite source evidence.

Code version: v1.5.0-codex.0
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json

import pytest

from app.core.wechat_contacts import canonical_contact_id
from app.core.wechat_display_names import WechatDisplayNames, display_names_path


OWNER = "wxid_synthetic_owner_a"
OTHER_OWNER = "wxid_synthetic_owner_b"
FRIEND = "wxid_synthetic_friend_a"
ALIAS = "Synthetic Friend <local> & Co."


def _canonical(username=FRIEND):
    return canonical_contact_id(username, authority="native_account_owner")


def _contact(**changes):
    return {
        "id": _canonical(),
        "platform": "wechat",
        "identity_status": "verified",
        "source_display_name": "Original source remark",
        "preferred_name": "Another source remark",
        **changes,
    }


def _account(owner=OWNER, **changes):
    return {
        "account_id": owner,
        "account_key": "account:" + owner,
        "partition_id": "ios-" + hashlib.md5(owner.encode()).hexdigest(),
        "identity_status": "Verified archive ID",
        "account_label": "Original account label",
        **changes,
    }


def _export_account(**changes):
    return _account(
        **{
            "partition_id": "html-synthetic-account",
            "identity_status": "Corroborated export ID",
            **changes,
        }
    )


def _write(root, contacts=None, *, raw=None):
    path = display_names_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (
        json.dumps(
            {"schema_version": 1, "contacts": contacts or {_canonical(): ALIAS}},
            ensure_ascii=False,
        ).encode()
        if raw is None
        else raw
    )
    path.write_bytes(data)
    return path


def test_missing_mapping_is_read_only_and_uses_the_private_messaging_path(tmp_path):
    path = display_names_path(tmp_path)
    assert path == tmp_path / "messaging" / "wechat" / "display_names.json"
    names = WechatDisplayNames(tmp_path)
    assert names.contact_name(_contact()) == ""
    assert names.account_label(_account()) == ""
    assert not path.exists()
    assert not path.parent.exists()


def test_hidden_source_preferences_keep_existing_names_and_exact_source_boundaries(
    tmp_path,
):
    hidden = "pdf-group-research"
    contacts = {_canonical(): ALIAS, _canonical(OWNER): "Existing account name"}
    path = _write(
        tmp_path,
        raw=json.dumps(
            {
                "schema_version": 1,
                "contacts": contacts,
                "hidden_account_sources": [hidden],
            }
        ).encode(),
    )
    before = path.read_bytes()
    names = WechatDisplayNames(tmp_path)
    assert names.contact_name(_contact()) == ALIAS
    assert names.account_label(_account()) == "Existing account name · " + OWNER[-6:]
    assert names.account_option_hidden({"partition_id": hidden})
    assert names.account_option_hidden({"id": hidden})
    for row in (
        {"partition_id": hidden + "-copy"},
        {"partition_id": "PDF-group-research"},
        {"partition_id": "", "id": hidden},
        {"partition_id": None, "id": hidden},
        {"account_key": "source:" + hidden},
        {"name": hidden},
        _account(),
    ):
        assert not names.account_option_hidden(row)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "hidden",
    [
        None,
        "pdf-group-research",
        {"source": "pdf-group-research"},
        [None],
        [123],
        [""],
        [" pdf-group-research"],
        ["pdf/group/research"],
        ["x" * 101],
        ["pdf-group-research", {}],
        ["pdf-group-research"] * 129,
    ],
)
def test_invalid_hidden_source_lists_leave_valid_names_and_all_options_available(
    tmp_path, hidden
):
    path = _write(
        tmp_path,
        raw=json.dumps(
            {
                "schema_version": 1,
                "contacts": {_canonical(): ALIAS},
                "hidden_account_sources": hidden,
            }
        ).encode(),
    )
    before = path.read_bytes()
    names = WechatDisplayNames(tmp_path)
    assert names.contact_name(_contact()) == ALIAS
    assert not names.account_option_hidden({"partition_id": "pdf-group-research"})
    assert path.read_bytes() == before


def test_legacy_preferences_and_removing_hidden_sources_restore_visibility(tmp_path):
    path = _write(tmp_path)
    legacy = path.read_bytes()
    row = {"partition_id": "pdf-group-research"}
    assert not WechatDisplayNames(tmp_path).account_option_hidden(row)
    preferences = json.loads(legacy)
    preferences["hidden_account_sources"] = [row["partition_id"]]
    path.write_text(json.dumps(preferences))
    assert WechatDisplayNames(tmp_path).account_option_hidden(row)
    preferences["hidden_account_sources"] = []
    path.write_text(json.dumps(preferences))
    restored = WechatDisplayNames(tmp_path)
    assert not restored.account_option_hidden(row)
    assert restored.contact_name(_contact()) == ALIAS
    assert json.loads(path.read_text())["contacts"] == json.loads(legacy)["contacts"]


def test_same_canonical_contact_uses_one_literal_alias_across_sources(tmp_path):
    path = _write(tmp_path)
    before = path.read_bytes()
    contacts = [
        _contact(
            source_id="synthetic-source-a",
            source_display_name="Original remark A",
            account_id=OWNER,
        ),
        _contact(
            source_id="synthetic-source-b",
            source_display_name="Original remark B",
            account_id=OTHER_OWNER,
        ),
    ]
    original_contacts = deepcopy(contacts)
    names = WechatDisplayNames(tmp_path)
    assert [names.contact_name(contact) for contact in contacts] == [ALIAS, ALIAS]
    assert contacts == original_contacts
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "changes",
    [
        {"platform": "other"},
        {"platform": "WeChat"},
        {"platform": ""},
        {"identity_status": "unresolved"},
        {"identity_status": "source_scoped"},
        {"identity_status": "verified_internal_username"},
        {"identity_status": "system"},
        {"identity_status": "Verified"},
        {"identity_status": ""},
        {"id": _canonical(OTHER_OWNER)},
        {"id": FRIEND},
        {"id": ""},
    ],
)
def test_contact_alias_never_upgrades_an_unverified_or_other_identity(
    tmp_path, changes
):
    _write(tmp_path)
    contact = _contact(**changes)
    contact["source_display_name"] = ALIAS
    contact["preferred_name"] = ALIAS
    contact["username"] = FRIEND
    original = deepcopy(contact)
    assert WechatDisplayNames(tmp_path).contact_name(contact) == ""
    assert contact == original


@pytest.mark.parametrize(
    "invalid_id",
    ["wxid_synthetic_friend_a", "wechat:source:synthetic", "wechat:contact:short"],
)
def test_noncanonical_mapping_keys_cannot_become_contact_identity(tmp_path, invalid_id):
    _write(tmp_path, {invalid_id: ALIAS})
    assert WechatDisplayNames(tmp_path).contact_name(_contact(id=invalid_id)) == ""


@pytest.mark.parametrize("alternate_keys", [False, True])
def test_account_alias_requires_exact_owner_key_and_partition(tmp_path, alternate_keys):
    alias = "Synthetic Account A"
    path = _write(tmp_path, {_canonical(OWNER): alias})
    row = _account()
    if alternate_keys:
        row["key"] = row.pop("account_key")
        row["id"] = row.pop("partition_id")
    original = deepcopy(row)
    before = path.read_bytes()
    names = WechatDisplayNames(tmp_path)
    assert names.account_owner(row) == OWNER
    assert names.account_label(row) == alias + " · " + OWNER[-6:]
    assert row == original
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "changes",
    [
        {"account_id": OTHER_OWNER},
        {"account_id": ""},
        {"account_key": "account:" + OTHER_OWNER},
        {"account_key": "source:synthetic"},
        {"account_key": ""},
        {"partition_id": "ios-" + hashlib.md5(OTHER_OWNER.encode()).hexdigest()},
        {"partition_id": "ios-" + "0" * 32},
        {"partition_id": ""},
        {"identity_status": "Source account unconfirmed"},
        {"identity_status": "verified"},
        {"identity_status": ""},
    ],
)
def test_account_alias_does_not_cross_owner_or_provenance_boundaries(tmp_path, changes):
    _write(tmp_path, {_canonical(OWNER): "Synthetic Account A"})
    row = _account(**changes)
    names = WechatDisplayNames(tmp_path)
    assert names.account_owner(row) == ""
    assert names.account_label(row) == ""


def test_fallback_account_keys_do_not_override_conflicting_primary_fields(tmp_path):
    _write(tmp_path, {_canonical(OWNER): "Synthetic Account A"})
    wrong_key = _account(account_key="account:" + OTHER_OWNER, key="account:" + OWNER)
    wrong_partition = _account(
        partition_id="ios-" + hashlib.md5(OTHER_OWNER.encode()).hexdigest(),
        id="ios-" + hashlib.md5(OWNER.encode()).hexdigest(),
    )
    names = WechatDisplayNames(tmp_path)
    assert names.account_owner(wrong_key) == ""
    assert names.account_owner(wrong_partition) == ""
    assert names.account_label(wrong_key) == ""
    assert names.account_label(wrong_partition) == ""


@pytest.mark.parametrize("container", [list, tuple])
@pytest.mark.parametrize(
    "name", ["Native account nickname", "Native account · preserved suffix"]
)
def test_owner_message_name_uses_exact_native_account_anchor_without_mutation(
    tmp_path, container, name
):
    path = _write(tmp_path, {_canonical(OWNER): "Explicit local viewing name"})
    names = WechatDisplayNames(tmp_path)
    contact = _contact(id=_canonical(OWNER))
    context = _account(direction="sent", sender_id=OWNER)
    anchor = _account(name=name + " · " + OWNER[-6:])
    anchors = container([anchor])
    original = deepcopy((contact, context, anchors))
    before = path.read_bytes()

    assert names.contact_name(contact) == "Explicit local viewing name"
    assert (
        names.owner_message_name(contact, context, verified_accounts=anchors) == name
    )
    assert (contact, context, anchors) == original
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "changes",
    [
        {"id": _canonical(FRIEND)},
        {"id": OWNER},
        {"id": "wechat:contact:short"},
        {"platform": "another_platform"},
        {"identity_status": "source_scoped"},
        {"identity_status": "unresolved"},
        {"identity_status": "Verified"},
    ],
)
def test_owner_message_name_requires_the_exact_verified_owner_contact(tmp_path, changes):
    names = WechatDisplayNames(tmp_path)
    contact = _contact(**{"id": _canonical(OWNER), **changes})
    context = _account(direction="sent", sender_id=OWNER)
    anchor = _account(name="Native account nickname · " + OWNER[-6:])
    assert names.owner_message_name(contact, context, verified_accounts=[anchor]) == ""


@pytest.mark.parametrize(
    "changes",
    [
        {"account_id": OTHER_OWNER},
        {"account_key": "account:" + OTHER_OWNER},
        {"account_key": "source:synthetic"},
        {"partition_id": "ios-" + hashlib.md5(OTHER_OWNER.encode()).hexdigest()},
        {"partition_id": "ios-" + "0" * 32},
        {"identity_status": "Corroborated export ID"},
        {"identity_status": "Source account unconfirmed"},
        {"direction": "received"},
        {"direction": "unknown"},
        {"sender_id": FRIEND},
        {"sender_id": OTHER_OWNER},
        {"sender_id": OWNER + " "},
        {"sender_id": None},
    ],
)
def test_owner_message_name_rejects_nonowner_direction_and_forged_context(
    tmp_path, changes
):
    names = WechatDisplayNames(tmp_path)
    contact = _contact(id=_canonical(OWNER))
    context = _account(**{"direction": "sent", "sender_id": OWNER, **changes})
    anchor = _account(name="Native account nickname · " + OWNER[-6:])
    assert names.owner_message_name(contact, context, verified_accounts=[anchor]) == ""


@pytest.mark.parametrize(
    "changes",
    [
        {"account_id": OTHER_OWNER},
        {"account_key": "account:" + OTHER_OWNER},
        {"account_key": "source:synthetic"},
        {"partition_id": "ios-" + hashlib.md5(OTHER_OWNER.encode()).hexdigest()},
        {"partition_id": "ios-" + "0" * 32},
        {"partition_id": "html-synthetic-account"},
        {"identity_status": "Corroborated export ID"},
        {"identity_status": "Source account unconfirmed"},
    ],
)
def test_owner_message_name_rejects_forged_or_other_partition_anchors(tmp_path, changes):
    names = WechatDisplayNames(tmp_path)
    contact = _contact(id=_canonical(OWNER))
    context = _account(direction="sent", sender_id=OWNER)
    anchor = _account(**{"name": "Native account nickname · " + OWNER[-6:], **changes})
    assert names.owner_message_name(contact, context, verified_accounts=[anchor]) == ""


@pytest.mark.parametrize(
    "name",
    [
        None,
        "",
        OWNER,
        OWNER + " · " + OWNER[-6:],
        " · " + OWNER[-6:],
        "Native account nickname",
        "Native account nickname · " + OTHER_OWNER[-6:],
        "Native account nickname · " + OWNER[-6:] + " extra",
    ],
)
def test_owner_message_name_requires_a_generated_nonidentifier_account_label(
    tmp_path, name
):
    names = WechatDisplayNames(tmp_path)
    contact = _contact(id=_canonical(OWNER))
    context = _account(direction="sent", sender_id=OWNER)
    anchor = _account(name=name)
    assert names.owner_message_name(contact, context, verified_accounts=[anchor]) == ""


@pytest.mark.parametrize("anchors", [(), [], None, {}, OWNER, [None], ["Native name"]])
def test_owner_message_name_requires_a_usable_native_anchor(tmp_path, anchors):
    names = WechatDisplayNames(tmp_path)
    contact = _contact(id=_canonical(OWNER))
    context = _account(direction="sent", sender_id=OWNER)
    assert names.owner_message_name(contact, context, verified_accounts=anchors) == ""


def test_owner_message_name_does_not_promote_a_corroborated_export_to_native(tmp_path):
    names = WechatDisplayNames(tmp_path)
    contact = _contact(id=_canonical(OWNER))
    context = _export_account(direction="sent", sender_id=OWNER)
    anchor = _account(name="Native account nickname · " + OWNER[-6:])
    assert names.account_owner(context, verified_accounts=[anchor]) == OWNER
    assert names.owner_message_name(contact, context, verified_accounts=[anchor]) == ""


def test_account_alias_uses_the_same_verified_contact_mapping(tmp_path):
    alias = "Synthetic Account A"
    _write(tmp_path, {_canonical(OWNER): alias})
    names = WechatDisplayNames(tmp_path)
    assert names.contact_name(_contact(id=_canonical(OWNER))) == alias
    assert names.account_label(_account()) == alias + " · " + OWNER[-6:]
    assert names.account_label(_account(OTHER_OWNER)) == ""


@pytest.mark.parametrize("alternate_keys", [False, True])
def test_corroborated_account_alias_requires_an_independent_native_anchor(
    tmp_path, alternate_keys
):
    alias = "Synthetic Account A"
    path = _write(tmp_path, {_canonical(OWNER): alias})
    row = _export_account()
    if alternate_keys:
        row["key"] = row.pop("account_key")
        row["id"] = row.pop("partition_id")
    anchors = [_account(OTHER_OWNER), _account()]
    original = deepcopy((row, anchors))
    before = path.read_bytes()
    names = WechatDisplayNames(tmp_path)
    assert names.account_owner(row) == ""
    assert names.account_owner(row, verified_accounts=anchors) == OWNER
    assert names.account_label(row) == ""
    assert (
        names.account_label(row, verified_accounts=anchors)
        == alias + " · " + OWNER[-6:]
    )
    assert (row, anchors) == original
    assert path.read_bytes() == before


@pytest.mark.parametrize("preference_state", ["missing", "empty"])
def test_trusted_owner_proof_does_not_require_a_display_preference(
    tmp_path, preference_state
):
    path = display_names_path(tmp_path)
    if preference_state == "empty":
        _write(tmp_path, raw=b'{"schema_version":1,"contacts":{}}')
    before = path.read_bytes() if path.exists() else None
    native = _account()
    exports = [
        _export_account(partition_id=f"html-synthetic-{index}") for index in (1, 2)
    ]
    original = deepcopy((native, exports))
    names = WechatDisplayNames(tmp_path)
    assert names.account_owner(native) == OWNER
    assert names.account_label(native) == ""
    for row in exports:
        assert names.account_owner(row) == ""
        assert names.account_owner(row, verified_accounts=[native]) == OWNER
        assert names.account_label(row, verified_accounts=[native]) == ""
    assert (native, exports) == original
    assert (path.read_bytes() if path.exists() else None) == before


@pytest.mark.parametrize("owner", [OWNER, "Synthetic_Legacy_ID"])
@pytest.mark.parametrize("alternate_keys", [False, True])
def test_export_option_identity_does_not_grant_a_native_viewing_alias(
    tmp_path, owner, alternate_keys
):
    path = _write(tmp_path, {_canonical(owner): "Native-only viewing preference"})
    row = _export_account(account_id=owner, account_key="account:" + owner)
    if alternate_keys:
        row["key"] = row.pop("account_key")
        row["id"] = row.pop("partition_id")
    before = (deepcopy(row), path.read_bytes())
    names = WechatDisplayNames(tmp_path)
    assert names.account_option_owner(row) == owner
    assert names.account_owner(row) == ""
    assert names.account_label(row) == ""
    assert (row, path.read_bytes()) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"account_id": None},
        {"account_id": ""},
        {"account_key": "account:" + OTHER_OWNER},
        {"account_key": "source:synthetic"},
        {"account_key": "", "key": "account:" + OWNER},
        {"identity_status": "Source account unconfirmed"},
        {"identity_status": "Verified archive ID"},
        {"identity_status": "Corroborated export ID "},
        {"partition_id": "ios-impostor"},
        {"partition_id": ""},
        {"partition_id": None},
        {"partition_id": "html/fixture"},
        {"partition_id": "h" * 101},
        {"partition_id": "", "id": "html-synthetic-account"},
    ],
)
def test_export_option_grouping_keeps_uncertain_or_conflicting_sources_separate(
    tmp_path, changes
):
    row = _export_account(**changes)
    original = deepcopy(row)
    assert WechatDisplayNames(tmp_path).account_option_owner(row) == ""
    assert row == original


@pytest.mark.parametrize(
    "owner",
    [
        "wxid_unknwn_123",
        "12345@chatroom",
        "gh_synthetic_account",
        "filehelper",
        "+8612345678901",
        "0" * 32,
        "wxid_with whitespace",
        "wxid_" + "a" * 1024,
    ],
)
def test_export_option_identity_rejects_nonpersonal_and_placeholder_identifiers(
    tmp_path, owner
):
    row = _export_account(account_id=owner, account_key="account:" + owner)
    assert WechatDisplayNames(tmp_path).account_option_owner(row) == ""


@pytest.mark.parametrize(
    "changes",
    [
        {"account_id": OTHER_OWNER},
        {"account_key": "account:" + OTHER_OWNER},
        {"account_key": "source:synthetic"},
        {"account_key": "", "key": "account:" + OWNER},
        {"identity_status": "Source account unconfirmed"},
        {"identity_status": "Verified archive ID"},
        {"partition_id": "ios-" + hashlib.md5(OWNER.encode()).hexdigest()},
        {"partition_id": "ios-impostor"},
        {"partition_id": "html/fixture"},
        {"partition_id": ""},
        {"partition_id": "h" * 101},
        {"partition_id": None},
    ],
)
def test_native_anchor_cannot_bless_an_unconfirmed_or_mismatched_export(
    tmp_path, changes
):
    _write(tmp_path, {_canonical(OWNER): "Synthetic Account A"})
    row = _export_account(**changes)
    row["account_label"] = "Synthetic Account A"
    before = deepcopy(row)
    assert (
        WechatDisplayNames(tmp_path).account_owner(row, verified_accounts=[_account()])
        == ""
    )
    assert (
        WechatDisplayNames(tmp_path).account_label(row, verified_accounts=[_account()])
        == ""
    )
    assert row == before


@pytest.mark.parametrize(
    "anchor",
    [
        _account(OTHER_OWNER),
        _account(account_key="account:" + OTHER_OWNER),
        _account(partition_id="ios-" + "0" * 32),
        _account(identity_status="Source account unconfirmed"),
        _export_account(),
        None,
        "Synthetic Account A",
    ],
)
def test_export_alias_rejects_wrong_unverified_and_recursive_anchors(tmp_path, anchor):
    _write(tmp_path, {_canonical(OWNER): "Synthetic Account A"})
    row = _export_account()
    assert (
        WechatDisplayNames(tmp_path).account_owner(row, verified_accounts=[anchor])
        == ""
    )
    assert (
        WechatDisplayNames(tmp_path).account_label(row, verified_accounts=[anchor])
        == ""
    )


@pytest.mark.parametrize("anchors", [None, {}, OWNER, {OWNER}])
def test_export_alias_rejects_invalid_anchor_containers(tmp_path, anchors):
    _write(tmp_path, {_canonical(OWNER): "Synthetic Account A"})
    assert (
        WechatDisplayNames(tmp_path).account_owner(
            _export_account(), verified_accounts=anchors
        )
        == ""
    )
    assert (
        WechatDisplayNames(tmp_path).account_label(
            _export_account(), verified_accounts=anchors
        )
        == ""
    )


@pytest.mark.parametrize(
    "payload",
    [
        b"not JSON",
        b"{",
        b"[]",
        b"null",
        b'{"schema_version":1,"contacts":[]}',
        b'{"schema_version":1,"contacts":null}',
        b'{"schema_version":1,"schema_version":1,"contacts":{}}',
        b'{"schema_version":1,"contacts":{},"contacts":{}}',
        b"\xff\xfeinvalid UTF-8",
    ],
)
def test_malformed_mapping_falls_back_without_modifying_evidence(tmp_path, payload):
    path = _write(tmp_path, raw=payload)
    names = WechatDisplayNames(tmp_path)
    assert names.contact_name(_contact()) == ""
    assert names.account_label(_account()) == ""
    assert path.read_bytes() == payload


@pytest.mark.parametrize("schema", [0, 2, True, "1", None])
def test_unknown_schema_is_not_silently_interpreted_as_current(tmp_path, schema):
    payload = json.dumps(
        {"schema_version": schema, "contacts": {_canonical(): ALIAS}}
    ).encode()
    path = _write(tmp_path, raw=payload)
    assert WechatDisplayNames(tmp_path).contact_name(_contact()) == ""
    assert path.read_bytes() == payload


def test_duplicate_contact_key_rejects_the_ambiguous_mapping(tmp_path):
    identifier = json.dumps(_canonical())
    payload = (
        '{"schema_version":1,"contacts":{'
        + identifier
        + ':"Synthetic first",'
        + identifier
        + ':"Synthetic second"}}'
    ).encode()
    path = _write(tmp_path, raw=payload)
    assert WechatDisplayNames(tmp_path).contact_name(_contact()) == ""
    assert path.read_bytes() == payload


@pytest.mark.parametrize(
    "alias",
    [
        None,
        17,
        True,
        [],
        {},
        "",
        "Synthetic\nAlias",
        "Synthetic\rAlias",
        "Synthetic\r\nAlias",
        "Synthetic\u2028Alias",
        "Synthetic\0Alias",
        "S" * 100_000,
    ],
)
def test_invalid_or_oversized_literal_names_are_not_displayed(tmp_path, alias):
    path = _write(tmp_path, {_canonical(): alias, _canonical(OWNER): alias})
    before = path.read_bytes()
    names = WechatDisplayNames(tmp_path)
    assert names.contact_name(_contact()) == ""
    assert names.account_label(_account()) == ""
    assert path.read_bytes() == before


def test_oversized_file_is_ignored_even_when_its_json_is_otherwise_valid(tmp_path):
    payload = json.dumps(
        {"schema_version": 1, "contacts": {_canonical(): ALIAS}}
    ).encode() + b" " * (8 * 1024 * 1024)
    path = _write(tmp_path, raw=payload)
    before = hashlib.sha256(payload).hexdigest()
    assert WechatDisplayNames(tmp_path).contact_name(_contact()) == ""
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("link_component", ["file", "wechat", "messaging"])
def test_symlinked_mapping_does_not_read_external_aliases(tmp_path, link_component):
    root = tmp_path / "store"
    outside = tmp_path / "outside"
    target = _write(outside)
    before = target.read_bytes()
    destination = display_names_path(root)
    if link_component == "wechat":
        destination.parent.parent.mkdir(parents=True)
        link = destination.parent
        link.symlink_to(target.parent, target_is_directory=True)
    elif link_component == "messaging":
        root.mkdir()
        link = destination.parent.parent
        link.symlink_to(target.parent.parent, target_is_directory=True)
    else:
        destination.parent.mkdir(parents=True)
        link = destination
        link.symlink_to(target)
    assert WechatDisplayNames(root).contact_name(_contact()) == ""
    assert target.read_bytes() == before
    assert link.is_symlink()


def test_explicitly_selected_root_may_itself_be_a_symlink(tmp_path):
    actual = tmp_path / "actual-store"
    _write(actual)
    selected = tmp_path / "selected-store"
    selected.symlink_to(actual, target_is_directory=True)
    assert WechatDisplayNames(selected).contact_name(_contact()) == ALIAS
    assert selected.is_symlink()


def test_new_instance_reads_updated_mapping_and_recovers_after_invalid_json(tmp_path):
    path = _write(tmp_path)
    assert WechatDisplayNames(tmp_path).contact_name(_contact()) == ALIAS
    path.write_text("{", encoding="utf-8")
    assert WechatDisplayNames(tmp_path).contact_name(_contact()) == ""
    updated = "Synthetic revised alias"
    _write(tmp_path, {_canonical(): updated})
    assert WechatDisplayNames(tmp_path).contact_name(_contact()) == updated
    assert json.loads(path.read_text())["contacts"][_canonical()] == updated
