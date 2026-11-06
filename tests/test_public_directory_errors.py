"""Directory validation retains useful errors without disclosing OS diagnostics.

Code version: v1.0.0-codex.0
"""

from pathlib import Path

import pytest

from app.web.presentation import validate_local_directory_path


@pytest.mark.parametrize("operation", ("resolve", "iterdir"))
def test_directory_inspection_errors_do_not_expose_private_diagnostics(
    tmp_path, monkeypatch, operation,
):
    original = getattr(Path, operation)

    def inspect(path, *args, **kwargs):
        if path == tmp_path:
            raise OSError("synthetic-private-path /private/secret; token=synthetic-secret")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, operation, inspect)
        result = validate_local_directory_path(str(tmp_path))

    assert result == (
        False,
        "The directory could not be inspected. Check its availability and permissions.",
        "",
    )
