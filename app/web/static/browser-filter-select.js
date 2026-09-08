/* Code version: v1.1.0-codex.1 */

(() => {
    const controllerUrl = new URL("select-controller.js?v=select-controller-v1.0.0", document.currentScript.src);
    let selectIndex = 0;

    function closeOtherMenus(activeSelect) {
        document.querySelectorAll("[data-browser-filter-select]").forEach((selectShell) => {
            if (selectShell !== activeSelect) {
                setMenuOpen(selectShell, false);
            }
        });
    }

    function setMenuOpen(selectShell, isOpen) {
        const trigger = selectShell.querySelector("[data-browser-filter-select-trigger]");
        const menu = selectShell.querySelector("[data-browser-filter-select-menu]");
        if (!trigger || !menu) return;

        selectShell.classList.toggle("is-open", isOpen);
        trigger.setAttribute("aria-expanded", String(isOpen));
        menu.hidden = !isOpen;
        if (isOpen) {
            closeOtherMenus(selectShell);
        } else {
            trigger.removeAttribute("aria-activedescendant");
        }
    }

    function initializeSelect(select) {
        if (!(select instanceof HTMLSelectElement) || select.dataset.browserFilterSelectBound === "1") {
            return;
        }
        const form = select.closest("form");
        if (!form || !select.options.length) return;

        select.dataset.browserFilterSelectBound = "1";
        const selectName = select.name || "option";
        const selectId = select.id || `browser_filter_${selectName}_${selectIndex++}`;
        const selectShell = document.createElement("div");
        selectShell.className = "trade-strategy-combobox browser-filter-select";
        selectShell.dataset.browserFilterSelect = "";

        select.parentElement.insertBefore(selectShell, select);
        selectShell.appendChild(select);
        select.classList.add("browser-filter-native-select");
        select.hidden = true;
        select.setAttribute("aria-hidden", "true");
        select.tabIndex = -1;

        const trigger = document.createElement("button");
        trigger.type = "button";
        trigger.className = "trade-strategy-select form-select trade-strategy-trigger browser-filter-select-trigger";
        trigger.dataset.browserFilterSelectTrigger = "";
        trigger.setAttribute("aria-haspopup", "listbox");
        trigger.setAttribute("aria-expanded", "false");
        trigger.setAttribute("aria-controls", `${selectId}_options`);

        const selectedLabel = document.createElement("span");
        selectedLabel.className = "trade-strategy-trigger-label browser-session-trigger-label";
        selectedLabel.dataset.browserFilterSelectLabel = "";
        const chevron = document.createElement("span");
        chevron.className = "browser-picker-trigger-chevron";
        chevron.setAttribute("aria-hidden", "true");
        trigger.append(selectedLabel, chevron);

        const menu = document.createElement("div");
        menu.className = "trade-strategy-dropdown browser-filter-select-dropdown";
        menu.id = `${selectId}_options`;
        menu.dataset.browserFilterSelectMenu = "";
        menu.setAttribute("role", "listbox");
        menu.setAttribute("aria-label", select.getAttribute("aria-label") || selectName);
        menu.hidden = true;

        const options = Array.from(select.options).map((nativeOption, optionIndex) => {
            const option = document.createElement("button");
            option.type = "button";
            option.disabled = nativeOption.disabled || nativeOption.parentElement?.disabled === true;
            option.className = "trade-strategy-dropdown-option browser-filter-select-option";
            option.id = `${selectId}_option_${optionIndex}`;
            option.dataset.browserFilterSelectOption = nativeOption.value;
            option.setAttribute("role", "option");
            option.setAttribute("aria-selected", "false");
            option.tabIndex = -1;

            const check = document.createElement("span");
            check.className = "trade-strategy-dropdown-check";
            check.setAttribute("aria-hidden", "true");
            const label = document.createElement("span");
            label.className = "trade-strategy-dropdown-text";
            label.textContent = nativeOption.textContent.trim();
            option.append(check, label);
            menu.appendChild(option);
            return option;
        });

        selectShell.append(trigger, menu);

        function selectedOption() {
            return options.find((option) => option.dataset.browserFilterSelectOption === select.value)
                || options[0];
        }

        function syncSelection() {
            const option = selectedOption();
            const label = option?.querySelector(".trade-strategy-dropdown-text")?.textContent?.trim() || "";
            selectedLabel.textContent = label;
            trigger.setAttribute("aria-label", `${select.getAttribute("aria-label") || selectName}: ${label}`);
            trigger.title = label;
            options.forEach((candidate) => {
                const isSelected = candidate === option;
                candidate.classList.toggle("is-selected", isSelected);
                candidate.classList.toggle("is-active", isSelected);
                candidate.setAttribute("aria-selected", String(isSelected));
            });
        }

        const controller = window.SHARED_SELECT.createController({
            getTrigger: () => trigger,
            getMenu: () => menu,
            getOptions: () => options,
            open: () => setMenuOpen(selectShell, true),
            close: () => setMenuOpen(selectShell, false),
        });

        function selectOption(option) {
            if (!option) return;
            select.value = option.dataset.browserFilterSelectOption || "";
            syncSelection();
            setMenuOpen(selectShell, false);
            select.dispatchEvent(new Event("change", {bubbles: true}));
        }

        trigger.addEventListener("click", () => {
            const isOpen = selectShell.classList.contains("is-open");
            setMenuOpen(selectShell, !isOpen);
            if (!isOpen) controller.highlightSelected();
        });

        controller.bindKeyboard();
        options.forEach((option) => {
            option.addEventListener("click", () => selectOption(option));
        });

        select.addEventListener("change", syncSelection);
        syncSelection();
    }

    document.addEventListener("click", (event) => {
        if (event.target instanceof Element && event.target.closest("[data-browser-filter-select]")) return;
        document.querySelectorAll("[data-browser-filter-select]").forEach((selectShell) => setMenuOpen(selectShell, false));
    });

    document.addEventListener("keydown", (event) => {
        if (event.key !== "Escape") return;
        document.querySelectorAll("[data-browser-filter-select]").forEach((selectShell) => setMenuOpen(selectShell, false));
    });

    document.addEventListener("DOMContentLoaded", async () => {
        // Cached templates may predate the shared script tag.
        if (!window.SHARED_SELECT) await import(controllerUrl.href);
        document.querySelectorAll(".browser-filter-form select.form-select").forEach(initializeSelect);
    });
})();
