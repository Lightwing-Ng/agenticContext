"""Preserve exact source scopes when projecting retained contact names.

Code version: v1.0.2-codex.0
All registries are in memory. No production data or raw source is opened.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3
import unittest

from app.core import wechat_contacts as C


class RetainedNameContextTests(unittest.TestCase):
    def test_future_snapshot_binding_does_not_invalidate_original_name_anchor(self):
        before = self.resolve()
        later = {**self.proof, "snapshot_id": "later-native-snapshot"}
        self.c.execute(
            "INSERT INTO sender_bindings VALUES(?,?,?,?,?,?,?)",
            (
                self.source, self.sender, later["snapshot_id"], "", self.cid,
                "native_contact_username", C._json(later),
            ),
        )
        after = self.resolve()
        self.assertEqual(after, before)
        later_result = self.resolve(source_snapshot_id=later["snapshot_id"])
        self.assertEqual(later_result["preferred_name"], before["preferred_name"])
        self.assertEqual(
            later_result["preferred_name_context"]["observation_sha256"],
            before["preferred_name_context"]["observation_sha256"],
        )

    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        C.initialize_contact_registry(self.c)
        self.addCleanup(self.c.close)
        self.owner, self.sender = "wxid_fixture_owner", "wxid_fixture_sender"
        self.account_hash = hashlib.md5(self.owner.encode(), usedforsecurity=False).hexdigest()
        self.source = "ios-" + self.account_hash
        self.old, self.new = "original-name-snapshot", "current-native-snapshot"
        self.cid = C.canonical_contact_id(self.sender, authority="native_contact_username")
        self.c.execute("INSERT INTO contacts VALUES(?,?,?,?,?,?)",
                       (self.cid, "wechat", "", self.sender, "personal", "verified"))
        self.old_binding = dict(
            source_id=self.source, sender_id=self.sender, source_snapshot=self.old,
            room_id="", contact_id=self.cid, authority="native_contact_username",
            evidence='{"native_evidence":"original"}',
        )
        self.c.execute("INSERT INTO sender_bindings VALUES(?,?,?,?,?,?,?)",
                       tuple(self.old_binding.values()))
        relative = f"Documents/{self.account_hash}/DB/WCDB_Contact.sqlite"
        self.proof = dict(
            kind="native_contact_snapshot_identity_binding_v1",
            source_metadata_sha256="a" * 64, source_id=self.source,
            snapshot_id=self.new, owner_account_id=self.owner,
            account_hash=self.account_hash, database_relative_path=relative,
            database_sha256="b" * 64,
            file_id=hashlib.sha1(("AppDomain-com.tencent.xin-" + relative).encode(),
                                usedforsecurity=False).hexdigest(),
            native_table="Friend", native_rowid=5, native_column="userName",
            native_username=self.sender, native_openim_appid="",
            generic_anchor_sha256=hashlib.sha256(C._json([self.old_binding]).encode()).hexdigest(),
            prior_name_context_snapshot=self.old,
            names_imported_or_ranked=False, backup_time_is_name_update_time=False,
        )
        self.c.execute("INSERT INTO sender_bindings VALUES(?,?,?,?,?,?,?)",
                       (self.source, self.sender, self.new, "", self.cid,
                        "native_contact_username", C._json(self.proof)))
        self.add_name("retained-remark", self.old, "remark", "Retained source remark")
        self.add_name("retained-nickname", self.old, "nickname", "Global profile nickname")
        self.message = dict(
            id="message-fixture", source_id=self.source, conversation_id="conversation-fixture",
            sender_id=self.sender, sender_label=self.sender, source_snapshot_id=self.new,
            account_id=self.owner, conversation_kind="direct", peer_id=self.sender, is_system=False,
        )
        self.repo = C.WechatContactRepository(Path("/not-used-by-memory-tests"))

    def add_name(self, ident, snapshot, kind, value, *, owner=None, room="", source=None):
        self.c.execute("INSERT INTO contact_observations VALUES(?,?,?,?,?,?,?,?,?)",
                       (ident, self.cid, source or self.source, snapshot,
                        self.owner if owner is None else owner, room, kind, value, "{}"))

    def rewrite_proof(self, **changes):
        self.proof.update(changes)
        self.c.execute("UPDATE sender_bindings SET evidence=? WHERE source_snapshot=?",
                       (C._json(self.proof), self.new))

    def resolve(self, **message_changes):
        request = {**self.message, **message_changes}
        return self.repo._resolve(self.c, [request])[request["id"]]

    def assert_raw(self, result, status=None):
        self.assertEqual(result["preferred_name"], self.sender)
        if status:
            self.assertEqual(result["preferred_name_context"]["status"], status)

    def test_valid_projection_retains_original_observation_and_raw_label(self):
        before = [tuple(row) for row in self.c.execute("SELECT * FROM contact_observations ORDER BY id")]
        result = self.resolve()
        self.assertEqual(result["preferred_name"], "Retained source remark")
        self.assertEqual(result["source_display_name"], self.sender)
        origin = result["preferred_name_context"]
        self.assertEqual(origin["observation_id"], "retained-remark")
        self.assertEqual(origin["original_source_snapshot"], self.old)
        self.assertFalse(origin["name_observation_copied"])
        self.assertFalse(origin["native_database_reopened"])
        self.assertEqual(before, [tuple(row) for row in self.c.execute(
            "SELECT * FROM contact_observations ORDER BY id")])
        self.assertEqual(self.c.execute(
            "SELECT COUNT(*) FROM contact_observations WHERE source_snapshot=?", (self.new,)).fetchone()[0], 0)

    def test_current_real_name_wins_even_with_malformed_old_alias(self):
        self.rewrite_proof(file_id="wrong")
        self.add_name("current", self.new, "nickname", "Actual current name")
        result = self.resolve()
        self.assertEqual(result["preferred_name"], "Actual current name")
        self.assertNotIn("preferred_name_context", result)

    def test_current_real_raw_identifier_observation_is_not_treated_as_missing(self):
        self.add_name("current-raw", self.new, "remark", self.sender)
        result = self.resolve()
        self.assertEqual(result["preferred_name"], self.sender)
        self.assertNotIn("preferred_name_context", result)

    def test_applicable_blank_snapshot_name_wins(self):
        self.add_name("blank", "", "remark", "Existing generic preference")
        result = self.resolve()
        self.assertEqual(result["preferred_name"], "Existing generic preference")
        self.assertNotIn("preferred_name_context", result)

    def test_owner_cannot_be_inferred_from_source_when_request_disagrees(self):
        self.assert_raw(self.resolve(account_id="wxid_other_owner"),
                        "invalid_retained_source_name_context")

    def test_proof_source_snapshot_and_username_must_match_exactly(self):
        for field, value in [
            ("source_id", "ios-other"), ("snapshot_id", "other"),
            ("native_username", self.sender.upper()),
            ("owner_account_id", "wxid_other_owner"), ("account_hash", "0" * 32),
        ]:
            with self.subTest(field=field):
                original = self.proof[field]
                self.rewrite_proof(**{field: value})
                self.assert_raw(self.resolve(), "invalid_retained_source_name_context")
                self.rewrite_proof(**{field: original})

    def test_native_locator_and_full_checksum_shapes_are_required(self):
        for field, value in [
            ("file_id", "0" * 40), ("database_relative_path", "Documents/other/DB/WCDB_Contact.sqlite"),
            ("database_sha256", "not-a-full-checksum"), ("source_metadata_sha256", ""),
            ("native_table", "Other"), ("native_column", "other"),
            ("native_rowid", True), ("native_rowid", 0),
            ("native_openim_appid", "unknown-realm"), ("names_imported_or_ranked", True),
            ("backup_time_is_name_update_time", True),
        ]:
            with self.subTest(field=field, value=value):
                original = self.proof[field]
                self.rewrite_proof(**{field: value})
                self.assert_raw(self.resolve(), "invalid_retained_source_name_context")
                self.rewrite_proof(**{field: original})

    def test_prior_snapshot_needs_actual_checksummed_generic_anchor(self):
        self.rewrite_proof(prior_name_context_snapshot="invented")
        self.assert_raw(self.resolve(), "invalid_retained_source_name_context")

    def test_empty_or_current_prior_snapshot_is_rejected(self):
        for prior in ("", self.new):
            self.rewrite_proof(prior_name_context_snapshot=prior)
            self.assert_raw(self.resolve(), "invalid_retained_source_name_context")

    def test_old_anchor_byte_change_is_detected(self):
        self.c.execute("UPDATE sender_bindings SET evidence='{}' WHERE source_snapshot=?", (self.old,))
        self.assert_raw(self.resolve(), "invalid_retained_source_name_context")

    def test_additional_generic_anchor_requires_separate_review(self):
        self.c.execute("INSERT INTO sender_bindings VALUES(?,?,?,?,?,?,?)",
                       (self.source, self.sender, "later", "", self.cid,
                        "native_contact_username", "{}"))
        self.assert_raw(self.resolve(), "invalid_retained_source_name_context")

    def test_noncanonical_old_anchor_is_rejected(self):
        self.c.execute("UPDATE sender_bindings SET authority='source_scoped_identifier' WHERE source_snapshot=?",
                       (self.old,))
        self.assert_raw(self.resolve(), "invalid_retained_source_name_context")

    def test_room_specific_name_applies_only_to_same_room(self):
        self.add_name("room", self.old, "remark", "One room retained name", room="one@chatroom")
        result = self.resolve(conversation_kind="group", peer_id="one@chatroom")
        self.assertEqual(result["preferred_name"], "One room retained name")
        self.assertEqual(result["preferred_name_context"]["original_room_id"], "one@chatroom")
        other = self.resolve(conversation_kind="group", peer_id="two@chatroom")
        self.assertEqual(other["preferred_name"], "Retained source remark")
        self.assertEqual(other["preferred_name_context"]["original_room_id"], "")

    def test_other_room_only_observation_cannot_leak(self):
        self.c.execute("DELETE FROM contact_observations")
        self.add_name("room", self.old, "remark", "Unrelated room", room="one@chatroom")
        self.assert_raw(self.resolve(conversation_kind="group", peer_id="two@chatroom"),
                        "retained_source_name_context_unavailable")

    def test_other_or_unknown_owner_observation_cannot_leak(self):
        self.c.execute("DELETE FROM contact_observations")
        self.add_name("other", self.old, "remark", "Other account", owner="other-owner")
        self.add_name("unknown", self.old, "remark", "Unknown owner", owner="")
        self.assert_raw(self.resolve(), "retained_source_name_context_unavailable")

    def test_same_priority_conflicting_names_are_held(self):
        self.add_name("conflict", self.old, "remark", "Conflicting source remark")
        self.assert_raw(self.resolve(), "conflicting_retained_source_name_context")

    def test_identical_name_observations_keep_stable_original_provenance(self):
        self.add_name("z-copy-evidence", self.old, "remark", "Retained source remark")
        result = self.resolve()
        self.assertEqual(result["preferred_name_context"]["observation_id"], "retained-remark")

    def test_duplicate_selected_aliases_are_rejected(self):
        self.c.execute("INSERT INTO sender_bindings VALUES(?,?,?,?,?,?,?)",
                       (self.source, self.sender, self.new, "one@chatroom", self.cid,
                        "native_contact_username", C._json(self.proof)))
        self.assert_raw(self.resolve(conversation_kind="group", peer_id="one@chatroom"),
                        "invalid_retained_source_name_context")

    def test_unmarked_native_binding_keeps_previous_resolver_behavior(self):
        self.c.execute("UPDATE sender_bindings SET evidence='{}' WHERE source_snapshot=?", (self.new,))
        result = self.resolve()
        self.assert_raw(result)
        self.assertNotIn("preferred_name_context", result)


    def alias_message(self):
        alias = "source_alias_fixture"
        self.c.execute("INSERT INTO sender_bindings VALUES(?,?,?,?,?,?,?)",
                       (self.source, alias, self.new, "", self.cid,
                        "corroborated_export_username", "{}"))
        return {**self.message, "id": "alias-message", "sender_id": alias, "sender_label": alias}

    def test_native_sender_first_cannot_lend_its_retained_proof_to_alias(self):
        alias = self.alias_message()
        result = self.repo._resolve(self.c, [self.message, alias])
        self.assertEqual(result[self.message["id"]]["preferred_name"], "Retained source remark")
        self.assertEqual(result[alias["id"]]["preferred_name"], alias["sender_id"])
        self.assertNotIn("preferred_name_context", result[alias["id"]])

    def test_alias_first_cannot_suppress_valid_native_sender_projection(self):
        alias = self.alias_message()
        result = self.repo._resolve(self.c, [alias, self.message])
        self.assertEqual(result[alias["id"]]["preferred_name"], alias["sender_id"])
        self.assertEqual(result[self.message["id"]]["preferred_name"], "Retained source remark")
        self.assertEqual(result[self.message["id"]]["preferred_name_context"]["observation_id"],
                         "retained-remark")

    def test_selected_message_proofs_have_independent_name_cache_identity(self):
        poisoned = {**self.message, "id": "additional-selected-proof"}
        proof = {**self.proof, "contact_id": self.cid}
        for requests in ([self.message, poisoned], [poisoned, self.message]):
            result = self.repo._resolve(
                self.c, requests, forwarded_proofs={poisoned["id"]: proof})
            self.assertEqual(result[self.message["id"]]["preferred_name"], "Retained source remark")
            self.assert_raw(result[poisoned["id"]], "invalid_retained_source_name_context")


if __name__ == "__main__":
    unittest.main(verbosity=2)
