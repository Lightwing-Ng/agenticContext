/* Code version: v1.1.0-codex.0 */

(function initializeSharedDisclosureMotion() {
    "use strict";

    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
    const controllers = new WeakMap();
    const active = new Map();
    // Worthward's bouncy preset: mass 1, stiffness 180, damping 18, velocity 0.
    const duration = 620;
    const frequency = Math.sqrt(180);
    const damped = Math.sqrt(180 - 9 * 9);

    function spring(progress) {
        if (progress === 0 || progress === 1) return progress;
        const time = progress * duration / 1000;
        return 1 - Math.exp(-9 * time)
            * (Math.cos(damped * time) + 9 / damped * Math.sin(damped * time));
    }

    function closeProgress(progress) {
        // Critical damping keeps the summary visible throughout closing.
        const displacement = (time) => 1 - Math.exp(-frequency * time) * (1 + frequency * time);
        return displacement(progress * duration / 1000) / displacement(duration / 1000);
    }

    function settleConflicts(details) {
        for (const [other, finish] of [...active]) {
            // Related fixed-height animations cannot measure each other's natural size.
            // Keep self active until move() captures its presented reversal geometry.
            if (other !== details && (other.contains(details) || details.contains(other))) finish();
        }
    }

    function bind(details, widthTarget) {
        if (controllers.has(details)) return;
        const summary = details.querySelector(":scope > summary");
        const body = details.querySelector(":scope > .ui-collapse-body");
        if (!summary || !body) return;
        const widthElement = typeof widthTarget === "function" ? widthTarget(details) : null;
        let animations = [];
        let targetOpen = details.open;
        let nativeOpen = details.open;
        const originalInert = body.inert;
        const originalExpanded = summary.getAttribute("aria-expanded");

        function stop() {
            animations.forEach((animation) => animation.cancel());
            animations = [];
            active.delete(details);
        }

        function finish() {
            stop();
            nativeOpen = targetOpen;
            details.open = targetOpen;
            body.inert = originalInert;
            if (originalExpanded === null) summary.removeAttribute("aria-expanded");
            else summary.setAttribute("aria-expanded", originalExpanded);
            delete details.dataset.collapseMotion;
        }

        function geometry() {
            // Computed CSS dimensions stay correct under an ancestor's transform.
            return {
                height: parseFloat(getComputedStyle(details).height),
                width: widthElement ? parseFloat(getComputedStyle(widthElement).width) : 0,
            };
        }

        function move(open, nativeToggle = false) {
            let start = geometry();
            const interrupted = animations.length > 0;
            // Capture first: settling a descendant can change the parent's natural size.
            settleConflicts(details);
            stop();
            targetOpen = open;
            if (reduced.matches || typeof details.animate !== "function") {
                finish();
                return;
            }
            if (nativeToggle && !interrupted) {
                details.open = !open;
                start = geometry();
            }
            details.open = open;
            const end = geometry();
            // Keep native content available until the closing animation has settled.
            nativeOpen = true;
            details.open = true;
            if (Math.abs(start.height - end.height) < 0.5 && Math.abs(start.width - end.width) < 0.5) {
                finish();
                return;
            }
            summary.setAttribute("aria-expanded", String(open));
            body.inert = open ? originalInert : true;
            if (!open && body.contains(document.activeElement)) summary.focus({ preventScroll: true });
            details.dataset.collapseMotion = open ? "open" : "close";
            const samples = Array.from({ length: 41 }, (_, index) => {
                const offset = index / 40;
                return { offset, position: open ? spring(offset) : closeProgress(offset) };
            });
            const options = { duration, easing: "linear", fill: "both" };
            const height = details.animate(samples.map(({ offset, position }) => ({
                height: `${start.height + (end.height - start.height) * position}px`,
                overflow: "clip",
                offset,
            })), options);
            const width = widthElement?.animate(samples.map(({ offset, position }) => ({
                width: `${start.width + (end.width - start.width) * position}px`,
                offset,
            })), options);
            const content = body.animate(samples.map(({ offset, position }) => ({
                transform: `translateY(${open ? (1 - position) * 6 : -position * 4}px) scaleY(${open ? 0.985 + 0.015 * position : 1 - 0.015 * position})`,
                transformOrigin: "top center",
                offset,
            })), options);
            animations = width ? [height, width, content] : [height, content];
            active.set(details, finish);
            height.finished.then(() => {
                if (animations[0] === height) finish();
            }).catch(() => {});
        }

        function setControlledOpen(open, animate) {
            if (animate) {
                move(open);
                return;
            }
            const restoreFocus = !open && body.contains(document.activeElement);
            settleConflicts(details);
            targetOpen = open;
            finish();
            if (restoreFocus) summary.focus({ preventScroll: true });
        }

        summary.addEventListener("click", (event) => {
            if (event.defaultPrevented || event.target.closest("a, button, input, select, textarea, [contenteditable]")) return;
            event.preventDefault();
            move(animations.length ? !targetOpen : !details.open);
        });
        details.addEventListener("toggle", () => {
            if (details.open !== nativeOpen) move(details.open, true);
        });
        controllers.set(details, { move, setOpen: setControlledOpen });
    }

    function enhance(root, { selector, widthTarget } = {}) {
        for (const [details, finish] of active) {
            if (!details.isConnected) finish();
        }
        if (!root || typeof root.querySelectorAll !== "function" || !selector) return;
        if (root.matches?.(selector)) bind(root, widthTarget);
        root.querySelectorAll(selector).forEach((details) => bind(details, widthTarget));
    }

    function setOpen(details, open, { animate = false } = {}) {
        const controller = controllers.get(details);
        if (controller) controller.setOpen(Boolean(open), animate);
        else if (details?.matches?.("details")) details.open = Boolean(open);
    }

    const finishAll = () => [...active.values()].forEach((finish) => finish());
    reduced.addEventListener("change", () => {
        if (reduced.matches) finishAll();
    });
    window.addEventListener("resize", finishAll);
    window.addEventListener("pagehide", finishAll);
    document.fonts?.addEventListener("loadingdone", finishAll);

    window.SharedDisclosureMotion = Object.freeze({ enhance, finishAll, setOpen });
}());
