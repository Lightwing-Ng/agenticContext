"""Behavioral checks for offline documentation navigation.

Code version: v1.1.0-codex.1
"""

from pathlib import Path

from scripts.check_docs import check_documents


def test_repository_documentation_links():
    """Every repository-local Markdown destination and heading must resolve."""
    errors, _, count = check_documents(Path(__file__).resolve().parents[1])
    assert count > 0
    assert not errors, "\n".join(errors)


def test_links_images_references_and_duplicate_headings(tmp_path):
    """Parse real Markdown instead of treating fenced examples as links."""
    (tmp_path / "README.md").write_text(
        "# Start\n[go][target]\n\n[target]: <Guide page.md#repeated-1>\n"
        "![icon](image.svg)\n[中文](Guide%20page.md#中文标题)\n"
        "```md\n[example](absent.md)\n```\n"
    )
    (tmp_path / "Guide page.md").write_text("# Repeated\n# Repeated\n# 中文标题\n")
    (tmp_path / "image.svg").write_text("<svg/>")
    assert check_documents(tmp_path) == ([], [], 2)


def test_missing_files_and_fragments_are_reported(tmp_path):
    (tmp_path / "README.md").write_text(
        "# Start\n[bad file](absent.md)\n[bad heading](#absent)\n![bad image](missing.png)\n"
    )
    errors, _, _ = check_documents(tmp_path)
    assert len(errors) == 3
    assert any("missing Markdown heading" in error for error in errors)


def test_external_and_shared_references_do_not_require_network_or_sibling(tmp_path):
    (tmp_path / "README.md").write_text(
        "[web](https://example.com/no-request)\n[shared](../SHARED.md)\n"
    )
    errors, shared, _ = check_documents(tmp_path)
    assert errors == []
    assert len(shared) == 1


def test_installed_shared_docs_are_validated(tmp_path):
    """Validate the canonical shared-doc tree when it is installed beside the project."""
    project = tmp_path / "project"
    project.mkdir()
    shared_docs = tmp_path / "shared_docs"
    shared_docs.mkdir()
    (shared_docs / "CONTRACT.md").write_text("# Contract\n")
    (project / "README.md").write_text(
        "[valid](../shared_docs/CONTRACT.md#contract)\n"
        "[missing](../shared_docs/MISSING.md)\n"
    )

    errors, shared, count = check_documents(project)

    assert count == 1
    assert len(shared) == 2
    assert len(errors) == 1
    assert "missing shared target" in errors[0]
