"""Regression tests for synchronized sibling-project color tokens.

Code version: v1.54.1-codex.1
"""

import hashlib
from pathlib import Path
import struct

from scripts.build_web_fonts import FACE_NAMES, checksum, extract_face


FONT_PATH = Path(__file__).resolve().parents[1] / "app/web/static/fonts/UniversNextforHSBC.ttc"
STYLE_PATH = Path(__file__).resolve().parents[1] / "app/web/static/style.css"


def _stylesheet() -> str:
    return STYLE_PATH.read_text(encoding="utf-8")


def test_cache_metrics_reuse_the_foundation_surface_and_type_contract() -> None:
    """Keep Cache summary cards on the same Foundation surface as the live specimen."""
    stylesheet = _stylesheet()

    card_start = stylesheet.index(".foundation-metric-card.metric-card,")
    card_rule = stylesheet[card_start:stylesheet.index("\n}", card_start)]
    accent_start = stylesheet.index(".foundation-metric-card.metric-card-accent strong {")
    accent_rule = stylesheet[accent_start:stylesheet.index("\n}", accent_start)]
    regular_start = stylesheet.index(".foundation-metric-card:not(.metric-card-accent) strong {")
    regular_rule = stylesheet[regular_start:stylesheet.index("\n}", regular_start)]
    progress_label_start = stylesheet.index(".foundation-metric-card .progress-metric-label {")
    progress_label_rule = stylesheet[
        progress_label_start:stylesheet.index("\n}", progress_label_start)
    ]

    for token in (
        "padding: 8px 8px 6px;",
        "border: 0;",
        "border-radius: 0;",
        "background: transparent;",
        "box-shadow: none;",
        "backdrop-filter: none;",
        "-webkit-backdrop-filter: none;",
    ):
        assert token in card_rule
    assert "workspace-article-background" not in card_rule
    for token in (
        "font-size: var(--font-metric-lg);",
        "font-weight: var(--font-weight-regular);",
        "text-align: center;",
        "background: none;",
        "color: var(--accent-text);",
    ):
        assert token in accent_rule
    assert "background: none;" in regular_rule
    assert "color: var(--text);" in regular_rule
    assert "font-size: var(--font-ui-md);" in progress_label_rule
    assert "line-height: 1.1;" in progress_label_rule
    assert "font-weight: var(--font-weight-regular);" in progress_label_rule


def test_typography_matches_the_sibling_font_contract() -> None:
    """Keep the local font family, primitives, and semantic aliases in sync."""
    stylesheet = _stylesheet()

    expected_tokens = (
        '@font-face {',
        'font-family: "Univers Next for HSBC";',
        'src: url("/static/fonts/UniversNextforHSBC-Regular.ttf") format("truetype");',
        "--font-size-1: 11px;",
        "--font-size-2: 12px;",
        "--font-size-3: 13px;",
        "--font-size-4: 14px;",
        "--font-size-5: 15px;",
        "--font-size-6: 24px;",
        "--font-size-7: 32px;",
        "--font-size-8: 36px;",
        '--font-family-brand: "Univers Next for HSBC";',
        '--font-family-cjk: "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;',
        '--font-family-mono-cjk: ui-monospace, "SFMono-Regular", Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", monospace;',
        "--font-ui-md: var(--font-size-4);",
        "--font-ui-lg: var(--font-size-5);",
        "--font-form-label: var(--font-size-5);",
        "--font-title-md: var(--font-size-6);",
        "--font-metric-xl: var(--font-size-8);",
        "--font-table-head: var(--font-size-3);",
        "--font-card-subtitle: var(--font-ui-lg);",
        "--font-metric-value: var(--font-metric-md);",
        "--font-numeric-fraction-scale: 0.76;",
    )
    for token in expected_tokens:
        assert token in stylesheet
    assert 'format("truetype-collection")' not in stylesheet


def test_hsbc_font_collection_is_present_and_checksum_pinned() -> None:
    """Keep the self-hosted HSBC font asset available after cross-platform pulls."""
    assert FONT_PATH.is_file()
    assert hashlib.sha256(FONT_PATH.read_bytes()).hexdigest() == (
        "e10a317b9da0016c24a9fce70ccbd33eb39458da15253d5abfe051d8cc33e21a"
    )


def test_standalone_web_fonts_preserve_every_approved_face() -> None:
    """Verify deterministic table extraction and valid standalone font checksums."""
    source = FONT_PATH.read_bytes()
    for index, face in enumerate(FACE_NAMES):
        offset = struct.unpack_from(">I", source, 12 + index * 4)[0]
        path = FONT_PATH.with_name(f"UniversNextforHSBC-{face}.ttf")
        extracted = path.read_bytes()
        assert extracted == extract_face(source, offset)
        assert checksum(extracted) == 0xB1B0AFBA
        assert path.name in _stylesheet()


def test_routine_labels_use_restrained_font_weights() -> None:
    """Keep ordinary labels readable while reserving bold for explicit emphasis."""
    stylesheet = _stylesheet()

    expected_rules = {
        ".workspace-kicker,": "font-weight: var(--font-weight-semibold);",
        ".browser-session-panel-label {": "font-weight: var(--font-weight-regular);",
        ".field > span,": "font-weight: var(--font-weight-regular);",
        ".field > .field-help {": "font-weight: var(--font-weight-regular);",
        ".cache-common-config-title {": "font-weight: var(--font-weight-regular);",
        ".cache-number-label {": "font-weight: var(--font-weight-regular);",
        ".summary-row dt {": "font-weight: var(--font-weight-regular);",
        ".events-table thead th {": "font-weight: var(--font-weight-semibold);",
    }

    for selector, declaration in expected_rules.items():
        selector_start = stylesheet.index(selector)
        selector_rule = stylesheet[selector_start:stylesheet.index("\n}", selector_start)]
        assert declaration in selector_rule
        assert "font-weight: var(--font-weight-bold);" not in selector_rule

    for selector in (".field > span,", ".cache-number-label {"):
        selector_start = stylesheet.index(selector)
        selector_rule = stylesheet[selector_start:stylesheet.index("\n}", selector_start)]
        assert "font-size: var(--font-form-label);" in selector_rule


def test_settings_fields_use_a_single_column_layout() -> None:
    """Keep every Settings category free of side-by-side field groups."""
    stylesheet = _stylesheet()

    assert ".settings-category-panel .field-grid {" in stylesheet
    assert "grid-template-columns: minmax(0, 1fr);" in stylesheet


def test_settings_layout_dimensions_reuse_the_sibling_width_contract() -> None:
    """Keep Settings content and controls on the shared 640px and 384px tokens."""
    stylesheet = _stylesheet()

    for token in (
        "--layout-content-width: 640px;",
        "--layout-control-width: 384px;",
        "--layout-physical-effect-bleed: 48px;",
        "--settings-general-option-max-width: var(--layout-content-width);",
        "--settings-action-package-max-width: var(--layout-content-width);",
        "--settings-form-shell-max-width: var(--layout-content-width);",
        "--settings-form-control-max-width: var(--layout-control-width);",
        "--style-token-demo-width: var(--layout-control-width);",
    ):
        assert token in stylesheet

    for fragment in (
        "#settings_workspace .workspace-summary-card.workspace-article-card > .report-heading-row {",
        ".settings-category-shell {",
        "width: min(100%, var(--layout-content-width));",
        ".settings-category-panel .field-grid {",
        ".settings-category-panel .field {",
        "width: min(100%, var(--settings-form-control-max-width));",
        ".settings-category-panel > .shadow-backup-section,",
        ".settings-action-package,",
        ".cache-common-config {\n    overflow: visible;",
        ".settings-shell-style-tokens > .settings-summary-card {",
        ".settings-content-scrollport {",
        "margin-inline-start: calc(-1 * var(--layout-physical-effect-bleed));",
        "padding-inline-start: var(--layout-physical-effect-bleed);",
        "#settings_workspace .workspace-summary-card.workspace-article-card {\n    display: flex;",
    ):
        assert fragment in stylesheet


def test_settings_execution_option_reuses_the_sibling_form_contract() -> None:
    """Keep the execution-option specimen aligned with the sibling Settings form."""
    stylesheet = _stylesheet()

    expected_fragments = (
        "--settings-general-option-radius: var(--radius-soft);",
        "--settings-general-option-pad-block: 14px;",
        "--settings-general-option-pad-inline: 16px;",
        "--settings-general-option-background: var(--glass-surface-background-strong);",
        "--settings-general-option-border: 0.5px solid color-mix(in srgb, var(--theme-text) 8%, transparent);",
        ".settings-general-form {",
        "display: grid;",
        "gap: 12px;",
        "width: min(100%, var(--settings-general-option-max-width));",
        ".settings-general-option {",
        "grid-template-columns: auto 1fr;",
        "padding: var(--settings-general-option-pad-block) var(--settings-general-option-pad-inline);",
        "transition: background-color 180ms var(--motion-standard), border-color 180ms var(--motion-standard), box-shadow 180ms var(--motion-standard);",
        ".settings-general-option input {",
        "accent-color: var(--theme-accent-primary);",
        ".settings-general-option:has(input:checked) {",
        "box-shadow: inset 0 0 0 1px var(--theme-accent-primary);",
        ".settings-general-option-title {",
        "font-size: var(--font-form-control);",
        ".settings-general-option-desc {",
        "font-weight: var(--font-weight-regular);",
        '.style-token-demo[data-style-token-density="tight"] .settings-general-option,',
    )
    for fragment in expected_fragments:
        assert fragment in stylesheet

    for removed_token in (
        "--settings-general-option-gap",
        "--settings-general-option-padding",
    ):
        assert removed_token not in stylesheet


def test_chart_tooltip_title_uses_the_medium_weight_token() -> None:
    """Keep the Style tokens tooltip title on the shared 500 weight."""
    stylesheet = _stylesheet()
    selector_start = stylesheet.index(".chart-tooltip-title {")
    selector_rule = stylesheet[selector_start:stylesheet.index("\n}", selector_start)]

    assert "font-weight: var(--font-weight-medium);" in selector_rule
    assert "font-weight: var(--font-weight-semibold);" not in selector_rule


def test_browser_filter_labels_use_the_medium_weight_token() -> None:
    """Give Local resources filter labels the shared medium emphasis."""
    stylesheet = _stylesheet()
    selector_start = stylesheet.index(".browser-filter-field > span {")
    selector_rule = stylesheet[selector_start:stylesheet.index("\n}", selector_start)]

    assert "font-weight: var(--font-weight-medium);" in selector_rule


def test_form_inputs_use_regular_weight_monospace_text() -> None:
    """Keep path and numeric values legible without an inherited bold weight."""
    stylesheet = _stylesheet()
    selector_start = stylesheet.index('input[type="text"],\ninput[type="number"] {')
    selector_rule = stylesheet[selector_start:stylesheet.index("\n}", selector_start)]

    assert 'font-family: "SFMono-Regular", "SF Mono", Menlo, monospace;' in selector_rule
    assert "font-weight: var(--font-weight-regular);" in selector_rule


def test_formatted_number_inputs_use_the_annotated_light_weight() -> None:
    """Apply the lighter numeric treatment through the reusable number-field owner."""
    stylesheet = _stylesheet()
    selector_start = stylesheet.index("input.formatted-number-input {")
    selector_rule = stylesheet[selector_start:stylesheet.index("\n}", selector_start)]

    assert "font-weight: 300;" in selector_rule


def test_danger_zone_actions_align_to_the_card_end() -> None:
    """Keep every destructive Settings action aligned to its card edge."""
    stylesheet = _stylesheet()
    selector_start = stylesheet.index(".danger-zone form {")
    selector_rule = stylesheet[selector_start:stylesheet.index("\n}", selector_start)]

    assert "display: flex;" in selector_rule
    assert "justify-content: flex-end;" in selector_rule


def test_cache_sidebar_cards_and_url_inputs_use_the_shared_control_treatment() -> None:
    """Keep shared configuration tactile and URL values legible in monospace."""
    stylesheet = _stylesheet()

    cache_card_start = stylesheet.index(".cache-common-config--physical {")
    cache_card_rule = stylesheet[cache_card_start:stylesheet.index("\n}", cache_card_start)]
    url_input_start = stylesheet.index('input[type="url"].text-input-control {')
    url_input_rule = stylesheet[url_input_start:stylesheet.index("\n}", url_input_start)]
    action_row_start = stylesheet.index('.page[data-cache-source="grok"] .sidebar-action-row {')
    action_row_rule = stylesheet[action_row_start:stylesheet.index("\n}", action_row_start)]
    action_slot_start = stylesheet.index(".cache-action-row {")
    action_slot_rule = stylesheet[action_slot_start:stylesheet.index("\n}", action_slot_start)]
    action_slot_buttons_start = stylesheet.index(".cache-action-row .sidebar-form-stop,")
    action_slot_buttons_rule = stylesheet[
        action_slot_buttons_start:stylesheet.index("\n}", action_slot_buttons_start)
    ]

    for token in (
        "border: var(--frosted-glass-border);",
        "background: var(--frosted-glass-background);",
        "box-shadow: var(--frosted-glass-shadow),",
        "backdrop-filter: var(--frosted-glass-blur);",
    ):
        assert token in cache_card_rule

    assert 'font-family: "SFMono-Regular", "SF Mono", Menlo, monospace;' in url_input_rule
    assert "font-weight: var(--font-weight-regular);" in url_input_rule
    assert "padding-block: 10px;" in action_row_rule
    assert "grid-template-columns: minmax(0, 1fr);" in action_slot_rule
    assert "grid-column: 1;" in action_slot_buttons_rule
    assert "justify-self: end;" in action_slot_buttons_rule


def test_settings_directory_picker_uses_the_folder_icon() -> None:
    """Keep directory picker buttons compact and backed by the local folder asset."""
    stylesheet = _stylesheet()

    assert ".settings-directory-choose-icon {" in stylesheet
    assert "width: var(--settings-round-icon-button-size);" in stylesheet
    assert "height: var(--settings-round-icon-button-icon-size);" in stylesheet
    assert 'mask: url("/static/images/folder.fill.svg") center/contain no-repeat;' in stylesheet


def test_cache_status_message_hangs_under_the_account_label() -> None:
    """Keep wrapped readiness copy aligned after the leading status icon."""
    stylesheet = _stylesheet()
    selector_start = stylesheet.index('.browser-session-status-message[data-role="browser-session-message"] {')
    selector_rule = stylesheet[selector_start:stylesheet.index("\n}", selector_start)]

    assert "--browser-session-status-indent: calc(" in selector_rule
    assert "var(--browser-session-status-checkmark-size)" in selector_rule
    assert "var(--browser-session-status-item-gap)" in selector_rule
    assert "margin: 0;" in selector_rule
    assert "padding-inline-start: var(--browser-session-status-indent);" in selector_rule
    assert "text-indent: 0;" in selector_rule

    copy_start = stylesheet.index(".browser-session-status-copy {")
    copy_rule = stylesheet[copy_start:stylesheet.index("\n}", copy_start)]
    assert "--browser-session-status-checkmark-size: 18px;" in copy_rule
    assert "--browser-session-status-item-gap: 8px;" in copy_rule

    item_start = stylesheet.index(".browser-session-status-item {")
    item_rule = stylesheet[item_start:stylesheet.index("\n}", item_start)]
    assert "align-items: center;" in item_rule
    assert "gap: var(--browser-session-status-item-gap);" in item_rule

    checkmark_start = stylesheet.index(
        ".browser-session-status-item .browser-session-status-checkmark {"
    )
    checkmark_rule = stylesheet[checkmark_start:stylesheet.index("\n}", checkmark_start)]
    assert "width: var(--browser-session-status-checkmark-size);" in checkmark_rule
    assert "height: var(--browser-session-status-checkmark-size);" in checkmark_rule


