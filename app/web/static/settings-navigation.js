/* Code version: v1.2.0-codex.1 */

(function initializeSettingsNavigation() {
    "use strict";

    const nav = document.querySelector(".settings-category-nav");
    const motionKey = "cachelikes:settings-navigation-motion:v1";
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    // Carry the pill's position across the separate Style tokens document.
    function restoreNavigationMotion() {
        if (!nav) return;
        try {
            const pending = JSON.parse(window.sessionStorage.getItem(motionKey) || "null");
            window.sessionStorage.removeItem(motionKey);
            if (!pending || reducedMotion.matches || Date.now() - pending.at > 10_000
                || pending.target !== window.location.pathname + window.location.hash
                || !Number.isInteger(pending.index) || pending.index < 0
                || pending.index >= nav.children.length) return;
            nav.style.setProperty("--motion-duration-spatial", "0s");
            nav.style.setProperty("--settings-category-active-index", String(pending.index));
            // Establish the old position before letting the shared CSS transition run.
            window.getComputedStyle(nav, "::before").transform;
            window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
                nav.style.removeProperty("--motion-duration-spatial");
                nav.style.removeProperty("--settings-category-active-index");
            }));
        } catch (_error) {
            // Navigation remains available when browser storage is disabled.
        }
    }
    nav?.addEventListener("click", (event) => {
        const link = event.target.closest("a");
        if (!link || event.button !== 0 || event.metaKey || event.ctrlKey
            || event.shiftKey || event.altKey || link.target) return;
        const target = new URL(link.href);
        if (target.origin !== window.location.origin || target.pathname === window.location.pathname) return;
        const index = Array.from(nav.children).findIndex((item) => item.classList.contains("is-active"));
        try {
            window.sessionStorage.setItem(motionKey, JSON.stringify({
                target: target.pathname + target.hash, index, at: Date.now(),
            }));
        } catch (_error) {}
    });
    restoreNavigationMotion();

    const shell = document.querySelector("[data-settings-category-shell]");
    const categoryLinks = Array.from(document.querySelectorAll("[data-settings-category]"));
    const categoryPanels = Array.from(document.querySelectorAll("[data-settings-panel]"));
    if (!shell || !categoryLinks.length || !categoryPanels.length) return;

    const categories = new Set(categoryPanels.map((panel) => panel.dataset.settingsPanel));
    const defaultCategory = "browser";
    const legacyCategoryAliases = new Map([["chatgpt", "llm"]]);

    function categoryFromHash() {
        const hashCategory = window.location.hash.replace(/^#settings-/, "");
        const category = legacyCategoryAliases.get(hashCategory) || hashCategory;
        return categories.has(category) ? category : defaultCategory;
    }

    function activateCategory(category, options = {}) {
        const nextCategory = categories.has(category) ? category : defaultCategory;
        const { updateHistory = false } = options;

        shell.dataset.activeCategory = nextCategory;
        categoryLinks.forEach((link) => {
            const isActive = link.dataset.settingsCategory === nextCategory;
            link.classList.toggle("is-active", isActive);
            if (isActive) {
                link.setAttribute("aria-current", "page");
            } else {
                link.removeAttribute("aria-current");
            }
        });
        categoryPanels.forEach((panel) => {
            const isActive = panel.dataset.settingsPanel === nextCategory;
            panel.classList.toggle("is-active", isActive);
            panel.hidden = !isActive;
        });
        if (updateHistory) {
            const nextHash = `#settings-${nextCategory}`;
            window.history.pushState(null, "", nextHash);
        }
    }

    categoryLinks.forEach((link) => {
        link.addEventListener("click", (event) => {
            if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
            event.preventDefault();
            activateCategory(link.dataset.settingsCategory, { updateHistory: true });
            if (window.CACHELIKES_RESPONSIVE.media("sidebarOverlayMax").matches) {
                window.setSidebarOpen?.(false, { animate: true });
            }
        });
    });

    window.addEventListener("hashchange", () => activateCategory(categoryFromHash()));
    document.documentElement.classList.add("settings-navigation-ready");
    activateCategory(categoryFromHash());
})();
