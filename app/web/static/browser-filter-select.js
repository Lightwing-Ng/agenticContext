/* Code version: v1.2.0-codex.1 */

(() => {
    const controllerUrl = new URL("select-controller.js?v=select-controller-v1.0.1", document.currentScript.src);
    const selectSelector = ".browser-filter-form select.form-select, select[data-shared-select-auto]";
    const selectStates = new WeakMap();
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

        const canOpen = isOpen && !trigger.disabled && !selectShell.hidden;
        selectShell.classList.toggle("is-open", canOpen);
        trigger.setAttribute("aria-expanded", String(canOpen));
        menu.hidden = !canOpen;
        if (canOpen) {
            closeOtherMenus(selectShell);
        } else {
            trigger.removeAttribute("aria-activedescendant");
        }
    }

    function fieldLabel(select) {
        return select.getAttribute("aria-label")
            || document.querySelector(`label[for="${CSS.escape(select.id)}"]`)?.textContent?.trim()
            || select.name
            || "Option";
    }

    function nativeOptionDisabled(nativeOption) {
        return nativeOption.disabled || nativeOption.parentElement?.disabled === true;
    }

    function initializeSelect(select) {
        if (!(select instanceof HTMLSelectElement)
            || select.dataset.browserFilterSelectBound === "1"
            || select.hidden
            || !select.options.length) {
            return;
        }

        select.dataset.browserFilterSelectBound = "1";
        const selectName = select.name || "option";
        const selectId = select.id || `shared_select_${selectName}_${selectIndex++}`;
        const selectKind = select.dataset.sharedSelectKind || selectName.replaceAll("_", "-");
        select.id = selectId;

        const selectShell = document.createElement("div");
        selectShell.className = "trade-strategy-combobox browser-filter-select";
        selectShell.dataset.browserFilterSelect = "";
        selectShell.dataset.sharedSelectField = "";
        selectShell.dataset.sharedSelectKind = selectKind;

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
        trigger.dataset.sharedSelectTrigger = "";
        trigger.setAttribute("aria-haspopup", "listbox");
        trigger.setAttribute("aria-expanded", "false");
        trigger.setAttribute("aria-controls", `${selectId}_options`);

        const selectedLabel = document.createElement("span");
        selectedLabel.className = "trade-strategy-trigger-label browser-session-trigger-label";
        selectedLabel.dataset.browserFilterSelectLabel = "";
        selectedLabel.dataset.sharedSelectTriggerLabel = "";
        const chevron = document.createElement("span");
        chevron.className = "browser-picker-trigger-chevron";
        chevron.setAttribute("aria-hidden", "true");
        trigger.append(selectedLabel, chevron);

        const menu = document.createElement("div");
        menu.className = "trade-strategy-dropdown browser-filter-select-dropdown";
        menu.id = `${selectId}_options`;
        menu.dataset.browserFilterSelectMenu = "";
        menu.dataset.sharedSelectDropdown = "";
        menu.setAttribute("role", "listbox");
        menu.setAttribute("aria-label", fieldLabel(select));
        menu.hidden = true;
        selectShell.append(trigger, menu);

        const state = {options: []};
        selectStates.set(select, {selectShell, trigger, menu, state});

        function selectedOption() {
            return state.options.find((option) => option.nativeOption.selected)
                || state.options.find((option) => option.nativeOption.value === select.value)
                || state.options[0];
        }

        function syncSelection() {
            const optionState = selectedOption();
            const label = optionState?.nativeOption.textContent?.trim() || "";
            selectedLabel.textContent = label;
            trigger.setAttribute("aria-label", `${fieldLabel(select)}: ${label}`);
            trigger.title = label;
            trigger.disabled = select.disabled;
            selectShell.setAttribute("aria-disabled", String(select.disabled));
            state.options.forEach((candidate) => {
                const isSelected = candidate === optionState;
                candidate.button.classList.toggle("is-selected", isSelected);
                candidate.button.classList.toggle("is-active", isSelected);
                candidate.button.setAttribute("aria-selected", String(isSelected));
            });
            if (select.disabled) setMenuOpen(selectShell, false);
        }

        function selectOption(optionState) {
            if (!optionState || nativeOptionDisabled(optionState.nativeOption)) return;
            const previousValue = select.value;
            if (optionState.nativeOption.value === previousValue) {
                trigger.focus({preventScroll: true});
                setMenuOpen(selectShell, false);
                return;
            }
            Array.from(select.options).forEach((nativeOption) => {
                const isSelected = nativeOption === optionState.nativeOption;
                nativeOption.selected = isSelected;
                nativeOption.defaultSelected = isSelected;
            });
            select.value = optionState.nativeOption.value;
            syncSelection();
            trigger.focus({preventScroll: true});
            setMenuOpen(selectShell, false);
            select.dispatchEvent(new Event("change", {bubbles: true}));
        }

        function renderOptions() {
            menu.replaceChildren();
            state.options = Array.from(select.options).map((nativeOption, optionIndex) => {
                const option = document.createElement("button");
                const isDisabled = nativeOptionDisabled(nativeOption);
                option.type = "button";
                option.disabled = isDisabled;
                option.hidden = nativeOption.hidden;
                option.className = "trade-strategy-dropdown-option browser-filter-select-option";
                option.id = `${selectId}_option_${optionIndex}`;
                option.dataset.browserFilterSelectOption = nativeOption.value;
                option.dataset.sharedSelectOption = nativeOption.value;
                option.setAttribute("role", "option");
                option.setAttribute("aria-selected", "false");
                option.setAttribute("aria-disabled", String(isDisabled));
                option.tabIndex = -1;

                const check = document.createElement("span");
                check.className = "trade-strategy-dropdown-check";
                check.setAttribute("aria-hidden", "true");
                const label = document.createElement("span");
                label.className = "trade-strategy-dropdown-text";
                label.textContent = nativeOption.textContent.trim();
                option.append(check, label);
                const optionState = {button: option, nativeOption};
                option.addEventListener("click", () => selectOption(optionState));
                menu.appendChild(option);
                return optionState;
            });
            syncSelection();
        }

        const controller = window.SHARED_SELECT.createController({
            getTrigger: () => trigger,
            getMenu: () => menu,
            getOptions: () => state.options.map((option) => option.button),
            open: () => setMenuOpen(selectShell, true),
            close: () => setMenuOpen(selectShell, false),
        });

        trigger.addEventListener("click", () => {
            const isOpen = selectShell.classList.contains("is-open");
            setMenuOpen(selectShell, !isOpen);
            if (!isOpen) controller.highlightSelected();
        });

        controller.bindKeyboard();
        select.addEventListener("change", syncSelection);
        select.addEventListener("shared-select:refresh", renderOptions);
        new MutationObserver((records) => {
            const optionsChanged = records.some((record) =>
                record.type === "childList" || record.target instanceof HTMLOptionElement);
            if (optionsChanged) {
                renderOptions();
            } else {
                syncSelection();
            }
        }).observe(select, {
            attributes: true,
            attributeFilter: ["disabled", "hidden", "label", "value"],
            childList: true,
            subtree: true,
        });
        renderOptions();
    }

    function refreshSelect(select) {
        const selectState = selectStates.get(select);
        if (!selectState) {
            initializeSelect(select);
            return;
        }
        select.dispatchEvent(new Event("shared-select:refresh"));
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
        document.querySelectorAll(selectSelector).forEach(initializeSelect);
        window.SHARED_SELECT_AUTO = Object.freeze({initialize: initializeSelect, refresh: refreshSelect});
    });
})();