def test_cache_sidebar_parameter_grids_use_one_field_per_row() -> None:
    """Prevent cache sidebar parameter fields from sharing a horizontal row."""
    stylesheet = _stylesheet()
    selector_start = stylesheet.index(".sidebar-section .field-grid {")
    selector_rule = stylesheet[selector_start:stylesheet.index("\n}", selector_start)]

    assert "grid-template-columns: minmax(0, 1fr);" in selector_rule


def test_browser_grid_cards_stretch_to_the_row_height() -> None:
    """Keep media cards bottom-aligned when their content heights differ."""
    stylesheet = _stylesheet()
    selector_start = stylesheet.index(".browser-gallery {")
    selector_rule = stylesheet[selector_start:stylesheet.index("\n}", selector_start)]

    assert "align-items: stretch;" in selector_rule


def test_settings_action_packages_reuse_the_sibling_composite_card() -> None:
    """Keep Settings action surfaces on the sibling composite-card pattern."""
    stylesheet = _stylesheet()

    expected_fragments = (
        ".settings-action-package {",
        "grid-template-columns: 36px minmax(0, 1fr);",
        "--settings-action-package-row-gap: 8px;",
        "--settings-action-package-max-width: var(--layout-content-width);",
        "background: var(--settings-action-package-background);",
        "border: var(--settings-action-package-border);",
        "box-shadow: var(--frosted-glass-shadow);",
        "backdrop-filter: var(--frosted-glass-blur);",
        ".settings-action-package-icon-shell {",
        ".settings-action-package-copy {",
        "display: contents;",
        ".settings-action-package-live-marker {",
        "@keyframes settings-action-package-live-breath",
        ".settings-action-package-form {",
        "justify-self: end;",
        ".settings-action-package:has(.settings-service-name) {",
        ".settings-agent-terminal-authorization-status {",
        ".settings-agent-terminal-authorization-status[hidden] {",
        ".settings-inline-button-primary {",
        ".style-token-action-package-live-control {",
        ".style-token-action-package-live-label {",
    )
    for fragment in expected_fragments:
        assert fragment in stylesheet

    inline_button_start = stylesheet.index(".settings-inline-button-primary {")
    inline_button_rule = stylesheet[
        inline_button_start:stylesheet.index("\n}", inline_button_start)
    ]
    for fragment in (
        "min-height: var(--primary-button-min-height);",
        "padding: var(--primary-button-pad-block) var(--primary-button-pad-inline);",
        "border: var(--primary-button-border);",
        "background: var(--primary-button-background);",
        "color: var(--primary-button-color);",
        "font-weight: var(--primary-button-font-weight);",
    ):
        assert fragment in inline_button_rule


def test_style_token_resizer_reuses_the_sibling_surface_resizer_contract() -> None:
    """Keep the Style tokens split control glassy, keyboard-reachable, and bounded."""
    stylesheet = _stylesheet()

    expected_fragments = (
        ".surface-resizer {",
        "touch-action: none;",
        ".surface-resizer--inline {",
        "cursor: col-resize;",
        ".surface-resizer--inline::after {",
        "width: var(--surface-resizer-handle-short);",
        "height: var(--surface-resizer-handle-long);",
        ".settings-shell-style-tokens > .style-token-shell {",
        "display: flex;",
        ".settings-shell-style-tokens > .style-token-shell > .style-token-list {",
        "overflow: auto;",
    )
    for fragment in expected_fragments:
        assert fragment in stylesheet


def test_light_color_tokens_match_the_sibling_theme() -> None:
    """Keep the light palette aligned with antigravity/config.toml."""
    stylesheet = _stylesheet()

    expected_tokens = (
        "--theme-background: #ffffff;",
        "--theme-panel: rgba(255, 255, 255, 0.68);",
        "--theme-panel-strong: rgba(255, 255, 255, 0.82);",
        "--theme-text: #0b0c0c;",
        "--theme-muted: #505a5f;",
        "--theme-accent-primary: #0055cc;",
        "--theme-accent-secondary: #ff2f92;",
        "--theme-accent-positive: #16a34a;",
        "--theme-error: #c81e1e;",
        "--theme-error-strong: #991b1b;",
        "--theme-warning: #f4c542;",
        "--theme-warning-text: #6b5200;",
        "--theme-success: #16a34a;",
    )

    for token in expected_tokens:
        assert token in stylesheet

def test_dark_color_tokens_match_the_sibling_theme() -> None:
    """Keep the dark palette aligned with antigravity/config.toml."""
    stylesheet = _stylesheet()

    expected_tokens = (
        "--theme-background: #0b0c0c;",
        "--theme-panel: rgba(255, 255, 255, 0.04);",
        "--theme-panel-strong: rgba(255, 255, 255, 0.08);",
        "--theme-text: #f3f4f6;",
        "--theme-muted: #94a3b8;",
        "--theme-accent-primary: #0055cc;",
        "--theme-accent-secondary: #f472b6;",
        "--theme-accent-positive: #2fff9c;",
        "--theme-error: #ef4444;",
        "--theme-error-strong: #b91c1c;",
        "--theme-warning: #f4c542;",
        "--theme-warning-text: #f4c542;",
        "--theme-success: #2fff9c;",
    )

    for token in expected_tokens:
        assert token in stylesheet


def test_theme_toggle_and_session_view_use_sibling_pressed_controls() -> None:
    """Keep global appearance and session mode controls on the sibling interaction pattern."""
    stylesheet = _stylesheet()

    expected_fragments = (
        '.global-theme-toggle[data-effective-theme="light"] .icon {',
        'mask-image: url("/static/images/moon.fill.svg");',
        '.global-theme-toggle[data-effective-theme="dark"] .icon {',
        'mask-image: url("/static/images/sun.max.fill.svg");',
        '.browser-session-control-button[aria-pressed="true"] {',
        "transform: translateY(1px) scale(0.985);",
        ':root:not([data-theme-override="light"]) {',
        ':root[data-theme-override="dark"] {',
    )
    for fragment in expected_fragments:
        assert fragment in stylesheet


def test_synchronized_color_aliases_cover_interactive_surfaces() -> None:
    """Protect aliases that let components consume the shared palette."""
    stylesheet = _stylesheet()

    expected_tokens = (
        "--accent-focus-ring: color-mix(in srgb, var(--accent) 16%, transparent);",
        "--accent-focus-glow: color-mix(in srgb, var(--accent) 18%, transparent);",
        "--color-success: var(--theme-success);",
        "--color-error: var(--theme-error);",
        "--color-warning: var(--theme-warning);",
        "--glass-mask-background: var(--glass-surface-background-strong);",
    )

    for token in expected_tokens:
        assert token in stylesheet


def test_non_pill_corner_radii_use_the_shared_ten_pixel_value() -> None:
    """Keep ordinary rounded surfaces at 10px while preserving pill and circle shapes."""
    stylesheet = _stylesheet()

    expected_tokens = (
        "--radius-panel: 10px;",
        "--radius-soft: var(--radius-panel);",
        "--strategy-stepper-radius: var(--radius-soft);",
        "--workspace-modal-radius: var(--radius-panel);",
        "--browser-media-frame-radius: var(--radius-panel);",
        "border-radius: var(--browser-media-frame-radius, var(--radius-panel));",
        "border-radius: 0 0 var(--radius-panel) var(--radius-panel);",
        "border-radius: var(--radius-panel) var(--radius-panel) 0 0;",
        "border-radius: var(--radius-panel) 0 0 var(--radius-panel);",
    )
    for token in expected_tokens:
        assert token in stylesheet

    assert "border-radius: 10px;" not in stylesheet
    assert "--radius-soft: 10px;" not in stylesheet

    for non_shared_radius in ("6px", "8px", "9px", "12px", "18px", "20px", "24px", "30px"):
        assert f"border-radius: {non_shared_radius};" not in stylesheet


def test_sidebar_actions_consume_shared_semantic_tokens() -> None:
    """Keep the cache controls on the sibling project's tokenized button system."""
    stylesheet = _stylesheet()

    expected_tokens = (
        "--sidebar-action-primary-background: var(--theme-accent-primary);",
        "--sidebar-action-primary-background-disabled: color-mix(in srgb, var(--theme-muted) 28%, transparent);",
        "--sidebar-action-danger-background: var(--theme-error-translucent);",
        "--sidebar-action-danger-background-hover: var(--theme-error);",
        "--sidebar-action-danger-color: var(--theme-error-strong);",
        "background: var(--sidebar-action-primary-background);",
        "background: var(--sidebar-action-danger-background);",
        "box-shadow: 0 0 0 3px var(--accent-focus-ring);",
    )

    for token in expected_tokens:
        assert token in stylesheet


def test_cache_stop_button_is_borderless_in_every_interactive_state() -> None:
    """Keep the Cache stop action on a borderless danger-button treatment."""
    stylesheet = _stylesheet()
    stop_start = stylesheet.index(".stop-button {")
    stop_rule = stylesheet[stop_start:stylesheet.index("\n}", stop_start)]
    stop_hover_start = stylesheet.index(".stop-button:hover,")
    stop_hover_rule = stylesheet[
        stop_hover_start:stylesheet.index("\n}", stop_hover_start)
    ]

    assert "border-width: 0;" in stop_rule
    assert "border-color: transparent;" in stop_rule
    assert "border-color: transparent;" in stop_hover_rule


def test_sidebar_shell_and_dock_consume_frosted_glass_tokens() -> None:
    """Keep the sidebar shell and dock on the sibling frosted-glass surface system."""
    stylesheet = _stylesheet()

    expected_tokens = (
        ".sidebar {",
        "background: var(--frosted-glass-background);",
        "border: var(--frosted-glass-border);",
        "box-shadow: var(--frosted-glass-shadow);",
        "backdrop-filter: var(--frosted-glass-blur);",
        ".sidebar-dock {",
        ".sidebar-dock-item.is-active {",
        "color: var(--accent-text);",
        ".sidebar-toggle {",
        "border: var(--settings-round-icon-button-border);",
        "[hidden] {",
        "display: none !important;",
        ".sidebar-section > .section-heading {",
        "position: relative;",
        "min-height: var(--settings-round-icon-button-size);",
        "padding-right: 0;",
        ".section-heading > .cache-source-switcher-combobox {",
        "flex: 1 1 100%;",
        "width: 100%;",
        ".sidebar-section > .section-heading > .cache-phase-live-marker {",
        "right: 38px;",
        "pointer-events: none;",
    )

    for token in expected_tokens:
        assert token in stylesheet


def test_sidebar_titles_reuse_sibling_hero_tokens() -> None:
    """Keep sidebar titles aligned with the sibling project's shared hero rail."""
    stylesheet = _stylesheet()

    expected_tokens = (
        "--font-card-title: var(--font-title-md);",
        "--workspace-title-rail-control-height: var(--settings-round-icon-button-size);",
        "--workspace-title-rail-collapsed-pad-inline-start: calc(var(--settings-round-icon-button-size) + 22px);",
        "font-size: var(--font-card-title);",
        "line-height: 1.18;",
        "letter-spacing: 0;",
        ".sidebar .hero {",
        "margin-bottom: 24px;",
        "min-height: var(--workspace-title-rail-control-height);",
    )

    for token in expected_tokens:
        assert token in stylesheet

    assert ".hero::after" not in stylesheet


def test_cache_source_heading_uses_the_shared_picker_and_live_marker() -> None:
    """Keep the cache heading aligned with the sibling picker and breathing marker."""
    stylesheet = _stylesheet()

    expected_tokens = (
        "--cache-phase-live-marker-size: 6px;",
        "--cache-phase-live-marker-color: var(--theme-accent-positive);",
        "--cache-phase-live-marker-duration: 1.8s;",
        ".section-heading > .cache-source-switcher-combobox {",
        "flex: 1 1 auto;",
        ".cache-source-switcher-combobox.is-cache-source-menu-open .cache-source-switcher-dropdown {",
        ".cache-phase-live-marker {",
        "0 0 0 4px color-mix(in srgb, var(--cache-phase-live-marker-color) 18%, transparent),",
        ".cache-phase-live-marker::before,",
        "animation: cachePhaseLiveBreath var(--cache-phase-live-marker-duration) var(--motion-emphasized) infinite;",
        "animation-delay: 0.9s;",
        "@keyframes cachePhaseLiveBreath {",
    )

    for token in expected_tokens:
        assert token in stylesheet


def test_events_table_consumes_shared_scrollable_table_tokens() -> None:
    """Protect the log stream table against drifting away from sibling table tokens."""
    stylesheet = _stylesheet()

    expected_tokens = (
        ".events-table-shell {",
        "background: var(--frosted-glass-background);",
        "border: var(--frosted-glass-border);",
        "box-shadow: var(--frosted-glass-shadow);",
        "backdrop-filter: var(--frosted-glass-blur);",
        "padding: var(--scrollable-data-table-header-padding);",
        "padding: var(--scrollable-data-table-cell-padding);",
        "color: var(--scrollable-data-table-header-color);",
        "background: var(--scrollable-data-table-row-background);",
        "background: var(--scrollable-data-table-row-background-alt);",
        ".browser-pagination.events-pagination {",
        "bottom: var(--events-pagination-edge-inset);",
        ".browser-pagination.events-pagination .local-store-page-button {",
    )

    for token in expected_tokens:
        assert token in stylesheet

    assert ".events-page-button" not in stylesheet
    assert ".events-page-indicator" not in stylesheet


def test_cache_dock_direct_link_and_gallery_use_shared_tokens() -> None:
    """Protect the direct cache dock link and local media browser compatibility layer."""
    stylesheet = _stylesheet()

    expected_tokens = (
        ".sidebar-dock:has(> .sidebar-dock-item:nth-child(1).is-active)",
        ".sidebar-dock:has(> .sidebar-dock-item:nth-child(4).is-active)",
        ".cache-source-mark::before",
        "mask-image: var(--cache-source-mark);",
        "background-color: currentColor;",
        ".cache-source-mark.is-full-color::before",
        "background: var(--cache-source-mark) center / contain no-repeat;",
        "mask-image: none;",
        ".dock-icon-chats",
        "mask: url(\"/static/images/arrow.down.message.fill.svg\")",
        ".dock-icon-cache",
        "mask: url(\"/static/images/photo.badge.arrow.down.fill.svg\")",
        ".dock-icon-local-resources",
        "mask: url(\"/static/images/photo.stack.svg\")",
        ".browser-empty-icon::before",
        ".browser-gallery",
        "grid-template-columns: repeat(4, minmax(0, 1fr));",
        ".browser-media-remove[hidden]",
        ".browser-dialog::backdrop",
        "--browser-media-viewer-control-gap: clamp(12px, 1.5vw, 18px);",
        ".browser-video-nav-button:first-child",
        ".browser-video-nav-button:last-child",
        "top: calc(var(--browser-media-frame-radius) - (var(--browser-media-viewer-control-size) / 2));",
        "background: var(--frosted-glass-background);",
        "border: var(--frosted-glass-border);",
        "box-shadow: var(--frosted-glass-shadow);",
        "backdrop-filter: var(--frosted-glass-blur);",
        "@media (prefers-reduced-motion: reduce)",
    )

    for token in expected_tokens:
        assert token in stylesheet

    for removed_menu_selector in (
        ".sidebar-dock-cache-menu",
        ".sidebar-dock-cache-trigger",
        ".sidebar-dock-cache-dropdown",
        ".sidebar-dock-cache-option",
    ):
        assert removed_menu_selector not in stylesheet


def test_waiting_feedback_uses_the_sibling_vector_spinner_and_modal() -> None:
    """Keep waiting states informative and aligned with the sibling workspace dialog."""
    stylesheet = _stylesheet()

    expected_tokens = (
        ".suggestion-loading-spinner {",
        ".suggestion-loading-spinner[hidden] {",
        "display: none;",
        'mask: url("/static/images/loading.spinner.svg") center/contain no-repeat;',
        "animation: ticker-suggestion-loading 700ms linear infinite;",
        ".workspace-modal-overlay {",
        "place-items: center;",
        ".workspace-modal-dialog {",
        "grid-template-columns: var(--workspace-modal-icon-size) minmax(0, 1fr);",
        ".workspace-modal-copy {",
        ".browser-media-loading-notice {",
        ".shadow-backup-status-spinner {",
    )

    for token in expected_tokens:
        assert token in stylesheet


