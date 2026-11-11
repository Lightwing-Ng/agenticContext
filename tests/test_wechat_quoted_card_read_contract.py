"""Retained quoted-card labels never rewrite the surrounding native evidence.

Code version: v1.0.0-codex.0
"""

from copy import deepcopy

import pytest

from app.core.wechat_reconcile import (
    native_quote_card_metadata_extends,
    native_text_metadata,
    validate_quoted_app_card_metadata,
)
from tests.test_wechat_macos_bank_v4_reader import _messages


def _projections():
    old, new = _messages("quoted_card")
    return old["native_text"], new["native_text"]


@pytest.mark.parametrize(
    "defect",
    [
        None,
        "known_title",
        "actor",
        "server",
        "type",
        "body",
        "non_reference",
        "deep_tree",
        "node_budget",
        "numeric_type",
    ],
)
def test_only_missing_labels_may_extend_an_otherwise_exact_evidence_tree(defect):
    old, new = _projections()
    known = deepcopy(new["quoted_reference"]["app_card_metadata"])
    old["quoted_reference"]["app_card_metadata"] = known
    new["quoted_reference"]["app_card_metadata"].update(
        publisher_name="Literal publisher",
        publisher_name_source="appmsg/sourcedisplayname",
    )
    if defect == "known_title":
        new["quoted_reference"]["app_card_metadata"]["safe_title"] = "Replacement title"
    elif defect == "actor":
        new["quoted_reference"]["source_sender_id"] = "wxid_other_actor"
    elif defect == "server":
        new["quoted_reference"]["source_ids"]["svrid"] = "999"
    elif defect == "type":
        new["quoted_reference"]["original_message_type"] = 3
    elif defect == "body":
        new["text_components"][0]["text"] = "Changed reply"
    elif defect == "non_reference":
        new["app_card_metadata"] = known
    elif defect == "deep_tree":
        tree = {}
        for _ in range(42):
            tree = {"child": tree}
        old["retained_context"] = deepcopy(tree)
        new["retained_context"] = tree
    elif defect == "node_budget":
        old["retained_context"] = [0] * 20_001
        new["retained_context"] = [0] * 20_001
    elif defect == "numeric_type":
        new["quoted_reference"]["app_card_metadata"]["appmsg_subtype"] = 76.0
    before = deepcopy((old, new))
    assert native_quote_card_metadata_extends(old, new) is (defect is None)
    assert (old, new) == before


@pytest.mark.parametrize(
    "change",
    [
        {"unknown": "value"},
        {"schema_version": True},
        {"appmsg_subtype": 19},
        {"safe_title": " "},
        {"safe_title": "x" * 1025},
        {"safe_title": "bad\x00title"},
        {"publisher_name": "Missing source"},
        {"app_name": "Wrong path", "app_name_source": "appmsg/author"},
    ],
)
def test_quoted_card_schema_stays_closed_and_literal(change):
    card = {"schema_version": 1, "appmsg_subtype": 76, "safe_title": "Literal title"}
    card.update(change)
    with pytest.raises(ValueError):
        validate_quoted_app_card_metadata(card)


def test_quoted_labels_cannot_introduce_a_payload_body_or_non_app_reference():
    _old, new = _projections()
    assert native_text_metadata(new, {})["quoted_reference"]["app_card_metadata"]
    invalid = deepcopy(new)
    invalid["text_components"].append({"kind": "quoted_text", "text": "Payload body"})
    with pytest.raises(ValueError):
        native_text_metadata(invalid, {})
    invalid = deepcopy(new)
    invalid["quoted_reference"]["original_message_type"] = 3
    with pytest.raises(ValueError):
        native_text_metadata(invalid, {})
