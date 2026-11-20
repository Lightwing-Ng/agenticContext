/* Code version: v1.0.1-codex.0 */

import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {test} from "node:test";
import {runInNewContext} from "node:vm";

const script = readFileSync(new URL("../app/web/static/resizer.js", import.meta.url), "utf8");

function harness({reducedMotion = false} = {}) {
    class Target {
        constructor() {
            this.listeners = new Map();
        }

        addEventListener(type, listener) {
            const listeners = this.listeners.get(type) || new Set();
            listeners.add(listener);
            this.listeners.set(type, listeners);
        }

        removeEventListener(type, listener) {
            this.listeners.get(type)?.delete(listener);
        }

        emit(type, properties = {}) {
            const event = {type, preventDefault() {}, ...properties};
            for (const listener of [...(this.listeners.get(type) || [])]) listener(event);
        }
    }

    class Element extends Target {
        constructor() {
            super();
            this.attributes = new Map();
            this.classes = new Set();
            this.classList = {
                add: (name) => this.classes.add(name),
                remove: (name) => this.classes.delete(name),
                contains: (name) => this.classes.has(name),
            };
            this.animations = [];
        }

        setAttribute(name, value) { this.attributes.set(name, value); }
        getAttribute(name) { return this.attributes.get(name); }
        setPointerCapture() {}
        releasePointerCapture() {}
        animate(frames, options) {
            const animation = {
                frames,
                options,
                currentTime: 0,
                cancelled: false,
                finished: new Promise(() => {}),
                cancel() { this.cancelled = true; },
            };
            this.animations.push(animation);
            return animation;
        }
    }

    const window = new Target();
    const media = new Target();
    media.matches = reducedMotion;
    window.matchMedia = () => media;
    const document = new Target();
    document.hidden = false;
    runInNewContext(script, {window, document, HTMLElement: Element, performance: {now: () => 0}});
    const root = new Element();
    const handle = new Element();
    handle.parentElement = root;
    let value = 50;
    const unbind = window.CACHE_LIKES_RESIZER.bind(handle, {
        root,
        getRange: () => ({minimum: 0, maximum: 100}),
        getValue: () => value,
        setValue: (next) => { value = next; },
        valueFromPointer: (position) => position,
    });
    const pointer = (target, type, x, time) => {
        target.emit(type, {button: 0, pointerId: 1, clientX: x, clientY: 0, timeStamp: time});
    };
    return {window, document, media, handle, pointer, unbind, get value() { return value; }};
}

function releaseAtBoundary(boundary, fast) {
    const state = harness();
    const start = 50;
    const near = boundary === "max" ? 95 : 5;
    const end = boundary === "max" ? 100 : 0;
    state.pointer(state.handle, "pointerdown", start, 0);
    state.pointer(state.window, "pointermove", near, fast ? 40 : 300);
    state.pointer(state.window, "pointerup", end, fast ? 50 : 400);
    return state;
}

test("fast release makes a larger bounded jelly at both edges without moving the model", () => {
    for (const boundary of ["min", "max"]) {
        const slow = releaseAtBoundary(boundary, false);
        const fast = releaseAtBoundary(boundary, true);
        assert.equal(slow.value, boundary === "max" ? 100 : 0);
        assert.equal(fast.value, slow.value);
        const slowAnimation = slow.handle.animations.at(-1);
        const fastAnimation = fast.handle.animations.at(-1);
        assert.equal(fastAnimation.options.pseudoElement, "::after");
        assert.equal(fastAnimation.options.duration, 620);
        const displacement = (animation) => Number(
            animation.frames[6].transform.match(/translateX\((-?[\d.]+)px\)/)[1],
        );
        assert.ok(Math.abs(displacement(fastAnimation)) > Math.abs(displacement(slowAnimation)) + 2);
        assert.ok(Math.abs(displacement(fastAnimation)) <= 8);
        const scales = fastAnimation.frames[6].transform.match(/scale\(([\d.]+), ([\d.]+)\)/);
        assert.ok(Number(scales[1]) > 1.1);
        assert.ok(Number(scales[2]) < 0.94);
        assert.equal(fastAnimation.frames.at(-1).transform,
            "translate(-50%, -50%) translateX(0px) scale(1, 1)");
        slow.unbind();
        fast.unbind();
    }
});

test("release direction follows the most recent motion and cancellation creates no spring", () => {
    const state = harness();
    state.pointer(state.handle, "pointerdown", 50, 0);
    state.pointer(state.window, "pointermove", 100, 40);
    state.pointer(state.window, "pointermove", 80, 70);
    state.pointer(state.window, "pointerup", 70, 90);
    assert.equal(state.value, 70);
    assert.match(state.handle.animations[0].frames[6].transform, /translateX\(-/);
    state.pointer(state.handle, "pointerdown", 50, 200);
    state.pointer(state.window, "pointermove", 90, 230);
    state.pointer(state.window, "pointercancel", 90, 240);
    assert.equal(state.value, 70);
    assert.equal(state.handle.animations.length, 1);
    assert.ok(state.handle.animations[0].cancelled);
    state.unbind();
});

test("interrupted settle carries its visible position and velocity into the next release", () => {
    const state = releaseAtBoundary("max", true);
    const previous = state.handle.animations[0];
    previous.currentTime = 93;
    const priorDisplacement = Number(
        previous.frames[6].transform.match(/translateX\((-?[\d.]+)px\)/)[1],
    );
    state.pointer(state.handle, "pointerdown", 100, 200);
    assert.equal(previous.cancelled, false);
    state.pointer(state.window, "pointermove", 90, 220);
    state.pointer(state.window, "pointerup", 80, 240);
    const next = state.handle.animations[1];
    const initialDisplacement = Number(
        next.frames[0].transform.match(/translateX\((-?[\d.]+)px\)/)[1],
    );
    assert.ok(previous.cancelled);
    assert.ok(Math.abs(initialDisplacement - priorDisplacement) < 0.001);
    assert.equal(state.value, 80);
    state.unbind();
});

test("blur and lost capture restore the pre-drag value", () => {
    const state = harness();
    state.pointer(state.handle, "pointerdown", 50, 0);
    state.pointer(state.window, "pointermove", 90, 30);
    state.window.emit("blur");
    assert.equal(state.value, 50);
    state.pointer(state.handle, "pointerdown", 50, 100);
    state.pointer(state.window, "pointermove", 10, 130);
    state.handle.emit("lostpointercapture");
    assert.equal(state.value, 50);
    state.unbind();
});

test("Reduced Motion clamps the value and skips transient deformation", () => {
    const state = harness({reducedMotion: true});
    state.pointer(state.handle, "pointerdown", 50, 0);
    state.pointer(state.window, "pointermove", 95, 40);
    state.pointer(state.window, "pointerup", 100, 50);
    assert.equal(state.value, 100);
    assert.equal(state.handle.animations.length, 0);
    state.unbind();
});

test("changing Reduced Motion during a settle cancels the visual layer", () => {
    const state = releaseAtBoundary("max", true);
    const animation = state.handle.animations[0];
    assert.equal(animation.cancelled, false);
    state.media.matches = true;
    state.media.emit("change");
    assert.equal(animation.cancelled, true);
    assert.equal(state.value, 100);
    state.unbind();
});
