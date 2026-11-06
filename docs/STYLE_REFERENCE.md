# Visual Style Reference

Documentation version: `v1.31.31-codex.0`

## Authority

`../../worthward/app` is the canonical complete visual baseline for agenticContext.
`../../neoMe/app` is the third maintained consumer of applicable shared components.
When a UI decision is not explicitly constrained by this project, follow the current
Worthward implementation and verify neoMe wherever it exposes the same component.
The explicit notification exception is the user-approved Local resources waiting
card during local media delete/restore. Its `--frosted-glass-notice-*` variant is
the shared notification baseline in all three projects; it does not replace the
general control material.

Before changing a shared UI component, also read
`docs/SHARED_UI_WORKFLOW.md` and the central ledger at
`/Users/lightwing/Desktop/shared_docs/SHARED_UI_SYNC.md`. The workflow defines the required
verification across every applicable maintained project and the Cache-first promotion path.

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

## Western Typeface Source

The normative three-project contract is
[`../../shared_docs/SHARED_UI_TYPOGRAPHY_CONTRACT.md`](../../shared_docs/SHARED_UI_TYPOGRAPHY_CONTRACT.md).
`app/web/static/fonts/UniversNextforHSBC.ttc` is the sole approved Western
typeface source for agenticContext. Every product string must use
`--font-family-base` and the locally served Univers Next for HSBC faces,
including paths, code and preformatted content, credentials, diagnostics,
system prompts, URLs, and token/value specimens. Legacy tokens and class names
containing `mono` or `monospace` are compatibility identifiers only; their
computed font family must resolve to `--font-family-base`, never to a generic or
platform monospace face. Named CJK fallback families remain available only for
glyphs absent from Univers Next for HSBC.

The maintainer has explicitly authorized this bundled font for these projects.
Do not remove, substitute, gate, or bypass it because of speculative licensing
or compliance concerns. KaTeX keeps its separately scoped, vendored mathematical
glyph fonts; that formula-only scope is not an alternate interface typeface.

Chromium does not honor TTC face fragments and selects the collection's first
Bold face for every CSS weight. agenticContext therefore serves deterministic
standalone transport faces extracted from its approved TTC; those files retain
the original glyphs, metrics, and PostScript names and do not constitute another
typeface.

## Required Workflow for UI Changes

1. Inspect the relevant reference files first. Start with
   `../../worthward/app/web/static/assets/css/app.css`, then follow
   its imports in `foundation/`, `layout/`, `components/`, and `views/`.
2. Reuse the reference project's token names, spacing, corner radii, surface treatment,
   motion curves, and accessibility states where they apply to existing agenticContext
   markup. Preserve the local typography contract above instead of copying the sibling's
   current `font-family` values.
3. Keep agenticContext-specific templates and interactions intact unless the
   requested change explicitly modifies behavior.
4. Verify the affected local page at `http://127.0.0.1:8666` at desktop and narrow
   viewport widths. Confirm the sidebar, dock, form controls, notices, and focus states
   remain usable.

## Local Adaptation

`app/web/static/style.css` contains a compatibility layer titled
`Sibling-project style synchronization`. It brings the shared shell, this project's
local Univers Next typography adaptation, frosted surfaces, and motion foundation into
its existing single-file stylesheet. Prefer extending that layer or migrating equivalent reference
rules deliberately; do not blindly paste whole product view styles.

If the sibling project changes materially, compare its current CSS modules with this
compatibility layer and update this document when the synchronization strategy changes.

## WeChat conversation share action

WeChat conversation and contact tables use the same proven native-source preference.
When an HTML observation matches an iPhone or iPad observation, the default row and
export use the native representative while the existing source disclosure retains
the other observation. Exact same-second source-order permutations do not create
duplicate rows; unrelated ordering anomalies affect only their independently
verified source interval. HTML-only messages and unresolved conflicts remain
visible. This is a data-projection change with no new control, font, material,
motion, spacing or responsive breakpoint.

Conversation detail reuses Worthward's direct-export Share action from
`templates/_macros.html`: the existing `.circular-icon-button` primitive and
byte-identical `square.and.arrow.up.svg`. Its only transport is the existing
complete-conversation CSV route. The search toolbar returns to All conversations
through the existing Circular icon button and `browser-session-neighbor-prev-icon`
left-arrow adapter. Its native link retains the account, conversation kind, sort,
and collection-page context, with an accessible label and back-navigation tooltip.
The toolbar centers Back with the search field. Above the existing 900px sidebar
breakpoint, Back remains at the toolbar's left edge, before the search-action
container with the existing 12px gap, in both sidebar states. The compact adapter
retains its sidebar-toggle alignment and safe-inset tokens.
The Share action's 32px circle and 18px glyph sit 10px before the theme action on the same
global-anchor centerline. The title reserves both circles without changing fonts
or introducing a breakpoint. At the existing 900px sidebar-overlay breakpoint,
the workspace Share and Back actions are hidden while that overlay is open,
preserving the sidebar's width and keyboard boundary. Closing the sidebar restores them.
The native download link retains its account and conversation-kind scope, label,
tooltip, and standard focus state; it introduces no menu or controller.

The contact aggregation toolbar consumes the same existing Circular icon button
for its Back and Export CSV links. Back reuses the conversation navigation button
classes and `browser-session-neighbor-prev-icon` left-arrow mask, retaining the
full original conversation return URL and source anchor. Export reuses Worthward's
marked direct-export glyph through the existing `browser-session-page-export-icon`
and byte-identical `square.and.arrow.up.svg`. Both controls keep the shared 32px
circle, 18px icon, accessible name, tooltip, focus states, and native link behavior.
The contact toolbar retains its own placement and CSV transport; it does not
import investment drawers or the conversation's global rail positioning rules.

## Shared Collapse

Worthward's `templates/_collapse.html`, `components/collapse.css`, and foundation
tokens are the canonical shared Collapse implementation. Shared disclosures keep the
native `details.ui-collapse > summary + .ui-collapse-body` structure so browser
keyboard activation and open-state semantics remain authoritative. The single `12px`
by `8px` down-chevron mask points right while closed through
`--collapse-icon-closed-rotation: -90deg` and points down while open through
`--collapse-icon-open-rotation: 0deg`; the transition uses the standard `180ms`
motion and becomes immediate under Reduced Motion. Product-specific disclosures may
retain their own geometry and content, but any repeated chevron affordance reuses the
same mask, size, and rotation tokens instead of defining another direction system.

## Shared Process List