def test_browser_session_refresh_uses_the_modal_dialog_banner_message() -> None:
    """Match the sibling floating modal banner for completed session refreshes."""
    stylesheet = _stylesheet()

    for token in (
        ".browser-session-refresh-banner {",
        ".notice-floating-banner-global.browser-session-refresh-banner {",
        "animation-name: browserSessionRefreshBannerFadeIn;",
        "grid-template-columns: var(--workspace-modal-icon-size) minmax(0, 1fr);",
        "calc(var(--workspace-modal-pad-inline) + var(--workspace-modal-title-margin-end))",
        ".browser-session-refresh-banner-icon {",
        'mask: url("/static/images/info.circle.fill.svg") center/contain no-repeat;',
        ".browser-session-refresh-banner-heading {",
        ".browser-session-refresh-banner-copy {",
    ):
        assert token in stylesheet


def test_inline_notice_banner_keeps_copy_wide_and_status_chip_compact() -> None:
    """Keep icon-free inline notices readable beside their status chip."""
    stylesheet = _stylesheet()
    inline_start = stylesheet.index(".notice-inline-banner {")
    inline_rule = stylesheet[inline_start:stylesheet.index("\n}", inline_start)]

    for token in (
        "grid-template-columns: minmax(0, 1fr) auto;",
        "column-gap: var(--workspace-modal-column-gap);",
    ):
        assert token in inline_rule

    assert ".notice-inline-banner > .notice-floating-copy {" in stylesheet
    assert ".notice-inline-banner > .status-chip {" in stylesheet
    assert "grid-column: 1;" in stylesheet[stylesheet.index(".notice-inline-banner > .notice-floating-copy {"):]
    assert "justify-self: end;" in stylesheet[stylesheet.index(".notice-inline-banner > .status-chip {"):]
    assert "grid-template-columns: minmax(0, 1fr);" in stylesheet[stylesheet.index("@media (max-width: 560px)"):]


def test_browser_session_controls_use_standard_round_buttons_and_frosted_tooltips() -> None:
    """Render both session controls as round icon buttons with structured frosted tooltips."""
    stylesheet = _stylesheet()
    rule_start = stylesheet.index(".browser-session-control-button {")
    rule = stylesheet[rule_start:stylesheet.index("\n}", rule_start)]

    for token in (
        "width: var(--settings-round-icon-button-size);",
        "height: var(--settings-round-icon-button-size);",
        "border: var(--settings-round-icon-button-border);",
        "border-radius: var(--settings-round-icon-button-radius);",
        "background: var(--settings-round-icon-button-background);",
        "box-shadow: var(--settings-round-icon-button-shadow);",
        "color: var(--settings-round-icon-button-color);",
    ):
        assert token in rule

    for token in (
        ".browser-session-control-tooltip {",
        "border: var(--tooltip-border);",
        "background: var(--tooltip-background);",
        "box-shadow: var(--tooltip-shadow);",
        "backdrop-filter: var(--tooltip-blur);",
        "opacity: 0;",
        "visibility: hidden;",
        ".browser-session-control-button:hover > .browser-session-control-tooltip,",
        ".browser-session-control-button:focus-visible > .browser-session-control-tooltip {",
        ".browser-session-control-tooltip-title {",
        ".browser-session-control-tooltip-copy {",
    ):
        assert token in stylesheet

    assert ".browser-session-view-icon {" in stylesheet
    assert 'mask: url("/static/images/text.line.magnify.svg") center/contain no-repeat;' in stylesheet
    assert ".browser-session-refresh-icon {" in stylesheet
    assert 'mask: url("/static/images/icloud.and.arrow.down.svg") center/contain no-repeat;' in stylesheet
    assert ".browser-session-refresh-button::after" not in stylesheet


def test_chatgpt_prompt_expands_vertically_inside_its_media_card() -> None:
    """Keep prompt expansion inline, width-stable, and aligned with sibling round controls."""
    stylesheet = _stylesheet()
    gallery_start = stylesheet.index(".browser-gallery {")
    gallery_rule = stylesheet[gallery_start:stylesheet.index("\n}", gallery_start)]

    expected_tokens = (
        ".browser-media-prompt {",
        ".browser-media-prompt-expand {",
        ".browser-media-prompt-expand[hidden] {",
        "border-radius: 50%;",
        'mask: url("/static/images/rectangle.expand.vertical.svg") center/contain no-repeat;',
        'mask-image: url("/static/images/rectangle.compress.vertical.svg");',
        ".browser-media-prompt-preview {",
        "max-height: 4.2em;",
        ".browser-media-prompt.is-expanded .browser-media-prompt-preview {",
        ".browser-media-prompt.is-fully-visible .browser-media-prompt-preview::after {",
        "max-height: min(42rem, 70svh);",
        "overflow-y: auto;",
        ".browser-media-prompt-markdown blockquote {",
        ".browser-media-prompt-markdown pre {",
    )

    for token in expected_tokens:
        assert token in stylesheet

    assert "align-items: stretch;" in gallery_rule
    assert ".browser-prompt-dialog {" not in stylesheet


def test_prompt_label_matches_card_metadata_label_on_one_line() -> None:
    """Keep the prompt heading aligned with metadata terms without wrapping."""
    stylesheet = _stylesheet()
    label_start = stylesheet.index(".browser-media-prompt-label {")
    label_rule = stylesheet[label_start:stylesheet.index("\n}", label_start)]

    for token in (
        "display: block;",
        "color: var(--theme-muted);",
        "font-size: var(--font-ui-xs);",
        "font-weight: var(--font-weight-semibold);",
        "line-height: 1.35;",
        "white-space: nowrap;",
    ):
        assert token in label_rule

    prompt_start = stylesheet.index(".browser-media-prompt {")
    prompt_rule = stylesheet[prompt_start:stylesheet.index("\n}", prompt_start)]
    assert "margin: 0 0 10px;" in prompt_rule
    assert "padding: 8px 11px 9px;" in prompt_rule


def test_browser_media_actions_use_standard_round_icon_controls() -> None:
    """Keep Safari, copy, and file-manager actions visually consistent."""
    stylesheet = _stylesheet()

    expected_tokens = (
        ".browser-media-card {",
        "overflow: visible;",
        ".browser-media-card:hover,",
        "z-index: var(--layer-control-affordance);",
        ".browser-media-round-action {",
        "width: var(--settings-round-icon-button-size);",
        "height: var(--settings-round-icon-button-size);",
        "border-radius: 50%;",
        'mask: url("/static/images/safari.svg") center/contain no-repeat;',
        'mask: url("/static/images/finder.svg") center/contain no-repeat;',
        ".browser-media-reveal.is-revealed {",
        "border-radius: 0 0 var(--radius-panel) var(--radius-panel);",
        "border-radius: var(--radius-panel) var(--radius-panel) 0 0;",
        "border-radius: var(--radius-panel) 0 0 var(--radius-panel);",
    )

    for token in expected_tokens:
        assert token in stylesheet

    assert ".browser-media-source-link-label {" not in stylesheet
    assert ".browser-media-source-link-url," not in stylesheet


def test_browser_view_dock_switches_between_grid_and_list_layouts() -> None:
    """Keep media view controls aligned with the existing frosted dock system."""
    stylesheet = _stylesheet()

    expected_tokens = (
        ".browser-view-dock {",
        "--browser-view-dock-item-step: calc(var(--settings-round-icon-button-size) + 6px);",
        ".browser-view-dock::before {",
        "width: var(--settings-round-icon-button-size);",
        "transform: translateX(var(--browser-view-active-shift, 0px));",
        ".browser-view-dock-item {",
        "width: var(--settings-round-icon-button-size);",
        "height: var(--settings-round-icon-button-size);",
        ".browser-view-dock-item.is-active {",
        "transform: none;",
        "width: var(--settings-round-icon-button-icon-size);",
        "height: var(--settings-round-icon-button-icon-size);",
        'mask: url("/static/images/rectangle.grid.1x3.fill.svg") center/contain no-repeat;',
        'mask: url("/static/images/rectangle.grid.3x2.fill.svg") center/contain no-repeat;',
        '.browser-gallery[data-view="list"] {',
        '.browser-gallery[data-view="list"] .browser-media-open {',
        "grid-column: 1 / 3;",
        "grid-row: 1 / 4;",
        "grid-template-columns: 168px minmax(0, 1fr);",
        "grid-template-rows: subgrid;",
        "grid-template-rows: auto minmax(90px, 1fr) auto;",
        "place-items: center;",
        "object-fit: contain;",
        '.browser-gallery[data-view="list"] .browser-media-card-body {',
        "grid-row: 1;",
        "align-content: start;",
        "align-items: start;",
        "padding: 12px 14px 4px;",
        "grid-template-columns: 96px minmax(0, 1fr);",
        "justify-items: start;",
        "text-align: left;",
        "grid-template-rows: auto minmax(0, 1fr);",
        '.browser-gallery[data-view="list"] .browser-media-prompt-heading {',
        "align-items: flex-start;",
        "min-height: 72px;",
        '.browser-gallery[data-view="list"] .browser-media-prompt-label::after {',
        '.browser-gallery[data-view="list"] .browser-media-source-actions {',
        "grid-column: 2;",
        "grid-row: 3;",
        "justify-self: end;",
        "align-self: end;",
        '.browser-gallery[data-view="list"] .browser-media-remove {',
        "left: 12px;",
        '.browser-gallery[data-view="list"] .browser-media-remove-label {',
        "text-overflow: ellipsis;",
    )

    for token in expected_tokens:
        assert token in stylesheet


def test_browser_cached_media_previews_preserve_full_image_ratio() -> None:
    """Keep cached image previews complete instead of forcing a crop box."""
    stylesheet = _stylesheet()
    preview_start = stylesheet.index(".browser-media-preview {")
    preview_rule = stylesheet[preview_start:stylesheet.index("\n}", preview_start)]
    media_start = stylesheet.index(".browser-media-preview img,\n.browser-media-preview video {")
    media_rule = stylesheet[media_start:stylesheet.index("\n}", media_start)]

    for token in ("height: auto;", "align-self: flex-start;"):
        assert token in preview_rule

    for token in (
        "height: auto;",
        "max-height: none;",
        "object-fit: contain;",
        "object-position: center;",
    ):
        assert token in media_rule

    assert "aspect-ratio: 4 / 3;" not in preview_rule
    assert "object-fit: cover;" not in media_rule


def test_browser_filter_actions_reuse_the_standard_secondary_button() -> None:
    """Keep both filter actions on the shared secondary-button surface."""
    stylesheet = _stylesheet()
    for token in (
        "--control-compact-height: 28px;",
        "--control-form-height: 36px;",
    ):
        assert token in stylesheet

    compact_start = stylesheet.index(".ghost-link--compact {")
    compact_rule = stylesheet[compact_start:stylesheet.index("\n}", compact_start)]
    select_start = stylesheet.index(".trade-strategy-select,\n.form-select {")
    select_rule = stylesheet[select_start:stylesheet.index("\n}", select_start)]
    source_start = stylesheet.index(".trade-strategy-select.browser-source-filter-trigger {")
    source_rule = stylesheet[source_start:stylesheet.index("\n}", source_start)]
    status_start = stylesheet.index(".status-chip,\n.ghost-link {")
    status_rule = stylesheet[status_start:stylesheet.index("\n}", status_start)]
    ghost_start = stylesheet.index(".ghost-link {\n    background:")
    ghost_rule = stylesheet[ghost_start:stylesheet.index("\n}", ghost_start)]
    icon_start = stylesheet.index(".browser-source-filter-trigger .browser-picker-selected-icon-shell {")
    icon_rule = stylesheet[icon_start:stylesheet.index("\n}", icon_start)]

    assert "min-height: var(--control-compact-height);" in compact_rule
    assert "height: var(--control-compact-height);" in compact_rule
    assert "padding-inline: 4px;" in compact_rule
    assert "font-size: var(--font-size-3);" in compact_rule
    assert "line-height: 1;" in compact_rule
    assert "min-height: var(--control-form-height);" in select_rule
    assert "min-height: var(--control-form-height);" in source_rule
    assert "min-height: var(--control-compact-height);" in status_rule
    assert "font-weight: var(--font-weight-medium);" in ghost_rule
    assert "font-weight: var(--font-weight-semibold);" not in ghost_rule
    assert "font-weight:" not in compact_rule
    assert "width: 18px;" in icon_rule
    assert "height: 18px;" in icon_rule

    browser_template = (
        STYLE_PATH.parents[1] / "templates/browser.html"
    ).read_text(encoding="utf-8")
    assert ".browser-filter-actions .ghost-link--compact {" not in stylesheet
    assert ".browser-filter-actions .secondary-button {" in stylesheet
    actions_start = stylesheet.index(".browser-filter-actions {")
    actions_rule = stylesheet[actions_start:stylesheet.index("\n}", actions_start)]
    assert "grid-template-columns: minmax(0, 1fr);" in actions_rule
    refresh_container_start = stylesheet.index(".browser-filter-actions .secondary-button {")
    refresh_container_rule = stylesheet[
        refresh_container_start:stylesheet.index("\n}", refresh_container_start)
    ]
    for token in (
        "width: fit-content;",
        "max-width: 100%;",
        "justify-self: end;",
    ):
        assert token in refresh_container_rule
    assert "\n    width: 100%;" not in refresh_container_rule
    for markup in (
        'class="secondary-button browser-session-back-link"',
        'class="secondary-button browser-clear-link"',
    ):
        assert markup in browser_template
    assert 'class="ghost-link ghost-link--compact browser-session-back-link"' not in browser_template
    assert "ChatGPT Media cache" not in browser_template
    assert "browser-chatgpt-media-link" not in browser_template
    assert 'class="secondary-button browser-refresh-button"' in browser_template


def test_style_token_secondary_button_preview_stays_intrinsic_and_reserves_svg_icon() -> None:
    """Keep the Style tokens specimen compact while leaving icon use data-driven."""
    stylesheet = _stylesheet()
    preview_start = stylesheet.index(".style-token-secondary-button-preview {")
    preview_rule = stylesheet[preview_start:stylesheet.index("\n}", preview_start)]
    button_start = stylesheet.index(".style-token-secondary-button-demo .secondary-button {")
    button_rule = stylesheet[button_start:stylesheet.index("\n}", button_start)]
    secondary_start = stylesheet.index(".secondary-button {")
    secondary_rule = stylesheet[secondary_start:stylesheet.index("\n}", secondary_start)]

    assert "gap: 6px;" in button_rule
    for token in ("width: fit-content;", "max-width: 100%;"):
        assert token in preview_rule
        assert token in button_rule
        assert token in secondary_rule
    assert "justify-self: end;" in secondary_rule
    assert "margin-inline-start: auto;" in secondary_rule
    assert "\n    width: auto;" not in secondary_rule
    assert "\n    width: 100%;" not in secondary_rule

    template = (
        STYLE_PATH.parents[1] / "templates/settings_style_tokens.html"
    ).read_text(encoding="utf-8")
    assert "row.use_icon" in template
    assert "row.icon_class" in template
    assert "data-style-token-secondary-button-use-icon" in template


