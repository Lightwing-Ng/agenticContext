"""Keep startup recovery on the catalog's existing canonical configured root.

Code version: v1.0.0-codex.0
"""

from pathlib import Path
from tempfile import TemporaryDirectory

from app.web.app import create_app


def test_startup_accepts_a_standard_temporary_directory_before_catalog_creation():
    """macOS exposes standard temporary paths through its system /var alias."""
    with TemporaryDirectory(prefix="agenticcontext-app-root-") as temporary:
        root = Path(temporary) / "local_store"
        application = create_app(root, agent_external_operations_enabled=False)
        try:
            assert application.extensions["local_media_catalog"].local_store_root == root.resolve()
            assert not (root / "messaging" / "wechat").exists()
        finally:
            application.extensions["runtime_shutdown"]()