The Tunnel ChatGPT and Gemini onboarding pages consume the same `Process List`
primitive published in both component catalogs. The semantic `ol.process-list`
contains `li.process-list-step` children with a numbered marker and content
column. Only continuing steps draw the accent connector. The shared
`--process-list-*` tokens own marker, gap, connector, heading, and supporting
copy geometry; Tunnel-specific nested instructions and actions stay within the
product adapter. neoMe imports the same primitive for future applicable flows
without adding a fictitious health workflow. The normative geometry and
acceptance boundaries are in the central shared layout contract.

## Shared Select

Worthward's standard single-value Shared select is the canonical replacement for an
ordinary native select. AgenticContext's `browser-filter-select.js` adapts both Local
resources filters and fields marked `data-shared-select-auto`: the hidden native
select remains the sole form and application-state authority, while the generated
button and listbox expose the shared accessible and keyboard contract. Product-owned
model, browser-session, source, multi-select, or searchable pickers may reuse the same
tokens and controller without being treated as standard-select consumers.

The standard trigger is a `30px` pill and its options have a `36px` minimum height.
The trigger uses the shared translucent Frosted Glass material. The menu adopts the
user-approved Worthward Backtest Period surface: a theme-highlight `56%`-to-`16%`
gradient over a theme-background layer controlled by
`--shared-select-dropdown-surface-opacity: 62%`. This dedicated menu alias does not
change the general Frosted Glass material or notification variant. Both consumers
retain the semantic border, shadow, hover shadow, and `12px` blur aliases.
The menu opens `4px` from the trigger,
uses `10px` padding and the soft radius, and is bounded by `min(360px, 55vh)`. Its
`12px` by `8px` current-color down-chevron points right while closed through
`--shared-select-chevron-closed-rotation: -90deg` and points down while open through
`--shared-select-chevron-open-rotation: 0deg`. The `90deg` transition uses `180ms`;
Reduced Motion removes it. Text-only options use
only check and text columns. Standard fields stay within the smaller of their parent
inline size and the shared `384px` control width.

The Settings operating-system adapter and Shared select catalog specimen consume
the same menu material and direction tokens without an OS-specific material override.
Explicitly opaque Cache/source product adapters remain named exceptions. Their
shared chevrons adopt the same direction and Reduced Motion contract without
changing product selection, portal placement, or form behavior.
The Local resources sidebar is the third named exception: its standard filter selects
(Media type, Sort order, Project, and Answerer) open over the next field's label and
trigger, which would read through the `62%` layer. `.browser-filter-form .browser-filter-select`
therefore sets `--shared-select-dropdown-material` to `--frosted-glass-opaque-background`,
the same base as the adjacent Source menu. Trigger material, blur, border, shadow, radius,
option geometry, and keyboard behavior are unchanged.
Only the existing flat Settings content host with a direct
`[data-settings-content-scrollport]` child disables its own backdrop filter. A
nested filtered ancestor would prevent the menu's `12px` blur from obscuring local
supporting text. The host retains its background, border, shadow, and layout;
ordinary cards and general Frosted Glass tokens are unchanged.
The catalog's inline-size containers retain their responsive measurement, but an
open Shared select elevates its owning `.style-token-demo` through the shared
global-popover layer. This prevents following specimens from intercepting options
after the menu's entrance animation finishes, including under Reduced Motion.

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

Field titles and reusable scrollable-table headers consume the shared
`--field-title-*` role: `15px`, regular `400` weight, normal line height and letter
spacing, and primary text color. Apply it to filter headings and Style token specimen
titles without changing the legacy `--font-form-label` role used by values and
supporting copy. The role is breakpoint-independent; existing containers continue to
own wrapping and available width.

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

### Session index columns

The Local resources session index assigns all five body columns explicit percentages;
the shared table controller derives the detached header widths from those cells.
Every allocation sums to 100%, and every integer percentage is divisible by 2 or 5.
The index stays narrow, the session title receives the largest share, and Source
headings and marks are centered. The four-column Zhihu index omits Source and assigns
that space to the answerer title.

The named `browser-session-index` inline-size container uses the available table
width, including sidebar and scrollbar effects, rather than a viewport breakpoint.
Widths above 800px use 4/56/10/10/20%; the middle range uses 5/48/12/15/20%; widths
up to 500px use 8/28/18/22/24%. Zhihu uses 4/66/10/20%, 5/60/15/20%, and
8/50/18/24%, respectively. Compact cells use 4px inline padding. Dates, identifiers,
and headings wrap within their column without creating horizontal table scrolling.
This is a product-specific table adapter; shared table tokens and financial tables
retain their existing owners.

### Zhihu answer detail columns

Above the existing 767px card-layout breakpoint, the four-column answer detail
table assigns No./Time/Question/Answer 5%/15%/25%/55% of the body table width.
Both the detached header and the body declare this product-specific allocation;
the shared table controller measures the body cells to keep the header aligned.
The narrow index and timestamp preserve sufficient space for their labels and
values, while the long answer receives most of the remaining width. At 767px
and below, the existing stacked answer cards own the layout instead of column
percentages. No shared table token or sibling-product table changes are needed.

### Answerer and session actions drawer

The Local resources session-detail actions drawer collapses to one 32px Circular action and expands on
hover or focus. Its expanded width is derived from `--browser-session-action-count` (three by default)
rather than a fixed multiple, and each action keeps its own offset of one 42px step (the 32px action
plus the 10px gap) and a 28ms stagger.
Zhihu answerer detail adds `Update latest answers from Zhihu` as a fourth action by setting the
modifier `.browser-session-actions--source-update` (count four); it reuses the local
`icloud.and.arrow.down.svg` mask that marks the ChatGPT session refresh. While it runs, the wait modal
covers the page; its unavailable state dims only the icon, so the drawer's hover opacity is unchanged.
The result reuses the shared `browser-session-refresh-banner` material. This is a product-specific
adapter; Worthward and neoMe have no equivalent Local resources session drawer.

### ChatGPT Media Sessions index

