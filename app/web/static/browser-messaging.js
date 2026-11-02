/* Code version: v1.0.0-codex.0 */

(function initializeMessagingTimestamps() {
    "use strict";

    const rows = Array.from(document.querySelectorAll(".wechat-message-row"), (row) => {
        const card = row.querySelector(":scope > .wechat-message");
        const summary = card?.querySelector(":scope > .wechat-provenance > summary");
        const time = summary?.querySelector(":scope > .wechat-message-header > time");
        if (!time) return null;
        // Preserve the original baseline and intrinsic card width after moving the clock.
        const slot = document.createElement("span");
        slot.className = "wechat-time-slot";
        slot.setAttribute("aria-hidden", "true");
        slot.textContent = time.textContent;
        time.before(slot);
        time.classList.add("wechat-message-time");
        row.append(time);
        return { row, card, summary, time, slot };
    }).filter(Boolean);

    function alignTimestamps() {
        const positions = rows.map(({ row, card, slot }) => {
            // Closed nested disclosures do not have a usable layout.
            if (row.closest("details:not([open])") || !row.getClientRects().length) return null;
            const bounds = row.getBoundingClientRect();
            const height = parseFloat(getComputedStyle(row).height);
            const scale = height > 0 ? bounds.height / height : 1;
            const style = getComputedStyle(card);
            return {
                top: (slot.getBoundingClientRect().top - bounds.top) / scale,
                inset: parseFloat(style.paddingRight) + parseFloat(style.borderRightWidth),
            };
        });
        rows.forEach(({ row }, index) => {
            const position = positions[index];
            if (!position) return;
            const values = {
                "--wechat-time-top": `${position.top}px`,
                "--wechat-time-inset": `${position.inset}px`,
            };
            Object.entries(values).forEach(([property, value]) => {
                if (row.style.getPropertyValue(property) !== value) {
                    row.style.setProperty(property, value);
                }
            });
        });
    }

    if (typeof ResizeObserver === "function") {
        const observer = new ResizeObserver(alignTimestamps);
        rows.forEach(({ row, card, summary }) => {
            [row, card, summary].forEach((element) => observer.observe(element));
        });
    }
    document.querySelectorAll("[data-wechat-forwarded]").forEach((details) => {
        details.addEventListener("toggle", () => {
            alignTimestamps();
            window.requestAnimationFrame(alignTimestamps);
        });
    });
    window.addEventListener("resize", alignTimestamps);
    document.fonts?.ready?.then(alignTimestamps).catch(() => {});
    document.fonts?.addEventListener("loadingdone", alignTimestamps);
    alignTimestamps();
}());
