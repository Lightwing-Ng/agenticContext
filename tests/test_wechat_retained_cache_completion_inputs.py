"""Keep accepted-reference reuse conditional on unchanged completion inputs.

Code version: v1.1.0-codex.0
"""

from contextlib import closing
import sqlite3

from app.core import wechat_contact_analysis as analysis
from app.core.wechat_secondary_completions import completion_path
from tests import test_wechat_statement_reviews_integration as fixtures
from tests import test_wechat_contact_statement_groups as grouping


overlap_store = fixtures.overlap_store
reviewed_store = fixtures.reviewed_store
retained_review_store = fixtures.retained_review_store
statement_store = grouping.statement_store


def test_new_completion_source_prevents_reusing_old_complete_author_membership(
    retained_review_store,
):
    fixture = retained_review_store
    sources_before = (fixture.history.read_bytes(), fixture.registry.read_bytes())
    with closing(sqlite3.connect(":memory:")) as output:
        output.executescript(analysis._REFERENCE_SCHEMA)
        assert analysis._copy_complete_event_index(
            fixture.cache, output, fixture.history, fixture.registry
        )
        assert output.execute("SELECT COUNT(*) FROM refs").fetchone()[0] > 0

    # Even an unexamined new sidecar invalidates the old accepted-author proof.
    completion = completion_path(fixture.store)
    with closing(sqlite3.connect(completion)) as connection:
        connection.execute("CREATE TABLE synthetic_source (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO synthetic_source VALUES ('new observation')")
        connection.commit()
    completion_before = completion.read_bytes()
    cache_before = fixture.cache.read_bytes()
    with closing(sqlite3.connect(":memory:")) as output:
        output.executescript(analysis._REFERENCE_SCHEMA)
        assert not analysis._copy_complete_event_index(
            fixture.cache, output, fixture.history, fixture.registry
        )
        assert output.execute("SELECT COUNT(*) FROM refs").fetchone() == (0,)
    assert completion.read_bytes() == completion_before
    assert fixture.cache.read_bytes() == cache_before
    assert (fixture.history.read_bytes(), fixture.registry.read_bytes()) == sources_before


def test_new_completion_source_prevents_reusing_old_compact_reference_cache(statement_store):
    fixture = statement_store
    grouping._legacy_cache(fixture)
    cache = analysis.contact_analysis_cache_path(fixture.store, grouping.CID)
    sources_before = (fixture.history.read_bytes(), fixture.registry.read_bytes())
    with closing(sqlite3.connect(":memory:")) as output:
        output.executescript(analysis._REFERENCE_SCHEMA)
        assert analysis._copy_legacy_references(
            cache, output, fixture.history, fixture.registry
        )
        assert output.execute("SELECT COUNT(*) FROM refs").fetchone()[0] > 0
    completion = completion_path(fixture.store)
    with closing(sqlite3.connect(completion)) as connection:
        connection.execute("CREATE TABLE synthetic_source (id TEXT PRIMARY KEY)")
        connection.commit()
    completion_before, cache_before = completion.read_bytes(), cache.read_bytes()
    with closing(sqlite3.connect(":memory:")) as output:
        output.executescript(analysis._REFERENCE_SCHEMA)
        assert not analysis._copy_legacy_references(
            cache, output, fixture.history, fixture.registry
        )
        assert output.execute("SELECT COUNT(*) FROM refs").fetchone() == (0,)
    assert completion.read_bytes() == completion_before
    assert cache.read_bytes() == cache_before
    assert (fixture.history.read_bytes(), fixture.registry.read_bytes()) == sources_before