def test_prompt_tag_specimen_reuses_the_saved_prompt_tag_contract() -> None:
    """Keep the Style tokens tag specimen on the live prompt-tag classes."""
    stylesheet = _stylesheet()
    prompt_tag_start = stylesheet.index(".browser-prompt-tag {")
    prompt_tag_rule = stylesheet[prompt_tag_start:stylesheet.index("\n}", prompt_tag_start)]

    for token in (
        "border-radius: var(--radius-pill);",
        "color: var(--accent-text);",
        "font-size: var(--font-ui-sm);",
        "font-weight: var(--font-weight-medium);",
    ):
        assert token in prompt_tag_rule

    template = (
        STYLE_PATH.parents[1] / "templates/settings_style_tokens.html"
    ).read_text(encoding="utf-8")
    assert 'data-style-token-demo="prompt-tag"' in template
    assert 'class="browser-prompt-tag"' in template
    assert 'class="browser-prompt-tag-remove"' in template
    assert 'data-style-token-demo="type-specimen"' not in template
    assert 'aria-label="Frosted glass demo"' in template
    assert "The Agent selector keeps the active browser visible" not in template

    cache_template = (
        STYLE_PATH.parents[1] / "templates/_cache_page.html"
    ).read_text(encoding="utf-8")
    assert 'class="secondary-button cache-settings-link"' in cache_template
    secondary_start = stylesheet.index(".secondary-button {")
    secondary_rule = stylesheet[secondary_start:stylesheet.index("\n}", secondary_start)]
    for token in (
        "display: inline-flex;",
        "align-items: center;",
        "justify-content: center;",
        "text-decoration: none;",
        "width: fit-content;",
        "max-width: 100%;",
        "justify-self: end;",
    ):
        assert token in secondary_rule


def test_style_token_component_catalog_consumes_the_sibling_control_contracts() -> None:
    """Keep shared Primary, Switch, material, and physical-layout primitives live."""
    stylesheet = _stylesheet()

    primary_start = stylesheet.index(".primary-button {")
    primary_rule = stylesheet[primary_start:stylesheet.index("\n}", primary_start)]
    for token in (
        "min-height: var(--primary-button-min-height);",
        "padding: var(--primary-button-pad-block) var(--primary-button-pad-inline);",
        "border: var(--primary-button-border);",
        "background: var(--primary-button-background);",
        "font-weight: var(--primary-button-font-weight);",
    ):
        assert token in primary_rule

    switch_start = stylesheet.index(".ios-switch-slider {")
    switch_rule = stylesheet[switch_start:stylesheet.index("\n}", switch_start)]
    assert "border-radius: var(--switch-radius);" in switch_rule
    assert "background: var(--switch-track-background);" in switch_rule
    assert "box-shadow: var(--switch-track-shadow);" in switch_rule

    for token in (
        "--segmented-control-material: var(--frosted-glass-background);",
        "--settings-round-icon-button-material: var(--frosted-glass-background);",
        "--settings-action-package-material: var(--frosted-glass-background);",
        "--workspace-modal-material: var(--frosted-glass-background);",
        "--notice-floating-material: var(--frosted-glass-background);",
        "--scrollable-data-table-header-material: var(--frosted-glass-background);",
        "--shared-select-trigger-material: var(--frosted-glass-background);",
        "--shared-select-dropdown-material: var(--frosted-glass-opaque-background);",
    ):
        assert token in stylesheet

    for fragment in (
        ".style-token-card {",
        "grid-template-columns: minmax(220px, var(--style-token-demo-width-effective)) minmax(280px, 1fr);",
        ".style-token-demo-surface {",
        "overflow: visible;",
        "@media (max-width: 900px) {",
        ".style-token-card { grid-template-columns: minmax(0, 1fr); }",
    ):
        assert fragment in stylesheet


def test_browser_refresh_action_uses_the_13px_annotation_size() -> None:
    """Keep the remaining Local resources refresh action on the shared text-size token."""
    stylesheet = _stylesheet()
    compact_start = stylesheet.index(".ghost-link--compact {")
    compact_rule = stylesheet[compact_start:stylesheet.index("\n}", compact_start)]
    refresh_start = stylesheet.index(".browser-refresh-button {")
    refresh_rule = stylesheet[refresh_start:stylesheet.index("\n}", refresh_start)]

    assert "font-size: var(--font-size-3);" in compact_rule
    assert "font-size: var(--font-size-3);" in refresh_rule


def test_browser_filter_select_uses_one_shared_frosted_surface() -> None:
    """Keep Local resources and Agent menus on the same frosted base."""
    stylesheet = _stylesheet()
    dropdown_start = stylesheet.index(".trade-strategy-dropdown {")
    dropdown_rule = stylesheet[dropdown_start:stylesheet.index("\n}", dropdown_start)]
    select_shell_start = stylesheet.index(".browser-filter-select {")
    select_shell_rule = stylesheet[select_shell_start:stylesheet.index("\n}", select_shell_start)]
    open_rule_start = stylesheet.index(".browser-filter-select.is-open .browser-filter-select-dropdown {")
    open_rule = stylesheet[open_rule_start:stylesheet.index("\n}", open_rule_start)]

    for token in (
        "border-radius: var(--radius-soft);",
        "background: var(--shared-select-dropdown-material);",
        "box-shadow: var(--frosted-glass-shadow);",
        "backdrop-filter: var(--frosted-glass-blur);",
        "-webkit-backdrop-filter: var(--frosted-glass-blur);",
        "border: var(--frosted-glass-border);",
        "background-clip: padding-box;",
    ):
        assert token in dropdown_rule
    agent_dropdown_start = stylesheet.index(
        ".backtest-shared-select-dropdown.browser-session-dropdown {"
    )
    agent_dropdown_rule = stylesheet[
        agent_dropdown_start:stylesheet.index("\n}", agent_dropdown_start)
    ]
    assert agent_dropdown_rule == (
        ".backtest-shared-select-dropdown.browser-session-dropdown {\n"
        "    z-index: var(--layer-global-popover);\n"
        "    display: none;"
    )
    assert ".backtest-shared-select-dropdown.browser-session-dropdown::before" not in stylesheet
    assert "position: relative;" in select_shell_rule
    assert "z-index: var(--layer-global-popover);" in stylesheet
    assert "display: grid;" in open_rule


def test_shared_select_reuses_regular_labels_pill_options_and_browser_height() -> None:
    """Keep the annotated select values on their shared production rules."""
    stylesheet = _stylesheet()
    label_start = stylesheet.index(
        ".trade-strategy-trigger-label.browser-session-trigger-label {"
    )
    label_rule = stylesheet[label_start:stylesheet.index("\n}", label_start)]
    option_start = stylesheet.index(".trade-strategy-dropdown-option {")
    option_rule = stylesheet[option_start:stylesheet.index("\n}", option_start)]
    agent_browser_start = stylesheet.index(
        ".agent-browser-combobox .agent-combobox-trigger {"
    )
    agent_browser_rule = stylesheet[
        agent_browser_start:stylesheet.index("\n}", agent_browser_start)
    ]

    assert "font-weight: var(--font-weight-regular);" in label_rule
    assert "border-radius: var(--shared-select-option-radius);" in option_rule
    assert "min-height: var(--control-form-height);" in agent_browser_rule
    assert "padding-block: 3px;" in agent_browser_rule


def test_browser_grid_filename_wraps_without_hiding_its_extension() -> None:
    """Allow grid filenames to wrap fully while keeping list rows compact."""
    stylesheet = _stylesheet()
    title_start = stylesheet.index(".browser-media-card-title {")
    title_rule = stylesheet[title_start:stylesheet.index("\n}", title_start)]
    list_title_start = stylesheet.index(
        '.browser-gallery[data-view="list"] .browser-media-card-title {'
    )
    list_title_rule = stylesheet[
        list_title_start:stylesheet.index("\n}", list_title_start)
    ]

    assert "display: block;" in title_rule
    assert "overflow-wrap: anywhere;" in title_rule
    assert "word-break: break-word;" in title_rule
    assert "-webkit-line-clamp" not in title_rule
    assert "display: block;" in list_title_rule
    assert "white-space: nowrap;" in list_title_rule


def test_chatgpt_session_metrics_put_the_session_name_on_its_own_row() -> None:
    """Keep the Foundation session metrics readable across breakpoints."""
    stylesheet = _stylesheet()

    for token in (
        ".browser-metric-grid.browser-session-metric-grid {",
        "grid-template-columns: repeat(2, minmax(0, 1fr));",
        ".browser-session-name-metric {",
        "grid-column: 1 / -1;",
        ".browser-session-controls-row {",
        "justify-content: flex-end;",
    ):
        assert token in stylesheet

    session_name_start = stylesheet.index(".browser-metric-grid .browser-session-name-metric strong {")
    session_name_rule = stylesheet[session_name_start:stylesheet.index("\n}", session_name_start)]
    assert "overflow-wrap: anywhere;" in session_name_rule


def test_browser_metric_foundation_and_prompt_copy_use_shared_sizes() -> None:
    """Keep browser metrics on Foundation and inline prompt copy on the shared type scale."""
    stylesheet = _stylesheet()

    browser_template = (
        STYLE_PATH.parents[1] / "templates/browser.html"
    ).read_text(encoding="utf-8")
    assert "foundation-metric-card" in browser_template
    assert "browser-media-primary-metric" not in browser_template
    assert ".browser-media-primary-metric strong {" not in stylesheet
    prompt_start = stylesheet.index(".browser-media-prompt-preview {")
    prompt_rule = stylesheet[prompt_start:stylesheet.index("\n}", prompt_start)]

    assert "font-size: var(--font-ui-sm);" in prompt_rule


def test_browser_media_preview_kind_uses_regular_weight() -> None:
    """Keep Image and Video preview labels on the regular UI weight."""
    stylesheet = _stylesheet()
    kind_start = stylesheet.index(".browser-preview-kind {")
    kind_rule = stylesheet[kind_start:stylesheet.index("\n}", kind_start)]

    assert "font-weight: var(--font-weight-regular);" in kind_rule


def test_browser_pagination_matches_the_sibling_floating_control_states() -> None:
    """Keep unselected browser pagination controls visually transparent by default."""
    stylesheet = _stylesheet()
    button_start = stylesheet.index(".browser-pagination .local-store-page-button {")
    button_rule = stylesheet[button_start:stylesheet.index("\n}", button_start)]
    hover_start = stylesheet.index(
        ".browser-pagination .local-store-page-button:not(.is-active):not(.local-store-page-placeholder):hover,"
    )
    hover_rule = stylesheet[hover_start:stylesheet.index("\n}", hover_start)]

    for token in (
        "border-radius: 50%;",
        "border-color: transparent;",
        "background: transparent;",
        "box-shadow: none;",
        "backdrop-filter: none;",
        "color: var(--theme-text);",
    ):
        assert token in button_rule

    assert "border: var(--frosted-glass-border);" in hover_rule
    assert "transform:" not in hover_rule

    ellipsis_start = stylesheet.index(".browser-pagination .local-store-page-ellipsis {")
    ellipsis_rule = stylesheet[ellipsis_start:stylesheet.index("\n}", ellipsis_start)]
    ellipsis_hover_start = stylesheet.index(
        ".browser-pagination .local-store-page-ellipsis:is(:hover, :focus-within) {"
    )
    ellipsis_hover_rule = stylesheet[ellipsis_hover_start:stylesheet.index("\n}", ellipsis_hover_start)]
    assert "border-radius: var(--radius-pill);" in ellipsis_rule
    assert "transition:" in ellipsis_rule
    assert "background: var(--frosted-glass-background-hover);" in ellipsis_hover_rule
    assert "box-shadow: var(--frosted-glass-shadow-active);" in ellipsis_hover_rule


def test_motion_tokens_match_the_sibling_non_linear_motion_contract() -> None:
    """Keep shared transitions valid and aligned with the sibling motion foundation."""
    stylesheet = _stylesheet()

    for token in (
        "--motion-duration-fast: 160ms;",
        "--motion-duration-standard: 240ms;",
        "--motion-duration-emphasized: 420ms;",
        "--motion-duration-spatial: 560ms;",
        "--motion-standard: cubic-bezier(0.2, 0, 0, 1);",
        "--motion-emphasized: cubic-bezier(0.16, 1, 0.3, 1);",
        "--motion-inertial: cubic-bezier(0.16, 1, 0.3, 1);",
        "--motion-bouncy: cubic-bezier(0.34, 1.56, 0.64, 1);",
        "--motion-overshoot: cubic-bezier(0.34, 1.32, 0.64, 1);",
    ):
        assert token in stylesheet


def test_agent_response_pagination_ellipsis_has_a_visible_hover_affordance() -> None:
    """Keep the Agent response ellipsis visibly interactive on hover."""
    stylesheet = _stylesheet()

    for token in (
        ".browser-pagination.agent-response-pagination .local-store-page-ellipsis:hover,",
        "cursor: pointer;",
        "background: var(--frosted-glass-background-hover);",
        "color: var(--accent-text-hover);",
        ".browser-pagination.agent-response-pagination .local-store-page-ellipsis-dots {",
        "transform: scale(1.12);",
    ):
        assert token in stylesheet


def test_browser_pagination_indicator_uses_composited_spatial_motion() -> None:
    """Keep pagination indicator movement smooth when page controls are rebuilt."""
    stylesheet = _stylesheet()
    indicator_start = stylesheet.index(".browser-pagination .local-store-pagination-indicator {")
    indicator_rule = stylesheet[indicator_start:stylesheet.index("\n}", indicator_start)]

    for token in (
        "transform: translate3d(0, 0, 0) scale(1);",
        "transition:",
        "transform var(--local-store-pagination-motion-duration) var(--local-store-pagination-motion-easing),",
        "opacity var(--motion-duration-standard) var(--motion-standard);",
        "will-change: transform, opacity;",
    ):
        assert token in indicator_rule


def test_browser_pagination_range_menu_uses_glass_and_gel_motion_tokens() -> None:
    """Keep range expansion aligned with the sibling popover motion language."""
    stylesheet = _stylesheet()

    for token in (
        ".browser-pagination-range-menu {",
        "background: var(--frosted-glass-opaque-background, var(--frosted-glass-background));",
        "background-clip: padding-box;",
        "backdrop-filter: var(--frosted-glass-blur);",
        "overflow-y: auto;",
        "scrollbar-width: none;",
        ".browser-pagination-range-menu.is-scrollable {",
        "overflow-y: auto;",
        "border-radius: var(--radius-soft);",
        "animation: browser-pagination-range-gel-in 300ms var(--motion-bouncy) both;",
        "@keyframes browser-pagination-range-gel-in {",
        "grid-template-columns: repeat(auto-fill, minmax(72px, 1fr));",
    ):
        assert token in stylesheet


def test_browser_pagination_range_menu_hides_the_scrollbar_track() -> None:
    """Keep the range menu scrollable without painting a scrollbar track."""
    stylesheet = _stylesheet()
    menu_start = stylesheet.index(".browser-pagination-range-menu {")
    menu_end = stylesheet.index(".browser-pagination-range-menu.is-scrollable {", menu_start)
    menu_rule = stylesheet[menu_start:menu_end]

    assert "overflow-y: auto;" in menu_rule
    assert "scrollbar-width: none;" in menu_rule
    assert "-ms-overflow-style: none;" in menu_rule
    assert "border-radius: var(--radius-soft);" in menu_rule
    assert "scrollbar-gutter:" not in menu_rule
    scrollbar_start = stylesheet.index(".browser-pagination-range-menu::-webkit-scrollbar {")
    scrollbar_rule = stylesheet[scrollbar_start:stylesheet.index("\n}", scrollbar_start)]
    assert "width: 0;" in scrollbar_rule
    assert "height: 0;" in scrollbar_rule
    assert "background: transparent;" in scrollbar_rule
    track_start = stylesheet.index(".browser-pagination-range-menu::-webkit-scrollbar-track {")
    track_rule = stylesheet[track_start:stylesheet.index("\n}", track_start)]
    assert "background: transparent;" in track_rule


def test_browser_pagination_range_menu_respects_clipping_ancestors() -> None:
    """Keep the range menu inside the nearest scrollable workspace boundary."""
    script = (
        Path(__file__).resolve().parents[1] / "app/web/static/local-media-browser.js"
    ).read_text(encoding="utf-8")

    for token in (
        "function paginationRangeMenuClipBounds(picker)",
        "style.overflowX !== \"visible\" || style.overflowY !== \"visible\"",
        "const clipTop = Math.max(viewportInset, clipBounds.top);",
        "const clipBottom = Math.min(window.innerHeight - viewportInset, clipBounds.bottom);",
        "const spaceAbove = Math.max(0, pickerRect.top - clipTop - menuGap);",
        "const spaceBelow = Math.max(0, clipBottom - pickerRect.bottom - menuGap);",
    ):
        assert token in script