ChatGPT Media uses the existing semantic `secondary-button browser-session-back-link` anchor
to enter the Sessions index. Session detail labels it `Back to all sessions` and returns to the
remembered index page; the flat gallery labels it `All sessions`. The index omits this link
because it is already the destination, while its Session View button remains available for
reverse navigation. The link uses the in-project Secondary button material and visible text
instead of the former `photo.stack.svg` Circular icon toggle and its pressed-state tooltip.
The index renders one `.browser-session-card` per session inside a `.browser-gallery`
`.browser-session-gallery`, so the existing four/three/two/one column breakpoints and the shared List
and Grid dock apply unchanged. Covers are never cropped: the image is drawn whole at the full cover
width in its own ratio (`width: 100%`, `height: auto`, `object-fit: contain`), with no fixed frame.
In Grid layout the cover is a flex-grown, centering grid, so every cover in a row takes the tallest
image's height, shorter images sit centered on `--theme-panel-strong`, and titles stay aligned. The
catalog's width and height, when recorded, are rendered as `width` and `height` attributes so the
browser reserves the ratio before lazy loading; a cover without them keeps a 96px minimum only while
loading or after a failure, for the status pill. List layout is a 112px cover column with the title
(two lines, then an ellipsis) on the left and the image count and date right-aligned on one line; each
row is as tall as its image. At 560px and below the list column is 88px, the count and date stack
under the title, and the grid keeps two columns rather than one.

The cover shell and image use their own `.browser-session-cover` and `.browser-session-cover-media`
classes. The media gallery's list rules target `.browser-preview` and `.browser-preview-media` with a
280px minimum height, so reusing those classes would stretch every list cover. Card surface, hover
lift, and focus ring mirror `.browser-media-card`; the ring appears only for keyboard focus. This is
a product-specific adapter; Worthward and neoMe have no ChatGPT media surface, and the shared
Secondary button, Circular button, and view dock primitives are consumed unchanged.

A session detail's `Back to all sessions` link names the session it leaves. The index opens on
the page that holds that session, whatever page was remembered, marks its card `.is-return-origin`,
centers it in the content scrollport, and moves keyboard focus to its cover link. The mark is the
standard blue interaction glow: the `--accent-border-medium` border with the 4px `--accent-focus-ring`
ring and the 18px `--accent-focus-glow`, layered over the card's resting or hover shadow. It is a
static shadow on the card itself, so it moves with native scrolling and needs no scroll handler, and
it stays until the next navigation. While a cover without a recorded size is still loading, the
script keeps the card centered on gallery size changes until the first wheel, touch, pointer, or key
input, or four seconds. A sidebar filter change, pagination, and the Dock entry start without an
origin. Dark mode uses the same tokens, so the glow is subtler there.

### Saved prompt actions

A ChatGPT media card's Prompt heading carries the same `Add as prompt` action as a cached user
message. It is the standard 32px Circular icon button, placed before the expand control in
`.browser-media-prompt-actions`. Hovering or focusing within the prompt section reveals it, touch
keeps it visible, and the saved state stays visible in the success color. Cards that show the same
prompt of one conversation share the saved state.

Each Saved prompts row ends its remark input line with a Circular save toggle. Its
`bookmark.slash.fill.svg` state removes the prompt; the row then stays in place at 45% opacity with
the toggle showing `text.bubble.fill.svg`, which saves the prompt and its remarks again until the
page reloads. The toggle is hover-revealed on fine pointers like the Copy action and always visible
on touch. A touch phone column wraps it under the full-width input, while a narrow pointer window
keeps it beside the input so no empty line is reserved. A prompt whose text is saved more than once
starts its remark row with a compact warning mark: `rectangle.on.rectangle.svg` and the copy count
on `--theme-warning-translucent` in `--theme-warning-text`. Both assets are byte-identical copies
of the Worthward files. These are product-specific adapters; the shared Circular icon button and
Tag primitives are consumed unchanged.

### Compact conversation effects

WeChat source disclosures use one bounded, autoescaped coordinate projection in
ordinary messages, forwarded records, and quoted transcripts. The existing
definition list shows a source file, database/table, device-local or exporter-local
message identifier, and distinct page/position coordinates when available. Export
identifiers retain their declared namespace; no local number becomes a server ID.
Repeated archive/member hashes, CRCs, ZIP directory spellings, compression details,
and offsets are omitted from the rendered document. Unknown structures add no empty
`Record` row. This presentation adapter leaves original provenance, identity proofs,
Markdown/CSV/API evidence, timestamps, and archive bytes intact. A storage migration
must preserve the complete logical locator before shared receipts can replace
per-message copies; hiding a field does not reclaim database space. Shared disclosure
materials, typography, motion, and breakpoints are unchanged.

WeChat `Source clock` values in chat disclosures and contact-source details share
one presentation-only formatter. Explicit device-local Chinese periods and AM/PM
render as `D MMM YYYY HH:mm` or `D MMM YYYY HH:mm:ss`, retaining the original
precision. ISO offsets remain literal and are never converted to a presumed viewer
timezone. Invalid or epoch-placeholder clocks stay verbatim; raw provenance, APIs,
CSV values, timeline clocks, typography and disclosure geometry are unchanged.

WeChat conversation types reuse one compact Badge macro in the conversation index
and detail heading. The adapter follows Worthward's allocation Badge geometry:
2px corners and 2px/6px block/inline padding. Light mode uses an opaque 16% muted
mix over the theme background with muted text. Dark mode retains the original
muted fill and theme-background text. Both system preference and the explicit
application theme override select the same pair. The index keeps the
Badge below the title; no new breakpoint or shared primitive is introduced.
Group chat, Private chat, Official account, and Unclassified chat use the same
gray treatment. Official-account copy comes only from the existing native peer
identifier classifier, with group classification taking precedence. Titles never
establish identity. Persisted conversation kinds, filters, account boundaries,
exports, and source records remain unchanged.

The Local resources conversation scrollport uses 8px padding on all four sides at
every breakpoint. Session detail reserves a separate 32px navigation rail outside
the scrollport; the magnified ruler therefore cannot cover message text. Combined
message lists omit that rail. This is a product adapter, not a change to the shared
48px physical-effect token.

Message cards use 8px block and 12px inline padding at every breakpoint. Ordinal
badges use the existing muted theme token and a 16% muted background. Known
timestamps sit below the ordinal, right-aligned with a 4px row gap. Fine-pointer
ruler pitch is 8px; coarse pointers retain the 24px targets.

`browser-chat-effects.js` paints the shared outer card shadows in an inert,
pointer-transparent sibling layer. Native cards retain their inset highlights,
while their content remains inside the original scroll owner. The effect layer
uses layout containment and the shared 48px paint allowance at its sides.
Its top clips at the chat pane boundary so scrolling shadows cannot paint
over the preceding transparent summary metrics. A step-end clip animation on the
same scroll timeline bounds bottom shadows to the scrollport until the list ends,
preventing a partially visible card from casting a hollow bubble below its content.
At the end, or when the list does not overflow, the full 48px bottom paint allowance
remains available. Tall messages do not enlarge the document. Full card
rectangles are placed in scroll-content coordinates and follow resizing,
content-size changes, and theme updates. A `ScrollTimeline` bound to the message
scrollport translates their track on the compositor, so shadows move in the same
frame as native scrolling; never reposition them from a `scroll` handler, which
trails asynchronous scrolling and visibly detaches shadows from their cards.
Engines without scroll timelines keep the cards' native, scrollport-clipped
shadows. No message content or interactive control is copied into the effect layer.

