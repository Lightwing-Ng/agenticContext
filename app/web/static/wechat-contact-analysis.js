/* Code version: v1.2.1-codex.0 */

(function initializeContactStatementNavigation() {
    "use strict";

    let pointer = null;

    function statementLink(target) {
        return target instanceof Element
            ? target.closest("a[data-wechat-contact-statement]") : null;
    }

    function hasSelection(link) {
        const selection = window.getSelection();
        if (!selection || selection.isCollapsed) return false;
        for (let index = 0; index < selection.rangeCount; ++index) {
            if (selection.getRangeAt(index).intersectsNode(link)) return true;
        }
        return false;
    }

    document.addEventListener("pointerdown", (event) => {
        const link = statementLink(event.target);
        pointer = event.button === 0 && !event.metaKey && !event.ctrlKey
            && !event.shiftKey && !event.altKey && link
            ? { id: event.pointerId, link, x: event.clientX, y: event.clientY, moved: false }
            : null;
    }, true);

    document.addEventListener("pointermove", (event) => {
        if (pointer?.id === event.pointerId
            && Math.hypot(event.clientX - pointer.x, event.clientY - pointer.y) > 5) {
            pointer.moved = true;
        }
    }, true);

    document.addEventListener("pointercancel", () => { pointer = null; }, true);
    document.addEventListener("click", (event) => {
        const link = statementLink(event.target);
        const dragged = pointer?.link === link && pointer.moved;
        pointer = null;
        if (!link || event.button !== 0 || event.detail === 0 || event.metaKey
            || event.ctrlKey || event.shiftKey || event.altKey) return;
        // Keep native link navigation unless this gesture selected or dragged text.
        if (dragged || hasSelection(link)) event.preventDefault();
    }, true);
}());

(function initializeContactSourceDisclosures() {
    "use strict";

    function enhance() {
        const content = document.querySelector("[data-contact-analysis]");
        if (!content) return;
        window.SharedDisclosureMotion?.enhance(content, {
            selector: "details.wechat-contact-sources",
        });
    }

    document.addEventListener("wechat:contact-content-ready", enhance);
    enhance();
}());

