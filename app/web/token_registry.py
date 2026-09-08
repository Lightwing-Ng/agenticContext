"""Read the local CSS foundation token registry for the Style tokens page.

Code version: v0.9.0-codex.1
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import re


STYLE_CSS_PATH = Path(__file__).resolve().parent / "static" / "style.css"
_CSS_TOKEN_PATTERN = re.compile(
    r"(?P<name>--[A-Za-z0-9_-]+)\s*:\s*(?P<value>.*?);",
    re.DOTALL,
)
_CSS_REFERENCE_TEMPLATE = r"var\(\s*{name}(?:\s*[,)]|\s*$)"


@dataclass(frozen=True, slots=True)
class CssTokenDefinition:
    """A custom property declared in the stylesheet's root token block."""

    name: str
    value: str
    line: int
    reference_count: int
    category: str


def _extract_root_blocks(css_text: str) -> list[tuple[int, str]]:
    """Return every root block, including the compatibility layer overrides."""
    blocks: list[tuple[int, str]] = []
    for selector_match in re.finditer(r":root(?:[^\{]*)\{", css_text):
        block_start = selector_match.end() - 1
        depth = 0
        for index in range(block_start, len(css_text)):
            character = css_text[index]
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    blocks.append((block_start + 1, css_text[block_start + 1:index]))
                    break
        else:
            raise ValueError("Could not find the closing brace for a root token block.")

    if not blocks:
        raise ValueError("Could not find a :root selector in the stylesheet.")
    return blocks


def _token_category(token_name: str) -> str:
    """Return a stable display group without duplicating a token catalog."""
    name = token_name.removeprefix("--")
    if name.startswith(("font-",)):
        return "Typography"
    if name.startswith(("theme-", "color-", "accent-")):
        return "Color and status"
    if name.startswith(("glass-", "frosted-", "liquid-", "tooltip-", "panel")):
        return "Surfaces and effects"
    if name.startswith(("control-", "text-input-", "settings-", "sidebar-action-", "mode-switch", "strategy-")):
        return "Controls"
    if name.startswith(("radius", "spacing", "page-", "global-", "responsive-", "sidebar-", "app-shell", "workspace-", "viewport-", "layer-", "motion-")):
        return "Layout and motion"
    if name.startswith(("browser-", "cache-", "local-store-", "scrollable-", "ticker-")):
        return "Product components"
    return "Foundation"


@lru_cache(maxsize=4)
def _load_cached_token_registry(css_path_text: str) -> dict[str, CssTokenDefinition]:
    css_path = Path(css_path_text)
    css_text = css_path.read_text(encoding="utf-8")
    registry: dict[str, CssTokenDefinition] = {}

    for root_content_start, root_content in _extract_root_blocks(css_text):
        for match in _CSS_TOKEN_PATTERN.finditer(root_content):
            token_name = match.group("name")
            if token_name in registry:
                continue
            token_value = match.group("value").strip()
            reference_pattern = re.compile(
                _CSS_REFERENCE_TEMPLATE.format(name=re.escape(token_name)),
                re.MULTILINE,
            )
            reference_count = len(reference_pattern.findall(css_text))
            absolute_start = root_content_start + match.start()
            line_number = css_text.count("\n", 0, absolute_start) + 1
            registry[token_name] = CssTokenDefinition(
                name=token_name,
                value=token_value,
                line=line_number,
                reference_count=reference_count,
                category=_token_category(token_name),
            )
    return registry


def load_css_token_registry(css_path: Path | None = None) -> dict[str, CssTokenDefinition]:
    """Load root token definitions and their stylesheet reference counts."""
    target_path = (css_path or STYLE_CSS_PATH).resolve()
    return dict(_load_cached_token_registry(str(target_path)))


def build_reused_style_token_groups(
    *,
    minimum_references: int = 2,
) -> list[dict[str, object]]:
    """Build the grouped rows shown by Settings → Style tokens."""
    grouped: dict[str, list[CssTokenDefinition]] = defaultdict(list)
    for definition in load_css_token_registry().values():
        if definition.reference_count >= minimum_references:
            grouped[definition.category].append(definition)

    category_order = (
        "Foundation",
        "Color and status",
        "Typography",
        "Surfaces and effects",
        "Controls",
        "Layout and motion",
        "Product components",
    )
    groups: list[dict[str, object]] = []
    for category in category_order:
        definitions = sorted(grouped.get(category, []), key=lambda item: item.name)
        if not definitions:
            continue
        groups.append(
            {
                "name": category,
                "tokens": [
                    {
                        "name": definition.name,
                        "value": definition.value,
                        "line": definition.line,
                        "reference_count": definition.reference_count,
                    }
                    for definition in definitions
                ],
            }
        )
    return groups