WeChat details reuse this same pane and inert effect layer, including stable card IDs,
while retaining their directional widths, alignment, human-message padding, and provenance
controls. No separate shadow implementation or navigation ruler is introduced. Effect geometry
uses fractional computed CSS dimensions rather than rounded client dimensions when deriving
ancestor scale, preventing deep-scroll drift on wrapped or fractional-height layouts.

WeChat source disclosures share the message's author/timestamp header row without a trailing
chevron; a closed card has no separate disclosure or footer row. Native summaries keep the
accessible name `Source details` and the existing Circular icon button minimum hit height.
Passive text and padding in the expanded source body toggle that same card closed,
restoring its compact dimensions. Native summaries, nested disclosures, interactive
controls, and noncollapsed text selections retain their own behavior. A divider after
the source fields reuses `wechat-quote-separator`; it appears only while the source
details are open and adds no closed-card row.
The author and a hidden time slot wrap inside the summary's single content column.
Each bubble sits in a full-width, visually transparent message row. The one semantic
timestamp moves to that row's right edge, aligned with the sent bubble's content inset.
Its original slot preserves the natural width measurement and exact vertical baseline.
Timestamp placement has two complete geometric states: distant bubbles retain their
natural width beside the common rail; nearby bubbles snap wide enough to contain the
entire clock and its content inset. The decision uses the undocked natural width and
one/two padding-inset thresholds for entry/release, not a viewport breakpoint. A cached,
temporary logical-size probe bypasses active width interpolation; contained width/min/max
constraints prevent intermediate clock-straddling frames. Nested bubbles may borrow the
ancestor's right padding while remaining within the outermost row. Named headers retain
their clock slot, and continued rows reserve vertical clearance only for real content
or source-target collisions. CSS transitions are disabled for these geometry targets so
the global Reduced Motion duration cannot animate a logical probe or delay clearance.
The WAAPI disclosure spring remains independent. ResizeObserver measures the baseline
before paint during wrapping, disclosure motion, font loading, and resize. Keeping the time outside the animated details also avoids
clipping it when the bubble springs open. The article references its time through
`aria-describedby`; without JavaScript, the original native header still shows its time.
`disclosure-motion.js` owns the reusable measured disclosure controller, using Worthward's
620ms bouncy spring (mass 1, stiffness 180, damping 18, initial velocity 0). `browser-messaging.js`
supplies the message card as its width target. Opening overshoots
before settling; critically damped closing keeps the summary visible. A small disclosure-body
translation/stretch accompanies the layout spring without scaling the entire message text.
Rapid reversal starts from the currently rendered dimensions. Reduced Motion skips animation
and immediately settles active motion when the preference changes; resize, font loading, and
page exit also clear transient animations. Closing content is inert until native closure.
Forwarded `Chat history` summaries consume that same controller and message-card width
target, including nested transcripts. Their native toggle no longer finishes the
animation it just started. Related active ancestors or descendants settle before a
new measurement, while independent disclosures and same-target reversal retain
their motion. Deep source anchors use the controller's immediate `setOpen` path,
including an ancestor currently closing, before measuring the scroll position.
The external-shadow adapter intersects only actively unfolding forwarded ancestors;
unrevealed child shadows remain hidden, and partial child shadows respect that clip.
Settled transcripts clear the temporary clip and retain their ordinary shadow bleed.
The observer watches only `data-collapse-motion`, avoiding its own style writes.
No new easing, typography, breakpoint, or card material is introduced. Coverage lives
in `tests/test_wechat_forwarded_motion_e2e.py` and
`tests/test_disclosure_motion_control_e2e.py`, alongside the existing source,
contact, forwarded, no-JavaScript, and nested transcript browser checks.
The verified-contact source adapter consumes the same controller without a width target, so
only disclosure height and source-body motion change; table columns and statement text retain
their geometry. Initial rendering and `wechat:contact-content-ready` adopt new disclosures
idempotently, and detached active disclosures are settled before replacement nodes are bound.
This product-specific controller remains independent of ScrollTimeline support. The shared
shadow adapter updates card geometry in the ResizeObserver delivery before paint so layout
springs do not detach shadows. Cards use the shared 8px/12px message padding, a 4px
header/body gap, and one 12px inter-card gap rather than stacked grid gaps and margins.
Message direction stays in expandable provenance and text exports. Presentation changes do
not infer forwarding status or alter unknown source directions.

Consecutive native messages show their sender visually only on the first message
of an adjacent run. Runs require the same exact native sender, account, conversation,
and direction in chronological order; elapsed time and calendar-day changes do not
restart the sender label. Unknown identities/clocks and system notices break runs;
filtered search results and page boundaries retain explicit sender labels. Original messages, accessible
names, timestamps, counts, and exports are unchanged. The compact variant removes
the closed header's layout space and reuses the timestamp rail as its native
source-disclosure target. A minimum 24px source target centers on the measured clock;
content collisions reserve only the clock line and its 4px content gap, extended
when needed to clear that target. Short messages reclaim the header space without
covering text or Finder controls. The shared 32px disclosure default is unchanged.
Without JavaScript the ordinary named header remains available.

Typed transfer and red-packet cards reuse the human message shell and the existing
resource row's 32px icon, 12px gap, text role, wrapping, colors, and inert semantics.
CNY/HKD transfer and red-packet variants select the four byte-identical currency
SVGs from Worthward. Unspecified or conflicting currency keeps a neutral transfer
or gift icon and the `Currency unknown` tooltip; a bare yen sign does not select CNY.
The same adapter covers ordinary, forwarded, and quoted messages. A fully proven
regional transfer can override a legacy subtype's presentation kind while keeping
original exports intact. Exact, field-scoped WeChat provider copy resolves through
the centralized English locale catalog, including Simplified/Traditional transfer,
red-packet, notification, and status variants. Translation keys and parameters stay
separate from literal memos, names, amounts, and greetings. The payment text reuses
the existing pre-wrapped body, with English language tags on translated fragments
and no forced English tag on freeform source fragments. Repeated translated
provider labels render once; original records and exports remain unchanged.
No new font, material, responsive breakpoint, payment action, or shared control is introduced.