(function initializeContactLoading() {
    "use strict";

    const content = document.querySelector("[data-contact-analysis]");
    const main = content?.closest("main");
    const modal = document.getElementById("cache_wait_modal");
    if (!content || !main || !modal) return;

    const closeButton = modal.querySelector("[data-wait-modal-close]");
    const title = document.getElementById("cache_wait_modal_title")?.textContent;
    const copy = document.getElementById("cache_wait_modal_copy")?.textContent;
    const contactId = content.dataset.wechatContactAnalysis;
    let generation = 0;
    let running = false;
    let busy = false;
    let timer = 0;
    let controller = null;
    let wait = null;
    let previousFocus = null;
    let transientFailures = 0;

    function syncModalState() {
        const blocked = busy && !modal.hidden;
        if (main.inert !== blocked) main.inert = blocked;
    }

    const modalObserver = new MutationObserver(syncModalState);
    const nextFrame = () => new Promise((resolve) => window.requestAnimationFrame(resolve));
    const current = (requestGeneration) => running && generation === requestGeneration;

    function finishWaiting() {
        const returnFocus = modal.contains(document.activeElement);
        busy = false;
        wait?.finish();
        wait = null;
        if (!window.CacheWaitModal) modal.hidden = true;
        syncModalState();
        content.setAttribute("aria-busy", "false");
        if (returnFocus) {
            const destination = previousFocus?.isConnected && previousFocus !== document.body
                ? previousFocus : document.querySelector("h2[data-contact-analysis-name]");
            destination?.focus({ preventScroll: true });
        }
    }

    function renderNetworkError() {
        const status = content.querySelector(".wechat-contact-status");
        if (!status) return;
        const heading = document.createElement("h3");
        heading.textContent = "Statements could not be loaded.";
        const message = document.createElement("p");
        message.textContent = "Refresh this view to try again.";
        const retry = document.createElement("a");
        retry.className = "secondary-button";
        retry.href = window.location.href;
        retry.textContent = "Refresh";
        status.replaceChildren(heading, message, retry);
        content.dataset.contactAnalysisState = "error";
    }

    async function acceptResult(payload, requestGeneration) {
        if (!current(requestGeneration)) return;
        if (typeof payload.html !== "string") throw new Error("Missing contact content.");
        const search = content.querySelector('input[name="q"]');
        const draft = search && search.value !== search.defaultValue ? search.value : null;
        const restoreSearchFocus = search && document.activeElement === search;
        const selection = search ? [search.selectionStart, search.selectionEnd] : null;
        const fragment = document.createElement("template");
        fragment.innerHTML = payload.html;
        if (!current(requestGeneration)) return;
        content.replaceChildren(fragment.content);
        content.dataset.contactAnalysisState = payload.status;
        const newSearch = content.querySelector('input[name="q"]');
        if (draft !== null && newSearch) newSearch.value = draft;
        if (typeof payload.display_name === "string" && payload.display_name) {
            document.querySelectorAll("[data-contact-analysis-name]").forEach((node) => {
                node.textContent = payload.display_name;
            });
            document.title = `${payload.display_name} · Contact analysis · Local resources`;
        }
        if (typeof payload.username === "string") {
            document.querySelectorAll("[data-contact-analysis-username]").forEach((node) => {
                node.textContent = payload.username;
            });
        }
        window.SHARED_NUMERIC_DISPLAY?.enhanceNumericDisplayElements(content);
        // The shared table observer adopts the new shell; pagination uses its shared adapter.
        document.dispatchEvent(new Event("wechat:contact-content-ready"));
        await document.fonts?.ready;
        await nextFrame();
        await nextFrame();
        if (!current(requestGeneration)) return;
        if (restoreSearchFocus && newSearch && !main.inert) {
            newSearch.focus({ preventScroll: true });
            if (selection.every(Number.isInteger)) newSearch.setSelectionRange(...selection);
        }
        running = false;
        finishWaiting();
    }

    async function poll(requestGeneration) {
        if (!current(requestGeneration)) return;
        controller = new AbortController();
        let retryableFailure = true;
        try {
            const response = await window.fetch(window.location.href, {
                headers: { Accept: "application/json" },
                cache: "no-store",
                credentials: "same-origin",
                signal: controller.signal,
            });
            if (!current(requestGeneration)) return;
            retryableFailure = [500, 502, 503, 504].includes(response.status);
            const payload = await response.json();
            if (!current(requestGeneration)) return;
            // Parsed source/protocol failures must not be hidden by transport retries.
            retryableFailure = false;
            if (payload.contact_id !== contactId) throw new Error("Contact scope changed.");
            if (response.status === 202 && payload.status === "indexing") {
                transientFailures = 0;
                timer = window.setTimeout(() => poll(requestGeneration), 2000);
                return;
            }
            if ((response.ok && payload.status === "ready")
                || (response.status === 503 && payload.status === "error")) {
                await acceptResult(payload, requestGeneration);
                return;
            }
            throw new Error("Contact statements unavailable.");
        } catch (_) {
            if (!current(requestGeneration)) return;
            if (retryableFailure && transientFailures < 3) {
                const delay = 2000 * (2 ** transientFailures++);
                timer = window.setTimeout(() => poll(requestGeneration), delay);
                return;
            }
            running = false;
            renderNetworkError();
            finishWaiting();
        }
    }

    function start() {
        if (running || content.dataset.contactAnalysisState !== "indexing") return;
        running = true;
        busy = true;
        transientFailures = 0;
        const requestGeneration = ++generation;
        previousFocus = document.activeElement;
        modalObserver.observe(modal, { attributes: true, attributeFilter: ["hidden"] });
        wait = window.CacheWaitModal?.show({ title, copy }) || null;
        if (!window.CacheWaitModal) modal.hidden = false;
        syncModalState();
        content.setAttribute("aria-busy", "true");
        closeButton?.focus({ preventScroll: true });
        poll(requestGeneration);
    }

    document.addEventListener("keydown", (event) => {
        if (busy && !modal.hidden && event.key === "Tab") {
            event.preventDefault();
            closeButton?.focus({ preventScroll: true });
        }
    });
    window.addEventListener("pagehide", () => {
        ++generation;
        running = false;
        window.clearTimeout(timer);
        controller?.abort();
        finishWaiting();
        modalObserver.disconnect();
    });
    window.addEventListener("pageshow", start);
    start();
}());
