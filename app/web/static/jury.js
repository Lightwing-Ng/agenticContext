/* Code version: v1.1.3-codex.1 */
(() => {
    "use strict";
    const root = document.querySelector("[data-jury-root]");
    if (!root) return;
    const find = (name) => root.querySelector("[data-jury-" + name + "]");
    const providerInputs = Array.from(root.querySelectorAll("[data-jury-provider]"));
    const modelPickers = Array.from(root.querySelectorAll("[data-jury-model-picker]")).map((picker) => ({
        provider: picker.dataset.juryModelPicker,
        picker,
        input: picker.querySelector("[data-jury-model-input]"),
        trigger: picker.querySelector("[data-jury-model-trigger]"),
        label: picker.querySelector("[data-jury-model-label]"),
        menu: picker.querySelector("[data-jury-model-menu]"),
        options: Array.from(picker.querySelectorAll("[data-jury-model-option]")),
    }));
    const browserOptions = Array.from(root.querySelectorAll("[data-jury-browser-option]"));
    const promptForm = document.getElementById("jury_prompt_form");
    const prompt = find("prompt");
    const submit = find("submit");
    const browserTrigger = find("browser-trigger");
    const browserMenu = find("browser-menu");
    const providerLabels = {chatgpt: "ChatGPT", grok: "Grok", gemini: "Gemini", claude: "Claude"};
    let browser = root.dataset.juryBrowser;
    let snapshot = {};
    let sessionId = "";
    let readyConfiguration = "";
    let checking = false;
    let busy = false;
    let checkGeneration = 0;
    let selectionGeneration = 0;
    let checkController;
    let pollTimer;
    let sessionsTimer;
    let lastTranscript = "";
    let lastSessions = "";
    let sessions = [];
    let disposed = false;
    let modelMenuPositionFrame = 0;
    let sidebarTransitioning = false;

    const selectedProviders = () => providerInputs.filter((input) => input.checked).map((input) => input.value);
    const selectedModels = () => Object.fromEntries(modelPickers
        .filter((picker) => selectedProviders().includes(picker.provider))
        .map((picker) => [picker.provider, picker.input.value]));
    const configurationKey = () => JSON.stringify([browser, selectedProviders(), selectedModels()]);
    const providerRecords = (payload) => Array.isArray(payload.providers) ? payload.providers
        : Object.entries(payload.providers || {}).map(([key, value]) => ({key, ...value}));
    const isRunning = () => snapshot.running === true;
    const isReady = () => selectedProviders().length >= 2 && readyConfiguration === configurationKey();
    const displayPhase = (value) => String(value || "ready").replaceAll("_", " ");

    function unavailableJurorMessage(records, selected) {
        const unavailable = records.filter((record) =>
            selected.includes(record.key) && record.ready !== true);
        if (!unavailable.length) return "";
        const descriptions = unavailable.map((record) => {
            const picker = modelPickers.find((item) => item.provider === record.key);
            const label = String(record.label || providerLabels[record.key] || record.key);
            const model = String(record.model || picker?.label.textContent || "").trim();
            const diagnostic = String(record.message || "Sign-in could not be verified")
                .replace(/\s+/g, " ").trim().replace(/[.!?]+$/, "")
                || "Sign-in could not be verified";
            return label + (model ? " (" + model + ")" : "") + " — " + diagnostic;
        });
        const one = descriptions.length === 1;
        return "Unavailable juror" + (one ? "" : "s") + ": " + descriptions.join("; ")
            + ". Sign in or deselect " + (one ? "this juror" : "these jurors")
            + ", then check accounts again; at least two must remain selected.";
    }

    async function requestJson(url, options = {}) {
        const response = await fetch(url, {
            credentials: "same-origin",
            cache: "no-store",
            ...options,
            headers: {"Accept": "application/json", "Content-Type": "application/json", ...(options.headers || {})},
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || payload.message || "The Jury request could not be completed.");
        return payload;
    }

    function setMessage(message, phase = "idle") {
        find("status-copy").textContent = message;
        find("status").dataset.status = phase;
        find("run-spinner").hidden = !isRunning();
    }

    function syncControls() {
        const locked = busy || Boolean(sessionId);
        providerInputs.forEach((input) => { input.disabled = locked; });
        modelPickers.forEach((picker) => {
            picker.input.disabled = locked;
            picker.trigger.disabled = locked;
            if (locked) closeModelMenu(picker);
        });
        browserTrigger.disabled = locked;
        find("max-rounds").disabled = locked;
        prompt.disabled = locked;
        find("check").disabled = checking || busy || isRunning() || selectedProviders().length < 2;
        find("new-session").disabled = busy || isRunning();
        submit.classList.toggle("is-stop", isRunning());
        submit.setAttribute("aria-label", isRunning() ? "Stop Jury" : "Start Jury");
        submit.title = isRunning() ? "Stop Jury" : "Start Jury";
        submit.disabled = busy || (!isRunning() && (Boolean(sessionId) || !isReady() || !prompt.value.trim()));
        find("composer-note").textContent = sessionId && !isRunning()
            ? "Choose New session for another question" : "One conversation per juror";
    }

    function renderAccountCheck(payload, configuration) {
        const records = providerRecords(payload);
        const selected = selectedProviders();
        const everySelectedReady = selected.length >= 2 && selected.every((key) =>
            records.some((record) => record.key === key && record.ready === true));
        const ready = payload.ready === true && everySelectedReady;
        readyConfiguration = ready ? configuration : "";
        find("ready-check").hidden = !ready;
        find("check-label").textContent = ready ? "All selected signed in" : "Not ready";
        find("check-message").textContent = ready ? (payload.message || "")
            : (unavailableJurorMessage(records, selected) || payload.message
                || "Every selected juror must be signed in. Deselect an unavailable juror to continue with at least two.");
    }

    async function checkAccounts() {
        const generation = ++checkGeneration;
        checkController?.abort();
        readyConfiguration = "";
        find("ready-check").hidden = true;
        if (selectedProviders().length < 2) {
            checking = false;
            find("check-spinner").hidden = true;
            find("check-label").textContent = "Select at least two jurors";
            find("check-message").textContent = "";
            syncControls();
            return;
        }
        const configuration = configurationKey();
        checkController = new AbortController();
        checking = true;
        find("check-spinner").hidden = false;
        find("check-label").textContent = "Checking selected accounts";
        find("check-message").textContent = "";
        syncControls();
        try {
            const payload = await requestJson("/api/jury/check", {
                method: "POST", signal: checkController.signal,
                body: JSON.stringify({
                    browser,
                    providers: selectedProviders(),
                    models: selectedModels(),
                }),
            });
            if (generation !== checkGeneration || configuration !== configurationKey() || disposed) return;
            renderAccountCheck(payload, configuration);
        } catch (error) {
            if (generation !== checkGeneration || error.name === "AbortError" || disposed) return;
            find("check-label").textContent = "Check unavailable";
            find("check-message").textContent = error.message;
        } finally {
            if (generation === checkGeneration && !disposed) {
                checking = false;
                find("check-spinner").hidden = true;
                syncControls();
            }
        }
    }

    function renderResponse(container, response, html) {
        // Rich HTML is rendered by the server's existing escaped Markdown boundary.
        // Raw provider text never enters an HTML sink.
        container.classList.toggle("jury-plain-response", !html);
        if (html) container.innerHTML = html;
        else container.textContent = response || "";
        if (typeof window.renderMathInElement === "function") {
            window.renderMathInElement(container, {
                throwOnError: false,
                delimiters: [
                    {left: "$$", right: "$$", display: true},
                    {left: "\\[", right: "\\]", display: true},
                    {left: "\\(", right: "\\)", display: false},
                ],
            });
        }
    }

    function conversationLink(value) {
        try {
            const url = new URL(value);
            return ["https:", "http:"].includes(url.protocol) ? url.href : "";
        } catch (_error) {
            return "";
        }
    }

    function renderTranscript() {
        const rounds = snapshot.rounds || [];
        const signature = JSON.stringify([sessionId, rounds, snapshot.response, snapshot.response_html, snapshot.consensus]);
        if (signature === lastTranscript) return;
        lastTranscript = signature;
        const roundList = find("rounds");
        const oldOpen = new Map(Array.from(roundList.children).map((detail) => [detail.dataset.round, detail.open]));
        const fragment = document.createDocumentFragment();
        rounds.forEach((round, index) => {
            const detail = document.createElement("details");
            detail.className = "ui-collapse jury-round";
            detail.dataset.round = String(round.round || index + 1);
            detail.open = oldOpen.has(detail.dataset.round) ? oldOpen.get(detail.dataset.round) : index === rounds.length - 1;
            const summary = document.createElement("summary");
            summary.textContent = "Round " + detail.dataset.round + (index ? " · Cross-review" : " · Independent checks");
            const body = document.createElement("div");
            body.className = "ui-collapse-body";
            (round.opinions || []).forEach((opinion) => {
                const article = document.createElement("article");
                article.className = "jury-opinion";
                article.dataset.provider = opinion.provider;
                const header = document.createElement("header");
                header.className = "jury-opinion-header";
                const heading = document.createElement("h4");
                heading.textContent = providerLabels[opinion.provider] || opinion.provider;
                header.appendChild(heading);
                if (opinion.verdict) {
                    const verdict = document.createElement("span");
                    verdict.textContent = displayPhase(opinion.verdict);
                    header.appendChild(verdict);
                }
                const url = conversationLink(opinion.conversation_url);
                if (url) {
                    const link = document.createElement("a");
                    link.href = url;
                    link.target = "_blank";
                    link.rel = "noopener noreferrer";
                    link.textContent = "Conversation";
                    link.setAttribute("aria-label", "Open " + heading.textContent + " conversation");
                    header.appendChild(link);
                }
                const content = document.createElement("div");
                content.className = "agent-response-answer-content";
                renderResponse(content, opinion.conclusion || opinion.response || opinion.message, opinion.response_html);
                article.append(header, content);
                if (opinion.evidence?.length) {
                    const evidence = document.createElement("ul");
                    evidence.className = "jury-evidence";
                    evidence.setAttribute("aria-label", heading.textContent + " evidence");
                    opinion.evidence.forEach((source) => {
                        const item = document.createElement("li");
                        const sourceUrl = conversationLink(source.url);
                        if (sourceUrl) {
                            const link = document.createElement("a");
                            link.href = sourceUrl;
                            link.target = "_blank";
                            link.rel = "noopener noreferrer";
                            link.textContent = new URL(sourceUrl).hostname;
                            item.append(link, document.createTextNode(" — " + (source.supports || "")));
                        } else {
                            item.textContent = source.supports || "Source URL unavailable";
                        }
                        evidence.appendChild(item);
                    });
                    article.appendChild(evidence);
                }
                if (opinion.unresolved?.length) {
                    const unresolved = document.createElement("div");
                    unresolved.className = "jury-unresolved";
                    const label = document.createElement("p");
                    label.textContent = "Remaining objections";
                    const list = document.createElement("ul");
                    opinion.unresolved.forEach((objection) => {
                        const item = document.createElement("li");
                        item.textContent = objection;
                        list.appendChild(item);
                    });
                    unresolved.append(label, list);
                    article.appendChild(unresolved);
                }
                body.appendChild(article);
            });
            detail.append(summary, body);
            fragment.appendChild(detail);
        });
        roundList.replaceChildren(fragment);
        find("conclusion").hidden = !snapshot.response;
        find("conclusion-title").textContent = snapshot.consensus === true ? "Shared conclusion" : "Review outcome";
        renderResponse(find("conclusion-content"), snapshot.response, snapshot.response_html);
        find("copy").hidden = !rounds.length && !snapshot.response;
    }

    function updateLocation() {
        const url = new URL(window.location.href);
        url.pathname = "/jury/" + browser;
        if (sessionId) url.searchParams.set("session_id", sessionId);
        else url.searchParams.delete("session_id");
        window.history.replaceState(null, "", url);
    }

    function applySnapshot(payload) {
        snapshot = payload;
        sessionId = payload.session_id || sessionId;
        if (payload.browser && payload.browser !== browser) setBrowser(payload.browser);
        const records = providerRecords(payload);
        if (records.length) {
            providerInputs.forEach((input) => { input.checked = records.some((record) => record.key === input.value); });
            records.forEach((record) => {
                if (record.model_selection) setModel(record.key, record.model_selection);
            });
            renderAccountCheck({
                ready: records.every((record) => record.ready === true), providers: records,
                message: payload.phase === "checking" ? "Verifying selected accounts before sending." : "",
            }, configurationKey());
        }
        if (payload.max_rounds) find("max-rounds").value = String(payload.max_rounds);
        find("introduction").hidden = Boolean(sessionId);
        find("question-header").hidden = !payload.question;
        find("question").textContent = payload.question || "";
        setMessage(payload.message || displayPhase(payload.phase), isRunning() ? "running" : (payload.phase || "idle"));
        updateLocation();
        renderTranscript();
        syncControls();
        renderSessions();
        if (isRunning()) schedulePoll();
    }

    function schedulePoll() {
        window.clearTimeout(pollTimer);
        if (!disposed) pollTimer = window.setTimeout(pollStatus, 1200);
    }

    async function pollStatus() {
        if (!sessionId || disposed) return;
        const requestedId = sessionId;
        const generation = selectionGeneration;
        try {
            const payload = await requestJson("/api/jury/status?session_id=" + encodeURIComponent(requestedId));
            if (requestedId !== sessionId || generation !== selectionGeneration || disposed) return;
            applySnapshot(payload);
        } catch (error) {
            if (requestedId !== sessionId || generation !== selectionGeneration || disposed) return;
            setMessage("Connection interrupted. " + error.message, "interrupted");
            if (isRunning()) schedulePoll();
        }
    }

    function renderSessions() {
        const signature = JSON.stringify([sessions, sessionId, isRunning()]);
        if (signature === lastSessions) return;
        lastSessions = signature;
        const list = find("session-list");
        const fragment = document.createDocumentFragment();
        sessions.forEach((session) => {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "secondary-button agent-execution-session";
            button.setAttribute("aria-pressed", String(session.session_id === sessionId));
            button.disabled = isRunning() && session.session_id !== sessionId;
            const title = document.createElement("span");
            title.className = "agent-execution-session-title";
            title.textContent = session.title || session.question || "Jury session";
            button.title = title.textContent;
            const phase = document.createElement("span");
            phase.className = "agent-execution-session-state";
            phase.textContent = displayPhase(session.phase);
            button.append(title, phase);
            button.addEventListener("click", () => selectSession(session.session_id));
            fragment.appendChild(button);
        });
        if (!sessions.length) {
            const empty = document.createElement("p");
            empty.className = "agent-recent-sessions-empty";
            empty.textContent = "No Jury sessions yet.";
            fragment.appendChild(empty);
        }
        list.replaceChildren(fragment);
        const activeCount = sessions.filter((session) => session.running).length;
        find("active-count").textContent = activeCount.toLocaleString("en-US");
        find("active-count").hidden = !activeCount;
    }

    async function loadSessions() {
        window.clearTimeout(sessionsTimer);
        const requestedBrowser = browser;
        try {
            const payload = await requestJson("/api/jury/sessions?browser=" + encodeURIComponent(browser));
            if (requestedBrowser !== browser || disposed) return;
            sessions = payload.sessions || [];
            renderSessions();
        } catch (error) {
            if (!sessionId && requestedBrowser === browser && !disposed) setMessage(error.message, "failed");
        } finally {
            if (!disposed) sessionsTimer = window.setTimeout(loadSessions, 5000);
        }
    }

    async function selectSession(id) {
        if (busy || (isRunning() && id !== sessionId)) return;
        checkController?.abort();
        checkGeneration += 1;
        checking = false;
        find("check-spinner").hidden = true;
        window.clearTimeout(pollTimer);
        selectionGeneration += 1;
        sessionId = id;
        snapshot = {};
        prompt.value = "";
        syncControls();
        await pollStatus();
    }

    function newSession() {
        if (busy || isRunning()) return;
        selectionGeneration += 1;
        sessionId = "";
        snapshot = {};
        window.clearTimeout(pollTimer);
        prompt.value = "";
        find("introduction").hidden = false;
        find("question-header").hidden = true;
        setMessage("Independent checks, shared review, one conversation per juror.");
        updateLocation();
        renderTranscript();
        renderSessions();
        syncControls();
        prompt.focus();
        checkAccounts();
    }

    function setModel(provider, value) {
        const picker = modelPickers.find((item) => item.provider === provider);
        const selected = picker?.options.find((option) => option.dataset.juryModelOption === value);
        if (!picker || !selected) return false;
        picker.input.value = value;
        picker.label.textContent = selected.dataset.label;
        picker.trigger.setAttribute(
            "aria-label",
            (providerLabels[provider] || provider) + " model tier: " + selected.dataset.label,
        );
        picker.options.forEach((option) => {
            option.setAttribute("aria-selected", String(option === selected));
            option.classList.toggle("is-selected", option === selected);
            option.classList.toggle("is-active", option === selected);
        });
        return true;
    }

    function modelMenuOverlay() {
        let overlay = document.querySelector("[data-shared-select-overlay]");
        if (overlay instanceof HTMLElement) return overlay;
        overlay = document.createElement("div");
        overlay.className = "shared-select-overlay";
        overlay.dataset.sharedSelectOverlay = "";
        document.body.appendChild(overlay);
        return overlay;
    }

    function resetModelMenuPosition(menu) {
        ["position", "left", "top", "right", "bottom", "width", "minWidth", "maxWidth", "maxHeight", "overflowY"]
            .forEach((property) => { menu.style[property] = ""; });
    }

    function restoreModelMenu(picker) {
        if (picker.menu.parentElement?.matches("[data-shared-select-overlay]")) {
            picker.picker.appendChild(picker.menu);
        }
        resetModelMenuPosition(picker.menu);
    }

    function positionModelMenu(picker) {
        if (picker.menu.hidden) return;
        const triggerRect = picker.trigger.getBoundingClientRect();
        const providerList = picker.picker.closest(".jury-provider-list");
        const sidebar = picker.picker.closest(".sidebar");
        if (!(providerList instanceof HTMLElement) || !(sidebar instanceof HTMLElement)) return;
        const contentRect = providerList.getBoundingClientRect();
        const sidebarRect = sidebar.getBoundingClientRect();
        const viewport = window.visualViewport;
        const viewportTop = Number(viewport?.offsetTop) || 0;
        const viewportHeight = Number(viewport?.height) || window.innerHeight;
        const viewportBottom = viewportTop + viewportHeight;
        const gap = 4;
        const edge = 10;
        const contentLeft = Math.max(sidebarRect.left + edge, contentRect.left);
        const contentRight = Math.min(sidebarRect.right - edge, contentRect.right);
        const maxWidth = Math.max(0, Math.min(240, contentRight - contentLeft));

        const overlay = modelMenuOverlay();
        if (picker.menu.parentElement !== overlay) overlay.appendChild(picker.menu);
        Object.assign(picker.menu.style, {
            position: "fixed",
            left: `${Math.round(contentLeft)}px`,
            top: `${Math.round(triggerRect.bottom + gap)}px`,
            right: "auto",
            bottom: "auto",
            width: "max-content",
            minWidth: `${Math.round(Math.min(triggerRect.width, maxWidth))}px`,
            maxWidth: `${Math.round(maxWidth)}px`,
            maxHeight: "none",
            overflowY: "auto",
        });
        const naturalRect = picker.menu.getBoundingClientRect();
        const menuStyle = getComputedStyle(picker.menu);
        const menuPadding = (Number.parseFloat(menuStyle.paddingLeft) || 0)
            + (Number.parseFloat(menuStyle.paddingRight) || 0);
        const optionWidth = picker.options.reduce((width, option) => {
            const optionStyle = getComputedStyle(option);
            const text = option.querySelector(".trade-strategy-dropdown-text");
            const check = option.querySelector(".trade-strategy-dropdown-check");
            const textRange = document.createRange();
            if (text) textRange.selectNodeContents(text);
            const contentWidth = (text ? textRange.getBoundingClientRect().width : 0)
                + (check?.getBoundingClientRect().width || 0);
            return Math.max(
                width,
                contentWidth
                    + (Number.parseFloat(optionStyle.columnGap) || 0)
                    + (Number.parseFloat(optionStyle.paddingLeft) || 0)
                    + (Number.parseFloat(optionStyle.paddingRight) || 0),
            );
        }, 0);
        const menuWidth = Math.min(
            maxWidth,
            Math.max(triggerRect.width, naturalRect.width, optionWidth + menuPadding) + 12,
        );
        const menuHeight = Math.max(picker.menu.scrollHeight, naturalRect.height);
        const spaceBelow = Math.max(0, viewportBottom - edge - triggerRect.bottom - gap);
        const spaceAbove = Math.max(0, triggerRect.top - viewportTop - edge - gap);
        const opensAbove = menuHeight > spaceBelow && spaceAbove > spaceBelow;
        const availableHeight = Math.max(0, opensAbove ? spaceAbove : spaceBelow);
        const visibleHeight = Math.min(menuHeight, availableHeight);
        const menuLeft = Math.min(
            Math.max(contentLeft, triggerRect.right - menuWidth),
            contentRight - menuWidth,
        );
        const menuTop = opensAbove
            ? Math.max(viewportTop + edge, triggerRect.top - gap - visibleHeight)
            : Math.min(viewportBottom - edge - visibleHeight, triggerRect.bottom + gap);
        Object.assign(picker.menu.style, {
            left: `${Math.round(menuLeft)}px`,
            top: `${Math.round(menuTop)}px`,
            width: `${Math.round(menuWidth)}px`,
            maxHeight: `${Math.round(availableHeight)}px`,
        });
    }

    function scheduleModelMenuPosition() {
        if (modelMenuPositionFrame) return;
        modelMenuPositionFrame = window.requestAnimationFrame(() => {
            modelMenuPositionFrame = 0;
            modelPickers.forEach(positionModelMenu);
            if (sidebarTransitioning && modelPickers.some((picker) => !picker.menu.hidden)) {
                scheduleModelMenuPosition();
            }
        });
    }

    function closeModelMenu(picker) {
        picker.menu.hidden = true;
        picker.trigger.setAttribute("aria-expanded", "false");
        picker.picker.classList.remove("is-open");
        restoreModelMenu(picker);
    }

    function openModelMenu(picker) {
        if (picker.trigger.disabled) return;
        modelPickers.forEach((item) => {
            if (item !== picker) closeModelMenu(item);
        });
        picker.menu.hidden = false;
        picker.trigger.setAttribute("aria-expanded", "true");
        picker.picker.classList.add("is-open");
        positionModelMenu(picker);
    }

    function closeBrowserMenu() {
        browserMenu.hidden = true;
        browserTrigger.setAttribute("aria-expanded", "false");
        find("browser-picker").classList.remove("is-agent-combobox-open");
    }
    function openBrowserMenu() {
        if (browserTrigger.disabled) return;
        browserMenu.hidden = false;
        browserTrigger.setAttribute("aria-expanded", "true");
        find("browser-picker").classList.add("is-agent-combobox-open");
    }
    function setBrowser(value) {
        const selected = browserOptions.find((option) => option.dataset.juryBrowserOption === value);
        if (!selected) return;
        browser = value;
        root.dataset.juryBrowser = value;
        find("browser-input").value = value;
        find("browser-label").textContent = selected.dataset.label;
        find("browser-icon").src = selected.dataset.icon;
        browserTrigger.setAttribute("aria-label", "Browser: " + selected.dataset.label);
        browserOptions.forEach((option) => {
            option.setAttribute("aria-selected", String(option === selected));
            option.classList.toggle("is-selected", option === selected);
        });
    }
    const browserController = window.SHARED_SELECT.createController({
        getTrigger: () => browserTrigger, getMenu: () => browserMenu, getOptions: () => browserOptions,
        open: openBrowserMenu, close: closeBrowserMenu,
    });
    browserController.bindKeyboard();
    browserTrigger.addEventListener("click", () => {
        if (browserMenu.hidden) openBrowserMenu();
        else closeBrowserMenu();
    });
    browserOptions.forEach((option) => option.addEventListener("click", () => {
        if (browserTrigger.disabled) return;
        setBrowser(option.dataset.juryBrowserOption);
        closeBrowserMenu();
        browserController.highlightSelected();
        updateLocation();
        sessions = [];
        renderSessions();
        loadSessions();
        checkAccounts();
    }));
    modelPickers.forEach((picker) => {
        const controller = window.SHARED_SELECT.createController({
            getTrigger: () => picker.trigger,
            getMenu: () => picker.menu,
            getOptions: () => picker.options,
            open: () => openModelMenu(picker),
            close: () => closeModelMenu(picker),
        });
        controller.bindKeyboard();
        picker.trigger.addEventListener("click", () => {
            if (picker.menu.hidden) openModelMenu(picker);
            else closeModelMenu(picker);
        });
        picker.options.forEach((option) => option.addEventListener("click", () => {
            if (picker.trigger.disabled || !setModel(picker.provider, option.dataset.juryModelOption)) return;
            closeModelMenu(picker);
            controller.highlightSelected();
            checkAccounts();
        }));
    });
    document.addEventListener("click", (event) => {
        if (!find("browser-picker").contains(event.target)) closeBrowserMenu();
        modelPickers.forEach((picker) => {
            if (!picker.picker.contains(event.target) && !picker.menu.contains(event.target)) closeModelMenu(picker);
        });
    });
    window.addEventListener("resize", scheduleModelMenuPosition);
    const jurySidebar = document.getElementById("jury_sidebar");
    jurySidebar?.addEventListener("scroll", scheduleModelMenuPosition, {passive: true});
    jurySidebar?.addEventListener("transitionrun", () => {
        sidebarTransitioning = true;
        scheduleModelMenuPosition();
    });
    ["transitionend", "transitioncancel"].forEach((eventName) => {
        jurySidebar?.addEventListener(eventName, () => {
            sidebarTransitioning = false;
            scheduleModelMenuPosition();
        });
    });
    window.visualViewport?.addEventListener("resize", scheduleModelMenuPosition);
    window.visualViewport?.addEventListener("scroll", scheduleModelMenuPosition);
    document.fonts?.ready.then(scheduleModelMenuPosition);
    providerInputs.forEach((input) => input.addEventListener("change", checkAccounts));
    find("check").addEventListener("click", checkAccounts);
    find("new-session").addEventListener("click", newSession);
    prompt.addEventListener("input", syncControls);
    prompt.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
            event.preventDefault();
            if (!submit.disabled) promptForm.requestSubmit();
        }
    });
    promptForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (submit.disabled) return;
        busy = true;
        const stopping = isRunning();
        syncControls();
        try {
            const payload = await requestJson(stopping ? "/api/jury/stop" : "/api/jury/start", {
                method: "POST",
                body: JSON.stringify(stopping ? {session_id: sessionId} : {
                    browser, providers: selectedProviders(), question: prompt.value.trim(),
                    max_rounds: Number(find("max-rounds").value), models: selectedModels(),
                }),
            });
            applySnapshot(payload);
            if (!stopping) prompt.value = "";
            loadSessions();
        } catch (error) {
            setMessage(error.message, "failed");
        } finally {
            busy = false;
            syncControls();
        }
    });
    find("copy").addEventListener("click", async () => {
        const parts = [snapshot.question || ""];
        if (snapshot.response) parts.push(snapshot.response);
        (snapshot.rounds || []).forEach((round) => {
            parts.push("Round " + round.round);
            (round.opinions || []).forEach((opinion) =>
                parts.push((providerLabels[opinion.provider] || opinion.provider) + "\n" + (opinion.response || "")));
        });
        try {
            await navigator.clipboard.writeText(parts.join("\n\n"));
            find("copy-feedback").textContent = "Copied";
        } catch (_error) {
            find("copy-feedback").textContent = "Copy failed";
        }
        window.setTimeout(() => { find("copy-feedback").textContent = ""; }, 2000);
    });
    const composer = prompt.closest(".agent-prompt-form");
    const resizeObserver = new ResizeObserver(() => {
        root.style.setProperty("--agent-composer-height", composer.getBoundingClientRect().height + "px");
    });
    resizeObserver.observe(composer);
    window.addEventListener("pagehide", () => {
        disposed = true;
        checkController?.abort();
        window.clearTimeout(pollTimer);
        window.clearTimeout(sessionsTimer);
        window.cancelAnimationFrame(modelMenuPositionFrame);
        modelMenuPositionFrame = 0;
        resizeObserver.disconnect();
    });
    window.addEventListener("pageshow", (event) => {
        if (!event.persisted) return;
        disposed = false;
        resizeObserver.observe(composer);
        loadSessions();
        if (sessionId) pollStatus();
        else checkAccounts();
    });
    const initialSession = new URL(window.location.href).searchParams.get("session_id");
    loadSessions();
    if (initialSession) selectSession(initialSession);
    else checkAccounts();
})();