PDF text-layer records may append consecutive, line-start `System Message:` notices
to a speaker's text. The WeChat presentation adapter separates that trailing suffix
into centered, wrapping, muted plain-text rows outside the bubble and timestamp
wrappers. Those rows have no card material, shadow, sender, or invented timestamp;
the exporter prefix is omitted from their visible copy. Ordinary quoted markers and
non-PDF text retain their original presentation. Raw record text, stable source IDs,
source offsets, account partitions, exports, and pagination counts stay unchanged.

Verified speaker names are ordinary same-tab contact links inside the native summary. Activating
the name opens Contact analysis; activating the summary itself retains source disclosure and
native keyboard behavior. The product-specific contact page reuses the standard shell, metrics,
search, select, floating pagination, and `scrollable-data-table-shell` fixed-header table. Its
five columns fit the available inline width without horizontal scrolling; full text wraps
within each cell. No bubble material, alternate typeface, sentiment inference, or new shared
component is introduced. The analysis preserves
source-account provenance and links each occurrence back to its actual source page.

Contact statements include text inside source-verified HTML forwarded transcripts.
Each embedded author must pass the same native-ID and published-source proof as
the chat card; display names never establish identity. A compact reference index
stores parent IDs, component paths and original clocks without duplicating bodies.
Search reads only the author's own text, excluding quoted and descendant text,
system notices and explicit media placeholders. The existing cell-note style labels
these occurrences `Forwarded message`. Source links locate the parent's page and
open only the target's transcript ancestors; source disclosures and sibling trees
retain their independent state. Browser Back restores each actual card and nested
transcript separately. No shared typography, table geometry or breakpoint changes.

Contact aggregation defaults to oldest first, matching WeChat's top-to-bottom
chronology. Its No. column uses plain chronological integers across pages in the
current search scope; totals retain their existing number formatting. An explicit
newest-first choice reverses known-time statements while preserving those numbers.
Unknown-time statements remain at the end in either order. Contact entry links,
search, pagination, and CSV share the default without changing the originating
conversation's return state. This is a WeChat adapter contract, with no new shared
control, typography token, or breakpoint.

Explicitly reviewed transcript copies may share one contact statement across
receiving conversations. The local `statement_reviews.json` declaration pins every
parent's text and provenance hashes, one verified receiving owner, and a preferred
native group packet. Complete native item sequences, resource-placeholder metadata,
literal authors, bodies, independent seconds, and HTML item suffixes must agree;
display names and isolated same-text/same-time matches are insufficient. Media is
not opened. The selected native receiving group is display context, not proof of
the original conversation: `Original conversation unknown` remains authoritative.
All accepted references and source observations remain available. A compact journal
restores prior whole-group membership before replaying changed or removed reviews;
complete compatible caches upgrade without rescanning original authors. This changes
only WeChat grouping and source preference, with no shared component or layout change.

A separate automatic proof handles complete copied transcripts with at least three
unique 32-character hexadecimal item IDs. Every flat text slot must match in order,
native author, literal body, and its own second-resolution clock; a verified native
packet anchors any independently owner-verified HTML copy. Receiving accounts,
groups, forwarding authors, and parent forwarding clocks may differ and remain
separate source observations. Conflicting item content, explicit original server
or chat IDs, incomplete roles, or ambiguous clocks prevent the new union. Item IDs
identify the complete copied packet here, never a globally unique server message.
Whole existing groups stay intact, iPad source preference selects the representative,
and original conversation/timezone uncertainty remains visible. Complete compatible
contact caches migrate without a source-wide author scan.

Two-item text transcripts use a separate, stronger proof. At least two distinct
native forwarding events must have the same verified owner, snapshot, receiving
group, forwarder, and direction, with distinct native server and local IDs. Each
needs exactly one independently verified HTML parent receipt with matching owner,
group, forwarder, direction, and second-resolution parent clock. Both complete
ordered opaque item IDs, native authors, literal bodies, child clocks, and explicit
identity constraints must agree. Missing or ambiguous receipts reject the cohort.
The ordinary three-or-more-item rule, whole-group conflict guards, preserved
observations, and original-conversation/timezone uncertainty remain unchanged.

Every merged contact statement retains a closed source disclosure, including
consistent all-native observations. The preferred account cell stays compact;
ordinary native source counts live in the existing chevron's title, while existing
conflict and corroborated-HTML count annotations remain inline. Opening or closing
sources reuses the same native disclosure and motion controller. Source-clock text
is formatted before comparing it with the actual displayed observation clock, so
equivalent formatting does not create another row. Different precision, explicit
time zones, invalid literal clocks, and source uncertainty remain inspectable.

Conversation-type labels reuse `_wechat_conversation_kind.html` and the same
`wechat-kind-badge.css` component in both chat history and contact statements.
The gray material, 2px radius, 2px/6px padding, small-text role, and Light/Dark
colors remain identical. Only the contact table permits the badge label to wrap
within its narrow column; it does not shrink the type or overlap the account column.

No./Time/Statement/Conversation/Source account use 10/20/40/15/15% at every width.
This user-selected allocation sums to 100%; no breakpoint reduces the No. or Time
share. No. values remain horizontally centered. Both the
colgroups and body cells declare the allocation; the existing shared controller
measures body cells to align the detached header after loading and resizing. The
header permits that measured width to shrink by a traditional scrollbar gutter;
the body retains its full scrollport width. At the reported 855px viewport, the last
page's five-digit numbers and complete timestamps must each occupy one rendered line.
Shared table tokens, typography,
and sibling financial-table layouts remain unchanged.

Contact loading reuses `_waiting_modal.html`, its centered frosted-glass notification material,
and its inline canonical spinner SVG. Optional initial-open/title/copy parameters preserve the
existing closed defaults for other consumers. A pending page keeps the same dialog and copy
across background status polls; it never reloads the document on a timer. The result settles
behind that dialog before one close. Shared 700ms rotation and Reduced Motion remain unchanged.
An interrupted status request or temporary non-JSON server/gateway error retries at 2, 4,
and 8 seconds while retaining that same dialog. A valid indexing response resets the
consecutive-failure budget. Contact-scope mismatches, malformed successful responses,
and structured source errors still stop loading; exhausted transport retries expose Refresh.
Leaving the page cancels pending retries and prevents late responses from changing it.

