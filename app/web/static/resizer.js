/* Code version: v1.1.1-codex.0 */

(() => {
    "use strict";

    const clamp = (value, minimum, maximum) => Math.min(Math.max(value, minimum), maximum);
    const dampingRatio = 0.8;
    const frequency = Math.sqrt(180);
    const decay = dampingRatio * frequency;
    const damped = frequency * Math.sqrt(1 - dampingRatio * dampingRatio);
    const settleDuration = 620;

    const bind = (handle, options = {}) => {
        if (!(handle instanceof HTMLElement)) {
            return () => {};
        }

        const axis = options.axis === "block" ? "block" : "inline";
        const root = options.root instanceof HTMLElement ? options.root : handle.parentElement;
        const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
        const getRange = () => {
            const range = options.getRange?.() || {};
            const minimum = Number(range.minimum);
            const maximum = Number(range.maximum);
            const safeMinimum = Number.isFinite(minimum) ? minimum : 0;
            return {
                minimum: safeMinimum,
                maximum: Number.isFinite(maximum) ? Math.max(safeMinimum, maximum) : safeMinimum,
            };
        };
        const syncAria = (value = options.getValue?.()) => {
            const range = getRange();
            if (Number.isFinite(range.minimum)) handle.setAttribute("aria-valuemin", String(Math.round(range.minimum)));
            if (Number.isFinite(range.maximum)) handle.setAttribute("aria-valuemax", String(Math.round(range.maximum)));
            if (Number.isFinite(value)) handle.setAttribute("aria-valuenow", String(Math.round(value)));
        };
        const commit = (value, source) => {
            const range = getRange();
            const nextValue = clamp(Number(value) || 0, range.minimum, range.maximum);
            options.setValue?.(nextValue, source);
            syncAria(nextValue);
        };
        let activePointerId = null;
        let recentPositions = [];
        let dragStartValue = null;
        let settleMotion = null;
        const pointerPosition = (event) => axis === "block" ? event.clientY : event.clientX;
        const recordPointer = (event) => {
            const time = Number(event.timeStamp) || performance.now();
            recentPositions.push({position: pointerPosition(event), time});
            recentPositions = recentPositions.filter((sample) => time - sample.time <= 100).slice(-8);
        };
        const releaseVelocity = () => {
            if (recentPositions.length < 2) return 0;
            const last = recentPositions[recentPositions.length - 1];
            const first = [...recentPositions].reverse()
                .find((sample) => last.time - sample.time >= 8);
            if (!first) return 0;
            const interval = (last.time - first.time) / 1000;
            return (last.position - first.position) / interval;
        };
        const springPosition = (time, initialPosition, velocity) => Math.exp(-decay * time) * (
            initialPosition * Math.cos(damped * time)
            + (velocity + decay * initialPosition) / damped * Math.sin(damped * time)
        );
        const springVelocity = (time, initialPosition, velocity) => Math.exp(-decay * time) * (
            velocity * Math.cos(damped * time)
            - (decay * velocity + frequency * frequency * initialPosition) / damped
                * Math.sin(damped * time)
        );
        const interruptSettle = () => {
            if (!settleMotion) return {position: 0, velocity: 0, atBoundary: false};
            const {animation, initialPosition, velocity, atBoundary} = settleMotion;
            const elapsed = clamp(Number(animation.currentTime) || 0, 0, settleDuration) / 1000;
            const state = {
                position: elapsed >= settleDuration / 1000
                    ? 0 : clamp(springPosition(elapsed, initialPosition, velocity), -8, 8),
                velocity: elapsed >= settleDuration / 1000
                    ? 0 : springVelocity(elapsed, initialPosition, velocity),
                atBoundary,
            };
            animation.cancel();
            settleMotion = null;
            return state;
        };
        const settle = (velocity, atBoundary, initialPosition = 0) => {
            if (reducedMotion.matches || typeof handle.animate !== "function"
                || (Math.abs(velocity) < 20 && Math.abs(initialPosition) < 0.1)) return;
            const cappedVelocity = clamp(velocity, -240, 240);
            const boundedPosition = clamp(initialPosition, -8, 8);
            const frames = Array.from({length: 41}, (_, index) => {
                const offset = index / 40;
                const time = offset * settleDuration / 1000;
                const displacement = offset === 1 ? 0 : clamp(
                    springPosition(time, boundedPosition, cappedVelocity), -8, 8,
                );
                const stretch = atBoundary ? Math.min(0.14, Math.abs(displacement) / 55) : 0;
                const axialScale = 1 + stretch;
                const crossScale = 1 - stretch * 0.65;
                const scaleX = axis === "inline" ? axialScale : crossScale;
                const scaleY = axis === "inline" ? crossScale : axialScale;
                const translation = axis === "inline"
                    ? `translateX(${displacement}px)` : `translateY(${displacement}px)`;
                return {
                    offset,
                    transform: `translate(-50%, -50%) ${translation} scale(${scaleX}, ${scaleY})`,
                };
            });
            try {
                const animation = handle.animate(frames, {
                    duration: settleDuration,
                    easing: "linear",
                    pseudoElement: "::after",
                });
                settleMotion = {animation, initialPosition: boundedPosition,
                    velocity: cappedVelocity, atBoundary};
                animation.finished.catch(() => {}).finally(() => {
                    if (settleMotion?.animation === animation) settleMotion = null;
                });
            } catch (_error) {
                settleMotion = null;
            }
        };
        const stop = (event) => {
            if (typeof event?.pointerId === "number"
                && activePointerId !== null && event.pointerId !== activePointerId) return;
            const isRelease = event?.type === "pointerup";
            if (isRelease) {
                recordPointer(event);
                commit(options.valueFromPointer?.(pointerPosition(event)), "pointer");
            }
            const velocity = isRelease ? releaseVelocity() : 0;
            const range = getRange();
            const value = Number(options.getValue?.());
            const atBoundary = Number.isFinite(value)
                && ((value <= range.minimum + 0.5 && velocity < 0)
                    || (value >= range.maximum - 0.5 && velocity > 0));
            const wasResizing = handle.classList.contains("is-resizing");
            if (wasResizing && !isRelease && Number.isFinite(dragStartValue)) {
                commit(dragStartValue, "cancel");
            }
            handle.classList.remove("is-resizing");
            root?.classList.remove("is-surface-resizing");
            const pointerId = activePointerId;
            activePointerId = null;
            if (pointerId !== null && typeof handle.releasePointerCapture === "function") {
                try {
                    handle.releasePointerCapture(pointerId);
                } catch (_error) {
                }
            }
            window.removeEventListener("pointermove", onPointerMove);
            window.removeEventListener("pointerup", stop);
            window.removeEventListener("pointercancel", stop);
            recentPositions = [];
            dragStartValue = null;
            if (wasResizing) options.onEnd?.();
            if (isRelease) {
                const previous = interruptSettle();
                settle(velocity + previous.velocity,
                    atBoundary || previous.atBoundary, previous.position);
            } else {
                interruptSettle();
            }
        };
        const onPointerMove = (event) => {
            if (activePointerId !== null && event.pointerId !== activePointerId) return;
            recordPointer(event);
            commit(options.valueFromPointer?.(pointerPosition(event)), "pointer");
        };
        const onVisibilityChange = () => {
            if (document.hidden) {
                stop();
            }
        };
        const onLostPointerCapture = () => {
            if (activePointerId !== null) stop();
        };
        const onWindowBlur = () => {
            stop();
        };
        const onMotionPreferenceChange = () => {
            if (reducedMotion.matches) interruptSettle();
        };
        const onPointerDown = (event) => {
            if (event.button !== 0) {
                return;
            }
            event.preventDefault();
            recentPositions = [];
            dragStartValue = Number(options.getValue?.());
            activePointerId = typeof event.pointerId === "number" ? event.pointerId : null;
            if (activePointerId !== null && typeof handle.setPointerCapture === "function") {
                try {
                    handle.setPointerCapture(activePointerId);
                } catch (_error) {
                }
            }
            handle.classList.add("is-resizing");
            root?.classList.add("is-surface-resizing");
            options.onStart?.();
            onPointerMove(event);
            window.addEventListener("pointermove", onPointerMove);
            window.addEventListener("pointerup", stop);
            window.addEventListener("pointercancel", stop);
        };
        const onKeyDown = (event) => {
            const decrementKey = axis === "block" ? "ArrowUp" : "ArrowLeft";
            const incrementKey = axis === "block" ? "ArrowDown" : "ArrowRight";
            const range = getRange();
            const currentValue = Number(options.getValue?.());
            const step = event.shiftKey ? Number(options.largeStep || 48) : Number(options.step || 16);
            let nextValue = null;
            if (event.key === decrementKey) nextValue = currentValue - step;
            if (event.key === incrementKey) nextValue = currentValue + step;
            if (event.key === "Home") nextValue = range.minimum;
            if (event.key === "End") nextValue = range.maximum;
            if (!Number.isFinite(nextValue)) {
                return;
            }
            event.preventDefault();
            commit(nextValue, "keyboard");
        };

        handle.addEventListener("pointerdown", onPointerDown);
        handle.addEventListener("lostpointercapture", onLostPointerCapture);
        handle.addEventListener("keydown", onKeyDown);
        window.addEventListener("blur", onWindowBlur);
        document.addEventListener("visibilitychange", onVisibilityChange);
        reducedMotion.addEventListener?.("change", onMotionPreferenceChange);
        syncAria();
        return () => {
            stop();
            interruptSettle();
            handle.removeEventListener("pointerdown", onPointerDown);
            handle.removeEventListener("lostpointercapture", onLostPointerCapture);
            handle.removeEventListener("keydown", onKeyDown);
            window.removeEventListener("blur", onWindowBlur);
            document.removeEventListener("visibilitychange", onVisibilityChange);
            reducedMotion.removeEventListener?.("change", onMotionPreferenceChange);
        };
    };

    window.CACHE_LIKES_RESIZER = Object.freeze({bind});
})();
