/* Code version: v1.2.0-codex.0 */

(function initializeBrowserChatEffects() {
    "use strict";

    // A main-thread scroll handler trails compositor scrolling and visibly detaches shadows
    // from their cards. Engines without scroll timelines keep the cards' native shadows.
    if (typeof window.ScrollTimeline !== "function" || typeof Element.prototype.animate !== "function") return;

    function splitShadows(value) {
        const layers = [];
        let depth = 0;
        let start = 0;
        for (let index = 0; index < value.length; index++) {
            if (value[index] === "(") depth++;
            else if (value[index] === ")") depth--;
            else if (value[index] === "," && depth === 0) {
                layers.push(value.slice(start, index).trim());
                start = index + 1;
            }
        }
        layers.push(value.slice(start).trim());
        return layers.filter(Boolean);
    }

    function scrollKeyframes(range) {
        return [{ transform: "translateY(0px)" }, { transform: `translateY(${-range}px)` }];
    }

    document.querySelectorAll("[data-browser-chat-pane]").forEach((pane) => {
        const scrollport = pane.querySelector("[data-chat-scrollport]");
        const layer = pane.querySelector("[data-chat-effects]");
        if (!scrollport || !layer || pane.hasAttribute("data-chat-effects-ready")) return;
        const track = document.createElement("div");
        track.className = "browser-chat-effect-track";
        const surfaces = Array.from(scrollport.querySelectorAll(".browser-chat-message, [data-chat-physical-surface]"))
            .filter((message) => message.closest("[data-browser-chat-pane]") === pane);
        const entries = surfaces.map((message) => {
            const effect = document.createElement("div");
            effect.className = "browser-chat-effect";
            effect.dataset.chatEffectFor = message.id;
            effect.hidden = true;
            track.append(effect);
            return { message, effect };
        });
        if (!entries.length) return;
        layer.append(track);
        // The scroll timeline moves the track in the same compositor frame as the cards.
        let range = 0;
        const timeline = new window.ScrollTimeline({ source: scrollport, axis: "block" });
        const scrollMotion = track.animate(scrollKeyframes(range), {
            timeline,
            fill: "both",
        });
        // A partial card must not cast a hollow bubble below the native content clip.
        // Restore bottom shadow bleed only at the end; an inactive timeline retains CSS bleed.
        layer.animate([
            { clipPath: "inset(0 calc(-1 * var(--layout-physical-effect-bleed)) 0)" },
            { clipPath: "inset(0 calc(-1 * var(--layout-physical-effect-bleed)) calc(-1 * var(--layout-physical-effect-bleed)))" },
        ], { timeline, fill: "both", easing: "steps(1, end)" });
        let frame = 0;
        let materialDirty = true;

        function updateEffects() {
            if (frame) window.cancelAnimationFrame(frame);
            frame = 0;
            if (materialDirty) {
                const style = window.getComputedStyle(pane);
                const shadows = splitShadows(style.getPropertyValue("--frosted-glass-shadow"));
                // Preserve the shared inset highlight while painting outer shadows separately.
                pane.style.setProperty("--browser-chat-outer-shadow",
                    shadows.filter((shadow) => !/\binset\b/.test(shadow)).join(", ") || "none");
                pane.style.setProperty("--browser-chat-inset-shadow",
                    shadows.filter((shadow) => /\binset\b/.test(shadow)).join(", ") || "none");
                materialDirty = false;
            }
            const layerBounds = layer.getBoundingClientRect();
            // Rounded client dimensions would turn fractional CSS sizes into scroll drift.
            const layerStyle = window.getComputedStyle(layer);
            const cssWidth = Number.parseFloat(layerStyle.width);
            const cssHeight = Number.parseFloat(layerStyle.height);
            const scaleX = cssWidth > 0 ? layerBounds.width / cssWidth : 1;
            const scaleY = cssHeight > 0 ? layerBounds.height / cssHeight : 1;
            const scrollTop = scrollport.scrollTop;
            const rectangles = entries.map(({ message }) => message.getBoundingClientRect());
            entries.forEach(({ effect }, index) => {
                const bounds = rectangles[index];
                if (entries[index].message.closest("details:not([open])")
                    || !entries[index].message.getClientRects().length || bounds.width <= 0 || bounds.height <= 0) {
                    effect.hidden = true;
                    return;
                }
                // Scroll-content coordinates stay valid while the timeline supplies the offset.
                effect.style.left = `${(bounds.left - layerBounds.left) / scaleX}px`;
                effect.style.top = `${(bounds.top - layerBounds.top) / scaleY + scrollTop}px`;
                effect.style.width = `${bounds.width / scaleX}px`;
                effect.style.height = `${bounds.height / scaleY}px`;
                effect.hidden = false;
            });
            const nextRange = Math.max(0, scrollport.scrollHeight - scrollport.clientHeight);
            if (nextRange !== range) {
                range = nextRange;
                scrollMotion.effect.setKeyframes(scrollKeyframes(range));
            }
            pane.setAttribute("data-chat-effects-ready", "");
        }

        function scheduleUpdate() {
            if (!frame) frame = window.requestAnimationFrame(updateEffects);
        }

        function scheduleMaterialUpdate() {
            materialDirty = true;
            scheduleUpdate();
        }

        window.addEventListener("resize", scheduleUpdate, { passive: true });
        // Toggle does not bubble; capture also covers nested native forwarded disclosures.
        scrollport.addEventListener("toggle", scheduleUpdate, true);
        window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", scheduleMaterialUpdate);
        if (typeof ResizeObserver === "function") {
            // Layout springs resize cards before paint; update their shadows in that frame.
            const observer = new ResizeObserver(updateEffects);
            observer.observe(pane);
            observer.observe(scrollport);
            entries.forEach(({ message }) => observer.observe(message));
        }
        if (typeof MutationObserver === "function") {
            const observer = new MutationObserver(scheduleMaterialUpdate);
            observer.observe(document.documentElement, { attributes: true });
        }
        document.fonts?.ready?.then(scheduleUpdate).catch(() => {});
        updateEffects();
    });
}());
