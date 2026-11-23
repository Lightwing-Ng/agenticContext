/* Code version: v1.3.1-codex.0 */

(() => {
    const section = document.querySelector("[data-shadow-backup-section]");
    const enabledInput = document.getElementById("shadow_backup_enabled");
    const mirrorDeletionsInput = document.getElementById("shadow_backup_mirror_deletions");
    const syncButton = document.getElementById("shadow_backup_sync_now");
    const statusCopy = document.getElementById("shadow_backup_status");
    const statusSpinner = statusCopy?.querySelector("[data-shadow-backup-status-spinner]");
    const statusMessage = statusCopy?.querySelector("[data-shadow-backup-status-copy]");
    const lastSyncedCopy = document.getElementById("shadow_backup_last_synced");
    const mirrorWarning = document.querySelector("[data-shadow-backup-mirror-warning]");

    if (!section || !enabledInput || !mirrorDeletionsInput) {
        return;
    }

    let isSyncRunning = statusCopy?.dataset.phase === "syncing";
    let statusRefreshTimer = null;

    function renderStatusCopy(message, phase) {
        if (statusCopy) {
            const target = statusMessage || statusCopy;
            target.textContent = message || "Shadow cloud backup status is unavailable.";
            statusCopy.dataset.phase = phase || "idle";
        }
        if (statusSpinner) {
            statusSpinner.hidden = !isSyncRunning;
        }
    }

    function updateControlState() {
        const enabled = enabledInput.checked;
        section.dataset.shadowBackupEnabled = String(enabled);
        if (syncButton) {
            syncButton.disabled = !enabled || isSyncRunning;
        }
        if (mirrorWarning) {
            mirrorWarning.hidden = !mirrorDeletionsInput.checked;
        }
        if (statusSpinner) {
            statusSpinner.hidden = !isSyncRunning;
        }
    }

    function renderStatus(snapshot) {
        isSyncRunning = Boolean(snapshot.running);
        renderStatusCopy(snapshot.message, snapshot.phase);
        if (lastSyncedCopy) {
            if (snapshot.last_synced_at) {
                const lastSynced = window.APP_DATES.dateTime(snapshot.last_synced_at, { timeZone: "UTC" });
                lastSyncedCopy.textContent = `Last successful sync: ${lastSynced} (UTC)`;
                lastSyncedCopy.hidden = false;
            } else {
                lastSyncedCopy.hidden = true;
            }
        }
        updateControlState();
    }

    async function refreshStatus() {
        try {
            const response = await fetch("/api/settings/shadow-backup/status", { cache: "no-store" });
            if (!response.ok) {
                return;
            }
            const snapshot = await response.json();
            renderStatus(snapshot);
            if (!snapshot.running && statusRefreshTimer !== null) {
                window.clearInterval(statusRefreshTimer);
                statusRefreshTimer = null;
            }
        } catch (_error) {
            // The page remains usable when a transient local request fails.
        }
    }

    function beginStatusPolling() {
        if (statusRefreshTimer !== null) {
            return;
        }
        statusRefreshTimer = window.setInterval(refreshStatus, 2_000);
        refreshStatus();
    }

    enabledInput.addEventListener("change", updateControlState);
    mirrorDeletionsInput.addEventListener("change", updateControlState);

    updateControlState();
    if (isSyncRunning) {
        beginStatusPolling();
    }
})();
