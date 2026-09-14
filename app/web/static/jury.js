/* Code version: v1.0.1-codex.1 */
(() => {
    "use strict";
    const root = document.querySelector("[data-jury-root]");
    if (!root) return;
    const find = (name) => root.querySelector("[data-jury-" + name + "]");
    const providerInputs = Array.from(root.querySelectorAll("[data-jury-provider]"));
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

    const selectedProviders = () => providerInputs.filter((input) => input.checked).map((input) => input.value);
    const configurationKey = () => JSON.stringify([browser, selectedProviders()]);
    const providerRecords = (payload) => Array.isArray(payload.providers) ? payload.providers
        : Object.entries(payload.providers || {}).map(([key, value]) => ({key, ...value}));
    const isRunning = () => snapshot.running === true;
    const isReady = () => selectedProviders().length >= 2 && readyConfiguration === configurationKey();
    const displayPhase = (value) => String(value || "ready").replaceAll("_", " ");

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
        find("check-message").textContent = payload.message || (ready ? ""
            : "Every selected juror must be signed in. Deselect an unavailable juror to continue with at least two.");
        providerInputs.forEach((input) => {
            const status = root.querySelector('[data-jury-provider-readiness="' + input.value + '"]');
            const record = records.find((entry) => entry.key === input.value);
            status.textContent = input.checked && record ? (record.ready ? "Ready" : "Unavailable") : "";
            status.title = input.checked && record ? (record.message || "") : "";
            if (input.checked && record) status.dataset.ready = String(record.ready === true);
            else delete status.dataset.ready;
        });
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
                body: JSON.stringify({browser, providers: selectedProviders()}),
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
    document.addEventListener("click", (event) => {
        if (!find("browser-picker").contains(event.target)) closeBrowserMenu();
    });
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
                    max_rounds: Number(find("max-rounds").value),
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
