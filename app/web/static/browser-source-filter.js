/* Code version: v1.4.0-codex.1 */

(() => {
    const controllerUrl = new URL("select-controller.js?v=select-controller-v1.0.0", document.currentScript.src);
    function initializeSourceFilter(combobox) {
        const form = combobox.closest("form");
        const trigger = combobox.querySelector("[data-browser-source-filter-trigger]");
        const menu = combobox.querySelector("[data-browser-source-filter-menu]");
        const input = combobox.querySelector("[data-browser-source-filter-input]");
        const selectedLabel = combobox.querySelector("[data-browser-source-filter-selected-label]");
        const selectedIcon = combobox.querySelector("[data-browser-source-filter-selected-icon]");
        const options = Array.from(combobox.querySelectorAll("[data-browser-source-filter-option]"));
        if (!form || !trigger || !menu || !input || !selectedLabel || !selectedIcon || !options.length) {
            return;
        }

        function selectedOption() {
            return options.find((option) => option.dataset.browserSourceFilterOption === input.value)
                || options[0];
        }

        function syncTriggerMetadata(option) {
            const label = option?.dataset.browserSourceFilterLabel || "All sources";
            trigger.setAttribute("aria-label", `Source: ${label}`);
            trigger.title = label;
        }

        const controller = window.SHARED_SELECT.createController({
            getTrigger: () => trigger,
            getMenu: () => menu,
            getOptions: () => options,
            open: () => setMenuOpen(true),
            close: () => setMenuOpen(false),
        });

        function setMenuOpen(isOpen) {
            combobox.classList.toggle("is-open", isOpen);
            trigger.setAttribute("aria-expanded", String(isOpen));
            menu.hidden = !isOpen;
            if (combobox.hasAttribute("data-browser-header-filter")) {
                if (isOpen) {
                    document.body.append(menu);
                    const rect = trigger.getBoundingClientRect();
                    menu.style.left = `${Math.max(10, Math.min(rect.left, window.innerWidth - menu.offsetWidth - 10))}px`;
                    menu.style.top = `${Math.min(rect.bottom + 4, window.innerHeight - menu.offsetHeight - 10)}px`;
                } else {
                    combobox.append(menu);
                }
            }
            if (isOpen) {
                controller.highlightSelected();
            } else {
                trigger.removeAttribute("aria-activedescendant");
            }
        }

        function selectOption(option) {
            const value = option.dataset.browserSourceFilterOption || "all";
            input.value = value;
            selectedLabel.textContent = option.dataset.browserSourceFilterLabel || "All sources";
            const iconUrl = option.dataset.browserSourceFilterIcon || "";
            selectedIcon.style.setProperty("--cache-source-mark", `url("${iconUrl}")`);
            syncTriggerMetadata(option);
            options.forEach((candidate) => {
                const isSelected = candidate === option;
                candidate.classList.toggle("is-selected", isSelected);
                candidate.classList.toggle("is-active", isSelected);
                candidate.setAttribute("aria-selected", String(isSelected));
            });
            setMenuOpen(false);
            form.requestSubmit();
        }

        trigger.addEventListener("click", () => setMenuOpen(menu.hidden));
        controller.bindKeyboard();
        options.forEach((option) => {
            option.addEventListener("click", () => selectOption(option));
        });

        syncTriggerMetadata(selectedOption());

        document.addEventListener("click", (event) => {
            if (!combobox.contains(event.target) && !menu.contains(event.target)) {
                setMenuOpen(false);
            }
        });
        if (combobox.hasAttribute("data-browser-header-filter")) {
            window.addEventListener("resize", () => setMenuOpen(false));
            document.addEventListener("scroll", (event) => {
                if (!menu.contains(event.target) && !menu.hidden) setMenuOpen(false);
            }, true);
        }
        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape" && !menu.hidden) {
                setMenuOpen(false);
                trigger.focus({ preventScroll: true });
            }
        });
    }

    document.addEventListener("DOMContentLoaded", async () => {
        // Cached templates may predate the shared script tag.
        if (!window.SHARED_SELECT) await import(controllerUrl.href);
        document.querySelectorAll("[data-browser-source-filter]").forEach(initializeSourceFilter);
    });
})();
