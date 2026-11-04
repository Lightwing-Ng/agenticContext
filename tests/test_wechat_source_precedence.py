"""Verify source-order evidence tolerates only independently verified clock ties.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import pytest

from app.core.wechat_source_precedence import monotone_pair_runs


def runs(rows):
    return monotone_pair_runs(
        rows,
        left_order=lambda row: row[0],
        right_order=lambda row: row[1],
        clock=lambda row: row[2],
    )


def test_same_second_reordering_retains_every_distinct_verified_pair():
    rows = [
        (1, 8, "2020-04-05T17:13:20"),
        (2, 10, "2020-04-05T17:16:52"),
        (3, 9, "2020-04-05T17:16:52"),
        (4, 11, "2020-04-05T17:16:54"),
    ]
    assert runs(reversed(rows)) == [rows]


def test_distinct_clock_inversion_partitions_independently_verified_runs():
    rows = [(number, number, str(number)) for number in range(1, 7)]
    rows[3] = (4, 10, "4")
    assert runs(rows) == [rows[:4], rows[4:]]


def test_clock_block_boundaries_use_the_full_range_not_only_the_last_row():
    rows = [(1, 8, "a"), (2, 10, "b"), (3, 9, "b"), (4, 7, "c")]
    assert runs(rows) == [rows[:3], rows[3:]]


@pytest.mark.parametrize(
    "rows,expected",
    [
        ([(1, 4, "a"), (1, 5, "a"), (2, 6, "b")], [[(2, 6, "b")]]),
        ([(1, 4, "a"), (2, 4, "a"), (3, 6, "b")], [[(3, 6, "b")]]),
        ([(1, 4, "a"), (1, 4, "a")], []),
    ],
)
def test_colliding_source_positions_never_choose_one_candidate(rows, expected):
    assert runs(rows) == expected


@pytest.mark.parametrize(
    "invalid",
    [
        (0, 1, "a"),
        (1, 0, "a"),
        (True, 1, "a"),
        (1, True, "a"),
        ("1", 1, "a"),
        (1, "1", "a"),
        (1, 1, None),
        (1, 1, ""),
        (1, 1, "a\0b"),
        (1, 1, float("nan")),
        (1, 1, float("inf")),
        (1, 1, 10**1000),
        (1, 1, True),
    ],
)
def test_incomplete_order_evidence_is_not_a_matching_anchor(invalid):
    assert runs([invalid]) == []


def test_epoch_clock_keys_support_verified_sql_projection_without_conversion():
    rows = [(1, 4, 1586078000.0), (2, 6, 1586078212.0), (3, 5, 1586078212.0)]
    assert runs(rows) == [rows]


def test_helper_does_not_mutate_or_copy_source_pair_objects():
    rows = [(1, 4, "a"), (2, 5, "b"), (3, 6, "c")]
    before = rows.copy()
    result = runs(rows)
    assert rows == before
    assert all(left is right for left, right in zip(result[0], rows, strict=True))
