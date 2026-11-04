"""Partition verified source matches without imposing order inside one source second.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from collections import Counter
from itertools import groupby
import math


VERSION = "v1.0.0-codex.0"


def _valid_clock(value):
    if type(value) is str:
        return bool(value) and "\0" not in value
    try:
        return type(value) in {int, float} and math.isfinite(value)
    except OverflowError:
        return False


def monotone_pair_runs(rows, *, left_order, right_order, clock):
    """Return monotone runs of already verified, unique cross-source event pairs.

    Callers must first establish the account, conversation, author, exact body,
    source clock and native event identity. This helper does not establish any
    of those facts. Each returned run still needs its caller's minimum anchor
    count and author checks before it can authorize source preference.

    Distinct messages within one exact source second may have opposite local
    ordering in two devices. Their complete clock block therefore has no
    internal ordering requirement. Separate clock blocks must agree in strict
    source order; an inversion starts a new independently checked run instead
    of invalidating every otherwise proven match in a long conversation.
    Colliding source positions remain unresolved rather than choosing a row.
    """
    staged = []
    for row in rows:
        left, right, stamp = left_order(row), right_order(row), clock(row)
        if (
            type(left) is not int
            or left <= 0
            or type(right) is not int
            or right <= 0
            or not _valid_clock(stamp)
        ):
            continue
        staged.append((left, right, stamp, row))
    left_counts = Counter(item[0] for item in staged)
    right_counts = Counter(item[1] for item in staged)
    ordered = sorted(
        (item for item in staged if left_counts[item[0]] == right_counts[item[1]] == 1),
        key=lambda item: item[0],
    )
    runs, current, previous_maximum = [], [], None
    for _, matches in groupby(ordered, key=lambda item: item[2]):
        block = list(matches)
        minimum = min(item[1] for item in block)
        maximum = max(item[1] for item in block)
        if previous_maximum is not None and minimum <= previous_maximum:
            runs.append(current)
            current = []
        current.extend(item[3] for item in block)
        previous_maximum = maximum
    if current:
        runs.append(current)
    return runs