Contact pagination keeps the active page's token-based blue fill before its shared
motion helper is ready. Only a measured indicator enables the animated variant;
missing or delayed JavaScript leaves the actual active page readable instead of
painting the previous-page arrow. The deferred contact adapter remeasures through
one animation frame after font loading, pagination resize, window resize, or visual
viewport resize. Four-digit page labels use the existing 11px `--font-ui-xs` role
inside the unchanged 30px desktop and 28px narrow-screen circular slots. Shorter
page labels and arrows keep their existing typography. Five-page grouping,
navigation parameters, and responsive breakpoints remain unchanged.

Complete legacy PDF quoted replies reuse the same message card as a presentation-only
variant. A semantic `blockquote` shows the quoted speaker and text before a 1px
`--theme-glass-border` rule, then the unchanged current-speaker disclosure and reply.
The current speaker uses the scoped 17px identity role. Message text, quoted
speaker/text, and resource primary labels use the scoped 15px content role,
backed by `--font-ui-lg`. These sizes apply at every viewport without a new
breakpoint. Header and card parents keep their original size, preserving the
existing timestamp, measurement-slot, and resource-status sizes. Every text role
retains the shared font family; the quote uses
`--theme-muted` instead of smaller type. No extra card, left rail, footer, or disclosure
row is added. Recognition requires the confirmed full quote envelope and exporter
separator; ambiguous text remains literal. Raw records, exported text, source offsets,
contact identities, timestamps, and source-account boundaries remain authoritative.
Focused coverage lives in `tests/test_wechat_quoted_reply.py` and
`tests/test_wechat_quoted_reply_e2e.py`.

Explicit WechatExporter HTML quotes use that same variant without changing the
template or CSS. Recognition requires the `quoted_reply` extraction role,
MesLocalID export namespace, Type 49 locator, and one `rendered_quote` component.
Its complete envelope must match the stored prefix, and the remaining reply must
match `primary_text_sha256`. Ordinary typed quotations and inconsistent evidence
stay literal. The quoted speaker remains an exported label, not a verified contact.
Original text, provenance, and exports remain unchanged. Focused integrity checks
also live in `tests/test_wechat_html_quotes.py`.

Native quoted speakers reuse the contact display-name preference only after the
receiving account, published source snapshot, and immutable `wxid_` identity are
verified. A group reference whose `fromusr` is the current room uses its explicit
`chatusr` author; a direct native author retains its own role. Wrong-room,
conflicting, unknown, or label-only references keep their source-rendered label.
The quoted name retains the existing 15px muted role and adds no new control or
spacing. Native forwarded quotes use the same verified receiving envelope.
Original speaker labels, quote references, bodies, source clocks, accounts, and
exports remain unchanged. Coverage lives in `tests/test_wechat_quoted_contacts.py`
and `tests/test_wechat_quoted_contacts_e2e.py`.

Legacy HTML Type 1 text uses the same quote variant only with a complete legacy
envelope, the exact 15-part spaced-hyphen separator, and a nonblank reply. The
MesLocalID/plain-text source and full stored-body SHA-256 must agree, and native
quote/reference declarations or nonempty quote/forward components prevent this
fallback. This is a text-format presentation, not evidence of native Type 49 roles.
Ordinary quotations remain literal. The shared text parser preserves exact offsets;
records, exports, clocks, labels, and account boundaries remain unchanged. Coverage
also lives in `tests/test_wechat_legacy_html_quotes.py` and the existing quote E2E cases.

Source-verified HTML forwarding reuses the existing `forwarded_conversation` and
`message_row` macros. The outer sender and forwarding clock remain visible beside
a default-closed `Chat history` summary. The transcript body is transparent and has
no border, corner radius, backdrop filter, or independent shadow surface. Individual
messages retain the standard bubbles, 17px speaker names and 15px text. Every nested
clock shares the outermost message's right rail while retaining its original header
baseline. Real source titles and reply text remain visible in a compact text caption.
The observed WeChat-generated titles `群聊的聊天记录` and `Group Chat History` render as the existing gray
Group chat badge inside that caption wrapper, including native, HTML, nested and
quoted transcripts. The badge keeps its intrinsic width and English language tag;
custom titles containing the phrase and ordinary message text remain literal.
Stored titles, provenance and exports remain unchanged. Generated fallback titles
do not add a repeated line. No font role, shared Collapse
primitive, or responsive breakpoint changes. Explicit
component evidence and exact stored-body validation replace only the flattened
presentation markers. Unproven or inconsistent records stay literal. Contact identity,
unknown time zones, original text, and exported evidence retain their existing rules.

Reviewed flattened nested previews reuse that same default-closed `Chat history`
disclosure. A pinned matching excerpt renders referenced indexed messages through
the standard recursive message macro, with the compact `Matched excerpt` caption
and the original source observation clocks. Those clocks keep the outermost right
rail and header baseline; they do not establish the preview's original chronology.
The reference requires verified receiving account, source partition, snapshot,
conversation, forwarder and component authors, plus an exact complete preview
match. It stores source IDs, paths and hashes, not another message-body copy.
Without that evidence, a reviewed nested preview remains one escaped literal block
under `Preview only`, with no invented child rows, authors or timestamps. Ordinary
colon-delimited text is not parsed into messages. Source disclosures retain both
the literal preview and the reference relationship. Unregistered contact objects
retain their verified display label without an unavailable analysis link. No
archive, contact registry, CSV, media, shared material, font role or breakpoint is
changed. Coverage lives in `tests/test_wechat_forwarded_previews.py` and
`tests/test_wechat_nested_forwarded_e2e.py`.

Native `system_text` records reuse the same plain-text notice presentation. Their
original timestamp occupies its own line above the centered body, using the normal
message clock's typography, color, and measured right rail. The system row remains
in document flow without a sender label, bubble, disclosure, or external shadow;
its stable message anchor and raw source/export data are preserved. A contact named
`System` does not become a system notice without the authoritative extraction kind.
Known XML system envelopes reuse the bounded text extractor for their display
projection. A numeric room prefix must match the current peer; explicit plain
copy takes precedence, otherwise a complete known template expands only its
source-declared profile and plain-text fields. Multiple names require an explicit
source separator. Hidden revoke actions remain inert and do not expose URLs.
Unknown or inconsistent schemas retain their literal escaped source; ordinary
message XML is unchanged. The projection changes neither stored records nor
exports, and the existing centered notice and right-aligned clock need no new
markup, styling, or breakpoint. Focused coverage lives in `tests/test_wechat_text.py`,
`tests/test_wechat_system_notice_markup.py`, and `tests/test_wechat_system_template_e2e.py`.
System rows render a clock only when both its display label and source timestamp
exist. Missing source times add neither an `Unknown time` line nor an empty `time`
element or dangling accessible description; the row title retains the source-time
limitation. The raw timestamp fields, text exports, ordering, and stable anchors stay
unchanged. Do not replace a missing clock with adjacent message times or a same-text
notice from another account. Known system clocks retain their existing right rail.
Recognized PDF and native system notices omit exact `wxid_` status-icon placeholders
ending in `@textstatusicon` from their visible text. The adapter removes one redundant
ASCII space only when it surrounds the omitted decoration, preserving word boundaries,
possessives, other whitespace, source offsets, and raw exports. Unknown placeholders
and ordinary user messages remain literal; no contact or status artwork is inferred.

