/* Code version: v1.0.0-codex.0 */

(function initializeWechatConversationRefresh() {
    "use strict";

    const button = document.querySelector("[data-wechat-conversation-refresh]");
    const banner = document.querySelector("[data-wechat-refresh-banner]");
    const bannerTitle = banner?.querySelector("[data-wechat-refresh-title]");
    const bannerCopy = banner?.querySelector("[data-wechat-refresh-copy]");
    const resultKey = "agenticContextWechatRefreshV1";
    const formatCount = (count) => new Intl.NumberFormat("en-US").format(count);
    const messageCount = (count) => `${formatCount(count)} message${count === 1 ? "" : "s"}`;
    const wait = (milliseconds) => new Promise((resolve) => window.setTimeout(resolve, milliseconds));
    let refreshing = false;

    function count(value) {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? Math.max(0, Math.floor(parsed)) : 0;
    }

    function showNotice(title, copy, failed = false) {
        if (!(banner instanceof HTMLElement)) return;
        banner.classList.toggle("error", failed);
        banner.classList.toggle("notice", !failed);
        if (bannerTitle) bannerTitle.textContent = title;
        if (bannerCopy) bannerCopy.textContent = copy;
        banner.hidden = false;
        banner.setAttribute("aria-hidden", "false");
    }

    function showResult(snapshot) {
        const added = count(snapshot.added);
        const total = count(snapshot.total);
        const summary = added
            ? `Added ${messageCount(added)}. The conversation now contains ${messageCount(total)}.`
            : `No new messages found. The conversation contains ${messageCount(total)}.`;
        showNotice(
            snapshot.partial ? "Chat history partly refreshed" : "Chat history refreshed",
            snapshot.partial ? `${summary} Some sources could not be refreshed.` : summary,
        );
    }

    function historyObject() {
        const previous = window.history.state;
        return previous === null || (typeof previous === "object" && !Array.isArray(previous))
            ? { ...previous } : null;
    }

    function restoreResult() {
        const previous = historyObject();
        const result = previous?.[resultKey];
        if (!result || result.version !== 1 || result.url !== window.location.href) return;
        delete previous[resultKey];
        try {
            window.history.replaceState(previous, "", window.location.href);
        } catch (_) {
            // A restricted history entry must not hide a completed refresh result.
        }
        showResult(result);
    }

    function rememberResult(snapshot) {
        const previous = historyObject();
        if (!previous) return;
        try {
            window.history.replaceState({
                ...previous,
                [resultKey]: {
                    version: 1,
                    url: window.location.href,
                    added: count(snapshot.added),
                    total: count(snapshot.total),
                    partial: snapshot.partial === true,
                },
            }, "", window.location.href);
        } catch (_) {
            // Refreshing the displayed records still works when history state is restricted.
        }
    }

    async function payload(response) {
        const value = await response.json().catch(() => null);
        return value && typeof value === "object" && !Array.isArray(value) ? value : {};
    }

    async function waitForRefresh(statusUrl) {
        const destination = new URL(statusUrl, window.location.href);
        if (destination.origin !== window.location.origin || !destination.pathname.startsWith("/api/")) {
            throw new Error("The local service returned an invalid refresh status address.");
        }
        let failedReads = 0;
        while (true) {
            await wait(1_000);
            let response;
            let snapshot;
            try {
                response = await fetch(destination.href, {
                    cache: "no-store",
                    headers: { Accept: "application/json" },
                });
                snapshot = await payload(response);
            } catch (_) {
                failedReads += 1;
                if (failedReads >= 5) {
                    throw new Error("Lost contact with the local service while refreshing chat history.");
                }
                continue;
            }
            if (!response.ok) {
                throw new Error(snapshot.error || "Unable to read the chat refresh status.");
            }
            failedReads = 0;
            if (snapshot.state === "running") continue;
            if (snapshot.state === "succeeded") return snapshot;
            throw new Error(snapshot.error || snapshot.message || "The chat history refresh failed.");
        }
    }

    restoreResult();
    if (!(button instanceof HTMLButtonElement)) return;

    button.addEventListener("click", async () => {
        if (refreshing || button.disabled) return;
        const endpoint = button.dataset.wechatRefreshUrl;
        const sessionId = button.dataset.wechatSessionId;
        if (!endpoint || !sessionId) return;
        refreshing = true;
        button.disabled = true;
        button.setAttribute("aria-busy", "true");
        const idleLabel = button.getAttribute("aria-label") || "Refresh chat history";
        const idleTitle = button.title;
        button.setAttribute("aria-label", "Refreshing chat history…");
        button.title = "Refreshing chat history…";
        if (banner instanceof HTMLElement) banner.hidden = true;
        const notice = window.CacheWaitModal?.show({
            title: "Refreshing chat history",
            copy: "Checking this conversation for new messages.",
        });

        try {
            const response = await fetch(endpoint, {
                method: "POST",
                cache: "no-store",
                headers: { Accept: "application/json", "Content-Type": "application/json" },
                body: JSON.stringify({ session_id: sessionId }),
            });
            const started = await payload(response);
            if (!response.ok) {
                throw new Error(started.error || "Unable to start the chat history refresh.");
            }
            const result = started.state === "succeeded"
                ? started : await waitForRefresh(started.status_url || "");
            rememberResult(result);
            window.location.reload();
        } catch (error) {
            notice?.finish();
            refreshing = false;
            button.disabled = false;
            button.removeAttribute("aria-busy");
            button.setAttribute("aria-label", idleLabel);
            button.title = idleTitle;
            showNotice(
                "Unable to refresh chat history",
                error instanceof Error ? error.message : "The chat history refresh failed.",
                true,
            );
        }
    });
})();
