/* Code version: v1.2.2-codex.0 */
(function initializeWechatExchange() {
    "use strict";

    const exportRoot = document.querySelector("[data-wechat-exchange-actions]");
    if (exportRoot) {
        const trigger = exportRoot.querySelector("[data-exchange-export-trigger]");
        const menu = exportRoot.querySelector("[data-exchange-export-menu]");
        const options = Array.from(menu.querySelectorAll('[role="menuitem"]'));
        const datasetOption = exportRoot.querySelector("[data-exchange-dataset-export]");
        const exportStatus = exportRoot.querySelector("[data-exchange-export-status]");
        let closeTimer;
        let suppressFocusOpen = false;
        let pointerInteraction = false;
        let preparingDataset = false;
        let exportToken = "";
        const setOpen = (open) => {
            window.clearTimeout(closeTimer);
            menu.hidden = !open;
            trigger.setAttribute("aria-expanded", String(open));
        };
        const closeAndRestoreFocus = () => {
            suppressFocusOpen = true;
            trigger.focus();
            suppressFocusOpen = false;
            setOpen(false);
        };
        exportRoot.addEventListener("pointerenter", (event) => {
            if (event.pointerType !== "touch") setOpen(true);
        });
        exportRoot.addEventListener("pointerdown", () => { pointerInteraction = true; });
        exportRoot.addEventListener("pointerleave", (event) => {
            if (event.pointerType === "touch") return;
            closeTimer = window.setTimeout(() => {
                if (!preparingDataset && !exportRoot.contains(document.activeElement)) setOpen(false);
            }, 120);
        });
        exportRoot.addEventListener("focusin", () => {
            if (!suppressFocusOpen) setOpen(true);
        });
        exportRoot.addEventListener("focusout", () => {
            queueMicrotask(() => {
                // Touch can blur the anchor before its synthesized click is dispatched.
                if (!preparingDataset && !pointerInteraction
                    && !exportRoot.contains(document.activeElement)) setOpen(false);
            });
        });
        trigger.addEventListener("click", (event) => {
            // A touch opens the choices; the explicit CSV choice remains a native download.
            if (event.pointerType === "touch"
                || window.matchMedia("(hover: none)").matches) {
                event.preventDefault();
                setOpen(true);
            }
        });
        exportRoot.addEventListener("keydown", (event) => {
            if (event.key === "Escape") {
                event.preventDefault();
                event.stopPropagation();
                closeAndRestoreFocus();
            } else if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
                event.preventDefault();
                setOpen(true);
                let index = options.indexOf(document.activeElement);
                if (event.key === "Home") index = 0;
                else if (event.key === "End") index = options.length - 1;
                else index = (index + (event.key === "ArrowDown" ? 1 : -1)
                    + options.length) % options.length;
                options[index].focus();
            }
        });
        document.addEventListener("pointerdown", (event) => {
            if (!exportRoot.contains(event.target)) {
                pointerInteraction = false;
                setOpen(false);
            }
        });
        document.addEventListener("keydown", () => { pointerInteraction = false; });

        const exportResponse = async (response) => {
            let data;
            try {
                data = await response.json();
            } catch (_error) {
                throw new Error("The dataset service returned an invalid response.");
            }
            if (!data || typeof data !== "object" || Array.isArray(data)) {
                throw new Error("The dataset service returned an invalid response.");
            }
            if (!response.ok) {
                const error = new Error(typeof data.error === "string" ? data.error : "Dataset export failed.");
                error.status = response.status;
                throw error;
            }
            return data;
        };

        const setExportStatus = (message) => {
            exportStatus.hidden = false;
            exportStatus.textContent = message;
        };

        const exportDownloadUrl = (value, token) => {
            if (typeof value !== "string") throw new Error("The dataset download is unavailable.");
            const url = new URL(value, window.location.href);
            const expectedPath = `/browser/wechat/exchange/download/${encodeURIComponent(token)}`;
            if (url.origin !== window.location.origin || url.pathname !== expectedPath
                || url.username || url.password || url.hash) {
                throw new Error("The dataset service returned an invalid download address.");
            }
            return url;
        };

        if (datasetOption && exportStatus) datasetOption.addEventListener("click", async (event) => {
            event.preventDefault();
            setOpen(true);
            if (preparingDataset) return;
            preparingDataset = true;
            datasetOption.setAttribute("aria-disabled", "true");
            setExportStatus("Preparing dataset…");
            try {
                if (!exportToken) {
                    const data = await exportResponse(await fetch("/api/browser/wechat/exchange/export", {
                        method: "POST",
                        credentials: "same-origin",
                        headers: { Accept: "application/json", "Content-Type": "application/json" },
                        body: JSON.stringify({
                            session_id: datasetOption.dataset.exchangeSessionId,
                            account: datasetOption.dataset.exchangeAccount,
                            chat_kind: datasetOption.dataset.exchangeChatKind,
                        }),
                    }));
                    if (data.status !== "preparing" || typeof data.token !== "string"
                        || !/^[A-Za-z0-9_-]{1,256}$/.test(data.token)) {
                        throw new Error("The dataset service returned an invalid export state.");
                    }
                    exportToken = data.token;
                }
                while (exportToken) {
                    const data = await exportResponse(await fetch(
                        `/api/browser/wechat/exchange/export/${encodeURIComponent(exportToken)}`,
                        { credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" } },
                    ));
                    if (data.status === "preparing") {
                        await new Promise((resolve) => window.setTimeout(resolve, 1_000));
                        continue;
                    }
                    const token = exportToken;
                    exportToken = "";
                    if (data.status === "failed") {
                        throw new Error(typeof data.error === "string" ? data.error : "Dataset export failed.");
                    }
                    if (data.status !== "ready") {
                        throw new Error("The dataset service returned an invalid export state.");
                    }
                    const url = exportDownloadUrl(data.download_url, token);
                    const download = document.createElement("a");
                    download.href = url.href;
                    download.download = "";
                    download.hidden = true;
                    document.body.append(download);
                    setExportStatus("Ready. Download started.");
                    download.click();
                    download.remove();
                }
            } catch (error) {
                if ([400, 404, 409, 410].includes(error.status)) exportToken = "";
                setExportStatus(`${error.message} ${exportToken
                    ? "Choose Dataset exchange again to resume."
                    : "Choose Dataset exchange again to retry."}`);
            } finally {
                preparingDataset = false;
                datasetOption.removeAttribute("aria-disabled");
            }
        });
    }

    const dialog = document.querySelector("[data-wechat-exchange-import]");
    const importTrigger = document.querySelector("[data-wechat-exchange-import-trigger]");
    if (!dialog || !importTrigger) return;

    const fileInput = dialog.querySelector("[data-exchange-file]");
    const chooseButton = dialog.querySelector("[data-exchange-choose]");
    const importButton = dialog.querySelector("[data-exchange-import]");
    const cancelButton = dialog.querySelector("[data-exchange-cancel]");
    const closeButtons = Array.from(dialog.querySelectorAll("[data-exchange-close]"));
    const status = dialog.querySelector("[data-exchange-status]");
    const preview = dialog.querySelector("[data-exchange-preview]");
    const filename = dialog.querySelector("[data-exchange-filename]");
    const accountList = dialog.querySelector("[data-exchange-accounts]");
    const warnings = dialog.querySelector("[data-exchange-warnings]");
    const conflictNote = dialog.querySelector("[data-exchange-conflict-note]");
    const resultLink = dialog.querySelector("[data-exchange-view-result]");
    const formatCount = (value) => new Intl.NumberFormat("en-US").format(value);
    let token = "";
    let generation = 0;
    let importing = false;

    const sameOriginUrl = (value) => {
        const url = new URL(value, window.location.href);
        if (url.origin !== window.location.origin) throw new Error("Invalid dataset endpoint.");
        return url;
    };

    const releaseToken = (value) => {
        if (!value) return;
        fetch(sameOriginUrl(dialog.dataset.cancelUrl), {
            method: "POST",
            credentials: "same-origin",
            headers: { Accept: "application/json", "Content-Type": "application/json" },
            body: JSON.stringify({ token: value }),
            keepalive: true,
        }).catch(() => {
            // Server expiry still releases a staged upload if dismissal loses connectivity.
        });
    };

    const setBusy = (busy) => {
        dialog.setAttribute("aria-busy", String(busy));
        chooseButton.disabled = busy;
        fileInput.disabled = busy;
        closeButtons.forEach((button) => { button.disabled = importing; });
        importButton.disabled = busy || !token;
    };

    const reset = () => {
        generation += 1;
        releaseToken(token);
        token = "";
        preview.hidden = true;
        resultLink.hidden = true;
        resultLink.removeAttribute("href");
        cancelButton.textContent = "Cancel";
        importButton.hidden = false;
        fileInput.value = "";
        filename.textContent = "No file selected";
        status.textContent = "Choose a .wechatx file to validate and preview.";
        setBusy(false);
    };

    const readResponse = async (response) => {
        let data;
        try {
            data = await response.json();
        } catch (_error) {
            throw new Error("The dataset service returned an invalid response.");
        }
        if (!response.ok) {
            throw new Error(typeof data.error === "string" ? data.error : "Dataset request failed.");
        }
        return data;
    };

    const validCount = (value) => Number.isSafeInteger(value) && value >= 0;
    const renderCounts = (data) => {
        dialog.querySelectorAll("[data-exchange-count]").forEach((node) => {
            const name = node.dataset.exchangeCount;
            node.textContent = formatCount(data.counts?.[name] ?? data[name]);
        });
    };

    importTrigger.addEventListener("click", () => {
        reset();
        dialog.showModal();
        chooseButton.focus();
    });
    chooseButton.addEventListener("click", () => fileInput.click());
    closeButtons.forEach((button) => button.addEventListener("click", () => {
        if (!importing) dialog.close();
    }));
    dialog.addEventListener("cancel", (event) => {
        if (importing) event.preventDefault();
    });
    dialog.addEventListener("close", () => {
        reset();
        importTrigger.focus();
    });

    fileInput.addEventListener("change", async () => {
        const file = fileInput.files?.[0];
        releaseToken(token);
        token = "";
        preview.hidden = true;
        resultLink.hidden = true;
        importButton.disabled = true;
        if (!file) return;
        filename.textContent = file.name;
        if (!file.name.toLowerCase().endsWith(".wechatx")) {
            status.textContent = "Choose a .wechatx dataset exported by this project.";
            return;
        }
        const currentGeneration = ++generation;
        setBusy(true);
        status.textContent = "Validating dataset and checking for duplicates…";
        const form = new FormData();
        form.append("file", file);
        let previewToken = "";
        try {
            const data = await readResponse(await fetch(sameOriginUrl(dialog.dataset.previewUrl), {
                method: "POST",
                body: form,
                credentials: "same-origin",
                headers: { Accept: "application/json" },
            }));
            previewToken = typeof data.token === "string" ? data.token : "";
            if (generation !== currentGeneration) {
                releaseToken(previewToken);
                return;
            }
            if (!["ready", "duplicate"].includes(data.status)
                || !Array.isArray(data.accounts)
                || !["accounts", "sources", "conversations", "messages", "resources"]
                    .every((key) => validCount(data.counts?.[key]))
                || !["added_messages", "duplicate_messages", "conflicting_messages"]
                    .every((key) => validCount(data[key]))
                || (data.status === "ready" && (typeof data.token !== "string" || !data.token))) {
                throw new Error("The dataset preview is incomplete. Nothing was imported.");
            }
            renderCounts(data);
            accountList.replaceChildren();
            data.accounts.forEach((account) => {
                const item = document.createElement("li");
                item.textContent = [account.label, account.external_id]
                    .filter((value) => typeof value === "string" && value).join(" · ") || "Unknown source account";
                accountList.append(item);
            });
            warnings.replaceChildren();
            if (Array.isArray(data.warnings)) data.warnings.forEach((warning) => {
                if (typeof warning !== "string") return;
                const item = document.createElement("li");
                item.textContent = warning;
                warnings.append(item);
            });
            warnings.hidden = !warnings.childElementCount;
            conflictNote.hidden = data.conflicting_messages === 0;
            preview.hidden = false;
            token = data.status === "ready" ? data.token : "";
            if (data.status === "duplicate") releaseToken(previewToken);
            status.textContent = data.status === "duplicate"
                ? "This dataset is already imported. No changes are needed."
                : "Dataset validated. Review the source accounts and counts, then choose Import.";
        } catch (error) {
            releaseToken(previewToken);
            if (generation === currentGeneration && error.name !== "AbortError") {
                status.textContent = error.message;
            }
        } finally {
            if (generation === currentGeneration) setBusy(false);
        }
    });

    importButton.addEventListener("click", async () => {
        if (!token || importing) return;
        importing = true;
        setBusy(true);
        status.textContent = "Importing the validated dataset…";
        try {
            const data = await readResponse(await fetch(sameOriginUrl(dialog.dataset.importUrl), {
                method: "POST",
                credentials: "same-origin",
                headers: { Accept: "application/json", "Content-Type": "application/json" },
                body: JSON.stringify({ token }),
            }));
            if (!["imported", "duplicate"].includes(data.status)
                || !["added_messages", "duplicate_messages", "conflicting_messages"]
                    .every((key) => validCount(data[key]))) {
                throw new Error("The import result could not be verified. Reopen the preview before retrying.");
            }
            token = "";
            ["added_messages", "duplicate_messages", "conflicting_messages"].forEach((name) => {
                dialog.querySelector(`[data-exchange-count="${name}"]`).textContent = formatCount(data[name]);
            });
            status.textContent = data.status === "duplicate"
                ? "This dataset was already imported. No duplicate copy was added."
                : `Imported ${formatCount(data.added_messages)} new messages. `
                    + `${formatCount(data.duplicate_messages)} duplicates and `
                    + `${formatCount(data.conflicting_messages)} conflicts checked.`;
            if (typeof data.redirect_url === "string") {
                try {
                    const url = sameOriginUrl(data.redirect_url);
                    if (url.pathname === "/browser") {
                        resultLink.href = url.href;
                        resultLink.hidden = false;
                    }
                } catch (_error) {
                    // A result never navigates to a path supplied by the dataset.
                }
            }
            cancelButton.textContent = "Close";
            importButton.hidden = true;
        } catch (error) {
            releaseToken(token);
            token = "";
            status.textContent = `${error.message} Choose the dataset again to check its current state.`;
        } finally {
            importing = false;
            setBusy(false);
        }
    });
}());
