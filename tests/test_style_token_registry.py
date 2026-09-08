"""Regression tests for the Settings → Style tokens registry.

Code version: v1.6.0-codex.1
"""

import re
from pathlib import Path

from app.web.token_registry import (
    build_reused_style_token_groups,
    build_reused_style_token_rows,
    build_style_token_component_rows,
    load_css_token_registry,
)


def test_registry_reads_shared_tokens_from_all_root_layers() -> None:
    registry = load_css_token_registry()

    assert registry["--frosted-glass-background"].reference_count >= 2
    assert registry["--frosted-glass-background"].line > 0
    assert registry["--style-token-column-gap"].value == "24px"


def test_style_token_groups_only_expose_reused_tokens() -> None:
    groups = build_reused_style_token_groups(minimum_references=2)
    token_names = [token["name"] for group in groups for token in group["tokens"]]

    assert token_names
    assert len(token_names) == len(set(token_names))
    assert all(
        token["reference_count"] >= 2
        for group in groups
        for token in group["tokens"]
    )


def test_style_token_rows_pair_every_category_with_a_live_component_demo() -> None:
    rows = build_reused_style_token_rows(minimum_references=2)

    assert {row["sample_kind"] for row in rows} == {
        "metric-summary",
        "range-mode",
    }
    assert all(row["sample_kind"] != "token-inventory" for row in rows)
    assert [row["name"] for row in rows] == [
        "Foundation",
        "Controls",
    ]
    assert rows[-1]["sample_kind"] == "range-mode"


def test_style_token_component_rows_form_a_complete_sorted_component_catalog() -> None:
    rows = build_style_token_component_rows()

    expected_ids = {
        "agent-browser-selector",
        "circular-icon-button",
        "frosted-glass",
        "global-theme-toggle",
        "modal-dialog",
        "modal-dialog-banner-message",
        "pagination",
        "primary-button",
        "prompt-tag",
        "scrollable-data-table",
        "secondary-button",
        "segmented-control",
        "settings-action-package",
        "settings-execution-option",
        "shared-select-dropdown",
        "shared-select-filter",
        "strategy-tuning-control",
        "switch",
        "text-input-control",
        "tooltip",
        "workspace-metric-value",
    }
    assert len(rows) == 21
    assert all(row["id"] != "workspace-article" for row in rows)
    assert {row["id"] for row in rows} == expected_ids
    assert len({row["id"] for row in rows}) == len(rows)
    assert [row["name"] for row in rows] == sorted(
        (row["name"] for row in rows),
        key=str.casefold,
    )
    assert all(row["tokens"] for row in rows)
    rows_by_id = {row["id"]: row for row in rows}
    assert rows_by_id["agent-browser-selector"]["sample_copy"] == ""
    assert {token["name"] for token in rows_by_id["prompt-tag"]["tokens"]} == {
        "--accent-border-strong",
        "--accent-surface-soft",
        "--accent-text",
        "--radius-pill",
        "--font-ui-sm",
        "--font-weight-medium",
    }
    assert rows_by_id["prompt-tag"]["name"] == "Tag"
    for row_id in (
        "global-theme-toggle",
        "pagination",
        "primary-button",
        "prompt-tag",
        "scrollable-data-table",
        "secondary-button",
        "shared-select-filter",
    ):
        assert rows_by_id[row_id]["sample_copy"] == ""
    assert rows_by_id["secondary-button"]["use_icon"] is False
    assert "icon_class" not in rows_by_id["secondary-button"]
    assert "--scrollable-data-table-min-width" in {
        token["name"] for token in rows_by_id["scrollable-data-table"]["tokens"]
    }
    assert len(rows_by_id["shared-select-dropdown"]["sample_options"]) == 12
    assert rows_by_id["shared-select-dropdown"]["sample_options"][-1] == {
        "value": "max",
        "label": "Max",
    }
    assert rows_by_id["settings-action-package"]["related_styles"] == (
        {"name": "Settings execution option", "target_id": "settings-execution-option"},
    )
    assert {token["name"] for token in rows_by_id["settings-execution-option"]["tokens"]} == {
        "--settings-general-option-background",
        "--settings-general-option-border",
        "--settings-general-option-max-width",
        "--settings-general-option-pad-block",
        "--settings-general-option-pad-inline",
        "--settings-general-option-radius",
    }
    assert any(
        token.get("editable") and token.get("unit") == "px"
        for row in rows
        for token in row["tokens"]
    )
    core_material_cards = {
        "circular-icon-button",
        "modal-dialog",
        "modal-dialog-banner-message",
        "scrollable-data-table",
        "segmented-control",
        "settings-action-package",
        "shared-select-filter",
    }
    for row_id in core_material_cards:
        assert any(
            token.get("reference_target_id") == "frosted-glass"
            for token in rows_by_id[row_id]["tokens"]
        )
    assert not expected_ids.intersection(
        {
            "investment-holdings-allocation-badge",
            "portfolio-donut-orbit",
            "ticker-identity-row",
            "ticker-input-control",
            "trade-strategy-stepper",
        }
    )


