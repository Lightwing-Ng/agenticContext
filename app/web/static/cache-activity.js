/* Code version: v1.1.0-codex.0 */

(() => {
    "use strict";

    const root = document.querySelector("[data-cache-activity]");
    const owner = document.querySelector(".cache-workspace-content");
    const theme = document.querySelector('[data-layout-role="global-theme-anchor"]');
    if (!root || !owner || !theme) return;
    const trigger = root.querySelector("[data-cache-activity-trigger]");
    const panel = root.querySelector("[data-cache-activity-panel]");
    const list = root.querySelector("[data-cache-activity-tasks]");
    const count = root.querySelector("[data-cache-activity-count]");
    const unavailable = root.querySelector("[data-cache-activity-unavailable]");
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

    function measure() {
        layoutFrame = 0;
        if (root.hidden) return;
        const box = owner.getBoundingClientRect();
        const anchor = theme.getBoundingClientRect();
        const gap = Math.max(0, box.right - anchor.right);
        setStyle("right", `${window.innerWidth - anchor.right}px`);
        setStyle("bottom", `${window.innerHeight - box.bottom + gap}px`);
        setStyle("--cache-activity-available-width", `${Math.max(anchor.width, box.width - gap * 2)}px`);
        setStyle("--cache-activity-available-height", `${Math.max(anchor.height, box.height - gap * 2)}px`);
        setStyle("--cache-activity-panel-height", `${panel.offsetHeight}px`);
    }

    function scheduleMeasure() {
        if (!layoutFrame) layoutFrame = window.requestAnimationFrame(measure);
    }

    function setOpen(next, restoreFocus = false) {
        open = next && !root.hidden;
        root.dataset.open = String(open);
        trigger.setAttribute("aria-expanded", String(open));
        trigger.setAttribute("aria-label", open ? "Hide running cache tasks" : "Show running cache tasks");
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
        meter.className = "cache-training-progress cache-activity-meter";
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
        return { row, title, meta, progress, track, fill };
    }

    function render(tasks) {
        const ids = new Set();
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
            setText(item.title, label);
            setText(item.meta, String(task.message || "Cache task in progress."));
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
        setText(count, formatter.format(rows.size));
        root.dataset.stale = "false";
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
    observer.observe(owner);
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