def test_segmented_control_uses_the_sibling_generic_pill_contract() -> None:
    """Keep Cache content modes on the sibling project's generic control contract."""
    stylesheet = _stylesheet()
    option_start = stylesheet.index(".segmented-control-option span {")
    option_rule = stylesheet[option_start:stylesheet.index("\n}", option_start)]
    selected_start = stylesheet.index(".segmented-control-option input:checked + span,")
    selected_rule = stylesheet[selected_start:stylesheet.index("\n}", selected_start)]
    control_start = stylesheet.index(".segmented-control,\n.range-mode-shell {")
    control_rule = stylesheet[control_start:stylesheet.index("\n}", control_start)]
    browser_start = stylesheet.index(".browser-content-mode-control {")
    browser_rule = stylesheet[browser_start:stylesheet.index("\n}", browser_start)]
    style_token_start = stylesheet.index(".style-token-page .range-mode-shell {")
    style_token_rule = stylesheet[style_token_start:stylesheet.index("\n}", style_token_start)]

    for token in (
        ".segmented-control,\n.range-mode-shell {",
        "--segmented-option-count: 2;",
        "grid-template-columns: repeat(var(--segmented-option-count), minmax(var(--segmented-option-min-width), 1fr));",
        "--mode-switch-radius: var(--radius-pill);",
        "--mode-switch-gap: 4px;",
        "border: 0;",
        ".segmented-control[data-option-count]::before,\n.range-mode-shell[data-option-count]::before {",
        "transform: translateX(calc((100% + var(--mode-switch-gap)) * var(--segmented-active-index, 0)));",
        ".segmented-control[data-segmented-pill=\"measured\"][data-option-count]::before,",
        "transform: translateX(var(--segmented-pill-left, calc((100% + var(--mode-switch-gap)) * var(--segmented-active-index, 0))));",
        ".segmented-control-option,\n.range-mode-option {",
        "text-decoration: none;",
        ".segmented-control-option input:checked + span,",
        "font-weight: var(--font-weight-regular);",
        "font-weight: var(--font-weight-bold);",
        "color: var(--color-white-adaptive);",
        ".browser-chat-list {",
        ".browser-chat-message {",
        ".browser-chat-message-link {",
        ".chart-panel.workspace.browser-workspace {",
        ".browser-text-summary-card {",
        "height: auto;",
        ".browser-session-table-number {",
        "text-align: center;",
        ".browser-chat-role-mark {",
        ".browser-session-table-role {",
        "display: table-cell;",
        ".browser-session-detail-actions {",
        ".browser-session-neighbor-nav {",
        ".browser-session-neighbor-button:disabled {",
        ".browser-session-neighbor-prev-icon {",
        ".browser-session-neighbor-next-icon {",
        ".browser-session-actions {",
        ".browser-session-action-button {",
        ".browser-session-actions > .browser-session-open-original-button {",
        ".browser-session-actions > .browser-session-refresh-button {",
        ".browser-session-actions > .browser-session-full-export-button {",
        ".browser-session-drawer-refresh-icon {",
        'mask-image: url("/static/images/arrow.trianglehead.clockwise.svg");',
        ".browser-session-page-export-icon {",
        ".browser-session-export-icon {",
        ".browser-session-index-table .browser-session-col-number {",
        ".browser-session-table-source {",
        ".browser-session-source-mark,",
        ".browser-session-table-role > .browser-chat-role-mark,",
        "display: grid;",
        "margin-inline: auto;",
        ".browser-session-table-updated-link {",
    ):
        assert token in stylesheet

    assert "font-weight: var(--font-weight-regular);" in option_rule
    assert "font-weight: var(--font-weight-bold);" not in option_rule
    assert "font-weight: var(--font-weight-bold);" in selected_rule
    for token in ("width: fit-content;", "max-width: 100%;", "margin-inline: auto;"):
        assert token in control_rule
    for token in ("width: fit-content;", "max-width: 100%;", "justify-self: center;"):
        assert token in browser_rule
        assert token in style_token_rule


def test_browser_session_safari_drawer_icon_uses_two_theme_accents() -> None:
    """Keep the session drawer's Safari mark in the shared two-tone icon system."""
    stylesheet = _stylesheet()
    icon_start = stylesheet.index(".browser-session-action-button .browser-session-safari-icon {")
    icon_rule = stylesheet[icon_start:stylesheet.index("\n}", icon_start)]

    for token in (
        "background: linear-gradient(90deg, var(--accent) 0%, var(--accent-secondary) 100%);",
        'mask: url("/static/images/safari.svg") center/contain no-repeat;',
        '-webkit-mask: url("/static/images/safari.svg") center/contain no-repeat;',
    ):
        assert token in icon_rule


def test_style_tokens_sidebar_icon_preserves_colorful_mark() -> None:
    """Keep the Style tokens sidebar mark colorful in both Settings nav variants."""
    stylesheet = _stylesheet()
    category_icon_start = stylesheet.index(".settings-category-nav-icon {")
    category_icon_rule = stylesheet[
        category_icon_start:stylesheet.index("\n}", category_icon_start)
    ]
    settings_icon_start = stylesheet.index(".settings-nav-item .icon,")
    settings_icon_rule = stylesheet[
        settings_icon_start:stylesheet.index("\n}", settings_icon_start)
    ]
    style_token_start = stylesheet.index(".settings-category-nav-item-style-tokens {")
    style_token_rule = stylesheet[
        style_token_start:stylesheet.index("\n}", style_token_start)
    ]
    settings_style_token_start = stylesheet.index(".settings-nav-item-style-tokens {")
    settings_style_token_rule = stylesheet[
        settings_style_token_start:stylesheet.index("\n}", settings_style_token_start)
    ]
    active_rule_start = stylesheet.index(".settings-nav-item.is-active .icon {")
    active_rule = stylesheet[
        active_rule_start:stylesheet.index("\n}", active_rule_start)
    ]

    assert "background: var(--settings-icon-background, var(--settings-category-icon-tint, var(--theme-muted)));" in category_icon_rule
    assert "background: var(--settings-icon-background, var(--settings-icon-tint, var(--theme-muted)));" in settings_icon_rule
    assert "background: var(--settings-icon-background, var(--accent-fill));" in active_rule
    for rule in (style_token_rule, settings_style_token_rule):
        assert "--settings-icon-background: linear-gradient(90deg, var(--accent) 0%, var(--accent-secondary) 100%);" in rule


def test_browser_session_title_reuses_regular_untagged_link_contract() -> None:
    """Keep cached session titles readable without browser-default underlines."""
    stylesheet = _stylesheet()
    title_start = stylesheet.index(".browser-session-table-title,")
    title_rule = stylesheet[title_start:stylesheet.index("\n}", title_start)]

    for token in (
        "font-size: var(--font-ui-lg);",
        "font-weight: var(--font-weight-regular);",
        "text-decoration: none;",
    ):
        assert token in title_rule


def test_browser_session_updated_link_reuses_untagged_link_contract() -> None:
    """Keep updated timestamps readable without browser-default underlines."""
    stylesheet = _stylesheet()
    updated_start = stylesheet.index(".browser-session-table-updated-link {")
    updated_rule = stylesheet[updated_start:stylesheet.index("\n}", updated_start)]

    assert "text-decoration: none;" in updated_rule


def test_browser_session_counts_reuse_regular_weight_and_roomy_cell_padding() -> None:
    """Keep session message counts legible and separated from adjacent columns."""
    stylesheet = _stylesheet()
    count_start = stylesheet.index(".events-table tbody td.browser-session-table-count {")
    count_rule = stylesheet[count_start:stylesheet.index("\n}", count_start)]

    for token in (
        "font-weight: var(--font-weight-regular);",
        "padding-inline: 10px;",
    ):
        assert token in count_rule


def test_cache_workspace_reuses_the_shared_title_rail_and_scroll_layer() -> None:
    """Keep Cache title and Events flow inside the named scroll layer."""
    stylesheet = _stylesheet()

    for token in (
        "--layout-edge-gap: var(--page-edge-pad);",
        "--layout-global-anchor-inset: calc(var(--layout-edge-gap) * 2);",
        "--layout-global-action-gap: 10px;",
        "--layout-sidebar-dock-bottom-gap: var(--layout-edge-gap);",
        "--workspace-title-rail-pad-block-start: var(--page-edge-pad);",
        "--workspace-title-rail-height: calc(var(--workspace-title-rail-pad-block-start) + var(--workspace-title-rail-control-height));",
        ".cache-overview-title-card {",
        "min-height: var(--workspace-title-rail-height);",
        "padding: var(--workspace-title-rail-pad-block-start) var(--workspace-article-pad-inline) 0;",
        ".cache-overview-title-card > .report-heading-row {",
        "min-height: var(--workspace-title-rail-control-height);",
        ".app-shell.is-sidebar-collapsed .cache-overview-title-card,",
        "padding-inline-start: var(--workspace-title-rail-collapsed-pad-inline-start);",
        ".cache-workspace-content {",
        "overflow-y: auto;",
        "padding-inline-end: 68px;",
        ".cache-overview-title-card .report-heading {",
        "text-wrap: balance;",
        "main[data-cache-page] #workspace_panel > .workspace-header > .cache-workspace-content > .workspace-grid {",
        "flex: 1 0 240px;",
        "height: auto;",
    ):
        assert token in stylesheet

    assert (
        "main[data-cache-page] #workspace_panel > .workspace-header > "
        ".cache-workspace-content > #overview {"
    ) not in stylesheet


def test_browser_prompt_source_header_is_centered() -> None:
    """Keep the saved-prompts Source header aligned with its centered marks."""
    stylesheet = _stylesheet()
    rule_start = stylesheet.index(".browser-prompt-table .browser-prompt-col-source {")
    rule = stylesheet[rule_start:stylesheet.index("\n}", rule_start)]

    assert "text-align: center;" in rule


def test_browser_summary_metric_layout_uses_foundation_card_width() -> None:
    """Keep ordinary browser summary metrics on Foundation card geometry."""
    stylesheet = _stylesheet()
    assert ".browser-metric-grid:not(.browser-session-metric-grid) > .metric-card strong {" not in stylesheet

    card_start = stylesheet.index(
        ".browser-metric-grid:not(.browser-session-metric-grid) > .metric-card {"
    )
    card_rule = stylesheet[card_start:stylesheet.index("\n}", card_start)]
    assert "width: 192px;" in card_rule
    assert "border-radius:" not in card_rule


def test_browser_summary_metric_contract_reuses_foundation_for_siblings() -> None:
    """Apply Foundation to every ordinary three-column summary card."""
    stylesheet = _stylesheet()

    selector = ".browser-metric-grid:not(.browser-session-metric-grid) > .metric-card"
    card_selector = f"{selector} {{"
    assert stylesheet.count(card_selector) == 2
    assert ".browser-prompts-primary-metric {" not in stylesheet
    assert ".browser-prompts-primary-metric strong {" not in stylesheet
    assert ".browser-media-primary-metric strong {" not in stylesheet

    narrow_rule_start = stylesheet.rindex(card_selector)
    narrow_rule = stylesheet[narrow_rule_start:stylesheet.index("\n}", narrow_rule_start)]
    assert "width: 100%;" in narrow_rule


def test_metric_labels_use_the_shared_regular_weight_token() -> None:
    """Keep all Local resources metric labels on the 400-weight baseline."""
    stylesheet = _stylesheet()
    label_start = stylesheet.index(".metric-label {")
    label_rule = stylesheet[label_start:stylesheet.index("\n}", label_start)]

    assert "font-weight: var(--font-weight-regular);" in label_rule
    assert "font-size: var(--font-ui-md);" in label_rule
    assert "font-weight: var(--font-weight-semibold);" not in label_rule


def test_browser_prompt_table_reallocates_space_after_saved_column_removal() -> None:
    """Keep the remaining saved-prompts columns sized without a dead gap."""
    stylesheet = _stylesheet()

    assert ".browser-prompt-table .browser-prompt-col-added {" not in stylesheet
    content_start = stylesheet.index(".browser-prompt-table .browser-prompt-col-content {")
    content_rule = stylesheet[content_start:stylesheet.index("\n}", content_start)]
    source_start = stylesheet.index(".browser-prompt-table .browser-prompt-col-source {")
    source_rule = stylesheet[source_start:stylesheet.index("\n}", source_start)]
    remarks_start = stylesheet.index(".browser-prompt-table .browser-prompt-col-remarks {")
    remarks_rule = stylesheet[remarks_start:stylesheet.index("\n}", remarks_start)]

    assert "width: 50%;" in content_rule
    assert "width: 10%;" in source_rule
    assert "width: 33%;" in remarks_rule


def test_browser_prompt_copy_action_is_hidden_until_prompt_hover_or_focus() -> None:
    """Keep prompt copying discoverable without occupying the prompt content."""
    stylesheet = _stylesheet()

    copy_start = stylesheet.index(".browser-prompt-table-prompt-cell .browser-prompt-copy-button {")
    copy_rule = stylesheet[copy_start:stylesheet.index("\n}", copy_start)]
    assert "position: absolute;" in copy_rule
    assert "opacity: 0;" in copy_rule
    assert "pointer-events: none;" in copy_rule
    assert ".browser-prompt-table tbody tr:hover .browser-prompt-copy-button" in stylesheet
    assert ".browser-prompt-table-prompt-cell:focus-within .browser-prompt-copy-button" in stylesheet


def test_browser_prompt_remarks_use_pill_tags_and_stored_controls() -> None:
    """Keep saved prompt remarks compact, removable, and keyboard reachable."""
    stylesheet = _stylesheet()

    tag_start = stylesheet.index(".browser-prompt-tag {")
    tag_rule = stylesheet[tag_start:stylesheet.index("\n}", tag_start)]
    assert "border-radius: var(--radius-pill);" in tag_rule
    assert ".browser-prompt-remark-editor input {" in stylesheet
    assert ".browser-prompt-remark-add {" not in stylesheet
    assert ".browser-prompt-remark-add:focus-visible" not in stylesheet
    input_start = stylesheet.index(".browser-prompt-remark-editor input {")
    input_rule = stylesheet[input_start:stylesheet.index("\n}", input_start)]
    assert "min-height: 32px;" in input_rule
    assert "height: 32px;" in input_rule
    assert "border: 0;" in input_rule
    assert "border-radius: var(--radius-panel);" in input_rule
    assert "font-size: var(--font-size-3);" in input_rule


def test_browser_session_messages_wrap_rich_content_inside_fixed_cells() -> None:
    """Keep code blocks and nested tables inside the session message column."""
    stylesheet = _stylesheet()

    message_start = stylesheet.index(".browser-session-table-message {")
    message_rule = stylesheet[message_start:stylesheet.index("\n}", message_start)]
    pre_start = stylesheet.index(".browser-session-table-message pre {")
    pre_rule = stylesheet[pre_start:stylesheet.index("\n}", pre_start)]
    code_start = stylesheet.index(".browser-session-table-message pre code {")
    code_rule = stylesheet[code_start:stylesheet.index("\n}", code_start)]
    nested_table_start = stylesheet.index(".browser-session-table-message table {")
    nested_table_rule = stylesheet[nested_table_start:stylesheet.index("\n}", nested_table_start)]

    for token in (
        "box-sizing: border-box;",
        "width: 100%;",
        "min-width: 0;",
        "max-width: 100%;",
        "overflow-x: auto;",
        "overflow-wrap: anywhere;",
        "word-break: break-word;",
    ):
        assert token in message_rule
    assert ".browser-session-table-message > * {" in stylesheet
    assert ".browser-session-table-message img," in stylesheet
    detail_table_start = stylesheet.index(".browser-session-detail-table {")
    detail_table_rule = stylesheet[detail_table_start:stylesheet.index("\n}", detail_table_start)]
    assert "width: 100%;" in detail_table_rule
    assert "min-width: 0;" in detail_table_rule
    message_cell_start = stylesheet.index(".browser-session-detail-table tbody > tr > td:last-child {")
    message_cell_rule = stylesheet[message_cell_start:stylesheet.index("\n}", message_cell_start)]
    assert "padding-right: 18px;" in message_cell_rule
    for token in (
        "box-sizing: border-box;",
        "max-width: 100%;",
        "overflow-x: auto;",
        "white-space: pre-wrap;",
        "overflow-wrap: anywhere;",
        "word-break: break-word;",
    ):
        assert token in pre_rule
    for token in ("white-space: inherit;", "overflow-wrap: inherit;", "word-break: inherit;"):
        assert token in code_rule
    for token in ("width: 100%;", "max-width: 100%;", "table-layout: fixed;"):
        assert token in nested_table_rule


