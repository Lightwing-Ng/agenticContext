/* Code version: v1.4.1-codex.0 */

(() => {
    "use strict";

    const contentModeStorageKey = "cachelikes:browser-content-mode:v1";
    const contentModeControl = document.querySelector("[data-cache-content-mode]");
    const contentModeInput = document.querySelector("[data-chatgpt-content-mode-input]");
    const mediaConfig = document.querySelector("[data-chatgpt-media-config]");
    const projectPicker = document.querySelector("[data-chatgpt-project-picker]");

    if (!contentModeInput || !mediaConfig) return;

    const browserInput = document.querySelector("#chatgpt_browser_input");
    const projectUrlInput = projectPicker?.querySelector("[data-chatgpt-project-url]");
    const projectNameInput = projectPicker?.querySelector("[data-chatgpt-project-name]");
    const projectTrigger = projectPicker?.querySelector("[data-chatgpt-project-trigger]");
    const projectSelectedLabel = projectPicker?.querySelector("[data-chatgpt-project-selected-label]");
    const projectSpinner = projectPicker?.querySelector("[data-chatgpt-project-spinner]");
    const projectMenu = projectPicker?.querySelector("[data-chatgpt-project-menu]");
    const projectCatalogTimeoutMs = 15000;
    let projectRequestId = 0;
    let projectRequestController = null;
    let projectCatalogBrowser = "";
    let projectCatalogLoaded = false;

    function readContentMode() {
        try {
            return window.sessionStorage.getItem(contentModeStorageKey) === "media" ? "media" : "text";
        } catch (_error) {
            return "text";
        }
    }

    function selectedBrowser() {
        return String(browserInput?.value || "").trim().toLowerCase();
    }

    function projectUrlKey(value) {
        const url = String(value || "").trim();
        try {
            const parsed = new URL(url);
            const projectPath = parsed.pathname.match(/^\/g\/(g-p-[^/]+?)(?:\/project)?\/?$/i);
            if (parsed.protocol === "https:"
                && ["chatgpt.com", "www.chatgpt.com"].includes(parsed.hostname)
                && !parsed.port && !parsed.username && !parsed.password && projectPath) {
                // Match the Agent catalog's stable identity across renamed project slugs.
                const projectId = projectPath[1].match(/^g-p-[0-9a-f]{32}(?=-|$)/i)?.[0];
                return `https://chatgpt.com/g/${(projectId || projectPath[1]).toLowerCase()}`;
            }
        } catch (_error) {
            // Unrecognized locations keep their exact saved identity.
        }
        return url;
    }

    function projectOptionButton(url, name, selected = false, project = {}) {
        const option = document.createElement("button");
        option.type = "button";
        option.className = `trade-strategy-dropdown-option agent-combobox-option${selected ? " is-selected is-active" : ""}`;
        option.dataset.chatgptProjectOption = "true";
        option.dataset.chatgptProjectUrl = url;
        option.dataset.chatgptProjectName = name;
        option.setAttribute("role", "option");
        option.setAttribute("aria-selected", String(selected));
        option.tabIndex = -1;

        const check = document.createElement("span");
        check.className = "trade-strategy-dropdown-check";
        check.setAttribute("aria-hidden", "true");
        const text = document.createElement("span");
        text.className = "trade-strategy-dropdown-text";
        text.textContent = name || "All generated media";
        const fallbackIcon = url ? "/static/images/chatgpt-project-terminal.svg" : "";
        const iconUrl = window.CACHELIKES_CHATGPT_PROJECT_ICONS?.projectIcon(project, fallbackIcon) || fallbackIcon;
        option.dataset.chatgptProjectIcon = iconUrl;
        if (iconUrl) {
            const icon = document.createElement("img");
            icon.className = "browser-picker-option-icon";
            icon.alt = "";
            icon.src = iconUrl;
            icon.setAttribute("aria-hidden", "true");
            option.append(check, icon, text);
        } else {
            option.append(check, text);
            option.style.gridTemplateColumns = "16px minmax(0, 1fr)";
        }
        return option;
    }

    function setProjectTriggerLabel(label) {
        const nextLabel = label || "All generated media";
        if (projectSelectedLabel) projectSelectedLabel.textContent = nextLabel;
        projectTrigger?.setAttribute("aria-label", `Project: ${nextLabel}`);
    }

    function applyProjectSelection(option) {
        if (!option || !(projectUrlInput instanceof HTMLInputElement)) return;
        const url = String(option.dataset.chatgptProjectUrl || "");
        const name = String(option.dataset.chatgptProjectName || "");
        projectUrlInput.value = url;
        if (projectNameInput instanceof HTMLInputElement) projectNameInput.value = name;
        projectMenu?.querySelectorAll("[data-chatgpt-project-option]").forEach((candidate) => {
            const selected = candidate === option;
            candidate.classList.toggle("is-selected", selected);
            candidate.classList.toggle("is-active", selected);
            candidate.setAttribute("aria-selected", String(selected));
        });
        setProjectTriggerLabel(name);
        const icon = projectPicker.querySelector("[data-chatgpt-project-selected-icon]");
        const shell = projectPicker.querySelector("[data-chatgpt-project-icon-shell]");
        const iconUrl = option.dataset.chatgptProjectIcon || "";
        if (shell) shell.hidden = !iconUrl;
        if (icon && iconUrl) icon.src = iconUrl;
        else icon?.removeAttribute("src");
    }

    function closeProjectMenu() {
        if (!projectPicker || !projectTrigger || !projectMenu) return;
        projectPicker.classList.remove("is-agent-combobox-open");
        projectTrigger.setAttribute("aria-expanded", "false");
        projectMenu.hidden = true;
    }

    function openProjectMenu() {
        if (!projectPicker || !projectTrigger || !projectMenu || projectTrigger.disabled) return;
        projectPicker.classList.add("is-agent-combobox-open");
        projectTrigger.setAttribute("aria-expanded", "true");
        projectMenu.hidden = false;
    }

    function setProjectLoading(loading) {
        if (projectSpinner) projectSpinner.hidden = !loading;
        if (projectTrigger) {
            projectTrigger.disabled = loading || contentModeInput.value !== "media";
            if (loading) {
                projectTrigger.setAttribute("aria-busy", "true");
                projectTrigger.setAttribute("aria-label", "Project: Loading recent projects");
            } else {
                projectTrigger.removeAttribute("aria-busy");
            }
        }
    }

    function renderProjectOptions(projects, errorMessage = "") {
        if (!projectMenu || !(projectUrlInput instanceof HTMLInputElement)) return;
        const selectedUrl = String(projectUrlInput.value || "").trim();
        const selectedKey = projectUrlKey(selectedUrl);
        const savedName = String(projectNameInput?.value || "").trim();
        const options = [projectOptionButton("", "All generated media", !selectedUrl)];
        let selectedOption = selectedUrl ? null : options[0];
        const seenProjects = new Set([""]);

        (Array.isArray(projects) ? projects : []).forEach((project) => {
            const url = String(project?.url || "").trim();
            const key = projectUrlKey(url);
            const name = String(project?.title || "").trim() || "Untitled project";
            if (!url || seenProjects.has(key)) return;
            seenProjects.add(key);
            const option = projectOptionButton(url, name, key === selectedKey, project);
            if (key === selectedKey) selectedOption = option;
            options.push(option);
        });

        if (selectedUrl && !selectedOption) {
            selectedOption = projectOptionButton(selectedUrl, savedName || "Saved project", true);
            selectedOption.dataset.chatgptProjectFallback = "true";
            options.push(selectedOption);
        }

        projectMenu.replaceChildren(...options);
        applyProjectSelection(selectedOption || options[0]);
        if (projectTrigger) {
            projectTrigger.disabled = contentModeInput.value !== "media";
            if (errorMessage) projectTrigger.title = errorMessage;
            else projectTrigger.removeAttribute("title");
        }
    }

    async function loadProjects({forceRefresh = false} = {}) {
        if (!projectPicker || contentModeInput.value !== "media") return;
        const browser = selectedBrowser();
        if (!browser) {
            renderProjectOptions([], "Select an authorized browser first.");
            return;
        }
        if (!forceRefresh && projectCatalogBrowser === browser && (projectCatalogLoaded || projectRequestController)) return;
        projectRequestController?.abort();
        const controller = new AbortController();
        projectRequestController = controller;
        const requestId = ++projectRequestId;
        const timeoutId = window.setTimeout(() => controller.abort(), projectCatalogTimeoutMs);
        projectCatalogBrowser = browser;
        projectCatalogLoaded = false;
        setProjectLoading(true);

        try {
            const query = new URLSearchParams({browser});
            if (forceRefresh) query.set("refresh", "1");
            const response = await fetch(`/api/agent/chatgpt-sources?${query.toString()}`, {
                cache: "no-store",
                signal: controller.signal,
                headers: {"Accept": "application/json"},
            });
            let payload;
            try {
                payload = await response.json();
            } catch (_jsonError) {
                throw new Error("The server returned a malformed project catalog response.");
            }
            if (!response.ok) throw new Error(payload.error || "Recent projects are unavailable.");
            if (requestId !== projectRequestId || browser !== selectedBrowser()) return;
            projectCatalogLoaded = true;
            renderProjectOptions(payload.projects, "");
        } catch (error) {
            if (requestId !== projectRequestId) return;
            const message = error?.name === "AbortError"
                ? "Recent projects timed out after 15 seconds."
                : error?.message || "Recent projects are unavailable.";
            renderProjectOptions([], message);
        } finally {
            window.clearTimeout(timeoutId);
            if (requestId === projectRequestId) {
                projectRequestController = null;
                setProjectLoading(false);
            }
        }
    }

    function bindProjectPicker() {
        if (!projectPicker || !projectTrigger || !projectMenu) return;

        projectTrigger.addEventListener("click", () => {
            if (projectMenu.hidden) openProjectMenu();
            else closeProjectMenu();
            void loadProjects();
        });
        projectMenu.addEventListener("click", (event) => {
            const option = event.target.closest("[data-chatgpt-project-option]");
            if (!option || !projectMenu.contains(option)) return;
            applyProjectSelection(option);
            closeProjectMenu();
        });
        projectTrigger.addEventListener("keydown", (event) => {
            if (!["ArrowDown", "Enter", " "].includes(event.key)) return;
            event.preventDefault();
            openProjectMenu();
            projectMenu.querySelector('[aria-selected="true"]')?.focus();
        });
        projectMenu.addEventListener("keydown", (event) => {
            const options = Array.from(projectMenu.querySelectorAll("[data-chatgpt-project-option]"));
            const currentIndex = options.indexOf(event.target);
            if (currentIndex < 0) return;
            let nextIndex = currentIndex;
            if (event.key === "ArrowDown") nextIndex = Math.min(currentIndex + 1, options.length - 1);
            else if (event.key === "ArrowUp") nextIndex = Math.max(currentIndex - 1, 0);
            else if (event.key === "Home") nextIndex = 0;
            else if (event.key === "End") nextIndex = options.length - 1;
            else if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                applyProjectSelection(event.target);
                closeProjectMenu();
                projectTrigger.focus();
                return;
            } else if (event.key === "Escape") {
                event.preventDefault();
                closeProjectMenu();
                projectTrigger.focus();
                return;
            } else return;
            event.preventDefault();
            options[nextIndex]?.focus();
        });
        document.addEventListener("click", (event) => {
            if (!projectPicker.contains(event.target)) closeProjectMenu();
            const browserOption = event.target.closest("[data-browser-option]");
            if (browserOption) window.setTimeout(() => loadProjects(), 0);
        });
        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape") closeProjectMenu();
        });
    }

    function applyContentMode(mode) {
        const normalizedMode = mode === "media" ? "media" : "text";
        contentModeInput.value = normalizedMode;
        mediaConfig.hidden = normalizedMode === "text";
        mediaConfig.querySelectorAll("input, select, textarea, button").forEach((control) => {
            control.disabled = normalizedMode === "text";
        });
        if (contentModeControl) {
            const options = Array.from(
                contentModeControl.querySelectorAll("[data-cache-content-mode-option]"),
            );
            const activeIndex = options.findIndex(
                (option) => option.dataset.cacheContentModeOption === normalizedMode,
            );
            contentModeControl.dataset.segmentedActiveIndex = String(Math.max(activeIndex, 0));
            options.forEach((option) => {
                const isActive = option.dataset.cacheContentModeOption === normalizedMode;
                option.classList.toggle("is-active", isActive);
                option.setAttribute("aria-checked", String(isActive));
            });
            window.CACHELIKES_SEGMENTED_CONTROLS?.sync(contentModeControl);
        }
        try {
            window.sessionStorage.setItem(contentModeStorageKey, normalizedMode);
        } catch (_error) {
        }
        if (normalizedMode === "media") void loadProjects();
        else closeProjectMenu();
    }

    bindProjectPicker();
    applyContentMode(readContentMode());
    contentModeControl?.addEventListener("click", (event) => {
        const option = event.target.closest("[data-cache-content-mode-option]");
        if (!option || !contentModeControl.contains(option)) return;
        event.preventDefault();
        applyContentMode(option.dataset.cacheContentModeOption);
    });
})();
