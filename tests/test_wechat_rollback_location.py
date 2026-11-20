"""Keep shared-body recovery workstations outside live application storage.

Code version: v1.0.1-codex.0
"""

import pytest

from tests import test_wechat_shared_storage_publish as publication_fixtures
from app.core.wechat_shared_storage_publish import publish_shared_group_bodies
from app.core import wechat_shared_storage_publish as publisher


publication = publication_fixtures.publication
shared_archive = publication_fixtures.shared_archive


def test_publication_rejects_rollback_inside_live_storage(publication):
    backup = publication.history.parent / "storage_backups" / "new-rollback"
    with pytest.raises(ValueError, match="outside the live local_store"):
        publish_shared_group_bodies(
            publication.store,
            publication.conversations,
            backup_directory=backup,
        )
    assert not backup.exists()


def test_publication_rejects_external_alias_to_live_storage(publication, tmp_path):
    alias = tmp_path / "live-storage-alias"
    alias.symlink_to(publication.history.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="outside the live local_store"):
        publish_shared_group_bodies(
            publication.store,
            publication.conversations,
            backup_directory=alias / "new-rollback",
        )
    assert not (publication.history.parent / "new-rollback").exists()


def test_rollback_fence_uses_actual_directory_identity_for_case_aliases(publication):
    alias = publication.store.with_name(publication.store.name.upper())
    backup = alias / "new-rollback"
    if alias.exists() and alias.samefile(publication.store):
        original = publication.history.read_bytes()
        with pytest.raises(ValueError, match="outside the live local_store"):
            publish_shared_group_bodies(
                publication.store,
                publication.conversations,
                backup_directory=backup,
            )
        assert publication.history.read_bytes() == original
        assert not backup.exists()
    else:
        alias.mkdir()
        assert not publisher._inside_directory(backup, publication.store)
