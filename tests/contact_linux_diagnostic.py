"""Failure-only text and actual-font evidence for the private Linux probe.

Code version: v1.0.0-codex.0
"""

import json
import os
from pathlib import Path
import time


SELECTOR = (
    ".wechat-contact-table[data-table-body] tbody tr:nth-child(2) td:nth-child(3)"
)
EXPRESSION = r"""() => {
    const cell = document.querySelector('.wechat-contact-table[data-table-body] tbody tr:nth-child(2) td:nth-child(3)');
    if (!cell) return {cellMissing: true};
    const rect = box => ({left: box.left, right: box.right, top: box.top,
        bottom: box.bottom, width: box.width, height: box.height});
    const style = node => {
        const value = getComputedStyle(node);
        return Object.fromEntries(['whiteSpace', 'overflowWrap', 'wordBreak',
            'font', 'fontFamily', 'fontSize', 'fontWeight', 'fontStretch',
            'fontStyle', 'letterSpacing', 'fontFeatureSettings', 'fontVariationSettings',
            'paddingLeft', 'paddingRight', 'borderLeftWidth', 'borderRightWidth',
            'textAlign', 'direction'].map(name => [name, value[name]]));
    };
    const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
    const nodes = [];
    let node, remaining = 1024;
    while ((node = walker.nextNode()) && nodes.length < 16 && remaining > 0) {
        const range = document.createRange();
        range.selectNodeContents(node);
        const characters = [];
        let offset = 0;
        for (const character of node.data) {
            if (remaining-- <= 0) break;
            const end = offset + character.length;
            range.setStart(node, offset);
            range.setEnd(node, end);
            characters.push({start: offset, end, codePoint: character.codePointAt(0),
                whitespace: /\s/u.test(character), rects: [...range.getClientRects()].slice(0, 8).map(rect)});
            offset = end;
        }
        range.selectNodeContents(node);
        nodes.push({text: node.data.slice(0, 1024), length: node.length,
            union: rect(range.getBoundingClientRect()),
            fragments: [...range.getClientRects()].slice(0, 64).map(rect),
            parent: node.parentElement.tagName, computed: style(node.parentElement), characters});
    }
    const range = document.createRange();
    range.selectNodeContents(cell);
    return {cell: rect(cell.getBoundingClientRect()), computed: style(cell),
        union: rect(range.getBoundingClientRect()),
        fragments: [...range.getClientRects()].slice(0, 128).map(rect), textNodes: nodes,
        fontsStatus: document.fonts.status,
        faces: [...document.fonts].slice(0, 64).map(face => ({family: face.family,
            style: face.style, weight: face.weight, stretch: face.stretch, status: face.status})),
        userAgent: navigator.userAgent};
}"""


def _bounded(owner, coroutine, timeout=0.75):
    import asyncio

    return owner._sync(asyncio.wait_for(coroutine, timeout=timeout))


def capture(page):
    """Never replace the assertion that caused this bounded postmortem."""
    began = time.monotonic()
    result = {"version": "v1.0.0-codex.0", "failure_only": True, "errors": []}
    session = None
    try:
        try:
            result["geometry"] = _bounded(page, page._impl_obj.evaluate(EXPRESSION))
        except Exception as error:
            result["errors"].append(
                {"operation": "geometry", "type": type(error).__name__}
            )
        try:
            session = _bounded(
                page.context, page.context._impl_obj.new_cdp_session(page._impl_obj)
            )
            _bounded(page, session.send("DOM.enable"))
            _bounded(page, session.send("CSS.enable"))
            document = _bounded(page, session.send("DOM.getDocument", {"depth": 0}))
            match = _bounded(
                page,
                session.send(
                    "DOM.querySelector",
                    {
                        "nodeId": document["root"]["nodeId"],
                        "selector": SELECTOR,
                    },
                ),
            )
            result["platform_fonts"] = _bounded(
                page,
                session.send(
                    "CSS.getPlatformFontsForNode", {"nodeId": match["nodeId"]}
                ),
            )
        except Exception as error:
            result["errors"].append(
                {"operation": "platform_fonts", "type": type(error).__name__}
            )
    except Exception as error:
        result["errors"].append({"operation": "capture", "type": type(error).__name__})
    finally:
        if session:
            try:
                _bounded(page, session.detach(), timeout=0.5)
            except Exception as error:
                result["errors"].append(
                    {"operation": "detach", "type": type(error).__name__}
                )
    result["geometry_and_fonts_elapsed_seconds"] = time.monotonic() - began
    destination = None
    try:
        directory = Path(os.environ["CONTACT_LINUX_DIAGNOSTIC"])
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"failure-{time.monotonic_ns()}.json"
        destination.write_text(json.dumps(result, indent=2) + "\n")
    except Exception:
        pass
    if destination is not None:
        screenshot = destination.with_suffix(".png")
        try:
            _bounded(
                page,
                page._impl_obj.screenshot(path=str(screenshot), timeout=1500),
                timeout=1.5,
            )
            result["screenshot"] = {
                "path": screenshot.name,
                "bytes": screenshot.stat().st_size,
                "over_one_mib": screenshot.stat().st_size > 1024 * 1024,
            }
        except Exception as error:
            result["errors"].append(
                {"operation": "screenshot", "type": type(error).__name__}
            )
        result["total_elapsed_seconds"] = time.monotonic() - began
        try:
            destination.write_text(json.dumps(result, indent=2) + "\n")
        except Exception:
            pass
    try:
        print(
            "Contact Linux text diagnostic: "
            + json.dumps(result, separators=(",", ":"))
        )
    except Exception:
        pass
