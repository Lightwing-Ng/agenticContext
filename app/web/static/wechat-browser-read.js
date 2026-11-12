/* Code version: v1.1.1-codex.0 */

(function initializeWechatReadPage() {
    "use strict";
    const root = document.querySelector("[data-wechat-read-pending]");
    const guardKey = "agenticContextWechatReadReloadV1";
    if (!root) {
        const state = window.history.state;
        if (state && typeof state === "object" && state[guardKey]) {
            const next = { ...state };
            delete next[guardKey];
            window.history.replaceState(next, "", window.location.href);
        }
        return;
    }
    const originalUrl = window.location.href;
    const title = root.querySelector("[data-wechat-read-title]");
    const copy = root.querySelector("[data-wechat-read-copy]");
    const retry = root.querySelector("[data-wechat-read-retry]");
    const placeholders = root.closest(".browser-workspace").querySelectorAll("[data-wechat-read-placeholder]");
    let stopped = false;
    let reads = 0;
    let failedReads = 0;
    let statusUrl = root.dataset.statusUrl;
    let pollRun = 0;
    let resumable = root.dataset.state === "running" && Boolean(statusUrl);
    const wait = (milliseconds) => new Promise((resolve) => window.setTimeout(resolve, milliseconds));

    function address(value) {
        const url = new URL(value, originalUrl);
        if (url.origin !== window.location.origin || !url.pathname.startsWith("/api/browser/wechat/read/")) {
            throw new Error("The saved-view status address is unavailable.");
        }
        return url.href;
    }

    function requestTask(value, method = "GET") {
        return fetch(address(value), {
            method,
            cache: "no-store",
            headers: { Accept: "application/json" },
            signal: AbortSignal.timeout(10_000),
        });
    }

    function fail(message) {
        resumable = false;
        root.dataset.state = "failed";
        for (const placeholder of placeholders) {
            placeholder.hidden = true;
            placeholder.removeAttribute("aria-busy");
        }
        if (title) title.hidden = false;
        if (copy) copy.textContent = message;
        if (retry) retry.hidden = false;
    }

    function canReloadCurrentView() {
        return !stopped && window.location.href === originalUrl
            && !document.documentElement.hasAttribute("data-navigation-target");
    }

    function ready(snapshot) {
        if (!canReloadCurrentView()) return;
        const previous = window.history.state;
        if (previous && typeof previous === "object" && previous[guardKey]?.url === originalUrl) {
            fail("Saved records are still being checked. Try loading this view again.");
            return;
        }
        window.history.replaceState({
            ...(previous && typeof previous === "object" ? previous : {}),
            [guardKey]: { url: originalUrl, task_id: snapshot.task_id, generation: snapshot.generation },
        }, "", originalUrl);
        stopped = true;
        resumable = false;
        window.location.reload();
    }

    async function poll() {
        const run = ++pollRun;
        while (!stopped && run === pollRun && window.location.href === originalUrl && reads < 1_800) {
            reads += 1;
            try {
                const response = await requestTask(statusUrl);
                const snapshot = await response.json();
                if (stopped || run !== pollRun || window.location.href !== originalUrl) return;
                if (!response.ok) throw new Error("This saved-view task has expired. Try loading the view again.");
                if (snapshot.task_id !== root.dataset.taskId || snapshot.generation !== Number(root.dataset.generation)) {
                    fail("The saved-view task changed. Try loading this view again.");
                    return;
                }
                failedReads = 0;
                if (snapshot.state === "succeeded") { ready(snapshot); return; }
                if (snapshot.state !== "running") { fail(snapshot.message || "Saved records could not be checked. Try again."); return; }
            } catch (_) {
                if (stopped || run !== pollRun || window.location.href !== originalUrl) return;
                failedReads += 1;
                if (failedReads >= 5) { fail("Lost contact with the local service. Try loading this view again."); return; }
            }
            await wait(reads < 4 ? 250 : 1_000);
        }
        if (run !== pollRun) return;
        if (!stopped && window.location.href === originalUrl) fail("Saved records are still being checked. Try loading this view again.");
    }

    retry?.addEventListener("click", async () => {
        retry.disabled = true;
        try {
            if (root.dataset.retryUrl) {
                await requestTask(root.dataset.retryUrl, "POST");
            }
            if (!canReloadCurrentView()) {
                retry.disabled = false;
                return;
            }
            const previous = window.history.state;
            if (previous && typeof previous === "object") {
                const next = { ...previous };
                delete next[guardKey];
                window.history.replaceState(next, "", originalUrl);
            }
            stopped = true;
            window.location.reload();
        } catch (_) {
            retry.disabled = false;
            fail("The local service is unavailable. Try again shortly.");
        }
    });
    window.addEventListener("pagehide", () => { stopped = true; pollRun += 1; });
    window.addEventListener("pageshow", (event) => {
        if (!event.persisted || window.location.href !== originalUrl) return;
        stopped = false;
        if (resumable) poll();
    });
    if (root.dataset.state === "failed" || !statusUrl) fail(copy?.textContent || "Try loading this view again.");
    else poll();
})();
