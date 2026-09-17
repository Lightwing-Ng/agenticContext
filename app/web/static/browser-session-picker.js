/* Code version: v1.8.1-codex.0 */

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
        let activeBrowser = "";
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

        function syncCacheBrowserUrl(browserId) {
            const page = document.querySelector("[data-cache-page]");
            if (!page || !page.querySelector("[data-cache-content-mode]") || !browserId) return;
            const source = page.dataset.cacheSource || "";
            const mode = page.dataset.cacheContentMode === "media" ? "media" : "text";
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
                selectedLabel.textContent = "Select browser";
                selectedIcon.removeAttribute("src");
                selectedIcon.alt = "";
                selectedIconShell.hidden = true;
                setStartButtonReady(false);
                try {
                    window.sessionStorage.setItem(selectionStorageKey, "");
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
                const browserId = button.dataset.browserOption || "";
                setSelectedBrowser(browserId);
                setMenuOpen(false);
                try {
                    window.sessionStorage.setItem(selectionStorageKey, browserId);
                } catch (_error) {
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

        if (initialBrowserId) {
            setSelectedBrowser(initialBrowserId);
            try {
                window.sessionStorage.setItem(selectionStorageKey, initialBrowserId);
            } catch (_error) {
            }
            syncCacheBrowserUrl(initialBrowserId);
            statusController?.setBrowser(initialBrowserId);
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
