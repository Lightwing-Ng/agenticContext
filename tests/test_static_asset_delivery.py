"""Verify executable MIME types for the modules used by Local resources.

Code version: v1.0.0-codex.1
"""

from pathlib import Path

import pytest

from app.web.app import create_app


@pytest.mark.parametrize("filename", ["browser-search.js", "vendor/fuse.min.mjs"])
def test_search_modules_are_served_with_javascript_mime(tmp_path, filename):
    application = create_app(
        tmp_path / "store",
        computer_use_settings_path=tmp_path / "settings.json",
        computer_use_runtime_root=tmp_path / "runtime",
        agent_external_operations_enabled=False,
    )
    response = application.test_client().get(f"/static/{filename}")
    assert response.status_code == 200
    assert response.data == (Path(application.static_folder) / filename).read_bytes()
    assert response.mimetype in {"text/javascript", "application/javascript"}, (
        f"Module {filename} cannot execute with Content-Type "
        f"{response.headers.get('Content-Type')!r}."
    )
