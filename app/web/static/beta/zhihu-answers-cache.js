/* Code version: v0.4.0-codex.1 */

const root = document.querySelector(
    '[data-beta-root][data-beta-experiment="zhihu-answers-cache"]',
);

if (root) initializeZhihuAnswersCache(root);

function initializeZhihuAnswersCache(root) {
    const form = root.querySelector("[data-zhihu-cache-form]");
    const profileInput = root.querySelector("#zhihu_profile_url");
    const startButton = root.querySelector("[data-zhihu-start]");
    const stopButton = root.querySelector("[data-zhihu-stop]");
    const exampleButton = root.querySelector("[data-zhihu-example]");
    const statusNode = root.querySelector("[data-zhihu-status]");
    const progressPanel = root.querySelector("[data-zhihu-progress]");
    const progressBar = root.querySelector("[data-zhihu-progress-bar]");
    const progressCopy = root.querySelector("[data-zhihu-progress-copy]");
    const phaseNode = root.querySelector("[data-zhihu-phase]");
    const archivePanel = root.querySelector("[data-zhihu-archive]");
    const archiveSummary = root.querySelector("[data-zhihu-archive-summary]");
    const archiveStatus = root.querySelector("[data-zhihu-archive-status]");
    const searchForm = root.querySelector("[data-zhihu-search-form]");
    const searchInput = root.querySelector("#zhihu_archive_query");
    const searchButton = root.querySelector("[data-zhihu-search]");
    const answerList = root.querySelector("[data-zhihu-answer-list]");
    const pagination = root.querySelector("[data-zhihu-pagination]");
    const previousButton = root.querySelector("[data-zhihu-previous]");
    const nextButton = root.querySelector("[data-zhihu-next]");
    const pageSummary = root.querySelector("[data-zhihu-page-summary]");
    const detailPanel = root.querySelector("[data-zhihu-detail]");
    const detailTitle = root.querySelector("[data-zhihu-detail-title]");
    const detailMeta = root.querySelector("[data-zhihu-detail-meta]");
    const detailStatus = root.querySelector("[data-zhihu-detail-status]");
    const detailBody = root.querySelector("[data-zhihu-detail-body]");
    const detailSource = root.querySelector("[data-zhihu-detail-source]");
    const detailClose = root.querySelector("[data-zhihu-detail-close]");
    const outputWrap = root.querySelector("[data-zhihu-output-wrap]");
    const outputNode = root.querySelector("[data-zhihu-output-dir]");
    const pollInterval = Math.max(
        250,
        Number.parseInt(form?.dataset.pollIntervalMs || "1000", 10) || 1000,
    );
    const pendingControllers = new Set();
    let pollTimer = 0;
    let lastSnapshot = null;
    let refreshGeneration = 0;
    let archiveGeneration = 0;
    let detailGeneration = 0;
    let archivePage = 1;
    let archivePageCount = 1;
    let archiveProfile = "";

    if (
        !form || !profileInput || !startButton || !stopButton || !statusNode
        || !archivePanel || !archiveSummary || !archiveStatus || !searchForm || !searchInput
        || !searchButton || !answerList || !pagination || !previousButton || !nextButton
        || !pageSummary || !detailPanel || !detailTitle || !detailMeta || !detailStatus
        || !detailBody || !detailSource || !detailClose
    ) return;

    let endpoints;
    try {
        endpoints = Object.fromEntries(
            ["status", "start", "stop", "answers"].map((name) => {
                const endpoint = new URL(form.dataset[`${name}Url`], window.location.href);
                if (endpoint.origin !== window.location.origin) {
                    throw new Error("The cache API must use this application's origin.");
                }
                return [name, endpoint];
            }),
        );
    } catch (error) {
        announce(error.message || "The local cache API is unavailable.", "error");
        return;
    }

    function announce(message, state = "") {
        statusNode.textContent = message;
        statusNode.dataset.state = state;
    }

    function count(value) {
        const numeric = Number(value);
        return Number.isFinite(numeric) && numeric > 0 ? Math.trunc(numeric) : 0;
    }

    function formatCount(value) {
        return count(value).toLocaleString("en-US");
    }

    function formatOptionalCount(value, label) {
        if (value === null || value === undefined || value === "") {
            return `${label} unavailable`;
        }
        return `${formatCount(value)} ${label}`;
    }

    function phaseLabel(value) {
        const words = String(value || "idle").replaceAll("_", " ").trim();
        return words ? `${words.charAt(0).toUpperCase()}${words.slice(1)}` : "Idle";
    }

    function isComplete(snapshot) {
        const phase = String(snapshot.phase || "").toLowerCase();
        const expected = count(snapshot.expected_answers);
        return !snapshot.running && (
            ["complete", "completed", "finished", "ready"].includes(phase)
            || (expected > 0 && count(snapshot.cached_answers) >= expected)
        );
    }

    function selectedBrowser() {
        return form.elements.namedItem("browser")?.value || "";
    }

    function setControlsRunning(running, phase = "") {
        const commitStarted = String(phase).toLowerCase() === "committing";
        if (running) form.setAttribute("aria-busy", "true");
        else form.removeAttribute("aria-busy");
        profileInput.disabled = running;
        form.querySelectorAll('input[name="browser"]').forEach((input) => {
            input.disabled = running;
        });
        startButton.disabled = running || !form.checkValidity();
        startButton.classList.toggle("is-pending", running);
        startButton.textContent = running ? "Caching…" : startButton.dataset.idleLabel;
        stopButton.hidden = !running;
        stopButton.disabled = !running || commitStarted;
    }

    function setMetric(name, value) {
        const node = root.querySelector(`[data-zhihu-metric="${name}"]`);
        if (node) node.textContent = formatCount(value);
    }

    function setChange(name, value) {
        const node = root.querySelector(`[data-zhihu-change="${name}"]`);
        if (node) node.textContent = formatCount(value);
    }

    function formatDate(value) {
        if (!value) return "";
        const date = new Date(value);
        if (Number.isNaN(date.valueOf())) return String(value);
        const parts = new Intl.DateTimeFormat("en-US", {
            day: "numeric",
            month: "short",
            year: "numeric",
            timeZone: "UTC",
        }).formatToParts(date);
        const part = (type) => parts.find((item) => item.type === type)?.value || "";
        return `${part("day")} ${part("month")} ${part("year")}`.trim();
    }

    function safeAnswerUrl(value) {
        try {
            const url = new URL(String(value));
            const hostname = url.hostname.toLowerCase();
            if (url.protocol !== "https:" || (hostname !== "zhihu.com" && !hostname.endsWith(".zhihu.com"))) {
                return "";
            }
            return url.href;
        } catch (_error) {
            return "";
        }
    }

    function metadataNode(text) {
        const node = document.createElement("span");
        node.textContent = text;
        return node;
    }

    function setArchiveStatus(message, state = "") {
        archiveStatus.textContent = message;
        archiveStatus.dataset.state = state;
    }

    function setDetailStatus(message, state = "") {
        detailStatus.textContent = message;
        detailStatus.dataset.state = state;
    }

    function renderArchive(payload) {
        const answers = Array.isArray(payload?.items) ? payload.items : [];
        const total = count(payload?.total);
        archivePage = Math.max(1, count(payload?.page));
        archivePageCount = Math.max(1, count(payload?.page_count));
        answerList.replaceChildren(...answers.map((answer) => {
            const item = document.createElement("li");
            item.className = "beta-zhihu-answer-item";
            const title = String(answer?.question_title || `Answer ${answer?.answer_id || ""}`).trim();
            const url = safeAnswerUrl(answer?.answer_url);
            const heading = document.createElement("button");
            heading.type = "button";
            heading.className = "beta-zhihu-answer-link";
            heading.textContent = title || "Untitled answer";
            heading.dataset.answerId = String(answer?.answer_id || "");
            heading.setAttribute("aria-label", `View cached copy: ${heading.textContent}`);
            const metadata = document.createElement("div");
            metadata.className = "beta-zhihu-answer-meta";
            if (answer?.answer_id) metadata.append(metadataNode(`Answer ${answer.answer_id}`));
            const updated = formatDate(answer?.updated_at);
            if (updated) metadata.append(metadataNode(`Updated ${updated}`));
            metadata.append(
                metadataNode(formatOptionalCount(answer?.voteup_count, "votes")),
                metadataNode(formatOptionalCount(answer?.comment_count, "comments")),
            );
            const actions = document.createElement("div");
            actions.className = "beta-zhihu-answer-actions";
            const localAction = document.createElement("button");
            localAction.type = "button";
            localAction.className = "secondary-button settings-inline-button";
            localAction.textContent = "View cached copy";
            localAction.dataset.answerId = String(answer?.answer_id || "");
            actions.append(localAction);
            if (url) {
                const source = document.createElement("a");
                source.className = "beta-text-link";
                source.textContent = "Open on Zhihu ↗";
                source.href = url;
                source.target = "_blank";
                source.rel = "noopener noreferrer";
                actions.append(source);
            }
            item.append(heading, metadata, actions);
            return item;
        }));
        const query = String(payload?.query || "").trim();
        archiveSummary.textContent = `${formatCount(total)} ${query ? "matches" : "cached"} · @${payload?.author_token || ""}`;
        pageSummary.textContent = `Page ${formatCount(archivePage)} of ${formatCount(archivePageCount)}`;
        previousButton.disabled = archivePage <= 1;
        nextButton.disabled = archivePage >= archivePageCount;
        pagination.hidden = total === 0 || archivePageCount <= 1;
        archivePanel.hidden = false;
        setArchiveStatus(
            total > 0
                ? `${formatCount(total)} ${query ? "matching" : "local"} answer records. Select View cached copy to read a stored body.`
                : query
                ? "No cached answers match this search."
                : "This local archive has no answer records.",
        );
    }

    function clearDetail() {
        detailGeneration += 1;
        detailPanel.hidden = true;
        detailTitle.textContent = "Cached answer";
        detailMeta.replaceChildren();
        detailBody.textContent = "";
        detailSource.hidden = true;
        detailSource.removeAttribute("href");
        setDetailStatus("");
    }

    function renderDetail(answer) {
        const title = String(answer?.question_title || `Answer ${answer?.answer_id || ""}`).trim();
        detailTitle.textContent = title || "Cached answer";
        detailMeta.replaceChildren();
        if (answer?.answer_id) detailMeta.append(metadataNode(`Answer ${answer.answer_id}`));
        const updated = formatDate(answer?.updated_at);
        if (updated) detailMeta.append(metadataNode(`Updated ${updated}`));
        detailMeta.append(
            metadataNode(formatOptionalCount(answer?.voteup_count, "votes")),
            metadataNode(formatOptionalCount(answer?.comment_count, "comments")),
        );
        const body = String(answer?.content_text || "").trim();
        const excerpt = String(answer?.excerpt || "").trim();
        detailBody.textContent = body || excerpt;
        setDetailStatus(
            body
                ? `Verified local body · ${formatCount(body.length)} characters · Record SHA-256 ${String(answer?.content_sha256 || "unavailable")}`
                : "The cache contains metadata only for this answer; Zhihu did not expose its complete body.",
            body ? "success" : "error",
        );
        const url = safeAnswerUrl(answer?.answer_url);
        if (url) {
            detailSource.href = url;
            detailSource.hidden = false;
        } else {
            detailSource.hidden = true;
            detailSource.removeAttribute("href");
        }
        detailPanel.hidden = false;
        detailPanel.focus({preventScroll: true});
        detailPanel.scrollIntoView({behavior: "smooth", block: "start"});
    }

    function renderSnapshot(rawSnapshot) {
        const snapshot = rawSnapshot && typeof rawSnapshot === "object" ? rawSnapshot : {};
        lastSnapshot = snapshot;
        const running = snapshot.running === true;
        const expected = count(snapshot.expected_answers);
        const processed = count(snapshot.processed_answers);
        const cached = count(snapshot.cached_answers);
        const unavailable = count(snapshot.unavailable_answers);
        const maximum = Math.max(expected, processed, cached, 1);
        const completed = isComplete(snapshot);
        const hasProgress = running || completed || Boolean(
            snapshot.last_error
            || snapshot.output_dir
            || expected
            || processed
            || cached
            || count(snapshot.pages_processed),
        );

        if (snapshot.profile_url && (running || document.activeElement !== profileInput)) {
            profileInput.value = String(snapshot.profile_url);
        }
        if (snapshot.browser) {
            const radio = form.querySelector(
                `input[name="browser"][value="${CSS.escape(String(snapshot.browser))}"]`,
            );
            if (radio) radio.checked = true;
        }
        if (running) archiveProfile = "";

        setControlsRunning(running, snapshot.phase);
        progressPanel.hidden = !hasProgress;
        phaseNode.textContent = phaseLabel(snapshot.phase);
        progressBar.max = maximum;
        progressBar.value = completed ? maximum : Math.min(processed, maximum);
        progressBar.setAttribute(
            "aria-valuetext",
            expected > 0
                ? `${formatCount(processed)} of ${formatCount(expected)} answers processed`
                : `${formatCount(processed)} answers processed`,
        );
        progressCopy.textContent = completed && unavailable > 0
            ? `${formatCount(cached)} unique answers cached; Zhihu reports ${formatCount(expected)}, with ${formatCount(unavailable)} not enumerable through its pagination.`
            : expected > 0
            ? `${formatCount(processed)} of ${formatCount(expected)} answers enumerated; ${formatCount(cached)} cached.`
            : `${formatCount(processed)} answers processed; ${formatCount(cached)} cached.`;
        for (const name of [
            "expected_answers", "processed_answers", "cached_answers", "unavailable_answers",
            "pages_processed", "duplicates",
        ]) setMetric(name, snapshot[name]);
        for (const name of ["added", "changed", "removed", "unchanged"]) {
            setChange(name, snapshot[name]);
        }
        outputNode.textContent = snapshot.output_dir ? String(snapshot.output_dir) : "";
        outputWrap.hidden = !snapshot.output_dir;
        searchButton.disabled = running || !snapshot.cache_exists;
        if (!snapshot.cache_exists) {
            archiveProfile = "";
            archivePanel.hidden = true;
            answerList.replaceChildren();
            clearDetail();
        }

        if (snapshot.last_error) {
            announce(String(snapshot.last_error), "error");
        } else if (snapshot.message) {
            announce(String(snapshot.message), completed ? "success" : "");
        } else if (completed) {
            announce("Cache complete. Search the local archive and verify stored answer bodies.", "success");
        } else if (running) {
            announce("Caching answers through the selected browser…");
        } else {
            announce("Ready to cache this complete answer collection.");
        }
    }

    function unwrapStatus(payload) {
        if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
            throw new Error("The local cache API returned an invalid status.");
        }
        if (payload.status && typeof payload.status === "object" && !Array.isArray(payload.status)) {
            return payload.status;
        }
        return payload;
    }

    async function requestJson(endpoint, options = {}) {
        if (endpoint.origin !== window.location.origin) {
            throw new Error("The browser client refused a cross-origin cache request.");
        }
        const controller = new AbortController();
        pendingControllers.add(controller);
        try {
            const response = await fetch(endpoint, {
                credentials: "same-origin",
                cache: "no-store",
                ...options,
                signal: controller.signal,
            });
            let payload = {};
            try {
                payload = await response.json();
            } catch (_error) {
                if (response.ok) throw new Error("The local cache API returned invalid JSON.");
            }
            if (!response.ok) {
                const message = typeof payload?.error === "string"
                    ? payload.error
                    : `The local cache API returned HTTP ${response.status}.`;
                throw new Error(message);
            }
            return payload;
        } finally {
            pendingControllers.delete(controller);
        }
    }

    function statusEndpoint() {
        const endpoint = new URL(endpoints.status);
        endpoint.searchParams.set("profile_url", profileInput.value.trim());
        return endpoint;
    }

    function answersEndpoint(page = archivePage) {
        const endpoint = new URL(endpoints.answers);
        endpoint.searchParams.set("profile_url", profileInput.value.trim());
        endpoint.searchParams.set("q", searchInput.value.trim());
        endpoint.searchParams.set("page", String(page));
        endpoint.searchParams.set("page_size", "20");
        return endpoint;
    }

    function answerEndpoint(answerId) {
        const endpoint = new URL(endpoints.answers);
        endpoint.pathname = `${endpoint.pathname.replace(/\/$/, "")}/${encodeURIComponent(answerId)}`;
        endpoint.searchParams.set("profile_url", profileInput.value.trim());
        return endpoint;
    }

    async function refreshArchive({page = archivePage, quiet = false} = {}) {
        if (!lastSnapshot?.cache_exists || lastSnapshot?.running) return;
        const generation = ++archiveGeneration;
        searchButton.disabled = true;
        if (!quiet) setArchiveStatus("Reading the local archive…");
        try {
            const payload = await requestJson(answersEndpoint(page));
            if (generation !== archiveGeneration) return;
            renderArchive(payload);
            archiveProfile = String(payload.profile_url || profileInput.value.trim());
        } catch (error) {
            if (error.name === "AbortError" || generation !== archiveGeneration) return;
            archivePanel.hidden = false;
            setArchiveStatus(error.message || "The local archive could not be read.", "error");
        } finally {
            if (generation === archiveGeneration) {
                searchButton.disabled = !lastSnapshot?.cache_exists || Boolean(lastSnapshot?.running);
            }
        }
    }

    async function readCachedAnswer(answerId) {
        if (!/^\d+$/.test(String(answerId || ""))) return;
        const generation = ++detailGeneration;
        detailPanel.hidden = false;
        detailTitle.textContent = `Cached answer ${answerId}`;
        detailMeta.replaceChildren();
        detailBody.textContent = "";
        detailSource.hidden = true;
        setDetailStatus("Reading the stored answer body…");
        try {
            const answer = await requestJson(answerEndpoint(answerId));
            if (generation !== detailGeneration) return;
            renderDetail(answer);
        } catch (error) {
            if (error.name === "AbortError" || generation !== detailGeneration) return;
            setDetailStatus(error.message || "The cached answer could not be read.", "error");
        }
    }

    function clearPollTimer() {
        window.clearTimeout(pollTimer);
        pollTimer = 0;
    }

    function schedulePoll() {
        clearPollTimer();
        if (!lastSnapshot?.running || document.hidden) return;
        pollTimer = window.setTimeout(() => refreshStatus(), pollInterval);
    }

    async function refreshStatus({quiet = false} = {}) {
        const profileUrl = profileInput.value.trim();
        if (!profileUrl) {
            lastSnapshot = null;
            setControlsRunning(false);
            if (!quiet) announce("Enter a Zhihu profile answers URL.");
            return;
        }
        const generation = ++refreshGeneration;
        try {
            const payload = await requestJson(statusEndpoint());
            if (generation !== refreshGeneration) return;
            const snapshot = unwrapStatus(payload);
            renderSnapshot(snapshot);
            if (
                snapshot.cache_exists
                && !snapshot.running
                && archiveProfile !== String(snapshot.profile_url || profileInput.value.trim())
            ) {
                archivePage = 1;
                searchInput.value = "";
                await refreshArchive({page: 1, quiet: true});
            }
            schedulePoll();
        } catch (error) {
            if (error.name === "AbortError" || generation !== refreshGeneration) return;
            announce(
                lastSnapshot?.running
                    ? `${error.message || "Status refresh failed."} Retrying…`
                    : error.message || "The current cache status could not be loaded.",
                "error",
            );
            if (lastSnapshot?.running) schedulePoll();
            else setControlsRunning(false);
        }
    }

    form.addEventListener("input", () => {
        if (!lastSnapshot?.running) startButton.disabled = !form.checkValidity();
    });

    profileInput.addEventListener("input", () => {
        archiveProfile = "";
        archiveGeneration += 1;
        clearDetail();
    });

    searchForm.addEventListener("submit", (event) => {
        event.preventDefault();
        if (searchButton.disabled) return;
        archivePage = 1;
        clearDetail();
        refreshArchive({page: 1});
    });

    previousButton.addEventListener("click", () => {
        if (archivePage <= 1) return;
        clearDetail();
        refreshArchive({page: archivePage - 1});
    });

    nextButton.addEventListener("click", () => {
        if (archivePage >= archivePageCount) return;
        clearDetail();
        refreshArchive({page: archivePage + 1});
    });

    answerList.addEventListener("click", (event) => {
        const trigger = event.target.closest("[data-answer-id]");
        if (!trigger || !answerList.contains(trigger)) return;
        readCachedAnswer(trigger.dataset.answerId);
    });

    detailClose.addEventListener("click", () => clearDetail());

    exampleButton.addEventListener("click", () => {
        profileInput.value = form.dataset.exampleUrl || profileInput.defaultValue;
        searchInput.value = "";
        archiveProfile = "";
        clearDetail();
        startButton.disabled = !form.checkValidity();
        refreshStatus();
    });

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (lastSnapshot?.running || !form.reportValidity()) return;
        clearPollTimer();
        refreshGeneration += 1;
        renderSnapshot({
            running: true,
            phase: "starting",
            message: "Starting the local Zhihu cache…",
            profile_url: profileInput.value.trim(),
            browser: selectedBrowser(),
        });
        try {
            const payload = await requestJson(endpoints.start, {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    profile_url: profileInput.value.trim(),
                    browser: selectedBrowser(),
                }),
            });
            renderSnapshot(unwrapStatus(payload));
            schedulePoll();
        } catch (error) {
            renderSnapshot({
                running: false,
                phase: "failed",
                profile_url: profileInput.value.trim(),
                browser: selectedBrowser(),
                last_error: error.message || "The cache could not start.",
            });
        }
    });

    stopButton.addEventListener("click", async () => {
        if (!lastSnapshot?.running) return;
        clearPollTimer();
        stopButton.disabled = true;
        announce("Requesting a safe stop…");
        try {
            const payload = await requestJson(endpoints.stop, {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: "{}",
            });
            renderSnapshot(unwrapStatus(payload));
            schedulePoll();
        } catch (error) {
            announce(error.message || "The stop request failed. Status polling will continue.", "error");
            stopButton.disabled = false;
            schedulePoll();
        }
    });

    window.addEventListener("pagehide", () => {
        clearPollTimer();
        pendingControllers.forEach((controller) => controller.abort());
        pendingControllers.clear();
    });

    document.addEventListener("visibilitychange", () => {
        if (document.hidden) {
            clearPollTimer();
        } else if (lastSnapshot?.running) {
            refreshStatus();
        }
    });

    setControlsRunning(false);
    refreshStatus({quiet: true});
}
