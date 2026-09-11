/* Code version: v1.14.2-codex.1 */

(() => {
    "use strict";

    const page = document.querySelector("[data-cache-page]");
    if (!page) return;

    const sourceKey = page.dataset.cacheSource || "cache";
    const sourceLabel = page.dataset.cacheSourceLabel || "Cache";
    const statusUrl = page.dataset.cacheStatusUrl || "";
    const progressStrategyName = page.dataset.cacheProgressStrategy || "queue";
    const statusPollIntervalMs = 3_000;
    const terminalPhases = new Set(["finished", "completed", "success", "stopped"]);
    const runningMaximumFields = Object.freeze({
        "zhihu:downloaded_posts": "processed_tweets",
    });
    const numberFormatter = new Intl.NumberFormat("en-US");
    const datetimeFormatter = new Intl.DateTimeFormat("en-US", {
        day: "numeric",
        month: "short",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hourCycle: "h23",
        timeZoneName: "short",
    });
    const timezoneCodesByOffset = Object.freeze({
        "-480": "PST",
        "-420": "MST",
        "-360": "CST",
        "-300": "EST",
        "-240": "EDT",
        "0": "UTC",
        "60": "CET",
        "120": "EET",
        "480": "CST",
        "540": "JST",
    });

    const phaseChip = document.getElementById("phase_chip");
    const phaseValue = document.getElementById("phase_value");
    const startButton = document.getElementById("start_button");
    const stopButton = document.getElementById("stop_button");
    const cacheActionRow = document.querySelector("[data-cache-action-row]");
    const startAction = document.querySelector(".sidebar-form-start");
    const stopAction = document.querySelector(".sidebar-form-stop");
    const browserSessionPanel = document.querySelector("[data-browser-session-panel]");
    const statusProgress = document.getElementById("status_progress");
    const statusProgressAudit = document.getElementById("status_progress_audit");
    const statusProgressFill = document.getElementById("status_progress_fill");
    const statusProgressValue = document.getElementById("status_progress_value");
    const statusProgressDetail = document.getElementById("status_progress_detail");
    const progressProcessedLabel = document.querySelector("[data-progress-unit-label]");
    const cacheSourceSwitcher = document.querySelector("[data-cache-source-switcher]");
    const sectionLinks = Array.from(document.querySelectorAll("[data-section-link]"));
    const statusFields = Array.from(document.querySelectorAll("[data-status-field]"));
    const cacheContentModeControl = document.querySelector("[data-cache-content-mode]");
    const cacheContentModeStorageKey = "cachelikes:browser-content-mode:v1";

    let lastRenderedStatusSignature = "";
    let statusPollTimer = 0;
    let statusRefreshInFlight = false;
    let statusRefreshFailed = false;

    function readRememberedContentMode() {
        try {
            const rememberedMode = window.sessionStorage.getItem(cacheContentModeStorageKey);
            return rememberedMode === "media" || rememberedMode === "text" ? rememberedMode : "text";
        } catch (_error) {
            return "text";
        }
    }

    function syncCacheSourceSwitcherContentMode(mode) {
        if (!cacheSourceSwitcher) return;
        const normalizedMode = mode === "media" ? "media" : "text";
        cacheSourceSwitcher.dataset.cacheSourceContentMode = normalizedMode;
        cacheSourceSwitcher.querySelectorAll("[data-cache-source-switcher-option]").forEach((option) => {
            const isAvailable = normalizedMode === "media"
                || option.dataset.cacheSourceTextAvailable === "true";
            option.hidden = !isAvailable;
            if (!isAvailable) option.classList.remove("is-active");
        });
    }

    function syncCacheContentMode(mode) {
        if (!cacheContentModeControl) return;
        const normalizedMode = mode === "media" ? "media" : "text";
        syncCacheSourceSwitcherContentMode(normalizedMode);
        page.querySelectorAll("[data-cache-runtime-mode]").forEach((input) => {
            input.value = normalizedMode;
        });
        page.querySelectorAll("[data-chatgpt-metric-mode]").forEach((element) => {
            element.hidden = element.dataset.chatgptMetricMode !== normalizedMode;
        });
        const options = Array.from(
            cacheContentModeControl.querySelectorAll("[data-cache-content-mode-option]"),
        );
        const activeIndex = options.findIndex(
            (option) => option.dataset.cacheContentModeOption === normalizedMode,
        );
        cacheContentModeControl.dataset.segmentedActiveIndex = String(Math.max(activeIndex, 0));
        options.forEach((option) => {
            const isActive = option.dataset.cacheContentModeOption === normalizedMode;
            option.classList.toggle("is-active", isActive);
            option.setAttribute("aria-checked", String(isActive));
        });
        window.CACHELIKES_SEGMENTED_CONTROLS?.sync(cacheContentModeControl);
    }

    function rememberCacheContentMode(mode) {
        try {
            window.sessionStorage.setItem(cacheContentModeStorageKey, mode);
        } catch (_error) {
        }
    }

    function initializeCacheContentMode() {
        if (!cacheContentModeControl) return;
        syncCacheContentMode(readRememberedContentMode());
        cacheContentModeControl.addEventListener("click", (event) => {
            const option = event.target.closest("[data-cache-content-mode-option]");
            if (!option || !cacheContentModeControl.contains(option)) return;
            event.preventDefault();
            const mode = option.dataset.cacheContentModeOption;
            rememberCacheContentMode(mode);
            syncCacheContentMode(mode);
            void refreshStatus();
        });
    }

    initializeCacheContentMode();

    function setTextIfChanged(element, value) {
        if (!element) return;
        const normalizedValue = String(value ?? "");
        if (element.textContent !== normalizedValue) element.textContent = normalizedValue;
    }

    function setStatusValueIfChanged(element, value) {
        if (!element) return;
        const normalizedValue = String(value ?? "");
        if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) {
            if (element.value !== normalizedValue) element.value = normalizedValue;
            return;
        }
        setTextIfChanged(element, normalizedValue);
    }

    function clampPercent(value) {
        return Math.min(Math.max(Math.round(Number(value) || 0), 0), 100);
    }

    function formatMetricNumber(value) {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? numberFormatter.format(parsed) : "0";
    }

    function formatDatetime(value) {
        const rawValue = String(value ?? "").trim();
        if (!rawValue) return "";
        const dateOnlyMatch = rawValue.match(/^(\d{4})[-/](\d{2})[-/](\d{2})$/);
        if (dateOnlyMatch) {
            return dateOnlyMatch[3] + "/" + dateOnlyMatch[2] + "/" + dateOnlyMatch[1];
        }
        const compactDateMatch = rawValue.match(/^(\d{4})(\d{2})(\d{2})$/);
        if (compactDateMatch) {
            return compactDateMatch[3] + "/" + compactDateMatch[2] + "/" + compactDateMatch[1];
        }

        const parsed = new Date(rawValue);
        if (Number.isNaN(parsed.getTime())) return "Unknown date";
        const parts = Object.fromEntries(
            datetimeFormatter.formatToParts(parsed).map((part) => [part.type, part.value]),
        );
        const timezoneLabel = parts.timeZoneName || "";
        const offsetMinutes = String(-parsed.getTimezoneOffset());
        const timezoneCode = timezoneLabel === "GMT" && offsetMinutes === "0"
            ? "UTC"
            : /^[A-Za-z]{3}$/.test(timezoneLabel)
                ? timezoneLabel.toUpperCase()
                : timezoneCodesByOffset[offsetMinutes] || "UTC";
        return parts.day + " " + parts.month + " " + parts.year
            + " " + parts.hour + ":" + parts.minute + ":" + parts.second
            + " (" + timezoneCode + ")";
    }

    function setPhaseState(phase) {
        const normalizedPhase = String(phase || "idle");
        if (phaseChip) {
            const phaseDescription = `Cache phase: ${normalizedPhase}`;
            if (phaseChip.dataset.phase !== normalizedPhase) phaseChip.dataset.phase = normalizedPhase;
            if (phaseChip.getAttribute("aria-label") !== phaseDescription) {
                phaseChip.setAttribute("aria-label", phaseDescription);
            }
            if (phaseChip.title !== phaseDescription) phaseChip.title = phaseDescription;
        }
        setTextIfChanged(phaseValue, normalizedPhase);
        if (phaseValue && phaseValue.dataset.phase !== normalizedPhase) {
            phaseValue.dataset.phase = normalizedPhase;
        }
    }

    function updateSectionLinkState(activeId) {
        const normalizedActiveId = ["overview", sourceKey].includes(activeId)
            // Cache page anchors belong to the Cache Dock section, not a source-specific Dock item.
            ? "cache"
            : activeId;
        sectionLinks.forEach((link) => {
            link.classList.toggle("is-active", link.dataset.sectionLink === normalizedActiveId);
        });
    }

    function initializeSectionTracking() {
        const settingsDockLink = document.querySelector('[data-section-link="settings"]');
        settingsDockLink?.addEventListener("click", () => {
            if (typeof window.setSidebarOpen === "function") window.setSidebarOpen(true);
        });

        const observedSections = Array.from(document.querySelectorAll(".anchor-section"));
        if (!("IntersectionObserver" in window)) return;
        const sectionObserver = new IntersectionObserver((entries) => {
            const visibleEntry = entries
                .filter((entry) => entry.isIntersecting)
                .sort((left, right) => right.intersectionRatio - left.intersectionRatio)[0];
            if (visibleEntry) updateSectionLinkState(visibleEntry.target.id);
        }, {
            rootMargin: "-10% 0px -55% 0px",
            threshold: [0.2, 0.4, 0.6],
        });
        observedSections.forEach((section) => sectionObserver.observe(section));
    }

    function initializeCacheSourceSwitcher() {
        if (!cacheSourceSwitcher) return;
        const trigger = cacheSourceSwitcher.querySelector("[data-cache-source-switcher-trigger]");
        const menu = cacheSourceSwitcher.querySelector("[data-cache-source-switcher-menu]");
        const options = Array.from(cacheSourceSwitcher.querySelectorAll("[data-cache-source-switcher-option]"));
        if (!trigger || !menu || !options.length) return;

        function navigableOptions() {
            return options.filter((option) => !option.hidden);
        }

        function selectedOption() {
            const availableOptions = navigableOptions();
            return availableOptions.find((option) => option.getAttribute("aria-selected") === "true")
                || availableOptions[0];
        }

        function setActiveOption(option) {
            options.forEach((candidate) => candidate.classList.toggle("is-active", candidate === option));
            if (option?.id) {
                trigger.setAttribute("aria-activedescendant", option.id);
                option.scrollIntoView({ block: "nearest" });
            }
        }

        function setMenuOpen(isOpen) {
            cacheSourceSwitcher.classList.toggle("is-cache-source-menu-open", isOpen);
            trigger.setAttribute("aria-expanded", String(isOpen));
            menu.hidden = !isOpen;
            if (isOpen) {
                setActiveOption(selectedOption());
            } else {
                trigger.removeAttribute("aria-activedescendant");
            }
        }

        function navigateToOption(option) {
            if (option.hidden) return;
            const targetPath = option.dataset.cacheSourceSwitcherPath || "";
            if (!targetPath) return;

            const targetUrl = new URL(targetPath, window.location.origin);
            if (targetUrl.origin !== window.location.origin || targetUrl.href === window.location.href) {
                setMenuOpen(false);
                trigger.focus({ preventScroll: true });
                return;
            }
            window.location.assign(targetUrl.href);
        }

        trigger.addEventListener("click", () => {
            setMenuOpen(menu.hidden);
        });
        trigger.addEventListener("keydown", (event) => {
            if (event.key === "Escape") {
                if (menu.hidden) return;
                event.preventDefault();
                setMenuOpen(false);
                return;
            }
            if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
            event.preventDefault();
            setMenuOpen(true);
            const availableOptions = navigableOptions();
            if (!availableOptions.length) return;
            const selectedIndex = Math.max(availableOptions.indexOf(selectedOption()), 0);
            const targetIndex = event.key === "Home"
                ? 0
                : event.key === "End"
                    ? availableOptions.length - 1
                    : Math.min(
                        Math.max(selectedIndex + (event.key === "ArrowDown" ? 1 : -1), 0),
                        availableOptions.length - 1,
                    );
            setActiveOption(availableOptions[targetIndex]);
            availableOptions[targetIndex].focus({ preventScroll: true });
        });

        options.forEach((option) => {
            option.addEventListener("click", () => navigateToOption(option));
            option.addEventListener("keydown", (event) => {
                if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
                    event.preventDefault();
                    const availableOptions = navigableOptions();
                    const currentIndex = availableOptions.indexOf(option);
                    if (currentIndex < 0) return;
                    const nextIndex = event.key === "Home"
                        ? 0
                        : event.key === "End"
                            ? availableOptions.length - 1
                            : Math.min(
                                Math.max(currentIndex + (event.key === "ArrowDown" ? 1 : -1), 0),
                                availableOptions.length - 1,
                            );
                    setActiveOption(availableOptions[nextIndex]);
                    availableOptions[nextIndex].focus({ preventScroll: true });
                } else if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    navigateToOption(option);
                } else if (event.key === "Escape") {
                    event.preventDefault();
                    setMenuOpen(false);
                    trigger.focus({ preventScroll: true });
                } else if (event.key === "Tab") {
                    setMenuOpen(false);
                }
            });
        });

        document.addEventListener("click", (event) => {
            if (!cacheSourceSwitcher.contains(event.target)) setMenuOpen(false);
        });
        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape" && !menu.hidden) {
                setMenuOpen(false);
                trigger.focus({ preventScroll: true });
            }
        });
    }

    function resolveStatusFieldValue(element, data, fieldName) {
        const persistedValue = data[fieldName];
        const runningMaxField = runningMaximumFields[`${sourceKey}:${fieldName}`];
        if (!data.running || !runningMaxField) return persistedValue;

        const persistedNumber = Number(persistedValue);
        const runningNumber = Number(data[runningMaxField]);
        if (!Number.isFinite(runningNumber)) return persistedValue;
        if (!Number.isFinite(persistedNumber)) return runningNumber;
        return Math.max(persistedNumber, runningNumber);
    }

    function updateStatusFields(data) {
        statusFields.forEach((element) => {
            const fieldName = element.dataset.statusField;
            if (!fieldName) return;
            const rawValue = resolveStatusFieldValue(element, data, fieldName);
            if (element.dataset.statusFormat === "number") {
                setStatusValueIfChanged(element, formatMetricNumber(rawValue));
                return;
            }
            const fallback = element.dataset.statusFallback || "";
            if (element.dataset.statusFormat === "datetime") {
                setStatusValueIfChanged(
                    element,
                    rawValue === null || rawValue === undefined || rawValue === ""
                        ? fallback
                        : formatDatetime(rawValue),
                );
                return;
            }
            setStatusValueIfChanged(element, rawValue === null || rawValue === undefined || rawValue === ""
                ? fallback
                : String(rawValue));
        });
    }

    function renderProgressState(state) {
        if (!statusProgress || !statusProgressFill) return;
        const completePercent = clampPercent(state.completePercent);
        const auditPercent = clampPercent(state.auditPercent);
        const isIndeterminate = Boolean(state.isIndeterminate);
        statusProgress.classList.toggle("is-indeterminate", isIndeterminate);
        statusProgress.classList.toggle("is-unavailable", !isIndeterminate && state.hasMeasuredProgress === false);
        statusProgress.classList.toggle("is-auditing", auditPercent > 0 && !isIndeterminate);
        statusProgressFill.style.width = `${completePercent}%`;
        if (statusProgressAudit) statusProgressAudit.style.width = `${auditPercent}%`;
        if (state.detail) statusProgress.setAttribute("aria-valuetext", state.detail);
        if (isIndeterminate || state.hasMeasuredProgress === false) {
            statusProgress.removeAttribute("aria-valuenow");
        } else {
            statusProgress.setAttribute("aria-valuenow", String(Math.max(completePercent, auditPercent)));
        }
        if (state.label) setTextIfChanged(statusProgressValue, state.label);
        if (state.detail) setTextIfChanged(statusProgressDetail, state.detail);
    }

    function discoveryProgress(data) {
        const discovered = Number(data.discovered_tweets) || 0;
        const processed = (Number(data.downloaded_posts) || 0)
            + (Number(data.skipped_tweets) || 0)
            + (Number(data.failed_tweets) || 0);
        let completePercent = 0;
        let isIndeterminate = false;
        if (["collecting", "starting", "stopping"].includes(data.phase)) {
            isIndeterminate = true;
        } else if (["downloading", "failed"].includes(data.phase)) {
            completePercent = (processed / Math.max(discovered, processed, 1)) * 100;
        } else if (terminalPhases.has(data.phase)) {
            completePercent = 100;
        }
        return {
            completePercent,
            auditPercent: 0,
            isIndeterminate,
            hasMeasuredProgress: !isIndeterminate,
            label: `${clampPercent(completePercent)}%`,
            detail: "",
        };
    }

    function parseGrokAuditProgress(message) {
        const match = String(message || "").match(/Auditing Grok image quality\s+(\d+)\s*\/\s*(\d+)/i);
        if (!match) return null;
        const current = Number(match[1]);
        const total = Number(match[2]);
        if (!Number.isFinite(current) || !Number.isFinite(total) || total <= 0) return null;
        return { current, total, percent: clampPercent((current / total) * 100) };
    }

    function grokAuditProgress(data) {
        const queued = Math.max(Number(data.queued_tweets) || 0, 0);
        const processed = Math.min(Math.max(Number(data.processed_tweets) || 0, 0), queued);
        const auditProgress = parseGrokAuditProgress(data.message);
        let completePercent = 0;
        let auditPercent = 0;
        let isIndeterminate = false;
        let detail = "No Grok sync is active.";

        if (auditProgress) {
            auditPercent = auditProgress.percent;
            detail = `Auditing image quality: ${formatMetricNumber(auditProgress.current)} of ${formatMetricNumber(auditProgress.total)} assets.`;
        } else if (data.running && !data.discovery_complete) {
            isIndeterminate = true;
            detail = queued > 0
                ? `Discovery is still running. ${formatMetricNumber(processed)} of ${formatMetricNumber(queued)} scheduled downloads processed; the total may increase.`
                : "Scanning the Grok library. The final download total is not known yet.";
        } else if (queued > 0) {
            completePercent = (processed / queued) * 100;
            const pending = Math.max(queued - processed, 0);
            detail = pending > 0
                ? `${formatMetricNumber(processed)} of ${formatMetricNumber(queued)} scheduled downloads processed; ${formatMetricNumber(pending)} pending.`
                : `${formatMetricNumber(processed)} of ${formatMetricNumber(queued)} scheduled downloads processed.`;
        } else if (terminalPhases.has(data.phase)) {
            completePercent = 100;
            detail = "No new Grok downloads were required for this run.";
        }

        return {
            completePercent,
            auditPercent,
            isIndeterminate,
            hasMeasuredProgress: !isIndeterminate,
            label: `${clampPercent(Math.max(completePercent, auditPercent))}%`,
            detail,
        };
    }

    function queueProgress(data) {
        const queued = Math.max(Number(data.queued_tweets) || 0, 0);
        const processed = Math.min(Math.max(Number(data.processed_tweets) || 0, 0), queued);
        const progressUnits = {
            images: "image assets",
            conversations: "sessions",
            sessions: "sessions",
            resources: "resources",
        };
        const progressUnit = progressUnits[data.progress_unit] || "items";
        const hasMeasuredProgress = queued > 0;
        const isIndeterminate = Boolean(data.running && !hasMeasuredProgress);
        let completePercent = hasMeasuredProgress ? (processed / queued) * 100 : 0;
        let label = "Ready";
        let detail = `No ${sourceLabel} sync is active.`;

        if (isIndeterminate) {
            label = "Scanning";
            detail = `Scanning ${sourceLabel}. The final work-item total is not known yet.`;
        } else if (data.phase === "failed" && hasMeasuredProgress) {
            const notProcessed = Math.max(queued - processed, 0);
            const percent = clampPercent(completePercent);
            label = `Failed at ${percent}%`;
            detail = `${formatMetricNumber(processed)} / ${formatMetricNumber(queued)} ${progressUnit} processed before failure (${percent}%); ${formatMetricNumber(notProcessed)} not processed.`;
        } else if (hasMeasuredProgress) {
            const pending = Math.max(queued - processed, 0);
            const percent = clampPercent(completePercent);
            label = `${percent}%`;
            detail = pending > 0
                ? `${formatMetricNumber(processed)} / ${formatMetricNumber(queued)} ${progressUnit} processed (${percent}%); ${formatMetricNumber(pending)} pending.`
                : `${formatMetricNumber(processed)} / ${formatMetricNumber(queued)} ${progressUnit} processed (${percent}%).`;
        } else if (terminalPhases.has(data.phase)) {
            completePercent = 100;
            label = "100%";
            detail = `No new ${sourceLabel} resources were required for this run.`;
        } else if (data.phase === "failed") {
            label = "Failed";
            detail = `${sourceLabel} sync failed before a work-item total was established.`;
        }

        return {
            completePercent,
            auditPercent: 0,
            isIndeterminate,
            hasMeasuredProgress: hasMeasuredProgress || terminalPhases.has(data.phase),
            label,
            detail,
        };
    }

    const progressStrategies = Object.freeze({
        discovery: discoveryProgress,
        "grok-audit": grokAuditProgress,
        queue: queueProgress,
    });

    function updateProgress(data) {
        const strategy = sourceKey === "grok" && readRememberedContentMode() === "text"
            ? progressStrategies.queue
            : progressStrategies[progressStrategyName] || progressStrategies.queue;
        renderProgressState(strategy(data));
    }

    function updateProgressUnitLabel(data) {
        if (!progressProcessedLabel) return;
        const labels = {
            images: "Image assets processed",
            conversations: "Sessions processed",
            sessions: "Sessions processed",
            resources: "Resources processed",
        };
        setTextIfChanged(progressProcessedLabel, labels[data.progress_unit] || "Work items processed");
    }

    function updateActionState(data) {
        const browserDownloadReady = browserSessionPanel
            ? browserSessionPanel.dataset.browserDownloadReady !== "false"
            : true;
        const isRunning = Boolean(data.running);
        const shouldDisableStart = isRunning || !browserDownloadReady;
        const shouldDisableStop = !isRunning;
        if (startButton && startButton.disabled !== shouldDisableStart) startButton.disabled = shouldDisableStart;
        if (stopButton && stopButton.disabled !== shouldDisableStop) stopButton.disabled = shouldDisableStop;
        if (cacheActionRow) cacheActionRow.dataset.actionRunning = String(isRunning);
        if (startAction) startAction.hidden = isRunning;
        if (stopAction) stopAction.hidden = !isRunning;
    }

    function renderStatus(data) {
        const nextSignature = JSON.stringify(data);
        if (nextSignature === lastRenderedStatusSignature && !statusRefreshFailed) return;
        lastRenderedStatusSignature = nextSignature;
        statusRefreshFailed = false;
        updateStatusFields(data);
        setPhaseState(data.phase);
        updateProgressUnitLabel(data);
        updateProgress(data);
        updateActionState(data);
    }

    function scheduleStatusRefresh(delayMs = statusPollIntervalMs) {
        window.clearTimeout(statusPollTimer);
        if (!statusUrl || document.hidden) return;
        statusPollTimer = window.setTimeout(() => void refreshStatus(), delayMs);
    }

    async function refreshStatus() {
        if (!statusUrl || statusRefreshInFlight || document.hidden) return;
        statusRefreshInFlight = true;
        try {
            const requestUrl = new URL(statusUrl, window.location.href);
            const requestedMode = readRememberedContentMode();
            requestUrl.searchParams.set("content_mode", requestedMode);
            const response = await fetch(requestUrl, { cache: "no-store" });
            if (!response.ok) throw new Error(`Status request failed with ${response.status}`);
            const data = await response.json();
            if (requestedMode === readRememberedContentMode()) renderStatus(data);
        } catch (_error) {
            statusRefreshFailed = true;
            setTextIfChanged(statusProgressDetail, "Status refresh temporarily unavailable.");
        } finally {
            statusRefreshInFlight = false;
            scheduleStatusRefresh();
        }
    }

    function handleVisibilityChange() {
        window.clearTimeout(statusPollTimer);
        if (!document.hidden) void refreshStatus();
    }

    document.addEventListener("visibilitychange", handleVisibilityChange);
    window.addEventListener("pagehide", () => window.clearTimeout(statusPollTimer), { once: true });

    initializeCacheSourceSwitcher();
    initializeSectionTracking();
    void refreshStatus();
})();