def test_style_tokens_route_renders_live_demos_and_settings_navigation(client) -> None:
    response = client.get("/settings/style-tokens")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "data-style-token-reference-skin" not in html
    assert 'aria-current="page"' in html
    assert 'data-style-token-card="style-token-surfaces-and-effects"' not in html
    assert 'data-style-token-card="style-token-typography"' not in html
    assert 'data-style-token-copy="Typography"' not in html
    assert 'data-style-token-copy="Surfaces and effects"' not in html
    assert 'class="style-token-card style-token-card--demo-only"' not in html
    assert 'data-style-token-demo="type-specimen"' not in html
    assert 'aria-label="Frosted glass demo"' in html
    assert 'class="metric-card foundation-metric-card metric-card-accent style-token-metric-card-demo"' in html
    assert ">Total trades</span>" in html
    assert ">2</strong>" in html
    for dead_demo in (
        "status-states",
        "control-playground",
        "workflow-card",
        "product-summary",
    ):
        assert f'data-style-token-demo="{dead_demo}"' not in html
    assert 'class="range-mode-shell"' in html
    assert 'data-segmented-pill="measured"' in html
    assert 'class="range-mode-option"' in html
    assert 'data-style-token-card="style-token-color-and-status"' not in html
    assert 'data-style-token-card="style-token-layout-and-motion"' not in html
    assert 'data-style-token-card="style-token-product-components"' not in html
    assert 'data-style-token-card="segmented-control"' in html
    assert 'data-style-token-copy="Segmented control"' in html
    assert 'data-style-token-inventory-demo' not in html
    assert html.count('data-style-token-card=') == 21
    assert 'data-style-token-card="strategy-tuning-control"' in html
    assert "--strategy-tune-button-size" in html
    assert "--strategy-tune-panel-padding" in html
    for token_name in (
        "--workspace-metric-label-font-size",
        "--workspace-metric-label-line-height",
        "--workspace-metric-label-letter-spacing",
        "--workspace-metric-label-font-weight",
        "--workspace-metric-label-color",
        "--workspace-metric-card-min-height",
    ):
        assert token_name in html
    assert 'href="#frosted-glass"' in html
    assert 'data-style-token-control' in html
    assert 'href="/settings/style-tokens"' in html
    assert 'sidebar.js?v=sidebar-v1.21.0-codex.1' in html
    assert 'segmented-control.js?v=segmented-control-v1.0.4-codex.1' in html


def test_cache_summary_metrics_reuse_the_foundation_metric_contract(client) -> None:
    response = client.get("/cache/chatgpt")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    class_sets = [set(value.split()) for value in re.findall(r'class="([^"]*)"', html)]
    assert any({"metric-grid", "foundation-metric-grid", "cache-summary-metrics"} <= classes for classes in class_sets)
    assert any({"progress-metric-grid", "foundation-metric-grid"} <= classes for classes in class_sets)
    # Each resource total has one owner; only distinct run work remains in the progress section.
    assert sum("foundation-metric-card" in classes for classes in class_sets) == 9
    assert sum("progress-metric-card" in classes for classes in class_sets) == 1
    for field in (
        "cached_sessions", "cached_messages", "discovered_tweets", "discovered_images",
        "downloaded_images", "skipped_tweets", "failed_tweets", "task_failures", "progress_processed_tweets",
    ):
        assert html.count(f'id="{field}"') == 1, field
    assert html.count('data-chatgpt-metric-mode="text"') == 2
    assert html.count('data-chatgpt-metric-mode="media" hidden') == 7
    assert 'aria-label="ChatGPT sync notice"' not in html
    assert 'class="metric-card foundation-metric-card">' not in html


