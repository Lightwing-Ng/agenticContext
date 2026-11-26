/* Code version: v1.5.0-codex.0 */

(function initializeWaitingModal() {
    "use strict";

    const modal = document.getElementById("cache_wait_modal");
    if (!modal) return;

    const title = document.getElementById("cache_wait_modal_title");
    const copy = document.getElementById("cache_wait_modal_copy");
    const closeButton = modal.querySelector("[data-wait-modal-close]");
    const meter = modal.querySelector("[data-wait-modal-progress]");
    const track = meter?.querySelector('[role="progressbar"]');
    const fill = track?.querySelector(".status-progress-fill");
    const progressCopy = meter?.querySelector("[data-wait-modal-progress-copy]");
    const number = new Intl.NumberFormat("en-US");
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

    function renderProgress(progress) {
        if (!meter || !track || !fill || !progressCopy) return;
        const { processed, total, label, unit } = progress ?? {};
        if (typeof label !== "string" || typeof unit !== "string"
            || !Number.isSafeInteger(processed) || !Number.isSafeInteger(total)
            || processed < 0 || total <= 0 || processed > total) {
            meter.hidden = true;
            return;
        }
        const percent = processed / total * 100;
        const wholePercent = Math.floor(percent);
        const detail = `${number.format(processed)} / ${number.format(total)} ${unit} processed (${wholePercent}%)`;
        track.setAttribute("aria-label", label);
        track.setAttribute("aria-valuenow", String(wholePercent));
        track.setAttribute("aria-valuetext", detail);
        fill.style.width = `${percent}%`;
        if (progressCopy.textContent !== detail) progressCopy.textContent = detail;
        meter.hidden = false;
    }

    function renderLatestWait() {
        const latestWait = Array.from(activeWaits.values()).at(-1);
        renderProgress(latestWait?.progress);
        if (!latestWait) {
            modal.hidden = true;
            return;
        }

        if (title && title.textContent !== latestWait.title) title.textContent = latestWait.title;
        if (copy && copy.textContent !== latestWait.copy) copy.textContent = latestWait.copy;
        modal.hidden = false;
    }

    function begin(options = {}) {
        const waitToken = Symbol("cache-wait");
        const delay = Math.max(0, Number(options.delay) || 0);
        const waitOptions = {
            title: normalizedText(options.title, "Working locally"),
            copy: normalizedText(options.copy, "Please wait while the app completes your request on this computer."),
            progress: options.progress,
        };
        const returnFocus = options.returnFocus;
        let timeoutId = 0;
        let finished = false;

        const reveal = () => {
            if (finished) return;
            window.clearTimeout(timeoutId);
            activeWaits.delete(waitToken);
            activeWaits.set(waitToken, waitOptions);
            renderLatestWait();
            if (returnFocus instanceof HTMLElement) {
                focusReturnTarget = returnFocus;
                closeButton?.focus({ preventScroll: true });
            }
        };

        if (delay > 0) {
            timeoutId = window.setTimeout(reveal, delay);
        } else {
            reveal();
        }

        return {
            reopen: reveal,
            update(options = {}) {
                if (finished) return;
                if (Object.hasOwn(options, "title")) {
                    waitOptions.title = normalizedText(options.title, waitOptions.title);
                }
                if (Object.hasOwn(options, "copy")) {
                    waitOptions.copy = normalizedText(options.copy, waitOptions.copy);
                }
                if (Object.hasOwn(options, "progress")) waitOptions.progress = options.progress;
                // Closing a notification dismisses its feedback while work continues.
                // A later progress update must not reopen that dismissed notification.
                if (activeWaits.has(waitToken)) renderLatestWait();
            },
            finish() {
                if (finished) return;
                finished = true;
                if (timeoutId) window.clearTimeout(timeoutId);
                const restoreFocus = returnFocus === focusReturnTarget && document.activeElement === closeButton;
                activeWaits.delete(waitToken);
                renderLatestWait();
                if (restoreFocus && modal.hidden) restoreCloseFocus();
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
        restoreCloseFocus();
    }

    function restoreCloseFocus() {
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
