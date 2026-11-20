/* Code version: v1.1.0-codex.0 */

(() => {
    "use strict";

    const selector = ".segmented-control[data-option-count], .range-mode-shell[data-option-count]";

    const getOptions = (shell) => Array.from(shell.children)
        .filter((option) => option instanceof HTMLElement)
        .filter((option) => option.classList.contains("segmented-control-option") || option.classList.contains("range-mode-option"))
        .filter((option) => !option.hidden);

    const getActiveIndex = (shell, options) => {
        const checkedIndex = options.findIndex((option) => {
            const input = option.querySelector("input");
            return input instanceof HTMLInputElement && input.checked && !input.disabled;
        });
        if (checkedIndex >= 0) return checkedIndex;

        const markedIndex = options.findIndex((option) => (
            option.classList.contains("is-active")
            || option.getAttribute("aria-checked") === "true"
        ));
        if (markedIndex >= 0) return markedIndex;

        const declaredIndex = Number.parseInt(shell.dataset.segmentedActiveIndex || "", 10);
        if (Number.isInteger(declaredIndex)) return declaredIndex;
        return 0;
    };

    const setAttributeIfChanged = (element, name, value) => {
        if (element.getAttribute(name) !== value) element.setAttribute(name, value);
    };

    const setStylePropertyIfChanged = (element, property, value) => {
        if (element.style.getPropertyValue(property) !== value) {
            element.style.setProperty(property, value);
        }
    };

    const syncMeasuredPill = (shell, options, activeIndex) => {
        if (shell.dataset.segmentedPill !== "measured" && shell.dataset.segmentedOverflow !== "1") {
            shell.style.removeProperty("--segmented-pill-left");
            shell.style.removeProperty("--segmented-pill-width");
            return;
        }
        const activeOption = options[activeIndex];
        if (!(activeOption instanceof HTMLElement)) {
            return;
        }
        const shellStyles = window.getComputedStyle(shell);
        const thumbInset = Number.parseFloat(shellStyles.getPropertyValue("--mode-switch-thumb-inset"))
            || Number.parseFloat(shellStyles.paddingLeft)
            || 0;
        setStylePropertyIfChanged(
            shell,
            "--segmented-pill-left",
            `${Math.max(0, activeOption.offsetLeft - thumbInset)}px`,
        );
        setStylePropertyIfChanged(
            shell,
            "--segmented-pill-width",
            `${Math.max(1, activeOption.offsetWidth)}px`,
        );
        shell.classList.add("is-pill-ready");
    };

    const syncOverflow = (shell, options) => {
        const styles = window.getComputedStyle(shell);
        const gap = Number.parseFloat(styles.columnGap) || 0;
        const padding = (Number.parseFloat(styles.paddingLeft) || 0)
            + (Number.parseFloat(styles.paddingRight) || 0);
        let optionWidth = 92;
        options.forEach((option) => {
            const label = option.querySelector("span");
            if (!(label instanceof HTMLElement)) return;
            const range = document.createRange();
            range.selectNodeContents(label);
            const labelStyles = window.getComputedStyle(label);
            optionWidth = Math.max(
                optionWidth,
                range.getBoundingClientRect().width
                    + (Number.parseFloat(labelStyles.paddingLeft) || 0)
                    + (Number.parseFloat(labelStyles.paddingRight) || 0),
            );
        });
        const naturalWidth = padding + optionWidth * options.length
            + gap * Math.max(0, options.length - 1);
        const overflowing = shell.dataset.segmentedOverflow === "1"
            ? naturalWidth > shell.clientWidth + 1
            : shell.scrollWidth > shell.clientWidth + 1;
        setAttributeIfChanged(shell, "data-segmented-overflow", overflowing ? "1" : "0");
        if (overflowing) {
            setStylePropertyIfChanged(shell, "--segmented-option-min-width", `${Math.ceil(optionWidth)}px`);
        } else {
            shell.style.removeProperty("--segmented-option-min-width");
            if (shell.scrollLeft) shell.scrollLeft = 0;
        }
    };

    const keepOptionVisible = (shell, option) => {
        if (shell.dataset.segmentedOverflow !== "1" || !(option instanceof HTMLElement)) return;
        const left = option.offsetLeft;
        const right = left + option.offsetWidth;
        if (left < shell.scrollLeft) {
            shell.scrollLeft = left;
        } else if (right > shell.scrollLeft + shell.clientWidth) {
            shell.scrollLeft = right - shell.clientWidth;
        }
    };

    const sync = (shell) => {
        if (!(shell instanceof HTMLElement)) return;
        const options = getOptions(shell);
        const optionCount = Math.max(options.length, 1);
        const activeIndex = Math.min(
            Math.max(getActiveIndex(shell, options), 0),
            optionCount - 1,
        );

        setAttributeIfChanged(shell, "data-option-count", String(optionCount));
        setAttributeIfChanged(shell, "data-segmented-active-index", String(activeIndex));
        setStylePropertyIfChanged(shell, "--segmented-option-count", String(optionCount));
        setStylePropertyIfChanged(shell, "--segmented-active-index", String(activeIndex));
        syncOverflow(shell, options);
        syncMeasuredPill(shell, options, activeIndex);
        keepOptionVisible(shell, options[activeIndex]);

        options.forEach((option, index) => {
            if (option.querySelector("input")) return;
            const isActive = index === activeIndex;
            option.classList.toggle("is-active", isActive);
            setAttributeIfChanged(option, "aria-checked", String(isActive));
        });
    };

    const bind = (shell) => {
        if (!(shell instanceof HTMLElement) || shell.dataset.segmentedControlBound === "1") return;
        shell.dataset.segmentedControlBound = "1";
        shell.addEventListener("change", () => sync(shell));
        shell.addEventListener("click", (event) => {
            const target = event.target instanceof Element ? event.target : null;
            const option = target?.closest(".segmented-control-option");
            if (!(option instanceof HTMLElement) || !shell.contains(option)) return;
            if (option.getAttribute("aria-disabled") === "true" || option.querySelector("input:disabled")) {
                event.preventDefault();
                return;
            }
            window.requestAnimationFrame(() => sync(shell));
        });
        shell.addEventListener("focusin", (event) => {
            const option = event.target instanceof Element
                ? event.target.closest(".segmented-control-option, .range-mode-option") : null;
            keepOptionVisible(shell, option);
        });
        sync(shell);
        if (typeof ResizeObserver === "function") {
            const observer = new ResizeObserver(() => sync(shell));
            observer.observe(shell);
        }
    };

    const syncAll = () => {
        document.querySelectorAll(selector).forEach((shell) => {
            bind(shell);
            sync(shell);
        });
    };

    window.CACHELIKES_SEGMENTED_CONTROLS = Object.freeze({sync, syncAll});
    syncAll();
    window.addEventListener("resize", syncAll, {passive: true});
    document.fonts?.ready.then(syncAll);

    if (typeof MutationObserver === "function") {
        const observer = new MutationObserver((records) => {
            const includesSegmentedControl = records.some((record) => (
                Array.from(record.addedNodes).some((node) => (
                    node instanceof Element
                    && (node.matches(selector) || node.querySelector(selector))
                )) || (record.type === "attributes" && record.target instanceof Element
                    && (record.target.matches(selector) || record.target.closest(selector)))
            ));
            if (includesSegmentedControl) {
                syncAll();
            }
        });
        observer.observe(document.body, {
            subtree: true,
            childList: true,
            attributes: true,
            attributeFilter: ["hidden", "disabled"],
        });
    }
})();
