"""Local display aliases require verified identity and never rewrite source evidence.

Code version: v1.0.0-codex.0
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
    assert WechatDisplayNames(tmp_path).account_label(row) == alias + " · " + OWNER[-6:]
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
    assert WechatDisplayNames(tmp_path).account_label(row) == ""


def test_fallback_account_keys_do_not_override_conflicting_primary_fields(tmp_path):
    _write(tmp_path, {_canonical(OWNER): "Synthetic Account A"})
    wrong_key = _account(account_key="account:" + OTHER_OWNER, key="account:" + OWNER)
    wrong_partition = _account(
        partition_id="ios-" + hashlib.md5(OTHER_OWNER.encode()).hexdigest(),
        id="ios-" + hashlib.md5(OWNER.encode()).hexdigest(),
    )
    names = WechatDisplayNames(tmp_path)
    assert names.account_label(wrong_key) == ""
    assert names.account_label(wrong_partition) == ""


def test_account_alias_uses_the_same_verified_contact_mapping(tmp_path):
    alias = "Synthetic Account A"
    _write(tmp_path, {_canonical(OWNER): alias})
    names = WechatDisplayNames(tmp_path)
    assert names.contact_name(_contact(id=_canonical(OWNER))) == alias
    assert names.account_label(_account()) == alias + " · " + OWNER[-6:]
    assert names.account_label(_account(OTHER_OWNER)) == ""


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
