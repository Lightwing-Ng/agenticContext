/* Code version: v1.1.2-codex.0 */
/*
 * iPhone Agent mode page: setup steps, live preview polling, and phone controls.
 *
 * Preview polling, frame acceptance, pause handling, and gesture timing follow the
 * vendored iPhone Use 0.3.10 widget (MIT, Copyright (c) 2026 ZHONG XIN), adapted from
 * MCP Apps tool calls to same-origin fetch requests against /api/iphone/*.
 */
(() => {
    "use strict";

    const root = document.querySelector("[data-iphone-root]");
    if (!root) return;

    const FRAME_INTERVAL_MS = 250;
    const SLOW_FRAME_INTERVAL_MS = 1000;
    const MAX_FRAME_BACKOFF_MS = 2000;
    const FRAME_TIMEOUT_MS = 3000;
    const ACTION_TIMEOUT_MS = 12000;
    // Typing and app launches may run for the runtime's own call budget before answering.
    const LONG_ACTION_TIMEOUT_MS = 120000;
    const SETUP_TIMEOUT_MS = 45000;
    const STATUS_ACTIVE_INTERVAL_MS = 2000;
    const STATUS_IDLE_INTERVAL_MS = 10000;
    const GESTURE_MAX_AGE_MS = 2500;
    const ACTIVITY_LIMIT = 60;
    const MAX_MESSAGE_LENGTH = 400;
    const MAX_TYPE_CONTINUATIONS = 40;
    const VIEWPORT_RETRY_MS = 2000;
    const FRAME_VIEWPORT_RATIO_TOLERANCE = 0.08;
    const DEFAULT_SCREEN = {width: 440, height: 956};
    const ACTIVE_JOB_STATES = new Set(["queued", "running"]);
    const FAILED_JOB_STATES = new Set(["failed", "interrupted"]);
    const IMAGE_TYPES = new Set(["image/jpeg", "image/png", "image/webp"]);
    // Only the status endpoint decides that the page cannot operate the phone.
    const TERMINAL_CODES = new Set(["operations_disabled", "loopback_required", "iphone_unavailable", "forbidden", "locked"]);
    const PREREQUISITE_CHECKS = ["macos", "full_xcode", "git", "node", "npm"];
    const TEAM_ID_PATTERN = /^[A-Z0-9]{10}$/;
    const BUNDLE_ID_PATTERN = /^[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+){2,}$/;
    const REVERSE_DNS_PATTERN = /^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+){2,}$/;
    const LAUNCH_BUNDLE_PATTERN = /^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+$/;
    const CHECK_LABELS = [
        ["macos", "macOS"],
        ["full_xcode", "Full Xcode"],
        ["git", "Git"],
        ["node", "Node.js 20.19–20.x, 22.12–22.x, or 24+"],
        ["npm", "npm 10 or newer"],
        ["configured", "Signing saved"],
        ["selected_device_present", "Selected iPhone connected"],
        ["paired", "iPhone trusts this Mac"],
        ["developer_mode", "Developer Mode on"],
        ["pinned_source", "WebDriverAgent downloaded"],
    ];
    const LIVE_LABELS = {connecting: "Connecting", live: "Live", paused: "Paused", offline: "Not connected"};
    const EMPTY_LABELS = {
        connecting: "Connecting to the iPhone",
        offline: "Not connected",
        locked: "Unlock the iPhone, then refresh the preview",
        authentication: "Finish signing in on the iPhone, then resume the preview",
        paused: "Preview paused",
        unavailable: "The screen is temporarily unavailable",
        setup: "Finish the setup steps to see the iPhone screen",
        disabled: "iPhone control is unavailable",
    };
    const SETUP_BUSY_LABELS = {
        doctor: "Checking this Mac",
        discover: "Looking for iPhones",
        configure: "Saving signing",
        fetch: "Starting the download",
        build: "Starting the build",
        start: "Starting WebDriverAgent",
        stop: "Stopping WebDriverAgent",
        ready: "Connecting to the iPhone",
    };
    const SETUP_FAILURE_LABELS = {
        doctor: "Mac check failed",
        discover: "iPhone search failed",
        configure: "Signing was not saved",
        fetch: "Download did not start",
        build: "Build did not start",
        start: "Start did not finish",
        stop: "Stop did not finish",
        ready: "Connection failed",
    };
    const SETUP_STEP_FOR_ACTION = {stop: "start"};
    const JOB_LABELS = {
        fetch: {queued: "Download queued", running: "Downloading", succeeded: "Downloaded", failed: "Download failed", stopped: "Download stopped", interrupted: "Download interrupted"},
        build: {queued: "Build queued", running: "Building", succeeded: "Built", failed: "Build failed", stopped: "Build stopped", interrupted: "Build interrupted"},
        start: {queued: "Start queued", running: "Starting", succeeded: "Finished", failed: "Stopped with an error", stopped: "Stopped", interrupted: "Interrupted"},
    };
    const FRIENDLY_ERRORS = {
        device_busy: "The iPhone is busy with another operation. Try again in a moment.",
        preview_paused: "The preview is paused. Resume it first.",
        phone_locked: "Unlock the iPhone, then try again.",
        pua_unreachable: "WebDriverAgent is not reachable. Finish the setup steps and start it.",
        not_ready: "WebDriverAgent is not ready yet. Start it in the setup steps.",
        mirroring_conflict: "Quit iPhone Mirroring on this Mac, unlock the iPhone, then connect again.",
        input_continuation_expired: "Typing was interrupted by another action. Check the field, then send the remaining text again.",
        timeout: "The request timed out. Check the iPhone and try again.",
        network_error: "The page could not reach the local server.",
    };

    const find = (name) => root.querySelector("[data-iphone-" + name + "]");
    const findAll = (selector) => Array.from(root.querySelectorAll(selector));
    const byValue = (attribute, value) => root.querySelector("[" + attribute + '="' + value + '"]');

    const statusCard = find("status-card");
    const statusLabel = find("status-label");
    const statusMessage = find("status-message");
    const statusSpinner = find("status-spinner");
    const statusCheck = find("status-check");
    const notice = find("notice");
    const noticeTitle = find("notice-title");
    const noticeCopy = find("notice-copy");
    const doctorChecks = find("doctor-checks");
    const doctorHints = find("doctor-hints");
    const deviceList = find("device-list");
    const signingForm = find("signing-form");
    const teamInput = find("team-id");
    const bundleInput = find("bundle-id");
    const signingSummary = find("signing-summary");
    const xcodeHints = findAll("[data-iphone-xcode-hint]");
    const setupButtons = findAll("[data-iphone-setup-action]");
    const stepItems = findAll("[data-iphone-step]");
    const liveBadge = find("live-badge");
    const liveText = find("live-text");
    const modelLabel = find("model");
    const device = find("device");
    const screen = find("screen");
    const image = find("image");
    const emptyState = find("empty");
    const emptyText = find("empty-text");
    const marker = find("marker");
    const tools = {
        refresh: byValue("data-iphone-tool", "refresh"),
        home: byValue("data-iphone-tool", "home"),
        screenshot: byValue("data-iphone-tool", "screenshot"),
        pause: byValue("data-iphone-tool", "pause"),
    };
    const typeForm = find("type-form");
    const typeInput = find("type-text");
    const typeSubmit = find("type-submit");
    const typeButton = find("type-send");
    const swipeButtons = findAll("[data-iphone-swipe]");
    const launchForm = find("launch-form");
    const launchInput = find("launch-query");
    const launchButton = find("launch-send");
    const appCandidates = find("app-candidates");
    const hardwareButtons = findAll("[data-iphone-button]");
    const activityLog = find("activity");
    const activityEmpty = find("activity-empty");
    const tunnelToggle = find("tunnel-enabled");
    const tunnelStatus = find("tunnel-status");
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    const defaultModelLabel = modelLabel.textContent;

    // Page usability: decided by the status endpoint, seeded by the server-rendered flags.
    let usable = false;
    let terminal = false;
    // Setup state.
    let status = null;
    let doctor = null;
    let doctorRequested = false;
    let devices = [];
    let selectedUdid = "";
    let setupAction = "";
    let readyResult = null;
    let statusTimer = 0;
    let statusInFlight = false;
    let statusQueued = false;
    // Preview state.
    let polling = false;
    let frameTimer = 0;
    let frameInFlight = false;
    let frameSeq = 0;
    let frameGeneration = 0;
    let eventId = 0;
    let streamId = "";
    let failures = 0;
    let dimensions = null;
    let viewport = null;
    let viewportRefreshInFlight = false;
    let viewportRefreshAfter = 0;
    let viewportRefreshError = "";
    let serviceUnavailable = false;
    let paused = false;
    let pausedByPage = false;
    let lastGoodFrame = null;
    let suspended = false;
    // Phone actions.
    let acting = "";
    let searchingApps = false;
    let tunnelState = null;
    let tunnelRequesting = false;
    let pointerStart = null;
    const gestureEffects = new Set();

    class RequestError extends Error {
        constructor(code, message, httpStatus = 0, details = {}) {
            super(message);
            this.code = code;
            this.httpStatus = httpStatus;
            this.details = details && typeof details === "object" ? details : {};
        }
    }

    const isObject = (value) => Boolean(value) && typeof value === "object" && !Array.isArray(value);
    const validSize = (value) => isObject(value) && Number.isFinite(value.width) && Number.isFinite(value.height)
        && value.width > 0 && value.height > 0;
    const validPoint = (value) => isObject(value) && Number.isFinite(value.x) && Number.isFinite(value.y);
    const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
    const hasImage = () => Boolean(image.getAttribute("src"));
    const shorten = (text) => {
        const value = String(text || "").replace(/\s+/g, " ").trim();
        return value.length > MAX_MESSAGE_LENGTH ? value.slice(0, MAX_MESSAGE_LENGTH - 1) + "…" : value;
    };
    const stringList = (value) => (Array.isArray(value) ? value : [])
        .filter((item) => typeof item === "string" && item.trim())
        .map((item) => shorten(item));

    function errorFromResponse(payload, httpStatus) {
        const error = isObject(payload) ? payload.error : undefined;
        if (isObject(error)) {
            return new RequestError(
                String(error.code || "request_failed"),
                String(error.message || "The iPhone request failed."),
                httpStatus,
                error,
            );
        }
        if (typeof error === "string" && error) {
            // The shared Agent gate answers 409 with a plain message when operations are disabled.
            return new RequestError(httpStatus === 409 ? "operations_disabled" : "request_failed", error, httpStatus);
        }
        if (httpStatus === 401) return new RequestError("locked", "Unlock Agent access, then reload this page.", httpStatus);
        if (httpStatus === 403) return new RequestError("forbidden", "This request is not allowed from here.", httpStatus);
        return new RequestError("request_failed", "The iPhone request failed (HTTP " + httpStatus + ").", httpStatus);
    }

    function resultFailure(payload, fallback) {
        const hints = stringList(payload.next_steps);
        if (isObject(payload.error)) {
            return new RequestError(
                String(payload.error.code || "request_failed"),
                String(payload.error.message || fallback),
                200,
                {...payload.error, next_steps: hints},
            );
        }
        const message = typeof payload.error === "string" && payload.error ? payload.error : fallback;
        return new RequestError("setup_failed", message, 200, {next_steps: hints});
    }

    async function request(url, {method = "GET", body, timeout = ACTION_TIMEOUT_MS, as = "json"} = {}) {
        const controller = new AbortController();
        const timer = window.setTimeout(() => controller.abort(), timeout);
        try {
            const headers = {"Accept": as === "blob" ? "image/png, application/json" : "application/json"};
            if (body !== undefined) headers["Content-Type"] = "application/json";
            const response = await fetch(url, {
                method,
                headers,
                body: body === undefined ? undefined : JSON.stringify(body),
                credentials: "same-origin",
                cache: "no-store",
                signal: controller.signal,
            });
            if (!response.ok) {
                let payload = null;
                try {
                    payload = await response.json();
                } catch (_error) {
                    payload = null;
                }
                throw errorFromResponse(payload, response.status);
            }
            if (as === "blob") return await response.blob();
            const payload = await response.json();
            if (!isObject(payload)) {
                throw new RequestError("invalid_response", "The local server sent an unexpected response.", response.status);
            }
            return payload;
        } catch (error) {
            if (error instanceof RequestError) throw error;
            if (controller.signal.aborted) throw new RequestError("timeout", FRIENDLY_ERRORS.timeout);
            if (error instanceof SyntaxError) {
                throw new RequestError("invalid_response", "The local server sent an unexpected response.");
            }
            throw new RequestError("network_error", FRIENDLY_ERRORS.network_error);
        } finally {
            window.clearTimeout(timer);
        }
    }

    function errorMessage(error) {
        if (!(error instanceof RequestError)) return "Something went wrong. Try again.";
        return shorten(FRIENDLY_ERRORS[error.code] || error.message || "The iPhone request failed.");
    }

    function errorWithHints(error) {
        const hints = error instanceof RequestError ? stringList(error.details.next_steps) : [];
        return [errorMessage(error), ...hints].join(" ");
    }

    // Activity log.

    function logActivity(text, tone = "info") {
        const entry = document.createElement("li");
        entry.className = "iphone-activity-entry";
        entry.dataset.tone = tone;
        const now = new Date();
        const time = document.createElement("time");
        time.className = "iphone-activity-time";
        time.dateTime = now.toISOString();
        time.textContent = now.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
        const copy = document.createElement("span");
        copy.className = "iphone-activity-text";
        copy.textContent = shorten(text);
        entry.append(time, copy);
        activityLog.append(entry);
        while (activityLog.children.length > ACTIVITY_LIMIT) activityLog.firstElementChild.remove();
        activityLog.hidden = false;
        activityEmpty.hidden = true;
        activityLog.scrollTop = activityLog.scrollHeight;
    }

    // Sidebar status, notice, and setup rendering.

    function setStatusLine(label, message = "", {tone = "info", busy = false, ok = false} = {}) {
        statusLabel.textContent = label;
        statusMessage.textContent = message;
        statusMessage.hidden = !message;
        statusSpinner.hidden = !busy;
        statusCheck.hidden = !ok;
        statusCard.dataset.tone = tone;
    }

    function setStepMessage(step, text, tone = "info") {
        const element = byValue("data-iphone-step-message", step);
        if (!element) return;
        element.textContent = text || "";
        element.dataset.tone = tone;
        element.hidden = !text;
    }

    function showNotice(title, copy) {
        noticeTitle.textContent = title;
        noticeCopy.textContent = copy;
        notice.hidden = false;
    }

    function enterTerminal(error) {
        terminal = true;
        usable = false;
        stopPolling();
        window.clearTimeout(statusTimer);
        statusTimer = 0;
        let title = "iPhone control is unavailable";
        let copy = errorMessage(error);
        if (error.code === "operations_disabled") {
            title = "iPhone control is turned off";
            copy = "This instance runs with Agent operations disabled, so it cannot set up or control an iPhone.";
        } else if (error.code === "loopback_required") {
            title = "Open this page on the Mac";
            copy = "The iPhone can only be set up and controlled from the Mac that runs this app, not from another device on the network.";
        } else if (error.code === "locked") {
            title = "Agent access is locked";
        }
        showNotice(title, copy);
        setTunnelStatus("iPhone control is unavailable on this page.");
        setStatusLine("Unavailable", copy, {tone: "error"});
        setLive("offline");
        if (!hasImage()) showEmpty("disabled");
        syncControls();
    }

    function setupState() {
        const setup = isObject(status) && isObject(status.setup) ? status.setup : {};
        return {
            configured: setup.configured === true,
            config: isObject(setup.config) ? setup.config : {},
            jobs: Array.isArray(setup.jobs) ? setup.jobs.filter(isObject) : [],
            service: isObject(setup.service) ? setup.service : {},
            error: setup.ok === false && typeof setup.error === "string" ? setup.error : "",
        };
    }

    const isActive = (job) => Boolean(job) && ACTIVE_JOB_STATES.has(job.state);
    const latestJob = (jobs, actions) => jobs.filter((job) => actions.includes(job.action)).at(-1) || null;
    const doctorChecksResult = () => (isObject(doctor) && isObject(doctor.checks) ? doctor.checks : null);

    function setupJobPending() {
        if (setupAction) return true;
        const {jobs, service} = setupState();
        return jobs.some((job) => isActive(job)
            && !((job.action === "start" || job.action === "recover") && service.ready === true));
    }

    function stepStateForJob(job, complete) {
        if (complete) return "complete";
        if (isActive(job)) return "running";
        if (job && FAILED_JOB_STATES.has(job.state)) return "attention";
        return "pending";
    }

    function renderJob(step, job, service) {
        const line = byValue("data-iphone-job-state", step);
        const log = byValue("data-iphone-job-log", step);
        if (!job) {
            line.hidden = true;
            log.hidden = true;
            log.textContent = "";
            return;
        }
        let text = JOB_LABELS[step][job.state] || shorten(job.state || "Unknown state");
        if (step === "start" && isActive(job)) {
            text = service.ready === true ? "Running and serving the iPhone" : "Starting WebDriverAgent";
        }
        const details = [];
        if (typeof job.error === "string" && job.error) details.push(shorten(job.error));
        details.push(...stringList(job.next_steps));
        line.textContent = details.length ? text + ". " + details.join(" ") : text + ".";
        line.dataset.tone = FAILED_JOB_STATES.has(job.state) ? "error" : "info";
        line.hidden = false;
        const tail = typeof job.log_tail === "string" ? job.log_tail.trim() : "";
        const following = log.hidden || log.scrollHeight - log.scrollTop - log.clientHeight < 8;
        if (log.textContent !== tail) log.textContent = tail;
        log.hidden = !tail;
        if (following) log.scrollTop = log.scrollHeight;
    }

    function renderSigningSummary(config, configured) {
        if (!configured) {
            signingSummary.hidden = true;
            teamInput.placeholder = "";
            return;
        }
        const team = typeof config.team_id === "string" ? config.team_id.slice(-4) : "";
        const parts = [];
        if (team) parts.push("team ending " + team);
        if (typeof config.bundle_id === "string" && config.bundle_id) parts.push(config.bundle_id);
        signingSummary.textContent = "Saved" + (parts.length ? ": " + parts.join(", ") : "") + ".";
        signingSummary.hidden = false;
        teamInput.placeholder = team ? "Saved, ending " + team : "";
        if (!bundleInput.value && document.activeElement !== bundleInput
            && typeof config.bundle_id === "string") {
            bundleInput.value = config.bundle_id;
        }
    }

    function renderStatusLine() {
        if (terminal) return;
        if (!status) {
            setStatusLine("Checking", "", {busy: true});
            return;
        }
        const {configured, jobs, service, error} = setupState();
        if (setupAction) {
            setStatusLine(SETUP_BUSY_LABELS[setupAction], "", {busy: true});
        } else if (error) {
            setStatusLine("Status unavailable", shorten(error), {tone: "error"});
        } else if (readyResult && readyResult.ready === true && service.ready === true) {
            setStatusLine("Ready", "The iPhone accepts commands.", {ok: true});
        } else if (service.ready === true) {
            setStatusLine("WebDriverAgent running", "Connect to verify that the iPhone accepts commands.");
        } else if (jobs.some(isActive)) {
            const job = jobs.filter(isActive).at(-1);
            const step = job.action === "recover" ? "start" : job.action;
            const label = (JOB_LABELS[step] || {})[job.state] || "Working";
            setStatusLine(label, "This can take a few minutes.", {busy: true});
        } else if (!configured) {
            setStatusLine("Setup needed", "Follow the steps below to connect a USB iPhone.");
        } else {
            setStatusLine("Not running", "Start WebDriverAgent to connect the iPhone.");
        }
    }

    function renderSetup() {
        const {configured, config, jobs, service} = setupState();
        const checks = doctorChecksResult();
        const fetchJob = latestJob(jobs, ["fetch"]);
        const buildJob = latestJob(jobs, ["build"]);
        const startJob = latestJob(jobs, ["start", "recover"]);
        if (readyResult && readyResult.ready === true && service.ready !== true && status) {
            readyResult = null;
            root.dataset.iphoneReady = "false";
        }
        const prerequisitesMet = Boolean(checks) && PREREQUISITE_CHECKS.every((key) => checks[key] === true);
        const states = {
            doctor: !checks ? "pending" : prerequisitesMet ? "complete" : "attention",
            discover: selectedUdid || config.udid ? "complete" : "pending",
            configure: configured ? "complete" : "pending",
            fetch: stepStateForJob(fetchJob, (checks && checks.pinned_source === true) || (fetchJob && fetchJob.state === "succeeded")),
            build: stepStateForJob(buildJob, (buildJob && buildJob.state === "succeeded") || service.ready === true),
            start: stepStateForJob(startJob, service.ready === true),
            ready: readyResult && readyResult.ready === true ? "complete" : readyResult ? "attention" : "pending",
        };
        stepItems.forEach((item) => {
            item.dataset.stepState = states[item.dataset.iphoneStep] || "pending";
        });
        renderJob("fetch", fetchJob, service);
        renderJob("build", buildJob, service);
        renderJob("start", startJob, service);
        renderSigningSummary(config, configured);
        renderStatusLine();
    }

    function renderDoctor() {
        doctorChecks.replaceChildren();
        doctorHints.replaceChildren();
        const checks = doctorChecksResult();
        if (!checks) {
            doctorChecks.hidden = true;
            doctorHints.hidden = true;
            return;
        }
        CHECK_LABELS.forEach(([key, label]) => {
            if (!(key in checks)) return;
            const passed = checks[key] === true;
            const item = document.createElement("li");
            item.className = "iphone-check";
            item.dataset.checkState = passed ? "pass" : "fail";
            const mark = document.createElement("span");
            mark.className = "iphone-check-mark";
            mark.setAttribute("aria-hidden", "true");
            const text = document.createElement("span");
            text.textContent = label;
            const state = document.createElement("span");
            state.className = "iphone-visually-hidden";
            state.textContent = passed ? ": ready" : ": needs attention";
            text.append(state);
            item.append(mark, text);
            doctorChecks.append(item);
        });
        doctorChecks.hidden = !doctorChecks.children.length;
        stringList(doctor.next_steps).forEach((hint) => {
            const item = document.createElement("li");
            item.textContent = hint;
            doctorHints.append(item);
        });
        doctorHints.hidden = !doctorHints.children.length;
    }

    function deviceDescription(item) {
        const parts = [];
        if (typeof item.model === "string" && item.model) parts.push(item.model);
        if (typeof item.ios_version === "string" && item.ios_version && item.ios_version !== "unknown") {
            parts.push("iOS " + item.ios_version);
        }
        if (typeof item.pairing_state === "string" && !["paired", "unknown"].includes(item.pairing_state)) {
            parts.push("not paired");
        }
        if (item.developer_mode === "disabled") parts.push("Developer Mode off");
        return parts.join(" · ");
    }

    function renderDevices() {
        deviceList.replaceChildren();
        const valid = devices.filter((item) => isObject(item) && typeof item.udid === "string" && item.udid);
        if (!selectedUdid && valid.length === 1) selectedUdid = valid[0].udid;
        valid.forEach((item, index) => {
            const row = document.createElement("div");
            row.className = "selection-list-row iphone-device-option";
            const label = document.createElement("label");
            label.className = "selection-list-identity iphone-device-identity";
            label.htmlFor = "iphone_device_" + index;
            const input = document.createElement("input");
            input.type = "radio";
            input.name = "iphone_device";
            input.id = "iphone_device_" + index;
            input.className = "selection-list-input";
            input.value = item.udid;
            input.checked = item.udid === selectedUdid;
            input.dataset.iphoneDeviceOption = "";
            input.addEventListener("change", () => {
                if (!input.checked) return;
                selectedUdid = input.value;
                renderSetup();
                syncControls();
            });
            const mark = document.createElement("span");
            mark.className = "selection-list-mark";
            mark.setAttribute("aria-hidden", "true");
            const text = document.createElement("span");
            text.className = "iphone-device-text";
            const name = document.createElement("span");
            name.className = "selection-list-name";
            name.textContent = typeof item.name === "string" && item.name ? item.name : "iPhone";
            text.append(name);
            const description = deviceDescription(item);
            if (description) {
                const meta = document.createElement("span");
                meta.className = "iphone-device-meta";
                meta.textContent = description;
                text.append(meta);
            }
            label.append(input, mark, text);
            row.append(label);
            deviceList.append(row);
        });
        deviceList.hidden = !deviceList.children.length;
    }

    function syncControls() {
        const {configured, jobs, service} = setupState();
        const locked = !usable || Boolean(setupAction);
        const anyActive = jobs.some(isActive);
        const startActive = isActive(latestJob(jobs, ["start", "recover"]));
        const checks = doctorChecksResult();
        const xcodeReady = Boolean(checks) && checks.full_xcode === true;
        // Save stays available so its validation can explain a missing iPhone or field.
        const disabled = {
            doctor: locked,
            discover: locked,
            configure: locked,
            fetch: locked || anyActive,
            build: locked || anyActive || !configured || !xcodeReady,
            start: locked || anyActive || !configured || !xcodeReady || service.ready === true,
            stop: locked || !startActive,
            ready: locked || service.ready !== true,
        };
        setupButtons.forEach((button) => {
            button.disabled = disabled[button.dataset.iphoneSetupAction] !== false;
        });
        const hint = !usable || xcodeReady ? ""
            : checks ? "Requires full Xcode. Install it, open it once, select it with xcode-select, then run the Mac check again."
                : "Run the Mac check first.";
        xcodeHints.forEach((element) => {
            element.textContent = hint;
            element.hidden = !hint;
        });
        deviceList.querySelectorAll("input").forEach((input) => {
            input.disabled = locked;
        });
        teamInput.disabled = !usable;
        bundleInput.disabled = !usable;

        const operable = usable && polling;
        const ready = operable && !acting && !paused && !viewportRefreshInFlight;
        tools.refresh.disabled = !operable || Boolean(acting);
        tools.home.disabled = !ready;
        tools.screenshot.disabled = !ready;
        tools.pause.disabled = !operable || Boolean(acting);
        const pauseLabel = paused ? "Resume preview" : "Pause preview";
        tools.pause.dataset.paused = String(paused);
        tools.pause.setAttribute("aria-label", pauseLabel);
        tools.pause.title = pauseLabel;
        typeInput.disabled = !usable;
        typeSubmit.disabled = !usable;
        typeButton.disabled = !ready || !typeInput.value.length;
        swipeButtons.forEach((button) => {
            button.disabled = !ready;
        });
        launchInput.disabled = !usable;
        launchButton.disabled = !ready || searchingApps || !launchInput.value.trim();
        appCandidates.querySelectorAll("button").forEach((button) => {
            button.disabled = !ready;
        });
        hardwareButtons.forEach((button) => {
            button.disabled = !ready;
        });
        tunnelToggle.disabled = !usable || tunnelRequesting || !tunnelState?.available;
        screen.dataset.interactive = String(canTouch());
    }

    // Status polling.

    function applyStatus(payload) {
        if (payload.supported === false) {
            const reason = typeof payload.reason === "string" && payload.reason
                ? payload.reason : "Setting up and controlling an iPhone needs macOS.";
            enterTerminal(new RequestError("iphone_unavailable", reason, 200));
            return;
        }
        if (payload.operations_enabled === false) {
            enterTerminal(new RequestError("operations_disabled", "", 200));
            return;
        }
        status = payload;
        if (!usable) {
            usable = true;
            notice.hidden = true;
            if (!hasImage()) showEmpty("setup");
        }
        const {configured, config, service} = setupState();
        if (!selectedUdid && typeof config.udid === "string" && config.udid) selectedUdid = config.udid;
        if (isObject(payload.preview) && typeof payload.preview.paused === "boolean" && !polling) {
            paused = payload.preview.paused;
        }
        renderSetup();
        if (configured) startPolling();
        if (!doctorRequested && service.ready !== true) {
            // Read-only diagnostics gate Build and Start on a full Xcode install.
            doctorRequested = true;
            window.setTimeout(() => runSetupAction("doctor", {quiet: true}), 0);
        }
        syncControls();
        loadTunnelState();
    }

    function scheduleStatus(delay) {
        window.clearTimeout(statusTimer);
        statusTimer = 0;
        if (terminal || suspended || document.hidden) return;
        const wait = Number.isFinite(delay) ? delay
            : setupJobPending() ? STATUS_ACTIVE_INTERVAL_MS : STATUS_IDLE_INTERVAL_MS;
        statusTimer = window.setTimeout(refreshStatus, wait);
    }

    async function refreshStatus() {
        window.clearTimeout(statusTimer);
        statusTimer = 0;
        if (terminal || suspended || document.hidden) return;
        if (statusInFlight) {
            statusQueued = true;
            return;
        }
        statusInFlight = true;
        statusQueued = false;
        try {
            applyStatus(await request("/api/iphone/status", {timeout: ACTION_TIMEOUT_MS}));
        } catch (error) {
            if (error instanceof RequestError && TERMINAL_CODES.has(error.code)) {
                enterTerminal(error);
            } else if (!status) {
                setStatusLine("Status unavailable", errorMessage(error), {tone: "error"});
            }
        } finally {
            statusInFlight = false;
            scheduleStatus(statusQueued ? 0 : undefined);
        }
    }

    // ChatGPT Tunnel access remains an explicit, process-scoped local preference.

    function setTunnelStatus(message, tone = "info") {
        tunnelStatus.textContent = message;
        tunnelStatus.dataset.tone = tone;
    }

    function applyTunnelState(payload) {
        if (typeof payload.available !== "boolean" || typeof payload.enabled !== "boolean"
            || (payload.enabled && !payload.available)) {
            throw new RequestError("invalid_response", "The local server sent an unexpected access status.");
        }
        tunnelState = {available: payload.available, enabled: payload.enabled};
        tunnelToggle.checked = payload.enabled;
        tunnelToggle.indeterminate = false;
        setTunnelStatus(!payload.available ? "iPhone access through ChatGPT Tunnel is unavailable."
            : payload.enabled ? "On for this app session. Reconnect ChatGPT to load the iPhone tools."
                : "Off. ChatGPT cannot start new iPhone actions through this Tunnel.");
    }

    async function loadTunnelState() {
        if (!usable || tunnelRequesting) return;
        tunnelRequesting = true;
        if (!tunnelState) setTunnelStatus("Checking access status.");
        syncControls();
        try {
            const payload = await request("/api/iphone/tunnel");
            if (usable) applyTunnelState(payload);
        } catch (error) {
            tunnelState = null;
            tunnelToggle.indeterminate = true;
            setTunnelStatus("Access status is unavailable. " + errorMessage(error), "error");
        } finally {
            tunnelRequesting = false;
            syncControls();
        }
    }

    async function changeTunnelAccess() {
        if (!usable || tunnelRequesting || !tunnelState?.available) return;
        const enabled = tunnelToggle.checked;
        let unconfirmed = false;
        tunnelRequesting = true;
        setTunnelStatus("Saving access preference.");
        syncControls();
        try {
            const payload = await request("/api/iphone/tunnel", {method: "POST", body: {enabled}});
            applyTunnelState(payload);
            logActivity(payload.enabled ? "Enabled iPhone access through ChatGPT Tunnel."
                : "Turned off iPhone access through ChatGPT Tunnel.", "ok");
        } catch (error) {
            // A lost response may follow a successful grant: read it back instead of replaying it.
            unconfirmed = true;
            tunnelState = null;
            tunnelToggle.indeterminate = true;
            logActivity("Tunnel access could not be confirmed: " + errorMessage(error), "error");
            setTunnelStatus("Access status could not be confirmed. Checking again.", "error");
        } finally {
            tunnelRequesting = false;
            syncControls();
            if (unconfirmed) await loadTunnelState();
        }
    }

    // Setup actions.

    function setupRequest(action) {
        if (action === "doctor") return {url: "/api/iphone/doctor", body: {}};
        if (action === "ready") return {url: "/api/iphone/ready", body: {}};
        if (action === "configure") {
            const teamId = teamInput.value.trim().toUpperCase();
            const bundleId = bundleInput.value.trim();
            const {config} = setupState();
            const udid = selectedUdid || (typeof config.udid === "string" ? config.udid : "");
            teamInput.value = teamId;
            bundleInput.value = bundleId;
            if (!udid) return {invalid: "Find and select an iPhone first.", focus: null};
            if (!TEAM_ID_PATTERN.test(teamId)) {
                return {invalid: "Enter the 10-character Team ID from Xcode Settings > Accounts.", focus: teamInput};
            }
            if (!BUNDLE_ID_PATTERN.test(bundleId)) {
                return {invalid: "Enter a reverse-DNS bundle ID with at least three parts, such as com.yourname.wda.", focus: bundleInput};
            }
            return {url: "/api/iphone/setup", body: {action, udid, team_id: teamId, bundle_id: bundleId}};
        }
        if (action === "stop") {
            const job = latestJob(setupState().jobs, ["start", "recover"]);
            return {url: "/api/iphone/setup", body: isActive(job) && typeof job.id === "string" ? {action, job_id: job.id} : {action}};
        }
        return {url: "/api/iphone/setup", body: {action}};
    }

    function handleSetupResult(action, payload, quiet) {
        const step = SETUP_STEP_FOR_ACTION[action] || action;
        if (action === "doctor") {
            doctor = payload;
            if (Array.isArray(payload.devices) && payload.devices.length) {
                devices = payload.devices;
                renderDevices();
            }
            renderDoctor();
            const checks = doctorChecksResult() || {};
            const ready = PREREQUISITE_CHECKS.every((key) => checks[key] === true);
            setStepMessage(step, ready ? "This Mac has the tools it needs." : "Some checks need attention. Follow the suggestions above.", ready ? "ok" : "info");
            if (!quiet) logActivity(ready ? "Mac check passed." : "Mac check found items to fix.", ready ? "ok" : "info");
            return;
        }
        if (action === "discover") {
            devices = Array.isArray(payload.devices) ? payload.devices : [];
            renderDevices();
            const count = deviceList.children.length;
            if (count) {
                setStepMessage(step, count === 1 ? "Found 1 iPhone." : "Found " + count + " iPhones. Choose one.", "ok");
            } else {
                setStepMessage(step, ["No iPhone found.", ...stringList(payload.next_steps)].join(" "), "error");
            }
            logActivity(count ? "Found " + count + (count === 1 ? " iPhone." : " iPhones.") : "No iPhone found.", count ? "ok" : "error");
            return;
        }
        if (action === "configure") {
            teamInput.value = "";
            setStepMessage(step, "Signing saved.", "ok");
            logActivity("Signing saved.", "ok");
            return;
        }
        if (action === "ready") {
            readyResult = payload;
            if (payload.ready === true) {
                root.dataset.iphoneReady = "true";
                setStepMessage(step, "The iPhone is ready.", "ok");
                logActivity("Connected. The iPhone accepts commands.", "ok");
                scheduleFrameNow();
            } else {
                root.dataset.iphoneReady = "false";
                const message = typeof payload.message === "string" && payload.message
                    ? shorten(payload.message) : "The iPhone is not ready yet. Try again in a moment.";
                setStepMessage(step, message, "info");
                logActivity("Not ready yet: " + message, "info");
            }
            return;
        }
        if (action === "stop") {
            const stopped = Array.isArray(payload.stopped_jobs) ? payload.stopped_jobs.length : 0;
            setStepMessage(step, stopped ? "WebDriverAgent is stopping." : "Nothing was running.", "ok");
            logActivity(stopped ? "Stopped WebDriverAgent." : "Nothing was running to stop.", "info");
            return;
        }
        const started = {fetch: "Download started.", build: "Build started.", start: "Start requested."}[action];
        let message = started;
        if (payload.already_ready) message = "WebDriverAgent is already running.";
        else if (payload.already_running) message = "Already running.";
        setStepMessage(step, message, "ok");
        logActivity(message, "ok");
    }

    async function runSetupAction(action, {quiet = false} = {}) {
        if (!usable || setupAction) return;
        const step = SETUP_STEP_FOR_ACTION[action] || action;
        const prepared = setupRequest(action);
        if (prepared.invalid) {
            setStepMessage(step, prepared.invalid, "error");
            prepared.focus?.focus();
            return;
        }
        setupAction = action;
        setStepMessage(step, "");
        renderStatusLine();
        syncControls();
        try {
            const payload = await request(prepared.url, {method: "POST", body: prepared.body, timeout: SETUP_TIMEOUT_MS});
            // Doctor reports unmet checks with ok=false; that is a result, not a failure.
            if (payload.ok === false && action !== "doctor") throw resultFailure(payload, "The setup step did not finish.");
            handleSetupResult(action, payload, quiet);
        } catch (error) {
            if (action === "ready") {
                readyResult = {ready: false};
                root.dataset.iphoneReady = "false";
            }
            const message = errorWithHints(error);
            setStepMessage(step, message, "error");
            if (!quiet) logActivity(SETUP_FAILURE_LABELS[action] + ": " + message, "error");
        } finally {
            setupAction = "";
            renderSetup();
            syncControls();
            refreshStatus();
        }
    }

    // Preview.

    function showEmpty(kind) {
        emptyState.dataset.state = kind;
        emptyText.textContent = EMPTY_LABELS[kind];
    }

    function setLive(kind) {
        if (!hasImage()) showEmpty(kind === "paused" ? "paused" : kind === "offline" ? "offline" : "connecting");
        root.dataset.iphoneLive = kind;
        liveBadge.dataset.state = kind;
        liveText.textContent = LIVE_LABELS[kind];
        liveBadge.title = "";
    }

    function fitFrame() {
        const size = dimensions || viewport || DEFAULT_SCREEN;
        device.style.setProperty("--iphone-screen-ratio", String(size.width / size.height));
        device.dataset.orientation = size.width > size.height ? "landscape" : "portrait";
    }

    function finishGesture(effect) {
        if (!gestureEffects.delete(effect)) return;
        effect.animation?.cancel();
        if (effect.timer) window.clearTimeout(effect.timer);
        effect.node.hidden = true;
        if (effect.node !== marker) effect.node.remove();
    }

    function hideGestures() {
        for (const effect of Array.from(gestureEffects)) finishGesture(effect);
    }

    function clearFrame() {
        const hadDimensions = Boolean(dimensions);
        frameGeneration += 1;
        hideGestures();
        image.removeAttribute("src");
        image.hidden = true;
        emptyState.hidden = false;
        dimensions = null;
        lastGoodFrame = null;
        frameSeq = 0;
        root.dataset.frameSeq = "0";
        root.dataset.iphoneBusy = "false";
        if (hadDimensions) fitFrame();
    }

    function retainFrame() {
        hideGestures();
        root.dataset.iphoneBusy = "false";
    }

    function pointStyle(point, size) {
        return {
            left: clamp(point.x / size.width, 0, 1) * 100 + "%",
            top: clamp(point.y / size.height, 0, 1) * 100 + "%",
        };
    }

    function showGesture(gesture) {
        if (image.hidden) return;
        const size = validSize(gesture.viewport) ? gesture.viewport : viewport;
        if (!validSize(size)) return;
        if (dimensions && Math.abs((size.width / size.height) / (dimensions.width / dimensions.height) - 1) > 0.08) return;
        const point = gesture.kind === "tap" ? gesture.point : gesture.from;
        if (!validPoint(point)) return;
        if (gesture.kind === "drag" && !validPoint(gesture.to)) return;
        // Overlapping indications keep independent lifetimes, so a new tap cannot erase one.
        const node = marker.hidden ? marker : marker.cloneNode(false);
        if (node !== marker) {
            node.removeAttribute("data-iphone-marker");
            screen.append(node);
        }
        Object.assign(node.style, pointStyle(point, size));
        node.hidden = false;
        const effect = {node};
        gestureEffects.add(effect);
        const movement = clamp(Number(gesture.duration_ms) || 650, 600, 1000);
        const duration = gesture.kind === "drag" ? movement + 1000 : 1600;
        if (reducedMotion.matches || typeof node.animate !== "function") {
            if (gesture.kind === "drag") Object.assign(node.style, pointStyle(gesture.to, size));
            effect.timer = window.setTimeout(() => finishGesture(effect), duration);
            return;
        }
        const delta = gesture.kind === "drag"
            ? "translate(" + ((gesture.to.x - point.x) / size.width * screen.clientWidth) + "px, "
                + ((gesture.to.y - point.y) / size.height * screen.clientHeight) + "px)"
            : "";
        const frames = gesture.kind === "drag"
            ? [
                {transform: "translate(0, 0) scale(.96)", opacity: 0},
                {transform: "translate(0, 0) scale(1)", opacity: 1, offset: 80 / duration},
                {transform: "translate(0, 0) scale(.92)", opacity: 1, offset: 200 / duration, easing: "cubic-bezier(.3,0,.2,1)"},
                {transform: delta + " scale(.92)", opacity: 1, offset: (movement + 200) / duration},
                {transform: delta + " scale(1.06)", opacity: 1, offset: (movement + 380) / duration},
                {transform: delta + " scale(1)", opacity: 1, offset: (duration - 350) / duration},
                {transform: delta + " scale(1)", opacity: 1, offset: (duration - 200) / duration},
                {transform: delta + " scale(.98)", opacity: 0},
            ]
            : [
                {transform: "scale(.96)", opacity: 0},
                {transform: "scale(1)", opacity: 1, offset: 0.06},
                {transform: "scale(.9)", opacity: 1, offset: 0.16},
                {transform: "scale(1.06)", opacity: 1, offset: 0.28},
                {transform: "scale(1)", opacity: 1, offset: 0.4},
                {transform: "scale(1)", opacity: 1, offset: 0.875},
                {transform: "scale(.98)", opacity: 0},
            ];
        effect.animation = node.animate(frames, {duration, easing: "linear"});
        effect.animation.onfinish = () => finishGesture(effect);
    }

    function consume(preview) {
        if (!isObject(preview)) return;
        if (typeof preview.service_ready === "boolean") serviceUnavailable = !preview.service_ready;
        if (typeof preview.stream_id === "string" && preview.stream_id !== streamId) {
            // Sequence numbers restart with a new stream; keep the last pixels until it delivers.
            streamId = preview.stream_id;
            frameSeq = 0;
            eventId = 0;
            retainFrame();
        }
        if (isObject(preview.device) && typeof preview.device.model === "string" && preview.device.model) {
            modelLabel.textContent = preview.device.model;
        } else if (!modelLabel.textContent) {
            modelLabel.textContent = defaultModelLabel;
        }
        if (validSize(preview.viewport)
            && (!viewport || viewport.width !== preview.viewport.width || viewport.height !== preview.viewport.height)) {
            pointerStart = null;
            viewport = {width: preview.viewport.width, height: preview.viewport.height};
            if (!dimensions) fitFrame();
        }
        if (preview.paused === true) {
            clearFrame();
            paused = true;
            setLive("paused");
            const reason = preview.pause_reason;
            if (reason === "device_locked") {
                liveBadge.title = "Unlock the iPhone, then refresh the preview.";
                showEmpty("locked");
            } else if (reason === "authentication" && !pausedByPage) {
                showEmpty("authentication");
            } else {
                showEmpty("paused");
            }
            syncControls();
            return;
        }
        if (preview.paused === false) {
            paused = false;
            pausedByPage = false;
        }
        if (preview.frame_available === false) {
            retainFrame();
            // Capture has no frame yet while USB streaming starts; that is still connecting.
            setLive(hasImage() || serviceUnavailable ? "offline" : "connecting");
        } else if (preview.frame_available === true || preview.frame) {
            setLive("live");
        }
        const frame = preview.frame;
        if (isObject(frame) && validSize(frame) && Number.isInteger(frame.seq) && frame.seq > frameSeq
            && typeof frame.data === "string" && frame.data.length > 0 && IMAGE_TYPES.has(frame.mimeType)) {
            frameSeq = frame.seq;
            serviceUnavailable = false;
            root.dataset.frameSeq = String(frame.seq);
            if (!dimensions || dimensions.width !== frame.width || dimensions.height !== frame.height) {
                pointerStart = null;
                dimensions = {width: frame.width, height: frame.height};
                fitFrame();
            }
            image.src = "data:" + frame.mimeType + ";base64," + frame.data;
            image.hidden = false;
            emptyState.hidden = true;
        }
        if (typeof preview.busy === "boolean") root.dataset.iphoneBusy = String(preview.busy);
        if (Array.isArray(preview.events)) {
            const now = Number.isFinite(preview.server_time) ? preview.server_time : Date.now();
            preview.events.forEach((gesture) => {
                if (!isObject(gesture) || !Number.isInteger(gesture.id) || gesture.id <= eventId) return;
                eventId = gesture.id;
                const age = now - gesture.at;
                if (Number.isFinite(age) && age >= -1000 && age <= GESTURE_MAX_AGE_MS
                    && (gesture.kind === "tap" || gesture.kind === "drag")) {
                    showGesture(gesture);
                }
            });
        }
        syncControls();
        refreshViewport();
    }

    const visible = () => polling && usable && !suspended && !document.hidden;

    function schedule(delay) {
        if (!visible() || frameTimer) return;
        frameTimer = window.setTimeout(() => {
            frameTimer = 0;
            poll();
        }, delay);
    }

    function scheduleFrameNow() {
        window.clearTimeout(frameTimer);
        frameTimer = 0;
        schedule(0);
    }

    async function poll() {
        if (!visible() || frameInFlight) return;
        frameInFlight = true;
        const started = performance.now();
        const generation = frameGeneration;
        let nextDelay = FRAME_INTERVAL_MS;
        try {
            const query = new URLSearchParams({after_seq: String(frameSeq), last_event_id: String(eventId)});
            const preview = await request("/api/iphone/screen/frame?" + query.toString(), {timeout: FRAME_TIMEOUT_MS});
            if (!visible() || generation !== frameGeneration) return;
            consume(preview);
            failures = 0;
            nextDelay = preview.paused === true || preview.frame_available === false
                ? SLOW_FRAME_INTERVAL_MS : Math.max(0, FRAME_INTERVAL_MS - (performance.now() - started));
        } catch (error) {
            failures += 1;
            nextDelay = Math.min(MAX_FRAME_BACKOFF_MS, 500 * failures);
            retainFrame();
            if (failures > 1 && root.dataset.iphoneLive !== "paused" && hasImage()) setLive("offline");
            // Let the status endpoint decide whether the page lost access.
            if (error instanceof RequestError && TERMINAL_CODES.has(error.code)) refreshStatus();
        } finally {
            frameInFlight = false;
            schedule(nextDelay);
        }
    }

    function startPolling() {
        if (polling || !usable) return;
        polling = true;
        if (!hasImage()) setLive(paused ? "paused" : "connecting");
        schedule(0);
    }

    function stopPolling() {
        polling = false;
        window.clearTimeout(frameTimer);
        frameTimer = 0;
        frameGeneration += 1;
    }

    // Phone actions.

    function postAction(op, args, timeout = ACTION_TIMEOUT_MS) {
        return request("/api/iphone/action", {method: "POST", body: {op, args}, timeout});
    }

    function unconfirmed(result) {
        return isObject(result) && result.action_complete === false ? " The result is not confirmed; check the iPhone." : "";
    }

    async function phoneAction(label, run) {
        if (acting || !usable) return;
        acting = label;
        syncControls();
        try {
            await run();
        } catch (error) {
            logActivity(label + " failed: " + errorWithHints(error), "error");
            if (error instanceof RequestError && TERMINAL_CODES.has(error.code)) refreshStatus();
            scheduleFrameNow();
        } finally {
            acting = "";
            syncControls();
        }
    }

    function canTouch() {
        return usable && polling && !acting && !paused && !viewportRefreshInFlight
            && frameMatchesViewport() && !image.hidden && hasImage() && image.complete
            && image.naturalWidth === dimensions.width && image.naturalHeight === dimensions.height;
    }

    function frameMatchesViewport() {
        return validSize(dimensions) && validSize(viewport)
            && Math.abs((dimensions.width / dimensions.height) / (viewport.width / viewport.height) - 1)
                <= FRAME_VIEWPORT_RATIO_TOLERANCE;
    }

    async function refreshViewport() {
        if (!visible() || paused || acting || setupAction || viewportRefreshInFlight
            || root.dataset.iphoneBusy === "true" || !validSize(dimensions) || !validSize(viewport)
            || frameMatchesViewport() || Date.now() < viewportRefreshAfter) return;
        const generation = frameGeneration;
        const currentStream = streamId;
        const size = dimensions;
        viewportRefreshInFlight = true;
        syncControls();
        try {
            // A new JPEG orientation can arrive before the runtime has read the phone's point viewport.
            const result = await postAction("observe", {mode: "tree", max_nodes: 1});
            if (!visible() || paused || generation !== frameGeneration || currentStream !== streamId
                || !dimensions || size.width !== dimensions.width || size.height !== dimensions.height) return;
            if (!validSize(result.viewport)) {
                throw new RequestError("invalid_response", "The iPhone did not return its screen dimensions.");
            }
            // Ignore a cached frame poll that began before this fresh viewport was read.
            frameGeneration += 1;
            pointerStart = null;
            viewport = {width: result.viewport.width, height: result.viewport.height};
            viewportRefreshError = "";
        } catch (error) {
            if (error instanceof RequestError && error.code !== "device_busy" && error.code !== viewportRefreshError) {
                viewportRefreshError = error.code;
                logActivity("Screen alignment could not be refreshed: " + errorMessage(error), "error");
            }
        } finally {
            viewportRefreshInFlight = false;
            viewportRefreshAfter = Date.now() + VIEWPORT_RETRY_MS;
            syncControls();
            scheduleFrameNow();
        }
    }

    function contentRect() {
        const box = image.getBoundingClientRect();
        // The frame header's size shapes the screen box; the decoded size is only a fallback.
        const naturalWidth = dimensions ? dimensions.width : image.naturalWidth;
        const naturalHeight = dimensions ? dimensions.height : image.naturalHeight;
        if (!naturalWidth || !naturalHeight || !box.width || !box.height) return box;
        // object-fit: contain may letterbox; map only the painted picture.
        const scale = Math.min(box.width / naturalWidth, box.height / naturalHeight);
        const width = naturalWidth * scale;
        const height = naturalHeight * scale;
        return {left: box.left + (box.width - width) / 2, top: box.top + (box.height - height) / 2, width, height};
    }

    function screenPoint(clientX, clientY) {
        if (!validSize(viewport)) return null;
        const box = contentRect();
        if (!box.width || !box.height) return null;
        const fractionX = (clientX - box.left) / box.width;
        const fractionY = (clientY - box.top) / box.height;
        if (fractionX < 0 || fractionX > 1 || fractionY < 0 || fractionY > 1) return null;
        const round = (value) => Math.round(value * 10) / 10;
        // The runtime requires points strictly inside the viewport.
        return {
            x: clamp(round(fractionX * viewport.width), 0, Math.max(0, viewport.width - 0.1)),
            y: clamp(round(fractionY * viewport.height), 0, Math.max(0, viewport.height - 0.1)),
        };
    }

    function sendTap(point) {
        phoneAction("Tap", async () => {
            const result = await postAction("tap", {x: point.x, y: point.y});
            logActivity("Tapped at " + point.x + ", " + point.y + "." + unconfirmed(result), "ok");
            scheduleFrameNow();
        });
    }

    function sendSwipe(direction) {
        phoneAction("Swipe " + direction, async () => {
            const result = await postAction("swipe", {direction});
            logActivity("Swiped " + direction + "." + unconfirmed(result), "ok");
            scheduleFrameNow();
        });
    }

    function typeText() {
        const text = typeInput.value;
        if (!text) return;
        const submit = typeSubmit.checked;
        phoneAction("Typing", async () => {
            let args = {text};
            if (/[\r\n]/.test(text)) args.allow_newlines = true;
            if (submit) args.submit = true;
            for (let round = 0; ; round += 1) {
                const result = await postAction("type_text", args, LONG_ACTION_TIMEOUT_MS);
                if (result.input_complete === false && typeof result.continue_token === "string" && result.continue_token) {
                    if (round >= MAX_TYPE_CONTINUATIONS) {
                        throw new RequestError("input_incomplete", "Typing stopped before the end. Check the field on the iPhone.");
                    }
                    const typed = Number.isFinite(result.characters) ? result.characters + " characters typed" : "Part of the text typed";
                    const remaining = Number.isFinite(result.remaining_characters) ? ", " + result.remaining_characters + " remaining" : "";
                    logActivity(typed + remaining + ". Continuing.", "info");
                    args = {continue_token: result.continue_token};
                    continue;
                }
                const count = Number.isFinite(result.characters) ? result.characters : Array.from(text).length;
                logActivity("Typed " + count + (count === 1 ? " character" : " characters")
                    + (result.submitted ? " and pressed Return." : ".") + unconfirmed(result), "ok");
                if (typeInput.value === text) typeInput.value = "";
                scheduleFrameNow();
                return;
            }
        });
    }

    function openApp(bundleId, name) {
        phoneAction("Opening " + name, async () => {
            const result = await postAction("launch_app", {bundle_id: bundleId}, LONG_ACTION_TIMEOUT_MS);
            logActivity("Opened " + name + "." + unconfirmed(result), "ok");
            appCandidates.replaceChildren();
            appCandidates.hidden = true;
            scheduleFrameNow();
        });
    }

    function renderCandidates(candidates, query) {
        appCandidates.replaceChildren();
        const valid = (Array.isArray(candidates) ? candidates : [])
            .filter((candidate) => isObject(candidate) && typeof candidate.bundle_id === "string"
                && LAUNCH_BUNDLE_PATTERN.test(candidate.bundle_id))
            .slice(0, 10);
        valid.forEach((candidate) => {
            const name = [candidate.name, candidate.store_name].find((value) => typeof value === "string" && value)
                || candidate.bundle_id;
            const item = document.createElement("div");
            item.className = "iphone-app-candidate";
            item.setAttribute("role", "listitem");
            const text = document.createElement("span");
            text.className = "iphone-app-candidate-text";
            const title = document.createElement("span");
            title.className = "iphone-app-candidate-name";
            title.textContent = name;
            const meta = document.createElement("span");
            meta.className = "iphone-app-candidate-meta";
            meta.textContent = candidate.installed_verified === true ? candidate.bundle_id + " · installed" : candidate.bundle_id;
            text.append(title, meta);
            const button = document.createElement("button");
            button.type = "button";
            button.className = "secondary-button";
            button.textContent = "Open";
            button.setAttribute("aria-label", "Open " + name);
            button.addEventListener("click", () => openApp(candidate.bundle_id, name));
            item.append(text, button);
            appCandidates.append(item);
        });
        appCandidates.hidden = !valid.length;
        if (valid.length) {
            logActivity("Found " + valid.length + (valid.length === 1 ? " app" : " apps") + " for “" + query + "”. Choose one to open.", "info");
        } else {
            logActivity("No app matched “" + query + "”. Try its bundle ID.", "error");
        }
        syncControls();
    }

    async function launchApp() {
        const query = launchInput.value.trim();
        if (!query || searchingApps) return;
        if (REVERSE_DNS_PATTERN.test(query)) {
            openApp(query, query);
            return;
        }
        searchingApps = true;
        syncControls();
        try {
            const result = await request("/api/iphone/apps?" + new URLSearchParams({query}).toString(), {timeout: SETUP_TIMEOUT_MS});
            if (result.ok === false) throw resultFailure(result, "The app lookup is unavailable.");
            renderCandidates(result.candidates, query);
        } catch (error) {
            logActivity("App search failed: " + errorWithHints(error), "error");
        } finally {
            searchingApps = false;
            syncControls();
        }
    }

    function pressButton(name) {
        const label = name === "volumeup" ? "Volume up" : "Volume down";
        phoneAction(label, async () => {
            const result = await postAction("press_button", {name});
            logActivity("Pressed " + label.toLowerCase() + "." + unconfirmed(result), "ok");
        });
    }

    function saveBlob(blob) {
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = "iphone-screenshot.png";
        link.hidden = true;
        document.body.append(link);
        link.click();
        link.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    }

    function takeScreenshot() {
        phoneAction("Screenshot", async () => {
            const pending = request("/api/iphone/screenshot", {timeout: ACTION_TIMEOUT_MS, as: "blob"});
            let copied = false;
            if (navigator.clipboard && typeof navigator.clipboard.write === "function"
                && typeof window.ClipboardItem === "function") {
                try {
                    // Passing the pending blob keeps the click's activation for the clipboard write.
                    await navigator.clipboard.write([new window.ClipboardItem({"image/png": pending})]);
                    copied = true;
                } catch (_error) {
                    copied = false;
                }
            }
            const blob = await pending;
            if (copied) {
                logActivity("Screenshot copied to the clipboard.", "ok");
                return;
            }
            saveBlob(blob);
            logActivity("Screenshot saved as iphone-screenshot.png.", "ok");
        });
    }

    function runScreenAction(name) {
        phoneAction(name === "refresh" ? "Refresh" : "Home", async () => {
            const previousEmpty = emptyState.dataset.state;
            if (name === "refresh") {
                frameGeneration += 1;  // Ignore a poll that started before this refresh.
                if (!hasImage()) showEmpty("connecting");
            }
            let result;
            try {
                result = await request("/api/iphone/screen/action", {method: "POST", body: {action: name}, timeout: ACTION_TIMEOUT_MS});
            } catch (error) {
                if (name === "refresh" && !hasImage()) showEmpty(previousEmpty);
                throw error;
            }
            if (name === "refresh") {
                pausedByPage = false;
                consume(result);
                if (result.service_ready === false) {
                    logActivity("Preview refreshed, but WebDriverAgent is not reachable. Check the setup steps.", "error");
                } else {
                    logActivity("Preview refreshed.", "ok");
                }
            } else {
                logActivity("Went to the Home Screen.", "ok");
            }
            scheduleFrameNow();
        });
    }

    function togglePause() {
        const resume = paused;
        phoneAction(resume ? "Resume preview" : "Pause preview", async () => {
            const result = await request("/api/iphone/screen", {
                method: "POST", body: {action: resume ? "resume" : "pause"}, timeout: ACTION_TIMEOUT_MS,
            });
            pausedByPage = !resume;
            consume(result);
            logActivity(resume ? "Preview resumed." : "Preview paused. Nothing is captured until you resume.", "ok");
            scheduleFrameNow();
        });
    }

    // Events.

    setupButtons.forEach((button) => {
        const action = button.dataset.iphoneSetupAction;
        if (action === "configure") return;
        button.addEventListener("click", () => runSetupAction(action));
    });
    signingForm.addEventListener("submit", (event) => {
        event.preventDefault();
        runSetupAction("configure");
    });
    tools.refresh.addEventListener("click", () => runScreenAction("refresh"));
    tools.home.addEventListener("click", () => runScreenAction("home"));
    tools.screenshot.addEventListener("click", takeScreenshot);
    tools.pause.addEventListener("click", togglePause);
    tunnelToggle.addEventListener("change", changeTunnelAccess);
    typeForm.addEventListener("submit", (event) => {
        event.preventDefault();
        if (!typeButton.disabled) typeText();
    });
    typeInput.addEventListener("input", syncControls);
    typeInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
            event.preventDefault();
            if (!typeButton.disabled) typeForm.requestSubmit();
        }
    });
    swipeButtons.forEach((button) => {
        button.addEventListener("click", () => sendSwipe(button.dataset.iphoneSwipe));
    });
    launchForm.addEventListener("submit", (event) => {
        event.preventDefault();
        if (!launchButton.disabled) launchApp();
    });
    launchInput.addEventListener("input", syncControls);
    hardwareButtons.forEach((button) => {
        button.addEventListener("click", () => pressButton(button.dataset.iphoneButton));
    });

    screen.addEventListener("pointerdown", (event) => {
        if (event.button !== 0 || !canTouch()) return;
        const point = screenPoint(event.clientX, event.clientY);
        if (!point) return;
        event.preventDefault();
        pointerStart = {id: event.pointerId, x: event.clientX, y: event.clientY, point};
        try {
            screen.setPointerCapture(event.pointerId);
        } catch (_error) {
            // Capture is a convenience for drags that leave the screen.
        }
    });
    screen.addEventListener("pointerup", (event) => {
        if (!pointerStart || event.pointerId !== pointerStart.id) return;
        const start = pointerStart;
        pointerStart = null;
        if (!canTouch()) return;
        const dx = event.clientX - start.x;
        const dy = event.clientY - start.y;
        const box = contentRect();
        const threshold = Math.max(12, Math.min(box.width, box.height) * 0.06);
        if (Math.hypot(dx, dy) < threshold) {
            sendTap(start.point);
        } else if (Math.abs(dx) > Math.abs(dy)) {
            sendSwipe(dx > 0 ? "right" : "left");
        } else {
            sendSwipe(dy > 0 ? "down" : "up");
        }
    });
    screen.addEventListener("pointercancel", () => {
        pointerStart = null;
    });

    image.addEventListener("load", () => {
        if (dimensions && hasImage()) lastGoodFrame = {source: image.getAttribute("src"), size: dimensions};
        syncControls();
    });
    image.addEventListener("error", () => {
        if (root.dataset.iphoneLive === "paused" || !hasImage()) return;
        if (lastGoodFrame && image.getAttribute("src") !== lastGoodFrame.source) {
            image.src = lastGoodFrame.source;
            dimensions = lastGoodFrame.size;
            fitFrame();
        } else if (!lastGoodFrame) {
            clearFrame();
            setLive("offline");
            showEmpty("unavailable");
        }
        frameSeq = 0;  // Request a replacement without blanking the last decoded frame.
        retainFrame();
        syncControls();
    });

    function visibilityChanged() {
        root.dataset.pageVisible = String(!suspended && !document.hidden);
        window.clearTimeout(frameTimer);
        frameTimer = 0;
        if (visible()) schedule(0);
        else retainFrame();
        if (suspended || document.hidden) {
            window.clearTimeout(statusTimer);
            statusTimer = 0;
        } else if (!terminal) {
            scheduleStatus(0);
        }
    }

    document.addEventListener("visibilitychange", () => {
        if (!document.hidden) suspended = false;
        visibilityChanged();
    });
    window.addEventListener("pagehide", () => {
        suspended = true;
        visibilityChanged();
    });
    window.addEventListener("pageshow", (event) => {
        // The first load also fires pageshow; only a restored or suspended page resumes here.
        if (!suspended && !event.persisted) return;
        suspended = false;
        visibilityChanged();
    });

    // Start.

    root.dataset.iphoneLive = "offline";
    root.dataset.frameSeq = "0";
    root.dataset.iphoneReady = "false";
    root.dataset.pageVisible = String(!document.hidden);
    fitFrame();
    setLive("offline");
    const seededUsable = root.dataset.iphoneSupported === "true"
        && root.dataset.iphoneOperationsEnabled === "true"
        && root.dataset.iphoneLoopback === "true";
    showEmpty(seededUsable ? "setup" : "disabled");
    if (seededUsable) setStatusLine("Checking", "", {busy: true});
    syncControls();
    // The server-rendered flags only seed the notice; the status endpoint is authoritative.
    refreshStatus();
})();