def test_browser_session_messages_default_to_compact_vertical_disclosure() -> None:
    """Keep long session messages compact until their standard control is activated."""
    stylesheet = _stylesheet()

    for token in (
        ".browser-session-table-message-shell {",
        ".browser-session-table-message-shell:not(.is-expanded) .browser-session-table-message {",
        "max-height: min(20rem, 42svh);",
        "overflow-y: hidden;",
        ".browser-session-table-message-shell.is-collapsible:not(.is-expanded) .browser-session-table-message::after {",
        ".browser-session-message-toggle {",
        "border-radius: 50%;",
        ".browser-session-message-toggle[hidden] {",
        ".browser-session-message-toggle-icon {",
        'mask: url("/static/images/rectangle.expand.vertical.svg") center/contain no-repeat;',
        ".browser-session-message-toggle[aria-expanded=\"true\"] .browser-session-message-toggle-icon {",
        'mask-image: url("/static/images/rectangle.compress.vertical.svg");',
    ):
        assert token in stylesheet


def test_browser_workspace_prefers_simplified_chinese_font_fallbacks() -> None:
    """Keep Local resources text rendered with Simplified Chinese glyph forms."""
    stylesheet = _stylesheet()
    workspace_start = stylesheet.index(".browser-workspace {")
    workspace_rule = stylesheet[workspace_start:stylesheet.index("\n}", workspace_start)]
    font_family = 'font-family: var(--font-family-brand), var(--font-family-cjk), "Helvetica Neue", Helvetica, Arial, "PingFang HK", "PingFang TC", "Microsoft JhengHei", sans-serif;'

    assert font_family in workspace_rule
    cjk_token_start = stylesheet.index('--font-family-cjk:')
    cjk_token = stylesheet[cjk_token_start:stylesheet.index("\n", cjk_token_start)]
    assert cjk_token.index('"PingFang SC"') < cjk_token.index('"Microsoft YaHei"')
    assert cjk_token.index('"Microsoft YaHei"') < cjk_token.index('"Noto Sans CJK SC"')


def test_browser_workspace_reuses_the_shared_title_rail_and_content_card() -> None:
    """Keep Local resources on the same title-rail/content-card workspace contract."""
    stylesheet = _stylesheet()
    summary_start = stylesheet.index(".browser-summary-card {")
    summary_rule = stylesheet[summary_start:stylesheet.index("\n}", summary_start)]
    content_start = stylesheet.index(".browser-content-card {")
    content_rule = stylesheet[content_start:stylesheet.index("\n}", content_start)]
    text_card_start = stylesheet.index(".browser-text-summary-card {")
    text_card_rule = stylesheet[text_card_start:stylesheet.index("\n}", text_card_start)]

    assert "display: flex;" in summary_rule
    assert "flex: 0 0 auto;" in summary_rule
    assert "width: min(100%, var(--layout-content-width));" in summary_rule
    assert "max-width: var(--layout-content-width);" in summary_rule
    assert "min-height: calc(var(--workspace-title-rail-control-height) + var(--workspace-article-pad-block-start));" in summary_rule
    assert "padding: var(--workspace-article-pad-block-start) var(--workspace-article-pad-inline) 0;" in summary_rule
    assert "display: flex;" in content_rule
    assert "flex: 1 1 0;" in content_rule
    assert "padding: 0 var(--workspace-article-pad-inline) var(--sidebar-dock-bottom-gap);" in content_rule
    assert "overflow: visible;" in text_card_rule
    assert ".workspace > .workspace-header:first-child > .browser-summary-card {" in stylesheet
    assert ".workspace > .workspace-header:first-child > .workspace-article-card:not(.cache-overview-title-card):not(.browser-summary-card) {" in stylesheet
    assert "html.sidebar-memory-collapsed .app-shell .workspace > .workspace-header:first-child > .workspace-summary-card:first-child:not(.cache-overview-title-card) {" in stylesheet
    assert "padding-inline-start: var(--workspace-title-rail-collapsed-pad-inline-start);" in stylesheet
    assert ".browser-workspace-header {" in stylesheet
    assert "grid-template-rows: auto minmax(0, 1fr);" in stylesheet
    assert ".browser-text-summary-card .browser-session-table-shell {" in stylesheet
    assert ".browser-text-summary-card .browser-session-table-scroll {" in stylesheet


def test_global_quick_actions_reuse_the_sibling_shell_positioning_contract() -> None:
    """Keep the theme action on the same top/right contract as the sibling shell."""
    stylesheet = _stylesheet()
    token_start = stylesheet.index("--global-quick-actions-right:")
    token_line = stylesheet[token_start:stylesheet.index("\n", token_start)]
    action_start = stylesheet.index(".global-quick-actions {")
    action_rule = stylesheet[action_start:stylesheet.index("\n}", action_start)]

    assert token_line == "--global-quick-actions-right: var(--layout-global-anchor-right);"
    assert "top: var(--global-quick-actions-top);" in action_rule
    assert "right: var(--global-quick-actions-right);" in action_rule

    touch_contract_start = stylesheet.index("/* Keep the touch target stationary")
    narrow_start = stylesheet.rfind(
        "@media (max-width: 560px) {",
        0,
        touch_contract_start,
    )
    narrow_end = touch_contract_start
    narrow_rule = stylesheet[narrow_start:narrow_end]
    for token in (
        "--sidebar-toggle-x: calc(",
        "var(--layout-sidebar-overlay-inline-size)",
        "var(--sidebar-overlay-inset-left)",
        "var(--settings-round-icon-button-size)",
    ):
        assert token in narrow_rule


def test_browser_picker_arrow_matches_the_shared_select_arrow() -> None:
    """Keep every picker arrow painted with its trigger text color."""
    stylesheet = _stylesheet()
    arrow_start = stylesheet.index("\n.browser-picker-trigger-chevron {") + 1
    arrow_rule = stylesheet[arrow_start:stylesheet.index("\n}", arrow_start)]

    for token in (
        "width: 12px;",
        "height: 8px;",
        "color: var(--text);",
        "font-size: 0;",
        "background-color: currentColor;",
        "mask: var(--browser-picker-chevron-image) center / 12px 8px no-repeat;",
        "-webkit-mask: var(--browser-picker-chevron-image) center / 12px 8px no-repeat;",
    ):
        assert token in arrow_rule

    select_start = stylesheet.index(".browser-filter-form select.form-select {")
    select_rule = stylesheet[select_start:stylesheet.index("\n}", select_start)]
    for token in (
        "appearance: none;",
        "-webkit-appearance: none;",
        "padding-inline-end: 34px;",
        "background-image: none;",
    ):
        assert token in select_rule

    native_fallback_start = stylesheet.index(
        ".browser-filter-field:has(> select.form-select)::after {"
    )
    native_fallback_rule = stylesheet[
        native_fallback_start:stylesheet.index("\n}", native_fallback_start)
    ]
    for token in (
        "right: 10px;",
        "bottom: calc(var(--control-form-height) / 2 + 4px);",
        "background-color: var(--text);",
        "mask: var(--browser-picker-chevron-image) center / 12px 8px no-repeat;",
        "-webkit-mask: var(--browser-picker-chevron-image) center / 12px 8px no-repeat;",
    ):
        assert token in native_fallback_rule

    assert "fill='currentColor'" in stylesheet
    assert 'content: "\\25BE";' not in stylesheet


def test_browser_picker_icon_shells_are_perfect_circles() -> None:
    """Keep every selected picker icon background circular at each supported size."""
    stylesheet = _stylesheet()
    selectors = (
        ".browser-picker-selected-icon-shell {",
        ".cache-source-switcher-trigger .browser-picker-selected-icon-shell,\n"
        ".cache-browser-session-trigger .browser-picker-selected-icon-shell {",
        ".agent-os-combobox .browser-picker-selected-icon-shell {",
        ".browser-source-filter-trigger .browser-picker-selected-icon-shell {",
    )
    for selector in selectors:
        selector_start = stylesheet.index(selector)
        selector_rule = stylesheet[selector_start:stylesheet.index("\n}", selector_start)]
        assert "border-radius: var(--radius-pill);" in selector_rule


def test_browser_content_mode_reuses_the_sibling_optimistic_navigation_skeleton() -> None:
    """Keep content-mode navigation on the sibling's skeleton and motion contract."""
    stylesheet = _stylesheet()

    for token in (
        ".navigation-skeleton-root {",
        ".navigation-skeleton-card,",
        ".navigation-skeleton-line {",
        "animation: browser-navigation-skeleton-sheen 1.15s var(--motion-shimmer) infinite;",
        ".browser-navigation-skeleton-root {",
        ".browser-navigation-skeleton-heading {",
        ".browser-navigation-skeleton-metrics {",
        ".browser-navigation-skeleton-results {",
        "@keyframes browser-navigation-skeleton-sheen {",
        "@media (prefers-reduced-motion: reduce) {",
    ):
        assert token in stylesheet

def test_agent_workspace_reuses_shared_glass_and_responsive_tokens() -> None:
    """Keep the fourth dock item and Agent workspace aligned with the shared shell."""
    stylesheet = _stylesheet()

    for token in (
        "/* Code version: v2.96.2-codex.1 */",
        "transform var(--sidebar-motion-duration) var(--motion-emphasized);",
        ".dock-icon-agent",
        'mask: url("/static/images/arrow.uturn.up.circle.svg")',
        '.settings-category-nav-item-browser {\n    --settings-category-icon-url: url("/static/images/safari.svg");',
        '.settings-nav-item-browser { --settings-category-icon-url: url("/static/images/safari.svg"); }',
        '.settings-category-nav-item-llm {\n    --settings-category-icon-url: url("/static/images/wand.and.sparkles.inverse.svg");',
        '.settings-nav-item-llm { --settings-category-icon-url: url("/static/images/wand.and.sparkles.inverse.svg"); }',
        '.settings-category-nav-item-agent {\n    --settings-category-icon-url: url("/static/images/arrow.uturn.up.circle.svg");',
        '.settings-nav-item-agent { --settings-category-icon-url: url("/static/images/arrow.uturn.up.circle.svg"); }',
        "width: 22px;",
        "height: 22px;",
        "/* Browser-mediated Computer Use Agent. */",
        ".agent-connect-fields {",
        "grid-template-columns: minmax(0, 1fr);",
        "gap: 14px;",
        ".agent-platform-combobox .browser-session-trigger-leading {",
        "flex: 1 1 auto;",
        ".agent-response-question-header.browser-session-table-message-shell {",
        ".agent-response-question {",
        "font-size: 17px;",
        ".agent-response-answer {",
        "font-size: var(--font-size-5);",
        ".agent-response-answer-content {",
        ".agent-response-pagination {",
        ".agent-platform-combobox .trade-strategy-trigger-label.browser-session-trigger-label {",
        ".agent-platform-combobox .browser-picker-selected-icon-shell {",
        ".agent-os-combobox .browser-picker-selected-icon-shell {",
        ".agent-os-combobox .browser-picker-option-icon {",
        ".agent-os-combobox .agent-combobox-option {",
        ".agent-combobox-loading-spinner {",
        ".agent-response-status {",
        ".agent-response-status-indicator {",
        ".agent-response-status-dot {",
        ".agent-response-status-spinner {",
        ".agent-conversation-link.is-traditional-handoff {",
        ".agent-conversation-link-label {",
        'background-image: url("/static/images/browser.edge.png");',
        ".agent-combobox-dropdown .trade-strategy-dropdown-option,",
        "font-weight: var(--font-weight-regular);",
        ".browser-session-status-item {",
        ".browser-session-status-terminal-checkmark {",
        ".browser-session-status-card-compact .agent-terminal-execution-status {",
        ".agent-combobox.is-agent-combobox-open .agent-combobox-dropdown:not([hidden]) {",
        ".agent-port-field {",
        "grid-column: auto;",
        ".settings-category-nav-item-agent {",
        "--settings-category-active-index: 5;",
        ".agent-workspace-grid {",
        "grid-template-columns: minmax(0, 1fr);",
        "grid-template-rows: minmax(0, 1fr);",
        ".agent-summary-card {",
        "box-sizing: border-box;",
        "padding: var(--workspace-title-rail-pad-block-start)",
        ".report-card.agent-summary-card {",
        "border-radius: 0;",
        "background: transparent;",
        "box-shadow: none;",
        "backdrop-filter: none;",
        "-webkit-backdrop-filter: none;",
        ".agent-summary-card > .report-heading-row {",
        ".agent-summary-card > .report-heading-row > div {",
        "align-items: center;",
        ".workspace > .workspace-header:first-child > .workspace-article-card:not(.cache-overview-title-card).agent-summary-card {",
        "padding-top: var(--workspace-title-rail-pad-block-start);",
        ".agent-task-card {",
        "display: flex;",
        ".agent-task-card > .agent-response-card,",
        "min-height: 0;",
        ".agent-composer-shell:focus-within {",
        ".agent-composer-submit-icon {",
        ".agent-composer-submit.is-stop .agent-composer-submit-icon {",
        ".agent-composer-submit.is-resume .agent-composer-submit-icon {",
        'mask-image: url("/static/images/stop.fill.svg");',
        "border-radius: var(--radius-soft);",
        ".agent-activity-panel {",
        ".agent-activity-list {",
        "overflow-y: auto;",
        ".agent-activity-item[data-status=\"completed\"] .agent-activity-status {",
        ".settings-agent-runtime-status {",
        ".agent-response-output {",
        "overflow-y: auto;",
            '"PingFang SC", "PingFang TC", "PingFang HK", "Microsoft YaHei", "Microsoft JhengHei"',
        ".agent-response-output h1,",
        "font-size: var(--font-card-title);",
        ".settings-agent-system-prompt {",
        ".settings-agent-limit-grid {",
        "@media (max-width: 1100px) {",
    ):
        assert token in stylesheet


def test_agent_composer_right_aligns_all_footer_controls_with_action_gap() -> None:
    """Keep every Composer footer control anchored to the shell's right edge."""
    stylesheet = _stylesheet()
    footer_start = stylesheet.rfind(".agent-composer-footer {")
    footer_rule = stylesheet[footer_start:stylesheet.index("\n}", footer_start)]

    assert "justify-content: flex-end;" in footer_rule
    assert "gap: 12px;" in footer_rule


def test_agent_combobox_trigger_labels_share_typography_contract() -> None:
    """Keep every Agent trigger label on the 15px UI typography contract."""
    stylesheet = _stylesheet()
    selector = ".agent-combobox-trigger .trade-strategy-trigger-label {"
    selector_start = stylesheet.index(selector)
    selector_rule = stylesheet[selector_start:stylesheet.index("\n}", selector_start)]

    for token in (
        "font-family: inherit;",
        "font-size: var(--font-ui-lg);",
        "font-weight: var(--font-weight-regular);",
        "line-height: 1.45;",
    ):
        assert token in selector_rule

    model_selector = ".agent-model-trigger,\n.agent-model-trigger .trade-strategy-trigger-label {"
    model_start = stylesheet.index(model_selector)
    model_rule = stylesheet[model_start:stylesheet.index("\n}", model_start)]
    assert "font-weight: var(--font-weight-regular);" in model_rule


