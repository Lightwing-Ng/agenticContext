/* Code version: v1.0.1-codex.0 */

(function initializeLocalResourceSidebarFilters() {
    "use strict";

    const filterForm = document.getElementById("browser_filter_form");
    if (!filterForm || filterForm.dataset.sidebarFiltersReady === "1") return;
    filterForm.dataset.sidebarFiltersReady = "1";
    const searchCandidate = document.querySelector("input[name='q'][data-browser-search-input]")
        || filterForm.querySelector("input[name='q']");
    const queryInput = searchCandidate?.form === filterForm ? searchCandidate : null;
    const usesSearchSuggestions = queryInput?.hasAttribute("data-browser-search-input") || false;
    const contentModeStorageKey = "cachelikes:browser-content-mode:v1";
    const contentModeInputs = filterForm
        ? Array.from(filterForm.querySelectorAll("input[name='view'][type='radio']"))
        : [];

    function readRememberedContentMode() {
        try {
            const stored = window.sessionStorage.getItem(contentModeStorageKey);
            return ["media", "text", "prompts"].includes(stored) ? stored : "";
        } catch (_error) {
            return "";
        }
    }

    function rememberContentMode(mode) {
        if (!["media", "text", "prompts"].includes(mode)) return;
        try {
            window.sessionStorage.setItem(contentModeStorageKey, mode);
        } catch (_error) {
        }
    }

    const contentModeNavigationTitles = Object.freeze({
        text: "Cached text browser",
        media: "Cached media browser",
        prompts: "Saved prompts",
    });
    let navigationSnapshot = null;
    let navigationFrame = 0;
    let navigationPaintTimer = 0;
    let navigationFallbackTimer = 0;

    function clearNavigationTimers() {
        window.cancelAnimationFrame(navigationFrame);
        window.clearTimeout(navigationPaintTimer);
        window.clearTimeout(navigationFallbackTimer);
        navigationFrame = navigationPaintTimer = navigationFallbackTimer = 0;
    }

    function restoreContentNavigation() {
        clearNavigationTimers();
        if (!navigationSnapshot) return;
        const { workspace, nodes, focusedElement, workspaceBusy, documentBusy,
            navigationTarget, wasNavigating, scrollPositions, documentX, documentY } = navigationSnapshot;
        navigationSnapshot = null;
        workspace.replaceChildren(...nodes);
        for (const { node, top, left } of scrollPositions) {
            node.scrollTop = top;
            node.scrollLeft = left;
        }
        delete workspace.dataset.browserNavigationSkeleton;
        for (const [element, attribute, value] of [
            [workspace, "aria-busy", workspaceBusy],
            [document.documentElement, "aria-busy", documentBusy],
            [document.documentElement, "data-navigation-target", navigationTarget],
        ]) {
            if (value === null) element.removeAttribute(attribute);
            else element.setAttribute(attribute, value);
        }
        document.body.classList.toggle("is-page-navigating", wasNavigating);
        window.scrollTo(documentX, documentY);
        if (focusedElement?.isConnected) focusedElement.focus({ preventScroll: true });
    }

    const navigationSkeletonLine = (width = "100%", className = "") => `
        <span class="navigation-skeleton-line${className ? ` ${className}` : ""}"
              style="--navigation-skeleton-width: ${width};"></span>
    `;

    function renderOptimisticContentModeNavigation(mode) {
        const workspace = document.getElementById("browser_workspace");
        const title = contentModeNavigationTitles[mode];
        if (!(workspace instanceof HTMLElement) || !title) return false;

        const metricWidths = mode === "media"
            ? ["34%", "22%", "46%"]
            : mode === "prompts"
                ? ["42%", "18%", "24%"]
                : ["32%", "21%", "39%"];
        const resultWidths = mode === "media"
            ? ["92%", "68%", "76%", "54%"]
            : mode === "prompts"
                ? ["80%", "94%", "66%", "88%"]
                : ["88%", "74%", "96%", "62%"];
        const metricCards = metricWidths.map((width) => `
            <div class="navigation-skeleton-card browser-navigation-skeleton-metric">
                ${navigationSkeletonLine(width)}
                ${navigationSkeletonLine("42%", "browser-navigation-skeleton-value")}
            </div>
        `).join("");
        const resultRows = Array.from({length: mode === "media" ? 6 : 8}, (_, index) => `
            <div class="browser-navigation-skeleton-row">
                ${navigationSkeletonLine(resultWidths[index % resultWidths.length])}
                ${navigationSkeletonLine(`${Math.max(42, 88 - index * 5)}%`)}
            </div>
        `).join("");

        workspace.innerHTML = `
            <div class="navigation-skeleton-status sr-only" role="status" aria-live="polite">
                Loading ${title}
            </div>
            <div class="navigation-skeleton-root browser-navigation-skeleton-root" data-browser-navigation-skeleton aria-hidden="true">
                <div class="browser-navigation-skeleton-heading">
                    <div class="browser-navigation-skeleton-title">
                        ${navigationSkeletonLine("28%", "browser-navigation-skeleton-kicker")}
                        ${navigationSkeletonLine("58%", "browser-navigation-skeleton-heading-line")}
                    </div>
                    <span class="navigation-skeleton-card browser-navigation-skeleton-search"></span>
                </div>
                <div class="browser-navigation-skeleton-metrics">
                    ${metricCards}
                </div>
                <div class="navigation-skeleton-card browser-navigation-skeleton-results">
                    <div class="browser-navigation-skeleton-toolbar">
                        ${navigationSkeletonLine("28%")}
                        ${navigationSkeletonLine("16%")}
                    </div>
                    <div class="browser-navigation-skeleton-result-list">
                        ${resultRows}
                    </div>
                </div>
            </div>
        `;
        workspace.dataset.browserNavigationSkeleton = "1";
        workspace.setAttribute("aria-busy", "true");
        document.documentElement.dataset.navigationTarget = `browser-${mode}`;
        document.documentElement.setAttribute("aria-busy", "true");
        document.body.classList.add("is-page-navigating");
        return true;
    }

    function navigateWithContentModeSkeleton(targetUrl, mode) {
        restoreContentNavigation();
        const workspace = document.getElementById("browser_workspace");
        if (workspace instanceof HTMLElement) {
            navigationSnapshot = {
                workspace,
                nodes: Array.from(workspace.childNodes),
                focusedElement: workspace.contains(document.activeElement) ? document.activeElement : null,
                scrollPositions: Array.from(
                    workspace.querySelectorAll("[data-chat-scrollport], [data-table-scroll]"),
                    (node) => ({ node, top: node.scrollTop, left: node.scrollLeft }),
                ),
                documentX: window.scrollX,
                documentY: window.scrollY,
                workspaceBusy: workspace.getAttribute("aria-busy"),
                documentBusy: document.documentElement.getAttribute("aria-busy"),
                navigationTarget: document.documentElement.getAttribute("data-navigation-target"),
                wasNavigating: document.body.classList.contains("is-page-navigating"),
            };
        }
        renderOptimisticContentModeNavigation(mode);
        let navigationCommitted = false;
        const commitNavigation = () => {
            if (navigationCommitted) return;
            navigationCommitted = true;
            clearNavigationTimers();
            window.location.assign(targetUrl.toString());
        };
        navigationFallbackTimer = window.setTimeout(commitNavigation, 120);
        navigationFrame = window.requestAnimationFrame(() => {
            navigationFrame = 0;
            navigationPaintTimer = window.setTimeout(commitNavigation, 0);
        });
    }

    // Restore message geometry before contact-return checkpoints and BFCache capture.
    window.addEventListener("pagehide", restoreContentNavigation);
    window.addEventListener("pageshow", (event) => {
        if (event.persisted) restoreContentNavigation();
    });

    document.addEventListener("click", (event) => {
        if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey
            || event.shiftKey || event.altKey) return;
        const link = event.target instanceof Element
            ? event.target.closest("a.wechat-conversation-back[href]") : null;
        if (!link || link.hasAttribute("download")
            || !["", "_self"].includes(link.target.toLowerCase())) return;
        const destination = new URL(link.href, window.location.href);
        if (destination.origin !== window.location.origin) return;
        event.preventDefault();
        navigateWithContentModeSkeleton(destination, "text");
    });

    function navigateToContentMode(mode) {
        if (!filterForm || !["media", "text", "prompts"].includes(mode)) return;
        const targetUrl = new URL(filterForm.action || window.location.href, window.location.origin);
        const formData = new FormData(filterForm);
        formData.set("view", mode);
        if (mode === "text") {
            formData.set("session_view", "1");
            const selectedSource = String(formData.get("source") || "").trim().toLowerCase();
            if (!["x", "chatgpt", "copilot", "codex", "claude", "claude_code", "gemini", "grok", "grok_build", "wechat", "zhihu"].includes(selectedSource) && !selectedSource.endsWith("_openclaw")) {
                formData.set("source", "chatgpt");
            }
        }
        targetUrl.search = new URLSearchParams(formData).toString();
        ["page", "media_id", "session", "session_page"].forEach((name) => targetUrl.searchParams.delete(name));
        if (mode !== "media") {
            targetUrl.searchParams.delete("kind");
            targetUrl.searchParams.delete("session_index");
            targetUrl.searchParams.delete("media_view");
        }
        if (mode !== "text") targetUrl.searchParams.delete("project");
        navigateWithContentModeSkeleton(targetUrl, mode);
    }

    const currentUrl = new URL(window.location.href);
    const rememberedContentMode = readRememberedContentMode();
    const navigationUrl = new URL(filterForm.action, window.location.href);
    if (currentUrl.pathname === navigationUrl.pathname
        && !currentUrl.searchParams.has("view") && rememberedContentMode) {
        currentUrl.searchParams.set("view", rememberedContentMode);
        window.location.replace(currentUrl.href);
        return;
    }
    const checkedContentMode = contentModeInputs.find((input) => input.checked)?.value || "text";
    rememberContentMode(checkedContentMode);
    if (filterForm) {
        let filterSubmitTimer = 0;
        const submitFilters = () => {
            if (filterSubmitTimer) window.clearTimeout(filterSubmitTimer);
            filterSubmitTimer = window.setTimeout(() => {
                filterSubmitTimer = 0;
                filterForm.requestSubmit();
            }, 180);
        };
        filterForm.addEventListener("change", (event) => {
            if (event.target.form !== filterForm) return;
            if (event.target.matches("input[name='view'][type='radio']")) {
                rememberContentMode(event.target.value);
                navigateToContentMode(event.target.value);
                return;
            }
            if (event.target.matches("select[name='answerer'], select[name='project'], select[name='account'], select[name='chat_kind']")) {
                for (const name of ["session", "session_page"]) {
                    const field = filterForm.querySelector(`[name="${name}"]`);
                    if (field) field.disabled = true;
                }
            }
            if (event.target.matches("select")) submitFilters();
        });
        filterForm.addEventListener("input", (event) => {
            if (event.target.form !== filterForm) return;
            if (event.target.matches("input[name='q']") && !usesSearchSuggestions) submitFilters();
        });
        if (queryInput && !filterForm.contains(queryInput) && !usesSearchSuggestions) {
            queryInput.addEventListener("input", submitFilters);
        }
    }

    document.addEventListener("change", (event) => {
        const control = event.target;
        if (!(control instanceof HTMLSelectElement)) return;
        const owner = control.form;
        if (owner?.matches("[data-local-resource-context-form]")) owner.requestSubmit();
    });
})();