WeChat media and file occurrences remain compact message bubbles with the normal
speaker, source disclosure, timestamp rail, and external shadow. A resource body
reuses the bundled `photo.stack.svg`, `film.circle.svg`,
`document.on.document.fill.svg`, and `play.fill.svg` symbols instead of loading
media previews. Its native button has a 44px minimum target and an inline status:
`Show in Finder`, `Local file candidate`, `Mac folder`, or `Not found on this Mac`.
Missing resources remain visible without an action. Folder actions explicitly
describe their scope rather than claiming an exact file match. Native Finder
feedback and failures stay inside the resource; neither removes the placeholder.
Verified native account mappings add the compact `Mac account linked` context.
Stale resource indices show an explicit refresh notice while retaining the stored
resource catalog. All geometry and color changes stay in the WeChat adapter.
On a selected session, that notice applies only to verified source families serving the
conversation. Ready HTML placeholders do not inherit an unrelated native catalog warning;
stale HTML proofs and applicable native failures remain visible. Global source views retain
their catalog-wide status. Source scopes, counts, export dependencies, and placeholder bodies
are unchanged by this notice selection.

Audited top-level HTML media nodes can also supply metadata-only timeline events.
These use the unchanged standard message bubble and plain `[Image]`, `[Video]`,
`[Voice message]`, `[Sticker]`, or `[File]` text, with the original direction and
timestamp rail. They have no preview, download, or Finder action. A stable source,
archive, and frame reference remains in provenance for a future media link; an
exporter-local message number never becomes a server identifier. Original HTML
wall clocks retain their unknown time zone. Browsing and session export consume
the same additive timeline without rewriting the source text archive.

The global HTML media-event publication uses `media_events.sqlite3`, a separately
published metadata index built from complete, hash-verified node audits. It reuses
the same plain message-bubble projection; the small JSON overlay remains compatible
and identical source-frame IDs appear once. Indexed conversation summaries and
message lookups support pagination, CSV, source navigation, and media-only source
conversations without opening ZIPs, media payloads, or a cloud-only native catalog.
Every publication pins source snapshots, account ownership, selected member receipts,
conversation scope, and the primary archive identity. Invalid or stale proof fails
closed; metadata availability does not assert that every device archive is complete.
No shared material, icon, type scale, or responsive breakpoint changes.

The Local resources search icon keeps its 16px SVG transport and a square slot
matching the search control's inner height. Its native lens center, rather than
the bounding box including the handle, aligns with the pill's semicircular end.
The shared Local resources search input is 30px high at every breakpoint; its
existing 1px outer border makes the full pill 32px high. Saved prompt remark
inputs use the same 30px height and the existing 999px `--radius-pill` token.
These are local component sizes, not changes to the global form-control tokens.
Between 601px and 900px the search field takes the shared `--layout-control-width`
(384px) at the right edge of its toolbar in every Local resources view. The compact
flow at 600px and below keeps the full row, and the desktop rail keeps its 322px field.

### Cache activity disclosure

Every application page reuses the shared Circular icon button and Live marker for a
cache task entry point. It is hidden when no cache task is running or queued, so a
page without cache work looks as it did before. `_cache_activity.html` and
`_cache_activity_assets.html` are included by the Cache, Agent, Jury, Local resources,
Settings, Style tokens, and Beta templates; the locked Agent access page has none.

The button shares the theme action's horizontal center and sits in the lower corner of
the page's workspace panel. It repeats one clearance below itself: the theme action's
distance from the workspace panel's right edge. On a Cache page that rectangle is also
the content scrollport's, so the right and bottom clearances stay equal there. A surface
pinned to that corner declares `data-cache-activity-clearance`; the Agent and Jury
composers do, and the entry then sits the same clearance above the composer instead of
covering its submit action. Geometry is measured from rendered rectangles, so the compact
layout, a growing composer, and safe-area insets remain authoritative. The disclosure is
outside the scrollport and never participates in content layout or scrolling.
The Cache page uses the shared full-width page rectangle; retaining its legacy
1,560px cap would place the viewport-anchored theme action outside the content owner
on wider displays and make these two anchor requirements incompatible.

Clicking expands the same glass surface toward the upper left, using the existing
popover material, soft radius, control-width maximum, and emphasized motion curve.
The read-only nonmodal dialog is titled `Cache tasks` and lists running tasks first,
then queued ones, each named by source and content mode. A running row shows its phase
description, work-item counts, and a meter. A queued row shows what it waits for and
has no meter. Escape restores the trigger's focus; clicking outside closes it. Reduced
Motion makes expansion immediate and keeps a static live marker; the marker is also
static when every listed task is queued. Long task lists scroll inside the page's content
scrollport, or below the title rail on a page whose content does not scroll as a whole.

The meter carries the Cache progress material in `cache-activity.css` rather than the
Cache page's scoped rules: a 6px borderless track, the positive fill, and the static
40% segment for an unknown total. It therefore measures the same on every page.

The lightweight `/api/cache/activity` endpoint reads in-memory task snapshots for every
registered source and content mode. It never hydrates caches or probes a browser. A row's
mode comes from the runtime it belongs to, and a queued row's reason is worded by the
task coordinator from registered labels only, never from task output.
Refresh failure retains the last known list with an explicit stale-status message
and pauses the breathing animation. Successful empty responses hide the entry point.
This adapter remains a Candidate review in the shared UI ledger; Worthward and neoMe
applicability is Pending.

On a Cache page, a queued task shows the `queued` phase, an empty progress track labeled
`Queued`, and the Stop action, which removes it from the queue. While another cache task
owns Safari, the account row reads `Not checked` with the reason and no status glyph, and
Start stays available because the task waits in the queue.

