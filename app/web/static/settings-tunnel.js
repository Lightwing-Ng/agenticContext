/* Code version: v1.0.0-codex.0 */

(() => {
    const TUNNEL_POLL_READY_MS = 10_000;
    const TUNNEL_POLL_PENDING_MS = 3_000;

    // Sibling-project clearable text input: the clear affordance appears only when
    // the field has a value and mirrors a user edit so Settings auto-save runs.
    document.querySelectorAll("[data-text-input-clear]").forEach((button) => {
        if (!(button instanceof HTMLButtonElement) || button.dataset.bound === "1") return;
        const input = button.parentElement?.querySelector("input.text-input-control");
        if (!(input instanceof HTMLInputElement)) return;
        button.dataset.bound = "1";
        const syncVisibility = () => {
            button.classList.toggle("is-visible", Boolean(input.value.trim()));
        };
        syncVisibility();
        input.addEventListener("input", syncVisibility);
        button.addEventListener("mousedown", (event) => {
            event.preventDefault();
        });
        button.addEventListener("click", () => {
            input.value = "";
            input.dispatchEvent(new Event("input", {bubbles: true}));
            input.dispatchEvent(new Event("change", {bubbles: true}));
            syncVisibility();
            input.focus();
        });
    });

    const tunnelPackage = document.querySelector("[data-tunnel-settings]");
    if (!(tunnelPackage instanceof HTMLElement)) return;
    const statusCopy = tunnelPackage.querySelector("[data-tunnel-status-copy]");
    const liveMarker = tunnelPackage.querySelector("[data-action-package-live-marker]");
    const restartButton = tunnelPackage.querySelector("[data-tunnel-restart]");
    let pollTimer = null;
    let ready = tunnelPackage.dataset.actionPackageLive === "true";

    function applyStatus(payload) {
        if (!payload || typeof payload !== "object") return;
        ready = payload.state === "ready";
        tunnelPackage.dataset.actionPackageLive = ready ? "true" : "false";
        if (liveMarker instanceof HTMLElement) liveMarker.hidden = !ready;
        const message = String(payload.presentation?.message || payload.message || "");
        if (statusCopy && message) statusCopy.textContent = message;
    }

    async function refreshStatus() {
        try {
            const response = await fetch(tunnelPackage.dataset.tunnelStatusUrl, {
                cache: "no-store",
                headers: {Accept: "application/json"},
            });
            if (response.ok) applyStatus(await response.json());
        } catch (_error) {
            // Keep the last known Tunnel state; the next poll retries.
        }
    }

    function schedulePoll() {
        if (pollTimer !== null) window.clearTimeout(pollTimer);
        pollTimer = window.setTimeout(async () => {
            pollTimer = null;
            if (document.visibilityState === "visible") await refreshStatus();
            schedulePoll();
        }, ready ? TUNNEL_POLL_READY_MS : TUNNEL_POLL_PENDING_MS);
    }

    restartButton?.addEventListener("click", async () => {
        if (!(restartButton instanceof HTMLButtonElement)) return;
        restartButton.disabled = true;
        try {
            const response = await fetch(tunnelPackage.dataset.tunnelRestartUrl, {
                method: "POST",
                headers: {Accept: "application/json"},
            });
            if (response.ok) applyStatus(await response.json());
        } catch (_error) {
            // The next status poll reports the outcome.
        } finally {
            restartButton.disabled = false;
            schedulePoll();
        }
    });

    schedulePoll();
})();
