/* Code version: v1.9.2-codex.1 */

(() => {
    const SESSION_CACHE_PREFIX = "cachelikes:browser-session:v8:";
    const SESSION_CACHE_TTL_MS = 300_000;
    const SESSION_STALE_MAX_AGE_MS = 1_800_000;
    const BOOTSTRAPPED_AGENT_PLATFORMS = new Set(["chatgpt", "grok", "claude"]);
    const statusRequests = new Map();

    function readSessionValue(key) {
        try {
            return window.sessionStorage.getItem(key);
        } catch (_error) {
            return null;
        }
    }

    function writeSessionValue(key, value) {
        try {
            window.sessionStorage.setItem(key, value);
        } catch (_error) {
        }
    }

    function readCachedStatus(cacheKey) {
        const cachedPayload = readSessionValue(cacheKey);
        if (!cachedPayload) return null;
        try {
            const cachedEntry = JSON.parse(cachedPayload);
            if (
                !cachedEntry
                || typeof cachedEntry.cached_at !== "number"
                || !cachedEntry.payload
            ) return null;
            return {
                ageMs: Math.max(Date.now() - cachedEntry.cached_at, 0),
                payload: cachedEntry.payload,
            };
        } catch (_error) {
            return null;
        }
    }

    function clientCachedPayload(payload, ageMs) {
        const source = payload && typeof payload === "object" ? payload : {};
        const freshness = source.browser_session_freshness
            && typeof source.browser_session_freshness === "object"
            ? source.browser_session_freshness
            : {};
        return {
            ...source,
            browser_session_freshness: {
                kind: "client_cache",
                cache_status: String(freshness.cache_status || "client_cache"),
                cached_at: String(freshness.cached_at || ""),
                age_seconds: Math.max(0, Math.floor(Number(ageMs || 0) / 1000)),
            },
        };
    }

    function requestBrowserStatus(platform, browserId, scope, options = {}) {
        const refresh = options.refresh === true;
        const requestScope = scope || "default";
        const requestKey = `${requestScope}:${platform}:${browserId}`;
        if (statusRequests.has(requestKey)) return statusRequests.get(requestKey);
        const query = new URLSearchParams({platform, browser: browserId});
        if (scope) query.set("scope", scope);
        if (refresh) query.set("refresh", "1");
        const request = fetch(
            `/api/browser-session?${query.toString()}`,
            {cache: "no-store"},
        )
            .then(async (response) => {
                const payload = await response.json();
                if (!response.ok) {
                    throw new Error(payload.error || "Failed to probe browser session.");
                }
                return payload;
            })
            .finally(() => statusRequests.delete(requestKey));
        statusRequests.set(requestKey, request);
        return request;
    }

    function initBrowserSessionStatus(root, options = {}) {
        if (!root) return null;

        let platform = String(options.platform || root.dataset.browserSessionPlatform || "").trim().toLowerCase();
        const statusCard = root.querySelector('[data-role="browser-session-status"]');
        const statusAccount = root.querySelector('[data-role="browser-session-account"]');
        const statusMessage = root.querySelector('[data-role="browser-session-message"]');
        const statusSpinner = root.querySelector('[data-role="browser-session-spinner"]');
        const statusCheckmark = root.querySelector('[data-role="browser-session-checkmark"]');
        const loginButton = root.querySelector('[data-role="browser-session-login"]');
        const recheckButton = root.querySelector('[data-role="browser-session-recheck"]');
        const loginMessage = root.querySelector('[data-role="browser-session-login-message"]');
        const hideReadyMessage = statusCard?.dataset.browserSessionHideReadyMessage === "true";
        const startButtonSelector = root.dataset.startButtonSelector || "";
        const requiresDownloadReady = root.dataset.requireDownloadReady === "true";
        const startButton = startButtonSelector ? document.querySelector(startButtonSelector) : null;
        const startButtonInitiallyDisabled = startButton ? startButton.disabled : false;
        const onStateChange = typeof options.onStateChange === "function" ? options.onStateChange : null;
        const scope = String(options.scope || root.dataset.browserSessionScope || "").trim().toLowerCase();
        let activeBrowser = "";
        let lastPayload = null;
        let statusRequestRevision = 0;
        let loginRequestRevision = 0;

        if (!platform || !statusCard || !statusAccount || !statusCheckmark) return null;

        function accountLabel(payload) {
            const platformLabel = String(root.dataset.browserSessionAccountLabel || "").trim();
            return platformLabel ? `${platformLabel} account` : (payload.account_name || "No signed-in account detected");
        }

        function notify(payload, browserId, state) {
            onStateChange?.(payload, browserId, state);
        }

        function setStartButtonReady(isReady) {
            if (requiresDownloadReady) {
                root.dataset.browserDownloadReady = String(isReady);
            }
            if (!requiresDownloadReady || !startButton || startButtonInitiallyDisabled) return;
            startButton.disabled = !isReady;
        }

        function hideStatusCheckmark() {
            statusCheckmark.hidden = true;
            statusCheckmark.removeAttribute("data-status-state");
        }

        function showStatusCheckmark(state) {
            statusCheckmark.setAttribute("aria-label", state === "ready" ? "Account verified" : "Account verification failed");
            statusCheckmark.dataset.statusState = state;
            statusCheckmark.hidden = false;
        }

        function setLoginMessage(message) {
            if (!loginMessage) return;
            loginMessage.textContent = message || "";
            loginMessage.hidden = !message;
        }

        function hideLoginAction() {
            if (recheckButton) recheckButton.hidden = true;
            if (!loginButton) return;
            loginButton.hidden = true;
            loginButton.disabled = true;
            loginButton.removeAttribute("aria-busy");
            setLoginMessage("");
        }

        function setLoginAction(payload, browserId) {
            if (!loginButton) return;
            const shouldShow = Boolean(payload)
                && payload.logged_in === false
                && Boolean(browserId)
                && Boolean(platform);
            if (!shouldShow) {
                hideLoginAction();
                return;
            }
            const browserLabels = {edge: "Edge", chrome: "Chrome", safari: "Safari"};
            const browserLabel = String(
                payload.browser_label || browserLabels[browserId] || browserId,
            ).trim();
            loginButton.textContent = `Open ${browserLabel} to sign in`;
            loginButton.hidden = false;
            loginButton.disabled = false;
            loginButton.removeAttribute("aria-busy");
            setLoginMessage("");
        }

        async function openLoginBrowser() {
            const requestPlatform = platform;
            const requestBrowser = activeBrowser;
            if (
                !loginButton
                || loginButton.hidden
                || loginButton.disabled
                || !requestPlatform
                || !requestBrowser
            ) return;
            const requestRevision = ++loginRequestRevision;
            loginButton.disabled = true;
            loginButton.setAttribute("aria-busy", "true");
            setLoginMessage("");
            try {
                const response = await fetch("/api/browser-session/open-login", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({platform: requestPlatform, browser: requestBrowser}),
                    credentials: "same-origin",
                });
                const payload = await response.json();
                if (!response.ok) {
                    throw new Error(payload.error || `Request failed with ${response.status}.`);
                }
                if (
                    requestRevision === loginRequestRevision
                    && requestPlatform === platform
                    && requestBrowser === activeBrowser
                ) {
                    setLoginMessage(
                        payload.message
                        || `Opened ${requestBrowser}. Sign in, then choose Recheck to verify the session.`,
                    );
                }
            } catch (error) {
                if (
                    requestRevision === loginRequestRevision
                    && requestPlatform === platform
                    && requestBrowser === activeBrowser
                ) {
                    setLoginMessage(
                        error instanceof Error ? error.message : "Could not open the sign-in browser.",
                    );
                }
            } finally {
                if (
                    requestRevision === loginRequestRevision
                    && requestPlatform === platform
                    && requestBrowser === activeBrowser
                ) {
                    loginButton.disabled = false;
                    loginButton.removeAttribute("aria-busy");
                }
            }
        }

        function setStatus(payload, browserId) {
            lastPayload = payload;
            statusCard.hidden = false;
            statusCard.removeAttribute("aria-busy");
            root.classList.remove("is-browser-status-loading", "is-browser-status-refreshing");
            const isReady = Boolean(payload.can_download);
            root.classList.toggle("is-browser-ready", isReady);
            statusAccount.textContent = accountLabel(payload);
            if (statusMessage) {
                statusMessage.textContent = payload.message || "";
                statusMessage.hidden = ((hideReadyMessage || statusCard.classList.contains("browser-session-status-card-compact")) && isReady) || !payload.message;
            }
            if (statusSpinner) statusSpinner.hidden = true;
            showStatusCheckmark(isReady ? "ready" : "error");
            setStartButtonReady(isReady);
            setLoginAction(payload, browserId);
            if (recheckButton) recheckButton.hidden = isReady;
            notify(payload, browserId, "ready");
        }

        function setLoadingState(browserId) {
            statusCard.hidden = false;
            statusCard.setAttribute("aria-busy", "true");
            root.classList.add("is-browser-status-loading");
            root.classList.remove("is-browser-ready", "is-browser-status-refreshing");
            statusAccount.textContent = "Checking signed-in account...";
            if (statusMessage) {
                statusMessage.textContent = "";
                statusMessage.hidden = true;
            }
            if (statusSpinner) statusSpinner.hidden = false;
            hideStatusCheckmark();
            hideLoginAction();
            setStartButtonReady(false);
            notify({can_download: false, account_name: "", message: "Checking signed-in account..."}, browserId, "loading");
        }

        function setRefreshingState(browserId) {
            statusCard.setAttribute("aria-busy", "true");
            root.classList.remove("is-browser-status-loading");
            root.classList.add("is-browser-status-refreshing");
            if (statusSpinner) statusSpinner.hidden = false;
            hideStatusCheckmark();
            hideLoginAction();
            notify(lastPayload || {can_download: false, account_name: "", message: "Refreshing signed-in account status..."}, browserId, "refreshing");
        }

        function clearStatus() {
            statusCard.hidden = true;
            statusCard.removeAttribute("aria-busy");
            root.classList.remove("is-browser-status-loading", "is-browser-status-refreshing", "is-browser-ready");
            if (statusMessage) {
                statusMessage.textContent = "";
                statusMessage.hidden = true;
            }
            if (statusSpinner) statusSpinner.hidden = true;
            hideStatusCheckmark();
            hideLoginAction();
            setStartButtonReady(false);
            notify(null, "", "cleared");
        }

        async function load(browserId, options = {}) {
            activeBrowser = browserId || "";
            if (!activeBrowser) {
                clearStatus();
                return;
            }

            const requestPlatform = platform;
            const requestRevision = ++statusRequestRevision;
            const cacheKey = `${SESSION_CACHE_PREFIX}${scope || "default"}:${requestPlatform}:${activeBrowser}`;
            const cachedStatus = readCachedStatus(cacheKey);
            const cachedPayload = cachedStatus?.payload;
            const requiresChatgptCapabilities = scope === "agent" && requestPlatform === "chatgpt"
                && !(cachedPayload?.model_catalog_complete
                    && cachedPayload?.model_options?.length
                    && cachedPayload?.effort_catalog_complete
                    && cachedPayload?.available_efforts?.length);
            // Account readiness alone must not pin an incomplete capability cache.
            // One bootstrap refresh obtains both pickers through the same browser.
            const forceRefresh = options.force === true || requiresChatgptCapabilities;
            const requiresAgentBootstrap = scope === "agent"
                && BOOTSTRAPPED_AGENT_PLATFORMS.has(requestPlatform)
                && Boolean(cachedPayload?.can_download)
                && !Object.prototype.hasOwnProperty.call(cachedPayload || {}, "agent_sources");
            if (!forceRefresh && cachedStatus && !requiresAgentBootstrap && cachedStatus.ageMs < SESSION_STALE_MAX_AGE_MS) {
                setStatus(
                    clientCachedPayload(cachedStatus.payload, cachedStatus.ageMs),
                    activeBrowser,
                );
                if (cachedStatus.payload.can_download && cachedStatus.ageMs < SESSION_CACHE_TTL_MS) return;
                setRefreshingState(activeBrowser);
            }
            else setLoadingState(activeBrowser);

            try {
                const payload = await requestBrowserStatus(requestPlatform, activeBrowser, scope, {refresh: forceRefresh});
                if (
                    activeBrowser !== browserId
                    || platform !== requestPlatform
                    || requestRevision !== statusRequestRevision
                ) return;
                writeSessionValue(cacheKey, JSON.stringify({cached_at: Date.now(), payload}));
                setStatus(payload, browserId);
            } catch (error) {
                if (
                    activeBrowser !== browserId
                    || platform !== requestPlatform
                    || requestRevision !== statusRequestRevision
                ) return;
                setStatus({
                    browser_label: statusAccount.textContent,
                    account_name: "",
                    can_download: false,
                    message: error instanceof Error ? error.message : "Failed to probe browser session.",
                }, browserId);
            }
        }

        recheckButton?.addEventListener("click", () => {
            void load(activeBrowser, {force: true});
        });

        loginButton?.addEventListener("click", () => {
            void openLoginBrowser();
        });

        const controller = {
            setBrowser(browserId) {
                void load(String(browserId || "").trim().toLowerCase());
            },
            setPlatform(platformId) {
                const nextPlatform = String(platformId || "").trim().toLowerCase();
                if (nextPlatform === platform) return;
                platform = nextPlatform;
                root.dataset.browserSessionPlatform = nextPlatform;
                lastPayload = null;
                clearStatus();
                void load(activeBrowser);
            },
            refresh() {
                return load(activeBrowser, {force: true});
            },
            getBrowser() {
                return activeBrowser;
            },
        };
        controller.setBrowser(
            typeof options.browserId === "string"
                ? options.browserId
                : (typeof options.getBrowser === "function" ? options.getBrowser() : ""),
        );
        return controller;
    }

    window.CACHELIKES_BROWSER_SESSION_STATUS = {init: initBrowserSessionStatus};
})();