The product-specific local directory browser reuses the Workspace modal surface, Secondary and
primary buttons, text-input material, folder asset, radii, typography, and color tokens. Its dialog
is viewport-bounded and overflow-hidden; the folder list is its single vertical scroll owner.
Breadcrumbs may scroll horizontally as path navigation, while the document and dialog do not.
At 600 px and below, path controls become two rows and folder metadata wraps without changing
selection semantics or introducing a positional selector.

## Component catalog alignment, 8 Sep 2026

The shared layout contract v1.3.0 removes the obsolete Workspace article specimen.
Secondary button is the canonical intrinsic-width glass-chip action, including its
13px catalog typography and a 32px minimum height. Align it to the right edge of its
owning container with end grid alignment and an automatic inline-start margin; retain
its intrinsic width. Shared dropdown/filter triggers are 30px, while their options
are 36px and the Agent session rail stays 36px. Options without media use only the
check and text columns so their labels do not truncate behind an empty icon slot.

Style-token copy actions share the global theme action's right anchor. Settings
navigation follows Worthward's 10px sidebar inset, 62-percent theme-background glass
layer, 28px icon slot, 16px monochrome symbols, transparent icon shells, and
muted-to-accent state change. Style tokens uses the shared sparkles symbol, and the
optional Beta Dock destination uses the same local `sparkles.2.svg` asset in both
projects. Modal and floating-notice close actions are error red and
hover-revealed on fine pointers, with keyboard-focus and touch visibility retained.
Both surfaces use the same two-column semantic grid. The absolute 24px close action
keeps equal 12px top and left insets; a 24px-minimum title row places the title in
the flexible column and vertically centers a single line on the close-action center.
The unchanged 36px topic icon and the paragraph or outside-marker list begin together
in the second row. List markers remain outside the content box so wrapped lines use a
hanging indent through the shared modal list-padding and marker-gap tokens, and the
shared 4px row gap owns all space between title and body.
Modal and floating-banner surfaces consume the canonical notification background,
border, shadow, and `saturate(160%) blur(18px)` directly. Catalog-only theme
overrides must not alter this variant. Notification titles are 15px semibold;
paragraph and list copy is 15px regular muted text, and the X glyph is 12px inside
the unchanged 24px close target. Wrapped titles grow without clipping or moving
that target. Named icon-free form dialogs inherit the same material while
preserving their product-specific grid, width, scrolling, and security behavior.
See tests/test_style_alignment_e2e.py for isolated responsive acceptance checks.

The shared waiting modal embeds the canonical `loading.spinner.svg` circle geometry
inside its existing 36px topic-icon wrapper. Its accent-colored paint does not depend
on an external mask request during form navigation. Keep the shared 700ms linear
rotation, the same title/body grid, and a visible static ring under reduced motion.
This indeterminate indicator does not infer completion percentages from elapsed time.
The Local resources Dock entry opts into this same notice before full-document
navigation. It follows Worthward's animation-frame-to-task paint boundary, with a
120ms fallback, so feedback appears before requesting the destination. Modified
clicks, downloads and other browsing contexts retain native behavior. Page exit
and persisted page restoration clear the navigation notice and its pending timers.
Every Dock host mounts the same partial and controller exactly once.
Verified-contact statement pagination opts into the same paint-before-navigation
notice for numeric, current-page and chunk-arrow links. Search, sort and exact
return context remain in the destination; keyboard and modified clicks retain
the shared navigation contract.

Circular actions use `.circular-icon-button` as the reusable primitive. Its 32px
control at every viewport width, 18px icon, pill radius, `--circular-icon-button-material` Frosted Glass
surface, border, shadow, and state colors come from `--circular-icon-button-*`; the former
`--settings-round-icon-button-*` names are compatibility aliases only. Sidebar,
global, Browser session, and media actions keep their semantic classes as adapters
and also expose the canonical class in markup.
Sidebar toggle offsets, title rails, global action reserves, and catalog copy rails
derive from the same 32px size token; the shared 10px edge gap and vertical
centerline do not change. Isolated sidebar and global theme actions retain a
transparent 44px coarse-pointer hit region without changing their painted or
computed 32px box. Adjacent actions do not gain overlapping hit regions. The
24px dismiss action, 32px
Process List marker, and 36px topic icon or Agent session rail remain separate sizes.
The catalog title and copy rail compensate for the existing 8px compact page pad
at `<=560px` using the global-anchor-minus-page-pad equation. This preserves their
centerline and right axis without introducing another breakpoint or moving the
global controls.

Pagination keeps `.local-store-pagination` and `.local-store-page-button` as its
public markup contract. Non-active page, arrow, and ellipsis controls inherit
`--local-store-pagination-button-color` and change the text and current-color glyph
together to `--local-store-pagination-button-color-hover` on hover or
`:focus-visible`. Spatial movement remains tokenized, with a 1ms linear Reduced
Motion adaptation.

Scrollable tables use one clipped `.scrollable-data-table-shell` with a direct-child
fixed `table[data-table-header]` and one `.scrollable-data-table-scroll` owner that
contains `table[data-table-body]`. The controller synchronizes columns, scrollbar
compensation, header height, and horizontal scroll. The shared header, cell, summary,
row, minimum-width, and scrollbar-gutter visuals consume the canonical
`--scrollable-data-table-*` tokens. AgenticContext-only shell and body aliases remain
local adapters; product table classes only adapt column semantics and content.
The first visible, non-summary body row remains the column-width authority. The
controller observes its actual cells so container-query column changes synchronize
the fixed header even when the table's outer bounds do not resize. Replacing,
hiding, or changing the eligibility of that row refreshes the observed cells. A
body-only mutation observer never observes the header or shell styles written by
synchronization; cleanup disconnects both observers.

Segmented controls use `.segmented-control`, shrink-wrap with `fit-content`, remain
centered within `max-width: 100%`, and create equal tracks with
`repeat(var(--segmented-option-count), minmax(0, 1fr))`. A measured pill is an
explicit adapter for content-driven widths, not the default overflow strategy.

Workspace metric labels use the Agent runtime form-field label as their shared
typographic reference: 15px regular primary text with normal line height and
letter spacing. Numeric values remain 24px regular. Metric cards reserve an
18px label line and 48px minimum height, then grow when a narrow column wraps
the label. The shared numeric renderer preserves the complete text value while
splitting integer, decimal, and suffix fragments into
`.workspace-metric-value-major`, `.workspace-metric-value-minor`, and
`.workspace-metric-value-suffix`. The Style tokens catalog uses `Total trades` and
`2,032.15%`; Browser and Cache metric consumers use the same renderer without
changing their accessible text.
