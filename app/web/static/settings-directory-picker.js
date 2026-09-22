/* Code version: v2.0.0-codex.0 */

(function initializeSettingsDirectoryPickers() {
    "use strict";

    /** Manual path validation remains bounded; browsing itself has no session timeout. */
    var VALIDATION_TIMEOUT_MS = 30000;
    var browser = document.querySelector("[data-settings-directory-browser]");
    var pickerButtons = Array.from(
        document.querySelectorAll("[data-settings-directory-picker]"),
    );
    if (!browser || !pickerButtons.length) return;

    var dialog = browser.querySelector(".settings-directory-browser-dialog");
    var title = browser.querySelector("#settings_directory_browser_title");
    var copy = browser.querySelector("#settings_directory_browser_copy");
    var closeButton = browser.querySelector("[data-directory-browser-close]");
    var cancelButton = browser.querySelector("[data-directory-browser-cancel]");
    var selectButton = browser.querySelector("[data-directory-browser-select]");
    var upButton = browser.querySelector("[data-directory-browser-up]");
    var goButton = browser.querySelector("[data-directory-browser-go]");
    var pathInput = browser.querySelector("[data-directory-browser-path]");
    var breadcrumbs = browser.querySelector("[data-directory-browser-breadcrumbs]");
    var listShell = browser.querySelector("[data-directory-browser-list-shell]");
    var directoryList = browser.querySelector("[data-directory-browser-list]");
    var emptyState = browser.querySelector("[data-directory-browser-empty]");
    var spinner = browser.querySelector("[data-directory-browser-spinner]");
    var dialogStatus = browser.querySelector("[data-directory-browser-status]");
    var activeSession = null;

    function parseJsonPayload(rawText) {
        try {
            return JSON.parse(rawText);
        } catch (_jsonError) {
            throw new Error("The server returned a malformed response.");
        }
    }

    async function requestJson(url, payload, signal) {
        var response = await fetch(url, {
            method: "POST",
            cache: "no-store",
            credentials: "same-origin",
            signal: signal,
            headers: {
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            body: JSON.stringify(payload),
        });
        var result = parseJsonPayload(await response.text());
        if (!response.ok) {
            throw new Error(result.error || "The directory request failed.");
        }
        return result;
    }

    function renderFieldStatus(context, message, isError) {
        if (!context.status) return;
        context.status.textContent = message || "";
        context.status.hidden = !message;
        context.status.classList.toggle("field-status--error", Boolean(isError));
    }

    function renderDialogStatus(message, isError) {
        if (!dialogStatus) return;
        dialogStatus.textContent = message || "";
        dialogStatus.hidden = !message;
        dialogStatus.classList.toggle("field-status--error", Boolean(isError));
    }

    function setDialogBusy(isBusy) {
        listShell?.setAttribute("aria-busy", String(isBusy));
        if (spinner) spinner.hidden = !isBusy;
        if (goButton) goButton.disabled = isBusy;
        if (upButton) upButton.disabled = isBusy || !activeSession?.parentPath;
        if (selectButton) selectButton.disabled = isBusy || !activeSession?.currentPath;
        browser.querySelectorAll("[data-directory-browser-breadcrumb]").forEach(function (button) {
            button.disabled = isBusy;
        });
        browser.querySelectorAll("[data-directory-browser-entry]").forEach(function (button) {
            button.disabled = isBusy || button.dataset.directoryAccessible !== "true";
        });
    }

    function resetDialogContents() {
        if (title) title.textContent = "Choose a folder";
        if (copy) copy.textContent = "Browse local folders, then choose the current folder.";
        if (pathInput) pathInput.value = "";
        breadcrumbs?.replaceChildren();
        directoryList?.replaceChildren();
        if (emptyState) emptyState.hidden = true;
        renderDialogStatus("");
        listShell?.setAttribute("aria-busy", "false");
        if (spinner) spinner.hidden = true;
    }

    function closeSession(session, options) {
        if (!session || activeSession !== session) return;
        session.controller?.abort();
        activeSession = null;
        browser.hidden = true;
        document.body.classList.remove("has-settings-directory-browser");
        session.context.button.removeAttribute("aria-expanded");
        session.context.button.removeAttribute("aria-busy");
        resetDialogContents();
        if (options?.restoreFocus !== false) {
            if (session.context.button.disabled) session.context.input.focus();
            else session.context.button.focus();
        }
    }

    function renderBreadcrumbs(session, items) {
        if (!breadcrumbs) return;
        breadcrumbs.replaceChildren();
        items.forEach(function (item, index) {
            var button = document.createElement("button");
            button.type = "button";
            button.className = "settings-directory-breadcrumb";
            button.dataset.directoryBrowserBreadcrumb = item.path;
            button.textContent = item.label;
            button.setAttribute("aria-label", "Open " + item.path);
            button.setAttribute("aria-current", index === items.length - 1 ? "location" : "false");
            button.addEventListener("click", function () {
                void navigateToDirectory(session, item.path, false);
            });
            breadcrumbs.append(button);
        });
    }

    function renderDirectories(session, items) {
        if (!directoryList) return;
        directoryList.replaceChildren();
        items.forEach(function (item) {
            var button = document.createElement("button");
            button.type = "button";
            button.className = "settings-directory-browser-entry";
            button.dataset.directoryBrowserEntry = item.path;
            button.dataset.directoryAccessible = String(Boolean(item.accessible));
            button.disabled = !item.accessible;
            button.setAttribute(
                "aria-label",
                item.accessible
                    ? "Open folder " + item.name
                    : item.name + ": " + (item.reason || "Unavailable"),
            );
            if (item.reason) button.title = item.reason;

            var icon = document.createElement("span");
            icon.className = "settings-directory-browser-entry-icon";
            icon.setAttribute("aria-hidden", "true");
            var name = document.createElement("span");
            name.className = "settings-directory-browser-entry-name";
            name.textContent = item.name;
            button.append(icon, name);
            if (item.is_symlink || item.reason) {
                var detail = document.createElement("span");
                detail.className = "settings-directory-browser-entry-detail";
                detail.textContent = item.reason || "Symbolic link";
                button.append(detail);
            }
            if (item.accessible) {
                button.addEventListener("click", function () {
                    void navigateToDirectory(session, item.path, false);
                });
            }
            directoryList.append(button);
        });
        if (emptyState) emptyState.hidden = items.length !== 0;
    }

    async function navigateToDirectory(session, requestedPath, recoverInvalid) {
        if (!session || activeSession !== session) return;
        session.controller?.abort();
        var controller = new AbortController();
        var requestId = ++session.requestId;
        session.controller = controller;
        if (pathInput) pathInput.value = requestedPath || "";
        renderDialogStatus("Loading folders…", false);
        setDialogBusy(true);
        try {
            var payload = await requestJson(
                "/api/settings/directory",
                {
                    field: session.context.fieldName,
                    path: requestedPath,
                    recover_invalid: Boolean(recoverInvalid),
                },
                controller.signal,
            );
            if (activeSession !== session || session.requestId !== requestId) return;
            session.currentPath = payload.current_path || "";
            session.parentPath = payload.parent_path || "";
            if (pathInput) pathInput.value = session.currentPath;
            renderBreadcrumbs(session, Array.isArray(payload.breadcrumbs) ? payload.breadcrumbs : []);
            renderDirectories(session, Array.isArray(payload.directories) ? payload.directories : []);
            renderDialogStatus(payload.notice || "", false);
            pathInput?.focus();
        } catch (error) {
            if (activeSession !== session || session.requestId !== requestId) return;
            if (error?.name !== "AbortError") {
                renderDialogStatus(error?.message || "Could not browse this directory.", true);
                pathInput?.focus();
            }
        } finally {
            if (activeSession === session && session.requestId === requestId) {
                session.controller = null;
                setDialogBusy(false);
            }
        }
    }

    function openDirectoryBrowser(context) {
        if (activeSession) {
            pathInput?.focus();
            return;
        }
        var session = {
            context: context,
            currentPath: "",
            parentPath: "",
            requestId: 0,
            controller: null,
        };
        activeSession = session;
        context.button.setAttribute("aria-expanded", "true");
        context.button.setAttribute("aria-busy", "true");
        renderFieldStatus(context, "");
        if (title) title.textContent = context.button.getAttribute("aria-label") || "Choose a folder";
        if (copy) {
            copy.textContent = "Browse local folders. The path changes only after you select the current folder.";
        }
        browser.hidden = false;
        document.body.classList.add("has-settings-directory-browser");
        directoryList?.replaceChildren();
        if (emptyState) emptyState.hidden = true;
        void navigateToDirectory(session, context.input.value, true);
    }

    async function selectCurrentDirectory() {
        var session = activeSession;
        if (!session?.currentPath) return;
        if (
            session.context.fieldName === "agent_allowed_root"
            && session.context.button.disabled
        ) {
            renderDialogStatus(
                "Stop the running Agent task before switching projects.",
                true,
            );
            return;
        }
        session.controller?.abort();
        var controller = new AbortController();
        var requestId = ++session.requestId;
        session.controller = controller;
        renderDialogStatus("Checking the selected folder…", false);
        setDialogBusy(true);
        try {
            var result = await requestJson(
                "/api/settings/directory/validate",
                {path: session.currentPath},
                controller.signal,
            );
            if (activeSession !== session || session.requestId !== requestId) return;
            if (!result.valid || !result.path) {
                renderDialogStatus(result.reason || "This folder cannot be selected.", true);
                return;
            }
            var selectedPath = result.path;
            closeSession(session, {restoreFocus: false});
            session.context.input.value = selectedPath;
            session.context.input.dispatchEvent(new Event("change", {bubbles: true}));
            session.context.input.focus();
            renderFieldStatus(session.context, "Folder selected: " + selectedPath, false);
        } catch (error) {
            if (activeSession !== session || session.requestId !== requestId) return;
            if (error?.name !== "AbortError") {
                renderDialogStatus(error?.message || "Could not select this folder.", true);
            }
        } finally {
            if (activeSession === session && session.requestId === requestId) {
                session.controller = null;
                setDialogBusy(false);
            }
        }
    }

    function cancelActiveSession() {
        var session = activeSession;
        if (!session) return;
        renderFieldStatus(session.context, "Selection cancelled.", false);
        closeSession(session);
    }

    closeButton?.addEventListener("click", cancelActiveSession);
    cancelButton?.addEventListener("click", cancelActiveSession);
    selectButton?.addEventListener("click", function () {
        void selectCurrentDirectory();
    });
    upButton?.addEventListener("click", function () {
        if (activeSession?.parentPath) {
            void navigateToDirectory(activeSession, activeSession.parentPath, false);
        }
    });
    goButton?.addEventListener("click", function () {
        if (activeSession) {
            void navigateToDirectory(activeSession, pathInput?.value || "", false);
        }
    });
    pathInput?.addEventListener("keydown", function (event) {
        if (event.key !== "Enter" || !activeSession) return;
        event.preventDefault();
        void navigateToDirectory(activeSession, pathInput.value, false);
    });
    browser.addEventListener("click", function (event) {
        if (event.target === browser) cancelActiveSession();
    });
    browser.addEventListener("keydown", function (event) {
        if (!activeSession) return;
        if (event.key === "Escape") {
            event.preventDefault();
            event.stopPropagation();
            cancelActiveSession();
            return;
        }
        if (event.key !== "Tab" || !dialog) return;
        var focusable = Array.from(
            dialog.querySelectorAll("button:not([disabled]), input:not([disabled])"),
        ).filter(function (element) {
            return !element.hidden && element.getClientRects().length > 0;
        });
        if (!focusable.length) return;
        var first = focusable[0];
        var last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
        }
    });

    pickerButtons.forEach(function (button) {
        var inputId = button.dataset.directoryInput;
        var fieldName = button.dataset.directoryField;
        var input = inputId ? document.getElementById(inputId) : null;
        var status = button.closest(".field")?.querySelector("[data-directory-picker-status]");
        if (!input || !fieldName) return;
        var context = {
            button: button,
            input: input,
            fieldName: fieldName,
            status: status,
            validationController: null,
            validationRequestId: 0,
        };

        input.removeAttribute("readonly");
        input.removeAttribute("aria-readonly");
        if (status) {
            if (!status.id && input.id) status.id = input.id + "_status";
            if (status.id && input.getAttribute("aria-describedby") !== status.id) {
                input.setAttribute("aria-describedby", status.id);
            }
            if (!status.getAttribute("role")) status.setAttribute("role", "status");
            if (!status.getAttribute("aria-live")) status.setAttribute("aria-live", "polite");
        }

        button.setAttribute("aria-haspopup", "dialog");
        button.setAttribute("aria-controls", browser.id);
        button.addEventListener("click", function () {
            openDirectoryBrowser(context);
        });

        input.addEventListener("change", async function () {
            var pathValue = (input.value || "").trim();
            var requestId = ++context.validationRequestId;
            context.validationController?.abort();
            if (!pathValue) {
                renderFieldStatus(context, "");
                input.removeAttribute("aria-invalid");
                return;
            }
            var controller = new AbortController();
            context.validationController = controller;
            var timeoutId = setTimeout(function () {
                controller.abort();
            }, VALIDATION_TIMEOUT_MS);
            try {
                var result = await requestJson(
                    "/api/settings/directory/validate",
                    {path: pathValue},
                    controller.signal,
                );
                if (
                    requestId !== context.validationRequestId
                    || pathValue !== (input.value || "").trim()
                ) return;
                if (result.valid) {
                    input.removeAttribute("aria-invalid");
                    renderFieldStatus(context, "", false);
                } else {
                    input.setAttribute("aria-invalid", "true");
                    renderFieldStatus(context, result.reason || "Invalid path.", true);
                }
            } catch (error) {
                if (requestId !== context.validationRequestId) return;
                input.setAttribute("aria-invalid", "true");
                if (error?.name === "AbortError") {
                    renderFieldStatus(
                        context,
                        "Path validation timed out. Check the folder path and try again.",
                        true,
                    );
                } else {
                    renderFieldStatus(
                        context,
                        error?.message || "Could not validate the folder path.",
                        true,
                    );
                }
            } finally {
                clearTimeout(timeoutId);
                if (requestId === context.validationRequestId) {
                    context.validationController = null;
                }
            }
        });
    });
})();
