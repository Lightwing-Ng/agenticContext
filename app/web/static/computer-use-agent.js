/* Code version: v3.43.0-codex.1 */

(() => {
    const BOOTSTRAPPED_SOURCE_PLATFORMS = new Set(["chatgpt", "grok", "claude"]);
    const AGENT_SESSION_SELECTION_CACHE_VERSION = 1;
    const AGENT_SESSION_SELECTION_CACHE_PREFIX = "cachelikes:agent-session-selection";
    const MAX_AGENT_SESSION_CACHE_VALUE_LENGTH = 2048;
    const AGENT_SESSION_MODES = new Set(["new", "project"]);
    const runtimeForm = document.getElementById("agent_runtime_form");
    const promptForm = document.getElementById("agent_prompt_form");
    if (!runtimeForm || !promptForm) return;

    // Retire the duplicate selector in already-cached server templates too.
    runtimeForm.querySelector("[data-agent-project-session-field]")?.remove();

    // Reserve the measured composer height while answers scroll behind its glass.
    const composerSizeObserver = new ResizeObserver(() => {
        promptForm.parentElement.style.setProperty(
            "--agent-composer-height", `${promptForm.getBoundingClientRect().height}px`,
        );
    });
    composerSizeObserver.observe(promptForm);

    const elements = {
        agentPage: document.querySelector("[data-agent-route-prefix]"),
        statusMessage: document.getElementById("agent_response_status"),
        statusMessageCopy: document.querySelector("[data-agent-response-status-copy]"),
        statusDot: document.querySelector("[data-agent-response-status-dot]"),
        statusSpinner: document.querySelector("[data-agent-response-status-spinner]"),
        errorRecord: document.getElementById("agent_error_record"),
        errorRecordContent: document.querySelector("[data-agent-error-record-content]"),
        doctorPanel: document.getElementById("agent_doctor_panel"),
        doctorStatus: document.getElementById("agent_doctor_status"),
        doctorSummary: document.getElementById("agent_doctor_summary"),
        doctorChecks: document.getElementById("agent_doctor_checks"),
        doctorEvents: document.getElementById("agent_doctor_events"),
        doctorActions: document.getElementById("agent_doctor_actions"),
        responseOutput: document.getElementById("agent_response_output"),
        responseQuestion: document.querySelector("[data-agent-response-question]"),
        responseAnswer: document.querySelector("[data-agent-response-answer]"),
        responseAnswerContent: document.querySelector("[data-agent-response-answer-content]"),
        responseCopy: document.querySelector("[data-agent-response-copy]"),
        responseCopyFeedback: document.querySelector("[data-agent-response-copy-feedback]"),
        responsePagination: document.querySelector("[data-agent-response-pagination]"),
        conversationLink: document.getElementById("agent_conversation_link"),
        conversationLinkLabel: document.querySelector("[data-agent-conversation-link-label]"),
        ask: document.getElementById("agent_ask_button"),
        resume: document.getElementById("agent_resume_button"),
        projectPath: document.querySelector("[data-agent-project-path]"),
        projectChoose: document.getElementById("agent_project_path_choose"),
        projectName: document.querySelector("[data-agent-project-name]"),
        workspacePath: promptForm.querySelector('input[name="workspace_path"]'),
        promptOs: promptForm.querySelector("[data-agent-prompt-os]"),
        promptPlatform: promptForm.querySelector("[data-agent-prompt-platform]"),
        promptBrowser: promptForm.querySelector("[data-agent-prompt-browser]"),
        modelInput: promptForm.querySelector("[data-agent-model-input]"),
        effortField: promptForm.querySelector("[data-agent-effort-field]"),
        effortCombobox: promptForm.querySelector(".agent-effort-combobox"),
        effortInput: promptForm.querySelector("[data-agent-effort-input]"),
        promptSessionMode: promptForm.querySelector("[data-agent-prompt-session-mode]"),
        promptConversationUrl: promptForm.querySelector("[data-agent-prompt-conversation-url]"),
        promptProjectUrl: promptForm.querySelector("[data-agent-prompt-project-url]"),
        promptSessionTitle: promptForm.querySelector("[data-agent-prompt-session-title]"),
        promptInput: promptForm.querySelector("[data-agent-prompt-input]"),
        promptOverflowToggle: promptForm.querySelector("[data-agent-composer-overflow-toggle]"),
        activityPanel: document.getElementById("agent_activity_panel"),
        activityCount: document.getElementById("agent_activity_count"),
        activityList: document.getElementById("agent_activity_list"),
        browserSession: document.querySelector("[data-agent-browser-session]"),
        terminalExecutionStatus: document.querySelector("[data-agent-terminal-execution-status]"),
        terminalExecutionCopy: document.querySelector("[data-agent-terminal-execution-copy]"),
        terminalExecutionCheckmark: document.querySelector("[data-agent-terminal-execution-checkmark]"),
        computeJob: document.querySelector("[data-agent-compute-job]"),
        computeJobStatus: document.querySelector("[data-agent-compute-job-status]"),
        computeJobState: document.querySelector("[data-agent-compute-job-state]"),
        computeJobId: document.querySelector("[data-agent-compute-job-id]"),
        computeJobProgress: document.querySelector("[data-agent-compute-job-progress]"),
        computeJobHelp: document.querySelector("[data-agent-compute-job-help]"),
        computeJobStop: document.querySelector("[data-agent-compute-job-stop]"),
        platformCombobox: document.querySelector(".agent-platform-combobox"),
        modelCombobox: document.querySelector(".agent-model-combobox"),
        sessionSource: document.querySelector("[data-agent-session-source]"),
        sessionMode: document.querySelector("[data-agent-session-mode]"),
        sessionModeCombobox: document.querySelector(".agent-session-mode-combobox"),
        projectField: document.querySelector("[data-agent-project-field]"),
        projectCombobox: document.querySelector('[data-agent-session-list="projects"]'),
        projectUrl: document.querySelector("[data-agent-project-url]"),
        newSessionButton: document.querySelector("[data-agent-new-session]"),
        comboboxTriggers: Array.from(document.querySelectorAll("[data-agent-combobox-trigger]")),
    };

    let lastPayload = {};
    let lastBrowserStatus = null;
    let preferredModel = elements.modelInput?.value || "";
    let browserStatusState = "cleared";
    let browserStatusController = null;
    let preferenceTimer = null;
    let responseStatusTimer = null;
    let activitySignature = "";
    let activityWasRunning = false;
    let activityRunIdentity = "";
    let activityCloseAnimation = null;
    const activityCurrent = document.createElement("ol");
    activityCurrent.className = "agent-activity-list agent-activity-current";
    activityCurrent.id = "agent_activity_current";
    activityCurrent.hidden = true;
    const activitySummary = elements.activityPanel?.querySelector("summary");
    const responseToolbar = elements.statusMessage?.closest(".agent-response-toolbar");
    // Align warm templates with the unified status disclosure without a server restart.
    if (responseToolbar && activitySummary && elements.activityPanel) {
        if (elements.activityCount) elements.activityCount.hidden = true;
        activitySummary.replaceChildren(...[elements.statusMessage, elements.activityCount].filter(Boolean));
        activitySummary.setAttribute("aria-label", "Toggle execution activity");
        responseToolbar.prepend(elements.activityPanel);
        elements.activityPanel.hidden = false;
    }
    elements.activityPanel?.after(activityCurrent);

    function syncActivityCurrent() {
        const panel = elements.activityPanel;
        if (!panel) return;
        activityCurrent.hidden = panel.hidden || panel.open || !activityCurrent.childElementCount;
        activitySummary?.setAttribute("aria-expanded", String(panel.open));
    }

    let activityTargetOpen = null;
    activitySummary?.addEventListener("click", (event) => {
        event.preventDefault();
        const panel = elements.activityPanel;
        const startHeight = panel.getBoundingClientRect().height;
        const open = !(activityTargetOpen ?? panel.open);
        activityCloseAnimation?.cancel();
        activityCloseAnimation = null;
        activityTargetOpen = open;
        const finish = () => {
            panel.open = open;
            panel.style.overflow = "";
            activityCloseAnimation = null;
            activityTargetOpen = null;
            syncActivityCurrent();
        };
        panel.open = open;
        syncActivityCurrent();
        if (open) elements.activityList.scrollTop = elements.activityList.scrollHeight;
        const endHeight = panel.getBoundingClientRect().height;
        if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
            finish();
            return;
        }
        // Keep the disclosure rendered while its height moves following content.
        panel.open = true;
        panel.style.overflow = "hidden";
        const style = getComputedStyle(panel);
        activityCloseAnimation = panel.animate([
            {height: `${startHeight}px`},
            {height: `${endHeight}px`},
        ], {
            duration: parseFloat(style.getPropertyValue("--motion-duration-emphasized")) || 420,
            easing: style.getPropertyValue("--motion-emphasized").trim(),
        });
        activityCloseAnimation.onfinish = finish;
    });
    elements.activityPanel?.addEventListener("toggle", syncActivityCurrent);
    let sourceBrowser = "";
    let sourcePlatform = "";
    let sourcesLoaded = false;
    let sourcesLoading = false;
    let automaticSourcesSuppressedAfterCompletion = false;
    let sourceRequestId = 0;
    let catalogState = "idle";
    let catalogError = "";
    let catalogAbort = null;
    let appliedBootstrapSignature = "";
    const CATALOG_TIMEOUT_MS = 15000;
    const HIGHEST_CHATGPT_EFFORT = "highest_available";
    const CHATGPT_EFFORT_CATALOG_FRESHNESS = new Map([
        ["live_browser", new Set(["miss", "refreshed"])],
        ["server_cache", new Set(["hit"])],
        ["stale_cache", new Set(["stale"])],
        ["client_cache", new Set(["miss", "refreshed", "hit", "stale"])],
    ]);
    let projectSessionRequestId = 0;
    let agentSources = {recent_sessions: [], projects: []};
    let projectSessions = [];
    let projectSessionsLoading = false;
    let verifiedProjectSessionsKey = "";
    let selectedRemoteConversationUrl = "";
    let selectedProjectConversationUrl = "new";
    let recentSelectionRequestId = 0;
    let sessionTitleOverride = "";
    let sessionSelectionRestoredKey = "";
    let boundAgentSessionSignature = "";
    let lastRenderedAgentRunning = elements.agentPage?.dataset.agentRunning === "true";
    let lastRenderedAgentRunIdentity = elements.agentPage?.dataset.agentRunId || "";
    let lastRenderedAgentRunRevision = normalizeAgentRunRevision(
        elements.agentPage?.dataset.agentRunRevision,
    );
    let lastRenderedAgentStartedAt = elements.agentPage?.dataset.agentStartedAt || "";
    let promptHasLocalDraft = false;
    let promptSubmissionPending = false;
    let effortSelectionTouched = false;
    let pendingSubmissionPreviousRunIdentity = "";
    let pendingSubmissionPreviousRunRevision = 0;
    let pendingSubmissionPreviousRunStartedAt = "";
    let responseHistory = [];
    let responseHistoryPage = 1;
    let responseHistorySignature = "";
    let renderedResponsePageKey = "";
    let renderedResponseContentSignature = "";
    let responseCopyValue = "";
    let responseCopyRevision = 0;
    let responseCopyFeedbackTimer = 0;
    let remoteSessionHistory = [];
    let remoteSessionHistoryPlatform = "";
    let remoteSessionHistoryUrl = "";
    let remoteSessionHistoryBrowser = "";
    let remoteSessionHistoryLoading = false;
    let remoteSessionHistoryError = "";
    let remoteSessionHistoryRequestId = 0;
    let doctorPayload = null;
    let doctorLoading = false;
    let doctorRequestId = 0;
    let agentPaginationRangeCloseTimer = 0;
    let agentPaginationRangeEventsBound = false;
    let agentPaginationRangePinnedPicker = null;
    let agentPaginationRangeFocusRestore = null;
    const paginationMotion = window.CACHELIKES_PAGINATION_MOTION;

    function executionStorageKey() {
        return `cachelikes:agent-execution-session:${selectedBrowser()}:${selectedPlatform()}`;
    }

    function rememberedExecutionSession() {
        try { return window.sessionStorage.getItem(executionStorageKey()) || ""; } catch (_error) { return ""; }
    }

    let executionScope = executionStorageKey();
    let executionSessionId = rememberedExecutionSession() || "new";
    // The server-rendered document belongs to the default worker, before browser selection restores.
    let lastRenderedExecutionSessionId = "primary";
    const executionConversationUrls = new Map();
    const executionConversationTitles = new Map();

    function rememberExecutionTitles(items) {
        for (const item of items || []) {
            const key = historyUrlKey(item.url);
            if (key && item.title) executionConversationTitles.set(key, item.title);
        }
        if (executionList) executionList.dataset.signature = "";
    }

    let executionSessionEpoch = 0;
    let executionSessions = [];
    let executionActiveCount = 0;
    let executionCanStart = false;
    let executionSelectionRestored = false;
    const executionDrafts = new Map();
    let restoredExecutionConfiguration = "";
    let newSessionSelectionPending = false;

    function syncNewSessionButton() {
        if (elements.newSessionButton) {
            elements.newSessionButton.disabled = promptSubmissionPending || newSessionSelectionPending;
        }
    }

    function restoreExecutionConfiguration(agent, {force = false} = {}) {
        if (agent?.session_id !== executionSessionId || !agent.workspace_path
            || agent.platform !== selectedPlatform() || agent.browser !== selectedBrowser()) return;
        const signature = JSON.stringify([executionScope, executionSessionId, agent.run_id,
            agent.workspace_path, agent.project_url, agent.conversation_url, agent.session_mode]);
        if (!force && restoredExecutionConfiguration === signature) return;
        restoredExecutionConfiguration = signature;
        syncProjectPath(agent.workspace_path);
        if (elements.projectPath) elements.projectPath.value = agent.workspace_path;
        const project = isAgentProjectUrl(selectedPlatform(), agent.project_url) ? agent.project_url : "";
        const conversation = isAgentConversationUrl(selectedPlatform(), agent.conversation_url)
            ? agent.conversation_url : "";
        const restoreChoice = (combobox, value, label) => {
            if (!combobox || !value) return;
            const menu = combobox.querySelector("[data-agent-combobox-menu]");
            if (menu && !Array.from(menu.querySelectorAll("[data-agent-combobox-option]"))
                .some((option) => option.dataset.agentComboboxOption === value)) {
                menu.append(sourceOptionButton(value, label));
            }
            selectSessionListValue(combobox, value, label);
        };
        const previousProject = selectedProjectUrl();
        sessionTitleOverride = agent.session_title || "";
        elements.sessionMode.value = project ? "project" : "new";
        selectedRemoteConversationUrl = project ? "" : conversation;
        if (elements.projectUrl) elements.projectUrl.value = "";

        selectedProjectConversationUrl = "new";
        if (project) {
            const known = agentSources.projects?.find(
                (item) => historyUrlKey(item.url) === historyUrlKey(project),
            );
            const currentProject = known?.url || project;
            restoreChoice(
                elements.projectCombobox,
                currentProject,
                known?.title || agent.project_title || currentProject,
            );
            selectedProjectConversationUrl = conversation || "new";
            if (historyUrlKey(currentProject) !== historyUrlKey(previousProject)) {
                projectSessions = [];
                projectSessionsLoading = true;
                void loadProjectSessions(currentProject);
            }
        }
        updateSessionChoiceInputs();
    }
    const executionRail = document.querySelector("[data-agent-execution-sessions]");
    // Keep warm server templates aligned without interrupting active sessions.
    document.querySelector("#agent_runtime_form")?.after(...(executionRail ? [executionRail] : []));
    const executionList = document.querySelector("[data-agent-execution-session-list]");

    function syncExecutionWorkspace(sessionId) {
        const session = executionSessions.find((item) => item.session_id === sessionId);
        const path = String(session?.workspace_path || "").trim();
        if (!path || path === String(elements.workspacePath?.value || "")) return;
        syncProjectPath(path);
        if (elements.projectPath instanceof HTMLInputElement) elements.projectPath.value = path;
    }

    function renderExecutionSessions(payload) {
        const enabled = selectedPlatform() === "chatgpt" && selectedBrowser() === "edge";
        if (executionRail) executionRail.hidden = false;
        if (payload.agent?.session_id && payload.agent?.conversation_url) {
            executionConversationUrls.set(payload.agent.session_id, payload.agent.conversation_url);
        }
        if (Array.isArray(payload.sessions)) {
            executionSessions = payload.sessions;
            if (!executionSelectionRestored) syncExecutionWorkspace(executionSessionId);
            if (!executionSelectionRestored) {
                executionSelectionRestored = true;
                const remembered = rememberedExecutionSession();
                const restored = remembered === "new" || executionSessions.some((item) => item.session_id === remembered)
                    ? remembered : "new";
                if (restored && restored !== executionSessionId) {
                    const epoch = executionSessionEpoch;
                    window.queueMicrotask(() => {
                        if (epoch === executionSessionEpoch) void selectExecutionSession(restored);
                    });
                }
            }
        }
        if (Number.isInteger(payload.active_count)) executionActiveCount = payload.active_count;
        if (typeof payload.can_start === "boolean") executionCanStart = payload.can_start;
        else if (Array.isArray(payload.sessions)) {
            executionCanStart = executionActiveCount === 0 || (
                enabled && executionActiveCount < 2
                && executionSessions.filter((item) => item.running).length === executionActiveCount
            );
        }
        renderRecentSessionList();
    }

    function renderRecentSessionList() {
        if (!executionList) return;
        const project = selectedSessionMode() === "project" ? selectedProjectUrl() : "";
        const inProjectMode = selectedSessionMode() === "project";
        const remote = inProjectMode ? projectSessions : (agentSources.recent_sessions || []);
        const remoteUrls = new Set(remote.map((item) => historyUrlKey(item.url)));
        const local = executionSessions.filter((item) => !inProjectMode || (project && (
            historyUrlKey(item.project_url) === historyUrlKey(project)
            || remoteUrls.has(historyUrlKey(item.conversation_url))
        )));
        const seen = new Set(local.map((item) => historyUrlKey(item.conversation_url)).filter(Boolean));
        const items = [...local];
        for (const item of remote) {
            if (!isAgentConversationUrl(selectedPlatform(), item.url) || seen.has(historyUrlKey(item.url))) continue;
            seen.add(historyUrlKey(item.url));
            items.push({conversation_url: item.url, session_title: item.title || item.name,
                project_url: project, updated_at: item.updated_at});
        }
        const capacity = document.querySelector("[data-agent-session-capacity]");
        if (capacity) {
            const count = local.filter((item) => item.running).length;
            capacity.hidden = count === 0;
            capacity.textContent = count ? count.toLocaleString("en-US") : "";
            capacity.setAttribute("aria-label", `${count.toLocaleString("en-US")} active sessions`);
        }
        const signature = JSON.stringify([items, executionSessionId, selectedConversationUrl(),
            project, projectSessionsLoading, catalogState, catalogError,
            verifiedProjectSessionsKey, [...executionConversationTitles], [...executionConversationUrls]]);
        if (executionList.dataset.signature === signature) return;
        executionList.dataset.signature = signature;
        const focusedId = document.activeElement?.dataset?.executionSessionId;
        executionList.replaceChildren(...items.map((session) => {
            const row = document.createElement("div");
            row.className = "agent-execution-session-row";
            const button = document.createElement("button");
            button.type = "button";
            button.className = "trade-strategy-dropdown-option agent-execution-session";
            if (session.session_id) button.dataset.executionSessionId = session.session_id;
            else button.dataset.recentConversationUrl = session.conversation_url;
            button.setAttribute("aria-pressed", String(session.session_id ? session.session_id === executionSessionId : historyUrlKey(selectedConversationUrl()) === historyUrlKey(session.conversation_url)));
            button.classList.toggle("is-selected", button.getAttribute("aria-pressed") === "true");
            const title = document.createElement("span");
            title.className = "agent-execution-session-title";
            const conversationUrl = session.conversation_url || executionConversationUrls.get(session.session_id);
            title.textContent = executionConversationTitles.get(historyUrlKey(conversationUrl))
                || session.session_title || "Untitled session";
            const state = document.createElement("span");
            state.className = "agent-execution-session-state";
            const stateLabel = session.paused ? "Paused" : session.running ? "Running"
                : session.phase === "finished" ? "Completed" : session.phase || "";
            if (session.running && !session.paused) {
                state.classList.add("suggestion-loading-spinner");
                state.setAttribute("role", "img");
                state.setAttribute("aria-label", stateLabel);
            } else {
                state.textContent = stateLabel;
            }
            button.title = [title.textContent, session.workspace_path, stateLabel].filter(Boolean).join(" · ");
            button.append(title, state);
            button.addEventListener("click", () => {
                if (session.session_id) void selectExecutionSession(session.session_id);
                else void selectRecentConversation(session);
            });
            row.append(button);
            const remoteRecordAbsent = !conversationUrl
                || !remoteUrls.has(historyUrlKey(conversationUrl));
            const remoteCatalogVerified = inProjectMode
                ? Boolean(project && !projectSessionsLoading
                    && verifiedProjectSessionsKey === historyUrlKey(project))
                : catalogState === "ready" && !sourcesLoading;
            const deletable = Boolean(
                session.session_id
                && !session.running
                && String(session.phase || "").toLowerCase() === "failed"
                && remoteCatalogVerified
                && remoteRecordAbsent
            );
            if (deletable) {
                row.classList.add("is-deletable");
                const deleteButton = document.createElement("button");
                deleteButton.type = "button";
                deleteButton.className = "agent-execution-session-delete";
                deleteButton.setAttribute("aria-label", `Delete failed session: ${title.textContent}`);
                deleteButton.title = "Delete failed session";
                deleteButton.addEventListener("click", (event) => {
                    event.stopPropagation();
                    void dismissFailedSession(
                        {...session, conversation_url: conversationUrl || ""},
                        deleteButton,
                    );
                });
                row.append(deleteButton);
            }
            return row;
        }));
        if (!items.length) {
            const empty = document.createElement("p");
            empty.className = "agent-recent-sessions-empty";
            empty.textContent = inProjectMode && !project ? `${projectChoicePlaceholder()} to view recent sessions.`
                : (inProjectMode ? projectSessionsLoading : sourcesLoading) ? "Loading recent sessions…"
                : catalogState === "error" ? catalogError : "No recent sessions.";
            executionList.append(empty);
        }
        if (focusedId) executionList.querySelector(
            `[data-execution-session-id="${CSS.escape(focusedId)}"]`,
        )?.focus();
    }

    async function dismissFailedSession(session, deleteButton) {
        if (!session?.session_id || deleteButton.disabled) return;
        deleteButton.disabled = true;
        try {
            await requestJson("/api/agent/session", {
                method: "DELETE",
                body: JSON.stringify({
                    session_id: session.session_id,
                    conversation_url: session.conversation_url || "",
                    remote_record_absent: true,
                }),
            });
            executionSessions = executionSessions.filter(
                (item) => item.session_id !== session.session_id,
            );
            executionConversationUrls.delete(session.session_id);
            if (executionSessionId === session.session_id) {
                await selectExecutionSession("new", {preserveSourceSelection: true});
            } else {
                renderRecentSessionList();
            }
        } catch (error) {
            deleteButton.disabled = false;
            setResponseStatusFallback(error.message);
        }
    }

    async function selectRecentConversation(session) {
        const requestId = ++recentSelectionRequestId;
        const scope = executionScope;
        const project = selectedProjectUrl();
        await selectExecutionSession("new", {preserveSourceSelection: true});
        if (requestId !== recentSelectionRequestId || scope !== executionScope
            || project !== selectedProjectUrl() || executionSessionId !== "new") return;
        selectedRemoteConversationUrl = session.conversation_url;
        sessionTitleOverride = session.session_title || "";
        if (selectedSessionMode() === "project") {
            selectedProjectConversationUrl = session.conversation_url;
        }
        updateSessionChoiceInputs();
        rememberSessionSelection();
        render(lastPayload);
        void loadSelectedSessionHistory(session.conversation_url);
    }

    async function beginNewSession() {
        if (promptSubmissionPending || newSessionSelectionPending) return;
        const retainedMode = selectedSessionMode();
        const retainedProjectUrl = selectedProjectUrl();
        newSessionSelectionPending = true;
        syncNewSessionButton();
        try {
            if (executionSessionId !== "new") {
                await selectExecutionSession("new", {preserveSourceSelection: true});
            }
            if (executionSessionId !== "new") return;

            if (elements.sessionMode) elements.sessionMode.value = retainedMode;
            if (retainedMode === "project" && elements.projectUrl instanceof HTMLInputElement) {
                elements.projectUrl.value = retainedProjectUrl;
            }
            selectedRemoteConversationUrl = "";
            selectedProjectConversationUrl = "new";
            sessionTitleOverride = "";
            resetRemoteSessionHistory();
            doctorRequestId += 1;
            doctorPayload = null;
            if (elements.doctorPanel) elements.doctorPanel.open = false;
            updateSessionChoiceInputs();
            rememberSessionSelection();
            render({...lastPayload, agent: {session_id: "new"}});
            elements.promptInput?.focus();
        } finally {
            newSessionSelectionPending = false;
            syncNewSessionButton();
        }
    }

    async function selectExecutionSession(sessionId, {routeChanged = false, previousScope = executionScope, preserveSourceSelection = false} = {}) {
        if (!routeChanged && (promptSubmissionPending || sessionId === executionSessionId)) return;
        executionDrafts.set(JSON.stringify([previousScope, executionSessionId]), elements.promptInput?.value || "");
        executionSessionId = sessionId;
        restoredExecutionConfiguration = "";
        projectSessionRequestId += 1;
        syncExecutionWorkspace(sessionId);
        try { window.sessionStorage.setItem(executionStorageKey(), sessionId); } catch (_error) {}
        const epoch = ++executionSessionEpoch;
        lastRenderedAgentRunIdentity = "";
        lastRenderedAgentRunRevision = 0;
        lastRenderedAgentStartedAt = "";
        lastRenderedAgentRunning = false;
        boundAgentSessionSignature = "";
        resetRemoteSessionHistory();
        selectedRemoteConversationUrl = "";
        doctorRequestId += 1;
        doctorPayload = null;
        if (elements.doctorPanel) elements.doctorPanel.open = false;
        sessionTitleOverride = "";
        if (elements.sessionMode && !preserveSourceSelection) elements.sessionMode.value = "new";
        if (elements.promptInput) elements.promptInput.value = executionDrafts.get(JSON.stringify([executionScope, sessionId])) || "";
        promptHasLocalDraft = Boolean(elements.promptInput?.value);
        // Clear the old stop target immediately; stale network responses cannot restore it.
        render({...lastPayload, agent: {session_id: sessionId}});
        if (elements.ask) elements.ask.disabled = true;
        resizePrompt();
        try {
            const payload = await requestJson("/api/agent/status", {headers: agentStatusHeaders()});
            if (epoch === executionSessionEpoch) render(payload);
        } catch (error) {
            if (epoch === executionSessionEpoch) setResponseStatusFallback(error.message);
        }
    }

    function agentStatusHeaders() {
        return {
            "X-CacheLikes-Agent-Browser": selectedBrowser(),
            "X-CacheLikes-Agent-Platform": selectedPlatform(),
            "X-CacheLikes-Agent-Workspace": String(elements.workspacePath?.value || ""),
        };
    }

    async function requestJson(url, options = {}) {
        const requestEpoch = executionSessionEpoch;
        const controller = new AbortController();
        const timeout = url === "/api/agent/status"
            ? window.setTimeout(() => controller.abort(), 8_000) : null;
        try {
            const response = await fetch(url, {
                ...options,
                signal: options.signal || controller.signal,
                headers: {"Content-Type": "application/json", "X-CacheLikes-Agent-Session": executionSessionId, ...(options.headers || {})},
            });
            const payload = await response.json();
            if (!response.ok) {
                if (url === "/api/agent/status" && response.status === 404
                    && payload.code === "unknown_agent_session" && requestEpoch === executionSessionEpoch
                    && executionSessionId !== "new") {
                    await selectExecutionSession("new");
                }
                throw new Error(payload.error || `Request failed with ${response.status}.`);
            }
            return payload;
        } finally {
            if (timeout !== null) window.clearTimeout(timeout);
        }
    }

    function formPayload(form) {
        return Object.fromEntries(new FormData(form).entries());
    }

    function selectedValue(selector, fallback) {
        return document.querySelector(`${selector} [data-agent-combobox-input]`)?.value || fallback;
    }

    function selectedOs() {
        return selectedValue(".agent-os-combobox", elements.promptOs?.value || "macos");
    }

    function selectedBrowser() {
        return selectedValue(".agent-browser-combobox", "edge");
    }

    function selectedPlatform() {
        return selectedValue(".agent-platform-combobox", "chatgpt");
    }

    function chatgptProjectIcon() {
        if (selectedPlatform() !== "chatgpt") return "";
        return elements.projectCombobox?.dataset.agentProjectIcon || "";
    }

    function chatgptProjectIconForItem(item) {
        const fallback = chatgptProjectIcon();
        if (selectedPlatform() !== "chatgpt") return fallback;
        return window.CACHELIKES_CHATGPT_PROJECT_ICONS?.projectIcon(item, fallback) || fallback;
    }

    function agentRunIdentity(agent) {
        return String(agent?.run_id || agent?.started_at || "");
    }

    function normalizeAgentRunRevision(value) {
        const revision = Number(value);
        return Number.isSafeInteger(revision) && revision > 0 ? revision : 0;
    }

    function agentRunRevision(agent) {
        return normalizeAgentRunRevision(agent?.run_revision);
    }

    function agentRunStartedAt(agent) {
        return String(agent?.started_at || "");
    }

    function runSupersedes(
        runIdentity,
        runRevision,
        startedAt,
        previousIdentity,
        previousRevision,
        previousStartedAt,
    ) {
        if (!runIdentity) return false;
        if (!previousIdentity) return true;
        if (runIdentity === previousIdentity) return false;
        if (runRevision && previousRevision) return runRevision > previousRevision;
        if (runRevision) return true;
        if (previousRevision || !startedAt) return false;
        if (!previousStartedAt) return true;
        return startedAt > previousStartedAt;
    }

    function normalizeAgentSelection() {
        if (selectedPlatform() === "chatgpt" || selectedBrowser() !== "safari") return;
        const browserCombobox = document.querySelector(".agent-browser-combobox");
        const edgeOption = browserCombobox?.querySelector('[data-agent-combobox-option="edge"]');
        const browserInput = browserCombobox?.querySelector("[data-agent-combobox-input]");
        if (!(browserInput instanceof HTMLInputElement) || !edgeOption) return;
        browserInput.value = "edge";
        syncComboboxTriggerFromOption(browserCombobox, edgeOption);
    }

    function selectedModel() {
        return selectedValue(".agent-model-combobox", "");
    }

    function selectedChatgptEffort() {
        return elements.effortInput instanceof HTMLInputElement
            ? (elements.effortInput.value || HIGHEST_CHATGPT_EFFORT)
            : HIGHEST_CHATGPT_EFFORT;
    }

    function selectedPlatformLabel() {
        return document.querySelector(".agent-platform-combobox [data-agent-combobox-selected-label]")?.textContent?.trim() || "Web AI";
    }

    function syncAgentRoute() {
        if (executionScope !== executionStorageKey()) {
            const previousScope = executionScope;
            executionScope = executionStorageKey();
            executionSessions = [];
            executionSelectionRestored = false;
            executionCanStart = false;
            lastPayload = {...lastPayload, can_start: false};
            delete lastPayload.sessions;
            void selectExecutionSession(rememberedExecutionSession() || "new", {routeChanged: true, previousScope});
        }
        const routePrefix = String(elements.agentPage?.dataset.agentRoutePrefix || "/agent").replace(/\/$/, "");
        const nextPath = `${routePrefix}/${encodeURIComponent(selectedBrowser())}/${encodeURIComponent(selectedPlatform())}`;
        const currentPath = `${window.location.pathname}${window.location.search}${window.location.hash}`;
        if (currentPath === nextPath) return;
        window.history.replaceState({}, "", nextPath);
        window.dispatchEvent(new Event("popstate"));
    }

    function selectedPlatformHomeUrl() {
        const option = Array.from(
            document.querySelectorAll(".agent-platform-combobox [data-agent-combobox-option]"),
        ).find((candidate) => candidate.dataset.agentComboboxOption === selectedPlatform());
        return option?.dataset.agentPlatformHomeUrl || "https://chatgpt.com/";
    }

    function selectedBrowserLabel() {
        return document.querySelector(".agent-browser-combobox [data-agent-combobox-selected-label]")?.textContent?.trim() || "selected browser";
    }

    function agentSnapshotMatchesSelection(agent) {
        const platform = String(agent?.platform || "").trim().toLowerCase();
        const browser = String(agent?.browser || "").trim().toLowerCase();
        const workspacePath = String(agent?.workspace_path || "").trim();
        const selectedWorkspacePath = String(elements.workspacePath?.value || "").trim();
        return Boolean(platform && browser)
            && platform === selectedPlatform()
            && browser === selectedBrowser()
            && Boolean(workspacePath && selectedWorkspacePath)
            && workspacePath === selectedWorkspacePath;
    }

    function isolatedForeignRunningAgent(agent) {
        if (!agent?.running) return {};
        // A task is global enough that the user must still be able to stop it,
        // but its project-specific prompt, response, activity, and errors must
        // never flow into a page for a different workspace or provider route.
        return {
            running: true,
            paused: false,
            phase: "running",
            message: "An Agent task is running in another project. Stop remains available here.",
            activity: [],
            history: [],
            prompt: "",
            response: "",
            response_html: "",
            error_traceback: "",
            last_error: "",
            conversation_url: "",
            project_url: "",
            workspace_path: "",
        };
    }

    function selectedSessionMode() {
        return elements.sessionMode?.value || "new";
    }

    function selectedConversationUrl() {
        if (selectedSessionMode() === "new") return selectedRemoteConversationUrl;
        if (selectedSessionMode() === "project") return selectedProjectConversationUrl === "new" ? "" : selectedProjectConversationUrl || "";
        return "";
    }

    function isChatgptConversationUrl(value) {
        return /^https:\/\/chatgpt\.com\/(?:g\/[^/]+\/)?c\/[^/]+\/?$/i.test(String(value || "").trim());
    }

    function isAgentConversationUrl(platform, value) {
        const candidate = String(value || "").trim();
        if (platform === "chatgpt") return isChatgptConversationUrl(candidate);
        if (platform === "gemini") return /^https:\/\/gemini\.google\.com\/app\/[A-Za-z0-9_-]+\/?$/i.test(candidate);
        if (platform === "grok") {
            return /^https:\/\/(?:www\.)?grok\.com\/(?:c\/[A-Za-z0-9_-]+\/?|project\/[A-Za-z0-9_-]+\/?\?chat=[A-Za-z0-9_-]+)$/i.test(candidate);
        }
        if (platform === "claude") {
            return /^https:\/\/claude\.ai\/(?:chat\/[A-Za-z0-9_-]+|project\/[A-Za-z0-9_-]+\/(?:chat|c)\/[A-Za-z0-9_-]+)\/?$/i.test(candidate);
        }
        return false;
    }

    function isAgentProjectUrl(platform, value) {
        const candidate = String(value || "").trim();
        if (!candidate || candidate.length > MAX_AGENT_SESSION_CACHE_VALUE_LENGTH) return false;
        try {
            const parsed = new URL(candidate);
            if (parsed.protocol !== "https:"
                || parsed.port
                || parsed.username
                || parsed.password) return false;
            const allowedHosts = {
                chatgpt: new Set(["chatgpt.com", "www.chatgpt.com"]),
                gemini: new Set(["gemini.google.com"]),
                grok: new Set(["grok.com", "www.grok.com"]),
                claude: new Set(["claude.ai", "www.claude.ai"]),
            };
            return allowedHosts[platform]?.has(parsed.hostname.toLowerCase()) || false;
        } catch (_error) {
            return false;
        }
    }

    function sessionSelectionCacheKey() {
        return `${AGENT_SESSION_SELECTION_CACHE_PREFIX}:v${AGENT_SESSION_SELECTION_CACHE_VERSION}`
            + `:${selectedPlatform()}:${selectedBrowser()}`;
    }

    function normalizedSessionCacheUrl(value) {
        const candidate = String(value || "").trim();
        return candidate.length <= MAX_AGENT_SESSION_CACHE_VALUE_LENGTH ? candidate : "";
    }

    function readRememberedSessionSelection() {
        try {
            const rawValue = window.localStorage.getItem(sessionSelectionCacheKey());
            if (!rawValue) return null;
            const payload = JSON.parse(rawValue);
            if (!payload || payload.version !== AGENT_SESSION_SELECTION_CACHE_VERSION) return null;
            const platform = selectedPlatform();
            const mode = AGENT_SESSION_MODES.has(payload.mode) ? payload.mode : "new";
            const projectUrl = normalizedSessionCacheUrl(payload.project_url);
            const projectSessionUrl = normalizedSessionCacheUrl(payload.project_session_url);
            return {
                version: AGENT_SESSION_SELECTION_CACHE_VERSION,
                mode,
                project_url: isAgentProjectUrl(platform, projectUrl) ? projectUrl : "",
                project_session_url: projectSessionUrl === "new"
                    ? "new"
                    : (isAgentConversationUrl(platform, projectSessionUrl) ? projectSessionUrl : "new"),
            };
        } catch (_error) {
            return null;
        }
    }

    function rememberSessionSelection() {
        const mode = selectedSessionMode();
        if (!AGENT_SESSION_MODES.has(mode)) return;
        const remembered = readRememberedSessionSelection() || {
            version: AGENT_SESSION_SELECTION_CACHE_VERSION,
            mode: "new",
            project_url: "",
            project_session_url: "new",
        };
        remembered.mode = mode;
        const platform = selectedPlatform();
        if (mode === "project") {
            const projectUrl = normalizedSessionCacheUrl(elements.projectUrl?.value);
            if (isAgentProjectUrl(platform, projectUrl)) {
                remembered.project_url = projectUrl;
                const projectSessionUrl = normalizedSessionCacheUrl(selectedProjectConversationUrl || "new");
                remembered.project_session_url = projectSessionUrl === "new"
                    ? "new"
                    : (isAgentConversationUrl(platform, projectSessionUrl) ? projectSessionUrl : "new");
            }
        }
        try {
            window.localStorage.setItem(sessionSelectionCacheKey(), JSON.stringify(remembered));
        } catch (_error) {
            // Local storage is a convenience cache and must never block the Agent form.
        }
    }

    function historyUrlKey(value) {
        const normalized = String(value || "").trim().replace(/\/+$/, "").toLowerCase();
        try {
            const parsed = new URL(normalized);
            const allowedHost = ["chatgpt.com", "www.chatgpt.com"].includes(parsed.hostname);
            const projectPath = parsed.pathname.match(
                /^\/g\/(g-p-[0-9a-f]{32})(?:-[^/]*)?(\/(?:project|c\/[^/]+))\/?$/i,
            );
            const rootConversationPath = parsed.pathname.match(/^\/c\/([^/]+)\/?$/i);
            if (parsed.protocol === "https:"
                && allowedHost
                && !parsed.port
                && !parsed.username
                && !parsed.password) {
                if (projectPath) {
                    return `https://chatgpt.com/g/${projectPath[1]}${projectPath[2]}`;
                }
                if (rootConversationPath) {
                    return `https://chatgpt.com/c/${rootConversationPath[1]}`;
                }
            }
        } catch (_error) {
            // Non-URL selection sentinels retain their existing string identity.
        }
        return normalized;
    }

    function resetRemoteSessionHistory() {
        remoteSessionHistoryRequestId += 1;
        remoteSessionHistory = [];
        remoteSessionHistoryPlatform = "";
        remoteSessionHistoryUrl = "";
        remoteSessionHistoryBrowser = "";
        remoteSessionHistoryLoading = false;
        remoteSessionHistoryError = "";
        responseHistorySignature = "";
        responseHistoryPage = 1;
    }

    function selectedHistoryConversationUrl() {
        const managed = executionSessions.some((item) => item.session_id === executionSessionId);
        if (managed) {
            const agent = lastPayload?.agent;
            return agent?.session_id === executionSessionId && agent.conversation_bound
                ? String(agent.conversation_url || "").trim() : "";
        }
        return selectedConversationUrl();
    }

    function remoteHistoryMatchesSelection() {
        const selectedUrl = selectedHistoryConversationUrl();
        return Boolean(selectedUrl)
            && remoteSessionHistoryPlatform === selectedPlatform()
            && historyUrlKey(remoteSessionHistoryUrl) === historyUrlKey(selectedUrl)
            && remoteSessionHistoryBrowser === selectedBrowser();
    }

    function selectedProjectUrl() {
        return elements.projectUrl?.value || "";
    }

    function selectedComboboxLabel(combobox) {
        const selectedValue = combobox?.querySelector("[data-agent-combobox-input]")?.value || "";
        const selectedOption = Array.from(
            combobox?.querySelectorAll("[data-agent-combobox-option]") || [],
        ).find((option) => option.dataset.agentComboboxOption === selectedValue);
        return selectedOption?.dataset.agentComboboxLabel
            || combobox?.querySelector("[data-agent-combobox-selected-label]")?.textContent?.trim()
            || "";
    }

    function selectedSessionTitle() {
        if (sessionTitleOverride) return sessionTitleOverride;
        return "";
    }

    function projectCollectionLabel() {
        return selectedPlatform() === "gemini" ? "Notebooks" : "Projects";
    }

    function projectChoicePlaceholder() {
        return selectedPlatform() === "gemini" ? "Choose a notebook" : "Choose a project";
    }

    function syncModelOptionsForPlatform() {
        const platform = selectedPlatform();
        const combobox = document.querySelector(".agent-model-combobox");
        const input = elements.modelInput;
        const menu = combobox?.querySelector("[data-agent-combobox-menu]");
        if (!combobox || !(input instanceof HTMLInputElement) || !menu) return;
        const liveCatalog = platform === "chatgpt"
            && lastBrowserStatus?.platform === platform
            && lastBrowserStatus?.browser === selectedBrowser()
            && lastBrowserStatus?.model_catalog_complete
            ? lastBrowserStatus : null;
        menu.querySelectorAll("[data-agent-model-generated]").forEach((option) => option.remove());
        if (liveCatalog) {
            (liveCatalog.model_options || []).forEach((model) => {
                if (!String(model.key || "").startsWith("live:") || !model.label) return;
                const option = createChatgptEffortOption(model.key, model.label);
                delete option.dataset.agentEffortGenerated;
                option.dataset.agentModelGenerated = "true";
                option.dataset.agentPlatform = "chatgpt";
                menu.append(option);
            });
        }
        const automatic = menu.querySelector('[data-agent-combobox-option="latest_available"]');
        if (automatic) {
            const label = liveCatalog?.actual_model || "Latest";
            automatic.dataset.agentComboboxLabel = label;
            const text = automatic.querySelector(".trade-strategy-dropdown-text");
            if (text) text.textContent = `ChatGPT · ${label}`;
        }
        const options = Array.from(menu.querySelectorAll("[data-agent-combobox-option]"));
        const providerLatest = liveCatalog && options.find((option) =>
            option.dataset.agentModelGenerated === "true"
            && option.dataset.agentComboboxOption === "live:latest",
        );
        options.forEach((option) => {
            option.hidden = option.dataset.agentPlatform !== platform
                || Boolean(providerLatest && option === automatic)
                || Boolean(liveCatalog && option.dataset.agentComboboxOption === "gpt-5.6-sol");
        });
        const visibleOptions = options.filter((option) => !option.hidden);
        let desiredModel = liveCatalog && preferredModel.startsWith("live:") ? preferredModel : input.value;
        if (providerLatest && desiredModel === "latest_available") desiredModel = "live:latest";
        let selectedOption = visibleOptions.find((option) => option.dataset.agentComboboxOption === desiredModel);
        if (!selectedOption) {
            selectedOption = visibleOptions.reduce((strongest, option) => {
                if (!strongest) return option;
                const optionStrength = Number(option.dataset.agentModelStrength || 0);
                const strongestStrength = Number(strongest.dataset.agentModelStrength || 0);
                if (optionStrength !== strongestStrength) {
                    return optionStrength > strongestStrength ? option : strongest;
                }
                return String(option.dataset.agentComboboxOption || "")
                    > String(strongest.dataset.agentComboboxOption || "")
                    ? option
                    : strongest;
            }, null);
        }
        if (!selectedOption) return;
        input.value = selectedOption.dataset.agentComboboxOption || "";
        const label = combobox.querySelector("[data-agent-combobox-selected-label]");
        if (label) label.textContent = selectedOption.dataset.agentComboboxLabel || "";
        const trigger = combobox.querySelector("[data-agent-combobox-trigger]");
        if (trigger) trigger.setAttribute("aria-label", `Model: ${selectedOption.dataset.agentComboboxLabel || ""}`);
        options.forEach((option) => {
            const isSelected = option === selectedOption;
            option.classList.toggle("is-selected", isSelected);
            option.classList.toggle("is-active", isSelected);
            option.setAttribute("aria-selected", String(isSelected));
        });
    }

    function createChatgptEffortOption(value, label) {
        const option = document.createElement("button");
        option.type = "button";
        option.className = "trade-strategy-dropdown-option agent-combobox-option";
        option.dataset.agentComboboxOption = value;
        option.dataset.agentComboboxLabel = label;
        option.dataset.agentEffortGenerated = "true";
        option.setAttribute("role", "option");
        option.setAttribute("aria-selected", "false");
        option.tabIndex = -1;
        const check = document.createElement("span");
        check.className = "trade-strategy-dropdown-check";
        check.setAttribute("aria-hidden", "true");
        const text = document.createElement("span");
        text.className = "trade-strategy-dropdown-text";
        text.textContent = label;
        option.append(check, text);
        return option;
    }

    function normalizedChatgptEffortLabels(values) {
        if (!Array.isArray(values)) return [];
        const labels = [];
        values.forEach((value) => {
            const normalized = String(value || "").replace(/\s+/g, " ").trim();
            if (
                normalized
                && !labels.some((label) => label.toLocaleLowerCase() === normalized.toLocaleLowerCase())
            ) {
                labels.push(normalized);
            }
        });
        return labels;
    }

    function verifiedChatgptEffortCatalog() {
        const status = lastBrowserStatus;
        const platform = String(status?.platform || "").trim().toLowerCase();
        const browser = String(status?.browser || "").trim().toLowerCase();
        const freshness = status?.browser_session_freshness;
        const freshnessKind = String(freshness?.kind || "").trim().toLowerCase();
        const cacheStatus = String(freshness?.cache_status || "").trim().toLowerCase();
        const allowedCacheStatuses = CHATGPT_EFFORT_CATALOG_FRESHNESS.get(freshnessKind);
        const cachedAt = String(freshness?.cached_at || "").trim();
        if (
            platform !== "chatgpt"
            || browser !== selectedBrowser()
            || !status?.effort_catalog_complete
            || !cachedAt
            || !allowedCacheStatuses?.has(cacheStatus)
            || (selectedModel().startsWith("live:")
                && selectedModel().slice(5).toLowerCase() !== String(status.actual_model || "").toLowerCase())
        ) return null;
        const labels = normalizedChatgptEffortLabels(status?.available_efforts);
        return labels.length ? {freshnessKind, labels} : null;
    }

    function syncChatgptEffortOptions() {
        const field = elements.effortField;
        const combobox = elements.effortCombobox;
        const input = elements.effortInput;
        const menu = combobox?.querySelector("[data-agent-combobox-menu]");
        if (!field || !combobox || !(input instanceof HTMLInputElement) || !menu) return;
        const isChatgpt = selectedPlatform() === "chatgpt";
        field.hidden = !isChatgpt;
        if (!isChatgpt) return;

        Array.from(menu.querySelectorAll("[data-agent-effort-generated]"))
            .forEach((option) => option.remove());
        const catalog = verifiedChatgptEffortCatalog();
        const automatic = menu.querySelector('[data-agent-combobox-option="highest_available"]');
        if (automatic) {
            const label = catalog?.labels.at(-1) || "Highest available";
            automatic.dataset.agentComboboxLabel = label;
            const text = automatic.querySelector(".trade-strategy-dropdown-text");
            if (text) text.textContent = catalog ? `${label} (Highest available)` : label;
        }
        if (catalog) field.dataset.agentEffortCatalogFreshness = catalog.freshnessKind;
        else delete field.dataset.agentEffortCatalogFreshness;
        const current = selectedChatgptEffort();
        const persistedPreference = String(input.dataset.agentEffortPreference || "").trim();
        const preferred = effortSelectionTouched
            ? current
            : (catalog ? (persistedPreference || current) : HIGHEST_CHATGPT_EFFORT);
        (catalog?.labels || []).forEach((label) => {
            menu.append(createChatgptEffortOption(label, label));
        });

        const options = Array.from(menu.querySelectorAll("[data-agent-combobox-option]"));
        const selected = options.find((option) => option.dataset.agentComboboxOption === preferred)
            || options.find((option) => option.dataset.agentComboboxOption === HIGHEST_CHATGPT_EFFORT)
            || null;
        if (!selected) return;
        input.value = selected.dataset.agentComboboxOption || HIGHEST_CHATGPT_EFFORT;
        syncComboboxTriggerFromOption(combobox, selected);
        options.forEach((option) => {
            const isSelected = option === selected;
            option.classList.toggle("is-selected", isSelected);
            option.classList.toggle("is-active", isSelected);
            option.setAttribute("aria-selected", String(isSelected));
        });
    }

    function syncPlatformState(agent = {}) {
        const platform = selectedPlatform();
        if (elements.promptPlatform instanceof HTMLInputElement) elements.promptPlatform.value = platform;
        if (elements.browserSession) {
            elements.browserSession.dataset.browserSessionPlatform = platform;
            elements.browserSession.dataset.browserSessionAccountLabel = selectedPlatformLabel();
        }
        syncModelOptionsForPlatform();
        syncChatgptEffortOptions();
        const sessionLabel = elements.sessionSource?.querySelector("[data-agent-session-platform-label]");
        if (sessionLabel) sessionLabel.textContent = "Session source";
        const sessionModeMenu = elements.sessionModeCombobox?.querySelector("[data-agent-combobox-menu]");
        Array.from(sessionModeMenu?.querySelectorAll("[data-agent-combobox-option]") || []).forEach((option) => {
        const supportedPlatforms = String(option.dataset.agentSessionPlatforms || "chatgpt,gemini,grok,claude")
                .split(",")
                .map((item) => item.trim())
                .filter(Boolean);
            option.hidden = !supportedPlatforms.includes(platform);
        });
        const collectionLabel = projectCollectionLabel();
        const projectOption = sessionModeMenu?.querySelector('[data-agent-combobox-option="project"]');
        if (projectOption) {
            projectOption.dataset.agentComboboxLabel = collectionLabel;
            const text = projectOption.querySelector(".trade-strategy-dropdown-text");
            if (text) text.textContent = collectionLabel;
        }
        const projectLabel = elements.projectField?.querySelector(".field-label");
        if (projectLabel) projectLabel.textContent = collectionLabel;
        syncSessionModeTrigger();
        const projectMenu = elements.projectCombobox?.querySelector("[data-agent-combobox-menu]");
        projectMenu?.setAttribute("aria-label", projectChoicePlaceholder());
        const sessionSourceMenu = elements.sessionModeCombobox?.querySelector("[data-agent-combobox-menu]");
        sessionSourceMenu?.setAttribute("aria-label", "Choose a session source");
        if (elements.sessionSource) elements.sessionSource.hidden = false;
        browserStatusController?.setPlatform?.(platform);
    }

    function syncConversationLink(agent) {
        if (!elements.conversationLink) return;
        const recordedUrl = String(agent?.conversation_url || "").trim();
        const agentPlatform = String(agent?.platform || selectedPlatform()).trim().toLowerCase();
        const agentBrowser = String(agent?.browser || selectedBrowser()).trim().toLowerCase();
        const currentPlatform = selectedPlatform();
        const samePlatform = agentPlatform === currentPlatform;
        const targetUrl = samePlatform && (recordedUrl.startsWith("https://chatgpt.com/")
            || recordedUrl.startsWith("https://gemini.google.com/")
            || recordedUrl.startsWith("https://grok.com/")
            || recordedUrl.startsWith("https://claude.ai/")
            ) ? recordedUrl
            : selectedPlatformHomeUrl();
        const hasRecordedTarget = samePlatform && isAgentConversationUrl(agentPlatform, recordedUrl);
        const platformLabel = selectedPlatformLabel();
        const browserLabel = agentBrowser === selectedBrowser()
            ? selectedBrowserLabel()
            : ({safari: "Safari", edge: "Edge", chrome: "Chrome"}[agentBrowser] || "selected browser");
        const traditionalHandoff = Boolean(
            samePlatform
            && ["failed", "interrupted"].includes(String(agent?.phase || ""))
            && agent?.traditional_handoff_available,
        );
        const handoffLabel = agent?.traditional_handoff_opened
            ? `Continue in ${browserLabel}`
            : `Open in ${browserLabel}`;
        elements.conversationLink.href = targetUrl;
        elements.conversationLink.dataset.agentBrowser = agentBrowser;
        elements.conversationLink.classList.toggle("is-traditional-handoff", traditionalHandoff);
        if (elements.conversationLinkLabel) {
            elements.conversationLinkLabel.hidden = !traditionalHandoff;
            elements.conversationLinkLabel.textContent = handoffLabel;
        }
        elements.conversationLink.setAttribute(
            "aria-label",
            traditionalHandoff
                ? `${handoffLabel}: failed ${platformLabel} Agent task`
                : (hasRecordedTarget
                    ? `Open ${platformLabel} conversation in ${browserLabel}`
                    : `Open ${platformLabel} in ${browserLabel}`),
        );
        elements.conversationLink.title = elements.conversationLink.getAttribute("aria-label") || "";
    }

    function sessionChoiceReady() {
        const mode = selectedSessionMode();
        if (mode === "new") return true;

        if (mode === "project") {
            const projectSession = selectedProjectConversationUrl || "new";
            return Boolean(selectedProjectUrl()) && (projectSession === "new" || Boolean(projectSession));
        }
        return false;
    }

    function setComboboxValue(combobox, value, label, icon = "") {
        if (!combobox) return;
        const input = combobox.querySelector("[data-agent-combobox-input]");
        if (input instanceof HTMLInputElement) input.value = value || "";
        const menu = combobox.querySelector("[data-agent-combobox-menu]");
        Array.from(menu?.querySelectorAll("[data-agent-combobox-option]") || []).forEach((option) => {
            const isSelected = Boolean(value) && option.dataset.agentComboboxOption === value;
            option.classList.toggle("is-selected", isSelected);
            option.classList.toggle("is-active", isSelected);
            option.setAttribute("aria-selected", String(isSelected));
        });
        syncComboboxTrigger(combobox, label, icon);
    }

    function syncComboboxTrigger(combobox, label, icon = "") {
        if (!combobox) return;
        const selectedLabel = combobox.querySelector("[data-agent-combobox-selected-label]");
        const selectedIcon = combobox.querySelector("[data-agent-combobox-selected-icon]");
        const selectedIconShell = selectedIcon?.closest(".browser-picker-selected-icon-shell");
        const trigger = combobox.querySelector("[data-agent-combobox-trigger]");
        if (selectedLabel) selectedLabel.textContent = label || "";
        if (selectedIcon instanceof HTMLImageElement) {
            selectedIcon.hidden = !icon;
            if (icon) selectedIcon.src = icon;
        }
        if (selectedIconShell instanceof HTMLElement) selectedIconShell.hidden = !icon;
        if (trigger) {
            const fieldLabel = combobox.closest(".field")?.querySelector(".field-label")?.textContent?.trim() || "Option";
            trigger.setAttribute("aria-label", `${fieldLabel}: ${label || ""}`);
        }
    }

    function syncComboboxTriggerFromOption(combobox, option) {
        if (!combobox || !option) return;
        syncComboboxTrigger(
            combobox,
            option.dataset.agentComboboxLabel || "",
            option.dataset.agentComboboxIcon || "",
        );
        const menu = combobox.querySelector("[data-agent-combobox-menu]");
        Array.from(menu?.querySelectorAll("[data-agent-combobox-option]") || []).forEach((other) => {
            const isSelected = other === option;
            other.classList.toggle("is-selected", isSelected);
            other.classList.toggle("is-active", isSelected);
            other.setAttribute("aria-selected", String(isSelected));
        });
    }

    function syncSessionModeTrigger() {
        const combobox = elements.sessionModeCombobox;
        if (!combobox) return;
        const selectedValue = elements.sessionMode?.value || "new";
        combobox.dataset.sourceMode = selectedValue;
        const option = Array.from(combobox.querySelectorAll("[data-agent-combobox-option]")).find(
            (candidate) => candidate.dataset.agentComboboxOption === selectedValue,
        );
        if (!option) return;
        syncComboboxTriggerFromOption(combobox, option);
    }

    function closeAllComboboxes() {
        document.querySelectorAll("[data-agent-combobox]").forEach((combobox) => {
            combobox.classList.remove("is-agent-combobox-open");
            combobox.querySelector("[data-agent-combobox-trigger]")?.setAttribute("aria-expanded", "false");
            const menu = combobox.querySelector("[data-agent-combobox-menu]");
            if (menu) menu.hidden = true;
        });
    }

    function closeAgentComposerCombobox(combobox) {
        if (!combobox) return;
        combobox.classList.remove("is-agent-combobox-open");
        combobox.querySelector("[data-agent-combobox-trigger]")?.setAttribute("aria-expanded", "false");
        const menu = combobox.querySelector("[data-agent-combobox-menu]");
        if (menu) menu.hidden = true;
    }

    function updateSessionChoiceInputs() {
        const mode = selectedSessionMode();
        const projectSessionValue = selectedProjectConversationUrl || "new";
        const executionMode = mode === "project"
            ? (projectSessionValue === "new" ? "project_new" : "project_session")
            : (selectedRemoteConversationUrl ? "recent" : "new");
        if (elements.promptSessionMode instanceof HTMLInputElement) elements.promptSessionMode.value = executionMode;
        if (elements.promptConversationUrl instanceof HTMLInputElement) elements.promptConversationUrl.value = selectedConversationUrl();
        if (elements.promptProjectUrl instanceof HTMLInputElement) elements.promptProjectUrl.value = selectedProjectUrl();
        if (elements.promptSessionTitle instanceof HTMLInputElement) elements.promptSessionTitle.value = selectedSessionTitle();

        if (elements.projectField) elements.projectField.hidden = mode !== "project";
        if (elements.sessionSource) {
            elements.sessionSource.dataset.agentSessionMode = executionMode;
        }
        const managed = executionSessions.some((item) => item.session_id === executionSessionId);
        const active = lastPayload.agent || {};
        // A selected execution session continues its bound conversation in every source mode.
        if (managed && active.session_id === executionSessionId
            && active.platform === selectedPlatform() && active.browser === selectedBrowser()
            && active.conversation_bound && isAgentConversationUrl(selectedPlatform(), active.conversation_url)) {
            if (elements.promptSessionMode) elements.promptSessionMode.value = active.project_url ? "project_session" : "recent";
            if (elements.promptConversationUrl) elements.promptConversationUrl.value = active.conversation_url;
            if (elements.promptProjectUrl) elements.promptProjectUrl.value = active.project_url || "";
            if (elements.promptSessionTitle) elements.promptSessionTitle.value = active.session_title || "";
        }
        syncSessionModeTrigger();
        renderRecentSessionList();
    }

    function sourceOptionButton(
        value,
        label,
        icon = "",
        selected = false,
        iconName = "",
        iconColor = "",
    ) {
        const option = document.createElement("button");
        option.type = "button";
        option.className = `trade-strategy-dropdown-option agent-combobox-option${selected ? " is-selected is-active" : ""}`;
        option.dataset.agentComboboxOption = value || "";
        option.dataset.agentComboboxLabel = label || "";
        if (icon) option.dataset.agentComboboxIcon = icon;
        if (iconName) option.dataset.agentComboboxIconName = iconName;
        if (iconColor) option.dataset.agentComboboxIconColor = iconColor;
        option.setAttribute("role", "option");
        option.setAttribute("aria-selected", String(selected));
        option.tabIndex = -1;
        const check = document.createElement("span");
        check.className = "trade-strategy-dropdown-check";
        check.setAttribute("aria-hidden", "true");
        const text = document.createElement("span");
        text.className = "trade-strategy-dropdown-text";
        text.textContent = label || "Untitled";
        if (icon) {
            const iconElement = document.createElement("img");
            iconElement.className = "browser-picker-option-icon";
            iconElement.src = icon;
            iconElement.alt = "";
            iconElement.setAttribute("aria-hidden", "true");
            option.append(check, iconElement, text);
        } else {
            option.append(check, text);
            option.style.gridTemplateColumns = "16px minmax(0, 1fr)";
        }
        return option;
    }

    function setProjectComboboxValue(value, label) {
        setComboboxValue(elements.projectCombobox, value, label, chatgptProjectIcon());
    }

    function setComboboxLoading(combobox, loading) {
        if (!combobox) return;
        const spinner = combobox.querySelector("[data-agent-combobox-spinner]");
        const trigger = combobox.querySelector("[data-agent-combobox-trigger]");
        if (spinner) spinner.hidden = !loading;
        if (!trigger) return;
        if (loading) {
            const fieldLabel = combobox.closest(".field")?.querySelector(".field-label")?.textContent?.trim() || "Option";
            trigger.setAttribute("aria-label", `${fieldLabel}: Loading`);
            trigger.setAttribute("aria-busy", "true");
        } else {
            trigger.removeAttribute("aria-busy");
        }
    }

    function populateListCombobox(combobox, items, emptyLabel, icon = "") {
        if (!combobox) return;
        const input = combobox.querySelector("[data-agent-combobox-input]");
        const menu = combobox.querySelector("[data-agent-combobox-menu]");
        const trigger = combobox.querySelector("[data-agent-combobox-trigger]");
        const listIcon = combobox === elements.projectCombobox ? chatgptProjectIcon() : icon;
        if (!menu || !trigger) return;
        const selectedValue = input instanceof HTMLInputElement ? input.value : "";
        let selectedOption = null;
        menu.replaceChildren();
        const safeItems = Array.isArray(items) ? items : [];
        safeItems.forEach((item) => {
            const itemValue = item.url || "";
            const itemIcon = combobox === elements.projectCombobox
                ? chatgptProjectIconForItem(item)
                : listIcon;
            const option = sourceOptionButton(
                itemValue,
                item.title || "Untitled",
                itemIcon,
                Boolean(selectedValue && itemValue === selectedValue),
                combobox === elements.projectCombobox ? String(item.icon || "").trim() : "",
                combobox === elements.projectCombobox ? String(item.icon_color || "").trim() : "",
            );
            option.dataset.agentSourceId = item.id || "";
            option.dataset.agentSourceUpdatedAt = item.updated_at || "";
            if (selectedValue && itemValue === selectedValue) selectedOption = option;
            menu.append(option);
        });
        const readyLabel = combobox.dataset.agentSessionList === "projects"
            ? projectChoicePlaceholder()
            : "Choose a recent session";
        const hasOptions = Boolean(menu.querySelector("[data-agent-combobox-option]"));
        if (hasOptions) {
            if (trigger) trigger.disabled = false;
            if (selectedOption) {
                if (input instanceof HTMLInputElement) input.value = selectedValue;
                syncComboboxTriggerFromOption(combobox, selectedOption);
            } else {
                setComboboxValue(combobox, "", readyLabel, listIcon);
            }
        } else {
            if (trigger) trigger.disabled = true;
            setComboboxValue(combobox, "", emptyLabel, listIcon);
        }
        setComboboxLoading(combobox, false);
        updateSessionChoiceInputs();
    }

    function resetProjectSessions(loading = false) {
        projectSessions = [];
        projectSessionsLoading = loading;
        verifiedProjectSessionsKey = "";
        selectedProjectConversationUrl = "new";
        updateSessionChoiceInputs();
    }

    function applySessionModeSelection(mode, {refreshSources = false} = {}) {
        selectedRemoteConversationUrl = "";
        sessionTitleOverride = "";
        resetRemoteSessionHistory();
        if (mode === "new") {
            if (elements.projectUrl instanceof HTMLInputElement) elements.projectUrl.value = "";
            selectedProjectConversationUrl = "new";
            setProjectComboboxValue("", projectChoicePlaceholder());
            resetProjectSessions();
        } else if (mode === "project") {
            selectedProjectConversationUrl = "new";
            resetProjectSessions();
            if (refreshSources) refreshAgentSessionSources();
        }
        updateSessionChoiceInputs();
    }

    function populateProjectSessions(items) {
        projectSessionsLoading = false;
        verifiedProjectSessionsKey = historyUrlKey(selectedProjectUrl());
        projectSessions = Array.isArray(items) ? items : [];
        rememberExecutionTitles(projectSessions);
        updateSessionChoiceInputs();
    }

    function selectSessionListValue(combobox, value, label) {
        if (!combobox || !value) return;
        const input = combobox.querySelector("[data-agent-combobox-input]");
        const menu = combobox.querySelector("[data-agent-combobox-menu]");
        const trigger = combobox.querySelector("[data-agent-combobox-trigger]");
        if (!(input instanceof HTMLInputElement) || !menu || !trigger) return;
        const option = Array.from(menu.querySelectorAll("[data-agent-combobox-option]")).find(
            (candidate) => candidate.dataset.agentComboboxOption === value,
        );
        if (!option) return;
        input.value = value;
        if (label) {
            option.dataset.agentComboboxLabel = label;
            const text = option.querySelector(".trade-strategy-dropdown-text");
            if (text) text.textContent = label;
        }
        syncComboboxTriggerFromOption(combobox, option);
        setComboboxLoading(combobox, false);
        if (trigger) trigger.disabled = false;
    }

    function restoreRememberedSessionSelection() {
        if (executionSessionId !== "new" && lastPayload.agent?.session_id === executionSessionId
            && lastPayload.agent?.workspace_path) {
            restoreExecutionConfiguration(lastPayload.agent, {force: true});
            return;
        }
        if (catalogState !== "ready") return;
        const cacheKey = sessionSelectionCacheKey();
        if (sessionSelectionRestoredKey === cacheKey) return;
        sessionSelectionRestoredKey = cacheKey;
        const remembered = readRememberedSessionSelection();
        if (!remembered || remembered.mode === "new") return;

        const modeInput = elements.sessionMode;
        const modeOption = Array.from(
            elements.sessionModeCombobox?.querySelectorAll("[data-agent-combobox-option]") || [],
        ).find((option) => option.dataset.agentComboboxOption === remembered.mode);
        if (!(modeInput instanceof HTMLInputElement) || !modeOption) return;
        modeInput.value = remembered.mode;
        syncComboboxTriggerFromOption(elements.sessionModeCombobox, modeOption);
        applySessionModeSelection(remembered.mode);

        if (!remembered.project_url) return;
        const projectOption = Array.from(
            elements.projectCombobox?.querySelectorAll("[data-agent-combobox-option]") || [],
        ).find((option) => historyUrlKey(option.dataset.agentComboboxOption)
            === historyUrlKey(remembered.project_url));
        if (!projectOption) return;
        const currentProjectUrl = projectOption.dataset.agentComboboxOption;
        selectSessionListValue(
            elements.projectCombobox,
            currentProjectUrl,
            projectOption.dataset.agentComboboxLabel || "",
        );
        if (elements.projectUrl instanceof HTMLInputElement) elements.projectUrl.value = currentProjectUrl;
        resetProjectSessions(true);
        updateSessionChoiceInputs();
        void restoreRememberedProjectSession(remembered, currentProjectUrl);
    }

    async function restoreRememberedProjectSession(remembered, projectUrl) {
        const loaded = await loadProjectSessions(projectUrl);
        if (loaded !== true
            || historyUrlKey(selectedProjectUrl()) !== historyUrlKey(projectUrl)) return;
        const sessionUrl = remembered.project_session_url;
        if (sessionUrl && sessionUrl !== "new") {
            const session = projectSessions.find((item) => historyUrlKey(item.url) === historyUrlKey(sessionUrl));
            if (session) {
                selectedProjectConversationUrl = session.url;
                sessionTitleOverride = session.title || "";
                updateSessionChoiceInputs();
                void loadSelectedSessionHistory(session.url);
            }
        }
        rememberSessionSelection();
        render(lastPayload);
    }

    function projectNameFromPath(path) {
        const normalizedPath = String(path || "").replace(/[\\/]+$/, "");
        return normalizedPath.split(/[\\/]/).pop() || normalizedPath;
    }

    function syncProjectPath(path) {
        const normalizedPath = String(path || "").trim();
        if (!normalizedPath) return;
        if (elements.workspacePath instanceof HTMLInputElement) elements.workspacePath.value = normalizedPath;
        if (elements.projectName) elements.projectName.textContent = projectNameFromPath(normalizedPath);
    }

    function syncExecutionChoices() {
        if (elements.promptOs instanceof HTMLInputElement) elements.promptOs.value = selectedOs();
        if (elements.promptPlatform instanceof HTMLInputElement) elements.promptPlatform.value = selectedPlatform();
        if (elements.promptBrowser instanceof HTMLInputElement) elements.promptBrowser.value = selectedBrowser();
        if (elements.modelInput instanceof HTMLInputElement) elements.modelInput.value = selectedModel();
        if (elements.effortInput instanceof HTMLInputElement) elements.effortInput.value = selectedChatgptEffort();
    }

    function preferencePayload() {
        return {
            workspace_path: elements.workspacePath?.value || "",
            operating_system: selectedOs(),
            platform: selectedPlatform(),
            browser: selectedBrowser(),
            model: selectedModel(),
            chatgpt_effort: selectedChatgptEffort(),
        };
    }

    function schedulePreferenceSave() {
        if (preferenceTimer !== null) window.clearTimeout(preferenceTimer);
        preferenceTimer = window.setTimeout(async () => {
            preferenceTimer = null;
            try {
                await requestJson("/api/agent/preferences", {
                    method: "POST",
                    body: JSON.stringify(preferencePayload()),
                });
            } catch (error) {
                setResponseStatusFallback(error.message);
            }
        }, 250);
    }

    function initializeComboboxes() {
        const comboboxes = Array.from(document.querySelectorAll("[data-agent-combobox]"));
        const closeCombobox = (combobox) => {
            const trigger = combobox.querySelector("[data-agent-combobox-trigger]");
            const menu = combobox.querySelector("[data-agent-combobox-menu]");
            combobox.classList.remove("is-agent-combobox-open");
            trigger?.setAttribute("aria-expanded", "false");
            if (menu) menu.hidden = true;
        };
        const toggleCombobox = (combobox) => {
            comboboxes.forEach((other) => {
                if (other !== combobox) closeCombobox(other);
            });
            const trigger = combobox.querySelector("[data-agent-combobox-trigger]");
            const menu = combobox.querySelector("[data-agent-combobox-menu]");
            const isOpen = combobox.classList.contains("is-agent-combobox-open");
            combobox.classList.toggle("is-agent-combobox-open", !isOpen);
            trigger?.setAttribute("aria-expanded", String(!isOpen));
            if (menu) menu.hidden = isOpen;
        };

        comboboxes.forEach((combobox) => {
            const input = combobox.querySelector("[data-agent-combobox-input]");
            const trigger = combobox.querySelector("[data-agent-combobox-trigger]");
            const menu = combobox.querySelector("[data-agent-combobox-menu]");
                if (!(input instanceof HTMLInputElement) || !trigger || !menu) return;
            const closeComboboxForSelection = () => {
                closeCombobox(combobox);
            };
            trigger?.addEventListener("click", () => {
                if (combobox === elements.projectCombobox) {
                    refreshAgentSessionSources();
                }
                toggleCombobox(combobox);
            });
            const selectOption = (option) => {
                const isSessionSource = combobox === elements.sessionModeCombobox
                    || combobox === elements.projectCombobox;
                if (isSessionSource && executionSessionId !== "new") {
                    // Explicit source selection starts a separate task; typing alone continues the selected one.
                    void selectExecutionSession("new", {preserveSourceSelection: true});
                }
                input.value = option.dataset.agentComboboxOption || "";
                if (combobox === elements.effortCombobox) effortSelectionTouched = true;
                if (combobox === elements.modelCombobox) preferredModel = input.value;
                syncComboboxTriggerFromOption(combobox, option);
                closeComboboxForSelection();
                normalizeAgentSelection();
                syncExecutionChoices();
                const isRouteSelection = combobox.classList.contains("agent-platform-combobox")
                    || combobox.classList.contains("agent-browser-combobox");
                if (combobox.classList.contains("agent-platform-combobox")) {
                    sessionTitleOverride = "";
                    resetRemoteSessionHistory();
                    sourceBrowser = "";
                    sourcesLoaded = false;
                    sourceRequestId += 1;
                    appliedBootstrapSignature = "";
                    projectSessionRequestId += 1;
                    agentSources = {recent_sessions: [], projects: []};
                    if (elements.projectUrl instanceof HTMLInputElement) elements.projectUrl.value = "";
                    selectedProjectConversationUrl = "new";
                    resetProjectSessions();
                    setProjectComboboxValue("", projectCollectionLabel());
                    setComboboxLoading(elements.projectCombobox, true);
                    syncPlatformState();
                }
                if (combobox.classList.contains("agent-browser-combobox")) {
                    sessionTitleOverride = "";
                    resetRemoteSessionHistory();
                    sourceBrowser = "";
                    sourcesLoaded = false;
                    appliedBootstrapSignature = "";
                    projectSessionRequestId += 1;
                    agentSources = {recent_sessions: [], projects: []};
                    if (elements.projectUrl instanceof HTMLInputElement) elements.projectUrl.value = "";
                    selectedProjectConversationUrl = "new";
                    resetProjectSessions();
                    setProjectComboboxValue("", projectCollectionLabel());
                    setComboboxLoading(elements.projectCombobox, true);
                    browserStatusController?.setBrowser(selectedBrowser());
                }
                if (isRouteSelection) syncAgentRoute();
                if (combobox.classList.contains("agent-session-mode-combobox")) {
                    applySessionModeSelection(input.value, {refreshSources: true});
                }
                if (combobox === elements.projectCombobox) {
                    sessionTitleOverride = "";
                    resetRemoteSessionHistory();
                    if (elements.projectUrl instanceof HTMLInputElement) elements.projectUrl.value = input.value;
                    resetProjectSessions(true);
                    loadProjectSessions(input.value);
                    updateSessionChoiceInputs();
                }
                if (
                    combobox.classList.contains("agent-session-mode-combobox")
                    || combobox === elements.projectCombobox
                ) {
                    rememberSessionSelection();
                }
                schedulePreferenceSave();
                render(lastPayload);
            };
            menu.addEventListener("click", (event) => {
                if (!(event.target instanceof Element)) return;
                const option = event.target.closest("[data-agent-combobox-option]");
                if (option && menu.contains(option)) {
                    selectOption(option);
                }
            });
        });

        document.addEventListener("click", (event) => {
            if (!(event.target instanceof Element) || event.target.closest("[data-agent-combobox]")) return;
            comboboxes.forEach(closeCombobox);
        });
        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape") comboboxes.forEach(closeCombobox);
        });
    }

    async function loadProjectSessions(projectUrl, options = {}) {
        if (!projectUrl) return false;
        const requestId = ++projectSessionRequestId;
        const projectKey = historyUrlKey(projectUrl);
        try {
            const query = new URLSearchParams({
                platform: selectedPlatform(),
                browser: selectedBrowser(),
                project_url: projectUrl,
            });
            if (options.forceRefresh) query.set("refresh", "1");
            const payload = await requestJson(`/api/agent/project-sessions?${query.toString()}`);
            if (requestId !== projectSessionRequestId
                || projectKey !== historyUrlKey(selectedProjectUrl())) return false;
            populateProjectSessions(payload.sessions || []);
            // Show durable cached choices immediately, then revalidate only an expired catalog.
            if (payload.cache?.status === "stale" && !options.forceRefresh) {
                loadProjectSessions(projectUrl, {forceRefresh: true});
            }
            return true;
        } catch (_error) {
            if (requestId !== projectSessionRequestId
                || projectKey !== historyUrlKey(selectedProjectUrl())) return false;
            resetProjectSessions();
            return false;
        }
    }

    async function loadSelectedSessionHistory(conversationUrl) {
        const selectedUrl = String(conversationUrl || "").trim();
        const platform = selectedPlatform();
        const supportsHistory = (platform === "chatgpt" && isChatgptConversationUrl(selectedUrl))
            || (platform === "grok" && isAgentConversationUrl(platform, selectedUrl));
        if (!supportsHistory) {
            resetRemoteSessionHistory();
            return;
        }
        const browserName = selectedBrowser();
        if (
            remoteHistoryMatchesSelection()
            && (remoteSessionHistoryLoading || !remoteSessionHistoryError)
        ) {
            return;
        }

        const requestId = ++remoteSessionHistoryRequestId;
        remoteSessionHistory = [];
        remoteSessionHistoryPlatform = platform;
        remoteSessionHistoryUrl = selectedUrl;
        remoteSessionHistoryBrowser = browserName;
        remoteSessionHistoryLoading = true;
        remoteSessionHistoryError = "";
        responseHistorySignature = "";
        responseHistoryPage = 1;
        render(lastPayload);

        try {
            const query = new URLSearchParams({browser: browserName, conversation_url: selectedUrl});
            const historyEndpoint = platform === "grok"
                ? "/api/agent/grok-session-history"
                : "/api/agent/chatgpt-session-history";
            const payload = await requestJson(`${historyEndpoint}?${query.toString()}`);
            if (requestId !== remoteSessionHistoryRequestId || !remoteHistoryMatchesSelection()) return;
            remoteSessionHistory = Array.isArray(payload.history)
                ? payload.history.filter((item) => item && item.prompt && item.response)
                : [];
            remoteSessionHistoryLoading = false;
            remoteSessionHistoryError = "";
            if (payload.title && remoteHistoryMatchesSelection()) {
                sessionTitleOverride = String(payload.title).trim();
            }
            responseHistorySignature = "";
            responseHistoryPage = Math.max(remoteSessionHistory.length, 1);
            render(lastPayload);
        } catch (error) {
            if (requestId !== remoteSessionHistoryRequestId) return;
            remoteSessionHistoryLoading = false;
            remoteSessionHistoryError = error.message
                || `Could not load the selected ${selectedPlatformLabel()} session history.`;
            responseHistorySignature = "";
            responseHistoryPage = 1;
            render(lastPayload);
        }
    }

    function applyAgentSources(payload) {
        const sourcePayload = payload && typeof payload === "object"
            ? payload
            : {recent_sessions: [], projects: []};
        agentSources = sourcePayload;
        rememberExecutionTitles(sourcePayload.recent_sessions);
        catalogState = "ready";
        catalogError = "";
        populateListCombobox(
            elements.projectCombobox,
            sourcePayload.projects,
            `No ${projectCollectionLabel().toLowerCase()} found`,
        );
        sourcesLoaded = true;
        clearCatalogLoadingState();
        renderRecentSessionList();
        restoreRememberedSessionSelection();
    }

    function applyAgentSourcesError(message) {
        agentSources = {recent_sessions: [], projects: []};
        catalogState = "error";
        catalogError = message || "Could not load Recent sessions";
        setProjectComboboxValue("", `${projectCollectionLabel()} are unavailable`);
        sourcesLoaded = true;
        clearCatalogLoadingState();
        restoreExecutionConfiguration(lastPayload.agent, {force: true});
    }

    function setCatalogControlsLoading(loading) {
        [elements.projectCombobox].forEach((combobox) => {
            if (!combobox) return;
            const trigger = combobox.querySelector("[data-agent-combobox-trigger]");
            if (trigger && loading) trigger.disabled = true;
            setComboboxLoading(combobox, loading);
        });
    }

    function syncSessionListControls(running = Boolean(lastPayload.agent?.running)) {
        [elements.projectCombobox].forEach((combobox) => {
            const trigger = combobox?.querySelector("[data-agent-combobox-trigger]");
            if (!trigger) return;
            const hasOptions = Boolean(combobox.querySelector("[data-agent-combobox-option]"));
            const canRetryCatalog = catalogState === "error";
            trigger.disabled = running
                || sourcesLoading
                || (!hasOptions && !canRetryCatalog);
        });
    }

    function clearCatalogLoadingState() {
        sourcesLoading = false;
        setCatalogControlsLoading(false);
        syncSessionListControls();
    }

    async function loadAgentSources(options = {}) {
        const forceRefresh = Boolean(options.forceRefresh);
        if (!lastBrowserStatus?.can_download || !selectedBrowser()) return;
        const browserName = selectedBrowser();
        const platform = selectedPlatform();
        const platformLabel = selectedPlatformLabel();
        const bootstrappedSources = lastBrowserStatus?.agent_sources;
        const bootstrappedError = lastBrowserStatus?.agent_sources_error;
        const supportsBootstrap = BOOTSTRAPPED_SOURCE_PLATFORMS.has(platform);
        if (!forceRefresh && supportsBootstrap && (bootstrappedSources || bootstrappedError)) {
            const bootstrapKind = bootstrappedSources ? "sources" : "error";
            const bootstrapValue = bootstrappedSources || String(bootstrappedError || "");
            const bootstrapSignature = `${browserName}|${platform}|${bootstrapKind}|${JSON.stringify(bootstrapValue)}`;
            if (bootstrapSignature !== appliedBootstrapSignature) {
                if (catalogAbort) {
                    catalogAbort.abort();
                    catalogAbort = null;
                }
                sourceRequestId += 1;
                sourceBrowser = browserName;
                sourcePlatform = platform;
                appliedBootstrapSignature = bootstrapSignature;
                if (bootstrappedSources) applyAgentSources(bootstrappedSources);
                else applyAgentSourcesError(String(bootstrappedError));
            }
            return;
        }
        if (
            !forceRefresh
            && sourcesLoading
            && sourceBrowser === browserName
            && sourcePlatform === platform
        ) return;
        if (
            !forceRefresh
            && sourcesLoaded
            && sourceBrowser === browserName
            && sourcePlatform === platform
        ) return;
        sourceBrowser = browserName;
        sourcePlatform = platform;
        if (catalogAbort) catalogAbort.abort();
        const requestController = new AbortController();
        catalogAbort = requestController;
        const requestId = ++sourceRequestId;
        catalogState = "loading";
        catalogError = "";
        sourcesLoading = true;
        renderRecentSessionList();
        if (elements.projectCombobox) {
            const trigger = elements.projectCombobox.querySelector("[data-agent-combobox-trigger]");
            const input = elements.projectCombobox.querySelector("[data-agent-combobox-input]");
            if (trigger) trigger.disabled = true;
            if (!(input instanceof HTMLInputElement) || !input.value) {
                setProjectComboboxValue("", projectCollectionLabel());
            }
            setComboboxLoading(elements.projectCombobox, true);
        }
        const timeoutId = window.setTimeout(() => requestController.abort(), CATALOG_TIMEOUT_MS);
        try {
            const query = new URLSearchParams({platform, browser: browserName});
            if (forceRefresh) query.set("refresh", "1");
            const response = await fetch(`/api/agent/sources?${query.toString()}`, {
                cache: "no-store",
                signal: requestController.signal,
                headers: {"Content-Type": "application/json", "Accept": "application/json"},
            });
            if (
                requestId !== sourceRequestId
                || browserName !== selectedBrowser()
                || platform !== selectedPlatform()
            ) {
                return;
            }
            let payload;
            try {
                payload = await response.json();
            } catch (_jsonError) {
                throw new Error("The server returned a malformed catalog response.");
            }
            if (!response.ok) {
                throw new Error(payload.error || `Could not load ${platformLabel} sessions`);
            }
            applyAgentSources(payload);
        } catch (error) {
            if (requestId !== sourceRequestId) {
                return;
            }
            if (error && error.name === "AbortError") {
                applyAgentSourcesError("Recent sessions timed out after 15 seconds.");
            } else {
                applyAgentSourcesError(error.message || `Could not load ${platformLabel} sessions`);
            }
        } finally {
            window.clearTimeout(timeoutId);
            if (requestId === sourceRequestId) clearCatalogLoadingState();
        }
    }

    function refreshAgentSessionSources() {
        const hasBootstrap = Boolean(lastBrowserStatus)
            && (
                Object.prototype.hasOwnProperty.call(lastBrowserStatus, "agent_sources")
                || Object.prototype.hasOwnProperty.call(lastBrowserStatus, "agent_sources_error")
            );
        if (hasBootstrap && browserStatusController?.refresh) {
            void browserStatusController.refresh();
            return;
        }
        void loadAgentSources({forceRefresh: true});
    }

    function bindCompletedAgentSession(agent, completedTransition) {
        const platform = String(agent?.platform || selectedPlatform()).trim().toLowerCase();
        if (!completedTransition || platform !== selectedPlatform() || agent?.running) return;
        // A completion transition must not start another browser collection.
        automaticSourcesSuppressedAfterCompletion = true;
        const conversationUrl = String(agent?.conversation_url || "").trim();
        if (!isAgentConversationUrl(platform, conversationUrl)) return;
        const signature = `${agent.started_at || ""}|${agent.finished_at || ""}|${conversationUrl}`;
        if (!agent.finished_at || signature === boundAgentSessionSignature) return;
        boundAgentSessionSignature = signature;
    }

    function browserVerificationPending() {
        return !lastBrowserStatus || ["loading", "refreshing"].includes(browserStatusState);
    }

    function readinessState(payload) {
        const platformLabel = selectedPlatformLabel();
        const runtime = payload.runtime || {};
        const hostOperatingSystem = runtime.host_operating_system || "";
        if (hostOperatingSystem && selectedOs() !== hostOperatingSystem) {
            const hostLabel = hostOperatingSystem === "macos" ? "this macOS host" : "this Windows host";
            return {ready: false, message: `The selected operating system is not available on ${hostLabel}.`};
        }
        if (!runtime.ready) {
            return {
                ready: false,
                message: runtime.message || "Computer Use is not ready on this host.",
            };
        }
        if (browserVerificationPending()) {
            return {
                ready: false,
                message: `Checking the signed-in ${platformLabel} account in ${selectedBrowserLabel()}...`,
            };
        }
        if (!lastBrowserStatus.can_download) {
            return {
                ready: false,
                message: lastBrowserStatus.message || `${selectedBrowserLabel()} is not signed in to ${platformLabel} Web.`,
            };
        }
        return {
            ready: true,
            message: lastBrowserStatus.message || `${selectedBrowserLabel()} is ready for ${platformLabel} Web.`,
        };
    }

    function responseStatusPresentation(agent, readiness) {
        const phase = String(agent?.phase || "").trim().toLowerCase();
        const running = Boolean(agent?.running);
        const hasAgentRun = running
            || Boolean(agent?.started_at)
            || Boolean(agent?.finished_at)
            || Boolean(agent?.response)
            || (Array.isArray(agent?.activity) && agent.activity.length > 0)
            || ["starting", "preparing", "submitting", "running", "finalizing", "paused"].includes(phase);
        const sessionMessage = remoteSessionHistoryLoading
            ? `Loading the selected ${selectedPlatformLabel()} session history…`
            : remoteSessionHistoryError;
        const pauseCopy = agent?.paused
            ? (agent.pause_reason || agent.message || "The Web Agent is paused.")
            : "";
        const message = sessionMessage
            || pauseCopy
            || (hasAgentRun ? String(agent?.message || "").trim() : "")
            || String(readiness.message || "").trim()
            || "Ready to use a signed-in Web AI session.";
        let status = "ready";
        let phaseLabel = "Ready";
        if (remoteSessionHistoryLoading) {
            status = "loading";
            phaseLabel = "Loading";
        } else if (remoteSessionHistoryError) {
            status = "failed";
            phaseLabel = "History unavailable";
        } else if (running && agent?.paused) {
            status = "paused";
            phaseLabel = "Paused";
        } else if (running && phase === "reconnecting") {
            status = "reconnecting";
            phaseLabel = "Reconnecting";
        } else if (running) {
            status = "running";
            phaseLabel = "Working";
        } else if (phase === "finished") {
            status = "finished";
            phaseLabel = "Finished";
        } else if (phase === "stopped") {
            status = "stopped";
            phaseLabel = "Stopped";
        } else if (phase === "failed") {
            status = "failed";
            phaseLabel = "Failed";
        } else if (phase === "interrupted") {
            status = "interrupted";
            phaseLabel = "Interrupted";
        } else if (!readiness.ready) {
            const verificationPending = browserVerificationPending();
            status = verificationPending ? "loading" : "failed";
            phaseLabel = verificationPending ? "Checking" : "Unavailable";
        }
        const runningCopy = status === "running"
            ? runningResponseStatusCopy(agent, message)
            : null;
        const copy = runningCopy?.text || `${phaseLabel} · ${message}`;
        return {
            status,
            copy,
            lines: runningCopy?.lines || null,
            loading: ["loading", "running", "reconnecting"].includes(status),
        };
    }

    function formatElapsedDuration(startedAt) {
        const startMilliseconds = Date.parse(String(startedAt || ""));
        if (!Number.isFinite(startMilliseconds)) return "";
        const elapsedSeconds = Math.max(0, Math.floor((Date.now() - startMilliseconds) / 1000));
        const hours = Math.floor(elapsedSeconds / 3_600);
        const minutes = Math.floor((elapsedSeconds % 3_600) / 60);
        const seconds = elapsedSeconds % 60;
        return [hours, minutes, seconds]
            .map((value) => String(value).padStart(2, "0"))
            .join(":");
    }

    function agentTurnCount(agent) {
        const rawCount = Number(agent?.turn_count);
        if (Number.isFinite(rawCount) && rawCount >= 0) return Math.floor(rawCount);
        const activity = Array.isArray(agent?.activity) ? agent.activity : [];
        return activity.length || null;
    }

    function runningResponseStatusCopy(agent, message) {
        const metrics = [];
        const elapsed = formatElapsedDuration(agent?.started_at);
        if (elapsed) metrics.push(elapsed);
        const turnCount = agentTurnCount(agent);
        if (turnCount !== null) metrics.push(`${turnCount.toLocaleString("en-US")} turns`);
        const summary = ["Working", ...metrics].filter(Boolean).join(" · ");
        const detail = String(message || "").trim();
        return {
            text: [summary, detail].filter(Boolean).join(" · "),
            lines: [summary, detail].filter(Boolean),
        };
    }

    function renderResponseStatusCopy(presentation) {
        if (!elements.statusMessageCopy) return;
        if (presentation.lines?.length === 2) {
            const summary = document.createElement("span");
            summary.dataset.agentResponseStatusLeading = "";
            summary.textContent = presentation.lines[0];
            const detail = document.createElement("span");
            detail.dataset.agentResponseStatusDetail = "";
            detail.textContent = presentation.lines[1];
            elements.statusMessageCopy.replaceChildren(summary, document.createElement("br"), detail);
            return;
        }
        elements.statusMessageCopy.textContent = presentation.copy;
    }

    function stopResponseStatusTimer() {
        if (responseStatusTimer === null) return;
        window.clearInterval(responseStatusTimer);
        responseStatusTimer = null;
    }

    function syncResponseStatusTimer(agent, status) {
        if (status !== "running" || !formatElapsedDuration(agent?.started_at)) {
            stopResponseStatusTimer();
            return;
        }
        if (responseStatusTimer !== null) return;
        responseStatusTimer = window.setInterval(() => {
            if (!lastPayload.agent?.running) {
                stopResponseStatusTimer();
                return;
            }
            renderResponseStatus(lastPayload.agent, readinessState(lastPayload));
        }, 1_000);
    }

    function renderResponseStatus(agent, readiness) {
        if (!elements.statusMessage) return;
        const presentation = responseStatusPresentation(agent, readiness);
        elements.statusMessage.hidden = false;
        elements.statusMessage.dataset.status = presentation.status;
        elements.statusMessage.setAttribute("aria-label", presentation.copy);
        elements.statusMessage.title = presentation.copy;
        renderResponseStatusCopy(presentation);
        if (elements.statusDot) elements.statusDot.hidden = presentation.loading;
        if (elements.statusSpinner) {
            elements.statusSpinner.hidden = !presentation.loading;
            elements.statusSpinner.classList.toggle("cache-phase-live-marker", presentation.status === "running");
            elements.statusSpinner.classList.toggle("suggestion-loading-spinner", presentation.status !== "running");
        }
        syncResponseStatusTimer(agent, presentation.status);
    }

    function setResponseStatusFallback(message) {
        if (!elements.statusMessage) return;
        stopResponseStatusTimer();
        const copy = String(message || "The Agent request failed.").trim();
        elements.statusMessage.hidden = false;
        elements.statusMessage.dataset.status = "failed";
        elements.statusMessage.setAttribute("aria-label", `Failed · ${copy}`);
        elements.statusMessage.title = copy;
        if (elements.statusMessageCopy) elements.statusMessageCopy.textContent = `Failed · ${copy}`;
        if (elements.statusDot) elements.statusDot.hidden = false;
        if (elements.statusSpinner) elements.statusSpinner.hidden = true;
    }

    function formatActivityTimestamp(timestamp) {
        const parsed = new Date(String(timestamp || ""));
        if (!Number.isFinite(parsed.getTime())) return "";
        try {
            return new Intl.DateTimeFormat("en-GB", {
                timeZone: "Asia/Hong_Kong",
                hour: "2-digit",
                minute: "2-digit",
                second: "2-digit",
                hour12: false,
            }).format(parsed);
        } catch (_error) {
            return "";
        }
    }

    function activityMeta(event) {
        const meta = String(event?.meta || "").trim();
        const timestamp = formatActivityTimestamp(event?.timestamp);
        if (!timestamp) return meta;
        return meta ? `${meta} · ${timestamp}` : timestamp;
    }

    function initializeBrowserSessionStatus() {
        if (!elements.browserSession || !window.CACHELIKES_BROWSER_SESSION_STATUS?.init) return;
        browserStatusController = window.CACHELIKES_BROWSER_SESSION_STATUS.init(elements.browserSession, {
            platform: selectedPlatform(),
            getBrowser: selectedBrowser,
            onStateChange(payload, browserId, state) {
                browserStatusState = String(state || "cleared");
                lastBrowserStatus = state === "cleared"
                    ? null
                    : {...(payload || {}), browser: browserId};
                render(lastPayload);
            },
        });
    }

    function renderActivity(events, running, finishedTransition = false, runIdentity = "") {
        if (!elements.activityPanel || !elements.activityList || !elements.activityCount) return;
        const safeEvents = Array.isArray(events) ? events : [];
        const signature = JSON.stringify(safeEvents);
        const changed = signature !== activitySignature;
        const followLatest = elements.activityList.scrollHeight - elements.activityList.scrollTop
            - elements.activityList.clientHeight < 24;
        if (changed) {
            activitySignature = signature;
            elements.activityList.replaceChildren(...safeEvents.map((event) => {
                const item = document.createElement("li");
                item.className = "agent-activity-item";
                item.dataset.status = event.status === "complete" ? "completed" : (event.status || "running");

                const status = document.createElement("span");
                status.className = "agent-activity-status";
                status.setAttribute("aria-hidden", "true");
                const live = document.createElement("span");
                live.className = "cache-phase-live-marker";
                status.append(live);

                const content = document.createElement("span");
                content.className = "agent-activity-content";
                const label = document.createElement("span");
                label.className = "agent-activity-label";
                label.textContent = event.label || "Working";
                const detail = document.createElement("span");
                detail.className = "agent-activity-detail";
                detail.textContent = event.detail || "";
                content.append(label, detail);

                const meta = document.createElement("span");
                meta.className = "agent-activity-meta";
                meta.textContent = activityMeta(event);
                item.append(status, content, meta);
                return item;
            }));
        }
        elements.activityPanel.hidden = false;
        elements.activityCount.textContent = String(safeEvents.length);
        if (running && safeEvents.length && (!activityWasRunning || runIdentity !== activityRunIdentity)) {
            activityCloseAnimation?.cancel();
            activityCloseAnimation = null;
            elements.activityPanel.open = true;
        } else if (finishedTransition) {
            activityCloseAnimation?.cancel();
            activityCloseAnimation = null;
            elements.activityPanel.open = false;
        }
        activityWasRunning = running;
        activityRunIdentity = runIdentity;
        elements.activityPanel.dataset.running = String(running);
        if (changed) {
            const current = elements.activityList.lastElementChild;
            activityCurrent.replaceChildren(...(current ? [current.cloneNode(true)] : []));
            if (elements.activityPanel.open && followLatest) {
                elements.activityList.scrollTop = elements.activityList.scrollHeight;
            }
        }
        syncActivityCurrent();
    }

    function renderErrorRecord(agent) {
        if (!elements.errorRecord || !elements.errorRecordContent) return;
        const errorText = String(agent?.error_traceback || agent?.last_error || "");
        const changed = elements.errorRecordContent.textContent !== errorText;
        if (changed) {
            elements.errorRecordContent.textContent = errorText;
            if (errorText) elements.errorRecord.open = true;
        }
        elements.errorRecord.hidden = !errorText;
    }

    function renderDoctor(payload = doctorPayload) {
        if (!elements.doctorPanel) return;
        if (!payload) {
            elements.doctorPanel.hidden = true;
            return;
        }
        const status = String(payload.status || "attention");
        const statusLabel = status === "healthy"
            ? "Healthy"
            : status === "blocked"
                ? "Blocked"
                : "Needs attention";
        const events = Array.isArray(payload.events) ? payload.events : [];
        elements.doctorPanel.hidden = status === "healthy" && events.length === 0;
        elements.doctorPanel.dataset.status = status;
        if (elements.doctorStatus) elements.doctorStatus.textContent = statusLabel;
        if (elements.doctorSummary) {
            elements.doctorSummary.textContent = String(
                payload.summary || "Agent diagnostics are unavailable.",
            );
        }
        if (elements.doctorChecks) {
            const checks = Array.isArray(payload.checks) ? payload.checks : [];
            elements.doctorChecks.replaceChildren(...checks.map((check) => {
                const item = document.createElement("li");
                item.dataset.status = String(check.status || "info");
                const label = document.createElement("strong");
                label.textContent = String(check.label || "Diagnostic");
                const detail = document.createElement("span");
                detail.textContent = String(check.detail || "");
                item.append(label, detail);
                return item;
            }));
        }
        if (elements.doctorEvents) {
            elements.doctorEvents.replaceChildren(...events.map((event) => {
                const item = document.createElement("li");
                item.dataset.status = String(event.status || "info");
                const label = document.createElement("strong");
                const sequence = Number.isFinite(Number(event.sequence))
                    ? `#${Number(event.sequence)}`
                    : "Event";
                const actionId = String(event.action_id || "").trim();
                label.textContent = `${sequence} ${String(event.kind || "event")}`
                    + (actionId ? ` · ${actionId}` : "");
                const detail = document.createElement("span");
                detail.textContent = String(event.detail || event.status || "");
                item.append(label, detail);
                return item;
            }));
        }
        if (elements.doctorActions) {
            const actions = Array.isArray(payload.actions)
                ? payload.actions.filter((action) => action && action.enabled)
                : [];
            elements.doctorActions.replaceChildren(...actions.map((action) => {
                const button = document.createElement("button");
                button.type = "button";
                button.className = "secondary-button agent-doctor-action";
                button.dataset.agentDoctorAction = String(action.id || "");
                button.textContent = String(action.label || "Recover");
                button.title = String(action.description || "");
                return button;
            }));
        }
    }

    async function loadDoctor() {
        if (doctorLoading) return;
        doctorLoading = true;
        const requestId = ++doctorRequestId;
        const runId = lastPayload.agent?.run_id || "";
        try {
            const payload = await requestJson("/api/agent/doctor");
            if (requestId !== doctorRequestId) return;
            if (runId !== (lastPayload.agent?.run_id || "")
                || (payload.run_id && payload.run_id !== runId)
                || !agentNeedsDoctor(lastPayload.agent)) return;
            doctorPayload = payload;
            renderDoctor(payload);
        } catch (error) {
            if (requestId !== doctorRequestId) return;
            if (runId !== (lastPayload.agent?.run_id || "") || !agentNeedsDoctor(lastPayload.agent)) return;
            doctorPayload = {
                status: "attention",
                summary: error.message || "Agent diagnostics could not be loaded.",
                checks: [],
                actions: [],
            };
            renderDoctor(doctorPayload);
        } finally {
            doctorLoading = false;
        }
    }

    function agentNeedsDoctor(agent) {
        return ["failed", "interrupted"].includes(String(agent?.phase || ""))
            || Boolean(agent?.paused)
            || ["invalid", "degraded"].includes(String(agent?.event_chain_state || ""))
            || (Boolean(agent?.context_file) && !agent?.running && agent?.phase !== "finished");
    }

    function normalizePaginationPage(value, fallback = 1) {
        const numericValue = Number(value);
        if (!Number.isFinite(numericValue)) return fallback;
        return Math.max(1, Math.trunc(numericValue));
    }

    function buildAgentPaginationItems(totalPages, currentPage) {
        if (totalPages <= 1) return [];
        const chunkSize = 5;
        const startPage = Math.floor((currentPage - 1) / chunkSize) * chunkSize + 1;
        const endPage = Math.min(startPage + chunkSize - 1, totalPages);
        const items = [];
        if (startPage > 1) {
            items.push({kind: "previous", page: startPage - 1});
            items.push({kind: "page", page: 1});
            items.push({
                kind: "ellipsis",
                position: "leading",
                firstPage: 1,
                lastPage: startPage - 1,
            });
        }
        for (let page = startPage; page <= endPage; page += 1) {
            items.push({kind: "page", page, isActive: page === currentPage});
        }
        if (endPage < totalPages) {
            items.push({
                kind: "ellipsis",
                position: "trailing",
                firstPage: endPage + 1,
                lastPage: totalPages,
            });
            items.push({kind: "page", page: totalPages});
            items.push({kind: "next", page: endPage + 1});
        }
        return items;
    }

    function buildAgentPaginationRanges(firstPage, lastPage, chunkSize = 5) {
        if (firstPage > lastPage) return [];
        const ranges = [];
        for (let startPage = firstPage; startPage <= lastPage; startPage += chunkSize) {
            ranges.push([startPage, Math.min(startPage + chunkSize - 1, lastPage)]);
        }
        const lastRange = ranges[ranges.length - 1];
        if (
            ranges.length > 1
            && lastRange[1] - lastRange[0] + 1 < chunkSize
        ) {
            ranges[ranges.length - 2][1] = lastRange[1];
            ranges.pop();
        }
        return ranges;
    }

    function agentPaginationRangeElements(picker) {
        return {
            trigger: picker?.querySelector("[data-pagination-range-trigger]") || null,
            menu: picker?.querySelector("[data-pagination-range-menu]") || null,
        };
    }

    function agentPaginationRangePickers() {
        return elements.responsePagination
            ? Array.from(elements.responsePagination.querySelectorAll(".browser-pagination-range-picker"))
            : [];
    }

    function positionAgentPaginationRangeMenu(picker) {
        const {menu} = agentPaginationRangeElements(picker);
        if (!menu || !picker.classList.contains("is-open")) return;
        menu.classList.remove("is-below");
        menu.style.removeProperty("--pagination-range-menu-shift-x");
        menu.style.removeProperty("--pagination-range-menu-max-height");
        const pickerRect = picker.getBoundingClientRect();
        const viewportInset = 12;
        const menuGap = 8;
        const spaceAbove = Math.max(96, pickerRect.top - viewportInset - menuGap);
        const spaceBelow = Math.max(96, window.innerHeight - pickerRect.bottom - viewportInset - menuGap);
        const grid = menu.querySelector(".browser-pagination-range-grid");
        const style = window.getComputedStyle(menu);
        const paddingTop = Number.parseFloat(style.paddingTop) || 0;
        const paddingBottom = Number.parseFloat(style.paddingBottom) || 0;
        const naturalMenuHeight = (grid?.scrollHeight || 0) + paddingTop + paddingBottom;
        if (naturalMenuHeight > spaceAbove && spaceBelow > spaceAbove) menu.classList.add("is-below");
        const availableHeight = menu.classList.contains("is-below") ? spaceBelow : spaceAbove;
        menu.style.setProperty("--pagination-range-menu-max-height", `${availableHeight}px`);
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

    function setAgentPaginationRangePickerOpen(picker, shouldOpen, {focusFirst = false} = {}) {
        if (!picker) return;
        const {trigger, menu} = agentPaginationRangeElements(picker);
        picker.classList.toggle("is-open", shouldOpen);
        trigger?.setAttribute("aria-expanded", shouldOpen ? "true" : "false");
        menu?.setAttribute("aria-hidden", shouldOpen ? "false" : "true");
        if (!shouldOpen) {
            menu?.classList.remove("is-below", "is-scrollable");
            menu?.style.removeProperty("--pagination-range-menu-shift-x");
            menu?.style.removeProperty("--pagination-range-menu-max-height");
            return;
        }
        agentPaginationRangePickers().forEach((otherPicker) => {
            if (otherPicker !== picker) setAgentPaginationRangePickerOpen(otherPicker, false);
        });
        window.requestAnimationFrame(() => {
            positionAgentPaginationRangeMenu(picker);
            if (focusFirst) menu?.querySelector(".browser-pagination-range-option")?.focus();
        });
    }

    function cancelAgentPaginationRangeClose() {
        if (!agentPaginationRangeCloseTimer) return;
        window.clearTimeout(agentPaginationRangeCloseTimer);
        agentPaginationRangeCloseTimer = 0;
    }

    function scheduleAgentPaginationRangeClose(picker) {
        cancelAgentPaginationRangeClose();
        if (agentPaginationRangePinnedPicker === picker) return;
        agentPaginationRangeCloseTimer = window.setTimeout(() => {
            agentPaginationRangeCloseTimer = 0;
            if (!picker.matches(":hover") && !picker.contains(document.activeElement)) {
                setAgentPaginationRangePickerOpen(picker, false);
            }
        }, 140);
    }

    function bindAgentPaginationRangeInteractions() {
        const pickers = agentPaginationRangePickers();
        pickers.forEach((picker) => {
            const {trigger, menu} = agentPaginationRangeElements(picker);
            picker.addEventListener("pointerenter", () => {
                cancelAgentPaginationRangeClose();
                setAgentPaginationRangePickerOpen(picker, true);
            });
            picker.addEventListener("pointerleave", () => scheduleAgentPaginationRangeClose(picker));
            picker.addEventListener("focusin", () => {
                cancelAgentPaginationRangeClose();
                if (agentPaginationRangeFocusRestore === picker) {
                    agentPaginationRangeFocusRestore = null;
                    return;
                }
                setAgentPaginationRangePickerOpen(picker, true);
            });
            picker.addEventListener("focusout", () => scheduleAgentPaginationRangeClose(picker));
            trigger?.addEventListener("click", () => {
                cancelAgentPaginationRangeClose();
                const shouldPin = agentPaginationRangePinnedPicker !== picker;
                if (agentPaginationRangePinnedPicker && agentPaginationRangePinnedPicker !== picker) {
                    setAgentPaginationRangePickerOpen(agentPaginationRangePinnedPicker, false);
                }
                agentPaginationRangePinnedPicker = shouldPin ? picker : null;
                setAgentPaginationRangePickerOpen(picker, shouldPin || picker.matches(":hover"));
            });
            trigger?.addEventListener("keydown", (event) => {
                if (event.key !== "ArrowDown") return;
                event.preventDefault();
                agentPaginationRangePinnedPicker = picker;
                setAgentPaginationRangePickerOpen(picker, true, {focusFirst: true});
            });
            menu?.addEventListener("keydown", (event) => {
                const options = Array.from(menu.querySelectorAll(".browser-pagination-range-option"));
                const currentIndex = options.indexOf(document.activeElement);
                let nextIndex = currentIndex;
                if (event.key === "ArrowDown" || event.key === "ArrowRight") nextIndex = Math.min(options.length - 1, currentIndex + 1);
                else if (event.key === "ArrowUp" || event.key === "ArrowLeft") nextIndex = Math.max(0, currentIndex - 1);
                else if (event.key === "Home") nextIndex = 0;
                else if (event.key === "End") nextIndex = options.length - 1;
                else return;
                event.preventDefault();
                options[nextIndex]?.focus();
            });
        });
        if (agentPaginationRangeEventsBound) return;
        agentPaginationRangeEventsBound = true;
        document.addEventListener("pointerdown", (event) => {
            const pagination = elements.responsePagination;
            if (pagination?.contains(event.target)) return;
            agentPaginationRangePinnedPicker = null;
            agentPaginationRangePickers().forEach((picker) => setAgentPaginationRangePickerOpen(picker, false));
        });
        document.addEventListener("keydown", (event) => {
            if (event.key !== "Escape") return;
            const openPicker = agentPaginationRangePickers().find((picker) => picker.classList.contains("is-open"));
            if (!openPicker) return;
            event.preventDefault();
            agentPaginationRangePinnedPicker = null;
            const trigger = agentPaginationRangeElements(openPicker).trigger;
            const shouldRestoreFocus = document.activeElement !== trigger;
            setAgentPaginationRangePickerOpen(openPicker, false);
            if (shouldRestoreFocus && trigger) {
                agentPaginationRangeFocusRestore = openPicker;
                trigger.focus();
            } else {
                agentPaginationRangeFocusRestore = null;
            }
        });
        window.addEventListener("resize", () => {
            agentPaginationRangePickers().forEach(positionAgentPaginationRangeMenu);
        }, {passive: true});
    }

    function createAgentPaginationRangePicker(item, pagination) {
        const picker = document.createElement("span");
        picker.className = "local-store-page-ellipsis browser-pagination-range-picker";
        picker.dataset.paginationEllipsis = item.position;

        const direction = item.position === "leading" ? "earlier" : "later";
        const trigger = document.createElement("button");
        trigger.type = "button";
        trigger.className = "browser-pagination-range-trigger";
        trigger.setAttribute("aria-label", `Show ${direction} conversation pages`);
        trigger.setAttribute("aria-haspopup", "menu");
        trigger.setAttribute("aria-expanded", "false");
        trigger.dataset.paginationRangeTrigger = "";
        const menuId = `agent_response_pagination_ranges_${item.position}`;
        trigger.setAttribute("aria-controls", menuId);
        const dots = document.createElement("span");
        dots.className = "local-store-page-ellipsis-dots";
        dots.setAttribute("aria-hidden", "true");
        trigger.append(dots);

        const menu = document.createElement("span");
        menu.id = menuId;
        menu.className = "browser-pagination-range-menu";
        menu.setAttribute("role", "menu");
        menu.setAttribute("aria-label", `${direction[0].toUpperCase()}${direction.slice(1)} conversation pages`);
        menu.setAttribute("aria-hidden", "true");
        menu.dataset.paginationRangeMenu = "";
        const grid = document.createElement("span");
        grid.className = "browser-pagination-range-grid";
        buildAgentPaginationRanges(item.firstPage, item.lastPage).forEach(([rangeStart, rangeEnd]) => {
            const option = document.createElement("button");
            option.type = "button";
            option.className = "browser-pagination-range-option";
            option.setAttribute("role", "menuitem");
            option.dataset.paginationRangeStart = String(rangeStart);
            option.dataset.paginationRangeEnd = String(rangeEnd);
            option.setAttribute("aria-label", `Conversation pages ${rangeStart} through ${rangeEnd}`);
            option.textContent = `${rangeStart}-${rangeEnd}`;
            option.addEventListener("click", () => {
                const targetPage = Number(option.dataset.paginationRangeStart);
                const nextAnimationState = paginationMotion?.capturePaginationAnimation(pagination, targetPage);
                agentPaginationRangePinnedPicker = null;
                setAgentPaginationRangePickerOpen(picker, false);
                responseHistoryPage = targetPage;
                renderAgentResponsePage({animationState: nextAnimationState});
            });
            grid.append(option);
        });
        menu.append(grid);
        picker.append(trigger, menu);
        return picker;
    }

    function positionAgentPaginationIndicator({immediate = false} = {}) {
        if (!elements.responsePagination || !paginationMotion) return;
        paginationMotion.positionPaginationIndicator(
            elements.responsePagination,
            elements.responsePagination.querySelector(".local-store-page-button.is-active"),
            {immediate},
        );
    }

    function renderAgentResponsePagination({animationState = null} = {}) {
        const pagination = elements.responsePagination;
        if (!pagination) return;
        agentPaginationRangePinnedPicker = null;
        agentPaginationRangeFocusRestore = null;
        cancelAgentPaginationRangeClose();
        const totalPages = responseHistory.length;
        responseHistoryPage = Math.min(
            Math.max(normalizePaginationPage(responseHistoryPage), 1),
            Math.max(totalPages, 1),
        );
        pagination.hidden = totalPages <= 1;
        if (totalPages <= 1) {
            paginationMotion?.clearPaginationAnimation(pagination);
            pagination.replaceChildren();
            pagination.style.removeProperty("--local-store-pagination-slots");
            pagination.classList.remove("is-animated");
            return;
        }

        const indicator = pagination.querySelector(".local-store-pagination-indicator")
            || document.createElement("span");
        indicator.className = "local-store-pagination-indicator";
        indicator.setAttribute("aria-hidden", "true");
        const items = buildAgentPaginationItems(totalPages, responseHistoryPage);
        const controls = items.map((item) => {
            if (item.kind === "ellipsis") {
                return createAgentPaginationRangePicker(item, pagination);
            }

            const button = document.createElement("button");
            button.type = "button";
            button.className = `local-store-page-button${item.isActive ? " is-active" : ""}${item.kind === "page" ? "" : " local-store-page-nav"}`;
            button.dataset.paginationTarget = String(item.page);
            button.dataset.paginationCurrent = item.isActive ? "1" : "0";
            if (item.isActive) button.setAttribute("aria-current", "page");
            if (item.kind === "page") {
                button.textContent = String(item.page);
                button.setAttribute("aria-label", `Conversation page ${item.page}`);
            } else {
                const isPrevious = item.kind === "previous";
                button.setAttribute(
                    "aria-label",
                    isPrevious ? "Previous conversation page group" : "Next conversation page group",
                );
                const icon = document.createElement("span");
                icon.className = `icon ${isPrevious ? "icon-page-prev" : "icon-page-next"}`;
                icon.setAttribute("aria-hidden", "true");
                button.append(icon);
            }
            button.addEventListener("click", () => {
                if (item.isActive) return;
                const nextAnimationState = paginationMotion?.capturePaginationAnimation(
                    pagination,
                    item.page,
                );
                responseHistoryPage = item.page;
                renderAgentResponsePage({animationState: nextAnimationState});
            });
            return button;
        });
        pagination.style.setProperty("--local-store-pagination-slots", String(items.length));
        pagination.replaceChildren(indicator, ...controls);
        bindAgentPaginationRangeInteractions();
        window.requestAnimationFrame(() => {
            if (animationState && paginationMotion) {
                paginationMotion.animatePaginationIndicator(pagination, animationState);
                return;
            }
            positionAgentPaginationIndicator({immediate: true});
        });
    }

    function clearResponseCopyFeedback() {
        if (responseCopyFeedbackTimer) {
            window.clearTimeout(responseCopyFeedbackTimer);
            responseCopyFeedbackTimer = 0;
        }
        const button = elements.responseCopy;
        if (!button) return;
        button.classList.remove("is-copied", "is-copy-failed");
        button.setAttribute("aria-label", "Copy answer");
        button.setAttribute("title", "Copy answer");
        if (elements.responseCopyFeedback) elements.responseCopyFeedback.textContent = "";
    }

    function renderResponseCopy(entry) {
        responseCopyRevision += 1;
        responseCopyValue = typeof entry?.response === "string" ? entry.response : "";
        clearResponseCopyFeedback();
        if (!elements.responseCopy) return;
        const copyAvailable = Boolean(responseCopyValue);
        elements.responseCopy.hidden = !copyAvailable;
        elements.responseCopy.disabled = !copyAvailable;
    }

    function copyResponseTextFallback(value) {
        const textarea = document.createElement("textarea");
        textarea.value = value;
        textarea.setAttribute("readonly", "");
        textarea.setAttribute("aria-hidden", "true");
        textarea.style.position = "fixed";
        textarea.style.opacity = "0";
        textarea.style.pointerEvents = "none";
        document.body.append(textarea);
        try {
            textarea.select();
            textarea.setSelectionRange(0, textarea.value.length);
            return document.execCommand("copy");
        } catch (_error) {
            return false;
        } finally {
            textarea.remove();
        }
    }

    async function copyResponseText(value) {
        if (!value) return false;
        if (navigator.clipboard?.writeText) {
            try {
                await navigator.clipboard.writeText(value);
                return true;
            } catch (_error) {
            }
        }
        return copyResponseTextFallback(value);
    }

    function setResponseCopyFeedback(didCopy) {
        const button = elements.responseCopy;
        if (!button) return;
        const copied = Boolean(didCopy);
        button.classList.toggle("is-copied", copied);
        button.classList.toggle("is-copy-failed", !copied);
        const label = copied ? "Answer copied" : "Unable to copy answer";
        button.setAttribute("aria-label", label);
        button.setAttribute("title", label);
        if (elements.responseCopyFeedback) elements.responseCopyFeedback.textContent = label;
        responseCopyFeedbackTimer = window.setTimeout(clearResponseCopyFeedback, 1_600);
    }

    function escapeAgentMathCurrencySymbols(expression) {
        const source = String(expression || "");
        let escaped = "";
        for (let index = 0; index < source.length; index += 1) {
            const character = source[index];
            if (character !== "$") {
                escaped += character;
                continue;
            }
            let precedingBackslashes = 0;
            for (let cursor = index - 1; cursor >= 0 && source[cursor] === "\\"; cursor -= 1) {
                precedingBackslashes += 1;
            }
            escaped += precedingBackslashes % 2 === 0 ? "\\$" : "$";
        }
        return escaped;
    }

    function renderAgentMath(root) {
        if (!root || typeof window.renderMathInElement !== "function") return;
        window.renderMathInElement(root, {
            delimiters: [
                {left: "$$", right: "$$", display: true},
                {left: "\\(", right: "\\)", display: false},
                {left: "\\begin{equation}", right: "\\end{equation}", display: true},
                {left: "\\begin{align}", right: "\\end{align}", display: true},
                {left: "\\begin{alignat}", right: "\\end{alignat}", display: true},
                {left: "\\begin{gather}", right: "\\end{gather}", display: true},
                {left: "\\[", right: "\\]", display: true},
            ],
            errorCallback: () => {},
            preProcess: escapeAgentMathCurrencySymbols,
            strict: "ignore",
            throwOnError: false,
            trust: false,
        });
    }

    function renderAgentResponsePage({animationState = null} = {}) {
        const entry = responseHistory[responseHistoryPage - 1] || null;
        const pageKey = String(responseHistoryPage);
        const contentSignature = JSON.stringify([
            entry?.prompt || "",
            entry?.response || "",
            entry?.response_html || "",
        ]);
        const pageChanged = pageKey !== renderedResponsePageKey;
        const contentChanged = contentSignature !== renderedResponseContentSignature;
        if (!pageChanged && !contentChanged) return;

        const answer = elements.responseAnswer;
        const previousScrollTop = answer?.scrollTop || 0;
        const previousScrollHeight = answer?.scrollHeight || 0;
        const previousClientHeight = answer?.clientHeight || 0;
        const hadRenderedContent = Boolean(renderedResponseContentSignature);
        const preserveScrollPosition = Boolean(answer)
            && !pageChanged
            && hadRenderedContent;
        const wasAtBottom = preserveScrollPosition
            && previousScrollHeight > previousClientHeight
            && previousScrollHeight - previousClientHeight - previousScrollTop <= 1;

        renderedResponsePageKey = pageKey;
        renderedResponseContentSignature = contentSignature;
        if (elements.responseQuestion) elements.responseQuestion.textContent = entry?.prompt || "";
        renderResponseCopy(entry);
        if (elements.responseAnswerContent) {
            elements.responseAnswerContent.innerHTML = entry?.response_html || "";
            renderAgentMath(elements.responseAnswerContent);
        } else if (elements.responseAnswer) {
            elements.responseAnswer.innerHTML = entry?.response_html || "";
            renderAgentMath(elements.responseAnswer);
        }
        if (elements.responseOutput) elements.responseOutput.hidden = !entry;
        renderAgentResponsePagination({animationState});

        if (answer) {
            if (!preserveScrollPosition) answer.scrollTop = 0;
            else if (wasAtBottom) answer.scrollTop = answer.scrollHeight;
            else answer.scrollTop = Math.min(
                previousScrollTop,
                Math.max(answer.scrollHeight - answer.clientHeight, 0),
            );
        }
    }

    function renderAgentResponse(agent) {
        const localHistory = Array.isArray(agent?.history)
            ? agent.history.filter((item) => item && item.prompt && item.response)
            : [];
        if (!localHistory.length && agent?.response) {
            localHistory.push({
                prompt: agent.prompt || "",
                response: agent.response,
                response_html: agent.response_html || "",
                started_at: agent.started_at || "",
                finished_at: agent.finished_at || "",
            });
        }
        const selectedUrl = selectedHistoryConversationUrl();
        let history = localHistory;
        if (selectedUrl) {
            const localSessionMatches = historyUrlKey(agent?.conversation_url) === historyUrlKey(selectedUrl);
            if (remoteHistoryMatchesSelection()) {
                history = [...remoteSessionHistory];
                if (localSessionMatches) history = mergeAgentHistory(history, localHistory);
            } else if (!executionSessions.some((item) => item.session_id === executionSessionId)) {
                history = [];
            }
        }
        const signature = JSON.stringify(
            history.map((item) => [
                item.started_at || "",
                item.finished_at || "",
                item.prompt || "",
                item.response || "",
                item.response_html || "",
            ]),
        );
        if (signature !== responseHistorySignature) {
            responseHistorySignature = signature;
            responseHistory = history;
            responseHistoryPage = Math.max(history.length, 1);
        }
        renderAgentResponsePage();
        return responseHistory.length > 0;
    }

    function mergeAgentHistory(primary, secondary) {
        const merged = Array.isArray(primary) ? [...primary] : [];
        const seen = new Set(merged.map((item) => `${item.prompt || ""}\u0000${item.response || ""}`));
        (Array.isArray(secondary) ? secondary : []).forEach((item) => {
            const key = `${item.prompt || ""}\u0000${item.response || ""}`;
            if (!seen.has(key) && item.prompt && item.response) {
                seen.add(key);
                merged.push(item);
            }
        });
        return merged;
    }

    function renderTerminalExecution(runtime) {
        if (!elements.terminalExecutionStatus || !elements.terminalExecutionCopy) return;
        const terminalExecution = runtime?.terminal_execution || {};
        const ready = Boolean(terminalExecution.ready);
        const statusLabel = terminalExecution.status_label || (ready ? "Granted" : "Not granted");
        elements.terminalExecutionStatus.dataset.ready = String(ready);
        elements.terminalExecutionStatus.title = terminalExecution.message || "";
        elements.terminalExecutionStatus.setAttribute("aria-label", `Terminal permission: ${statusLabel}`);
        elements.terminalExecutionCopy.textContent = statusLabel;
        if (elements.terminalExecutionCheckmark) {
            elements.terminalExecutionCheckmark.dataset.statusState = ready ? "ready" : "error";
            elements.terminalExecutionCheckmark.hidden = false;
        }
    }

    function renderComputeJob(job) {
        if (!elements.computeJob) return;
        const state = String(job?.state || "idle");
        const jobId = String(job?.job_id || "");
        const active = Boolean(job?.active);
        const progress = job?.progress || {};
        const progressText = progress.summary
            || (Number.isFinite(Number(progress.evaluations_completed))
                ? `${Number(progress.evaluations_completed).toLocaleString()} evaluations completed`
                : "Waiting for heartbeat");
        elements.computeJob.hidden = state === "idle";
        elements.computeJob.dataset.jobId = jobId;
        if (elements.computeJobStatus) elements.computeJobStatus.textContent = job?.message || "Compute job status is available.";
        if (elements.computeJobState) elements.computeJobState.textContent = state;
        if (elements.computeJobId) elements.computeJobId.textContent = jobId || "—";
        if (elements.computeJobProgress) elements.computeJobProgress.textContent = progressText;
        if (elements.computeJobHelp) {
            elements.computeJobHelp.textContent = job?.can_resume
                ? "A complete checkpoint is available. Resume with job_start and this resume_job_id; it is never submitted automatically."
                : "Start uses job_start. Resume appears only after a complete checkpoint is available.";
        }
        if (elements.computeJobStop) {
            elements.computeJobStop.hidden = !active;
            elements.computeJobStop.disabled = !active;
        }
    }

    function render(payload, {fromAsk = false} = {}) {
        const nextPayload = payload || {};
        const hasPersistedAgent = Object.prototype.hasOwnProperty.call(nextPayload, "agent");
        if (!hasPersistedAgent) return;
        renderExecutionSessions(nextPayload);
        const persistedAgent = nextPayload.agent || {};
        restoreExecutionConfiguration(persistedAgent);
        if (persistedAgent.session_id === executionSessionId
            && lastRenderedExecutionSessionId !== executionSessionId) {
            lastRenderedExecutionSessionId = executionSessionId;
            lastRenderedAgentRunIdentity = "";
            lastRenderedAgentRunRevision = 0;
            lastRenderedAgentStartedAt = "";
            lastRenderedAgentRunning = false;
        }
        const readiness = readinessState(nextPayload);
        const selectionMatchesAgent = agentSnapshotMatchesSelection(persistedAgent);
        const agent = selectionMatchesAgent
            ? persistedAgent
            : isolatedForeignRunningAgent(persistedAgent);
        const running = Boolean(agent.running);
        const paused = Boolean(agent.paused);
        if (running) automaticSourcesSuppressedAfterCompletion = false;
        const platformLabel = selectedPlatformLabel();
        const phase = String(agent.phase || "").trim().toLowerCase();
        const runIdentity = agentRunIdentity(agent);
        const runRevision = agentRunRevision(agent);
        const startedAt = agentRunStartedAt(agent);
        const acknowledgedNewRun = fromAsk
            && promptSubmissionPending
            && Boolean(runIdentity)
            && runIdentity !== pendingSubmissionPreviousRunIdentity;
        const incomingRunIsStale = Boolean(lastRenderedAgentRunIdentity)
            && Boolean(runIdentity)
            && runIdentity !== lastRenderedAgentRunIdentity
            && !acknowledgedNewRun
            && !runSupersedes(
                runIdentity,
                runRevision,
                startedAt,
                lastRenderedAgentRunIdentity,
                lastRenderedAgentRunRevision,
                lastRenderedAgentStartedAt,
        );
        if (incomingRunIsStale) return;
        lastPayload = {...nextPayload, agent};
        const pendingRunConfirmed = promptSubmissionPending && (
            acknowledgedNewRun || runSupersedes(
                runIdentity,
                runRevision,
                startedAt,
                pendingSubmissionPreviousRunIdentity,
                pendingSubmissionPreviousRunRevision,
                pendingSubmissionPreviousRunStartedAt,
            )
        );
        const sameRenderedRun = Boolean(runIdentity)
            && runIdentity === lastRenderedAgentRunIdentity;
        const completedTransition = !running && (
            (lastRenderedAgentRunning === true && sameRenderedRun)
            || pendingRunConfirmed
        );
        const finishedTransition = completedTransition
            && phase === "finished";
        const shouldCollapseActivity = finishedTransition;
        if (running || fromAsk || pendingRunConfirmed) {
            promptSubmissionPending = false;
            pendingSubmissionPreviousRunIdentity = "";
            pendingSubmissionPreviousRunRevision = 0;
            pendingSubmissionPreviousRunStartedAt = "";
        }
        syncNewSessionButton();
        syncExecutionChoices();
        syncPlatformState(agent);
        bindCompletedAgentSession(agent, completedTransition);
        lastRenderedAgentRunning = running;
        if (elements.agentPage) {
            elements.agentPage.dataset.agentRunning = String(running);
            if (runIdentity) elements.agentPage.dataset.agentRunId = runIdentity;
            else delete elements.agentPage.dataset.agentRunId;
            elements.agentPage.dataset.agentRunRevision = String(runRevision);
            if (startedAt) elements.agentPage.dataset.agentStartedAt = startedAt;
            else delete elements.agentPage.dataset.agentStartedAt;
        }
        if (runIdentity) lastRenderedAgentRunIdentity = runIdentity;
        lastRenderedAgentRunRevision = runRevision;
        if (startedAt) lastRenderedAgentStartedAt = startedAt;
        syncConversationLink(agent);

        const heading = document.querySelector("[data-agent-heading]");
        if (heading) heading.textContent = `${platformLabel} Web Agent`;
        if (elements.promptInput) {
            elements.promptInput.placeholder = "Do anything";
        }

        renderAgentResponse(agent);
        clearCompletedPromptIfUnchanged(agent, shouldCollapseActivity);
        renderErrorRecord(agent);
        if (doctorPayload?.run_id && doctorPayload.run_id !== agent.run_id) {
            doctorPayload = null;
            doctorRequestId += 1;
            if (elements.doctorPanel) elements.doctorPanel.open = false;
        }
        if (agentNeedsDoctor(agent)) {
            if (!doctorPayload) loadDoctor();
            else renderDoctor();
        } else if (doctorPayload) {
            doctorPayload = null;
            doctorRequestId += 1;
            renderDoctor(null);
        }
        renderResponseStatus(agent, readiness);
        renderTerminalExecution(lastPayload.runtime);
        renderComputeJob(lastPayload.compute_job);
        renderActivity(agent.activity, running, shouldCollapseActivity, runIdentity);
        updateSessionChoiceInputs();
        if (
            readiness.ready
            && !running
            && !completedTransition
            && !automaticSourcesSuppressedAfterCompletion
        ) loadAgentSources();

        if (elements.resume) {
            elements.resume.hidden = !paused;
            elements.resume.disabled = !paused;
        }
        if (elements.ask) {
            elements.ask.disabled = ((!readiness.ready || !sessionChoiceReady() || !executionCanStart || promptSubmissionPending) && !running);
            elements.ask.classList.toggle("is-stop", running);
            elements.ask.dataset.agentAction = running ? "stop" : "ask";
            const label = running ? "Stop Agent task" : `Ask ${platformLabel} Web`;
            elements.ask.setAttribute("aria-label", label);
            elements.ask.setAttribute("title", label);
        }
        if (elements.projectChoose) elements.projectChoose.disabled = running;
        elements.comboboxTriggers.forEach((trigger) => {
            const isLockedComposerChoice = trigger.closest(
                ".agent-model-combobox, .agent-effort-combobox"
            );
            if (isLockedComposerChoice) {
                trigger.disabled = running;
                return;
            }
            if (running) {
                const sessionSourceChoice = trigger.closest(".agent-session-mode-combobox");
                trigger.disabled = !sessionSourceChoice;
                return;
            }
            const isRuntimeChoice = trigger.closest(
                ".agent-platform-combobox, .agent-browser-combobox, .agent-model-combobox, .agent-session-mode-combobox"
            );
            if (isRuntimeChoice) trigger.disabled = false;
        });
        // Restored run metadata deliberately omits message bodies; read the bound provider history.
        const historyUrl = selectedHistoryConversationUrl();
        if (!running && agent.run_id && !agent.response && !agent.history?.length
            && executionSessions.some((item) => item.session_id === executionSessionId)
            && ["chatgpt", "grok"].includes(selectedPlatform())
            && isAgentConversationUrl(selectedPlatform(), historyUrl)
            && !remoteHistoryMatchesSelection()) {
            void loadSelectedSessionHistory(historyUrl);
        }
        syncSessionListControls(running);
        if (running) {
            closeAgentComposerCombobox(elements.modelCombobox);
            closeAgentComposerCombobox(elements.effortCombobox);
        }
    }

    async function mutate(url, payload = {}) {
        const epoch = executionSessionEpoch;
        try {
            const response = await requestJson(url, {
                method: "POST",
                body: JSON.stringify(payload),
            });
            if (epoch !== executionSessionEpoch) return;
            if (url === "/api/agent/ask" && response.agent?.session_id) {
                executionSessionId = response.agent.session_id;
                try { window.sessionStorage.setItem(executionStorageKey(), executionSessionId); } catch (_error) {}
                executionSessionEpoch += 1;
            }
            if (response.doctor) doctorPayload = response.doctor;
            render(response, {fromAsk: url === "/api/agent/ask"});
        } catch (error) {
            if (epoch !== executionSessionEpoch) return;
            if (url === "/api/agent/ask") {
                promptSubmissionPending = false;
                pendingSubmissionPreviousRunIdentity = "";
                pendingSubmissionPreviousRunRevision = 0;
                pendingSubmissionPreviousRunStartedAt = "";
                syncNewSessionButton();
            }
            setResponseStatusFallback(error.message);
            if (elements.errorRecord && elements.errorRecordContent) {
                elements.errorRecordContent.textContent = error.message;
                elements.errorRecord.hidden = false;
                elements.errorRecord.open = true;
            }
            if (elements.responseOutput && !responseHistory.length) elements.responseOutput.hidden = true;
        }
    }

    function promptCollapsedHeight() {
        if (!(elements.promptInput instanceof HTMLTextAreaElement)) return 0;
        const styles = window.getComputedStyle(elements.promptInput);
        const lineHeight = Number.parseFloat(styles.lineHeight) || 24;
        const paddingBlock = (Number.parseFloat(styles.paddingTop) || 0)
            + (Number.parseFloat(styles.paddingBottom) || 0);
        const borderBlock = (Number.parseFloat(styles.borderTopWidth) || 0)
            + (Number.parseFloat(styles.borderBottomWidth) || 0);
        return Math.ceil((lineHeight * 2) + paddingBlock + borderBlock);
    }

    function isPromptExpanded() {
        return elements.promptOverflowToggle?.getAttribute("aria-expanded") === "true";
    }

    function syncPromptOverflowToggle(canExpand) {
        const toggle = elements.promptOverflowToggle;
        if (!toggle) return;
        if (!canExpand) {
            elements.promptInput?.classList.remove("is-expanded");
            toggle.setAttribute("aria-expanded", "false");
        }
        const expanded = isPromptExpanded();
        const label = expanded ? "Collapse question or task" : "Expand question or task";
        toggle.hidden = !canExpand;
        toggle.setAttribute("aria-label", label);
        toggle.setAttribute("title", label);
    }

    function resizePrompt() {
        if (!(elements.promptInput instanceof HTMLTextAreaElement)) return;
        const collapsedHeight = promptCollapsedHeight();
        elements.promptInput.style.height = "auto";
        const canExpand = Boolean(elements.promptInput.value.trim())
            && elements.promptInput.scrollHeight > collapsedHeight + 1;
        syncPromptOverflowToggle(canExpand);
        if (!isPromptExpanded()) {
            elements.promptInput.style.height = `${collapsedHeight}px`;
            return;
        }
        const expandedHeightLimit = Math.min(
            360,
            Math.max(collapsedHeight, Math.round(window.innerHeight * 0.45)),
        );
        const expandedHeight = Math.min(
            expandedHeightLimit,
            Math.max(collapsedHeight, elements.promptInput.scrollHeight),
        );
        elements.promptInput.style.height = `${expandedHeight}px`;
    }

    function setPromptExpanded(expanded) {
        if (!(elements.promptInput instanceof HTMLTextAreaElement)) return;
        elements.promptInput.classList.toggle("is-expanded", expanded);
        elements.promptOverflowToggle?.setAttribute("aria-expanded", String(expanded));
        const label = expanded ? "Collapse question or task" : "Expand question or task";
        elements.promptOverflowToggle?.setAttribute("aria-label", label);
        elements.promptOverflowToggle?.setAttribute("title", label);
        if (!expanded) elements.promptInput.scrollTop = 0;
        resizePrompt();
    }

    function clearCompletedPromptIfUnchanged(agent, shouldClear) {
        if (!shouldClear || !(elements.promptInput instanceof HTMLTextAreaElement)) return;
        const completedPrompt = String(agent?.prompt || "");
        if (promptHasLocalDraft) return;
        if (!completedPrompt || elements.promptInput.value !== completedPrompt) return;
        elements.promptInput.value = "";
        promptHasLocalDraft = false;
        setPromptExpanded(false);
    }

    promptForm.addEventListener("submit", (event) => {
        event.preventDefault();
        if (promptSubmissionPending || elements.ask?.disabled || lastPayload.agent?.running || elements.ask?.classList.contains("is-stop")) return;
        updateSessionChoiceInputs();
        schedulePreferenceSave();
        promptHasLocalDraft = false;
        promptSubmissionPending = true;
        syncNewSessionButton();
        pendingSubmissionPreviousRunIdentity = agentRunIdentity(lastPayload.agent)
            || elements.agentPage?.dataset.agentRunId
            || "";
        pendingSubmissionPreviousRunRevision = agentRunRevision(lastPayload.agent)
            || normalizeAgentRunRevision(elements.agentPage?.dataset.agentRunRevision);
        pendingSubmissionPreviousRunStartedAt = agentRunStartedAt(lastPayload.agent)
            || elements.agentPage?.dataset.agentStartedAt
            || "";
        mutate("/api/agent/ask", formPayload(promptForm));
    });
    elements.resume?.addEventListener("click", () => {
        mutate("/api/agent/resume");
    });
    elements.newSessionButton?.addEventListener("click", () => {
        void beginNewSession();
    });
    elements.computeJobStop?.addEventListener("click", async () => {
        const jobId = String(elements.computeJob?.dataset.jobId || "");
        if (!jobId || elements.computeJobStop.disabled) return;
        elements.computeJobStop.disabled = true;
        try {
            const response = await requestJson("/api/agent/compute-job/stop", {
                method: "POST",
                body: JSON.stringify({
                    job_id: jobId,
                    workspace_path: String(elements.workspacePath?.value || ""),
                }),
            });
            lastPayload = {...lastPayload, compute_job: response.compute_job || {}};
            renderComputeJob(lastPayload.compute_job);
        } catch (error) {
            setResponseStatusFallback(error.message);
            elements.computeJobStop.disabled = false;
        }
    });
    elements.ask?.addEventListener("click", () => {
        if (elements.ask?.classList.contains("is-stop")) {
            mutate("/api/agent/stop");
            return;
        }
        if (!elements.ask.disabled) promptForm.requestSubmit();
    });
    elements.promptInput?.addEventListener("input", () => {
        promptHasLocalDraft = true;
        resizePrompt();
    });
    elements.promptOverflowToggle?.addEventListener("click", () => {
        setPromptExpanded(!isPromptExpanded());
        elements.promptInput?.focus();
    });
    elements.responseCopy?.addEventListener("click", async () => {
        const value = responseCopyValue;
        const revision = responseCopyRevision;
        if (!value || elements.responseCopy?.disabled) return;
        const didCopy = await copyResponseText(value);
        if (revision !== responseCopyRevision) return;
        setResponseCopyFeedback(didCopy);
    });
    window.addEventListener(
        "resize",
        () => {
            resizePrompt();
            positionAgentPaginationIndicator({immediate: true});
        },
        {passive: true},
    );
    elements.promptInput?.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
        event.preventDefault();
        if (!elements.ask?.disabled && !elements.ask?.classList.contains("is-stop")) promptForm.requestSubmit();
    });
    elements.projectPath?.addEventListener("change", () => {
        syncProjectPath(elements.projectPath.value);
        schedulePreferenceSave();
    });
    elements.conversationLink?.addEventListener("click", (event) => {
        event.preventDefault();
        requestJson("/api/agent/open-conversation", {
            method: "POST",
        }).catch((error) => {
            setResponseStatusFallback(error.message);
        });
    });
    elements.doctorPanel?.addEventListener("click", (event) => {
        const target = event.target instanceof Element ? event.target : null;
        const button = target?.closest("[data-agent-doctor-action]");
        if (!button) return;
        const action = button.dataset.agentDoctorAction;
        if (action === "new_task") {
            elements.doctorPanel.open = false;
            elements.promptInput?.focus();
            return;
        }
        if (action === "open_conversation") {
            elements.conversationLink?.click();
            return;
        }
        button.disabled = true;
        mutate("/api/agent/doctor/recover", {action}).finally(() => {
            button.disabled = false;
        });
    });

    initializeComboboxes();
    initializeBrowserSessionStatus();
    syncPlatformState();
    updateSessionChoiceInputs();
    syncProjectPath(elements.projectPath?.value || elements.workspacePath?.value || "");
    syncExecutionChoices();
    syncNewSessionButton();
    syncAgentRoute();
    resizePrompt();
    renderAgentMath(elements.responseAnswerContent || elements.responseAnswer);

    async function pollStatus() {
        const epoch = executionSessionEpoch;
        try {
            const payload = await requestJson("/api/agent/status", {headers: agentStatusHeaders()});
            if (epoch === executionSessionEpoch) render(payload);
        } catch (_error) {
            if (epoch === executionSessionEpoch && elements.statusMessage) {
                stopResponseStatusTimer();
                const copy = "Reconnecting · Local status is unavailable. The last known task state is preserved.";
                elements.statusMessage.hidden = false;
                elements.statusMessage.dataset.status = "reconnecting";
                elements.statusMessage.setAttribute("aria-label", copy);
                elements.statusMessage.title = copy;
                if (elements.statusMessageCopy) elements.statusMessageCopy.textContent = copy;
                if (elements.statusDot) elements.statusDot.hidden = true;
                if (elements.statusSpinner) {
                    elements.statusSpinner.hidden = false;
                    elements.statusSpinner.classList.remove("cache-phase-live-marker");
                    elements.statusSpinner.classList.add("suggestion-loading-spinner");
                }
            }
        } finally {
            window.setTimeout(
                pollStatus,
                executionActiveCount || lastPayload.agent?.running || lastPayload.compute_job?.active ? 800 : 2_500,
            );
        }
    }
    pollStatus();
})();
