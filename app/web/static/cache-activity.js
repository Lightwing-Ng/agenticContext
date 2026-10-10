/* Code version: v1.2.0-claude.0 */

(() => {
    "use strict";

    const root = document.querySelector("[data-cache-activity]");
    const theme = document.querySelector('[data-layout-role="global-theme-anchor"]');
    if (!root || !theme) return;
    const trigger = root.querySelector("[data-cache-activity-trigger]");
    const panel = root.querySelector("[data-cache-activity-panel]");
    const list = root.querySelector("[data-cache-activity-tasks]");
    const count = root.querySelector("[data-cache-activity-count]");
    const unavailable = root.querySelector("[data-cache-activity-unavailable]");
    // The entry sits in the corner of the workspace panel, so it keeps one place on every
    // page. A surface pinned to that corner, such as a composer, asks for clearance.
    const workspaceSelector = ".workspace";
    const clearanceSelector = "[data-cache-activity-clearance]";
    const scrollportSelector = '[data-layout-role="content-scrollport"]';
    const titleRailSelector = '[data-layout-role="title-rail"]';
    const formatter = new Intl.NumberFormat("en-US");
    const rows = new Map();
    let open = false;
    let pollTimer = 0;
    let layoutFrame = 0;
    let request = null;
    let stopped = false;

    function setText(node, text) {
        if (node.textContent !== text) node.textContent = text;
    }

    function nonnegativeCount(value) {
        const number = Number(value);
        return Number.isFinite(number) ? Math.max(0, number) : 0;
    }

    function setStyle(name, value) {
        if (root.style.getPropertyValue(name) !== value) root.style.setProperty(name, value);
    }

    function renderedBox(selector) {
        for (const element of document.querySelectorAll(selector)) {
            const box = element.getBoundingClientRect();
            if (box.width > 0 && box.height > 0) return box;
        }
        return null;
    }

    function resolveGeometry() {
        const viewport = { left: 0, top: 0, right: window.innerWidth, bottom: window.innerHeight };
        const clamp = (box, limit) => ({
            left: Math.max(box.left, limit.left),
            top: Math.max(box.top, limit.top),
            right: Math.min(box.right, limit.right),
            bottom: Math.min(box.bottom, limit.bottom),
        });
        const workspace = clamp(renderedBox(workspaceSelector) || viewport, viewport);
        const pinned = renderedBox(clearanceSelector);
        const position = pinned
            ? { ...workspace, bottom: Math.min(workspace.bottom, Math.max(workspace.top, pinned.top)) }
            : workspace;
        // The open surface stays below the title rail. A scrollport's bleed beyond the
        // workspace panel is paint room, not space the surface may occupy.
        const scrollport = renderedBox(scrollportSelector);
        const titleRail = renderedBox(titleRailSelector);
        const bounds = scrollport
            ? clamp(scrollport, position)
            : { ...position, top: Math.min(position.bottom, Math.max(position.top, titleRail?.bottom ?? position.top)) };
        return { workspace, position, bounds };
    }

    function measure() {
        layoutFrame = 0;
        if (root.hidden) return;
        const { workspace, position, bounds } = resolveGeometry();
        const anchor = theme.getBoundingClientRect();
        // The theme action's clearance from the workspace edge is the one clearance the
        // entry repeats below itself, whichever surface it belongs to.
        const gap = Math.max(0, workspace.right - anchor.right);
        const bottom = position.bottom - gap;
        setStyle("right", `${window.innerWidth - anchor.right}px`);
        setStyle("bottom", `${window.innerHeight - bottom}px`);
        setStyle("--cache-activity-available-width", `${Math.max(anchor.width, anchor.right - bounds.left - gap)}px`);
        setStyle("--cache-activity-available-height", `${Math.max(anchor.height, bottom - bounds.top - gap)}px`);
        setStyle("--cache-activity-panel-height", `${panel.offsetHeight}px`);
    }

    function scheduleMeasure() {
        if (!layoutFrame) layoutFrame = window.requestAnimationFrame(measure);
    }

    function setOpen(next, restoreFocus = false) {
        open = next && !root.hidden;
        root.dataset.open = String(open);
        trigger.setAttribute("aria-expanded", String(open));
        trigger.setAttribute("aria-label", open ? "Hide cache tasks" : "Show cache tasks");
        panel.setAttribute("aria-hidden", String(!open));
        panel.inert = !open;
        if (open) {
            measure();
            panel.focus({ preventScroll: true });
        } else if (restoreFocus && !root.hidden) {
            trigger.focus({ preventScroll: true });
        }
    }

    function createRow(task) {
        const row = document.createElement("li");
        row.className = "cache-activity-task";
        row.dataset.cacheActivityTask = "";
        row.dataset.taskId = task.id;
        const title = document.createElement("span");
        title.className = "cache-activity-task-title";
        const meta = document.createElement("p");
        meta.className = "cache-activity-meta";
        const progress = document.createElement("p");
        progress.className = "cache-activity-progress";
        const meter = document.createElement("div");
        meter.className = "cache-activity-meter";
        const track = document.createElement("div");
        track.className = "status-progress cache-activity-progress-track";
        track.setAttribute("role", "progressbar");
        track.setAttribute("aria-valuemin", "0");
        track.setAttribute("aria-valuemax", "100");
        const fill = document.createElement("span");
        fill.className = "status-progress-fill";
        track.append(fill);
        meter.append(track);
        row.append(title, meta, meter, progress);
        list.append(row);
        return { row, title, meta, meter, progress, track, fill };
    }

    function render(tasks) {
        const ids = new Set();
        let runningCount = 0;
        for (const task of tasks) {
            if (!task || typeof task.id !== "string" || ids.has(task.id)) continue;
            ids.add(task.id);
            let item = rows.get(task.id);
            if (!item) {
                item = createRow(task);
                rows.set(task.id, item);
            }
            const mode = task.content_mode === "media" ? "Media" : task.content_mode === "text" ? "Text" : "";
            const label = `${task.label || "Cache"}${mode ? ` · ${mode}` : ""}`;
            // A queued task has not started, so it shows why it waits instead of a meter.
            const queued = task.phase === "queued";
            if (!queued) runningCount += 1;
            item.row.dataset.taskState = queued ? "queued" : "running";
            setText(item.title, label);
            setText(item.meta, String(task.message || (queued ? "Queued. Waiting to start." : "Cache task in progress.")));
            item.meter.hidden = queued;
            item.progress.hidden = queued;
            const processed = nonnegativeCount(task.processed);
            const total = nonnegativeCount(task.total);
            const units = { sessions: "sessions", conversations: "sessions", images: "images", answers: "answers", resources: "resources", items: "items" };
            const unit = units[task.unit] || "items";
            const measured = total > 0;
            const percent = measured ? Math.min(100, processed / total * 100) : 0;
            const progressText = measured
                ? `${formatter.format(processed)} / ${formatter.format(total)} ${unit} processed (${Math.round(percent)}%)`
                : processed > 0 ? `${formatter.format(processed)} ${unit} processed` : "Waiting for a work-item total.";
            setText(item.progress, progressText);
            item.track.setAttribute("aria-label", `${label} cache progress`);
            item.track.classList.toggle("is-indeterminate", !measured);
            item.track.setAttribute("aria-valuetext", !measured && processed > 0
                ? `${progressText}; total not yet known.` : progressText);
            if (measured) {
                item.track.setAttribute("aria-valuenow", String(Math.round(percent * 100) / 100));
                item.fill.style.width = `${percent}%`;
            } else {
                item.track.removeAttribute("aria-valuenow");
                item.fill.style.removeProperty("width");
            }
        }
        for (const [id, item] of rows) {
            if (!ids.has(id)) {
                item.row.remove();
                rows.delete(id);
            }
        }
        // Rows follow the server order: running tasks first, then the queue.
        let position = 0;
        for (const id of ids) {
            const row = rows.get(id).row;
            if (list.children[position] !== row) list.insertBefore(row, list.children[position] || null);
            position += 1;
        }
        setText(count, formatter.format(rows.size));
        root.dataset.stale = "false";
        root.dataset.running = String(runningCount > 0);
        unavailable.hidden = true;
        if (!rows.size) {
            const hadFocus = root.contains(document.activeElement);
            setOpen(false);
            root.hidden = true;
            if (hadFocus) theme.focus({ preventScroll: true });
        } else {
            root.hidden = false;
            measure();
        }
    }

    async function refresh() {
        if (request || document.hidden || stopped) return;
        window.clearTimeout(pollTimer);
        request = new AbortController();
        const timeout = window.setTimeout(() => request?.abort(), 10_000);
        try {
            const response = await fetch(root.dataset.activityUrl, { cache: "no-store", signal: request.signal });
            if (!response.ok) throw new Error("Activity is unavailable");
            const data = await response.json();
            if (!Array.isArray(data.tasks)) throw new Error("Activity is unavailable");
            render(data.tasks);
        } catch (_error) {
            if (!root.hidden) {
                root.dataset.stale = "true";
                unavailable.hidden = false;
                scheduleMeasure();
            }
        } finally {
            window.clearTimeout(timeout);
            request = null;
            if (!document.hidden && !stopped) pollTimer = window.setTimeout(refresh, 3_000);
        }
    }

    trigger.addEventListener("click", () => setOpen(!open));
    document.addEventListener("pointerdown", (event) => {
        if (open && !root.contains(event.target)) setOpen(false);
    });
    document.addEventListener("keydown", (event) => {
        if (open && event.key === "Escape") {
            event.preventDefault();
            setOpen(false, true);
        }
    });
    document.addEventListener("visibilitychange", () => {
        window.clearTimeout(pollTimer);
        if (!document.hidden) void refresh();
    });
    const observer = new ResizeObserver(scheduleMeasure);
    // A hidden surface reports a size change when its page switches to it.
    document.querySelectorAll(
        `${workspaceSelector}, ${clearanceSelector}, ${scrollportSelector}, ${titleRailSelector}`,
    ).forEach((element) => observer.observe(element));
    observer.observe(theme);
    observer.observe(panel);
    window.addEventListener("resize", scheduleMeasure);
    window.addEventListener("pageshow", () => { stopped = false; void refresh(); });
    window.addEventListener("pagehide", () => {
        stopped = true;
        window.clearTimeout(pollTimer);
        request?.abort();
    });
    void refresh();
})();