def test_every_metric_card_instance_reuses_the_foundation_contract(client) -> None:
    """Keep rendered and retained metric-card markup on the single Foundation contract."""
    template_root = Path(__file__).parents[1] / "app/web/templates"
    template_markups = [
        path.read_text(encoding="utf-8")
        for path in sorted(template_root.glob("*.html"))
    ]
    rendered_markups = [
        client.get(route).get_data(as_text=True)
        for route in (
            "/cache/x",
            "/cache/grok",
            "/cache/chatgpt",
            "/cache/gemini",
            "/browser?view=media",
            "/browser?view=prompts",
            "/browser?view=text",
        )
    ]

    for markup in template_markups + rendered_markups:
        metric_classes = [
            set(class_attribute.split())
            for class_attribute in re.findall(r'class="([^"]*)"', markup)
            if "metric-card" in class_attribute.split()
        ]
        for classes in metric_classes:
            assert "foundation-metric-card" in classes
            assert "metric-card-accent" in classes


def test_style_tokens_route_renders_requested_browser_components_and_table(client) -> None:
    response = client.get("/settings/style-tokens")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    for card_id in (
        "secondary-button",
        "primary-button",
        "global-theme-toggle",
        "prompt-tag",
        "pagination",
        "shared-select-filter",
        "shared-select-dropdown",
        "agent-browser-selector",
        "scrollable-data-table",
        "switch",
        "text-input-control",
        "modal-dialog",
        "modal-dialog-banner-message",
        "settings-action-package",
        "settings-execution-option",
        "tooltip",
        "workspace-metric-value",
        "frosted-glass",
    ):
        assert f'data-style-token-card="{card_id}"' in html
    assert 'data-source-xpath="/html/body/main/div[3]/aside/section[2]/form/div[2]/button"' in html
    assert 'class="style-token-secondary-button-preview style-token-component-action-row"' in html
    assert 'data-style-token-secondary-button-use-icon="false"' in html
    assert 'class="icon agent-effort-refresh-icon"' not in html
    assert 'data-source-xpath="/html/body/main/div[3]/section/div/div[2]/nav"' in html
    assert 'data-source-xpath="/html/body/main/div[3]/aside/section[2]/form/label[2]/div/button"' in html
    assert 'data-source-route="/agent/edge/claude"' in html
    assert 'data-source-selector="label.field:nth-of-type(2) > div.agent-browser-session-field.is-browser-status-loading > div.trade-strategy-combobox.agent-combobox:nth-of-type(1) > button.trade-strategy-select.form-select"' in html
    assert 'aria-label="Browser: Edge"' in html
    assert 'data-style-token-agent-browser-option="edge"' in html
    assert 'data-style-token-demo="global-theme-toggle"' in html
    assert 'data-style-token-theme-toggle' in html
    assert 'aria-label="Switch to Dark mode"' in html
    assert 'data-style-token-theme-toggle-label' not in html
    assert 'class="style-token-theme-toggle-label"' not in html
    assert 'data-style-token-demo="primary-button"' in html
    assert 'data-style-token-primary-button' in html
    assert 'data-source-selector="button#start_button"' in html
    assert 'data-style-token-demo="shared-cache-settings-link"' not in html
    assert 'data-style-token-shared-cache-settings-link' not in html
    assert 'data-style-token-demo="prompt-tag"' in html
    assert 'aria-label="Tag demo"' in html
    assert 'data-style-token-prompt-tag' in html
    assert 'data-source-route="/browser"' in html
    assert 'aria-label="Remove remark PS"' in html
    assert 'data-style-token-table-demo' in html
    assert 'class="style-token-table-demo-summary-row"' not in html
    assert 'class="style-token-component-kicker">Sessions</p>' not in html
    for token_name in (
        "--field-title-font-size",
        "--field-title-line-height",
        "--field-title-letter-spacing",
        "--field-title-font-weight",
        "--field-title-color",
    ):
        assert html.count(token_name) >= 3
    assert '<legend class="sr-only">Execution option</legend>' not in html
    assert 'data-style-token-table-filter-option="buy"' in html
    assert 'data-style-token-table-pagination' in html
    assert html.count('data-style-token-table-row') == 12
    assert html.count('data-style-token-shared-filter-option=') == 15
    assert html.count('data-style-token-table-filter-option=') == 3
    assert 'data-style-token-switch' in html
    assert 'data-style-token-text-input-clear' in html
    assert html.count(' data-style-token-dismiss aria-label=') == 2
    assert 'data-style-token-action-package' in html
    assert 'data-style-token-action-package-live' in html
    assert 'data-action-package-live-marker' in html
    assert 'class="settings-action-package settings-callout-card-primary style-token-action-package"' in html
    assert 'class="settings-nav-icon-shell settings-action-package-icon-shell settings-callout-icon-shell"' in html
    assert 'class="settings-general-form style-token-settings-general-form"' in html
    assert 'class="settings-general-option-title">' in html
    assert 'class="settings-general-option-desc">' in html
    assert 'class="settings-action-package-copy settings-callout-text"' in html
    assert 'class="settings-inline-button settings-inline-button-primary"' in html
    assert 'class="switch-row style-token-action-package-live-control"' in html
    assert 'class="style-token-action-package-live-label">Live marker</span>' in html
    assert 'data-style-token-action-live-control' not in html
    assert 'Show live state' not in html
    assert 'data-style-token-reference="frosted-glass"' in html
    assert 'data-style-token-reference="settings-execution-option"' in html
    for removed_copy in (
        "The Local resources action keeps the production secondary-button surface and states.",
        "The primary cache action uses the shared blue surface and pending and disabled states.",
        "The live theme control keeps the current appearance visible and reverses its action label.",
        "The floating pager keeps the active page in a blue spatial indicator.",
        "Saved prompt remarks use a compact blue pill with a working remove and restore affordance.",
        "The standard trigger, dropdown, selected state, and filter options are shared by table headers and forms.",
        "The sticky header, internal scroll, Type filter, and in-shell pagination remain synchronized.",
    ):
        assert removed_copy not in html

    script = (Path(__file__).parents[1] / "app/web/static/settings-style-tokens.js").read_text()
    for fragment in (
        "bindStyleTokenTableDemos",
        "bindStyleTokenFilterMenus",
        "bindStyleTokenAgentBrowserDemo",
        "bindStyleTokenPagination",
        "bindStyleTokenThemeToggleDemo",
        "bindStyleTokenPrimaryButtonDemo",
        "attachStyleTokenControls",
        "attachTextInputClearHandlers",
        "attachStyleTokenReferences",
        "bindPromptTagDemo",
        "bindDismissibleDemos",
        "bindActionPackageDemo",
        "bindStyleTokenDensity",
        "data-action-package-live-marker",
        "data-style-token-action-package-live",
        "aria-busy",
        "dragGeometry = measureDragGeometry();",
        "targetY === lastHandleY",
        "window.requestAnimationFrame",
        "__styleTokenOnSelect",
        "Tag removed; restoring preview",
    ):
        assert fragment in script
    for fragment in (
        "bindSegmentedDemos",
        "bindToggleDemos",
        "bindWorkflowDemos",
        "bindProductDemos",
        "data-style-token-workflow-action",
        "data-style-token-product-action",
    ):
        assert fragment not in script

    segmented_script = (Path(__file__).parents[1] / "app/web/static/segmented-control.js").read_text()
    for fragment in (
        '.range-mode-shell[data-option-count]',
        'segmentedPill !== "measured"',
        'ResizeObserver',
        '--segmented-pill-left',
        '--segmented-pill-width',
    ):
        assert fragment in segmented_script


def test_style_token_page_inherits_the_global_theme_contract() -> None:
    template = (
        Path(__file__).parents[1] / "app/web/templates/settings_style_tokens.html"
    ).read_text()
    stylesheet = (Path(__file__).parents[1] / "app/web/static/style.css").read_text()

    assert "data-style-token-reference-skin" not in template
    assert 'theme-mode.js' in template
    assert '.style-token-page .global-theme-toggle[data-effective-theme="light"] .icon' in stylesheet


def test_settings_page_links_to_style_tokens(client) -> None:
    response = client.get("/settings")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'href="/settings/style-tokens"' in html
    assert html.count("Style tokens") == 1
