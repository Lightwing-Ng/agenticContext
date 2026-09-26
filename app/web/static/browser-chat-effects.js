/* Code version: v1.0.0-codex.0 */

(function initializeBrowserChatEffects() {
    "use strict";

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

    document.querySelectorAll("[data-browser-chat-pane]").forEach((pane) => {
        const scrollport = pane.querySelector("[data-chat-scrollport]");
        const layer = pane.querySelector("[data-chat-effects]");
        if (!scrollport || !layer || pane.hasAttribute("data-chat-effects-ready")) return;
        const entries = Array.from(scrollport.querySelectorAll(".browser-chat-message"), (message) => {
            const effect = document.createElement("div");
            effect.className = "browser-chat-effect";
            effect.dataset.chatEffectFor = message.id;
            effect.hidden = true;
            layer.append(effect);
            return { message, effect };
        });
        if (!entries.length) return;
        let frame = 0;
        let materialDirty = true;
        let bleed = 48;

        function updateEffects() {
            frame = 0;
            if (materialDirty) {
                const style = window.getComputedStyle(pane);
                const shadows = splitShadows(style.getPropertyValue("--frosted-glass-shadow"));
                bleed = Number.parseFloat(style.getPropertyValue("--layout-physical-effect-bleed")) || 48;
                // Preserve the shared inset highlight while painting outer shadows separately.
                pane.style.setProperty("--browser-chat-outer-shadow",
                    shadows.filter((shadow) => !/\binset\b/.test(shadow)).join(", ") || "none");
                pane.style.setProperty("--browser-chat-inset-shadow",
                    shadows.filter((shadow) => /\binset\b/.test(shadow)).join(", ") || "none");
                materialDirty = false;
            }
            const layerBounds = layer.getBoundingClientRect();
            const viewport = scrollport.getBoundingClientRect();
            const scaleX = layerBounds.width / layer.clientWidth || 1;
            const scaleY = layerBounds.height / layer.clientHeight || 1;
            const rectangles = entries.map(({ message }) => message.getBoundingClientRect());
            entries.forEach(({ effect }, index) => {
                const bounds = rectangles[index];
                effect.hidden = bounds.bottom < viewport.top - bleed * scaleY
                    || bounds.top > viewport.bottom + bleed * scaleY;
                if (effect.hidden) return;
                // Full rectangles avoid inventing rounded edges at the scroll boundary.
                effect.style.left = `${(bounds.left - layerBounds.left) / scaleX}px`;
                effect.style.top = `${(bounds.top - layerBounds.top) / scaleY}px`;
                effect.style.width = `${bounds.width / scaleX}px`;
                effect.style.height = `${bounds.height / scaleY}px`;
            });
            pane.setAttribute("data-chat-effects-ready", "");
        }

        function scheduleUpdate() {
            if (!frame) frame = window.requestAnimationFrame(updateEffects);
        }

        function scheduleMaterialUpdate() {
            materialDirty = true;
            scheduleUpdate();
        }

        scrollport.addEventListener("scroll", scheduleUpdate, { passive: true });
        window.addEventListener("resize", scheduleUpdate, { passive: true });
        window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", scheduleMaterialUpdate);
        if (typeof ResizeObserver === "function") {
            const observer = new ResizeObserver(scheduleUpdate);
            observer.observe(pane);
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
