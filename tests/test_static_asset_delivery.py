"""Verify executable MIME types for the modules used by Local resources.

Code version: v1.1.0-codex.1
"""

from pathlib import Path
import mimetypes

import pytest

from app.web.app import create_app


def _isolated_application(tmp_path):
    return create_app(
        tmp_path / "store",
        computer_use_settings_path=tmp_path / "settings.json",
        computer_use_runtime_root=tmp_path / "runtime",
        agent_external_operations_enabled=False,
    )


@pytest.mark.parametrize("filename", ["browser-search.js", "vendor/fuse.min.mjs"])
def test_search_modules_are_served_with_javascript_mime(tmp_path, filename):
    application = _isolated_application(tmp_path)
    response = application.test_client().get(f"/static/{filename}")
    assert response.status_code == 200
    assert response.data == (Path(application.static_folder) / filename).read_bytes()
    assert response.mimetype in {"text/javascript", "application/javascript"}, (
        f"Module {filename} cannot execute with Content-Type "
        f"{response.headers.get('Content-Type')!r}."
    )


@pytest.mark.parametrize("filename", ["browser-search.js", "vendor/fuse.min.mjs"])
@pytest.mark.parametrize("host_mime", ["text/plain", "application/octet-stream"])
def test_module_delivery_overrides_host_mime_without_changing_global_types(
    tmp_path, monkeypatch, filename, host_mime
):
    mimetypes.guess_type(filename)
    extension = Path(filename).suffix
    monkeypatch.setitem(mimetypes.types_map, extension, host_mime)
    test_search_modules_are_served_with_javascript_mime(tmp_path, filename)
    assert mimetypes.guess_type(filename)[0] == host_mime


@pytest.mark.parametrize("filename", ["browser-search.js", "vendor/fuse.min.mjs"])
def test_javascript_headers_preserve_head_ranges_and_conditional_requests(tmp_path, filename):
    application = _isolated_application(tmp_path)
    client = application.test_client()
    url = f"/static/{filename}?v=mime-contract"
    response = client.get(url)
    expected = (Path(application.static_folder) / filename).read_bytes()
    assert response.data == expected
    assert response.mimetype == "text/javascript"
    etag = response.headers["ETag"]

    head = client.head(url)
    assert head.status_code == 200
    assert head.data == b""
    assert head.headers["Content-Length"] == str(len(expected))
    assert head.headers["ETag"] == etag
    assert head.mimetype == "text/javascript"

    partial = client.get(url, headers={"Range": "bytes=0-31"})
    assert partial.status_code == 206
    assert partial.data == expected[:32]
    assert partial.headers["Content-Range"] == f"bytes 0-31/{len(expected)}"
    assert partial.headers["Content-Length"] == "32"
    assert partial.headers["ETag"] == etag
    assert partial.mimetype == "text/javascript"

    cached = client.get(url, headers={"If-None-Match": etag})
    assert cached.status_code == 304
    assert cached.data == b""
    assert cached.headers["ETag"] == etag
    assert cached.headers["Cache-Control"] == response.headers["Cache-Control"]
    # Werkzeug removes entity headers from 304; cache upgrades use new graph URLs.
    assert "Content-Type" not in cached.headers

    invalid_range = client.get(url, headers={"Range": f"bytes={len(expected) + 1}-"})
    assert invalid_range.status_code == 416
    assert invalid_range.mimetype == "text/html"


def test_javascript_mime_policy_does_not_relabel_errors_or_other_routes(tmp_path):
    application = _isolated_application(tmp_path)

    @application.get("/dynamic.js")
    def plain_dynamic_response():
        return application.response_class("Plain dynamic content.", mimetype="text/plain")

    client = application.test_client()
    missing = client.get("/static/missing-script.mjs")
    assert missing.status_code == 404
    assert missing.mimetype == "text/html"
    dynamic = client.get("/dynamic.js")
    assert dynamic.status_code == 200
    assert dynamic.mimetype == "text/plain"
    assert dynamic.data == b"Plain dynamic content."
    for filename in ("browser-search.css", "images/favicon.svg"):
        response = client.get(f"/static/{filename}")
        assert response.status_code == 200
        assert response.mimetype == mimetypes.guess_type(filename)[0]
        assert response.data == (Path(application.static_folder) / filename).read_bytes()
