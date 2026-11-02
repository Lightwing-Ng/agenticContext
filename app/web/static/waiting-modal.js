/* Code version: v1.2.0-codex.0 */

(function initializeWaitingModal() {
    "use strict";

    const modal = document.getElementById("cache_wait_modal");
    if (!modal) return;

    const title = document.getElementById("cache_wait_modal_title");
    const copy = document.getElementById("cache_wait_modal_copy");
    const closeButton = modal.querySelector("[data-wait-modal-close]");
    const activeWaits = new Map();
    let focusReturnTarget = null;
    let navigationWait = null;
    let navigationFrame = 0;
    let navigationPaintTimer = 0;
    let navigationFallbackTimer = 0;

    function normalizedText(value, fallback) {
        const text = typeof value === "string" ? value.trim() : "";
        return text || fallback;
    }

    function optionsFromElement(element) {
        if (!(element instanceof HTMLElement)) return null;
        const titleText = element.dataset.waitTitle;
        const copyText = element.dataset.waitCopy;
        if (!titleText && !copyText) return null;
        return {
            title: normalizedText(titleText, "Working locally"),
            copy: normalizedText(copyText, "Please wait while the app completes your request on this computer."),
        };
    }

    function renderLatestWait() {
        const latestWait = Array.from(activeWaits.values()).at(-1);
        if (!latestWait) {
            modal.hidden = true;
            return;
        }

        if (title) title.textContent = latestWait.title;
        if (copy) copy.textContent = latestWait.copy;
        modal.hidden = false;
    }

    function begin(options = {}) {
        const waitToken = Symbol("cache-wait");
        const delay = Math.max(0, Number(options.delay) || 0);
        const waitOptions = {
            title: normalizedText(options.title, "Working locally"),
            copy: normalizedText(options.copy, "Please wait while the app completes your request on this computer."),
        };
        let timeoutId = 0;
        let finished = false;

        const reveal = () => {
            if (finished) return;
            activeWaits.set(waitToken, waitOptions);
            renderLatestWait();
        };

        if (delay > 0) {
            timeoutId = window.setTimeout(reveal, delay);
        } else {
            reveal();
        }

        return {
            finish() {
                if (finished) return;
                finished = true;
                if (timeoutId) window.clearTimeout(timeoutId);
                activeWaits.delete(waitToken);
                renderLatestWait();
            },
        };
    }

    function show(options = {}) {
        return begin({ ...options, delay: 0 });
    }

    function hide() {
        activeWaits.clear();
        renderLatestWait();
    }

    function closeModal() {
        hide();
        if (focusReturnTarget instanceof HTMLElement && focusReturnTarget.isConnected) {
            focusReturnTarget.focus({ preventScroll: true });
        }
        focusReturnTarget = null;
    }

    window.CacheWaitModal = Object.freeze({ begin, show, hide });

    function clearNavigationTimers() {
        if (navigationFrame) window.cancelAnimationFrame(navigationFrame);
        if (navigationPaintTimer) window.clearTimeout(navigationPaintTimer);
        if (navigationFallbackTimer) window.clearTimeout(navigationFallbackTimer);
        navigationFrame = navigationPaintTimer = navigationFallbackTimer = 0;
    }

    function navigateAfterPaint(link, options) {
        clearNavigationTimers();
        navigationWait?.finish();
        navigationWait = show(options);
        const destination = link.href;
        let committed = false;
        const commit = () => {
            if (committed) return;
            committed = true;
            clearNavigationTimers();
            window.location.assign(destination);
        };
        // Match Worthward's optimistic navigation: paint once before leaving.
        navigationFallbackTimer = window.setTimeout(commit, 120);
        navigationFrame = window.requestAnimationFrame(() => {
            navigationFrame = 0;
            navigationPaintTimer = window.setTimeout(commit, 0);
        });
    }

    function resetNavigationWait() {
        clearNavigationTimers();
        navigationWait?.finish();
        navigationWait = null;
        focusReturnTarget = null;
        hide();
    }

    window.addEventListener("pagehide", resetNavigationWait);
    window.addEventListener("pageshow", (event) => {
        if (event.persisted) resetNavigationWait();
    });

    closeButton?.addEventListener("click", closeModal);
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && !modal.hidden) {
            event.preventDefault();
            closeModal();
        }
    });

    document.addEventListener("submit", (event) => {
        if (event.defaultPrevented || !(event.target instanceof HTMLFormElement)) return;
        const submitter = event.submitter instanceof HTMLElement ? event.submitter : null;
        const options = optionsFromElement(submitter) || optionsFromElement(event.target);
        if (!options) return;
        focusReturnTarget = submitter || document.activeElement;
        show(options);
    });

    document.addEventListener("click", (event) => {
        if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
            return;
        }
        const link = event.target instanceof Element
            ? event.target.closest("a[data-wait-title], a[data-wait-copy]")
            : null;
        const options = optionsFromElement(link);
        if (!options) return;
        if (link.hasAttribute("download") || !["", "_self"].includes(link.target.toLowerCase())) return;
        const destination = new URL(link.href, window.location.href);
        if (destination.origin !== window.location.origin) return;
        if (destination.href.includes("#") && destination.pathname === window.location.pathname
            && destination.search === window.location.search) return;
        focusReturnTarget = link;
        if (link.hasAttribute("data-wait-navigation")) {
            event.preventDefault();
            navigateAfterPaint(link, options);
        } else {
            show(options);
        }
    });
})();