def test_agent_model_trigger_uses_fifteen_pixel_type_token() -> None:
    """Keep the selected Agent model label at the requested 15px size."""
    stylesheet = _stylesheet()
    selector = ".agent-model-trigger .trade-strategy-trigger-label {"
    selector_start = stylesheet.index(selector)
    selector_rule = stylesheet[selector_start:stylesheet.index("\n}", selector_start)]

    assert "font-size: var(--font-size-5);" in selector_rule


def test_agent_composer_triggers_reserve_a_protected_chevron_column() -> None:
    """Keep model and thinking-effort labels separated from their chevrons."""
    stylesheet = _stylesheet()
    selector = ".agent-model-trigger,\n.agent-effort-trigger {"
    selector_start = stylesheet.index(selector)
    selector_rule = stylesheet[selector_start:stylesheet.index("\n}", selector_start)]

    for token in (
        "display: grid;",
        "grid-template-columns: minmax(0, 1fr) 12px;",
        "column-gap: 8px;",
        "align-items: center;",
    ):
        assert token in selector_rule

    chevron_selector = (
        ".agent-model-trigger .browser-picker-trigger-chevron,\n"
        ".agent-effort-trigger .browser-picker-trigger-chevron {"
    )
    chevron_start = stylesheet.index(chevron_selector)
    chevron_rule = stylesheet[chevron_start:stylesheet.index("\n}", chevron_start)]
    assert "justify-self: end;" in chevron_rule


def test_agent_effort_trigger_uses_fifteen_pixel_type_token_without_wrapping() -> None:
    """Keep the visible ChatGPT effort label readable at every viewport width."""
    stylesheet = _stylesheet()
    selector = ".agent-effort-trigger .trade-strategy-trigger-label {"
    selector_start = stylesheet.index(selector)
    selector_rule = stylesheet[selector_start:stylesheet.index("\n}", selector_start)]

    assert "font-size: var(--font-size-5);" in selector_rule
    assert "padding-inline-end: 6px;" in selector_rule
    assert "white-space: nowrap;" in selector_rule






def test_visible_chatgpt_effort_uses_a_two_row_compact_composer_footer() -> None:
    """Keep the 15px effort control and submit action inside narrow ChatGPT composers."""
    stylesheet = _stylesheet()
    selector = ".agent-composer-footer:has(.agent-effort-controls:not([hidden])) {"
    selector_start = stylesheet.index(selector)
    selector_rule = stylesheet[selector_start:stylesheet.index("\n}", selector_start)]

    for token in (
        "display: grid;",
        "grid-template-columns: minmax(0, 1fr) auto;",
        "gap: 8px 12px;",
    ):
        assert token in selector_rule


def test_agent_prompt_uses_the_dedicated_sixteen_pixel_type_token() -> None:
    """Keep the Agent question field at the requested 16px size."""
    stylesheet = _stylesheet()

    assert "--font-agent-prompt: 16px;" in stylesheet
    prompt_start = stylesheet.index(".agent-prompt-input {")
    prompt_rule = stylesheet[prompt_start:stylesheet.index("\n}", prompt_start)]
    assert "font-size: var(--font-agent-prompt);" in prompt_rule


def test_agent_composer_uses_a_two_line_regular_weight_prompt_with_a_standard_toggle() -> None:
    """Keep the Agent question field compact until its shared control expands it."""
    stylesheet = _stylesheet()
    prompt_start = stylesheet.index(".agent-prompt-input {")
    prompt_rule = stylesheet[prompt_start:stylesheet.index("\n}", prompt_start)]
    toggle_start = stylesheet.index(
        ".browser-session-message-toggle.agent-composer-overflow-toggle {"
    )
    toggle_rule = stylesheet[toggle_start:stylesheet.index("\n}", toggle_start)]
    pagination_start = stylesheet.index(".browser-pagination.agent-response-pagination {")
    pagination_rule = stylesheet[pagination_start:stylesheet.index("\n}", pagination_start)]

    assert "--font-weight-regular: 400;" in stylesheet
    for token in (
        "min-height: 0;",
        "max-height: min(360px, 45svh);",
        "resize: none;",
        "font-weight: var(--font-weight-regular);",
    ):
        assert token in prompt_rule
    for token in (
        "position: absolute;",
        "top: 12px;",
        "right: 12px;",
        "z-index: var(--layer-control-affordance);",
        "margin: 0;",
    ):
        assert token in toggle_rule
    for token in (
        "left: 50%;",
        "padding: 4px;",
        "border: var(--frosted-glass-border);",
        "background: var(--frosted-glass-background);",
        "box-shadow: var(--frosted-glass-shadow);",
        "backdrop-filter: var(--frosted-glass-blur);",
    ):
        assert token in pagination_rule


def test_agent_composer_uses_one_frosted_surface_and_an_invisible_vertical_scrollbar() -> None:
    """Keep the Composer shell frosted while the prompt area stays immersive and scrollable."""
    stylesheet = _stylesheet()

    shell_start = stylesheet.index(".agent-composer-shell {")
    shell_rule = stylesheet[shell_start:stylesheet.index("\n}", shell_start)]
    prompt_start = stylesheet.index(".agent-prompt-input {")
    prompt_rule = stylesheet[prompt_start:stylesheet.index("\n}", prompt_start)]
    input_state_selector = ".agent-composer-shell .agent-prompt-input,"
    input_state_start = stylesheet.index(input_state_selector)
    input_state_rule = stylesheet[
        input_state_start:stylesheet.index("\n}", input_state_start)
    ]
    scrollbar_selector = ".agent-composer-shell .agent-prompt-input::-webkit-scrollbar {"
    scrollbar_start = stylesheet.index(scrollbar_selector)
    scrollbar_rule = stylesheet[scrollbar_start:stylesheet.index("\n}", scrollbar_start)]

    for token in (
        "position: relative;",
        "border: var(--frosted-glass-border);",
        "background: var(--frosted-glass-background);",
        "box-shadow: var(--frosted-glass-shadow);",
        "backdrop-filter: var(--frosted-glass-blur);",
        "-webkit-backdrop-filter: var(--frosted-glass-blur);",
    ):
        assert token in shell_rule
    for token in (
        "border: 0;",
        "background: transparent;",
        "overflow-y: auto;",
        "scrollbar-width: none;",
        "-ms-overflow-style: none;",
    ):
        assert token in prompt_rule
    for token in (
        "border: 0;",
        "background: transparent;",
        "box-shadow: none;",
        "outline: none;",
    ):
        assert token in input_state_rule
    assert "var(--frosted-glass" not in prompt_rule
    assert "width: 0;" in scrollbar_rule
    assert "height: 0;" in scrollbar_rule


def test_agent_summary_rail_does_not_retain_redundant_readiness_copy() -> None:
    """Keep the Agent title rail focused on its heading and remove stale status CSS."""
    stylesheet = _stylesheet()

    assert ".agent-summary-card > .agent-readiness" not in stylesheet
    assert ".agent-readiness-dot" not in stylesheet


def test_agent_response_toolbar_owns_the_compact_lifecycle_status() -> None:
    """Keep Agent lifecycle feedback in the response toolbar beside the handoff action."""
    stylesheet = _stylesheet()
    toolbar_start = stylesheet.index(".agent-response-toolbar {\n    justify-content: space-between;")
    toolbar_rule = stylesheet[toolbar_start:stylesheet.index("\n}", toolbar_start)]
    status_start = stylesheet.index(".agent-response-status {\n")
    status_rule = stylesheet[status_start:stylesheet.index("\n}", status_start)]

    assert "gap: 12px;" in toolbar_rule
    assert "min-width: 0;" in status_rule
    assert "margin: 0;" in status_rule
    assert "flex: 1 1 auto;" in status_rule
    assert "align-items: flex-start;" in status_rule

    status_copy_start = stylesheet.index(".agent-response-status-copy {")
    status_copy_rule = stylesheet[status_copy_start:stylesheet.index("\n}", status_copy_start)]
    for token in (
        "-webkit-line-clamp: 2;",
        "overflow-wrap: anywhere;",
        "word-break: break-word;",
        "white-space: normal;",
    ):
        assert token in status_copy_rule

    detail_start = stylesheet.index(".agent-activity-detail {")
    detail_rule = stylesheet[detail_start:stylesheet.index("\n}", detail_start)]
    assert "overflow-wrap: anywhere;" in detail_rule
    assert "white-space: normal;" in detail_rule

    meta_start = stylesheet.index(".agent-activity-meta {")
    meta_rule = stylesheet[meta_start:stylesheet.index("\n}", meta_start)]
    assert "white-space: nowrap;" in meta_rule


def test_agent_composer_triggers_use_the_compact_shared_height_token() -> None:
    """Keep the model and effort controls aligned to the compact 32px rail."""
    stylesheet = _stylesheet()
    selector = ".agent-model-trigger,\n.agent-effort-trigger {"
    selector_start = stylesheet.index(selector)
    selector_rule = stylesheet[selector_start:stylesheet.index("\n}", selector_start)]

    assert "--agent-composer-control-height: 32px;" in stylesheet
    for token in (
        "height: var(--agent-composer-control-height);",
        "min-height: var(--agent-composer-control-height);",
        "padding: 4px 10px;",
    ):
        assert token in selector_rule


def test_agent_composer_dropdowns_open_above_the_bottom_composer() -> None:
    """Keep Agent model and effort menus inside the viewport above the Composer."""
    stylesheet = _stylesheet()
    selector = ".agent-model-dropdown,\n.agent-effort-dropdown {"
    selector_start = stylesheet.index(selector)
    selector_rule = stylesheet[selector_start:stylesheet.index("\n}", selector_start)]

    for token in (
        "top: auto;",
        "bottom: calc(100% + 4px);",
        "left: auto;",
        "right: 0;",
    ):
        assert token in selector_rule


def test_agent_effort_dropdown_keeps_full_labels_inside_the_viewport() -> None:
    """Size live effort menus to their labels while keeping them inside the viewport."""
    stylesheet = _stylesheet()
    selector = ".agent-model-dropdown,\n.agent-effort-dropdown {"
    selector_start = stylesheet.index(selector)
    selector_rule = stylesheet[selector_start:stylesheet.index("\n}", selector_start)]
    text_start = stylesheet.index(
        ".agent-model-dropdown .trade-strategy-dropdown-text,\n.agent-effort-dropdown .trade-strategy-dropdown-text {"
    )
    text_rule = stylesheet[text_start:stylesheet.index("\n}", text_start)]

    assert "width: max-content;" in selector_rule
    assert "max-width: min(360px, calc(100vw - 20px));" in selector_rule
    assert "box-sizing: border-box;" in selector_rule
    assert "white-space: normal;" in text_rule
    assert "overflow-wrap: anywhere;" in text_rule


def test_agent_session_list_triggers_use_the_requested_36px_height() -> None:
    """Keep the Recent projects and Project session controls compact and aligned."""
    stylesheet = _stylesheet()
    selector = ".agent-session-list-combobox .agent-combobox-trigger {"
    selector_start = stylesheet.index(selector)
    selector_rule = stylesheet[selector_start:stylesheet.index("\n}", selector_start)]

    assert "height: var(--control-form-height);" in selector_rule
    assert "min-height: var(--control-form-height);" in selector_rule


def test_agent_doctor_actions_stack_vertically() -> None:
    """Keep recovery actions in one column while allowing their shadows to escape."""
    stylesheet = _stylesheet()
    selector_start = stylesheet.index(".agent-doctor-actions {")
    selector_rule = stylesheet[selector_start:stylesheet.index("\n}", selector_start)]

    assert "display: grid;" in selector_rule
    assert "grid-template-columns: minmax(0, 1fr);" in selector_rule
    assert "justify-items: start;" in selector_rule
    assert ".agent-doctor-action {\n    width: 100%;" not in stylesheet
    assert ".agent-doctor-action {\n    min-height: 36px;" not in stylesheet


def test_agent_session_lists_open_above_the_sidebar_trigger() -> None:
    """Keep long session menus inside the sidebar viewport near its bottom edge."""
    stylesheet = _stylesheet()
    selector = ".agent-session-list-combobox .agent-session-list-menu {"
    selector_start = stylesheet.index(selector)
    selector_rule = stylesheet[selector_start:stylesheet.index("\n}", selector_start)]

    assert "top: auto;" in selector_rule
    assert "bottom: calc(100% + 4px);" in selector_rule


def test_agent_recent_session_list_is_inline_and_scrollable() -> None:
    """Render Recent sessions directly while keeping a bounded sidebar scrollport."""
    stylesheet = _stylesheet()
    selector = ".agent-session-list-menu-direct {"
    selector_start = stylesheet.index(selector)
    selector_rule = stylesheet[selector_start:stylesheet.index("\n}", selector_start)]

    assert "position: static;" in selector_rule
    assert "display: grid !important;" in selector_rule
    assert "--agent-session-list-dock-gap: var(--sidebar-dock-bottom-gap);" in selector_rule
    assert "--agent-session-list-menu-min-height: var(--control-compact-height);" in selector_rule
    assert "--agent-session-list-menu-available-height: var(--agent-session-list-menu-min-height);" in selector_rule
    assert "max-height: min(" in selector_rule
    assert "overflow-y: auto;" in selector_rule
    assert "scrollbar-width: none;" in selector_rule
    assert "scrollbar-gutter: auto;" in selector_rule
    assert "-ms-overflow-style: none;" in selector_rule

    scrollbar_start = stylesheet.index(
        ".agent-session-list-menu-direct::-webkit-scrollbar {",
    )
    scrollbar_rule = stylesheet[scrollbar_start:stylesheet.index("\n}", scrollbar_start)]
    assert "width: 0;" in scrollbar_rule
    assert "height: 0;" in scrollbar_rule

    track_start = stylesheet.index(
        ".agent-session-list-menu-direct::-webkit-scrollbar-track,",
    )
    track_rule = stylesheet[track_start:stylesheet.index("\n}", track_start)]
    assert "background: transparent;" in track_rule


def test_agent_session_source_raises_above_the_inline_recent_session_list_when_open() -> None:
    """Keep the source menu hit-testable while the selected recent list remains inline."""
    stylesheet = _stylesheet()
    selector = ".agent-session-mode-combobox {"
    selector_start = stylesheet.index(selector)
    selector_rule = stylesheet[selector_start:stylesheet.index("\n}", selector_start)]
    open_selector = ".agent-session-mode-combobox.is-agent-combobox-open {"
    open_start = stylesheet.index(open_selector)
    open_rule = stylesheet[open_start:stylesheet.index("\n}", open_start)]

    assert "z-index: var(--layer-surface-content);" in selector_rule
    assert "z-index: calc(var(--layer-global-popover) + 1);" in open_rule
    upward_start = stylesheet.index(
        ".agent-session-source:has(> .agent-session-detail-field:not([hidden]))\n"
        "    .agent-session-mode-combobox.is-agent-combobox-open",
    )
    upward_rule = stylesheet[upward_start:stylesheet.index("\n}", upward_start)]
    assert "top: auto;" in upward_rule
    assert "bottom: calc(100% + 4px);" in upward_rule


def test_browser_session_status_labels_share_nonbold_left_typography() -> None:
    """Keep account, message, and terminal status text on one non-bold typography contract."""
    stylesheet = _stylesheet()
    selector = (
        ".browser-session-status-account,\n"
        ".browser-session-status-message[data-role=\"browser-session-message\"],\n"
        ".agent-terminal-execution-label {"
    )
    selector_start = stylesheet.index(selector)
    selector_rule = stylesheet[selector_start:stylesheet.index("\n}", selector_start)]

    for token in (
        "font-family: inherit;",
        "font-size: var(--font-ui-lg);",
        "font-weight: var(--font-weight-regular);",
        "line-height: 1.3;",
        "text-align: left;",
    ):
        assert token in selector_rule


