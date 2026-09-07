/* Code version: v1.32.0-codex.1 */

(function initializeLocalMediaBrowser() {
    "use strict";

    const dataNode = document.getElementById("browser_media_data");
    const dialog = document.getElementById("browser_detail_dialog");
    if (!dataNode || !dialog) return;

    const filterForm = document.querySelector(".browser-filter-form");
    const queryInput = document.querySelector("input[name='q'][data-browser-search-input]")
        || filterForm?.querySelector("input[name='q']");
    const usesSearchSuggestions = queryInput?.hasAttribute("data-browser-search-input") || false;
    const contentModeStorageKey = "cachelikes:browser-content-mode:v1";
    const contentModeInputs = filterForm
        ? Array.from(filterForm.querySelectorAll("input[name='view'][type='radio']"))
        : [];

    function readRememberedContentMode() {
        try {
            const stored = window.sessionStorage.getItem(contentModeStorageKey);
            return ["media", "text", "prompts"].includes(stored) ? stored : "";
        } catch (_error) {
            return "";
        }
    }

    function rememberContentMode(mode) {
        if (!["media", "text", "prompts"].includes(mode)) return;
        try {
            window.sessionStorage.setItem(contentModeStorageKey, mode);
        } catch (_error) {
        }
    }

    const contentModeNavigationTitles = Object.freeze({
        text: "Cached text browser",
        media: "Cached media browser",
        prompts: "Saved prompts",
    });

    const navigationSkeletonLine = (width = "100%", className = "") => `
        <span class="navigation-skeleton-line${className ? ` ${className}` : ""}"
              style="--navigation-skeleton-width: ${width};"></span>
    `;

    function renderOptimisticContentModeNavigation(mode) {
        const workspace = document.getElementById("browser_workspace");
        const title = contentModeNavigationTitles[mode];
        if (!(workspace instanceof HTMLElement) || !title) return false;

        const metricWidths = mode === "media"
            ? ["34%", "22%", "46%"]
            : mode === "prompts"
                ? ["42%", "18%", "24%"]
                : ["32%", "21%", "39%"];
        const resultWidths = mode === "media"
            ? ["92%", "68%", "76%", "54%"]
            : mode === "prompts"
                ? ["80%", "94%", "66%", "88%"]
                : ["88%", "74%", "96%", "62%"];
        const metricCards = metricWidths.map((width) => `
            <div class="navigation-skeleton-card browser-navigation-skeleton-metric">
                ${navigationSkeletonLine(width)}
                ${navigationSkeletonLine("42%", "browser-navigation-skeleton-value")}
            </div>
        `).join("");
        const resultRows = Array.from({length: mode === "media" ? 6 : 8}, (_, index) => `
            <div class="browser-navigation-skeleton-row">
                ${navigationSkeletonLine(resultWidths[index % resultWidths.length])}
                ${navigationSkeletonLine(`${Math.max(42, 88 - index * 5)}%`)}
            </div>
        `).join("");

        workspace.innerHTML = `
            <div class="navigation-skeleton-status sr-only" role="status" aria-live="polite">
                Loading ${title}
            </div>
            <div class="navigation-skeleton-root browser-navigation-skeleton-root" data-browser-navigation-skeleton aria-hidden="true">
                <div class="browser-navigation-skeleton-heading">
                    <div class="browser-navigation-skeleton-title">
                        ${navigationSkeletonLine("28%", "browser-navigation-skeleton-kicker")}
                        ${navigationSkeletonLine("58%", "browser-navigation-skeleton-heading-line")}
                    </div>
                    <span class="navigation-skeleton-card browser-navigation-skeleton-search"></span>
                </div>
                <div class="browser-navigation-skeleton-metrics">
                    ${metricCards}
                </div>
                <div class="navigation-skeleton-card browser-navigation-skeleton-results">
                    <div class="browser-navigation-skeleton-toolbar">
                        ${navigationSkeletonLine("28%")}
                        ${navigationSkeletonLine("16%")}
                    </div>
                    <div class="browser-navigation-skeleton-result-list">
                        ${resultRows}
                    </div>
                </div>
            </div>
        `;
        workspace.dataset.browserNavigationSkeleton = "1";
        workspace.setAttribute("aria-busy", "true");
        document.documentElement.dataset.navigationTarget = `browser-${mode}`;
        document.documentElement.setAttribute("aria-busy", "true");
        document.body.classList.add("is-page-navigating");
        return true;
    }

    function navigateToContentMode(mode) {
        if (!filterForm || !["media", "text", "prompts"].includes(mode)) return;
        const targetUrl = new URL(filterForm.action || window.location.href, window.location.origin);
        const formData = new FormData(filterForm);
        formData.set("view", mode);
        if (mode === "text") formData.set("session_view", "1");
        targetUrl.search = new URLSearchParams(formData).toString();
        ["page", "media_id", "session", "session_page"].forEach((name) => targetUrl.searchParams.delete(name));
        if (mode !== "media") targetUrl.searchParams.delete("kind");
        renderOptimisticContentModeNavigation(mode);
        let navigationCommitted = false;
        const commitNavigation = () => {
            if (navigationCommitted) return;
            navigationCommitted = true;
            window.location.assign(targetUrl.toString());
        };
        const fallbackTimer = window.setTimeout(commitNavigation, 120);
        window.requestAnimationFrame(() => {
            window.setTimeout(() => {
                window.clearTimeout(fallbackTimer);
                commitNavigation();
            }, 0);
        });
    }

    const currentUrl = new URL(window.location.href);
    const rememberedContentMode = readRememberedContentMode();
    if (!currentUrl.searchParams.has("view") && rememberedContentMode) {
        currentUrl.searchParams.set("view", rememberedContentMode);
        window.location.replace(currentUrl.href);
        return;
    }
    const checkedContentMode = contentModeInputs.find((input) => input.checked)?.value || "text";
    rememberContentMode(checkedContentMode);
    if (filterForm) {
        let filterSubmitTimer = 0;
        const submitFilters = () => {
            if (filterSubmitTimer) window.clearTimeout(filterSubmitTimer);
            filterSubmitTimer = window.setTimeout(() => {
                filterSubmitTimer = 0;
                filterForm.requestSubmit();
            }, 180);
        };
        filterForm.addEventListener("change", (event) => {
            if (event.target.matches("input[name='view'][type='radio']")) {
                rememberContentMode(event.target.value);
                navigateToContentMode(event.target.value);
                return;
            }
            if (event.target.matches("select")) submitFilters();
        });
        filterForm.addEventListener("input", (event) => {
            if (event.target.matches("input[name='q']") && !usesSearchSuggestions) submitFilters();
        });
        if (queryInput && !filterForm.contains(queryInput) && !usesSearchSuggestions) {
            queryInput.addEventListener("input", submitFilters);
        }
    }

    let mediaItems = [];
    try {
        const parsed = JSON.parse(dataNode.textContent || "[]");
        mediaItems = Array.isArray(parsed) ? parsed : [];
    } catch (_error) {
        mediaItems = [];
    }

    const mediaById = new Map(
        mediaItems
            .filter((item) => item && typeof item.id === "string")
            .map((item) => [item.id, item]),
    );
    const previewElements = Array.from(document.querySelectorAll("[data-preview]"));
    const mediaCards = Array.from(document.querySelectorAll("[data-media-id]"));
    const mediaGallery = document.querySelector(".browser-gallery");
    const viewButtons = Array.from(document.querySelectorAll("[data-browser-view]"));
    const sessionRefreshButton = document.querySelector("[data-chatgpt-session-refresh]");
    const sessionViewButton = document.querySelector("[data-chatgpt-session-view]");
    const sessionRefreshTooltipTitle = sessionRefreshButton?.querySelector("[data-session-refresh-tooltip-title]");
    const sessionRefreshTooltipCopy = sessionRefreshButton?.querySelector("[data-session-refresh-tooltip-copy]");
    const sessionRefreshBanner = document.querySelector("[data-chatgpt-session-refresh-banner]");
    const sessionRefreshBannerTitle = sessionRefreshBanner?.querySelector("[data-session-refresh-title]");
    const sessionRefreshBannerCopy = sessionRefreshBanner?.querySelector("[data-session-refresh-copy]");
    const sourceCopyButtons = Array.from(document.querySelectorAll("[data-media-copy-source-url]"));
    const revealButtons = Array.from(document.querySelectorAll("[data-media-reveal]"));
    const promptToggleButtons = Array.from(document.querySelectorAll("[data-media-prompt-toggle]"));
    const promptAddButtons = Array.from(document.querySelectorAll("[data-prompt-add]"));
    const promptCopyButtons = Array.from(document.querySelectorAll("[data-prompt-copy]"));
    const promptRemarkRemoveButtons = Array.from(document.querySelectorAll("[data-prompt-remark-remove]"));
    const mediaViewStorageKey = "cachelikes.browser.mediaView";
    const paginationMotion = window.CACHELIKES_PAGINATION_MOTION;

    const wait = (milliseconds) => new Promise((resolve) => {
        window.setTimeout(resolve, milliseconds);
    });

    const setSessionRefreshTooltip = (title, copy) => {
        if (sessionRefreshTooltipTitle) sessionRefreshTooltipTitle.textContent = title;
        if (sessionRefreshTooltipCopy) sessionRefreshTooltipCopy.textContent = copy;
    };

    const parseOptionalCount = (url, parameter) => {
        const rawValue = url.searchParams.get(parameter);
        if (rawValue === null) return null;
        const parsedValue = Number.parseInt(rawValue, 10);
        return Number.isFinite(parsedValue) ? Math.max(0, parsedValue) : null;
    };

    const formatCount = (count) => new Intl.NumberFormat("en-US").format(count);
    const imageCountLabel = (count) => `${formatCount(count)} generated image${count === 1 ? "" : "s"}`;

    function showSessionRefreshResult() {
        if (!sessionRefreshBanner) return;
        const currentUrl = new URL(window.location.href);
        const rawUpdatedCount = currentUrl.searchParams.get("session_updated");
        if (rawUpdatedCount === null) return;

        const updatedCount = Math.max(0, Number.parseInt(rawUpdatedCount, 10) || 0);
        const sessionLabel = sessionRefreshButton?.dataset.chatgptSessionLabel?.trim()
            || document.querySelector(".browser-session-name-metric strong")?.textContent?.trim()
            || "the current ChatGPT session";
        const discoveredCount = parseOptionalCount(currentUrl, "session_discovered");
        const cachedCount = parseOptionalCount(currentUrl, "session_cached");
        const skippedCount = parseOptionalCount(currentUrl, "session_skipped");
        const failedCount = parseOptionalCount(currentUrl, "session_failed");
        const detailParts = [];
        if (discoveredCount !== null) {
            detailParts.push(`The refresh found ${imageCountLabel(discoveredCount)} in the session.`);
        }
        if (skippedCount) {
            detailParts.push(
                `${imageCountLabel(skippedCount)} ${skippedCount === 1 ? "was" : "were"} already cached.`,
            );
        }
        if (cachedCount !== null) {
            detailParts.push(
                `The ChatGPT image cache now contains ${formatCount(cachedCount)} images.`,
            );
        }
        if (failedCount) {
            detailParts.push(
                `${imageCountLabel(failedCount)} could not be fetched.`,
            );
        }
        if (sessionRefreshBannerTitle) {
            sessionRefreshBannerTitle.textContent = updatedCount
                ? `Added ${imageCountLabel(updatedCount)}`
                : "No new generated images found";
        }
        if (sessionRefreshBannerCopy) {
            const summary = updatedCount
                ? `Pulled ${imageCountLabel(updatedCount)} from ChatGPT session ${sessionLabel} and added them to the local media cache.`
                : `Checked ChatGPT session ${sessionLabel} for new generated images. Nothing new was pulled; the local media cache is unchanged.`;
            sessionRefreshBannerCopy.textContent = [summary, ...detailParts].join(" ");
        }
        sessionRefreshBanner.hidden = false;
        ["session_updated", "session_discovered", "session_cached", "session_skipped", "session_failed"]
            .forEach((parameter) => currentUrl.searchParams.delete(parameter));
        window.history.replaceState({}, "", currentUrl.toString());
    }

    showSessionRefreshResult();

    async function refreshCurrentChatGPTSession(button) {
        const conversationUrl = button.dataset.chatgptSessionUrl || "";
        if (!conversationUrl) return;

        const waitNotice = window.CacheWaitModal?.show({
            title: "Refreshing ChatGPT session",
            copy: "Scanning only this session for newly generated images. The browser stays in the background.",
        });
        button.disabled = true;
        setSessionRefreshTooltip(
            "Refreshing this session",
            "Checking ChatGPT for newly generated images.",
        );
        button.setAttribute("aria-label", "Refreshing session…");

        try {
            const startResponse = await fetch("/api/browser/chatgpt/session/refresh", {
                method: "POST",
                cache: "no-store",
                headers: {
                    Accept: "application/json",
                    "Content-Type": "application/json",
                },
                body: JSON.stringify({ conversation_url: conversationUrl }),
            });
            const startPayload = await startResponse.json();
            if (!startResponse.ok) {
                throw new Error(startPayload.error || "Unable to start the ChatGPT session refresh.");
            }

            const statusUrl = startPayload.status_url || "/api/chatgpt/status";
            const initialResourceCount = Number(startPayload.resource_count) || 0;
            let updatedCount = 0;
            let refreshSummary = {
                discoveredImages: null,
                cachedImages: null,
                skippedImages: null,
                failedImages: null,
            };
            while (true) {
                await wait(1_000);
                const statusResponse = await fetch(statusUrl, {
                    cache: "no-store",
                    headers: { Accept: "application/json" },
                });
                if (!statusResponse.ok) continue;
                const snapshot = await statusResponse.json();
                if (snapshot.running) continue;
                if (snapshot.last_error || snapshot.phase === "failed") {
                    throw new Error(snapshot.last_error || "The ChatGPT session refresh failed.");
                }
                updatedCount = Math.max(
                    0,
                    (Number(snapshot.downloaded_images) || 0) - initialResourceCount,
                );
                refreshSummary = {
                    discoveredImages: Math.max(0, Number(snapshot.discovered_images) || 0),
                    cachedImages: Math.max(0, Number(snapshot.downloaded_images) || 0),
                    skippedImages: Math.max(0, Number(snapshot.skipped_tweets) || 0),
                    failedImages: Math.max(0, Number(snapshot.failed_tweets) || 0),
                };
                break;
            }

            const refreshedUrl = new URL(window.location.href);
            refreshedUrl.searchParams.set("source", "chatgpt");
            refreshedUrl.searchParams.set(
                "session",
                startPayload.session_key || button.dataset.chatgptSessionKey || "",
            );
            refreshedUrl.searchParams.set("refresh", "1");
            refreshedUrl.searchParams.set("session_updated", String(updatedCount));
            Object.entries({
                session_discovered: refreshSummary.discoveredImages,
                session_cached: refreshSummary.cachedImages,
                session_skipped: refreshSummary.skippedImages,
                session_failed: refreshSummary.failedImages,
            }).forEach(([parameter, value]) => {
                if (value !== null) refreshedUrl.searchParams.set(parameter, String(value));
            });
            window.location.assign(refreshedUrl.toString());
        } catch (error) {
            waitNotice?.finish();
            button.disabled = false;
            setSessionRefreshTooltip(
                "Refresh this session",
                "Check this ChatGPT session for newly generated images.",
            );
            button.setAttribute("aria-label", "Refresh this session");
            window.alert(error instanceof Error ? error.message : "Unable to refresh this ChatGPT session.");
        }
    }

    sessionRefreshButton?.addEventListener("click", () => {
        refreshCurrentChatGPTSession(sessionRefreshButton);
    });

    sessionViewButton?.addEventListener("click", () => {
        const isPressed = sessionViewButton.getAttribute("aria-pressed") === "true";
        sessionViewButton.setAttribute("aria-pressed", String(!isPressed));
        const targetUrl = new URL(window.location.href);
        targetUrl.searchParams.set("source", "chatgpt");
        targetUrl.searchParams.set("session_view", isPressed ? "0" : "1");
        targetUrl.searchParams.delete("page");
        targetUrl.searchParams.delete("session");
        window.location.assign(targetUrl.toString());
    });

    function applyMediaView(view, { persist = true } = {}) {
        if (!mediaGallery || !["grid", "list"].includes(view)) return;
        mediaGallery.dataset.view = view;
        mediaGallery.setAttribute(
            "aria-label",
            view === "list" ? "Cached media list" : "Cached media gallery",
        );
        viewButtons.forEach((button) => {
            const isActive = button.dataset.browserView === view;
            button.classList.toggle("is-active", isActive);
            button.setAttribute("aria-pressed", String(isActive));
        });
        window.requestAnimationFrame(updatePromptToggleVisibility);
        if (!persist) return;
        try {
            window.localStorage.setItem(mediaViewStorageKey, view);
        } catch (_error) {
        }
    }

    let initialMediaView = "grid";
    try {
        const savedMediaView = window.localStorage.getItem(mediaViewStorageKey);
        if (["grid", "list"].includes(savedMediaView)) initialMediaView = savedMediaView;
    } catch (_error) {
    }
    applyMediaView(initialMediaView, { persist: false });

    viewButtons.forEach((button) => {
        button.addEventListener("click", () => applyMediaView(button.dataset.browserView || "grid"));
    });

    const previewObserver = "IntersectionObserver" in window
        ? new IntersectionObserver((entries, observer) => {
            entries.forEach((entry) => {
                if (!entry.isIntersecting) return;
                loadPreview(entry.target);
                observer.unobserve(entry.target);
            });
        }, { rootMargin: "240px 0px", threshold: 0.01 })
        : null;

    function previewShell(element) {
        return element.closest("[data-preview-shell]");
    }

    function setPreviewStatus(element, message, { failed = false, loading = false } = {}) {
        const shell = previewShell(element);
        const status = shell?.querySelector("[data-preview-status]");
        if (!status) return;
        const statusCopy = status.querySelector("[data-preview-status-copy]");
        const spinner = status.querySelector("[data-preview-spinner]");
        if (statusCopy) {
            statusCopy.textContent = message;
        } else {
            status.textContent = message;
        }
        status.hidden = !message;
        if (spinner) spinner.hidden = !loading;
        shell.classList.toggle("is-load-failed", failed);
        shell.classList.toggle("is-ready", !failed && !message);
    }

    function loadPreview(element) {
        if (!element || element.dataset.previewLoaded === "1") return;
        const source = element.dataset.mediaSrc;
        if (!source) {
            setPreviewStatus(element, "Preview unavailable", { failed: true });
            return;
        }

        element.dataset.previewLoaded = "1";
        setPreviewStatus(element, "Loading preview", { loading: true });
        const onReady = () => setPreviewStatus(element, "");
        const onError = () => {
            element.hidden = true;
            setPreviewStatus(element, "Preview unavailable", { failed: true });
        };
        element.addEventListener("load", onReady, { once: true });
        element.addEventListener("loadeddata", onReady, { once: true });
        element.addEventListener("error", onError, { once: true });
        element.src = source;
        if (element.tagName === "VIDEO") element.load();
    }

    previewElements.forEach((element) => {
        if (previewObserver) {
            previewObserver.observe(element);
        } else if (element.tagName === "IMG") {
            loadPreview(element);
        }
    });

    async function copyText(value) {
        if (!value) return false;
        if (navigator.clipboard?.writeText) {
            try {
                await navigator.clipboard.writeText(value);
                return true;
            } catch (_error) {
            }
        }

        const textarea = document.createElement("textarea");
        textarea.value = value;
        textarea.setAttribute("readonly", "");
        textarea.style.position = "fixed";
        textarea.style.opacity = "0";
        textarea.style.pointerEvents = "none";
        document.body.append(textarea);
        textarea.select();
        let didCopy = false;
        try {
            didCopy = document.execCommand("copy");
        } catch (_error) {
            didCopy = false;
        }
        textarea.remove();
        return didCopy;
    }

    function setPromptCopyFeedback(button, didCopy) {
        const feedback = button.querySelector("[data-prompt-copy-feedback]");
        button.classList.remove("is-copied", "is-copy-failed");
        void button.offsetWidth;
        button.classList.add(didCopy ? "is-copied" : "is-copy-failed");
        button.setAttribute("aria-label", didCopy ? "Prompt copied" : "Unable to copy prompt");
        button.title = didCopy ? "Prompt copied" : "Unable to copy prompt";
        if (feedback) feedback.textContent = didCopy ? "Prompt copied." : "Unable to copy prompt.";
        window.setTimeout(() => {
            button.classList.remove("is-copied", "is-copy-failed");
            button.setAttribute("aria-label", "Copy prompt");
            button.title = "Copy prompt";
            if (feedback) feedback.textContent = "";
        }, 1_600);
    }

    promptCopyButtons.forEach((button) => {
        button.addEventListener("click", async () => {
            const didCopy = await copyText(button.dataset.promptText || "");
            setPromptCopyFeedback(button, didCopy);
        });
    });

    function promptRemarksRoot(element) {
        return element.closest("[data-prompt-remarks]");
    }

    function setPromptRemarksFeedback(root, message) {
        const feedback = root?.querySelector("[data-prompt-remarks-feedback]");
        if (!feedback) return;
        feedback.textContent = message;
        window.setTimeout(() => {
            if (feedback.textContent === message) feedback.textContent = "";
        }, 1_600);
    }

    function updatePromptRemarkOptions(options) {
        const uniqueOptions = new Map();
        (Array.isArray(options) ? options : []).forEach((value) => {
            const remark = String(value || "").trim();
            if (!remark) return;
            const key = remark.toLocaleLowerCase();
            if (!uniqueOptions.has(key)) uniqueOptions.set(key, remark);
        });

        const datalist = document.querySelector("[data-prompt-remark-options]");
        if (!datalist) return;
        datalist.replaceChildren();
        Array.from(uniqueOptions.values())
            .sort((left, right) => left.localeCompare(right))
            .forEach((remark) => {
                const option = document.createElement("option");
                option.value = remark;
                option.textContent = remark;
                datalist.append(option);
            });
    }

    function bindPromptRemarkRemoveButton(button) {
        button.addEventListener("click", () => {
            const root = promptRemarksRoot(button);
            removePromptRemark(root, button.dataset.promptRemark || "", button);
        });
    }

    function renderPromptRemarks(root, remarks) {
        const tags = root?.querySelector("[data-prompt-tags]");
        if (!tags) return;
        tags.replaceChildren();
        (Array.isArray(remarks) ? remarks : []).forEach((value) => {
            const remark = String(value || "").trim();
            if (!remark) return;

            const tag = document.createElement("span");
            tag.className = "browser-prompt-tag";
            tag.dataset.promptTag = "";
            tag.dataset.promptRemark = remark;

            const label = document.createElement("span");
            label.className = "browser-prompt-tag-label";
            label.textContent = remark;

            const removeButton = document.createElement("button");
            removeButton.type = "button";
            removeButton.className = "browser-prompt-tag-remove";
            removeButton.dataset.promptRemarkRemove = "";
            removeButton.dataset.promptRemark = remark;
            removeButton.setAttribute("aria-label", `Remove remark ${remark}`);
            removeButton.title = "Remove remark";
            removeButton.textContent = "×";
            bindPromptRemarkRemoveButton(removeButton);

            tag.append(label, removeButton);
            tags.append(tag);
        });
    }

    function applyPromptRemarkPayload(root, payload) {
        renderPromptRemarks(root, payload?.item?.remarks || []);
        updatePromptRemarkOptions(payload?.remark_options || []);
    }

    async function addPromptRemark(root, remark) {
        const promptId = root?.dataset.promptId || "";
        const normalizedRemark = String(remark || "").trim();
        if (!promptId) return;
        if (!normalizedRemark) {
            setPromptRemarksFeedback(root, "Enter or choose a remark.");
            return;
        }

        const input = root.querySelector("[data-prompt-remark-input]");
        if (input?.disabled) return;
        if (input) input.disabled = true;
        try {
            const response = await fetch(`/api/browser/prompts/${encodeURIComponent(promptId)}/remarks`, {
                method: "POST",
                cache: "no-store",
                headers: {
                    Accept: "application/json",
                    "Content-Type": "application/json",
                },
                body: JSON.stringify({ remark: normalizedRemark }),
            });
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.error || "Unable to add this remark.");
            applyPromptRemarkPayload(root, payload);
            root.querySelector("[data-prompt-remark-input]").value = "";
            setPromptRemarksFeedback(root, payload.created ? "Remark added." : "Remark already added.");
        } catch (error) {
            setPromptRemarksFeedback(root, error instanceof Error ? error.message : "Unable to add this remark.");
        } finally {
            if (input) input.disabled = false;
        }
    }

    async function removePromptRemark(root, remark, button = null) {
        const promptId = root?.dataset.promptId || "";
        const normalizedRemark = String(remark || "").trim();
        if (!promptId || !normalizedRemark) return;
        if (button) button.disabled = true;
        try {
            const response = await fetch(`/api/browser/prompts/${encodeURIComponent(promptId)}/remarks`, {
                method: "DELETE",
                cache: "no-store",
                headers: {
                    Accept: "application/json",
                    "Content-Type": "application/json",
                },
                body: JSON.stringify({ remark: normalizedRemark }),
            });
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.error || "Unable to remove this remark.");
            applyPromptRemarkPayload(root, payload);
            setPromptRemarksFeedback(root, "Remark removed.");
        } catch (error) {
            if (button) button.disabled = false;
            setPromptRemarksFeedback(root, error instanceof Error ? error.message : "Unable to remove this remark.");
        }
    }

    promptRemarkRemoveButtons.forEach(bindPromptRemarkRemoveButton);

    document.querySelectorAll("[data-prompt-remark-input]").forEach((input) => {
        input.addEventListener("keydown", (event) => {
            if (event.key !== "Enter" || event.isComposing) return;
            event.preventDefault();
            const root = input.closest("[data-prompt-remarks]");
            addPromptRemark(root, input.value.trim());
        });
    });

    promptAddButtons.forEach((button) => {
        button.addEventListener("click", async () => {
            if (button.disabled) return;
            button.disabled = true;
            try {
                const response = await fetch("/api/browser/prompts", {
                    method: "POST",
                    cache: "no-store",
                    headers: {
                        Accept: "application/json",
                        "Content-Type": "application/json",
                    },
                    body: JSON.stringify({
                        source: button.dataset.promptSource || "",
                        conversation_id: button.dataset.promptConversationId || "",
                        message_key: button.dataset.promptMessageKey || "",
                    }),
                });
                const payload = await response.json();
                if (!response.ok) throw new Error(payload.error || "Unable to save this prompt.");
                button.classList.add("is-added");
                button.setAttribute("aria-label", "Added as prompt");
                button.title = "Added as prompt";
                const feedback = button.querySelector("[data-prompt-add-feedback]");
                if (feedback) feedback.textContent = "Added as prompt.";
            } catch (error) {
                button.disabled = false;
                window.alert(error instanceof Error ? error.message : "Unable to save this prompt.");
            }
        });
    });

    const copyFeedbackTimers = new WeakMap();

    function setCopyFeedback(button, didCopy) {
        const activeTimer = copyFeedbackTimers.get(button);
        if (activeTimer) window.clearTimeout(activeTimer);

        const feedback = button.querySelector("[data-media-copy-feedback]");
        button.classList.remove("is-copied", "is-copy-failed");
        void button.offsetWidth;
        button.classList.add(didCopy ? "is-copied" : "is-copy-failed");
        button.setAttribute("aria-label", didCopy ? "Original URL copied" : "Unable to copy original URL");
        button.title = didCopy ? "URL copied" : "Unable to copy URL";
        if (feedback) feedback.textContent = didCopy ? "Original URL copied." : "Unable to copy original URL.";

        const timer = window.setTimeout(() => {
            button.classList.remove("is-copied", "is-copy-failed");
            button.setAttribute("aria-label", "Copy original URL");
            button.title = "Copy URL";
            if (feedback) feedback.textContent = "";
            copyFeedbackTimers.delete(button);
        }, 1_600);
        copyFeedbackTimers.set(button, timer);
    }

    sourceCopyButtons.forEach((button) => {
        button.addEventListener("click", async () => {
            const sourceLink = button.parentElement?.querySelector("[data-media-source-link]");
            const url = sourceLink instanceof HTMLAnchorElement ? sourceLink.href : "";
            const didCopy = await copyText(url);
            setCopyFeedback(button, didCopy);
        });
    });

    const revealFeedbackTimers = new WeakMap();

    function setRevealFeedback(button, fileManager) {
        const activeTimer = revealFeedbackTimers.get(button);
        if (activeTimer) window.clearTimeout(activeTimer);

        const defaultLabel = button.dataset.defaultLabel || "Show in file manager";
        const feedback = button.querySelector("[data-media-reveal-feedback]");
        const successLabel = `Shown in ${fileManager || "file manager"}`;
        button.classList.remove("is-revealed");
        void button.offsetWidth;
        button.classList.add("is-revealed");
        button.setAttribute("aria-label", successLabel);
        button.title = successLabel;
        if (feedback) feedback.textContent = `${successLabel}.`;

        const timer = window.setTimeout(() => {
            button.classList.remove("is-revealed");
            button.setAttribute("aria-label", defaultLabel);
            button.title = defaultLabel;
            if (feedback) feedback.textContent = "";
            revealFeedbackTimers.delete(button);
        }, 1_600);
        revealFeedbackTimers.set(button, timer);
    }

    revealButtons.forEach((button) => {
        button.addEventListener("click", async () => {
            const card = button.closest("[data-media-id]");
            const item = card ? mediaById.get(card.dataset.mediaId || "") : null;
            if (!item) return;

            button.disabled = true;
            try {
                const response = await fetch(`/api/browser/media/${encodeURIComponent(item.id)}/reveal`, {
                    method: "POST",
                    headers: { Accept: "application/json" },
                });
                const payload = await response.json();
                if (!response.ok) throw new Error(payload.error || "Unable to show the cached media file.");
                setRevealFeedback(button, payload.file_manager);
            } catch (error) {
                window.alert(error instanceof Error ? error.message : "Unable to show the cached media file.");
            } finally {
                button.disabled = false;
            }
        });
    });

    function togglePrompt(button) {
        const sourceId = button.dataset.promptSource || "";
        const source = sourceId ? document.getElementById(sourceId) : null;
        if (!source) return;

        const prompt = button.closest(".browser-media-prompt");
        if (!prompt) return;
        const isExpanded = button.getAttribute("aria-expanded") === "true";
        const nextExpanded = !isExpanded;
        const filename = button.dataset.promptTitle || "this media item";
        const action = nextExpanded ? "Collapse" : "Expand";

        prompt.classList.toggle("is-expanded", nextExpanded);
        button.setAttribute("aria-expanded", String(nextExpanded));
        button.setAttribute("aria-label", `${action} prompt for ${filename}`);
        button.title = `${action} prompt`;
        window.requestAnimationFrame(updatePromptToggleVisibility);
    }

    function updatePromptToggleVisibility() {
        promptToggleButtons.forEach((button) => {
            const sourceId = button.dataset.promptSource || "";
            const source = sourceId ? document.getElementById(sourceId) : null;
            const prompt = button.closest(".browser-media-prompt");
            if (!source || !prompt) return;

            if (button.getAttribute("aria-expanded") === "true") {
                button.hidden = false;
                prompt.classList.remove("is-fully-visible");
                return;
            }

            const isFullyVisible = source.scrollHeight <= source.clientHeight + 1;
            button.hidden = isFullyVisible;
            prompt.classList.toggle("is-fully-visible", isFullyVisible);
        });
    }

    promptToggleButtons.forEach((button) => {
        button.addEventListener("click", () => togglePrompt(button));
    });

    window.requestAnimationFrame(updatePromptToggleVisibility);
    if (document.fonts?.ready) {
        document.fonts.ready.then(updatePromptToggleVisibility);
    }
    if ("ResizeObserver" in window) {
        const promptResizeObserver = new ResizeObserver(() => {
            window.requestAnimationFrame(updatePromptToggleVisibility);
        });
        promptToggleButtons.forEach((button) => {
            const sourceId = button.dataset.promptSource || "";
            const source = sourceId ? document.getElementById(sourceId) : null;
            if (source) promptResizeObserver.observe(source);
        });
    }

    const pagination = document.querySelector(".browser-pagination");

    function positionPaginationIndicator({ immediate = false } = {}) {
        if (!pagination || !paginationMotion) return;
        paginationMotion.positionPaginationIndicator(
            pagination,
            pagination.querySelector(".local-store-page-button.is-active"),
            { immediate },
        );
    }

    if (pagination) {
        window.requestAnimationFrame(() => positionPaginationIndicator());
        window.addEventListener(
            "resize",
            () => positionPaginationIndicator({ immediate: true }),
            { passive: true },
        );
    }

    const paginationRangePickers = pagination
        ? Array.from(pagination.querySelectorAll(".browser-pagination-range-picker"))
        : [];
    let pinnedPaginationRangePicker = null;
    let paginationRangeCloseTimer = 0;

    function paginationRangeElements(picker) {
        return {
            trigger: picker?.querySelector("[data-pagination-range-trigger]") || null,
            menu: picker?.querySelector("[data-pagination-range-menu]") || null,
        };
    }

    function paginationRangeMenuContentHeight(menu) {
        const grid = menu?.querySelector(".browser-pagination-range-grid");
        if (!menu || !grid) return 0;
        const style = window.getComputedStyle(menu);
        const paddingTop = Number.parseFloat(style.paddingTop) || 0;
        const paddingBottom = Number.parseFloat(style.paddingBottom) || 0;
        return grid.scrollHeight + paddingTop + paddingBottom;
    }

    function paginationRangeMenuClipBounds(picker) {
        let ancestor = picker?.parentElement || null;
        while (ancestor) {
            const style = window.getComputedStyle(ancestor);
            if (style.overflowX !== "visible" || style.overflowY !== "visible") {
                const rect = ancestor.getBoundingClientRect();
                return {
                    top: rect.top,
                    bottom: rect.bottom,
                };
            }
            ancestor = ancestor.parentElement;
        }
        return {
            top: 0,
            bottom: window.innerHeight,
        };
    }

    function positionPaginationRangeMenu(picker) {
        const { menu } = paginationRangeElements(picker);
        if (!menu || !picker.classList.contains("is-open")) return;
        menu.classList.remove("is-below");
        menu.style.removeProperty("--pagination-range-menu-shift-x");
        menu.style.removeProperty("--pagination-range-menu-max-height");
        const pickerRect = picker.getBoundingClientRect();
        const viewportInset = 12;
        const menuGap = 8;
        const clipBounds = paginationRangeMenuClipBounds(picker);
        const clipTop = Math.max(viewportInset, clipBounds.top);
        const clipBottom = Math.min(window.innerHeight - viewportInset, clipBounds.bottom);
        const spaceAbove = Math.max(0, pickerRect.top - clipTop - menuGap);
        const spaceBelow = Math.max(0, clipBottom - pickerRect.bottom - menuGap);
        const naturalMenuHeight = paginationRangeMenuContentHeight(menu);
        if (naturalMenuHeight > spaceAbove && spaceBelow > spaceAbove) {
            menu.classList.add("is-below");
        }
        const availableHeight = menu.classList.contains("is-below") ? spaceBelow : spaceAbove;
        menu.style.setProperty("--pagination-range-menu-max-height", `${Math.max(1, availableHeight)}px`);
        menu.classList.toggle("is-scrollable", naturalMenuHeight > menu.clientHeight + 1);
        const menuWidth = menu.offsetWidth;
        const idealMenuLeft = pickerRect.left + (pickerRect.width / 2) - (menuWidth / 2);
        let horizontalShift = 0;
        if (idealMenuLeft < viewportInset) {
            horizontalShift = viewportInset - idealMenuLeft;
        } else if (idealMenuLeft + menuWidth > window.innerWidth - viewportInset) {
            horizontalShift = window.innerWidth - viewportInset - idealMenuLeft - menuWidth;
        }
        menu.style.setProperty("--pagination-range-menu-shift-x", `${horizontalShift}px`);
    }

    function setPaginationRangePickerOpen(picker, shouldOpen, { focusFirst = false } = {}) {
        if (!picker) return;
        const { trigger, menu } = paginationRangeElements(picker);
        picker.classList.toggle("is-open", shouldOpen);
        trigger?.setAttribute("aria-expanded", shouldOpen ? "true" : "false");
        menu?.setAttribute("aria-hidden", shouldOpen ? "false" : "true");
        if (!shouldOpen) {
            menu?.classList.remove("is-below");
            menu?.classList.remove("is-scrollable");
            menu?.style.removeProperty("--pagination-range-menu-shift-x");
            menu?.style.removeProperty("--pagination-range-menu-max-height");
            return;
        }
        paginationRangePickers.forEach((otherPicker) => {
            if (otherPicker !== picker) setPaginationRangePickerOpen(otherPicker, false);
        });
        window.requestAnimationFrame(() => {
            positionPaginationRangeMenu(picker);
            if (focusFirst) {
                menu?.querySelector(".browser-pagination-range-option")?.focus();
            }
        });
    }

    function cancelPaginationRangeClose() {
        if (!paginationRangeCloseTimer) return;
        window.clearTimeout(paginationRangeCloseTimer);
        paginationRangeCloseTimer = 0;
    }

    function schedulePaginationRangeClose(picker) {
        cancelPaginationRangeClose();
        if (pinnedPaginationRangePicker === picker) return;
        paginationRangeCloseTimer = window.setTimeout(() => {
            paginationRangeCloseTimer = 0;
            if (!picker.matches(":hover") && !picker.contains(document.activeElement)) {
                setPaginationRangePickerOpen(picker, false);
            }
        }, 140);
    }

    paginationRangePickers.forEach((picker) => {
        const { trigger, menu } = paginationRangeElements(picker);
        picker.addEventListener("pointerenter", () => {
            cancelPaginationRangeClose();
            setPaginationRangePickerOpen(picker, true);
        });
        picker.addEventListener("pointerleave", () => schedulePaginationRangeClose(picker));
        picker.addEventListener("focusin", () => {
            cancelPaginationRangeClose();
            setPaginationRangePickerOpen(picker, true);
        });
        picker.addEventListener("focusout", () => schedulePaginationRangeClose(picker));
        trigger?.addEventListener("click", () => {
            cancelPaginationRangeClose();
            const shouldPin = pinnedPaginationRangePicker !== picker;
            if (pinnedPaginationRangePicker && pinnedPaginationRangePicker !== picker) {
                setPaginationRangePickerOpen(pinnedPaginationRangePicker, false);
            }
            pinnedPaginationRangePicker = shouldPin ? picker : null;
            setPaginationRangePickerOpen(picker, shouldPin || picker.matches(":hover"));
        });
        trigger?.addEventListener("keydown", (event) => {
            if (event.key !== "ArrowDown") return;
            event.preventDefault();
            pinnedPaginationRangePicker = picker;
            setPaginationRangePickerOpen(picker, true, { focusFirst: true });
        });
        menu?.addEventListener("keydown", (event) => {
            const options = Array.from(menu.querySelectorAll(".browser-pagination-range-option"));
            const currentIndex = options.indexOf(document.activeElement);
            let nextIndex = currentIndex;
            if (event.key === "ArrowDown" || event.key === "ArrowRight") {
                nextIndex = Math.min(options.length - 1, currentIndex + 1);
            } else if (event.key === "ArrowUp" || event.key === "ArrowLeft") {
                nextIndex = Math.max(0, currentIndex - 1);
            } else if (event.key === "Home") {
                nextIndex = 0;
            } else if (event.key === "End") {
                nextIndex = options.length - 1;
            } else {
                return;
            }
            event.preventDefault();
            options[nextIndex]?.focus();
        });
    });

    if (paginationRangePickers.length) {
        document.addEventListener("pointerdown", (event) => {
            if (pagination?.contains(event.target)) return;
            pinnedPaginationRangePicker = null;
            paginationRangePickers.forEach((picker) => setPaginationRangePickerOpen(picker, false));
        });
        document.addEventListener("keydown", (event) => {
            if (event.key !== "Escape") return;
            const openPicker = paginationRangePickers.find((picker) => picker.classList.contains("is-open"));
            if (!openPicker) return;
            event.preventDefault();
            pinnedPaginationRangePicker = null;
            setPaginationRangePickerOpen(openPicker, false);
            paginationRangeElements(openPicker).trigger?.focus();
        });
        window.addEventListener("resize", () => {
            paginationRangePickers.forEach(positionPaginationRangeMenu);
        }, { passive: true });
    }

    const viewerMedia = dialog.querySelector("[data-viewer-media]");
    const viewerTitle = dialog.querySelector("[data-viewer-title]");
    const viewerSource = dialog.querySelector("[data-viewer-source]");
    const viewerCreator = dialog.querySelector("[data-viewer-creator]");
    const viewerDate = dialog.querySelector("[data-viewer-date]");
    const viewerFilename = dialog.querySelector("[data-viewer-filename]");
    const viewerSize = dialog.querySelector("[data-viewer-size]");
    const viewerDescription = dialog.querySelector("[data-viewer-description]");
    const viewerSourceLink = dialog.querySelector("[data-viewer-source-link]");
    const viewerSourceLinkLabel = dialog.querySelector("[data-viewer-source-link-label]");
    const viewerSourceUrl = dialog.querySelector("[data-viewer-source-url]");
    const closeButtons = Array.from(dialog.querySelectorAll("[data-dialog-close]"));
    const mediaOpenButtons = Array.from(document.querySelectorAll("[data-media-open]"));
    const mediaDeleteButtons = Array.from(document.querySelectorAll("[data-media-delete]"));
    const mediaRestoreButtons = Array.from(document.querySelectorAll("[data-media-restore]"));
    let activeTrigger = null;
    let viewerVideo = null;
    let viewerMediaResizeHandler = null;

    function setText(element, value, fallback = "—") {
        if (element) element.textContent = value || fallback;
    }

    function clearViewerMedia() {
        if (viewerMediaResizeHandler) {
            window.removeEventListener("resize", viewerMediaResizeHandler);
            viewerMediaResizeHandler = null;
        }
        if (viewerVideo) {
            viewerVideo.pause();
            viewerVideo.removeAttribute("src");
            viewerVideo.load();
            viewerVideo = null;
        }
        dialog.querySelectorAll(".browser-video-navigation").forEach((navigation) => navigation.remove());
        if (viewerMedia) viewerMedia.replaceChildren();
        dialog.style.removeProperty("width");
        dialog.style.removeProperty("height");
    }

    function validExternalUrl(value) {
        if (typeof value !== "string" || !value) return "";
        try {
            const parsed = new URL(value, window.location.href);
            return parsed.protocol === "http:" || parsed.protocol === "https:" ? value : "";
        } catch (_error) {
            return "";
        }
    }

    function getAdjacentVideoItem(item, direction) {
        const videoItems = mediaItems.filter((mediaItem) => mediaItem?.media_kind === "video");
        const currentIndex = videoItems.findIndex((mediaItem) => mediaItem.id === item.id);
        if (currentIndex < 0) return null;
        return videoItems[currentIndex + direction] || null;
    }

    function createVideoNavigationButton(direction, item) {
        const isPrevious = direction < 0;
        const button = document.createElement("button");
        button.type = "button";
        button.className = "settings-round-icon-button browser-video-nav-button";
        button.setAttribute("aria-label", isPrevious ? "Previous video" : "Next video");
        button.title = isPrevious ? "Previous video" : "Next video";
        button.disabled = !getAdjacentVideoItem(item, direction);
        const icon = document.createElement("span");
        icon.className = `icon ${isPrevious ? "icon-page-prev" : "icon-page-next"}`;
        icon.setAttribute("aria-hidden", "true");
        button.appendChild(icon);
        button.addEventListener("click", () => {
            const nextItem = getAdjacentVideoItem(item, direction);
            if (nextItem) openMediaItem(nextItem, activeTrigger);
        });
        return button;
    }

    function getFrameInset(frameStyles, startProperty, endProperty) {
        return (parseFloat(frameStyles[startProperty] || "0") || 0)
            + (parseFloat(frameStyles[endProperty] || "0") || 0);
    }

    function resizeViewerFrame(frame, intrinsicWidth, intrinsicHeight, fallbackRatio) {
        if (!frame.isConnected) return;
        const ratio = intrinsicWidth > 0 && intrinsicHeight > 0
            ? intrinsicWidth / intrinsicHeight
            : fallbackRatio;
        const dialogStyles = window.getComputedStyle(dialog);
        const edgeInset = parseFloat(dialogStyles.getPropertyValue("--browser-media-viewer-edge-inset")) || 48;
        const frameStyles = window.getComputedStyle(frame);
        const controlSize = parseFloat(dialogStyles.getPropertyValue("--browser-media-viewer-control-size")) || 36;
        const controlGap = parseFloat(dialogStyles.getPropertyValue("--browser-media-viewer-control-gap")) || 14;
        const horizontalInset = getFrameInset(frameStyles, "paddingLeft", "paddingRight")
            + getFrameInset(frameStyles, "borderLeftWidth", "borderRightWidth");
        const verticalInset = getFrameInset(frameStyles, "paddingTop", "paddingBottom")
            + getFrameInset(frameStyles, "borderTopWidth", "borderBottomWidth");
        const horizontalControlAllowance = 2 * (controlSize + controlGap);
        const maxFrameWidth = Math.max(
            1,
            Math.min(1440, window.innerWidth - edgeInset - horizontalControlAllowance),
        );
        const maxFrameHeight = Math.max(1, Math.min(900, window.innerHeight - edgeInset));
        const maxMediaWidth = Math.max(1, maxFrameWidth - horizontalInset);
        const maxMediaHeight = Math.max(1, maxFrameHeight - verticalInset);
        let mediaWidth = maxMediaWidth;
        let mediaHeight = mediaWidth / ratio;
        if (mediaHeight > maxMediaHeight) {
            mediaHeight = maxMediaHeight;
            mediaWidth = mediaHeight * ratio;
        }
        dialog.style.width = `${Math.round(mediaWidth + horizontalInset)}px`;
        dialog.style.height = `${Math.round(mediaHeight + verticalInset)}px`;
    }

    function createMediaLoadingNotice(message) {
        const notice = document.createElement("p");
        notice.className = "browser-media-loading-notice";

        const spinner = document.createElement("span");
        spinner.className = "suggestion-loading-spinner";
        spinner.setAttribute("aria-hidden", "true");

        const copy = document.createElement("span");
        copy.textContent = message;
        notice.append(spinner, copy);
        return notice;
    }

    function createImagePlayer(item) {
        const player = document.createElement("div");
        player.className = "browser-media-frame browser-image-player";
        player.setAttribute("data-image-player", "true");

        const image = document.createElement("img");
        image.className = "browser-dialog-media-element browser-dialog-image-element";
        image.alt = item.alt_text || item.title || item.filename;
        image.decoding = "async";
        const loadingNotice = createMediaLoadingNotice("Loading image…");
        player.append(image, loadingNotice);

        const resizePlayer = () => resizeViewerFrame(
            player,
            image.naturalWidth,
            image.naturalHeight,
            1,
        );
        const finishLoading = () => {
            loadingNotice.hidden = true;
            resizePlayer();
        };
        image.addEventListener("load", finishLoading, { once: true });
        image.src = item.media_url;
        if (image.complete && image.naturalWidth > 0) finishLoading();
        resizePlayer();
        window.addEventListener("resize", resizePlayer, { passive: true });
        return { player, image, resizeHandler: resizePlayer };
    }

    function createVideoPlayer(item) {
        const player = document.createElement("div");
        player.className = "browser-media-frame browser-video-player";
        player.setAttribute("data-video-player", "true");

        const video = document.createElement("video");
        video.className = "browser-video-player-media";
        video.playsInline = true;
        video.setAttribute("playsinline", "");
        video.setAttribute("webkit-playsinline", "");
        video.preload = "metadata";
        video.controls = true;
        video.setAttribute("aria-label", "Video preview");
        const loadingNotice = createMediaLoadingNotice("Loading video…");

        const navigation = document.createElement("div");
        navigation.className = "browser-video-navigation";
        navigation.setAttribute("aria-label", "Video navigation");
        navigation.append(
            createVideoNavigationButton(-1, item),
            createVideoNavigationButton(1, item),
        );
        player.append(video, loadingNotice);

        const resizePlayer = () => resizeViewerFrame(
            player,
            video.videoWidth,
            video.videoHeight,
            16 / 9,
        );
        video.addEventListener("loadedmetadata", resizePlayer, { once: true });
        video.addEventListener("loadeddata", () => {
            loadingNotice.hidden = true;
        }, { once: true });
        video.src = item.media_url;
        resizePlayer();
        window.addEventListener("resize", resizePlayer, { passive: true });
        return { player, video, navigation, resizeHandler: resizePlayer };
    }

    function openDetails(trigger) {
        const card = trigger.closest("[data-media-id]");
        const item = mediaById.get(card?.dataset.mediaId);
        if (!item || !viewerMedia) return;
        openMediaItem(item, trigger);
    }

    function openMediaItem(item, trigger = activeTrigger) {
        if (!item || !viewerMedia) return;
        const isVideo = item.media_kind === "video";
        activeTrigger = trigger || activeTrigger;
        clearViewerMedia();
        dialog.classList.add("is-media-viewer");
        dialog.classList.toggle("is-image-lightbox", !isVideo);
        dialog.classList.toggle("is-video-player", isVideo);
        dialog.setAttribute("aria-label", isVideo ? "Video player" : "Image preview");
        const closeButton = dialog.querySelector("[data-dialog-close]");
        if (closeButton) closeButton.setAttribute("aria-label", isVideo ? "Close video player" : "Close image preview");
        setText(viewerTitle, item.title, item.filename);
        setText(viewerSource, item.source_label);
        setText(viewerCreator, item.creator || item.project_name);
        setText(viewerDate, item.captured_at_label);
        setText(viewerFilename, item.filename);
        setText(viewerSize, item.size_label || `${item.content_bytes || 0} bytes`);
        setText(viewerDescription, item.description || item.alt_text, "");
        if (viewerDescription) viewerDescription.hidden = !(item.description || item.alt_text);

        const externalUrl = validExternalUrl(item.source_url);
        const sourceLinkLabels = {
            x: "Open original post",
            chatgpt: "Open original session",
            grok: "Open original source",
        };
        if (viewerSourceLink) {
            viewerSourceLink.hidden = !externalUrl;
            if (externalUrl) {
                viewerSourceLink.href = externalUrl;
            } else {
                viewerSourceLink.removeAttribute("href");
            }
        }
        setText(viewerSourceLinkLabel, sourceLinkLabels[item.source] || "Open original source", "");
        setText(viewerSourceUrl, externalUrl, "");

        const mediaPlayer = isVideo ? createVideoPlayer(item) : createImagePlayer(item);
        viewerMediaResizeHandler = mediaPlayer.resizeHandler;
        if (isVideo) {
            viewerVideo = mediaPlayer.video;
            viewerVideo.addEventListener("error", () => {
                setText(viewerMedia, "Preview unavailable.");
            }, { once: true });
        } else {
            mediaPlayer.image.addEventListener("error", () => {
                setText(viewerMedia, "Preview unavailable.");
            }, { once: true });
        }
        viewerMedia.appendChild(mediaPlayer.player);
        if (isVideo && mediaPlayer.navigation) {
            dialog.appendChild(mediaPlayer.navigation);
        }

        if (!dialog.open && !dialog.hasAttribute("open")) {
            if (typeof dialog.showModal === "function") {
                dialog.showModal();
            } else {
                dialog.setAttribute("open", "");
            }
            if (closeButton) closeButton.focus();
        }
        dialog.classList.add("is-open");
        if (viewerMediaResizeHandler) {
            viewerMediaResizeHandler();
            window.requestAnimationFrame(viewerMediaResizeHandler);
        }
    }

    function closeDetails() {
        const wasOpen = dialog.open || dialog.hasAttribute("open") || dialog.classList.contains("is-open");
        clearViewerMedia();
        if (dialog.open && typeof dialog.close === "function") {
            dialog.close();
        } else {
            dialog.removeAttribute("open");
        }
        dialog.classList.remove("is-open");
        dialog.classList.remove("is-media-viewer", "is-image-lightbox", "is-video-player");
        dialog.removeAttribute("aria-label");
        const closeButton = dialog.querySelector("[data-dialog-close]");
        if (closeButton) closeButton.setAttribute("aria-label", "Close media details");
        if (wasOpen && activeTrigger && activeTrigger.isConnected) activeTrigger.focus();
        activeTrigger = null;
    }

    function setCardPreviewSource(card, item) {
        const preview = card.querySelector("[data-preview]");
        if (!preview) return;
        const source = item.preview_url || item.media_url;
        preview.dataset.mediaSrc = source || "";
        preview.removeAttribute("src");
        delete preview.dataset.previewLoaded;
        preview.hidden = false;
        setPreviewStatus(preview, "Loading preview");
        loadPreview(preview);
    }

    function setCardState(card, item) {
        const isDeleted = Boolean(item.is_deleted);
        card.dataset.deleted = String(isDeleted);
        card.classList.toggle("is-deleted", isDeleted);
        const deleteButton = card.querySelector("[data-media-delete]");
        const deletedBar = card.querySelector("[data-media-deleted-bar]");
        if (deleteButton) {
            deleteButton.hidden = isDeleted;
            deleteButton.disabled = false;
            deleteButton.setAttribute(
                "aria-label",
                `Delete locally and stop tracking ${item.title || item.filename}`,
            );
            deleteButton.title = "Delete locally and stop tracking";
        }
        if (deletedBar) {
            deletedBar.hidden = !isDeleted;
            const message = deletedBar.querySelector("[data-media-deleted-message]");
            if (message) {
                message.textContent = isDeleted ? "Deleted locally; future tracking stopped" : "";
            }
        }
        setCardPreviewSource(card, item);
    }

    function moveDeletedCardToListEnd(card, item) {
        if (!item?.is_deleted || !mediaGallery || !mediaGallery.contains(card)) return;
        mediaGallery.appendChild(card);
    }

    function updateMediaItems(item) {
        const index = mediaItems.findIndex((candidate) => candidate?.id === item.id);
        if (index >= 0) {
            mediaItems[index] = item;
        } else {
            mediaItems.push(item);
        }
        if (item.is_deleted) {
            mediaItems = mediaItems.filter((candidate) => candidate?.id !== item.id);
            mediaItems.push(item);
        }
    }

    async function updateMedia(card, action) {
        const item = mediaById.get(card.dataset.mediaId);
        if (!item) return;
        const button = action === "delete"
            ? card.querySelector("[data-media-delete]")
            : card.querySelector("[data-media-restore]");
        if (button) button.disabled = true;
        const wait = window.CacheWaitModal?.begin?.({
            title: action === "delete" ? "Deleting local cache entry" : "Restoring local cache entry",
            copy: action === "delete"
                ? "Removing this resource from local storage and stopping future tracking for it."
                : "Restoring this resource to local storage and resuming future tracking for it.",
            delay: 120,
        });
        try {
            const response = await fetch(`/api/browser/media/${encodeURIComponent(item.id)}/${action}`, {
                method: "POST",
                headers: { "Accept": "application/json" },
            });
            const payload = await response.json();
            if (!response.ok || !payload.item) {
                throw new Error(payload.error || "The cache action failed.");
            }
            mediaById.set(payload.item.id, payload.item);
            updateMediaItems(payload.item);
            setCardState(card, payload.item);
            moveDeletedCardToListEnd(card, payload.item);
        } catch (error) {
            if (button) button.disabled = false;
            window.alert(error instanceof Error ? error.message : "The cache action failed.");
        } finally {
            wait?.finish?.();
        }
    }

    mediaOpenButtons.forEach((button) => {
        button.addEventListener("click", () => openDetails(button));
    });
    mediaDeleteButtons.forEach((button) => {
        button.addEventListener("click", (event) => {
            event.stopPropagation();
            const card = button.closest("[data-media-id]");
            if (card) updateMedia(card, "delete");
        });
    });
    mediaRestoreButtons.forEach((button) => {
        button.addEventListener("click", (event) => {
            event.stopPropagation();
            const card = button.closest("[data-media-id]");
            if (card) updateMedia(card, "restore");
        });
    });
    closeButtons.forEach((button) => button.addEventListener("click", closeDetails));
    dialog.addEventListener("cancel", (event) => {
        event.preventDefault();
        closeDetails();
    });
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && (dialog.open || dialog.hasAttribute("open"))) {
            event.preventDefault();
            closeDetails();
        }
    });
    dialog.addEventListener("click", (event) => {
        if (event.target === dialog) closeDetails();
    });
})();
