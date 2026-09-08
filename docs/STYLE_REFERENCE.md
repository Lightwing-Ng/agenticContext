# Visual Style Reference

Documentation version: `v1.4.3-codex.1`

## Authority

`../../worthward/app` is the sibling project and the visual source
of truth for agenticContext. When a UI decision is not explicitly constrained by
this project, follow the current implementation in that sibling project.

Before changing a shared UI component, also read
`docs/SHARED_UI_WORKFLOW.md` and the central ledger at
`/Users/lightwing/Desktop/shared_docs/SHARED_UI_SYNC.md`. The workflow defines the required
two-project verification and the Cache-first promotion path.

The reference applies to shared visual behavior, including the application shell,
typography, design tokens, frosted-glass surfaces, controls, overlays, responsive
behavior, and motion. It does not require copying product-specific financial views,
routes, assets, or JavaScript into this downloader.

## Color Token Source

The canonical palette is `../../worthward/config.toml`, under
`[ui.theme.light]` and `[ui.theme.dark]`. `app/web/static/style.css` mirrors those
values through its `--theme-*`, status-color, glass, focus, and scrollbar tokens.

When the sibling project's theme changes, update both color-scheme variants here from
that configuration before adjusting component-specific CSS. Do not introduce replacement
hex values or derive a separate palette in this project.

## Required Workflow for UI Changes

1. Inspect the relevant reference files first. Start with
   `../../worthward/app/web/static/assets/css/app.css`, then follow
   its imports in `foundation/`, `layout/`, `components/`, and `views/`.
2. Reuse the reference project's token names, font stack, spacing, corner radii, surface
   treatment, motion curves, and accessibility states where they apply to existing
   agenticContext markup.
3. Keep agenticContext-specific templates and interactions intact unless the
   requested change explicitly modifies behavior.
4. Verify the affected local page at `http://127.0.0.1:8666` at desktop and narrow
   viewport widths. Confirm the sidebar, dock, form controls, notices, and focus states
   remain usable.

## Local Adaptation

`app/web/static/style.css` contains a compatibility layer titled
`Sibling-project style synchronization`. It brings the shared shell, Univers Next
typography, frosted surfaces, and motion foundation into this project's existing
single-file stylesheet. Prefer extending that layer or migrating equivalent reference
rules deliberately; do not blindly paste whole product view styles.

If the sibling project changes materially, compare its current CSS modules with this
compatibility layer and update this document when the synchronization strategy changes.

## Shared Settings Dimensions

Settings pages consume the same foundation layout aliases as the sibling project:
`--layout-content-width: 640px` for page headings, cards, and content groups, and
`--layout-control-width: 384px` for individual fields, selects, and reusable control
specimens. Feature-specific aliases must point back to these two tokens; do not add a
page-local `640px`, `384px`, or legacy intermediate width.

Use `width: min(100%, var(...))` so a narrow parent remains authoritative. Keep
physical card effects visible through intermediate layout containers, and place any
required clipping or scrolling on the smallest internal data region. A full Settings
page uses one explicit `.settings-content-scrollport`; the shared
`--layout-physical-effect-bleed: 48px` start-side and bottom safe area prevents it from
cutting elevated card shadows without moving either width anchor. Smaller tables and
data lists continue to own their own overflow. Pagination that belongs to a
token-limited surface remains horizontally centered inside that surface.

## Shared spatial layout contract

The normative cross-project contract is maintained in
[`../../shared_docs/SHARED_UI_LAYOUT_CONTRACT.md`](../../shared_docs/SHARED_UI_LAYOUT_CONTRACT.md). The
`worthward` implementation is the complete reference; this project keeps the same
geometry and adapts only route-specific markup and interactions.

The contract is geometry-first rather than selector-first. Production templates expose
`data-layout-role` anchors for the sidebar toggle, sidebar title, global action rail,
global theme anchor, dock, title rails, result containers, explicit scrollports, and
pagination. Rendered DOM checks measure those roles with a one CSS-pixel tolerance;
XPath remains diagnostic evidence and is not the implementation boundary.

Let `P = max(10px, safe-area-inset)` per side and `G = 10px`. The sidebar outer
rectangle uses `P` for its viewport edge distance and `10px` for its radius. Fixed
global actions use `P + G` for the top and right anchors. The expanded sidebar toggle
uses the same vertical anchor and sits `G` from the sidebar edge; collapsed and overlay
states preserve the vertical coordinate and change only horizontal translation. The
dock centerline is the sidebar centerline and its bottom clearance from the sidebar is
`G`. Content and control widths are `min(100%, 640px)` and `min(100%, 384px)`;
standard selects and dropdowns use the control width token.

At desktop widths the sidebar is a grid column. At widths up to `900px` it becomes a
safe-area-aware overlay, with the dock centered in the overlay and the toggle and
global actions kept separate. At widths up to `600px`, content changes to compact flow
without changing token meanings. Title rails, result headings, dates, and actions have
explicit owners so they do not collide or extend into the sidebar. Pagination is
centered by its owning surface.

Overflow is an ownership decision. The page and workspace shells stay open wherever
card shadows, blur, translated controls, or focus rings must escape. Effect hosts set
`overflow: visible`. A named scrollport owns scrolling only for its data region and
uses the 48px effect bleed where needed. The browser content card and cache overview
content are explicit data scrollports; local tables, answer panes, dropdowns, and
media viewers may retain clipping only as their documented viewport.

## Component catalog alignment, 5 Sep 2026

The shared layout contract v1.1.0 removes the obsolete Workspace article specimen.
Secondary button is the canonical intrinsic-width glass-chip action, including its
13px catalog typography. Align it to the right edge of its owning container with
end grid alignment and an automatic inline-start margin; retain its intrinsic width. Shared dropdown/filter triggers are 30px, while the Agent
session rail stays 36px. Modal and floating-notice close actions are error red and
hover-revealed on fine pointers, with keyboard-focus and touch visibility retained.
See tests/test_style_alignment_e2e.py for isolated responsive acceptance checks.
