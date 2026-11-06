/* Code version: v1.1.2-codex.0 */

(function initializeWechatContactNavigation() {
    "use strict";

    const initializedPaginations = new WeakSet();
    let currentPagination = null;

    function initializePagination(root = document) {
        if (currentPagination && !currentPagination.node.isConnected) {
            currentPagination.cleanup();
            currentPagination = null;
        }
        const scope = root.matches?.("[data-contact-analysis]")
            ? root : root.querySelector?.("[data-contact-analysis]");
        const pagination = scope?.querySelector(".browser-pagination");
        if (!pagination || initializedPaginations.has(pagination)) return;
        currentPagination?.cleanup();
        initializedPaginations.add(pagination);
        let geometryFrame = null;
        let disposed = false;
        const position = () => {
            geometryFrame = null;
            if (disposed || !pagination.isConnected) return;
            const active = pagination.querySelector('.local-store-page-button[aria-current="page"]');
            if (!active?.getClientRects().length) return;
            window.CACHELIKES_PAGINATION_MOTION?.positionPaginationIndicator(
                pagination, active, { immediate: true },
            );
        };
        const schedulePosition = () => {
            if (!disposed && pagination.isConnected && geometryFrame === null) {
                geometryFrame = window.requestAnimationFrame(position);
            }
        };
        // The active button keeps its own fill until the shared indicator is measured.
        position();
        window.addEventListener("resize", schedulePosition, { passive: true });
        window.visualViewport?.addEventListener("resize", schedulePosition, { passive: true });
        document.fonts?.ready?.then(schedulePosition).catch(() => {});
        const observer = typeof ResizeObserver === "function" ? new ResizeObserver(schedulePosition) : null;
        observer?.observe(pagination);
        currentPagination = {
            node: pagination,
            cleanup() {
                disposed = true;
                if (geometryFrame !== null) window.cancelAnimationFrame(geometryFrame);
                window.removeEventListener("resize", schedulePosition);
                window.visualViewport?.removeEventListener("resize", schedulePosition);
                observer?.disconnect();
                initializedPaginations.delete(pagination);
            },
        };
    }

    initializePagination();
    document.addEventListener("wechat:contact-content-ready", () => initializePagination());

    const stateKey = "agenticContextWechatContactReturnV1";
    const scrollport = document.querySelector("[data-wechat-resources] [data-chat-scrollport]");
    if (!scrollport) return;
    let restoreGeneration = 0;
    let restoring = false;

    function remember(focusedLink = null) {
        const previous = window.history.state;
        if (previous !== null && (typeof previous !== "object" || Array.isArray(previous))) return;
        const bounds = scrollport.getBoundingClientRect();
        const anchor = Array.from(scrollport.querySelectorAll("[data-chat-message-id][id]"))
            .find((message) => message.getBoundingClientRect().bottom > bounds.top + scrollport.clientTop + 1);
        const openDetails = Array.from(scrollport.querySelectorAll("details.wechat-provenance[open]"))
            .map((details) => details.closest(".wechat-message[id]")?.id).filter(Boolean);
        const openForwarded = Array.from(scrollport.querySelectorAll("details[data-wechat-forwarded][open]"))
            .map((details) => details.querySelector(":scope > .ui-collapse-body[id]")?.id).filter(Boolean);
        const checkpoint = {
            version: 1,
            url: window.location.href,
            scrollTop: scrollport.scrollTop,
            scrollLeft: scrollport.scrollLeft,
            documentX: window.scrollX,
            documentY: window.scrollY,
            anchorId: anchor?.id || "",
            anchorOffset: anchor ? anchor.getBoundingClientRect().top - bounds.top : 0,
            openDetails,
            openForwarded,
            focusedLinkId: focusedLink?.id || previous?.[stateKey]?.focusedLinkId || "",
        };
        try {
            window.history.replaceState({ ...previous, [stateKey]: checkpoint }, "");
        } catch (_) {
            // Restricted history state must not block the ordinary link navigation.
        }
    }

    function checkpoint() {
        const saved = window.history.state?.[stateKey];
        return saved?.version === 1 && saved.url === window.location.href
            && [saved.scrollTop, saved.scrollLeft, saved.documentX, saved.documentY, saved.anchorOffset]
                .every(Number.isFinite)
            && typeof saved.anchorId === "string" && typeof saved.focusedLinkId === "string"
            && Array.isArray(saved.openDetails) && saved.openDetails.every((id) => typeof id === "string")
            && (saved.openForwarded === undefined || (Array.isArray(saved.openForwarded)
                && saved.openForwarded.every((id) => typeof id === "string")))
            ? saved : null;
    }

    const nextFrame = () => new Promise((resolve) => window.requestAnimationFrame(resolve));

    async function restore() {
        const saved = checkpoint();
        if (!saved) return;
        const generation = ++restoreGeneration;
        restoring = true;
        try {
            await document.fonts?.ready?.catch(() => {});
            if (generation !== restoreGeneration) return;
            if (saved.openForwarded !== undefined) {
                const forwarded = new Set(saved.openForwarded);
                scrollport.querySelectorAll("details[data-wechat-forwarded]").forEach((details) => {
                    const id = details.querySelector(":scope > .ui-collapse-body[id]")?.id;
                    if (id && details.open !== forwarded.has(id)) details.open = forwarded.has(id);
                });
            }
            const open = new Set(saved.openDetails);
            scrollport.querySelectorAll("details.wechat-provenance").forEach((details) => {
                const id = details.closest(".wechat-message[id]")?.id;
                if (id && details.open !== open.has(id)) details.open = open.has(id);
            });
            await nextFrame();
            await nextFrame();
            const motions = scrollport.getAnimations({ subtree: true }).filter((animation) => {
                const end = animation.effect?.getComputedTiming().endTime;
                return Number.isFinite(end) && end <= 2000;
            });
            await Promise.allSettled(motions.map((animation) => animation.finished));
            await nextFrame();
            if (generation !== restoreGeneration || saved.url !== window.location.href) return;
            scrollport.scrollLeft = saved.scrollLeft;
            const anchor = saved.anchorId && document.getElementById(saved.anchorId);
            scrollport.scrollTop = anchor && scrollport.contains(anchor)
                ? scrollport.scrollTop + anchor.getBoundingClientRect().top
                    - scrollport.getBoundingClientRect().top - saved.anchorOffset
                : saved.scrollTop;
            window.scrollTo(saved.documentX, saved.documentY);
            const focusedLink = saved.focusedLinkId && document.getElementById(saved.focusedLinkId);
            if (focusedLink?.matches("a[data-wechat-contact-link]")) {
                focusedLink.focus({ preventScroll: true });
            }
        } finally {
            if (generation === restoreGeneration) restoring = false;
        }
    }

    document.addEventListener("click", (event) => {
        if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey
            || event.shiftKey || event.altKey) return;
        const link = event.target instanceof Element
            ? event.target.closest("a[data-wechat-contact-link]") : null;
        if (!link || link.hasAttribute("download") || (link.target && link.target !== "_self")) return;
        const destination = new URL(link.href, window.location.href);
        if (destination.origin !== window.location.origin) return;
        remember(link);
    }, true);

    window.addEventListener("pagehide", () => {
        ++restoreGeneration;
        if (!restoring && checkpoint()) remember();
        restoring = false;
    });
    window.addEventListener("pageshow", restore);
}());
