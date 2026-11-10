"""Contact grouping upgrades reject reference caches from older completion inputs.

Code version: v1.1.0-codex.0
"""

from contextlib import closing
import hashlib
import sqlite3

import pytest

from app.core import wechat_contact_analysis as analysis
from app.core.wechat_secondary_completions import completion_path
from tests import test_wechat_contact_analysis as fixtures


contact_analysis_store = fixtures.contact_analysis_store


def _reuse(fixture, cache):
    with sqlite3.connect(":memory:") as output:
        output.executescript(analysis._REFERENCE_SCHEMA)
        return analysis._copy_complete_event_index(
            cache, output, fixture.history, fixture.registry,
        )


@pytest.mark.parametrize(
    ("change", "reusable"),
    [("catalog_created", False), ("legacy_absent_catalog", True),
     ("legacy_without_present_completion_pin", False)],
)
def test_complete_reference_upgrade_reuses_only_current_completion_inputs(
    contact_analysis_store, change, reusable
):
    fixture = contact_analysis_store
    page = analysis.query_contact_analysis(fixture.store, fixtures.CID)
    assert page["status"] == "ready" and page["total_count"] > 0
    cache = analysis.contact_analysis_cache_path(fixture.store, fixtures.CID)
    assert _reuse(fixture, cache) is True
    if change in {"catalog_created", "legacy_without_present_completion_pin"}:
        # Cache acceptance uses the identity here; timeline readers validate its contents.
        with sqlite3.connect(completion_path(fixture.store)) as catalog:
            catalog.execute("CREATE TABLE archive_info(key TEXT PRIMARY KEY,value TEXT)")
    if change.startswith("legacy_"):
        identities = {
            str(path.resolve()): analysis._identity(path)
            for path in (fixture.history, fixture.registry)
        }
        old_pin = hashlib.sha256(analysis._json(identities).encode()).hexdigest()
        with sqlite3.connect(cache) as retained:
            retained.execute("UPDATE info SET value=? WHERE key='source_inputs'", (old_pin,))
    assert _reuse(fixture, cache) is reusable
    with closing(analysis._readonly(cache, derived_cache=True)) as retained:
        assert retained.execute("SELECT COUNT(*) FROM refs").fetchone()[0] > 0
