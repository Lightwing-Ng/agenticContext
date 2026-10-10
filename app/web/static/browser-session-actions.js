/* Code version: v1.2.0-codex.0 */

(function initializeBrowserSessionActions() {
    "use strict";

    const root = document.querySelector("[data-browser-session-actions]");
    if (!(root instanceof HTMLElement)) return;

    const openOriginalButton = root.querySelector("[data-browser-session-open-original]");
    if (openOriginalButton instanceof HTMLButtonElement) {
        openOriginalButton.addEventListener("click", () => {
            const originalUrl = openOriginalButton.dataset.browserSessionOriginalUrl;
            if (originalUrl) window.open(originalUrl, "_blank", "noopener,noreferrer");
        });
    }

    root.querySelectorAll("[data-browser-session-refresh-url]").forEach((button) => {
        if (!(button instanceof HTMLButtonElement)) return;
        button.addEventListener("click", () => {
            const refreshUrl = button.dataset.browserSessionRefreshUrl;
            if (refreshUrl) window.location.assign(refreshUrl);
        });
    });

    document.querySelectorAll("[data-browser-session-download-url]").forEach((button) => {
        if (!(button instanceof HTMLButtonElement)) return;
        button.addEventListener("click", () => {
            const downloadUrl = button.dataset.browserSessionDownloadUrl;
            if (downloadUrl) window.location.assign(downloadUrl);
        });
    });

    const zhihuRefreshEndpoint = "/api/browser/zhihu/answerer/refresh";
    const zhihuRefreshButton = root.querySelector("[data-browser-zhihu-answerer-refresh]");
    const zhihuRefreshBanner = document.querySelector("[data-zhihu-answerer-refresh-banner]");
    const zhihuRefreshBannerTitle = zhihuRefreshBanner?.querySelector("[data-zhihu-answerer-refresh-title]");
    const zhihuRefreshBannerCopy = zhihuRefreshBanner?.querySelector("[data-zhihu-answerer-refresh-copy]");
    const zhihuRefreshResultParameters = Object.freeze({
        added: "answerer_added",
        changed: "answerer_changed",
        checked: "answerer_checked",
    });

    const wait = (milliseconds) => new Promise((resolve) => {
        window.setTimeout(resolve, milliseconds);
    });

    const formatCount = (count) => new Intl.NumberFormat("en-US").format(count);
    const answerCountLabel = (count) => `${formatCount(count)} answer${count === 1 ? "" : "s"}`;
    const wasOrWere = (count) => (count === 1 ? "was" : "were");
    const nonnegativeCount = (value) => Math.max(0, Number(value) || 0);

    function showZhihuRefreshResult() {
        if (!(zhihuRefreshBanner instanceof HTMLElement)) return;
        const currentUrl = new URL(window.location.href);
        if (!currentUrl.searchParams.has(zhihuRefreshResultParameters.added)) return;

        const readCount = (parameter) => {
            const parsedValue = Number.parseInt(currentUrl.searchParams.get(parameter) ?? "", 10);
            return Number.isFinite(parsedValue) ? Math.max(0, parsedValue) : null;
        };
        const addedCount = readCount(zhihuRefreshResultParameters.added) ?? 0;
        const changedCount = readCount(zhihuRefreshResultParameters.changed) ?? 0;
        const checkedCount = readCount(zhihuRefreshResultParameters.checked);
        const answererLabel = zhihuRefreshButton?.dataset.browserZhihuAnswererLabel?.trim()
            || "the current answerer";

        let title = "No new answers found";
        let summary = `No answers newer than the local text cache were found for Zhihu answerer ${answererLabel}.`;
        if (addedCount) {
            title = `Added ${answerCountLabel(addedCount)}`;
            summary = `Pulled ${answerCountLabel(addedCount)} from Zhihu answerer ${answererLabel} `
                + `and added ${addedCount === 1 ? "it" : "them"} to the local text cache.`;
        }

        // Rows Zhihu re-serves with different markup (for example, another image host) count
        // as refreshed, never as new answers, so they only qualify the already-cached count.
        const detailParts = [];
        if (checkedCount) {
            const cachedCount = Math.max(0, checkedCount - addedCount);
            let detail = `Checked the newest ${answerCountLabel(checkedCount)}`;
            if (cachedCount) {
                const cachedLabel = cachedCount === checkedCount
                    ? (checkedCount === 1 ? "it was" : "all were")
                    : `${formatCount(cachedCount)} ${wasOrWere(cachedCount)}`;
                detail += `; ${cachedLabel} already cached`;
                if (changedCount) detail += ` (${formatCount(changedCount)} refreshed)`;
            }
            detailParts.push(`${detail}.`);
        }

        if (zhihuRefreshBannerTitle) zhihuRefreshBannerTitle.textContent = title;
        if (zhihuRefreshBannerCopy) zhihuRefreshBannerCopy.textContent = [summary, ...detailParts].join(" ");
        zhihuRefreshBanner.hidden = false;
        Object.values(zhihuRefreshResultParameters)
            .forEach((parameter) => currentUrl.searchParams.delete(parameter));
        window.history.replaceState({}, "", currentUrl.toString());
    }

    showZhihuRefreshResult();

    async function waitForZhihuRefresh(statusUrl) {
        let failedReads = 0;
        while (true) {
            await wait(1_000);
            let snapshot = null;
            try {
                const statusResponse = await fetch(statusUrl, {
                    cache: "no-store",
                    headers: { Accept: "application/json" },
                });
                if (statusResponse.ok) snapshot = await statusResponse.json();
            } catch (_error) {
                snapshot = null;
            }
            if (snapshot === null) {
                failedReads += 1;
                if (failedReads >= 5) {
                    throw new Error("Lost contact with the local service while updating Zhihu answers.");
                }
                continue;
            }
            failedReads = 0;
            if (snapshot.running) continue;
            if (snapshot.last_error || snapshot.phase === "failed") {
                throw new Error(snapshot.last_error || "The Zhihu answer update failed.");
            }
            if (snapshot.phase === "stopped") {
                throw new Error("The Zhihu answer update was stopped before it finished.");
            }
            return snapshot.performance_metrics || {};
        }
    }

    async function refreshZhihuAnswerer(button) {
        const profileUrl = button.dataset.browserZhihuAnswererUrl || "";
        if (!profileUrl || button.disabled) return;

        const idleLabel = button.getAttribute("aria-label") || "";
        const waitNotice = window.CacheWaitModal?.show({
            title: "Updating Zhihu answers",
            copy: "Checking Zhihu for this answerer’s newest answers. The browser stays in the background.",
        });
        button.disabled = true;
        button.setAttribute("aria-busy", "true");
        button.setAttribute("aria-label", "Updating answers…");
        button.title = "Updating answers…";

        try {
            const startResponse = await fetch(zhihuRefreshEndpoint, {
                method: "POST",
                cache: "no-store",
                headers: {
                    Accept: "application/json",
                    "Content-Type": "application/json",
                },
                body: JSON.stringify({ profile_url: profileUrl }),
            });
            const startPayload = await startResponse.json().catch(() => ({}));
            if (!startResponse.ok) {
                throw new Error(startPayload.error || "Unable to start the Zhihu answer update.");
            }

            const metrics = await waitForZhihuRefresh(startPayload.status_url || "/api/zhihu/status");
            const refreshedUrl = new URL(window.location.href);
            refreshedUrl.searchParams.set(
                zhihuRefreshResultParameters.added,
                String(nonnegativeCount(metrics.added)),
            );
            refreshedUrl.searchParams.set(
                zhihuRefreshResultParameters.changed,
                String(nonnegativeCount(metrics.changed)),
            );
            refreshedUrl.searchParams.set(
                zhihuRefreshResultParameters.checked,
                String(nonnegativeCount(metrics.available_answers)),
            );
            window.location.assign(refreshedUrl.toString());
        } catch (error) {
            waitNotice?.finish();
            button.disabled = false;
            button.removeAttribute("aria-busy");
            button.setAttribute("aria-label", idleLabel);
            button.title = idleLabel;
            window.alert(error instanceof Error ? error.message : "Unable to update this answerer from Zhihu.");
        }
    }

    if (zhihuRefreshButton instanceof HTMLButtonElement) {
        zhihuRefreshButton.addEventListener("click", () => {
            refreshZhihuAnswerer(zhihuRefreshButton);
        });
    }
})();
