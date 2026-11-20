/* Code version: v1.5.0-codex.0 */

(function initializeWechatRefresh() {
    "use strict";

    const button = document.querySelector("[data-wechat-conversation-refresh], [data-wechat-account-refresh]");
    const banner = document.querySelector("[data-wechat-refresh-banner]");
    const bannerTitle = banner?.querySelector("[data-wechat-refresh-title]");
    const bannerCopy = banner?.querySelector("[data-wechat-refresh-copy]");
    const pendingReadSelector = '[data-wechat-read-pending][data-state="running"]';
    const resultKey = "agenticContextWechatRefreshV1";
    const contactReturnKey = "agenticContextWechatContactReturnV1";
    const formatCount = (count) => new Intl.NumberFormat("en-US").format(count);
    const messageCount = (count) => `${formatCount(count)} message${count === 1 ? "" : "s"}`;
    const wait = (milliseconds) => new Promise((resolve) => window.setTimeout(resolve, milliseconds));
    const memberSnapshotMessages = {
        saved: "Member snapshot saved.",
        unavailable: "Local member list unavailable.",
        failed: "Member snapshot could not be saved.",
    };
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
        const upgraded = count(snapshot.upgraded);
        const reparsed = count(snapshot.reparsed);
        const total = count(snapshot.total);
        const actions = [];
        if (added) actions.push(`Added ${messageCount(added)}.`);
        if (upgraded) actions.push(`Restored ${formatCount(upgraded)} message placeholder${upgraded === 1 ? "" : "s"}.`);
        if (reparsed) actions.push(`Repaired details for ${messageCount(reparsed)}.`);
        if (!added && !upgraded && !reparsed) {
            actions.push(snapshot.partial ? "No new messages were added." : "No new messages found.");
        }
        if (!snapshot.account) {
            actions.push(`The conversation ${added ? "now contains" : "contains"} ${messageCount(total)}.`);
        }
        if (snapshot.partial) {
            const coverage = coverageDetails(snapshot);
            actions.push(...(coverage.length ? coverage : ["Some sources could not be refreshed."]));
        }
        const memberSnapshot = memberSnapshotMessage(snapshot);
        if (memberSnapshot) actions.push(memberSnapshot);
        const subject = snapshot.account ? "WeChat account" : "Chat history";
        showNotice(`${subject}${snapshot.partial ? " partly refreshed" : " refreshed"}`, actions.join("\n"));
    }

    function historyObject() {
        const previous = window.history.state;
        return previous === null || (typeof previous === "object" && !Array.isArray(previous))
            ? { ...previous } : null;
    }

    function clearSavedResult() {
        const previous = historyObject();
        const result = previous?.[resultKey];
        if (!result || result.version !== 1 || result.url !== window.location.href) return;
        delete previous[resultKey];
        try {
            window.history.replaceState(previous, "", window.location.href);
        } catch (_) {
            // A restricted history entry must not hide a completed refresh result.
        }
    }

    function restoreResult() {
        if (!(banner instanceof HTMLElement)) return;
        const result = historyObject()?.[resultKey];
        if (!result || result.version !== 1 || result.url !== window.location.href) return;
        if (!(result.account && result.error) && !document.querySelector(pendingReadSelector)) clearSavedResult();
        if (result.error) {
            showNotice(`Unable to refresh ${result.account ? "WeChat account" : "chat history"}`, result.error, true);
        } else {
            showResult(result);
        }
        if (result.followLatest === true) {
            const scrollport = document.querySelector("[data-wechat-resources] [data-chat-scrollport]");
            const scrollToLatest = () => window.requestAnimationFrame(() => {
                if (scrollport && window.location.href === result.url) {
                    scrollport.scrollTop = scrollport.scrollHeight;
                }
            });
            if (document.readyState === "complete") scrollToLatest();
            else window.addEventListener("load", scrollToLatest, { once: true });
        }
    }

    function saveResult(destination, result) {
        const previous = historyObject();
        if (!previous) return;
        try {
            window.history.replaceState({
                ...previous, [resultKey]: { version: 1, url: destination, ...result },
            }, "", destination);
        } catch (_) {
            // Open the updated records even if this entry cannot store the result.
        }
    }

    function reloadAfterRefresh(snapshot, account) {
        const previous = historyObject();
        const destination = new URL(window.location.href);
        const pageSize = count(button.dataset.wechatRefreshFollowPageSize);
        const total = count(snapshot.total);
        const failed = snapshot.state === "failed";
        const followLatest = !failed && !account && pageSize > 0 && count(snapshot.added) > 0 && total > 0
            && !destination.hash && previous?.[contactReturnKey]?.url !== destination.href;
        if (followLatest) {
            const latestPage = Math.ceil(total / pageSize);
            const requestedPage = count(destination.searchParams.get("page")) || 1;
            if (latestPage !== requestedPage) destination.searchParams.set("page", String(latestPage));
        }
        saveResult(destination.href, {
            account,
            followLatest,
            added: count(snapshot.added),
            upgraded: count(snapshot.upgraded),
            reparsed: count(snapshot.reparsed),
            total,
            partial: snapshot.partial === true,
            skipped_parts: count(snapshot.skipped_parts),
            unavailable_parts: count(snapshot.unavailable_parts),
            excluded: count(snapshot.excluded),
            member_snapshot: snapshot.member_snapshot,
            error: failed ? refreshError(snapshot) : undefined,
        });
        if (destination.href === window.location.href) window.location.reload();
        else window.location.replace(destination.href);
    }

    async function payload(response) {
        const value = await response.json().catch(() => null);
        return value && typeof value === "object" && !Array.isArray(value) ? value : {};
    }

    function progressCopy(snapshot, account) {
        const total = count(snapshot.parts_total);
        const current = count(snapshot.part_current);
        const completed = count(snapshot.parts_completed);
        const source = current > 0 && current <= total ? `source ${formatCount(current)} of ${formatCount(total)}` : "available chat sources";
        const phases = {
            preparing: account ? "Preparing chat sources for this account." : "Preparing available chat sources.",
            members: "Saving the local group member snapshot.",
            reading: `Reading ${source}.`,
            comparing: `Comparing messages in ${source}.`,
            checking: "Validating possible changes.",
            saving: "Saving verified changes.",
            verifying: account ? "Checking the updated account." : "Checking the updated conversation.",
            complete: account ? "WeChat account refreshed." : "Chat history refreshed.",
        };
        const phase = Object.hasOwn(phases, snapshot.phase) ? snapshot.phase : "preparing";
        const lines = [phases[phase]];
        if (total && !["preparing", "reading", "comparing"].includes(phase)) {
            lines.push(`${formatCount(Math.min(completed, total))} of ${formatCount(total)} sources processed.`);
        }
        if (phase !== "preparing") {
            lines.push(`${messageCount(count(snapshot.checked))} checked.`);
            lines.push(`${formatCount(count(snapshot.candidates))} possible update${count(snapshot.candidates) === 1 ? "" : "s"} identified.`);
        }
        lines.push(...coverageDetails(snapshot));
        return lines.join(" ");
    }

    function coverageDetails(snapshot) {
        const lines = [];
        const skipped = count(snapshot.skipped_parts);
        if (skipped) lines.push(`${formatCount(skipped)} source${skipped === 1 ? "" : "s"} could not be verified.`);
        const unavailable = count(snapshot.unavailable_parts);
        if (unavailable) lines.push(`${formatCount(unavailable)} additional source${unavailable === 1 ? " needs" : "s need"} verified keys.`);
        const excluded = count(snapshot.excluded);
        if (excluded) lines.push(`${formatCount(excluded)} record${excluded === 1 ? "" : "s"} could not be processed.`);
        return lines;
    }

    function memberSnapshotMessage(snapshot) {
        const status = snapshot.member_snapshot;
        return typeof status === "string" && Object.hasOwn(memberSnapshotMessages, status)
            ? memberSnapshotMessages[status]
            : "";
    }

    function refreshError(snapshot) {
        const message = snapshot.error || snapshot.message || "The chat history refresh failed.";
        const memberSnapshot = memberSnapshotMessage(snapshot);
        return memberSnapshot ? `${message}\n${memberSnapshot}` : message;
    }

    async function waitForRefresh(statusUrl, onProgress) {
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
            onProgress(snapshot);
            if (snapshot.state === "running") continue;
            if (snapshot.state === "succeeded" || snapshot.state === "failed") return snapshot;
            throw new Error(refreshError(snapshot));
        }
    }

    banner?.querySelector("[data-wechat-refresh-dismiss]")?.addEventListener("click", clearSavedResult);
    restoreResult();
    if (!(button instanceof HTMLButtonElement)) return;

    button.addEventListener("click", async () => {
        if (refreshing || button.disabled) return;
        const endpoint = button.dataset.wechatRefreshUrl;
        const sessionId = button.dataset.wechatSessionId;
        const accountId = button.dataset.wechatAccountId;
        if (!endpoint || !(accountId || sessionId)) return;
        const account = Boolean(accountId);
        const scope = account ? "WeChat account" : "chat history";
        const readRetry = account && document.querySelector("[data-wechat-read-retry]");
        if (account) clearSavedResult();
        refreshing = true;
        button.disabled = true;
        if (readRetry) readRetry.disabled = true;
        button.setAttribute("aria-busy", "true");
        const idleLabel = button.getAttribute("aria-label") || `Refresh ${scope}`;
        const idleTitle = button.title;
        button.setAttribute("aria-label", `Refreshing ${scope}…`);
        button.title = `Refreshing ${scope}…`;
        if (banner instanceof HTMLElement) banner.hidden = true;
        const notice = window.CacheWaitModal?.show({
            title: `Refreshing ${scope}`,
            copy: progressCopy({ phase: "preparing" }, account),
        });
        const showProgress = (snapshot) => notice?.update?.({ copy: progressCopy(snapshot, account) });

        try {
            const response = await fetch(endpoint, {
                method: "POST",
                cache: "no-store",
                headers: { Accept: "application/json", "Content-Type": "application/json" },
                body: JSON.stringify(account ? { account_id: accountId } : { session_id: sessionId }),
            });
            const started = await payload(response);
            if (!response.ok) {
                throw new Error(started.error || `Unable to start the ${scope} refresh.`);
            }
            showProgress(started);
            const result = ["succeeded", "failed"].includes(started.state)
                ? started : await waitForRefresh(started.status_url || "", showProgress);
            if (result.state === "failed" && result.member_snapshot !== "saved") {
                throw new Error(refreshError(result));
            }
            reloadAfterRefresh(result, account);
        } catch (error) {
            notice?.finish();
            refreshing = false;
            button.disabled = false;
            if (readRetry) readRetry.disabled = false;
            button.removeAttribute("aria-busy");
            button.setAttribute("aria-label", idleLabel);
            button.title = idleTitle;
            const message = error instanceof Error ? error.message : `The ${scope} refresh failed.`;
            if (account) saveResult(window.location.href, { account: true, error: message });
            showNotice(`Unable to refresh ${scope}`, message, true);
            if (account) window.dispatchEvent(new Event("wechat:account-refresh-settled"));
        }
    });
})();
