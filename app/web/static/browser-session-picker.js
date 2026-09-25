/* Code version: v1.9.1-codex.0 */

(() => {
    function closeOtherMenus(activePanel) {
        document.querySelectorAll("[data-browser-session-panel]").forEach((panel) => {
            if (panel !== activePanel) {
                panel.classList.remove("is-browser-menu-open");
                const trigger = panel.querySelector('[data-role="browser-picker-trigger"]');
                if (trigger) {
                    trigger.setAttribute("aria-expanded", "false");
                }
            }
        });
    }

    function initBrowserSessionPanel(panel) {
        const platform = panel.dataset.platform;
        const selectionStorageKey = panel.dataset.selectionStorageKey;
        const hiddenInputSelector = panel.dataset.hiddenInputSelector || "";
        const startButtonSelector = panel.dataset.startButtonSelector || "";
        const requiresDownloadReady = panel.dataset.requireDownloadReady === "true";
        const trigger = panel.querySelector('[data-role="browser-picker-trigger"]');
        const selectedLabel = panel.querySelector('[data-role="browser-picker-selected-label"]');
        const selectedIcon = panel.querySelector('[data-role="browser-picker-selected-icon"]');
        const selectedIconShell = panel.querySelector('[data-role="browser-picker-selected-icon-shell"]');
        const hiddenInput = hiddenInputSelector ? document.querySelector(hiddenInputSelector) : null;
        const startButton = startButtonSelector ? document.querySelector(startButtonSelector) : null;
        const startButtonInitiallyDisabled = startButton ? startButton.disabled : false;
        const optionButtons = Array.from(panel.querySelectorAll("[data-browser-option]"));
        const cachePage = document.querySelector("[data-cache-page]");
        const isClaudeCache = platform === "claude" && cachePage?.dataset.cacheSource === "claude";
        const contentModeControl = cachePage?.querySelector("[data-cache-content-mode]");
        let activeBrowser = "";
        let rememberedTextBrowser = "";
        let wasClaudeMedia = false;
        const statusController = window.CACHELIKES_BROWSER_SESSION_STATUS?.init(panel, {
            platform,
            getBrowser: () => activeBrowser,
        });

        function setStartButtonReady(isReady) {
            if (requiresDownloadReady) {
                panel.dataset.browserDownloadReady = String(isReady);
            }
            if (!requiresDownloadReady || !startButton || startButtonInitiallyDisabled) {
                return;
            }
            startButton.disabled = !isReady;
        }

        function setMenuOpen(isOpen) {
            panel.classList.toggle("is-browser-menu-open", isOpen);
            trigger.setAttribute("aria-expanded", String(isOpen));
            if (isOpen) {
                closeOtherMenus(panel);
            }
        }

        function cacheSelectionFromUrl() {
            const match = window.location.pathname.match(
                /^\/cache\/[^/]+\/(?:text|media)\/(safari|edge|chrome)$/,
            );
            return match ? match[1] : "";
        }

        function isClaudeMedia() {
            if (!isClaudeCache) return false;
            const match = window.location.pathname.match(/^\/cache\/claude\/(text|media)\//);
            return (match ? match[1] : cachePage.dataset.cachePageContentMode) === "media";
        }

        function syncClaudeBrowserOptions() {
            if (!isClaudeCache) return;
            const media = isClaudeMedia();
            optionButtons.forEach((button) => {
                const unavailable = media && button.dataset.browserOption !== "safari";
                button.hidden = unavailable;
                button.disabled = unavailable;
            });
            if (media) {
                if (!wasClaudeMedia && activeBrowser && activeBrowser !== "safari") {
                    rememberedTextBrowser = activeBrowser;
                }
                const safariAvailable = optionButtons.some((button) => button.dataset.browserOption === "safari");
                trigger.disabled = !safariAvailable;
                const selected = safariAvailable ? "safari" : "";
                if (activeBrowser !== selected) {
                    setSelectedBrowser(selected);
                    syncCacheBrowserUrl(selected);
                    statusController?.setBrowser(selected);
                }
            } else {
                trigger.disabled = false;
                if (wasClaudeMedia && rememberedTextBrowser && activeBrowser !== rememberedTextBrowser) {
                    setSelectedBrowser(rememberedTextBrowser);
                    syncCacheBrowserUrl(rememberedTextBrowser);
                    statusController?.setBrowser(rememberedTextBrowser);
                }
            }
            wasClaudeMedia = media;
            setMenuOpen(false);
        }

        function syncCacheBrowserUrl(browserId) {
            const page = document.querySelector("[data-cache-page]");
            if (!page || !page.querySelector("[data-cache-content-mode]") || !browserId) return;
            const source = page.dataset.cacheSource || "";
            const modeFromUrl = window.location.pathname.match(/^\/cache\/[^/]+\/(text|media)\//);
            const mode = modeFromUrl ? modeFromUrl[1] : (
                page.dataset.cachePageContentMode === "media" ? "media" : "text"
            );
            if (!source) return;
            page.querySelectorAll("[data-cache-content-mode-option]").forEach((option) => {
                const optionMode = option.dataset.cacheContentModeOption === "media" ? "media" : "text";
                option.setAttribute("href", `/cache/${source}/${optionMode}/${browserId}`);
            });
            const nextPath = `/cache/${source}/${mode}/${browserId}`;
            if (window.location.pathname === nextPath) {
                page.dataset.cacheBrowser = browserId;
                return;
            }
            window.history.replaceState(null, "", nextPath);
            page.dataset.cacheBrowser = browserId;
        }

        function setSelectedBrowser(browserId) {
            activeBrowser = browserId || "";
            if (hiddenInput) {
                hiddenInput.value = activeBrowser;
            }
            optionButtons.forEach((button) => {
                button.classList.toggle("is-selected", button.dataset.browserOption === activeBrowser);
                button.setAttribute("aria-selected", String(button.dataset.browserOption === activeBrowser));
            });

            const selectedButton = optionButtons.find((button) => button.dataset.browserOption === activeBrowser);
            if (!selectedButton) {
                selectedLabel.textContent = isClaudeMedia()
                    ? "Safari required on macOS"
                    : "Select browser";
                selectedIcon.removeAttribute("src");
                selectedIcon.alt = "";
                selectedIconShell.hidden = true;
                setStartButtonReady(false);
                try {
                    if (!isClaudeMedia()) window.sessionStorage.setItem(selectionStorageKey, "");
                } catch (_error) {
                }
                return;
            }

            selectedLabel.textContent = selectedButton.dataset.browserLabel;
            selectedIcon.src = selectedButton.dataset.browserIcon;
            selectedIcon.alt = `${selectedButton.dataset.browserLabel} icon`;
            selectedIconShell.hidden = false;
        }

        trigger.addEventListener("click", () => {
            setMenuOpen(!panel.classList.contains("is-browser-menu-open"));
        });

        optionButtons.forEach((button) => {
            button.addEventListener("click", () => {
                if (button.hidden || button.disabled) return;
                const browserId = button.dataset.browserOption || "";
                setSelectedBrowser(browserId);
                setMenuOpen(false);
                if (!isClaudeMedia()) {
                    rememberedTextBrowser = browserId;
                    try {
                        window.sessionStorage.setItem(selectionStorageKey, browserId);
                    } catch (_error) {
                    }
                }
                syncCacheBrowserUrl(browserId);
                statusController?.setBrowser(browserId);
            });
        });

        document.addEventListener("click", (event) => {
            if (!panel.contains(event.target)) {
                setMenuOpen(false);
            }
        });

        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape") {
                setMenuOpen(false);
            }
        });

        const storedSelection = (() => {
            try {
                return window.sessionStorage.getItem(selectionStorageKey) || "";
            } catch (_error) {
                return "";
            }
        })();
        const urlSelection = cacheSelectionFromUrl();
        const hiddenInputSelection = hiddenInput ? (hiddenInput.value || "").trim() : "";
        const defaultBrowserId = optionButtons.length === 1 ? (optionButtons[0].dataset.browserOption || "") : "";
        const initialBrowserId = optionButtons.some((button) => button.dataset.browserOption === urlSelection)
            ? urlSelection
            : optionButtons.some((button) => button.dataset.browserOption === storedSelection)
                ? storedSelection
            : optionButtons.some((button) => button.dataset.browserOption === hiddenInputSelection)
                ? hiddenInputSelection
            : defaultBrowserId;
        rememberedTextBrowser = storedSelection || initialBrowserId;
        wasClaudeMedia = isClaudeMedia();
        const initialBrowser = wasClaudeMedia
            ? (optionButtons.some((button) => button.dataset.browserOption === "safari") ? "safari" : "")
            : initialBrowserId;
        if (isClaudeCache) {
            optionButtons.forEach((button) => {
                const unavailable = wasClaudeMedia && button.dataset.browserOption !== "safari";
                button.hidden = unavailable;
                button.disabled = unavailable;
            });
            trigger.disabled = wasClaudeMedia && !initialBrowser;
            contentModeControl?.addEventListener("click", syncClaudeBrowserOptions);
            window.addEventListener("popstate", syncClaudeBrowserOptions);
        }

        if (initialBrowser) {
            setSelectedBrowser(initialBrowser);
            if (!wasClaudeMedia) {
                try {
                    window.sessionStorage.setItem(selectionStorageKey, initialBrowser);
                } catch (_error) {
                }
            }
            syncCacheBrowserUrl(initialBrowser);
            statusController?.setBrowser(initialBrowser);
            return;
        }

        setSelectedBrowser("");
        setStartButtonReady(false);
    }

    document.addEventListener("DOMContentLoaded", () => {
        document.querySelectorAll("[data-browser-session-panel]").forEach((panel) => {
            initBrowserSessionPanel(panel);
        });
    });
})();
