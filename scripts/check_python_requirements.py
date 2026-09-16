"""Validate installed distributions against the project's declared constraints.

Code version: v1.0.1-codex.1
"""

from __future__ import annotations

import argparse
from importlib import metadata
from pathlib import Path
import sys

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name


RUNTIME_DISTRIBUTIONS = frozenset(
    canonicalize_name(name)
    for name in (
        "Flask",
        "playwright",
        "yt-dlp",
        "pyarrow",
        "Pillow",
        "markdown-it-py",
        "packaging",
        "tiktoken",
        "tzdata",
    )
)
MODE_DISTRIBUTIONS = {
    "runtime": RUNTIME_DISTRIBUTIONS,
    "test": RUNTIME_DISTRIBUTIONS | {canonicalize_name("pytest")},
    "quality": RUNTIME_DISTRIBUTIONS
    | {
        canonicalize_name("pip-audit"),
        canonicalize_name("pytest"),
        canonicalize_name("pytest-cov"),
        canonicalize_name("ruff"),
    },
}


def validate_installed_requirements(
    requirements_path: Path,
    mode: str,
) -> list[str]:
    """Return deterministic diagnostics for missing or incompatible distributions."""
    selected_distributions = MODE_DISTRIBUTIONS[mode]
    diagnostics: list[str] = []
    try:
        requirement_lines = requirements_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return [f"Unable to read {requirements_path}: {exc}"]

    for line_number, raw_line in enumerate(requirement_lines, start=1):
        requirement_text = raw_line.partition("#")[0].strip()
        if not requirement_text:
            continue
        try:
            requirement = Requirement(requirement_text)
        except InvalidRequirement as exc:
            diagnostics.append(
                f"Invalid requirement on line {line_number}: {exc}"
            )
            continue
        if canonicalize_name(requirement.name) not in selected_distributions:
            continue
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        try:
            installed_version = metadata.version(requirement.name)
        except metadata.PackageNotFoundError:
            diagnostics.append(f"{requirement.name} is not installed")
            continue
        if requirement.specifier and not requirement.specifier.contains(
            installed_version,
            prereleases=True,
        ):
            diagnostics.append(
                f"{requirement.name} {installed_version} does not satisfy "
                f"{requirement.specifier}"
            )
    return diagnostics


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse the resolver-facing mode and requirements path."""
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=tuple(MODE_DISTRIBUTIONS))
    parser.add_argument("requirements", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Print actionable version diagnostics and fail closed on any mismatch."""
    arguments = parse_args(sys.argv[1:] if argv is None else argv)
    diagnostics = validate_installed_requirements(
        arguments.requirements,
        arguments.mode,
    )
    if not diagnostics:
        return 0
    for diagnostic in diagnostics:
        print(diagnostic, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
