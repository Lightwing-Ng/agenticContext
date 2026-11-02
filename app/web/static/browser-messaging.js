/* Code version: v1.9.0-codex.0 */

(function initializeMessagingDisclosures() {
    "use strict";

    document.querySelectorAll("[data-wechat-resource-reveal]").forEach((button) => {
        button.addEventListener("click", async () => {
            const feedback = button.querySelector("[data-wechat-resource-feedback]");
            button.disabled = true;
            try {
                const response = await fetch(button.dataset.wechatResourceReveal, {
                    method: "POST",
                    headers: { Accept: "application/json", "Content-Type": "application/json" },
                    body: "{}",
                });
                const result = await response.json();
                if (!response.ok) throw new Error(result.error || "Unable to open Finder.");
                feedback.textContent = result.target_kind === "folder"
                    ? "Folder shown in Finder" : "File shown in Finder";
            } catch (error) {
                feedback.textContent = error instanceof Error ? error.message : "Unable to open Finder.";
            } finally {
                button.disabled = false;
            }
        });
    });
    const systemRows = Array.from(document.querySelectorAll(".wechat-system-message-row"));
    const timestampRows = Array.from(document.querySelectorAll(".wechat-message-row"), (row) => {
        const card = row.querySelector(":scope > .wechat-message");
        const summary = card?.querySelector(":scope > .wechat-provenance > summary");
        const time = summary?.querySelector(":scope > .wechat-message-header > time");
        if (!time) return null;
        // Keep the original baseline and intrinsic bubble width after detaching the clock.
        const slot = document.createElement("span");
        slot.className = "wechat-time-slot";
        slot.setAttribute("aria-hidden", "true");
        slot.textContent = time.textContent;
        time.before(slot);
        time.classList.add("wechat-message-time");
        row.append(time);
        const continued = card.dataset.wechatShowSender === "false";
        if (continued) {
            card.classList.add("wechat-message--run-continued");
            row.classList.add("wechat-message-row--run-continued");
        }
        return { row, card, summary, time, slot, continued };
    }).filter(Boolean);

    function clockRailRight(row) {
        let anchor = row;
        let parent = anchor.parentElement?.closest(".wechat-message-row");
        while (parent) {
            anchor = parent;
            parent = anchor.parentElement?.closest(".wechat-message-row");
        }
        const bounds = anchor.getBoundingClientRect();
        const height = parseFloat(getComputedStyle(anchor).height);
        const scale = height > 0 ? bounds.height / height : 1;
        const card = anchor.querySelector(":scope > .wechat-message")
            || row.closest("[data-chat-scrollport]")?.querySelector(":scope > .wechat-message-row > .wechat-message");
        const style = card ? getComputedStyle(card) : null;
        const inset = style ? parseFloat(style.paddingRight) + parseFloat(style.borderRightWidth) : 12;
        return bounds.right - inset * scale;
    }

    function alignTimestamps() {
        const positions = timestampRows.map(({ row, card, summary, time, slot, continued }) => {
            // A closed forwarded disclosure has no usable layout until it becomes visible.
            if (row.closest("details:not([open])") || !row.getClientRects().length) return null;
            const bounds = row.getBoundingClientRect();
            const height = parseFloat(getComputedStyle(row).height);
            const scale = height > 0 ? bounds.height / height : 1;
            const style = getComputedStyle(card);
            const clearance = parseFloat(row.style.getPropertyValue("--wechat-run-time-clearance")) || 0;
            const cardBox = card.getBoundingClientRect();
            const clockBox = time.getBoundingClientRect();
            const clockHeight = clockBox.height / scale;
            const targetOutset = continued
                ? Math.max(0, (summary.getBoundingClientRect().height / scale - clockHeight) / 2)
                : 0;
            // Every embedded level shares the outer message rail without moving its baseline.
            const inset = (bounds.right - clockRailRight(row)) / scale;
            const top = continued
                ? (cardBox.top - bounds.top) / scale + parseFloat(style.borderTopWidth)
                    + parseFloat(style.paddingTop)
                : (slot.getBoundingClientRect().top - bounds.top) / scale;
            const clockLeft = bounds.right - (inset + clockBox.width / scale) * scale;
            const clockBottom = bounds.top + (top + clockHeight + targetOutset) * scale;
            let overlaps = false;
            if (continued) {
                card.querySelectorAll(":scope > .wechat-message-text, :scope > .wechat-message-quote > p, :scope > .wechat-resource, :scope > .wechat-provenance[open] > .ui-collapse-body, :scope > .wechat-forwarded > summary").forEach((content) => {
                    const range = document.createRange();
                    range.selectNodeContents(content);
                    const rectangles = content.matches(".wechat-resource, .ui-collapse-body, summary")
                        ? [content.getBoundingClientRect()] : Array.from(range.getClientRects());
                    overlaps ||= rectangles.some((box) => box.width > 0 && box.right > clockLeft - 4 * scale
                        && box.top - clearance * scale < clockBottom);
                });
            }
            return {
                top,
                inset,
                clockWidth: clockBox.width / scale,
                clockHeight,
                // The centered source target retains its hit area without reserving a full header.
                clearance: overlaps ? clockHeight + Math.max(4, targetOutset + 1) : 0,
            };
        });
        timestampRows.forEach(({ row, continued }, index) => {
            if (!positions[index]) return;
            const values = {
                "--wechat-time-top": `${positions[index].top}px`,
                "--wechat-time-inset": `${positions[index].inset}px`,
                ...(continued ? {
                    "--wechat-run-clock-width": `${positions[index].clockWidth}px`,
                    "--wechat-run-clock-height": `${positions[index].clockHeight}px`,
                    "--wechat-run-time-clearance": `${positions[index].clearance}px`,
                } : {}),
            };
            Object.entries(values).forEach(([property, value]) => {
                if (row.style.getPropertyValue(property) !== value) {
                    row.style.setProperty(property, value);
                }
            });
        });
        // System-only embedded transcripts also retain the same outer rail.
        systemRows.forEach((row) => {
            if (row.closest("details:not([open])") || !row.getClientRects().length) return;
            const bounds = row.getBoundingClientRect();
            const height = parseFloat(getComputedStyle(row).height);
            const scale = height > 0 ? bounds.height / height : 1;
            const inset = `${(bounds.right - clockRailRight(row)) / scale}px`;
            if (row.style.getPropertyValue("--wechat-time-inset") !== inset) {
                row.style.setProperty("--wechat-time-inset", inset);
            }
        });
    }

    if (typeof ResizeObserver === "function") {
        // Width springs and wrapping names can move the original baseline before paint.
        const observer = new ResizeObserver(alignTimestamps);
        timestampRows.forEach(({ row, card, summary }) => {
            [row, card, summary].forEach((element) => observer.observe(element));
        });
        systemRows.forEach((row) => observer.observe(row));
    }
    window.addEventListener("resize", alignTimestamps);
    document.fonts?.ready?.then(alignTimestamps).catch(() => {});
    document.fonts?.addEventListener("loadingdone", alignTimestamps);
    alignTimestamps();

    window.SharedDisclosureMotion?.enhance(document, {
        selector: ".wechat-message > details.wechat-provenance",
        widthTarget: (details) => details.parentElement,
    });
    document.querySelectorAll("[data-wechat-forwarded]").forEach((details) => {
        details.addEventListener("toggle", () => {
            // Native nested disclosure owns its layout; finish only existing source motions.
            window.SharedDisclosureMotion?.finishAll();
            alignTimestamps();
            window.requestAnimationFrame(alignTimestamps);
        });
    });
    let anchorGeneration = 0;
    function revealForwardedSource() {
        const generation = ++anchorGeneration;
        let identifier;
        try {
            identifier = decodeURIComponent(window.location.hash.slice(1));
        } catch (_) {
            return;
        }
        if (!/^wechat_message_[a-f0-9]{64}(?:_quote|_forwarded_[1-9][0-9]*)*$/.test(identifier)) return;
        let target = document.getElementById(identifier);
        if (!target && document.querySelector('[data-wechat-event-projection="true"]')) {
            // A saved source fragment can refer to a page compacted by source grouping.
            window.location.replace(`/browser/wechat/messages/${identifier.slice("wechat_message_".length)}`);
            return;
        }
        if (target?.hasAttribute("data-wechat-source-alias")) {
            target = target.closest("article.wechat-message");
        }
        if (!target?.closest("[data-wechat-resources] [data-chat-scrollport]")) return;
        let disclosure = target.parentElement?.closest("details[data-wechat-forwarded]");
        while (disclosure) {
            disclosure.open = true;
            disclosure = disclosure.parentElement?.closest("details[data-wechat-forwarded]");
        }
        const position = () => {
            if (generation !== anchorGeneration || !target.isConnected) return;
            alignTimestamps();
            target.scrollIntoView({ block: "center", behavior: "instant" });
        };
        // Reveal only the source ancestors; provenance and sibling transcripts stay closed.
        alignTimestamps();
        window.requestAnimationFrame(() => window.requestAnimationFrame(position));
        document.fonts?.ready?.then(position).catch(() => {});
    }
    window.addEventListener("hashchange", revealForwardedSource);
    revealForwardedSource();
}());
