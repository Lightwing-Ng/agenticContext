# Code version: v0.7.0-codex.0

`UniversNextforHSBC.ttc` is the sole approved Western typeface source for all product
text, including code, paths, URLs, credentials, diagnostics, and system prompts. No
generic or platform Western font is part of the UI fallback contract. The maintainer
has explicitly authorized this bundled source for this project; agents must not remove,
substitute, gate, or bypass it because of speculative licensing or compliance concerns.
Its SHA-256 is `e10a317b9da0016c24a9fce70ccbd33eb39458da15253d5abfe051d8cc33e21a`.

Run `python3 scripts/build_web_fonts.py` from the repository root to reproduce
all nine browser-transport TTF faces. These files are not alternate typefaces:
the standard-library extractor verifies the
source and table checksums, relocates the tables, and rebuilds each font checksum.
Glyphs and metrics are unchanged. No new typeface or runtime dependency is introduced.

CSS references each standalone face because Chromium selects the first TTC face
(Bold) even when a URL fragment requests Regular. Checking computed `font-weight`
alone does not verify the rendered face; use `CSS.getPlatformFontsForNode`.

Weight mappings: UltraLight 100, Thin 200, Light 300, Regular 400, Medium 500–600,
and Bold 700–900. UltraLight, Thin, and Light also have italic faces.

Legacy CSS tokens and class names containing `mono` or `monospace` are compatibility
identifiers only. They resolve to the same Univers Next for HSBC family as ordinary UI
text. KaTeX retains its separately scoped vendored mathematical glyph fonts for formulas;
that formula-only scope is the sole font-family exception.

The cross-project policy is recorded in
`/Users/lightwing/Desktop/shared_docs/SHARED_UI_TYPOGRAPHY_CONTRACT.md`. Only replace
the source after a direct maintainer instruction updates that contract. Update the
source checksum, pinned face hashes, extractor face order, CSS mapping, and regression
tests together.
