"""Record bounded metadata only after an original Jury navigation timeout.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import datetime
import faulthandler
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit


DIRECTORY = Path(os.environ.get("COVERAGE_FILE", "test-results/.coverage")).parent
OUTPUT = DIRECTORY / "jury-navigation-failures.jsonl"
THREADS = DIRECTORY / "jury-navigation-threads.log"


def record(kind, nodeid, value):
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("a") as output:
        output.write(
            json.dumps(
                {
                    "version": "v1.0.0-codex.0",
                    "pid": os.getpid(),
                    "kind": kind,
                    "nodeid": nodeid,
                    "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    **value,
                }
            )
            + "\n"
        )
        output.flush()


def static_path(url):
    value = urlsplit(str(url))
    return (
        value.path
        if value.hostname in {"127.0.0.1", "localhost"}
        and value.path.startswith("/static/")
        else None
    )


def tree_state(tree):
    resources = []

    def visit(frame):
        for resource in frame.get("resources", []):
            path = static_path(resource.get("url", ""))
            if path and len(resources) < 128:
                resources.append(
                    {
                        "path": path,
                        "type": resource.get("type"),
                        "failed": resource.get("failed"),
                        "canceled": resource.get("canceled"),
                    }
                )
        for child in frame.get("childFrames", []):
            visit(child)

    visit(tree)
    return resources


def record_resource_tree(channel, nodeid, kind):
    record("resource_tree_begin", nodeid, {"state": kind})
    try:
        tree = channel.send("Page.getResourceTree")["frameTree"]
        record(kind, nodeid, {"resources": tree_state(tree)})
    except Exception as error:
        record("resource_tree_error", nodeid, {"error_type": type(error).__name__})


EXPRESSION = r"""JSON.stringify((() => {
    const path = value => {
        try { const url = new URL(value,location.href); return url.origin === location.origin && url.pathname.startsWith('/static/') ? url.pathname : null; }
        catch (_) { return null; }
    };
    const resources = performance.getEntriesByType('resource').map(entry => ({path:path(entry.name),duration:entry.duration,startTime:entry.startTime,responseEnd:entry.responseEnd,responseStatus:entry.responseStatus})).filter(entry => entry.path).slice(-128);
    const finished = new Set(resources.map(entry => entry.path));
    const declared = Array.from(document.querySelectorAll('script[src],link[href],img[src]')).map(element => ({path:path(element.src || element.href),tag:element.tagName,stylesheet:element.rel === 'stylesheet',sheet_available:element.rel === 'stylesheet' ? Boolean(element.sheet) : null,image_complete:element.tagName === 'IMG' ? element.complete : null})).filter(entry => entry.path).slice(0,128);
    const navigation = performance.getEntriesByType('navigation')[0];
    return {
        ready_state:document.readyState,
        font_status:document.fonts.status,
        navigation:navigation ? {domContentLoadedEventStart:navigation.domContentLoadedEventStart,domContentLoadedEventEnd:navigation.domContentLoadedEventEnd,loadEventStart:navigation.loadEventStart,loadEventEnd:navigation.loadEventEnd,responseEnd:navigation.responseEnd,responseStatus:navigation.responseStatus,duration:navigation.duration} : null,
        resources,declared,
        pending_inferred:declared.filter(entry => !finished.has(entry.path) || entry.image_complete === false || (entry.stylesheet && !entry.sheet_available))
    };
})())"""


def _capture(page, url, nodeid, error):
    began = time.monotonic()
    browser = page.context.browser
    record(
        "navigation_failure",
        nodeid,
        {
            "pathname": urlsplit(str(url)).path,
            "original_exception_type": type(error).__name__,
            "browser_connected": browser.is_connected() if browser else None,
            "browser_version": browser.version if browser else None,
            "contexts": len(browser.contexts) if browser else None,
        },
    )
    with THREADS.open("a") as output:
        output.write("\n" + nodeid + "\n")
        output.flush()
        faulthandler.dump_traceback(file=output, all_threads=True)
        output.flush()
    channel = None
    try:
        channel = page.context.new_cdp_session(page)
        responsive = False
        record("runtime_begin", nodeid, {})
        try:
            result = channel.send(
                "Runtime.evaluate",
                {"expression": EXPRESSION, "returnByValue": True, "timeout": 500},
            )
            if "exceptionDetails" in result:
                raise RuntimeError("Runtime evaluation exception")
            record(
                "runtime", nodeid, {"snapshot": json.loads(result["result"]["value"])}
            )
            responsive = True
        except Exception as runtime_error:
            record(
                "runtime_unresponsive",
                nodeid,
                {"error_type": type(runtime_error).__name__},
            )
            scripts = {}
            stack = []

            def parsed(event):
                scripts[event["scriptId"]] = static_path(event.get("url", ""))

            def paused(event):
                for frame in event.get("callFrames", [])[:12]:
                    location = frame.get("location", {})
                    stack.append(
                        {
                            "functionName": frame.get("functionName", "")[:120],
                            "static_script_path": static_path(frame.get("url", ""))
                            or scripts.get(location.get("scriptId")),
                            "lineNumber": location.get("lineNumber"),
                            "columnNumber": location.get("columnNumber"),
                        }
                    )

            channel.on("Debugger.scriptParsed", parsed)
            channel.on("Debugger.paused", paused)
            try:
                channel.send("Debugger.enable")
                channel.send("Debugger.pause")
                channel.send("Runtime.getIsolateId")
                record("paused_stack", nodeid, {"frames": stack})
                record_resource_tree(channel, nodeid, "resource_tree_paused")
            finally:
                try:
                    channel.send("Debugger.resume")
                    record("debugger_resumed", nodeid, {})
                except Exception as resume_error:
                    record(
                        "debugger_resume_error",
                        nodeid,
                        {"error_type": type(resume_error).__name__},
                    )
        if responsive:
            record_resource_tree(channel, nodeid, "resource_tree")
    except Exception as cdp_error:
        record("cdp_error", nodeid, {"error_type": type(cdp_error).__name__})
    finally:
        if channel:
            try:
                channel.detach()
            except Exception:
                pass
    record(
        "capture_complete",
        nodeid,
        {"seconds_after_original_failure": round(time.monotonic() - began, 4)},
    )


def record_navigation_failure(page, error):
    """Inspect the still-open failed page without replacing its navigation exception."""
    nodeid = os.environ.get("PYTEST_CURRENT_TEST", "").split(" (", 1)[0][:512]
    # The exception carries the requested URL even when navigation never committed.
    import re

    match = re.search(r'navigating to "([^"]+)"', str(error))
    target = match.group(1) if match else page.url
    _capture(page, target, nodeid, error)
    error.add_note(f"Jury navigation metadata: {OUTPUT}; Python threads: {THREADS}.")
