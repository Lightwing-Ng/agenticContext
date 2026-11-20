/* Code version: v1.2.0-codex.0 */

(function initializeSharedDisclosureMotion() {
    "use strict";

    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
    const controllers = new WeakMap();
    const active = new Map();
    const duration = 620;
    const dampingRatio = 0.8;
    const frequency = Math.sqrt(180);
    const decay = dampingRatio * frequency;
    const damped = frequency * Math.sqrt(1 - dampingRatio * dampingRatio);
    const clamp = (value, minimum, maximum) => Math.min(maximum, Math.max(minimum, value));

    function spring(time, initialVelocity = 0) {
        const coefficient = (initialVelocity - decay) / damped;
        return 1 + Math.exp(-decay * time)
            * (-Math.cos(damped * time) + coefficient * Math.sin(damped * time));
    }

    function springVelocity(time, initialVelocity = 0) {
        const coefficient = (initialVelocity - decay) / damped;
        const cosine = Math.cos(damped * time);
        const sine = Math.sin(damped * time);
        return Math.exp(-decay * time)
            * (decay * cosine - decay * coefficient * sine
                + damped * sine + coefficient * damped * cosine);
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
        let motionState = null;
        let targetOpen = details.open;
        let nativeOpen = details.open;
        const originalInert = body.inert;
        const originalExpanded = summary.getAttribute("aria-expanded");

        function stop() {
            animations.forEach((animation) => animation.cancel());
            animations = [];
            motionState = null;
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
            const elapsed = clamp(Number(animations[0]?.currentTime) || 0, 0, duration) / 1000;
            const carried = motionState ? {
                height: (motionState.end.height - motionState.start.height)
                    * springVelocity(elapsed, motionState.heightVelocity),
                width: (motionState.end.width - motionState.start.width)
                    * springVelocity(elapsed, motionState.widthVelocity),
            } : {height: 0, width: 0};
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
            const heightDistance = end.height - start.height;
            const widthDistance = end.width - start.width;
            const heightVelocity = Math.abs(heightDistance) > 0.5
                ? clamp(carried.height / heightDistance, -8, 8) : 0;
            const widthVelocity = Math.abs(widthDistance) > 0.5
                ? clamp(carried.width / widthDistance, -8, 8) : 0;
            motionState = {start, end, heightVelocity, widthVelocity};
            const samples = Array.from({ length: 41 }, (_, index) => {
                const offset = index / 40;
                const time = offset * duration / 1000;
                return {
                    offset,
                    height: offset === 1 ? 1 : spring(time, heightVelocity),
                    width: offset === 1 ? 1 : spring(time, widthVelocity),
                };
            });
            const options = { duration, easing: "linear", fill: "both" };
            const height = details.animate(samples.map(({ offset, height: position }) => ({
                height: `${Math.max(Math.min(start.height, end.height), start.height + heightDistance * position)}px`,
                overflow: "clip",
                offset,
            })), options);
            const width = widthElement?.animate(samples.map(({ offset, width: position }) => ({
                width: `${Math.max(Math.min(start.width, end.width), start.width + widthDistance * position)}px`,
                offset,
            })), options);
            const content = body.animate(samples.map(({ offset, height: position }) => ({
                transform: `translateY(${open ? (1 - clamp(position, 0, 1)) * 6 : -clamp(position, 0, 1) * 4}px)`,
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
