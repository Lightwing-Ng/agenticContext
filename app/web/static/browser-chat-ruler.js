/* Code version: v1.1.0-codex.0 */

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
        let lensFrame = 0;
        let pointerY = null;
        const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
        const finePointer = window.matchMedia("(hover: hover) and (pointer: fine)");

        function updateMagnification() {
            if (lensFrame) window.cancelAnimationFrame(lensFrame);
            lensFrame = 0;
            const focusBounds = focusedEntry?.marker.matches(":focus-visible")
                ? focusedEntry.marker.getBoundingClientRect() : null;
            const center = pointerY ?? (focusBounds ? focusBounds.top + focusBounds.height / 2 : null);
            const enabled = center !== null && !reducedMotion.matches && finePointer.matches;
            // Transform the strokes only, keeping message layout and pointer targets stable.
            entries.forEach(({ marker }) => {
                const bounds = marker.getBoundingClientRect();
                const radius = Math.max(1, bounds.height) * 3;
                const distance = enabled ? Math.abs(bounds.top + bounds.height / 2 - center) / radius : 1;
                const influence = distance < 1 ? (1 + Math.cos(Math.PI * distance)) / 2 : 0;
                marker.style.setProperty("--chat-marker-scale", String(1 + influence * 0.6));
                marker.style.setProperty("--chat-marker-shift", `${-3 * influence}px`);
            });
        }

        function scheduleMagnification() {
            if (!lensFrame) lensFrame = window.requestAnimationFrame(updateMagnification);
        }

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
            previewLabel.textContent = `${author}${number ? ` · ${number}` : ""}`;
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
            updateMagnification();
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
            scrollport.scrollTo({ top: Math.max(0, top), behavior: reducedMotion.matches ? "instant" : "smooth" });
            scheduleUpdate();
        }

        entries.forEach((entry, index) => {
            entry.marker.addEventListener("pointerenter", (event) => {
                hoveredEntry = entry;
                dismissedEntry = null;
                if (event.pointerType !== "touch") {
                    const bounds = entry.marker.getBoundingClientRect();
                    pointerY = Number.isFinite(event.clientY) ? event.clientY : bounds.top + bounds.height / 2;
                    scheduleMagnification();
                }
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

        ruler.addEventListener("pointermove", (event) => {
            if (event.pointerType === "touch") return;
            pointerY = event.clientY;
            scheduleMagnification();
        });
        ruler.addEventListener("pointerleave", () => {
            pointerY = null;
            scheduleMagnification();
        });
        reducedMotion.addEventListener("change", scheduleMagnification);
        finePointer.addEventListener("change", scheduleMagnification);

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
