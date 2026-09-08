/* Code version: v1.3.0-codex.1 */

(() => {
    "use strict";

    const status = document.querySelector("[data-style-token-copy-status]");

    const showStatus = (message) => {
        if (!status) {
            return;
        }
        status.textContent = message;
        window.clearTimeout(showStatus.timeoutId);
        showStatus.timeoutId = window.setTimeout(() => {
            status.textContent = "";
        }, 1800);
    };

    const copyText = async (value) => {
        if (navigator.clipboard?.writeText) {
            await navigator.clipboard.writeText(value);
            return;
        }
        const helper = document.createElement("textarea");
        helper.value = value;
        helper.setAttribute("readonly", "");
        helper.style.position = "fixed";
        helper.style.opacity = "0";
        document.body.appendChild(helper);
        helper.select();
        document.execCommand("copy");
        helper.remove();
    };

    const bindRangeModeDemos = () => {
        document.querySelectorAll(".style-token-demo .range-mode-shell").forEach((shell) => {
            if (!(shell instanceof HTMLElement) || shell.dataset.bound === "1") {
                return;
            }
            shell.dataset.bound = "1";

            const syncActiveValue = () => {
                const checkedInput = shell.querySelector('input[type="radio"]:checked');
                const options = Array.from(shell.querySelectorAll(".range-mode-option"));
                const activeIndex = Math.max(
                    0,
                    options.findIndex((option) => option.querySelector('input[type="radio"]') === checkedInput),
                );
                const optionCount = Math.max(options.length, 1);
                const nextValue = checkedInput instanceof HTMLInputElement ? checkedInput.value : "overview";
                shell.dataset.active = nextValue;
                shell.dataset.optionCount = String(optionCount);
                shell.dataset.segmentedActiveIndex = String(activeIndex);
                shell.style.setProperty("--segmented-option-count", String(optionCount));
                shell.style.setProperty("--segmented-active-index", String(activeIndex));
                window.CACHELIKES_SEGMENTED_CONTROLS?.sync?.(shell);
            };

            shell.querySelectorAll('input[type="radio"]').forEach((input) => {
                input.addEventListener("change", syncActiveValue);
            });
            syncActiveValue();
        });
    };

    const bindStyleTokenResizer = () => {
        const shell = document.querySelector("[data-style-token-shell]");
        const handle = shell?.querySelector("[data-style-token-resizer]");
        if (!(shell instanceof HTMLElement) || !(handle instanceof HTMLElement) || handle.dataset.bound === "1") {
            return;
        }
        const minWidth = 220;
        let dragGeometry = null;
        const measureDragGeometry = () => {
            const rect = shell.getBoundingClientRect();
            const computed = getComputedStyle(shell);
            const columnGap = Number.parseFloat(computed.getPropertyValue("--style-token-column-gap")) || 24;
            return {rect, columnGap};
        };
        const getDragGeometry = () => dragGeometry || measureDragGeometry();
        const getWidthRange = () => {
            const {rect, columnGap} = getDragGeometry();
            const maximum = Math.max(minWidth, rect.width - columnGap - 280);
            return {minimum: minWidth, maximum};
        };
        const widthFromPointer = (clientX) => {
            const {rect, columnGap} = getDragGeometry();
            return clientX - rect.left - (columnGap / 2);
        };
        const getCurrentWidth = () => {
            const demo = shell.querySelector(".style-token-demo");
            return demo instanceof HTMLElement ? demo.getBoundingClientRect().width : minWidth;
        };
        const setCurrentWidth = (nextWidth) => {
            shell.style.setProperty("--style-token-demo-width-current", `${nextWidth}px`);
        };
        let handlePositionFrame = 0;
        let lastHandleY = null;
        const syncHandleY = () => {
            const rect = shell.getBoundingClientRect();
            if (!rect.height) {
                return;
            }
            const visibleTop = Math.max(0, rect.top);
            const visibleBottom = Math.min(window.innerHeight, rect.bottom);
            const visibleHeight = visibleBottom - visibleTop;
            if (visibleHeight <= 0) {
                return;
            }
            const visibleCenterY = visibleTop + (visibleHeight / 2);
            const targetY = Math.min(Math.max(16, visibleCenterY - rect.top), rect.height - 16);
            if (targetY === lastHandleY) {
                return;
            }
            lastHandleY = targetY;
            shell.style.setProperty("--style-token-resizer-y", `${targetY}px`);
        };
        const scheduleHandleY = () => {
            if (handlePositionFrame) {
                return;
            }
            handlePositionFrame = window.requestAnimationFrame(() => {
                handlePositionFrame = 0;
                syncHandleY();
            });
        };
        const unbind = window.CACHE_LIKES_RESIZER?.bind(handle, {
            axis: "inline",
            root: shell,
            getRange: getWidthRange,
            getValue: getCurrentWidth,
            setValue: setCurrentWidth,
            valueFromPointer: widthFromPointer,
            onStart: () => {
                dragGeometry = measureDragGeometry();
            },
            onEnd: () => {
                dragGeometry = null;
            },
        });
        handle.dataset.bound = "1";
        syncHandleY();
        window.addEventListener("resize", scheduleHandleY, {passive: true});
        window.addEventListener("scroll", scheduleHandleY, {passive: true});
        handle._cacheLikesResizerCleanup = () => {
            unbind?.();
            window.removeEventListener("resize", scheduleHandleY);
            window.removeEventListener("scroll", scheduleHandleY);
            if (handlePositionFrame) {
                window.cancelAnimationFrame(handlePositionFrame);
            }
            delete handle.dataset.bound;
        };
    };

    const setStyleTokenMenuOpen = (container, trigger, menu, isOpen) => {
        container.classList.toggle("is-open", isOpen);
        trigger.setAttribute("aria-expanded", String(isOpen));
        menu.hidden = !isOpen;
        menu.setAttribute("aria-hidden", String(!isOpen));
    };

    const syncStyleTokenFilterSelection = (container, option) => {
        const trigger = container.querySelector("[data-style-token-shared-filter-trigger], [data-style-token-table-filter-trigger]");
        const label = container.querySelector("[data-style-token-shared-filter-label], [data-style-token-table-filter-label]");
        const options = Array.from(container.querySelectorAll("[data-style-token-shared-filter-option], [data-style-token-table-filter-option]"));
        const value = option?.dataset.styleTokenSharedFilterOption || option?.dataset.styleTokenTableFilterOption || "";
        options.forEach((candidate) => {
            const selected = candidate === option;
            candidate.classList.toggle("is-selected", selected);
            candidate.classList.toggle("is-active", selected);
            candidate.setAttribute("aria-selected", String(selected));
        });
        if (label && option) {
            label.textContent = option.textContent.trim();
        }
        if (trigger && option) {
            const menuLabel = container.dataset.styleTokenMenuLabel
                || (container.hasAttribute("data-style-token-table-filter") ? "Type filter" : "Sort cached text");
            trigger.setAttribute("aria-label", `${menuLabel}: ${option.textContent.trim()}`);
        }
        return value;
    };

    const moveStyleTokenMenuFocus = (options, current, key) => {
        const currentIndex = Math.max(0, options.indexOf(current));
        if (key === "Home") {
            return options[0];
        }
        if (key === "End") {
            return options.at(-1);
        }
        const direction = key === "ArrowUp" ? -1 : 1;
        return options[(currentIndex + direction + options.length) % options.length];
    };

    const bindStyleTokenFilterMenus = () => {
        document.querySelectorAll("[data-style-token-shared-filter], [data-style-token-table-filter]").forEach((container) => {
            if (!(container instanceof HTMLElement) || container.dataset.bound === "1") {
                return;
            }
            const trigger = container.querySelector("[data-style-token-shared-filter-trigger], [data-style-token-table-filter-trigger]");
            const menu = container.querySelector("[data-style-token-shared-filter-menu], [data-style-token-table-filter-menu]");
            const options = Array.from(container.querySelectorAll("[data-style-token-shared-filter-option], [data-style-token-table-filter-option]"));
            if (!(trigger instanceof HTMLButtonElement) || !(menu instanceof HTMLElement) || !options.length) {
                return;
            }
            container.dataset.bound = "1";
            const selected = options.find((option) => option.getAttribute("aria-selected") === "true") || options[0];
            syncStyleTokenFilterSelection(container, selected);
            trigger.addEventListener("click", () => {
                setStyleTokenMenuOpen(container, trigger, menu, !container.classList.contains("is-open"));
            });
            trigger.addEventListener("keydown", (event) => {
                if (event.key === "Escape") {
                    event.preventDefault();
                    setStyleTokenMenuOpen(container, trigger, menu, false);
                    trigger.focus({preventScroll: true});
                    return;
                }
                if (["ArrowDown", "ArrowUp", "Home", "End", "Enter", " "].includes(event.key)) {
                    event.preventDefault();
                    setStyleTokenMenuOpen(container, trigger, menu, true);
                    const current = options.find((option) => option.getAttribute("aria-selected") === "true") || selected;
                    const focusTarget = ["ArrowUp", "End"].includes(event.key) ? options.at(-1) : current;
                    focusTarget?.focus({preventScroll: true});
                }
            });
            options.forEach((option) => {
                option.addEventListener("click", () => {
                    const value = syncStyleTokenFilterSelection(container, option);
                    const nativeSelect = container.querySelector("select");
                    if (nativeSelect instanceof HTMLSelectElement) {
                        nativeSelect.value = value;
                        nativeSelect.dispatchEvent(new Event("change", {bubbles: true}));
                    }
                    setStyleTokenMenuOpen(container, trigger, menu, false);
                    container.__styleTokenOnSelect?.(value);
                });
                option.addEventListener("keydown", (event) => {
                    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
                        event.preventDefault();
                        moveStyleTokenMenuFocus(options, option, event.key)?.focus({preventScroll: true});
                        return;
                    }
                    if (["Enter", " "].includes(event.key)) {
                        event.preventDefault();
                        option.click();
                        trigger.focus({preventScroll: true});
                        return;
                    }
                    if (event.key === "Escape") {
                        event.preventDefault();
                        setStyleTokenMenuOpen(container, trigger, menu, false);
                        trigger.focus({preventScroll: true});
                    }
                });
            });
        });
        if (document.documentElement.dataset.styleTokenFilterMenusBound === "1") {
            return;
        }
        document.documentElement.dataset.styleTokenFilterMenusBound = "1";
        document.addEventListener("click", (event) => {
            if (event.target instanceof Element && event.target.closest("[data-style-token-shared-filter], [data-style-token-table-filter]")) {
                return;
            }
            document.querySelectorAll("[data-style-token-shared-filter].is-open, [data-style-token-table-filter].is-open").forEach((container) => {
                const trigger = container.querySelector("[data-style-token-shared-filter-trigger], [data-style-token-table-filter-trigger]");
                const menu = container.querySelector("[data-style-token-shared-filter-menu], [data-style-token-table-filter-menu]");
                if (trigger instanceof HTMLButtonElement && menu instanceof HTMLElement) {
                    setStyleTokenMenuOpen(container, trigger, menu, false);
                }
            });
        });
    };

    const setStyleTokenAgentBrowserMenuOpen = (container, trigger, menu, isOpen) => {
        container.classList.toggle("is-agent-combobox-open", isOpen);
        trigger.setAttribute("aria-expanded", String(isOpen));
        menu.hidden = !isOpen;
        menu.setAttribute("aria-hidden", String(!isOpen));
    };

    const syncStyleTokenAgentBrowserSelection = (container, option) => {
        const input = container.querySelector("[data-style-token-agent-browser-input]");
        const trigger = container.querySelector("[data-style-token-agent-browser-trigger]");
        const label = container.querySelector("[data-style-token-agent-browser-selected-label]");
        const icon = container.querySelector("[data-style-token-agent-browser-selected-icon]");
        const options = Array.from(container.querySelectorAll("[data-style-token-agent-browser-option]"));
        if (!option) {
            return;
        }
        const value = option.dataset.styleTokenAgentBrowserOption || "";
        const optionLabel = option.dataset.styleTokenAgentBrowserLabel || option.textContent.trim();
        const optionIcon = option.dataset.styleTokenAgentBrowserIcon || "";
        if (input instanceof HTMLInputElement) {
            input.value = value;
        }
        if (label) {
            label.textContent = optionLabel;
        }
        if (icon instanceof HTMLImageElement && optionIcon) {
            icon.src = optionIcon;
        }
        if (trigger) {
            trigger.setAttribute("aria-label", `Browser: ${optionLabel}`);
        }
        options.forEach((candidate) => {
            const selected = candidate === option;
            candidate.classList.toggle("is-selected", selected);
            candidate.classList.toggle("is-active", selected);
            candidate.setAttribute("aria-selected", String(selected));
        });
    };

    const bindStyleTokenAgentBrowserDemo = () => {
        document.querySelectorAll("[data-style-token-agent-browser]").forEach((container) => {
            if (!(container instanceof HTMLElement) || container.dataset.bound === "1") {
                return;
            }
            const trigger = container.querySelector("[data-style-token-agent-browser-trigger]");
            const menu = container.querySelector("[data-style-token-agent-browser-menu]");
            const options = Array.from(container.querySelectorAll("[data-style-token-agent-browser-option]"));
            if (!(trigger instanceof HTMLButtonElement) || !(menu instanceof HTMLElement) || !options.length) {
                return;
            }
            container.dataset.bound = "1";
            const inputValue = container.querySelector("[data-style-token-agent-browser-input]")?.value || "edge";
            const selected = options.find((option) => option.dataset.styleTokenAgentBrowserOption === inputValue)
                || options.find((option) => option.getAttribute("aria-selected") === "true")
                || options[0];
            syncStyleTokenAgentBrowserSelection(container, selected);
            setStyleTokenAgentBrowserMenuOpen(
                container,
                trigger,
                menu,
                container.classList.contains("is-agent-combobox-open") || !menu.hidden,
            );
            trigger.addEventListener("click", () => {
                const isOpen = container.classList.contains("is-agent-combobox-open");
                document.querySelectorAll("[data-style-token-agent-browser].is-agent-combobox-open").forEach((other) => {
                    if (other === container) {
                        return;
                    }
                    const otherTrigger = other.querySelector("[data-style-token-agent-browser-trigger]");
                    const otherMenu = other.querySelector("[data-style-token-agent-browser-menu]");
                    if (otherTrigger instanceof HTMLButtonElement && otherMenu instanceof HTMLElement) {
                        setStyleTokenAgentBrowserMenuOpen(other, otherTrigger, otherMenu, false);
                    }
                });
                setStyleTokenAgentBrowserMenuOpen(container, trigger, menu, !isOpen);
            });
            trigger.addEventListener("keydown", (event) => {
                if (event.key === "Escape") {
                    event.preventDefault();
                    setStyleTokenAgentBrowserMenuOpen(container, trigger, menu, false);
                    trigger.focus({preventScroll: true});
                    return;
                }
                if (["ArrowDown", "ArrowUp", "Home", "End", "Enter", " "].includes(event.key)) {
                    event.preventDefault();
                    setStyleTokenAgentBrowserMenuOpen(container, trigger, menu, true);
                    const current = options.find((option) => option.getAttribute("aria-selected") === "true") || options[0];
                    const focusTarget = ["ArrowUp", "End"].includes(event.key) ? options.at(-1) : current;
                    focusTarget?.focus({preventScroll: true});
                }
            });
            options.forEach((option) => {
                option.addEventListener("click", () => {
                    syncStyleTokenAgentBrowserSelection(container, option);
                    setStyleTokenAgentBrowserMenuOpen(container, trigger, menu, false);
                    showStatus(`Browser preview: ${option.dataset.styleTokenAgentBrowserLabel || option.textContent.trim()}`);
                });
                option.addEventListener("keydown", (event) => {
                    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
                        event.preventDefault();
                        moveStyleTokenMenuFocus(options, option, event.key)?.focus({preventScroll: true});
                        return;
                    }
                    if (["Enter", " "].includes(event.key)) {
                        event.preventDefault();
                        option.click();
                        trigger.focus({preventScroll: true});
                        return;
                    }
                    if (event.key === "Escape") {
                        event.preventDefault();
                        setStyleTokenAgentBrowserMenuOpen(container, trigger, menu, false);
                        trigger.focus({preventScroll: true});
                    }
                });
            });
        });
        if (document.documentElement.dataset.styleTokenAgentBrowserBound === "1") {
            return;
        }
        document.documentElement.dataset.styleTokenAgentBrowserBound = "1";
        document.addEventListener("click", (event) => {
            if (event.target instanceof Element && event.target.closest("[data-style-token-agent-browser]")) {
                return;
            }
            document.querySelectorAll("[data-style-token-agent-browser].is-agent-combobox-open").forEach((container) => {
                const trigger = container.querySelector("[data-style-token-agent-browser-trigger]");
                const menu = container.querySelector("[data-style-token-agent-browser-menu]");
                if (trigger instanceof HTMLButtonElement && menu instanceof HTMLElement) {
                    setStyleTokenAgentBrowserMenuOpen(container, trigger, menu, false);
                }
            });
        });
        document.addEventListener("keydown", (event) => {
            if (event.key !== "Escape") {
                return;
            }
            document.querySelectorAll("[data-style-token-agent-browser].is-agent-combobox-open").forEach((container) => {
                const trigger = container.querySelector("[data-style-token-agent-browser-trigger]");
                const menu = container.querySelector("[data-style-token-agent-browser-menu]");
                if (trigger instanceof HTMLButtonElement && menu instanceof HTMLElement) {
                    setStyleTokenAgentBrowserMenuOpen(container, trigger, menu, false);
                    trigger.focus({preventScroll: true});
                }
            });
        });
    };

    const syncStyleTokenPaginationIndicator = (pagination, immediate = true) => {
        const active = pagination.querySelector(".local-store-page-button.is-active");
        if (!active) {
            return;
        }
        window.CACHELIKES_PAGINATION_MOTION?.positionPaginationIndicator(pagination, active, {immediate});
    };

    const setStyleTokenPaginationPage = (pagination, page) => {
        const target = String(page);
        const button = Array.from(pagination.querySelectorAll(":scope > .local-store-page-button"))
            .find((candidate) => candidate.dataset.paginationTarget === target);
        if (!button) {
            return;
        }
        pagination.querySelectorAll(":scope > .local-store-page-button").forEach((candidate) => {
            const selected = candidate === button;
            candidate.classList.toggle("is-active", selected);
            candidate.dataset.paginationCurrent = selected ? "1" : "0";
            candidate.toggleAttribute("aria-current", selected);
            if (selected) {
                candidate.setAttribute("aria-current", "page");
            }
        });
        syncStyleTokenPaginationIndicator(pagination, false);
    };

    const bindStyleTokenPagination = () => {
        document.querySelectorAll("[data-style-token-pagination]").forEach((pagination) => {
            if (!(pagination instanceof HTMLElement) || pagination.dataset.bound === "1") {
                return;
            }
            pagination.dataset.bound = "1";
            pagination.querySelectorAll(":scope > .local-store-page-button").forEach((button) => {
                button.addEventListener("click", () => {
                    setStyleTokenPaginationPage(pagination, button.dataset.paginationTarget);
                    showStatus(`Showing session page ${button.dataset.paginationTarget}`);
                });
            });
            const range = pagination.querySelector("[data-style-token-pagination-range]");
            const rangeTrigger = range?.querySelector("[data-style-token-pagination-range-trigger]");
            const rangeMenu = range?.querySelector("[data-style-token-pagination-range-menu]");
            if (range instanceof HTMLElement && rangeTrigger instanceof HTMLButtonElement && rangeMenu instanceof HTMLElement) {
                const setRangeOpen = (isOpen) => {
                    range.classList.toggle("is-open", isOpen);
                    rangeTrigger.setAttribute("aria-expanded", String(isOpen));
                    rangeMenu.hidden = !isOpen;
                    rangeMenu.setAttribute("aria-hidden", String(!isOpen));
                };
                rangeTrigger.addEventListener("click", () => {
                    setRangeOpen(!range.classList.contains("is-open"));
                });
                range.querySelectorAll("[data-pagination-target]").forEach((option) => {
                    option.addEventListener("click", () => {
                        setRangeOpen(false);
                        setStyleTokenPaginationPage(pagination, option.dataset.paginationTarget);
                        showStatus(`Range ${option.textContent.trim()} selected`);
                    });
                });
                range.addEventListener("keydown", (event) => {
                    if (event.key === "Escape") {
                        event.preventDefault();
                        setRangeOpen(false);
                        rangeTrigger.focus({preventScroll: true});
                    }
                });
                document.addEventListener("click", (event) => {
                    if (!(event.target instanceof Node) || range.contains(event.target)) {
                        return;
                    }
                    setRangeOpen(false);
                });
            }
            syncStyleTokenPaginationIndicator(pagination);
        });
    };

    const bindStyleTokenTableDemos = () => {
        document.querySelectorAll("[data-style-token-table-demo]").forEach((demo) => {
            if (!(demo instanceof HTMLElement) || demo.dataset.bound === "1") {
                return;
            }
            const rows = Array.from(demo.querySelectorAll("[data-style-token-table-row]"));
            const pagination = demo.querySelector("[data-style-token-table-pagination]");
            const summary = demo.querySelector("[data-style-token-table-filter-summary]");
            const filter = demo.querySelector("[data-style-token-table-filter]");
            const pageSize = Number(demo.dataset.styleTokenTablePageSize || 6);
            if (!(pagination instanceof HTMLElement) || !(summary instanceof HTMLElement) || !(filter instanceof HTMLElement)) {
                return;
            }
            demo.dataset.bound = "1";
            let activeFilter = "all";
            let currentPage = 1;
            const matchingRows = () => rows.filter((row) => activeFilter === "all" || row.dataset.styleTokenTableFilterValue === activeFilter);
            const renderPagination = (pageCount) => {
                pagination.replaceChildren();
                if (pageCount <= 1) {
                    pagination.hidden = true;
                    return;
                }
                pagination.hidden = false;
                const indicator = document.createElement("span");
                indicator.className = "local-store-pagination-indicator";
                indicator.setAttribute("aria-hidden", "true");
                pagination.append(indicator);
                for (let page = 1; page <= pageCount; page += 1) {
                    const button = document.createElement("button");
                    button.type = "button";
                    button.className = `local-store-page-button${page === currentPage ? " is-active" : ""}`;
                    button.dataset.paginationTarget = String(page);
                    button.dataset.paginationCurrent = page === currentPage ? "1" : "0";
                    button.setAttribute("aria-label", `Table page ${page}`);
                    if (page === currentPage) {
                        button.setAttribute("aria-current", "page");
                    }
                    button.textContent = String(page);
                    button.addEventListener("click", () => {
                        currentPage = page;
                        render();
                    });
                    pagination.append(button);
                }
                syncStyleTokenPaginationIndicator(pagination);
            };
            const render = () => {
                const matching = matchingRows();
                const pageCount = Math.max(1, Math.ceil(matching.length / pageSize));
                currentPage = Math.min(currentPage, pageCount);
                const firstIndex = (currentPage - 1) * pageSize;
                rows.forEach((row) => {
                    const rowIndex = matching.indexOf(row);
                    row.hidden = rowIndex < firstIndex || rowIndex >= firstIndex + pageSize;
                });
                summary.textContent = `${matching.length} filtered of ${rows.length} total`;
                renderPagination(pageCount);
            };
            filter.__styleTokenOnSelect = (value) => {
                activeFilter = value;
                currentPage = 1;
                render();
            };
            render();
        });
    };

    const bindSecondaryButtonDemo = () => {
        document.querySelectorAll("[data-style-token-secondary-button]").forEach((button) => {
            button.addEventListener("click", () => showStatus("Refresh cache control preview"));
        });
    };

    const bindStyleTokenPrimaryButtonDemo = () => {
        document.querySelectorAll("[data-style-token-primary-button]").forEach((button) => {
            if (!(button instanceof HTMLButtonElement) || button.dataset.bound === "1") {
                return;
            }
            button.dataset.bound = "1";
            button.addEventListener("click", () => showStatus("Start action preview"));
        });
    };

    const bindStyleTokenThemeToggleDemo = () => {
        document.querySelectorAll("[data-style-token-theme-toggle]").forEach((button) => {
            if (!(button instanceof HTMLButtonElement) || button.dataset.bound === "1") {
                return;
            }
            button.dataset.bound = "1";
            const sync = (effectiveTheme) => {
                const isDark = effectiveTheme === "dark";
                const nextLabel = isDark ? "Switch to Light mode" : "Switch to Dark mode";
                button.dataset.effectiveTheme = isDark ? "dark" : "light";
                button.setAttribute("aria-label", nextLabel);
                button.title = nextLabel;
                button.setAttribute("aria-pressed", String(isDark));
            };
            sync(button.dataset.effectiveTheme || "light");
            button.addEventListener("click", () => {
                const nextTheme = button.dataset.effectiveTheme === "dark" ? "light" : "dark";
                sync(nextTheme);
                showStatus(`Theme preview: ${nextTheme}`);
            });
        });
    };

    const bindStyleTokenStrategyTuningDemo = () => {
        document.querySelectorAll("[data-style-token-strategy-tuning]").forEach((demo) => {
            if (!(demo instanceof HTMLElement) || demo.dataset.bound === "1") {
                return;
            }
            const button = demo.querySelector("[data-style-token-strategy-tune-button]");
            const panel = demo.querySelector("[data-style-token-strategy-tuning-panel]");
            if (!(button instanceof HTMLButtonElement) || !(panel instanceof HTMLElement)) {
                return;
            }
            demo.dataset.bound = "1";
            const sync = (open) => {
                button.classList.toggle("is-active", open);
                button.setAttribute("aria-pressed", String(open));
                button.setAttribute("aria-expanded", String(open));
                panel.hidden = !open;
            };
            sync(button.getAttribute("aria-pressed") === "true");
            button.addEventListener("click", () => {
                sync(button.getAttribute("aria-pressed") !== "true");
                showStatus(button.getAttribute("aria-pressed") === "true"
                    ? "Strategy parameters expanded"
                    : "Strategy parameters collapsed");
            });
        });
    };

    const attachStyleTokenControls = () => {
        const shell = document.querySelector("[data-style-token-shell]");
        if (!(shell instanceof HTMLElement)) {
            return;
        }
        shell.querySelectorAll("[data-style-token-control]").forEach((control) => {
            if (!(control instanceof HTMLElement) || control.dataset.bound === "1") {
                return;
            }
            control.dataset.bound = "1";
            control.querySelectorAll("[data-style-token-stepper]").forEach((button) => {
                button.addEventListener("click", () => {
                    const direction = button.dataset.styleTokenStepper === "down" ? -1 : 1;
                    const currentValue = Number(control.dataset.styleTokenValue || 0);
                    const minimum = Number(control.dataset.styleTokenMin || 0);
                    const nextValue = Math.max(minimum, currentValue + direction);
                    const tokenName = control.dataset.styleTokenName || "";
                    const unit = control.dataset.styleTokenUnit || "";
                    shell.style.setProperty(tokenName, `${nextValue}${unit}`);
                    shell.querySelectorAll("[data-style-token-control]").forEach((peerControl) => {
                        if (!(peerControl instanceof HTMLElement) || peerControl.dataset.styleTokenName !== tokenName) {
                            return;
                        }
                        peerControl.dataset.styleTokenValue = String(nextValue);
                        const input = peerControl.querySelector("[data-style-token-value-input]");
                        if (input instanceof HTMLInputElement) {
                            input.value = `${nextValue}${unit}`;
                        }
                    });
                    showStatus(`${tokenName}: ${nextValue}${unit}`);
                });
            });
        });
    };

    const attachTextInputClearHandlers = () => {
        document.querySelectorAll("[data-style-token-text-input-clear]").forEach((button) => {
            if (!(button instanceof HTMLButtonElement) || button.dataset.bound === "1") {
                return;
            }
            button.dataset.bound = "1";
            const shell = button.closest(".style-token-text-input-shell");
            const input = shell?.querySelector("[data-style-token-text-input]");
            if (!(input instanceof HTMLInputElement)) {
                return;
            }
            const sync = () => {
                button.hidden = input.value.length === 0;
            };
            button.addEventListener("click", () => {
                input.value = "";
                input.dispatchEvent(new Event("input", {bubbles: true}));
                input.focus({preventScroll: true});
                sync();
                showStatus("Text input cleared");
            });
            input.addEventListener("input", sync);
            sync();
        });
    };

    const attachStyleTokenReferences = () => {
        const shell = document.querySelector("[data-style-token-shell]");
        if (!(shell instanceof HTMLElement)) {
            return;
        }
        const reveal = (targetId, shouldScroll) => {
            const targetCard = shell.querySelector(`[data-style-token-card="${CSS.escape(targetId)}"]`);
            if (!(targetCard instanceof HTMLElement)) {
                return;
            }
            targetCard.classList.remove("is-linked-highlight");
            void targetCard.offsetWidth;
            targetCard.classList.add("is-linked-highlight");
            window.setTimeout(() => targetCard.classList.remove("is-linked-highlight"), 700);
            if (shouldScroll) {
                targetCard.scrollIntoView({behavior: "smooth", block: "center"});
            }
        };
        shell.querySelectorAll("[data-style-token-reference]").forEach((reference) => {
            if (!(reference instanceof HTMLElement) || reference.dataset.bound === "1") {
                return;
            }
            reference.dataset.bound = "1";
            const targetId = reference.dataset.styleTokenReference || "";
            reference.addEventListener("mouseenter", () => reveal(targetId, false));
            reference.addEventListener("focus", () => reveal(targetId, false));
            reference.addEventListener("click", (event) => {
                event.preventDefault();
                history.replaceState(null, "", `#${targetId}`);
                reveal(targetId, true);
            });
        });
        const initialTarget = decodeURIComponent(window.location.hash.slice(1));
        if (initialTarget) {
            window.requestAnimationFrame(() => reveal(initialTarget, true));
        }
    };

    const bindPromptTagDemo = () => {
        document.querySelectorAll("[data-style-token-prompt-tag-remove]").forEach((button) => {
            if (!(button instanceof HTMLButtonElement) || button.dataset.bound === "1") {
                return;
            }
            button.dataset.bound = "1";
            const tag = button.closest("[data-style-token-prompt-tag]");
            button.addEventListener("click", () => {
                if (!(tag instanceof HTMLElement)) {
                    return;
                }
                tag.classList.add("style-token-dismissible-hidden");
                showStatus("Tag removed; restoring preview");
                window.setTimeout(() => tag.classList.remove("style-token-dismissible-hidden"), 800);
            });
        });
    };

    const bindDismissibleDemos = () => {
        document.querySelectorAll("[data-style-token-dismiss]").forEach((button) => {
            if (!(button instanceof HTMLButtonElement) || button.dataset.bound === "1") {
                return;
            }
            button.dataset.bound = "1";
            const surface = button.closest("[data-style-token-dismissible]");
            button.addEventListener("click", () => {
                if (!(surface instanceof HTMLElement)) {
                    return;
                }
                surface.classList.add("style-token-dismissible-hidden");
                showStatus("Demo dismissed; restoring preview");
                window.setTimeout(() => surface.classList.remove("style-token-dismissible-hidden"), 800);
            });
        });
    };

    const bindActionPackageDemo = () => {
        document.querySelectorAll("[data-style-token-action-package]").forEach((actionPackage) => {
            if (!(actionPackage instanceof HTMLElement) || actionPackage.dataset.bound === "1") {
                return;
            }
            actionPackage.dataset.bound = "1";
            const button = actionPackage.querySelector("[data-style-token-action-button]");
            const copy = actionPackage.querySelector("[data-style-token-action-copy]");
            const liveMarker = actionPackage.querySelector("[data-action-package-live-marker]");
            const control = actionPackage.parentElement?.querySelector("[data-style-token-action-package-live]");
            const setLive = (isLive) => {
                actionPackage.dataset.actionPackageLive = isLive ? "true" : "false";
                if (liveMarker instanceof HTMLElement) {
                    liveMarker.hidden = !isLive;
                }
            };
            if (control instanceof HTMLInputElement) {
                control.addEventListener("change", () => setLive(control.checked));
                setLive(control.checked);
            }
            if (button instanceof HTMLButtonElement) {
                const originalCopy = copy?.textContent || "";
                button.addEventListener("click", () => {
                    button.disabled = true;
                    button.classList.add("is-pending");
                    button.toggleAttribute("aria-busy", true);
                    button.textContent = button.dataset.pendingLabel || "Refreshing…";
                    if (copy) {
                        copy.textContent = actionPackage.dataset.actionPackagePendingCopy
                            || "Refreshing local metadata and packaged assets.";
                    }
                    setLive(true);
                    window.setTimeout(() => {
                        button.disabled = false;
                        button.classList.remove("is-pending");
                        button.toggleAttribute("aria-busy", false);
                        button.textContent = button.dataset.defaultLabel || "Refresh";
                        if (copy) {
                            copy.textContent = actionPackage.dataset.actionPackageDefaultCopy || originalCopy;
                        }
                        setLive(control instanceof HTMLInputElement && control.checked);
                    }, 1200);
                });
            }
        });
    };

    const bindStyleTokenDensity = () => {
        const demos = Array.from(document.querySelectorAll(".style-token-demo"));
        const sync = (demo) => {
            const width = demo.getBoundingClientRect().width;
            demo.dataset.styleTokenDensity = width <= 320 ? "tight" : width <= 360 ? "compact" : "comfortable";
        };
        if (typeof ResizeObserver === "function") {
            const observer = new ResizeObserver((entries) => entries.forEach((entry) => sync(entry.target)));
            demos.forEach((demo) => observer.observe(demo));
            return;
        }
        window.requestAnimationFrame(() => demos.forEach(sync));
    };

    bindRangeModeDemos();
    bindStyleTokenResizer();
    bindStyleTokenTableDemos();
    bindStyleTokenFilterMenus();
    bindStyleTokenAgentBrowserDemo();
    bindStyleTokenPagination();
    bindSecondaryButtonDemo();
    bindStyleTokenPrimaryButtonDemo();
    bindStyleTokenThemeToggleDemo();
    bindStyleTokenStrategyTuningDemo();
    attachStyleTokenControls();
    attachTextInputClearHandlers();
    attachStyleTokenReferences();
    bindPromptTagDemo();
    bindDismissibleDemos();
    bindActionPackageDemo();
    bindStyleTokenDensity();

    document.addEventListener("click", (event) => {
        const button = event.target.closest("[data-style-token-copy]");
        if (!button) {
            return;
        }
        const tokenName = button.dataset.styleTokenCopy;
        if (!tokenName) {
            return;
        }
        copyText(tokenName)
            .then(() => {
                button.classList.add("is-copied");
                window.clearTimeout(button.copyResetTimeoutId);
                button.copyResetTimeoutId = window.setTimeout(() => {
                    button.classList.remove("is-copied");
                }, 1800);
                showStatus(`Copied: ${tokenName}`);
            })
            .catch(() => showStatus("Copy unavailable"));
    });
})();
