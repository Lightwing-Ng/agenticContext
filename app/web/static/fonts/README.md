# Code version: v0.5.0

`UniversNextforHSBC.ttc` is the only approved Western typeface source for the
application. No platform Western font is part of the runtime fallback contract.
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

KaTeX retains its vendored mathematical glyph fonts as a scoped formula-rendering
exception; they do not participate in ordinary interface text.

Only replace the source with an approved font licensed for this application.
Update the source checksum, extractor face order, and CSS mapping together.
