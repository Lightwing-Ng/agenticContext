/* Code version: v1.0.0-codex.0 */

(function initializeBrowserChatRulers() {
    "use strict";

    document.querySelectorAll("[data-browser-chat-pane]").forEach((pane) => {
        const scrollport = pane.querySelector("[data-chat-scrollport]");
        const ruler = pane.querySelector(".browser-chat-ruler");
        const preview = pane.querySelector("[data-chat-preview]");
        const previewLabel = preview?.querySelector("[data-chat-preview-label]");
        const previewCopy = preview?.querySelector("[data-chat-preview-copy]");
        if (!scrollport || !ruler || !preview?.id || !previewLabel || !previewCopy) return;

        const messages = new Map(Array.from(
            scrollport.querySelectorAll("[data-chat-message-id][id]"),
            (message) => [message.id, message],
        ));
        const entries = Array.from(ruler.querySelectorAll("[data-chat-marker]"))
            .map((marker) => ({ marker, message: messages.get(marker.dataset.chatTarget) }))
            .filter((entry) => entry.message);
        if (!entries.length) return;

        let focusedEntry = null;
        let hoveredEntry = null;
        let previewEntry = null;
        let dismissedEntry = null;
        let previewHovered = false;
        let closeTimer = 0;
        let frame = 0;

        function setDescription(entry, visible) {
            if (!entry) return;
            const ids = new Set((entry.marker.getAttribute("aria-describedby") || "")
                .split(/\s+/).filter(Boolean));
            if (visible) ids.add(preview.id);
            else ids.delete(preview.id);
            if (ids.size) entry.marker.setAttribute("aria-describedby", Array.from(ids).join(" "));
            else entry.marker.removeAttribute("aria-describedby");
        }

        function positionPreview() {
            if (preview.hidden || !previewEntry) return;
            const paneBounds = pane.getBoundingClientRect();
            const markerBounds = previewEntry.marker.getBoundingClientRect();
            const desiredTop = markerBounds.top + markerBounds.height / 2
                - paneBounds.top - pane.clientTop - preview.offsetHeight / 2;
            const maximumTop = Math.max(0, pane.clientHeight - preview.offsetHeight);
            preview.style.top = `${Math.max(0, Math.min(desiredTop, maximumTop))}px`;
        }

        function hidePreview() {
            window.clearTimeout(closeTimer);
            setDescription(previewEntry, false);
            previewEntry = null;
            preview.hidden = true;
        }

        function showPreview(entry) {
            window.clearTimeout(closeTimer);
            if (!entry || entry === dismissedEntry) return;
            setDescription(previewEntry, false);
            previewEntry = entry;
            const author = entry.message.dataset.chatAuthor || "Message";
            const number = entry.message.dataset.chatNumber || "";
            const content = entry.message.querySelector(".browser-chat-message-content");
            const text = (content?.textContent || "").replace(/\s+/g, " ").trim();
            const characters = Array.from(text);
            previewLabel.textContent = `${author}${number ? ` #${number}` : ""}`;
            previewCopy.textContent = characters.length > 240
                ? `${characters.slice(0, 240).join("")}…`
                : text || "No text preview available.";
            preview.hidden = false;
            setDescription(entry, true);
            positionPreview();
        }

        function settlePreview() {
            window.clearTimeout(closeTimer);
            closeTimer = window.setTimeout(() => {
                if (previewHovered) return;
                const entry = hoveredEntry || focusedEntry;
                if (entry && entry !== dismissedEntry) showPreview(entry);
                else hidePreview();
            }, 120);
        }

        function updateRuler() {
            frame = 0;
            const padding = Number.parseFloat(window.getComputedStyle(scrollport).paddingTop) || 0;
            const anchor = scrollport.getBoundingClientRect().top + scrollport.clientTop + padding;
            let closest = entries[0];
            let distance = Infinity;
            entries.forEach((entry) => {
                const candidateDistance = Math.abs(entry.message.getBoundingClientRect().top - anchor);
                if (candidateDistance < distance) {
                    closest = entry;
                    distance = candidateDistance;
                }
            });
            entries.forEach((entry) => {
                if (entry === closest) entry.marker.setAttribute("aria-current", "true");
                else entry.marker.removeAttribute("aria-current");
                entry.marker.tabIndex = entry === (focusedEntry || closest) ? 0 : -1;
            });
            positionPreview();
        }

        function scheduleUpdate() {
            if (!frame) frame = window.requestAnimationFrame(updateRuler);
        }

        function activate(entry) {
            dismissedEntry = null;
            entry.marker.focus({ preventScroll: true });
            showPreview(entry);
            const padding = Number.parseFloat(window.getComputedStyle(scrollport).paddingTop) || 0;
            const top = scrollport.scrollTop + entry.message.getBoundingClientRect().top
                - scrollport.getBoundingClientRect().top - scrollport.clientTop - padding;
            const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
            scrollport.scrollTo({ top: Math.max(0, top), behavior: reducedMotion ? "instant" : "smooth" });
            scheduleUpdate();
        }

        entries.forEach((entry, index) => {
            entry.marker.addEventListener("pointerenter", () => {
                hoveredEntry = entry;
                dismissedEntry = null;
                showPreview(entry);
            });
            entry.marker.addEventListener("pointerleave", () => {
                if (hoveredEntry === entry) hoveredEntry = null;
                settlePreview();
            });
            entry.marker.addEventListener("focus", () => {
                focusedEntry = entry;
                dismissedEntry = null;
                showPreview(entry);
                scheduleUpdate();
            });
            entry.marker.addEventListener("blur", () => {
                if (focusedEntry === entry) focusedEntry = null;
                settlePreview();
                scheduleUpdate();
            });
            entry.marker.addEventListener("click", (event) => {
                event.preventDefault();
                activate(entry);
            });
            entry.marker.addEventListener("keydown", (event) => {
                const offsets = { ArrowUp: -1, ArrowLeft: -1, ArrowDown: 1, ArrowRight: 1 };
                let targetIndex = index;
                if (Object.hasOwn(offsets, event.key)) {
                    targetIndex = Math.max(0, Math.min(entries.length - 1, index + offsets[event.key]));
                } else if (event.key === "Home") targetIndex = 0;
                else if (event.key === "End") targetIndex = entries.length - 1;
                else if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    activate(entry);
                    return;
                } else return;
                event.preventDefault();
                entries[targetIndex].marker.focus({ preventScroll: true });
            });
        });

        preview.addEventListener("pointerenter", () => {
            previewHovered = true;
            window.clearTimeout(closeTimer);
        });
        preview.addEventListener("pointerleave", () => {
            previewHovered = false;
            settlePreview();
        });
        document.addEventListener("keydown", (event) => {
            if (event.key !== "Escape" || preview.hidden) return;
            dismissedEntry = previewEntry;
            previewHovered = false;
            hidePreview();
            event.preventDefault();
        });
        scrollport.addEventListener("scroll", scheduleUpdate, { passive: true });
        window.addEventListener("resize", scheduleUpdate, { passive: true });
        if (typeof ResizeObserver === "function") {
            const observer = new ResizeObserver(scheduleUpdate);
            observer.observe(pane);
            observer.observe(scrollport);
            entries.forEach((entry) => observer.observe(entry.message));
        }
        preview.hidden = true;
        ruler.hidden = false;
        updateRuler();
    });
}());
