"""Verify optional completion catalog pins across equivalent store paths.

Code version: v1.0.0-codex.0
"""

from app.core import wechat_conversation_events as events
from tests.test_wechat_conversation_event_paths import _relative_repository
from tests.test_wechat_conversation_events import event_store as event_store


def test_resident_completion_pin_survives_relative_root_spelling(
    event_store, monkeypatch
):
    absolute = event_store.repository
    relative = _relative_repository(event_store, monkeypatch)
    before = events._inputs(relative)
    completion = absolute.path.with_name("secondary_message_completions.sqlite3")
    completion.write_bytes(b"Input metadata only; never open the payload.")

    relative_inputs = events._inputs(relative)
    absolute_inputs = events._inputs(absolute)
    assert relative_inputs == absolute_inputs
    assert relative_inputs.keys() - before.keys() == {str(completion)}
    fingerprint = events._fingerprint(relative_inputs)
    assert fingerprint == events._fingerprint(absolute_inputs)
    assert fingerprint != events._fingerprint(before)
