"""Keep shared-body recovery workstations outside live application storage.

Code version: v1.0.0-codex.0
"""

import pytest

from tests import test_wechat_shared_storage_publish as publication_fixtures
from app.core.wechat_shared_storage_publish import publish_shared_group_bodies


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
