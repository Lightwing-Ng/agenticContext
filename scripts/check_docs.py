"""Validate local Markdown navigation without network access or sibling dependencies.

Code version: v1.1.0-codex.1
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt


def document_anchors(tokens) -> set[str]:
    """Build GitHub-style anchors for ordinary headings, including duplicate titles."""
    anchors: set[str] = set()
    for index, token in enumerate(tokens):
        if token.type != "heading_open":
            continue
        inline = tokens[index + 1]
        title = "".join(
            child.content for child in inline.children or []
            if child.type in {"text", "code_inline", "image"}
        )
        slug = re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
        anchor = slug
        suffix = 0
        while anchor in anchors:
            suffix += 1
            anchor = f"{slug}-{suffix}"
        anchors.add(anchor)
    return anchors


def check_documents(root: Path) -> tuple[list[str], list[str], int]:
    """Check repository links and report optional cross-repository references separately."""
    root = root.resolve()
    shared_root = (root.parent / "shared_docs").resolve()
    paths = sorted({*root.glob("*.md"), *(root / "docs").rglob("*.md")})
    parser = MarkdownIt()
    parsed = {path: parser.parse(path.read_text(encoding="utf-8")) for path in paths}
    errors: list[str] = []
    shared: set[str] = set()
    for source, tokens in parsed.items():
        for block in tokens:
            for token in block.children or []:
                attribute = {"link_open": "href", "image": "src"}.get(token.type)
                if attribute is None:
                    continue
                href = token.attrGet(attribute) or ""
                url = urlsplit(href)
                if url.scheme or url.netloc:
                    continue
                target = (source.parent / unquote(url.path)).resolve() if url.path else source
                location = f"{source.relative_to(root)}:{(block.map or [0])[0] + 1}"
                if not target.is_relative_to(root):
                    shared.add(f"{location}: {href}")
                    if shared_root.exists() and target.is_relative_to(shared_root):
                        if not target.exists():
                            errors.append(f"{location}: missing shared target {href}")
                        elif url.fragment and target.suffix.lower() == ".md":
                            shared_tokens = parser.parse(target.read_text(encoding="utf-8"))
                            if unquote(url.fragment) not in document_anchors(shared_tokens):
                                errors.append(f"{location}: missing shared Markdown heading {href}")
                    continue
                if not target.exists():
                    errors.append(f"{location}: missing local target {href}")
                    continue
                if url.fragment and target.suffix.lower() == ".md":
                    if target not in parsed:
                        parsed_tokens = parser.parse(target.read_text(encoding="utf-8"))
                    else:
                        parsed_tokens = parsed[target]
                    if unquote(url.fragment) not in document_anchors(parsed_tokens):
                        errors.append(f"{location}: missing Markdown heading {href}")
    return errors, sorted(shared), len(paths)


def main() -> int:
    """Expose the same check to local operators and both CI entrypoints."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    errors, shared, count = check_documents(args.root)
    for error in errors:
        print(error)
    for reference in shared:
        print(f"Shared reference (not checked): {reference}")
    print(f"Documentation: {count} files, {len(errors)} errors, {len(shared)} shared references.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
