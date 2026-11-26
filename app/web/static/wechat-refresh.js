/* Code version: v1.9.0-codex.0 */

(function initializeWechatRefresh() {
    "use strict";

    const button = document.querySelector("[data-wechat-conversation-refresh], [data-wechat-account-refresh]");
    const banner = document.querySelector("[data-wechat-refresh-banner]");
    const bannerTitle = banner?.querySelector("[data-wechat-refresh-title]");
    const bannerCopy = banner?.querySelector("[data-wechat-refresh-copy]");
    const pendingReadSelector = '[data-wechat-read-pending][data-state="running"]';
    const resultKey = "agenticContextWechatRefreshV1";
    const pendingKey = "agenticContextWechatPendingRefreshV1";
    const contactReturnKey = "agenticContextWechatContactReturnV1";
    // Keep this browser's status watch bounded independently of the worker.
    const statusLifetime = 2 * 60 * 60 * 1_000;
    const statusOutageLimit = 30_000;
    const refreshStatusPath = /^\/api\/browser\/wechat\/refresh\/[A-Za-z0-9_-]{24,128}$/;
    const legacyStatusError = "The refresh status was lost. The local service may have restarted. "
        + "Reload this conversation to check for updates before trying again.";
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
        let result = historyObject()?.[resultKey];
        if (!result || result.version !== 1 || result.url !== window.location.href) return;
        if (result.error === legacyStatusError) {
            result = { account: result.account === true, interrupted: true, publicationUnknown: true };
            saveResult(window.location.href, result);
        }
        if (!result.interrupted && !(result.account && result.error) && !document.querySelector(pendingReadSelector)) {
            clearSavedResult();
        }
        showRefreshResult(result);
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

    function showRefreshResult(result) {
        const scope = result.account ? "WeChat account" : "Chat history";
        if (result.interrupted) {
            if (result.publicationUnknown === false) {
                showNotice(`${scope} refresh interrupted`,
                    "The local service stopped before this refresh finished. You can try again.");
            } else {
                showNotice(`${scope} refresh status unavailable`,
                    "The local service could not confirm the result. Reload this view to check for saved changes before trying again.");
            }
        } else if (result.error) {
            showNotice(`Unable to refresh ${result.account ? scope : "chat history"}`, result.error, true);
        } else {
            showResult(result);
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

    function refreshStatusUrl(value) {
        if (typeof value !== "string") return null;
        try {
            const url = new URL(value, window.location.href);
            if (url.origin === window.location.origin && refreshStatusPath.test(url.pathname)
                && !url.search && !url.hash && !url.username && !url.password) {
                return url.pathname;
            }
        } catch (_) {
            // Accept only the local refresh status route.
        }
        return null;
    }

    function savePendingRefresh(statusUrl, expiresAt) {
        const previous = historyObject();
        if (!previous) return;
        const url = window.location.href;
        if (statusUrl) {
            previous[pendingKey] = { url, statusUrl, expiresAt };
        } else if (previous[pendingKey]?.url === url) {
            delete previous[pendingKey];
        } else {
            return;
        }
        try {
            window.history.replaceState(previous, "", url);
        } catch (_) {
            // A restricted history entry can still display the current refresh.
        }
    }

    function getPendingRefresh() {
        const pending = historyObject()?.[pendingKey];
        return pending?.url === window.location.href ? pending : null;
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

    function progressCopy(snapshot, account, showSourceCounts = true) {
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
        if (showSourceCounts && total && !["preparing", "reading", "comparing"].includes(phase)) {
            lines.push(`${formatCount(Math.min(completed, total))} of ${formatCount(total)} sources processed.`);
        }
        if (phase !== "preparing") {
            lines.push(`${messageCount(count(snapshot.checked))} checked.`);
            lines.push(`${formatCount(count(snapshot.candidates))} possible update${count(snapshot.candidates) === 1 ? "" : "s"} identified.`);
        }
        lines.push(...coverageDetails(snapshot));
        return lines.join(" ");
    }

    function refreshProgress(snapshot) {
        const sources = count(snapshot.parts_total);
        const current = count(snapshot.part_current);
        const records = count(snapshot.part_rows_total);
        if (snapshot.phase === "comparing" && records > 0 && current > 0 && current <= sources) {
            return {
                label: `Source ${formatCount(current)} of ${formatCount(sources)} record processing progress`,
                unit: "records",
                total: records,
                processed: Math.min(count(snapshot.part_rows_processed), records),
            };
        }
        if (sources === 0) return null;
        return {
            label: "WeChat refresh source processing progress",
            unit: "sources",
            total: sources,
            processed: Math.min(count(snapshot.parts_completed), sources),
        };
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

    async function waitForRefresh(statusUrl, expiresAt, onProgress) {
        let outageStart;
        while (statusUrl && Date.now() < expiresAt) {
            await wait(1_000);
            const controller = new AbortController();
            const timeout = window.setTimeout(() => controller.abort(), 10_000);
            try {
                const response = await fetch(statusUrl, {
                    cache: "no-store",
                    headers: { Accept: "application/json" },
                    redirect: "error",
                    signal: controller.signal,
                });
                if (response.status === 404) break;
                if (response.ok) {
                    const snapshot = await payload(response);
                    if (snapshot.state === "interrupted") return snapshot;
                    if (["succeeded", "failed"].includes(snapshot.state)) {
                        onProgress(snapshot);
                        return snapshot;
                    }
                    if (snapshot.state === "running") {
                        outageStart = undefined;
                        onProgress(snapshot);
                        continue;
                    }
                }
            } catch (_) {
                // The service can briefly be unavailable during a restart.
            } finally {
                window.clearTimeout(timeout);
            }
            outageStart ??= Date.now();
            if (Date.now() - outageStart >= statusOutageLimit) return { state: "status-unavailable" };
        }
        return { state: "interrupted" };
    }

    banner?.querySelector("[data-wechat-refresh-dismiss]")?.addEventListener("click", clearSavedResult);
    restoreResult();
    if (!(button instanceof HTMLButtonElement)) return;

    async function refresh(pending) {
        if (refreshing || (!pending && button.disabled)) return;
        const endpoint = button.dataset.wechatRefreshUrl;
        const sessionId = button.dataset.wechatSessionId;
        const accountId = button.dataset.wechatAccountId;
        if (!endpoint || !(accountId || sessionId)) return;
        const account = Boolean(accountId);
        const scope = account ? "WeChat account" : "chat history";
        const originalUrl = window.location.href;
        const readRetry = account && document.querySelector("[data-wechat-read-retry]");
        clearSavedResult();
        refreshing = true;
        const idleDisabled = button.disabled;
        button.disabled = true;
        if (readRetry) readRetry.disabled = true;
        button.setAttribute("aria-busy", "true");
        const idleLabel = button.getAttribute("aria-label") || `Refresh ${scope}`;
        const idleTitle = button.title;
        button.setAttribute("aria-label", `Refreshing ${scope}…`);
        button.title = `Refreshing ${scope}…`;
        if (banner instanceof HTMLElement) banner.hidden = true;
        const title = `Refreshing ${scope}`;
        const initialCopy = progressCopy({ phase: "preparing" }, account);
        const activity = window.CacheActivity?.track({
            id: "wechat:refresh",
            label: title,
            unit: "sources",
            message: initialCopy,
        }, () => notice?.reopen());
        const notice = window.CacheWaitModal?.show({
            title,
            copy: initialCopy,
            returnFocus: activity?.trigger,
        });
        const showProgress = (snapshot) => {
            const progress = refreshProgress(snapshot);
            notice?.update({
                copy: progressCopy(snapshot, account, !progress || progress.unit !== "sources"),
                progress,
            });
            const total = count(snapshot.parts_total);
            activity?.update({
                message: progressCopy(snapshot, account, false),
                total,
                processed: Math.min(count(snapshot.parts_completed), total),
            });
        };

        let outcome;
        let reloading = false;
        try {
            let result;
            if (pending) {
                result = await waitForRefresh(refreshStatusUrl(pending.statusUrl), pending.expiresAt, showProgress);
            } else {
                const response = await fetch(endpoint, {
                    method: "POST",
                    cache: "no-store",
                    headers: { Accept: "application/json", "Content-Type": "application/json" },
                    body: JSON.stringify(account ? { account_id: accountId } : { session_id: sessionId }),
                });
                result = await payload(response);
                if (!response.ok) {
                    throw new Error(result.error || `Unable to start the ${scope} refresh.`);
                }
                if (window.location.href !== originalUrl) return;
                if (result.state === "running") {
                    showProgress(result);
                    const statusUrl = refreshStatusUrl(result.status_url);
                    const expiresAt = Date.now() + statusLifetime;
                    if (statusUrl) savePendingRefresh(statusUrl, expiresAt);
                    result = await waitForRefresh(statusUrl, expiresAt, showProgress);
                }
            }
            if (window.location.href !== originalUrl) return;
            if (result.state !== "status-unavailable") savePendingRefresh();
            if (result.state === "failed" && result.member_snapshot !== "saved") {
                throw new Error(refreshError(result));
            }
            if (["succeeded", "failed"].includes(result.state)) {
                reloadAfterRefresh(result, account);
                reloading = true;
                return;
            }
            outcome = { account, interrupted: true, publicationUnknown: result.publication_unknown !== false };
        } catch (error) {
            outcome = { account, error: error instanceof Error ? error.message : `The ${scope} refresh failed.` };
        } finally {
            if (!reloading) {
                notice?.finish();
                activity?.finish();
                refreshing = false;
                button.disabled = idleDisabled;
                if (readRetry) readRetry.disabled = false;
                button.removeAttribute("aria-busy");
                button.setAttribute("aria-label", idleLabel);
                button.title = idleTitle;
            }
        }
        if (window.location.href !== originalUrl) return;
        if (account || outcome.interrupted) saveResult(originalUrl, outcome);
        showRefreshResult(outcome);
        if (account) window.dispatchEvent(new Event("wechat:account-refresh-settled"));
    }

    button.addEventListener("click", () => refresh(getPendingRefresh()));
    const pending = getPendingRefresh();
    if (pending) refresh(pending);
})();
