/* Code version: v1.22.0-codex.1 */

(function initializeSidebar() {
    "use strict";

    const appShell = document.getElementById("app_shell");
    const appSidebar = document.querySelector(".sidebar");
    const sidebarToggle = document.getElementById("sidebar_toggle");
    const sidebarBackdrop = document.getElementById("sidebar_backdrop");
    const sidebarDock = document.querySelector(".sidebar-dock");
    if (!appShell || !appSidebar || !sidebarToggle) return;

    const sidebarOverlayMedia = window.CACHELIKES_RESPONSIVE.media("sidebarOverlayMax");
    const sidebarMemoryKey = "cachelikes:sidebar-open";
    const dockLocationMemoryPrefix = "cachelikes:dock-location:v1:";
    const dockSections = new Set(["agent", "cache", "local-resources", "settings"]);
    const cacheSectionPaths = new Set(["/cache/x", "/cache/grok", "/cache/chatgpt", "/cache/gemini", "/cache/claude", "/cache/zhihu"]);
    const reducedMotionMedia = window.matchMedia("(prefers-reduced-motion: reduce)");
    const sidebarGelAnimationNames = new Set([
        "workspace-sidebar-gel-open",
        "workspace-sidebar-gel-close",
    ]);
    const sidebarGelCandidateSelector = [
        ".workspace-mobile-summary-shell > :not(.workspace-summary-card)",
        ".settings-workspace-header > :not(.settings-summary-card)",
        ".agent-workspace > .agent-workspace-grid",
        "#settings_workspace .settings-category-shell",
    ].join(", ");
    const sidebarGelTargetSelector = "[data-sidebar-gel-content]";
    const sidebarTitleTargetSelector = ".workspace-summary-card > .report-heading-row";
    const legacyCachePathMap = new Map([
        ["/", "/cache/x"],
        ["/grok", "/cache/grok"],
        ["/chatgpt", "/cache/chatgpt"],
        ["/gemini", "/cache/gemini"],
        ["/claude", "/cache/claude"],
        ["/zhihu", "/cache/zhihu"],
    ]);
    const localResourceFilterNames = ["view", "source", "kind", "q", "sort", "session_view"];
    const agentRoutePattern = /^\/agent\/(?:safari\/chatgpt|(?:edge|chrome)\/(?:chatgpt|gemini|grok|claude))$/;
    const settingsCategoryPattern = /^#settings-(browser|downloads|chatgpt|cloud|maintenance)$/;
    const dockLinks = sidebarDock
        ? Array.from(sidebarDock.querySelectorAll("[data-dock-section], [data-section-link]"))
        : [];
    const sidebarMotionDurationMs = 560;
    let isSidebarOpen = sidebarToggle.getAttribute("aria-expanded") === "true";
    let sidebarMotionResetTimer = 0;
    let sidebarMotionEndHandler = null;
    let sidebarTitleAnimations = [];
    let dockPositionFrame = 0;

    function readSidebarMemory() {
        try {
            const storedValue = window.sessionStorage.getItem(sidebarMemoryKey);
            if (storedValue === "true") return true;
            if (storedValue === "false") return false;
        } catch (_error) {
        }
        return !sidebarOverlayMedia.matches;
    }

    function writeSidebarMemory(value) {
        try {
            window.sessionStorage.setItem(sidebarMemoryKey, String(Boolean(value)));
        } catch (_error) {
        }
    }

    function dockLocationMemoryKey(section) {
        return `${dockLocationMemoryPrefix}${section}`;
    }

    function normalizeDockLocation(section, value) {
        if (!dockSections.has(section) || !value) return "";

        let targetUrl;
        try {
            targetUrl = new URL(value, window.location.origin);
        } catch (_error) {
            return "";
        }
        if (targetUrl.origin !== window.location.origin) return "";

        if (section === "agent") {
            return targetUrl.pathname === "/agent" || agentRoutePattern.test(targetUrl.pathname)
                ? `${targetUrl.pathname}${targetUrl.search}${targetUrl.hash}`
                : "";
        }
        if (section === "cache") {
            // Migrate the previous Cache destination, which incorrectly pointed
            // at the local browser, to the first cache source page.
            if (targetUrl.pathname === "/browser") return "/cache/chatgpt";
            const normalizedPath = legacyCachePathMap.get(targetUrl.pathname) || targetUrl.pathname;
            return cacheSectionPaths.has(normalizedPath) ? normalizedPath : "";
        }
        if (section === "settings") {
            if (targetUrl.pathname !== "/settings") return "";
            const categoryHash = settingsCategoryPattern.test(targetUrl.hash) ? targetUrl.hash : "";
            return `${targetUrl.pathname}${categoryHash}`;
        }
        if (targetUrl.pathname !== "/browser") return "";

        const normalizedUrl = new URL(targetUrl.pathname, window.location.origin);
        localResourceFilterNames.forEach((name) => {
            if (targetUrl.searchParams.has(name)) {
                normalizedUrl.searchParams.set(name, targetUrl.searchParams.get(name) || "");
            }
        });
        return `${normalizedUrl.pathname}${normalizedUrl.search}`;
    }

    function readDockLocation(section) {
        try {
            return normalizeDockLocation(
                section,
                window.sessionStorage.getItem(dockLocationMemoryKey(section)) || "",
            );
        } catch (_error) {
            return "";
        }
    }

    function writeDockLocation(section, location) {
        const normalizedLocation = normalizeDockLocation(section, location);
        if (!normalizedLocation) return;
        try {
            window.sessionStorage.setItem(dockLocationMemoryKey(section), normalizedLocation);
        } catch (_error) {
        }
    }

    function dockSectionForLink(link) {
        const explicitSection = link?.dataset.dockSection || "";
        if (dockSections.has(explicitSection)) return explicitSection;
        const legacySection = link?.dataset.sectionLink || "";
        if (["llm", "local-resources", "settings"].includes(legacySection)) return legacySection;
        return legacySection ? "cache" : "";
    }

    function activeDockSection() {
        const activeLink = dockLinks.find((link) => link.getAttribute("aria-current") === "page");
        const section = dockSectionForLink(activeLink);
        return dockSections.has(section) ? section : "";
    }

    function currentLocalResourcesLocation() {
        const filterForm = document.querySelector(".browser-filter-form");
        const browserUrl = new URL("/browser", window.location.origin);
        if (!(filterForm instanceof HTMLFormElement)) return browserUrl.pathname;

        localResourceFilterNames.forEach((name) => {
            const field = filterForm.elements.namedItem(name);
            if (field instanceof RadioNodeList) {
                browserUrl.searchParams.set(name, field.value);
            } else if (field instanceof HTMLInputElement || field instanceof HTMLSelectElement) {
                browserUrl.searchParams.set(name, field.value);
            }
        });
        return `${browserUrl.pathname}${browserUrl.search}`;
    }

    function currentSettingsLocation() {
        if (settingsCategoryPattern.test(window.location.hash)) {
            return `${window.location.pathname}${window.location.hash}`;
        }
        const activeCategory = document.querySelector('[data-settings-category][aria-current="page"]');
        const category = activeCategory?.dataset.settingsCategory || "";
        return category ? `/settings#settings-${category}` : "/settings";
    }

    function currentDockLocation(section) {
        if (section === "agent") {
            return window.location.pathname === "/agent" || agentRoutePattern.test(window.location.pathname)
                ? `${window.location.pathname}${window.location.search}${window.location.hash}`
                : "/agent";
        }
        if (section === "cache") {
            const normalizedPath = legacyCachePathMap.get(window.location.pathname) || window.location.pathname;
            return cacheSectionPaths.has(normalizedPath) ? normalizedPath : "/cache/chatgpt";
        }
        if (section === "local-resources") return currentLocalResourcesLocation();
        if (section === "settings") return currentSettingsLocation();
        return "";
    }

    function dockSectionForCurrentPath() {
        const normalizedPath = legacyCachePathMap.get(window.location.pathname) || window.location.pathname;
        if (normalizedPath === "/agent" || agentRoutePattern.test(window.location.pathname)) return "agent";
        if (cacheSectionPaths.has(normalizedPath)) return "cache";
        if (window.location.pathname === "/browser") return "local-resources";
        if (window.location.pathname === "/settings") return "settings";
        return "";
    }

    function syncDockActiveState() {
        const activeSection = dockSectionForCurrentPath();
        if (!activeSection) return;
        dockLinks.forEach((link) => {
            const isActive = dockSectionForLink(link) === activeSection;
            link.classList.toggle("is-active", isActive);
            if (isActive) {
                link.setAttribute("aria-current", "page");
            } else {
                link.removeAttribute("aria-current");
            }
        });
    }

    function syncDockDestinations() {
        dockLinks.forEach((link) => {
            const section = dockSectionForLink(link);
            const rememberedLocation = readDockLocation(section);
            if (rememberedLocation) link.href = rememberedLocation;
        });
    }

    function rememberCurrentDockLocation() {
        const section = activeDockSection();
        if (!section) return;
        writeDockLocation(section, currentDockLocation(section));
        syncDockDestinations();
    }

    function clearSidebarMotionState() {
        if (sidebarMotionResetTimer) {
            window.clearTimeout(sidebarMotionResetTimer);
            sidebarMotionResetTimer = 0;
        }
        if (sidebarMotionEndHandler) {
            appShell.removeEventListener("animationend", sidebarMotionEndHandler);
        }
        sidebarMotionEndHandler = null;
        appShell.classList.remove("is-sidebar-animating", "is-sidebar-opening", "is-sidebar-closing");
    }

    function syncSidebarGelTargets() {
        const targets = Array.from(appShell.querySelectorAll(sidebarGelCandidateSelector));
        targets.forEach((target) => target.setAttribute("data-sidebar-gel-content", ""));
        return targets;
    }

    function clearSidebarTitleMotion() {
        sidebarTitleAnimations.forEach((animation) => animation.cancel());
        sidebarTitleAnimations = [];
    }

    function animateSidebarTitlesFrom(firstRects, targets) {
        if (reducedMotionMedia.matches || sidebarOverlayMedia.matches) return;
        sidebarTitleAnimations = targets.map((target, index) => {
            if (typeof target.animate !== "function") return null;
            const first = firstRects[index];
            const last = target.getBoundingClientRect();
            const deltaX = first.left - last.left;
            const deltaY = first.top - last.top;
            if (Math.abs(deltaX) < 0.5 && Math.abs(deltaY) < 0.5) return null;
            const animation = target.animate(
                [
                    {transform: `translate3d(${deltaX.toFixed(2)}px, ${deltaY.toFixed(2)}px, 0)`},
                    {transform: "none"},
                ],
                {
                    duration: sidebarMotionDurationMs,
                    easing: "cubic-bezier(0.16, 1, 0.3, 1)",
                    fill: "both",
                },
            );
            animation.finished.catch(() => {}).finally(() => animation.cancel());
            return animation;
        }).filter(Boolean);
    }

    function setSidebarMotionState(direction) {
        clearSidebarMotionState();
        const targets = syncSidebarGelTargets();
        if (
            !direction
            || sidebarOverlayMedia.matches
            || reducedMotionMedia.matches
            || !targets.length
            || !appShell.querySelector(sidebarGelTargetSelector)
        ) {
            return;
        }

        void appShell.offsetWidth;

        appShell.classList.add(
            "is-sidebar-animating",
            direction === "opening" ? "is-sidebar-opening" : "is-sidebar-closing",
        );
        sidebarMotionEndHandler = (event) => {
            if (!sidebarGelAnimationNames.has(event.animationName)) return;
            clearSidebarMotionState();
        };
        appShell.addEventListener("animationend", sidebarMotionEndHandler);
        sidebarMotionResetTimer = window.setTimeout(() => {
            clearSidebarMotionState();
        }, sidebarMotionDurationMs + 120);
    }

    function scheduleDockPosition() {
        if (!sidebarDock) return;
        if (dockPositionFrame) window.cancelAnimationFrame(dockPositionFrame);
        dockPositionFrame = window.requestAnimationFrame(() => {
            dockPositionFrame = 0;
            if (sidebarOverlayMedia.matches) {
                sidebarDock.style.left = "";
                return;
            }
            const sidebarRect = appSidebar.getBoundingClientRect();
            sidebarDock.style.left = `${Math.round(sidebarRect.left + sidebarRect.width / 2)}px`;
        });
    }

    function applySidebarState(nextIsOpen, options = {}) {
        const { persist = true, animate = false } = options;
        const wasOpen = isSidebarOpen;
        isSidebarOpen = Boolean(nextIsOpen);

        document.documentElement.classList.toggle("sidebar-memory-collapsed", !isSidebarOpen);
        sidebarToggle.setAttribute("aria-hidden", "false");
        sidebarToggle.setAttribute("aria-expanded", String(isSidebarOpen));
        appShell.classList.toggle("is-sidebar-open", isSidebarOpen);
        appShell.classList.toggle("is-sidebar-collapsed", !isSidebarOpen);

        appSidebar.hidden = false;
        appSidebar.style.display = "";
        appSidebar.setAttribute("aria-hidden", String(!isSidebarOpen));
        if ("inert" in appSidebar) appSidebar.inert = !isSidebarOpen;

        if (sidebarBackdrop) {
            const shouldShowBackdrop = sidebarOverlayMedia.matches && isSidebarOpen;
            sidebarBackdrop.hidden = !shouldShowBackdrop;
            sidebarBackdrop.setAttribute("aria-hidden", String(!shouldShowBackdrop));
            if ("inert" in sidebarBackdrop) sidebarBackdrop.inert = !shouldShowBackdrop;
            sidebarBackdrop.tabIndex = shouldShowBackdrop ? 0 : -1;
        }

        if (animate && wasOpen !== isSidebarOpen) {
            setSidebarMotionState(isSidebarOpen ? "opening" : "closing");
            const settleDelay = reducedMotionMedia.matches || sidebarOverlayMedia.matches
                ? 0
                : sidebarMotionDurationMs + 20;
            window.setTimeout(scheduleDockPosition, settleDelay);
        }
        if (persist) writeSidebarMemory(isSidebarOpen);
        scheduleDockPosition();
    }

    function applySidebarStateWithMotion(nextIsOpen, options = {}) {
        clearSidebarTitleMotion();
        const shouldAnimate = options.animate !== false;
        const commit = () => applySidebarState(nextIsOpen, {...options, animate: shouldAnimate});
        if (
            !shouldAnimate
            || nextIsOpen
            || reducedMotionMedia.matches
            || sidebarOverlayMedia.matches
        ) {
            commit();
            return;
        }

        const targets = Array.from(appShell.querySelectorAll(sidebarTitleTargetSelector))
            .filter((target) => target.getClientRects().length > 0);
        if (!targets.length) {
            commit();
            return;
        }
        const firstRects = targets.map((target) => target.getBoundingClientRect());
        commit();
        animateSidebarTitlesFrom(firstRects, targets);
    }

    window.setSidebarOpen = function setSidebarOpen(isOpen, options = {}) {
        if (options.animate) {
            applySidebarStateWithMotion(isOpen, options);
            return;
        }
        applySidebarState(isOpen, options);
    };

    syncSidebarGelTargets();
    applySidebarState(readSidebarMemory(), { persist: false });
    syncDockActiveState();
    rememberCurrentDockLocation();
    syncDockDestinations();

    sidebarDock?.addEventListener("click", (event) => {
        const dockLink = event.target instanceof Element
            ? event.target.closest("[data-dock-section], [data-section-link]")
            : null;
        if (!(dockLink instanceof HTMLAnchorElement)) return;
        rememberCurrentDockLocation();
        const rememberedLocation = readDockLocation(dockSectionForLink(dockLink));
        if (rememberedLocation) dockLink.href = rememberedLocation;
    });

    const localResourceFilterForm = document.querySelector(".browser-filter-form");
    localResourceFilterForm?.addEventListener("input", rememberCurrentDockLocation);
    localResourceFilterForm?.addEventListener("change", rememberCurrentDockLocation);

    document.addEventListener("click", (event) => {
        const categoryLink = event.target instanceof Element
            ? event.target.closest("[data-settings-category]")
            : null;
        if (!categoryLink) return;
        window.requestAnimationFrame(rememberCurrentDockLocation);
    });

    window.addEventListener("hashchange", rememberCurrentDockLocation);
    window.addEventListener("popstate", rememberCurrentDockLocation);

    sidebarToggle.addEventListener("click", () => {
        applySidebarStateWithMotion(!isSidebarOpen, { animate: true });
    });

    sidebarBackdrop?.addEventListener("click", () => {
        if (!sidebarOverlayMedia.matches || !isSidebarOpen) return;
        applySidebarStateWithMotion(false, { animate: true });
    });

    const handleViewportChange = () => {
        clearSidebarMotionState();
        clearSidebarTitleMotion();
        applySidebarState(isSidebarOpen, { persist: false });
    };
    if (typeof sidebarOverlayMedia.addEventListener === "function") {
        sidebarOverlayMedia.addEventListener("change", handleViewportChange);
    } else if (typeof sidebarOverlayMedia.addListener === "function") {
        sidebarOverlayMedia.addListener(handleViewportChange);
    }

    if (typeof reducedMotionMedia.addEventListener === "function") {
        reducedMotionMedia.addEventListener("change", () => {
            clearSidebarMotionState();
            clearSidebarTitleMotion();
        });
    } else if (typeof reducedMotionMedia.addListener === "function") {
        reducedMotionMedia.addListener(() => {
            clearSidebarMotionState();
            clearSidebarTitleMotion();
        });
    }

    window.addEventListener("resize", scheduleDockPosition);
    window.addEventListener("orientationchange", () => {
        scheduleDockPosition();
    });
    window.addEventListener("pageshow", () => {
        clearSidebarMotionState();
        clearSidebarTitleMotion();
        scheduleDockPosition();
        syncDockActiveState();
        rememberCurrentDockLocation();
    });
})();
