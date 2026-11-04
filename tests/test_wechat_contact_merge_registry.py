"""Explicit person declarations and reference unions preserve native evidence.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from contextlib import ExitStack, closing
from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest

from app.core import wechat_contact_merges as merges
from app.core.wechat_contact_analysis import _REFERENCE_SCHEMA
from app.core.wechat_contacts import canonical_contact_id


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _contact(username):
    return canonical_contact_id(username, authority="native_contact_username")


FIRST = _contact("wxid_merge_first")
SECOND = _contact("wxid_merge_second")
THIRD = _contact("wxid_merge_third")
FOURTH = _contact("wxid_merge_fourth")


def _declaration(target=FIRST, members=None):
    return {
        "target_contact_id": target,
        "member_contact_ids": [FIRST, SECOND] if members is None else members,
        "authority": "user_confirmed_same_person",
        "evidence_id": _digest("Synthetic explicit user confirmation."),
        "confirmed_at": "2000-01-02T03:04:05+00:00",
        "confirmation": "The user explicitly confirmed these synthetic identities.",
    }


def _write_registry(root, associations):
    path = merges.contact_merge_registry_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 1, "associations": associations}),
        encoding="utf-8",
    )
    return path


def test_missing_and_empty_declarations_do_not_associate_or_create_files(tmp_path):
    path = merges.contact_merge_registry_path(tmp_path)
    missing = merges.read_contact_merges(tmp_path)
    assert missing["associations"] == []
    assert not path.exists()
    scope = merges.resolve_contact_merge(tmp_path, FIRST)
    assert scope["is_associated"] is False
    assert scope["canonical_contact_id"] == FIRST
    assert scope["member_contact_ids"] == [FIRST]

    _write_registry(tmp_path, [])
    preserved = path.read_bytes()
    empty = merges.read_contact_merges(tmp_path)
    assert empty["associations"] == []
    assert empty["digest"] != missing["digest"]
    assert path.read_bytes() == preserved


def test_valid_declaration_resolves_both_members_and_preserves_source(tmp_path):
    declaration = _declaration(members=[SECOND, FIRST])
    declaration["native_identities"] = [
        {"contact_id": SECOND, "username": "wxid_merge_second"},
        {"contact_id": FIRST, "username": "wxid_merge_first"},
    ]
    path = _write_registry(tmp_path, [declaration])
    preserved = path.read_bytes()
    before = merges.read_contact_merges(tmp_path)
    for requested in (FIRST, SECOND):
        scope = merges.resolve_contact_merge(tmp_path, requested)
        assert scope["requested_contact_id"] == requested
        assert scope["canonical_contact_id"] == FIRST
        assert scope["member_contact_ids"] == [FIRST, SECOND]
        assert scope["association_authority"] == merges.AUTHORITY
        assert scope["association_evidence_id"] == declaration["evidence_id"]
        assert scope["native_identity_pins"] == declaration["native_identities"]
        assert scope["registry_digest"] == before["digest"]
    unrelated = merges.resolve_contact_merge(tmp_path, THIRD)
    assert unrelated["is_associated"] is False
    assert unrelated["canonical_contact_id"] == THIRD
    assert path.read_bytes() == preserved


def test_disjoint_declarations_do_not_extend_each_others_membership(tmp_path):
    path = _write_registry(
        tmp_path,
        [_declaration(), _declaration(THIRD, [THIRD, FOURTH])],
    )
    preserved = path.read_bytes()
    first = merges.resolve_contact_merge(tmp_path, SECOND)
    second = merges.resolve_contact_merge(tmp_path, FOURTH)
    assert first["canonical_contact_id"] == FIRST
    assert set(first["member_contact_ids"]) == {FIRST, SECOND}
    assert second["canonical_contact_id"] == THIRD
    assert set(second["member_contact_ids"]) == {THIRD, FOURTH}
    assert path.read_bytes() == preserved


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("authority", "same_display_name"),
        ("authority", "native_contact_username"),
        ("evidence_id", "an unpinned assertion"),
        ("target_contact_id", "wxid_merge_first"),
        ("target_contact_id", THIRD),
        ("member_contact_ids", [FIRST]),
        ("member_contact_ids", [FIRST, FIRST]),
        ("member_contact_ids", [FIRST, "wechat:contact:source:" + "a" * 64]),
        ("confirmation", ""),
        ("confirmed_at", False),
    ],
)
def test_untrusted_or_ambiguous_declarations_fail_closed(tmp_path, field, value):
    declaration = _declaration()
    declaration[field] = value
    path = _write_registry(tmp_path, [declaration])
    preserved = path.read_bytes()
    with pytest.raises(ValueError):
        merges.resolve_contact_merge(tmp_path, FIRST)
    assert path.read_bytes() == preserved


@pytest.mark.parametrize(
    "pins",
    [
        "not a list",
        [{"contact_id": FIRST, "username": "wxid_merge_first"}],
        [
            {"contact_id": FIRST, "username": "wxid_merge_first"},
            {"contact_id": FIRST, "username": "wxid_merge_first"},
        ],
        [
            {"contact_id": FIRST, "username": "wxid_merge_first"},
            {"contact_id": THIRD, "username": "wxid_merge_third"},
        ],
        [
            {"contact_id": FIRST, "username": "wxid_merge_first"},
            {"contact_id": SECOND, "username": ""},
        ],
        [
            {"contact_id": FIRST, "username": "wxid_merge_first"},
            {"contact_id": SECOND, "username": "wxid_merge_second", "trusted": True},
        ],
    ],
)
def test_native_pin_structure_must_cover_every_distinct_member(tmp_path, pins):
    declaration = _declaration()
    declaration["native_identities"] = pins
    path = _write_registry(tmp_path, [declaration])
    preserved = path.read_bytes()
    with pytest.raises(ValueError):
        merges.read_contact_merges(tmp_path)
    assert path.read_bytes() == preserved


@pytest.mark.parametrize(
    "second",
    [
        _declaration(SECOND, [SECOND, THIRD]),
        _declaration(THIRD, [THIRD, FIRST]),
        _declaration(FIRST, [FIRST, SECOND]),
    ],
)
def test_overlapping_associations_and_chains_are_rejected(tmp_path, second):
    path = _write_registry(tmp_path, [_declaration(), deepcopy(second)])
    preserved = path.read_bytes()
    with pytest.raises(ValueError, match="overlap|repeat|chains"):
        merges.read_contact_merges(tmp_path)
    assert path.read_bytes() == preserved


@pytest.mark.parametrize(
    "raw",
    [
        '{"schema_version":1,"schema_version":1,"associations":[]}',
        '{"schema_version":true,"associations":[]}',
        '{"schema_version":1,"associations":[],"trust_names":true}',
        '{"schema_version":1,"associations":[{"authority":"a","authority":"b"}]}',
        "{not valid JSON}",
    ],
)
def test_malformed_schema_and_duplicate_keys_are_never_accepted(tmp_path, raw):
    path = _write_registry(tmp_path, [])
    path.write_text(raw, encoding="utf-8")
    preserved = path.read_bytes()
    with pytest.raises(ValueError):
        merges.read_contact_merges(tmp_path)
    assert path.read_bytes() == preserved


def test_registry_reads_enforce_size_and_stable_source_identity(tmp_path, monkeypatch):
    path = _write_registry(tmp_path, [_declaration()])
    preserved = path.read_bytes()
    with monkeypatch.context() as limited:
        limited.setattr(merges, "MAX_REGISTRY_BYTES", 16)
        with pytest.raises(ValueError, match="too large"):
            merges.read_contact_merges(tmp_path)
    initial = merges._identity(path)
    observations = iter([initial, (*initial[:-1], initial[-1] + 1)])
    monkeypatch.setattr(merges, "_identity", lambda unused: next(observations))
    with pytest.raises(merges.ContactMergeSourceChanged, match="changed while reading"):
        merges.read_contact_merges(tmp_path)
    assert path.read_bytes() == preserved


def test_symlinked_declaration_cannot_replace_another_file(tmp_path):
    target = tmp_path / "retained-declaration.json"
    target.write_text('{"schema_version":1,"associations":[]}', encoding="utf-8")
    path = merges.contact_merge_registry_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.symlink_to(target)
    preserved = target.read_bytes()
    with pytest.raises(ValueError, match="local regular files|symbolic links"):
        merges.read_contact_merges(tmp_path)
    assert path.is_symlink()
    assert target.read_bytes() == preserved


def _reference(name, *, conversation="synthetic-original-room"):
    identifier = _digest("reference:" + name)
    return (
        identifier,
        _digest("parent:" + name),
        "[]",
        "wechat_message_" + identifier,
        conversation,
        "synthetic-source",
        "account:wxid_merge_viewer",
        946782245.0,
        1,
        1,
        "2000-01-02T03:04:05+00:00",
        "2 Jan 2000 03:04:05",
        "UTC+00:00",
        "",
    )


def _source(references, groups, membership):
    source = sqlite3.connect(":memory:")
    source.executescript(_REFERENCE_SCHEMA)
    source.executemany(
        "INSERT INTO refs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", references
    )
    source.executemany("INSERT INTO statement_groups VALUES(?,?,?)", groups)
    source.executemany("INSERT INTO statement_members VALUES(?,?)", membership)
    return source


def _dump(source):
    return tuple(source.iterdump())


def test_shared_references_union_transitive_groups_without_losing_members():
    a, b, c, d = [_reference(name) for name in "abcd"]
    one, two, three = [_digest("group:" + name) for name in ("one", "two", "three")]
    specifications = [
        (
            FIRST,
            [a, b],
            [(one, a[0], "same-original-room")],
            [(a[0], one), (b[0], one)],
        ),
        (
            SECOND,
            [b, c],
            [(two, b[0], "same-original-room")],
            [(b[0], two), (c[0], two)],
        ),
        (
            THIRD,
            [c, d],
            [(three, c[0], "same-original-room")],
            [(c[0], three), (d[0], three)],
        ),
    ]
    with ExitStack() as stack:
        members = [
            (contact, stack.enter_context(closing(_source(refs, groups, membership))))
            for contact, refs, groups, membership in specifications
        ]
        preserved = [_dump(source) for _, source in members]
        output = stack.enter_context(closing(sqlite3.connect(":memory:")))
        output.executescript(_REFERENCE_SCHEMA)
        result = merges.copy_contact_reference_union(output, members)
        assert result == {"references": 4, "statements": 1}
        assert output.execute("SELECT * FROM statement_groups").fetchall() == [
            (one, a[0], "same-original-room")
        ]
        assert dict(output.execute("SELECT * FROM statement_members")) == {
            row[0]: one for row in (a, b, c, d)
        }
        assert (
            output.execute("SELECT COUNT(*) FROM person_reference_contacts").fetchone()[
                0
            ]
            == 6
        )
        assert (
            output.execute("SELECT COUNT(*) FROM person_member_groups").fetchone()[0]
            == 3
        )
        assert [_dump(source) for _, source in members] == preserved


@pytest.mark.parametrize("conflict", ["reference_metadata", "group_context"])
def test_conflicting_exact_references_or_event_context_fail_closed(conflict):
    reference = _reference("shared")
    group = _digest("shared-group")
    changed = list(reference)
    if conflict == "reference_metadata":
        changed[-1] = "a conflicting source clock"
    contexts = (
        "first-room",
        "other-room" if conflict == "group_context" else "first-room",
    )
    with ExitStack() as stack:
        members = []
        for contact, value, context in zip(
            (FIRST, SECOND), (reference, tuple(changed)), contexts
        ):
            source = stack.enter_context(
                closing(
                    _source(
                        [value],
                        [(group, reference[0], context)],
                        [(reference[0], group)],
                    )
                )
            )
            members.append((contact, source))
        preserved = [_dump(source) for _, source in members]
        output = stack.enter_context(closing(sqlite3.connect(":memory:")))
        output.executescript(_REFERENCE_SCHEMA)
        with pytest.raises(merges.ContactMergeSourceChanged, match="conflicting"):
            merges.copy_contact_reference_union(output, members)
        assert output.execute("SELECT COUNT(*) FROM refs").fetchone()[0] == 0
        assert [_dump(source) for _, source in members] == preserved


def test_distinct_same_clock_references_are_not_collapsed_without_event_evidence():
    a, b = _reference("a"), _reference("b")
    one, two = _digest("group-one"), _digest("group-two")
    with (
        closing(
            _source(
                [a, b],
                [(one, a[0], "same-room"), (two, b[0], "same-room")],
                [(a[0], one), (b[0], two)],
            )
        ) as source,
        closing(sqlite3.connect(":memory:")) as output,
    ):
        preserved = _dump(source)
        output.executescript(_REFERENCE_SCHEMA)
        assert merges.copy_contact_reference_union(output, [(FIRST, source)]) == {
            "references": 2,
            "statements": 2,
        }
        assert _dump(source) == preserved


def test_union_bound_and_checkpoint_failure_leave_source_caches_unchanged(monkeypatch):
    a, b = _reference("a"), _reference("b")
    group = _digest("bounded-group")
    with closing(
        _source([a, b], [(group, a[0], "same-room")], [(a[0], group), (b[0], group)])
    ) as source:
        preserved = _dump(source)
        with closing(sqlite3.connect(":memory:")) as output:
            output.executescript(_REFERENCE_SCHEMA)
            with monkeypatch.context() as limited:
                limited.setattr(merges, "MAX_UNION_REFERENCES", 1)
                with pytest.raises(ValueError, match="bounded limit"):
                    merges.copy_contact_reference_union(output, [(FIRST, source)])
            assert output.execute("SELECT COUNT(*) FROM refs").fetchone()[0] == 0
        with closing(sqlite3.connect(":memory:")) as output:
            output.executescript(_REFERENCE_SCHEMA)
            checkpoints = []

            def changed():
                checkpoints.append(True)
                if len(checkpoints) > 1:
                    raise merges.ContactMergeSourceChanged("Synthetic source change.")

            with pytest.raises(
                merges.ContactMergeSourceChanged, match="Synthetic source"
            ):
                merges.copy_contact_reference_union(
                    output, [(FIRST, source)], checkpoint=changed
                )
            assert len(checkpoints) > 1
            assert output.execute("SELECT COUNT(*) FROM refs").fetchone()[0] == 0
        assert _dump(source) == preserved