def test_browser_session_status_uses_the_same_leading_slot_for_loading_and_failure() -> None:
    """Keep loading and failure indicators aligned with the terminal permission label."""
    stylesheet = _stylesheet()

    spinner_start = stylesheet.index(".browser-session-status-spinner {")
    spinner_rule = stylesheet[spinner_start:stylesheet.index("\n}", spinner_start)]
    assert "--loading-spinner-size: var(--browser-session-status-checkmark-size);" in spinner_rule

    error_start = stylesheet.index('.browser-session-status-checkmark[data-status-state="error"] {')
    error_rule = stylesheet[error_start:stylesheet.index("\n}", error_start)]
    assert "background-color: var(--theme-error);" in error_rule
    assert 'mask: url("/static/images/xmark.circle.fill.svg") center/contain no-repeat;' in error_rule
    assert '-webkit-mask: url("/static/images/xmark.circle.fill.svg") center/contain no-repeat;' in error_rule

    directory_error_start = stylesheet.index(".settings-directory-status.field-status--error::before {")
    directory_error_rule = stylesheet[
        directory_error_start:stylesheet.index("\n}", directory_error_start)
    ]
    assert 'mask: url("/static/images/xmark.circle.fill.svg") center/contain no-repeat;' in directory_error_rule
    assert '-webkit-mask: url("/static/images/xmark.circle.fill.svg") center/contain no-repeat;' in directory_error_rule


def test_agent_compact_status_card_has_no_border() -> None:
    """Keep the compact Agent status card borderless while preserving its surface."""
    stylesheet = _stylesheet()

    card_start = stylesheet.index(".browser-session-status-card-compact {")
    card_rule = stylesheet[card_start:stylesheet.index("\n}", card_start)]
    assert "border-width: 0;" in card_rule


def test_cache_browser_session_status_card_has_no_border() -> None:
    """Keep the Cache browser-session status card borderless without changing its surface."""
    stylesheet = _stylesheet()

    card_start = stylesheet.index(".browser-session-panel .browser-session-status-card {")
    card_rule = stylesheet[card_start:stylesheet.index("\n}", card_start)]
    assert "border-width: 0;" in card_rule


def test_browser_session_message_time_uses_explicit_two_line_layout() -> None:
    """Keep detail-table message timestamps split into date and clock rows at every width."""
    stylesheet = _stylesheet()

    time_start = stylesheet.index(
        ".browser-session-detail-table time.browser-session-message-time {"
    )
    time_rule = stylesheet[time_start:stylesheet.index("\n}", time_start)]
    span_start = stylesheet.index(".browser-session-message-time > span {")
    span_rule = stylesheet[span_start:stylesheet.index("\n}", span_start)]

    for token in ("display: inline-grid;", "gap: 2px;", "white-space: normal;"):
        assert token in time_rule
    for token in ("display: block;", "white-space: nowrap;"):
        assert token in span_rule


def test_agent_runtime_labels_use_the_sidebar_label_type_contract() -> None:
    """Keep Agent runtime labels at 15px with medium emphasis only where requested."""
    stylesheet = _stylesheet()

    label_start = stylesheet.index(".agent-runtime-form .field > .field-label {")
    label_rule = stylesheet[label_start:stylesheet.index("\n}", label_start)]
    assert "font-size: var(--font-ui-lg);" in label_rule

    project_selector = ".agent-runtime-form > label.field > .field-label {"
    project_start = stylesheet.index(project_selector)
    project_rule = stylesheet[project_start:stylesheet.index("\n}", project_start)]
    assert "font-weight: var(--font-weight-medium);" in project_rule

    browser_selector = ".agent-runtime-form .agent-connect-fields > .field:nth-child(2) > .field-label {"
    browser_start = stylesheet.index(browser_selector)
    browser_rule = stylesheet[browser_start:stylesheet.index("\n}", browser_start)]
    assert "font-weight: var(--font-weight-regular);" in browser_rule
    assert "font-weight: var(--font-weight-medium);" not in browser_rule


def test_path_inputs_prefer_trailing_directories_when_narrow() -> None:
    """Keep the useful end of every long path visible in its input."""
    stylesheet = _stylesheet()
    selector_start = stylesheet.index(".path-display-input {")
    selector_rule = stylesheet[selector_start:stylesheet.index("\n}", selector_start)]

    assert "direction: rtl;" in selector_rule
    assert "text-align: left;" in selector_rule
    assert "text-overflow: ellipsis;" in selector_rule


def test_agent_response_pagination_keeps_spatial_effects_unclipped() -> None:
    """Keep the answer scrollable while allowing pagination motion to escape its shell."""
    stylesheet = _stylesheet()

    for selector in (".agent-workspace-grid {", ".agent-task-card {", "\n.agent-response-card {", ".agent-response-output {"):
        selector_start = stylesheet.index(selector) + (1 if selector.startswith("\n") else 0)
        selector_rule = stylesheet[selector_start:stylesheet.index("\n}", selector_start)]
        assert "overflow: visible;" in selector_rule

    pagination_start = stylesheet.index(".agent-response-pagination {")
    pagination_rule = stylesheet[pagination_start:stylesheet.index("\n}", pagination_start)]
    assert "position: absolute;" in pagination_rule
    assert "z-index: var(--layer-control-affordance);" in pagination_rule
    assert "overflow: visible;" in pagination_rule

    answer_start = stylesheet.index(".agent-response-answer {")
    answer_rule = stylesheet[answer_start:stylesheet.index("\n}", answer_start)]
    assert "overflow-x: hidden;" in answer_rule
    assert "overflow-y: auto;" in answer_rule


def test_agent_doctor_actions_keep_spatial_effects_unclipped() -> None:
    """Keep Doctor recovery action effects outside the rounded panel boundary."""
    stylesheet = _stylesheet()

    panel_start = stylesheet.index(".agent-doctor-panel {")
    panel_rule = stylesheet[panel_start:stylesheet.index("\n}", panel_start)]
    events_start = stylesheet.index(".agent-doctor-events {")
    events_rule = stylesheet[events_start:stylesheet.index("\n}", events_start)]

    assert "overflow: visible;" in panel_rule
    assert "overflow-y: auto;" in events_rule


def test_agent_response_copy_uses_the_global_action_rail_without_consuming_scroll_space() -> None:
    """Keep the answer copy control aligned to the global rail and out of answer text flow."""
    stylesheet = _stylesheet()

    answer_start = stylesheet.index(".agent-response-answer {")
    answer_rule = stylesheet[answer_start:stylesheet.index("\n}", answer_start)]
    content_start = stylesheet.index(".agent-response-answer-content {")
    content_rule = stylesheet[content_start:stylesheet.index("\n}", content_start)]
    copy_start = stylesheet.index(".agent-response-copy {")
    copy_rule = stylesheet[copy_start:stylesheet.index("\n}", copy_start)]

    shell_start = stylesheet.index(".agent-response-answer-shell {")
    shell_rule = stylesheet[shell_start:stylesheet.index("\n}", shell_start)]
    assert "position: relative;" in shell_rule
    assert "margin-inline-end: calc(-1 * var(--agent-action-rail-bleed));" in shell_rule
    assert "overflow-x: hidden;" in answer_rule
    assert "overflow-y: auto;" in answer_rule
    assert "max-height:" not in answer_rule
    assert "var(--settings-round-icon-button-size) + var(--layout-edge-gap)" in answer_rule
    assert "display: flow-root;" in content_rule
    for declaration in (
        "position: absolute;",
        "top: 12px;",
        "right: 0;",
        "z-index: var(--layer-control-affordance);",
    ):
        assert declaration in copy_rule
    assert ".agent-response-copy-feedback {" in stylesheet
    assert ".agent-response-copy.is-copied .agent-response-copy-icon {" in stylesheet


def test_agent_response_actions_align_to_the_global_action_rail() -> None:
    """Keep the response action rows on the same horizontal rail as the theme control."""
    stylesheet = _stylesheet()

    toolbar_start = stylesheet.rindex(".agent-response-toolbar {")
    toolbar_rule = stylesheet[toolbar_start:stylesheet.index("\n}", toolbar_start)]
    question_header_start = stylesheet.index(".agent-response-question-header.browser-session-table-message-shell {")
    question_header_rule = stylesheet[
        question_header_start:stylesheet.index("\n}", question_header_start)
    ]
    rail_bleed = "margin-inline-end: calc(-1 * var(--agent-action-rail-bleed));"

    assert rail_bleed in toolbar_rule
    assert rail_bleed in question_header_rule


def test_agent_current_project_name_uses_requested_type_size() -> None:
    """Keep the combined Current project label readable and wrappable."""
    stylesheet = _stylesheet()
    selector = ".agent-runtime-form > label.field > .field-label.agent-project-label {"
    selector_start = stylesheet.index(selector)
    selector_rule = stylesheet[selector_start:stylesheet.index("\n}", selector_start)]

    assert "overflow-wrap: anywhere;" in selector_rule
    assert "white-space: normal;" in selector_rule
    name_selector = (
        ".agent-runtime-form > label.field > .field-label.agent-project-label "
        "[data-agent-project-name] {"
    )
    name_selector_start = stylesheet.index(name_selector)
    name_selector_rule = stylesheet[name_selector_start:stylesheet.index("\n}", name_selector_start)]
    assert "font-size: 17px;" in name_selector_rule


def test_agent_response_question_and_answer_use_requested_type_sizes() -> None:
    """Keep the Agent question heading at 17px and the answer container at 15px."""
    stylesheet = _stylesheet()

    question_start = stylesheet.index(".agent-response-question {")
    question_rule = stylesheet[question_start:stylesheet.index("\n}", question_start)]
    answer_start = stylesheet.index(".agent-response-answer {")
    answer_rule = stylesheet[answer_start:stylesheet.index("\n}", answer_start)]
    heading_start = stylesheet.index(".agent-response-output h1,")
    heading_rule = stylesheet[heading_start:stylesheet.index("\n}", heading_start) + 2]
    subheading_start = stylesheet.index(".agent-response-output h4,")
    subheading_rule = stylesheet[subheading_start:stylesheet.index("\n}", subheading_start)]

    assert "font-size: 17px;" in question_rule
    assert "font-weight: var(--font-weight-medium);" in question_rule
    assert "font-weight: var(--font-weight-semibold);" not in question_rule
    assert "font-size: var(--font-card-title);" not in question_rule
    assert "font-size: var(--font-size-5);" in answer_rule
    assert ".agent-response-output h3" not in heading_rule
    assert ".agent-response-output h3" not in subheading_rule


def test_agent_response_code_blocks_wrap_long_lines() -> None:
    """Keep long rendered Agent code blocks within the answer column."""
    stylesheet = _stylesheet()

    pre_start = stylesheet.index(".agent-response-answer-content pre {")
    pre_rule = stylesheet[pre_start:stylesheet.index("\n}", pre_start)]
    code_start = stylesheet.index(".agent-response-answer-content pre code {")
    code_rule = stylesheet[code_start:stylesheet.index("\n}", code_start)]

    for token in (
        "white-space: pre-wrap;",
        "overflow-wrap: anywhere;",
        "word-break: break-word;",
    ):
        assert token in pre_rule
    for token in (
        "white-space: inherit;",
        "overflow-wrap: inherit;",
        "word-break: inherit;",
    ):
        assert token in code_rule


def test_agent_response_header_and_answer_pin_the_composer() -> None:
    """Keep both response regions independently scrollable without moving the composer."""
    stylesheet = _stylesheet()

    toolbar_start = stylesheet.rfind(".agent-response-toolbar {")
    toolbar_rule = stylesheet[toolbar_start:stylesheet.index("\n}", toolbar_start)]
    header_start = stylesheet.index(".agent-response-question-header.browser-session-table-message-shell {")
    header_rule = stylesheet[header_start:stylesheet.index("\n}", header_start)]
    answer_start = stylesheet.index(".agent-response-answer {")
    answer_rule = stylesheet[answer_start:stylesheet.index("\n}", answer_start)]
    composer_start = stylesheet.rfind(".agent-task-card > .agent-prompt-form {")
    composer_rule = stylesheet[composer_start:stylesheet.index("\n}", composer_start)]

    scroll_start = stylesheet.index(".agent-response-question {")
    scroll_rule = stylesheet[scroll_start:stylesheet.index("\n}", scroll_start)]
    for rule in (scroll_rule, answer_rule):
        assert "overflow-y: auto;" in rule or "overflow: auto;" in rule
        assert "scrollbar-gutter: stable;" in rule
        assert "overscroll-behavior: contain;" in rule
    for declaration in (
        "box-sizing: border-box;",
        "flex: 0 0 auto;",
    ):
        assert declaration in toolbar_rule
    assert "box-sizing: border-box;" in header_rule
    assert "flex: 0 1 auto;" in header_rule
    assert "min-height: var(--settings-round-icon-button-size);" in toolbar_rule
    assert "min-height: calc(var(--settings-round-icon-button-size) + 12px);" in header_rule
    assert "overflow-anchor: none;" in answer_rule
    assert "position: relative;" in composer_rule
    assert "align-self: end;" in composer_rule
    assert "grid-area: 2 / 1;" in composer_rule


def test_agent_error_record_is_collapsible_and_vertically_scrollable() -> None:
    """Keep long Agent tracebacks inside a bounded accessible record."""
    stylesheet = _stylesheet()

    record_start = stylesheet.index(".agent-error-record {")
    record_rule = stylesheet[record_start:stylesheet.index("\n}", record_start)]
    scroll_start = stylesheet.index(".agent-error-record-scroll {")
    scroll_rule = stylesheet[scroll_start:stylesheet.index("\n}", scroll_start)]
    content_start = stylesheet.index(".agent-error-record-content {")
    content_rule = stylesheet[content_start:stylesheet.index("\n}", content_start)]

    assert "overflow: hidden;" in record_rule
    assert "max-height: min(22rem, 38svh);" in scroll_rule
    assert "overflow-y: auto;" in scroll_rule
    assert "overscroll-behavior: contain;" in scroll_rule
    assert "white-space: pre-wrap;" in content_rule
    assert "overflow-wrap: anywhere;" in content_rule


def test_agent_activity_expansion_uses_shared_gel_motion() -> None:
    """Keep Activity expansion on the shared soft-body motion contract."""
    stylesheet = _stylesheet()
    activity_rule_start = stylesheet.index(".agent-activity-panel[open] > .agent-activity-list {")
    activity_rule = stylesheet[activity_rule_start:stylesheet.index("\n}", activity_rule_start)]

    for token in (
        "animation: agent-activity-gel-open var(--motion-duration-emphasized) var(--motion-bouncy) both;",
        "transform-origin: top center;",
        "will-change: transform, opacity;",
    ):
        assert token in activity_rule

    keyframes_start = stylesheet.index("@keyframes agent-activity-gel-open {")
    keyframes = stylesheet[keyframes_start:stylesheet.index("\n}\n\n@media", keyframes_start)]
    for token in (
        "transform: translate3d(0, -6px, 0) scale3d(0.985, 0.94, 1);",
        "transform: translate3d(0, 2px, 0) scale3d(1.01, 1.015, 1);",
        "transform: translate3d(0, 0, 0) scale3d(1, 1, 1);",
        "filter: saturate(82%);",
        "filter: saturate(108%);",
    ):
        assert token in keyframes


def test_agent_sidebar_primary_comboboxes_use_the_compact_form_height() -> None:
    """Keep Web service and Session source controls on the shared 36px rail."""
    stylesheet = _stylesheet()
    selector = (
        ".agent-platform-combobox .agent-combobox-trigger,\n"
        ".agent-session-mode-combobox .agent-combobox-trigger {"
    )
    start = stylesheet.index(selector)
    rule = stylesheet[start:stylesheet.index("\n}", start)]

    for token in (
        "height: var(--control-form-height);",
        "min-height: var(--control-form-height);",
        "padding-block: 3px;",
    ):
        assert token in rule