def build_reused_style_token_rows(
    *,
    minimum_references: int = 2,
) -> list[dict[str, object]]:
    """Adapt the live registry to cards with working project-component demos."""
    rows: list[dict[str, object]] = []
    excluded_groups = {
        "Color and status",
        "Layout and motion",
        "Product components",
        "Surfaces and effects",
        "Typography",
    }
    for group in build_reused_style_token_groups(
        minimum_references=minimum_references,
    ):
        group_name = str(group["name"])
        if group_name in excluded_groups:
            continue
        tokens = list(group["tokens"])
        slug = re.sub(r"[^a-z0-9]+", "-", group_name.lower()).strip("-")
        demo = _build_style_token_demo(group_name=group_name)
        rows.append(
            {
                "id": f"style-token-{slug}",
                "name": group_name,
                **demo,
                "tokens": tokens,
            }
        )
    return rows


def build_style_token_component_rows() -> list[dict[str, object]]:
    """Return the explicit production-component catalog for Style tokens."""
    registry = load_css_token_registry()

    def token_rows(
        names: tuple[str, ...],
        material_names: tuple[str, ...] = (),
    ) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for name in names:
            definition = registry.get(name)
            if definition is None:
                continue
            row: dict[str, object] = {
                "name": definition.name,
                "value": definition.value,
                "line": definition.line,
                "reference_count": definition.reference_count,
                "editable": False,
            }
            numeric_match = re.fullmatch(r"(-?\d+)(px)?", definition.value)
            if numeric_match:
                numeric_value = int(numeric_match.group(1))
                row.update(
                    editable=True,
                    numeric_value=numeric_value,
                    unit=numeric_match.group(2) or "",
                    min_value=0 if numeric_value >= 0 else numeric_value,
                )
            if name in material_names:
                row.update(
                    value="Frosted glass",
                    editable=False,
                    reference_label="Frosted glass",
                    reference_target_id="frosted-glass",
                )
            rows.append(row)
        return sorted(rows, key=lambda row: str(row["name"]).casefold())

    period_options = (
        {"value": "1d", "label": "1 day"},
        {"value": "3d", "label": "3 days"},
        {"value": "1w", "label": "1 week"},
        {"value": "1mo", "label": "1 month"},
        {"value": "3mo", "label": "3 months"},
        {"value": "6mo", "label": "6 months"},
        {"value": "1y", "label": "1 year"},
        {"value": "2y", "label": "2 years"},
        {"value": "3y", "label": "3 years"},
        {"value": "5y", "label": "5 years"},
        {"value": "10y", "label": "10 years"},
        {"value": "max", "label": "Max"},
    )
    specs: tuple[dict[str, object], ...] = (
        {
            "id": "agent-browser-selector",
            "name": "Agent browser selector",
            "sample_kind": "agent-browser-selector",
            "sample_title": "Browser",
            "token_names": ("--control-form-height", "--control-liquid-background", "--control-liquid-background-hover", "--control-liquid-border", "--control-liquid-shadow", "--control-liquid-shadow-focus", "--control-liquid-blur", "--shared-select-dropdown-material", "--shared-select-option-radius", "--theme-success-strong", "--radius-pill", "--font-table-body", "--field-title-font-size", "--field-title-line-height", "--field-title-letter-spacing", "--field-title-font-weight", "--field-title-color"),
            "material_names": ("--shared-select-dropdown-material",),
        },
        {
            "id": "circular-icon-button",
            "name": "Circular icon button",
            "sample_kind": "round-icon-button",
            "token_names": ("--settings-round-icon-button-material", "--settings-round-icon-button-size", "--settings-round-icon-button-icon-size", "--settings-round-icon-button-radius", "--settings-round-icon-button-background", "--settings-round-icon-button-background-hover", "--settings-round-icon-button-shadow", "--settings-round-icon-button-shadow-hover", "--settings-round-icon-button-shadow-active", "--settings-round-icon-button-color", "--settings-round-icon-button-color-hover"),
            "material_names": ("--settings-round-icon-button-material",),
        },
        {
            "id": "frosted-glass",
            "name": "Frosted glass",
            "sample_kind": "glass-surface",
            "sample_title": "The quick brown fox jumps over the lazy dog.",
            "sample_copy": "Transparency and backdrop-filter are tested over a layered gradient.",
            "token_names": ("--frosted-glass-background", "--frosted-glass-border", "--frosted-glass-shadow", "--frosted-glass-blur"),
        },
        {
            "id": "global-theme-toggle",
            "name": "Global theme toggle",
            "sample_kind": "global-theme-toggle",
            "sample_title": "Appearance",
            "token_names": ("--settings-round-icon-button-size", "--settings-round-icon-button-icon-size", "--settings-round-icon-button-radius", "--settings-round-icon-button-background", "--settings-round-icon-button-background-hover", "--settings-round-icon-button-shadow", "--settings-round-icon-button-shadow-hover", "--settings-round-icon-button-color", "--settings-round-icon-button-color-hover", "--frosted-glass-blur"),
        },
        {
            "id": "modal-dialog",
            "name": "Modal dialog",
            "sample_kind": "modal-dialog",
            "sample_title": "Refreshing local cache",
            "sample_copy": "We are checking the local catalog for new items. Keep this page open while the refresh finishes.",
            "token_names": ("--workspace-modal-material", "--workspace-modal-radius", "--workspace-modal-pad-block", "--workspace-modal-pad-inline", "--workspace-modal-close-offset", "--workspace-modal-close-center-offset", "--workspace-modal-close-size", "--workspace-modal-icon-size", "--workspace-modal-column-gap", "--workspace-modal-row-gap"),
            "material_names": ("--workspace-modal-material",),
        },
        {
            "id": "modal-dialog-banner-message",
            "name": "Modal dialog banner message",
            "sample_kind": "floating-banner",
            "sample_title": "Cache settings updated",
            "sample_copy": "New browser sessions will use the saved settings.",
            "token_names": ("--notice-floating-material", "--workspace-modal-radius", "--workspace-modal-pad-block", "--workspace-modal-pad-inline", "--workspace-modal-close-offset", "--workspace-modal-close-center-offset", "--workspace-modal-close-size", "--workspace-modal-icon-size", "--workspace-modal-column-gap"),
            "material_names": ("--notice-floating-material",),
            "related_styles": ({"name": "Modal dialog", "target_id": "modal-dialog"},),
        },
        {
            "id": "pagination",
            "name": "Pagination",
            "sample_kind": "local-store-pagination",
            "sample_title": "Sessions",
            "token_names": ("--local-store-pagination-material", "--radius-pill", "--accent-fill", "--accent-shadow-strong", "--font-table-body", "--motion-duration-spatial", "--motion-bouncy"),
            "material_names": ("--local-store-pagination-material",),
        },
        {
            "id": "primary-button",
            "name": "Primary button",
            "sample_kind": "primary-button",
            "sample_title": "Start",
            "token_names": ("--primary-button-background", "--primary-button-background-disabled", "--primary-button-background-hover", "--primary-button-background-pending", "--primary-button-border", "--primary-button-color", "--primary-button-color-disabled", "--primary-button-font-weight", "--primary-button-min-height", "--primary-button-pad-block", "--primary-button-pad-inline", "--primary-button-radius"),
        },
        {
            "id": "prompt-tag",
            "name": "Tag",
            "sample_kind": "prompt-tag",
            "sample_title": "PS",
            "token_names": ("--accent-border-strong", "--accent-surface-soft", "--accent-text", "--radius-pill", "--font-ui-sm", "--font-weight-medium"),
        },
        {
            "id": "scrollable-data-table",
            "name": "Scrollable data table",
            "sample_kind": "scrollable-data-table",
            "sample_title": "Cache history",
            "token_names": ("--scrollable-data-table-header-material", "--scrollable-data-table-header-padding", "--scrollable-data-table-cell-padding", "--scrollable-data-table-summary-padding", "--scrollable-data-table-header-height", "--scrollable-data-table-min-width", "--scrollable-data-table-header-color", "--scrollable-data-table-scrollbar-gutter", "--scrollable-data-table-row-background", "--scrollable-data-table-row-background-alt", "--scrollable-data-table-summary-background", "--scrollable-data-table-summary-border", "--scrollable-data-table-summary-shadow", "--scrollable-data-table-summary-blur", "--field-title-font-size", "--field-title-line-height", "--field-title-letter-spacing", "--field-title-font-weight", "--field-title-color"),
            "material_names": ("--scrollable-data-table-header-material",),
        },
        {
            "id": "secondary-button",
            "name": "Secondary button",
            "sample_kind": "secondary-button",
            "sample_title": "Refresh cache",
            "use_icon": False,
            "token_names": ("--secondary-button-min-height", "--glass-chip-background-strong", "--glass-chip-background-hover", "--glass-chip-border", "--glass-chip-shadow", "--glass-chip-shadow-hover", "--radius-pill", "--font-size-3", "--font-weight-semibold"),
        },
        {
            "id": "segmented-control",
            "name": "Segmented control",
            "sample_kind": "range-mode",
            "token_names": ("--segmented-control-material", "--mode-switch-radius", "--mode-switch-pad", "--mode-switch-gap", "--mode-switch-min-height", "--mode-switch-thumb-inset", "--mode-switch-thumb-offset", "--mode-switch-label-pad-inline", "--mode-switch-label-min-height", "--mode-switch-thumb-background"),
            "material_names": ("--segmented-control-material",),
        },
        {
            "id": "settings-action-package",
            "name": "Settings action package",
            "sample_kind": "action-package",
            "sample_title": "Refresh local metadata",
            "sample_copy": "Refresh cached metadata and packaged browser assets without leaving Settings.",
            "token_names": ("--settings-action-package-material", "--settings-action-package-column-gap", "--settings-action-package-row-gap", "--settings-action-package-copy-gap", "--settings-action-package-background", "--settings-action-package-border", "--settings-action-package-live-marker-size", "--settings-action-package-live-marker-color", "--settings-action-package-live-marker-duration", "--style-token-demo-width"),
            "material_names": ("--settings-action-package-material",),
            "related_styles": ({"name": "Settings execution option", "target_id": "settings-execution-option"},),
        },
        {
            "id": "settings-execution-option",
            "name": "Settings execution option",
            "sample_kind": "settings-general-option",
            "sample_title": "Update existing cache entries",
            "sample_copy": "When enabled, refresh existing metadata as well as newly discovered items.",
            "token_names": ("--settings-general-option-background", "--settings-general-option-border", "--settings-general-option-max-width", "--settings-general-option-pad-block", "--settings-general-option-pad-inline", "--settings-general-option-radius"),
            "related_styles": ({"name": "Settings action package", "target_id": "settings-action-package"},),
        },
        {
            "id": "shared-select-dropdown",
            "name": "Shared select dropdown",
            "sample_kind": "shared-select-dropdown",
            "sample_title": "Period",
            "sample_copy": "The standard Period trigger exposes the full shared option range.",
            "sample_value": "1y",
            "sample_options": period_options,
            "token_names": ("--shared-select-dropdown-material",),
            "material_names": ("--shared-select-dropdown-material",),
            "related_styles": ({"name": "Shared select filter", "target_id": "shared-select-filter"},),
        },
        {
            "id": "shared-select-filter",
            "name": "Shared select filter",
            "sample_kind": "shared-select-filter",
            "sample_title": "Sort cached text",
            "token_names": ("--shared-select-control-height", "--shared-select-trigger-material", "--shared-select-dropdown-padding", "--shared-select-dropdown-radius", "--shared-select-dropdown-max-height", "--shared-select-option-min-height", "--shared-select-option-padding", "--shared-select-option-radius", "--shared-select-option-gap", "--control-liquid-background", "--control-liquid-background-hover", "--control-liquid-border"),
            "material_names": ("--shared-select-trigger-material", "--shared-select-dropdown-material"),
        },
        {
            "id": "strategy-tuning-control",
            "name": "Strategy tuning control",
            "sample_kind": "strategy-tuning-control",
            "sample_title": "Strategy",
            "sample_button": "Grid Trading",
            "sample_fields": (
                {"label": "Trigger price min", "value": "1.00"},
                {"label": "Trigger price max", "value": "1,000.00"},
                {"label": "Rise %", "value": "2.00"},
                {"label": "Fall %", "value": "1.00"},
            ),
            "token_names": ("--strategy-tune-button-material", "--strategy-tune-button-size", "--strategy-tune-button-icon-size", "--strategy-tune-button-radius", "--strategy-tune-panel-material", "--strategy-tune-panel-gap", "--strategy-tune-panel-padding", "--strategy-tune-panel-radius", "--strategy-tune-panel-row-gap", "--strategy-tune-panel-row-height", "--strategy-tune-panel-label-share", "--strategy-tune-panel-border", "--strategy-tune-panel-shadow", "--strategy-tune-panel-blur"),
            "material_names": ("--strategy-tune-button-material", "--strategy-tune-panel-material"),
            "related_styles": (
                {"name": "Circular icon button", "target_id": "circular-icon-button"},
                {"name": "Shared select filter", "target_id": "shared-select-filter"},
            ),
        },
        {
            "id": "switch",
            "name": "Switch",
            "sample_kind": "switch",
            "sample_title": "Update existing cache entries",
            "token_names": ("--switch-width", "--switch-height", "--switch-radius", "--switch-track-background", "--switch-track-background-checked", "--switch-track-shadow", "--switch-track-shadow-checked", "--switch-thumb-inset", "--switch-thumb-size", "--switch-thumb-radius", "--switch-thumb-background", "--switch-thumb-shadow", "--switch-thumb-offset"),
        },
        {
            "id": "text-input-control",
            "name": "Text input control",
            "sample_kind": "text-input-control",
            "sample_title": "Cache label",
            "sample_value": "Saved prompts",
            "token_names": ("--text-input-control-radius", "--text-input-control-pad-block", "--text-input-control-pad-inline", "--text-input-control-background", "--text-input-control-border", "--text-input-control-color", "--text-input-control-font-size", "--text-input-control-shadow", "--text-input-control-shadow-hover", "--field-title-font-size", "--field-title-line-height", "--field-title-letter-spacing", "--field-title-font-weight", "--field-title-color"),
        },
        {
            "id": "tooltip",
            "name": "Tooltip",
            "sample_kind": "chart-tooltip",
            "sample_title": "28/08/2026 10:08:00 (CST)",
            "token_names": ("--tooltip-background", "--tooltip-border", "--tooltip-shadow", "--tooltip-blur", "--chart-tooltip-min-width", "--chart-tooltip-max-width", "--chart-tooltip-padding", "--chart-tooltip-radius", "--chart-tooltip-row-gap", "--chart-tooltip-item-gap"),
            "material_names": ("--tooltip-background", "--tooltip-border", "--tooltip-shadow", "--tooltip-blur"),
        },
        {
            "id": "workspace-metric-value",
            "name": "Workspace metric value",
            "sample_kind": "metric-value",
            "sample_title": "Total trades",
            "sample_value": "2",
            "token_names": ("--workspace-metric-label-font-size", "--workspace-metric-label-line-height", "--workspace-metric-label-letter-spacing", "--workspace-metric-label-font-weight", "--workspace-metric-label-color", "--workspace-metric-value-font-size", "--workspace-metric-value-line-height", "--workspace-metric-value-letter-spacing", "--workspace-metric-value-font-weight", "--workspace-metric-decimal-scale", "--workspace-metric-card-padding", "--workspace-metric-card-row-gap", "--workspace-metric-card-radius", "--workspace-metric-card-label-min-height", "--workspace-metric-card-min-height"),
        },
    )

    rows: list[dict[str, object]] = []
    for spec in specs:
        token_names = tuple(str(name) for name in spec.get("token_names", ()))
        material_names = tuple(str(name) for name in spec.get("material_names", ()))
        row = {key: value for key, value in spec.items() if key not in {"token_names", "material_names"}}
        row.setdefault("sample_title", "")
        row.setdefault("sample_copy", "")
        row.setdefault("sample_value", "")
        row.setdefault("sample_options", ())
        row.setdefault("related_styles", ())
        row["tokens"] = token_rows(token_names, material_names)
        rows.append(row)
    return sorted(rows, key=lambda row: str(row["name"]).casefold())


def _build_style_token_demo(
    *,
    group_name: str,
) -> dict[str, object]:
    """Return an interactive, representative component for one token category."""
    if group_name == "Foundation":
        return {
            "sample_kind": "metric-summary",
        }
    if group_name == "Controls":
        return {
            "sample_kind": "range-mode",
        }
    raise ValueError(f"Unsupported Style tokens demo group: {group_name}")
