/* Code version: v1.0.0-codex.0 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../app/web/static/browser-chat-ruler.js", import.meta.url), "utf8");

function fixture({ count = 3, text = "A cached message.", reducedMotion = true, valid = true } = {}) {
    let document;
    function element() {
        const attrs = new Map();
        const listeners = new Map();
        const selectors = new Map();
        return {
            attrs, selectors, dataset: {}, style: {}, hidden: false,
            clientTop: 0, clientHeight: 240, offsetHeight: 120, textContent: "",
            getAttribute: (name) => attrs.get(name) || null,
            setAttribute: (name, value) => attrs.set(name, value),
            removeAttribute: (name) => attrs.delete(name),
            querySelector: (selector) => selectors.get(selector) || null,
            querySelectorAll: (selector) => selectors.get(selector) || [],
            getBoundingClientRect: () => ({ top: 100, height: 240 }),
            addEventListener(name, handler) {
                const handlers = listeners.get(name) || [];
                handlers.push(handler);
                listeners.set(name, handlers);
            },
            emit(name, fields = {}) {
                const event = { prevented: false, preventDefault() { this.prevented = true; }, ...fields };
                (listeners.get(name) || []).forEach((handler) => handler(event));
                return event;
            },
            focus(options) {
                this.focusOptions = options;
                if (document.activeElement === this) return;
                document.activeElement?.emit("blur");
                document.activeElement = this;
                this.emit("focus");
            },
            set innerHTML(_value) { throw new Error("Preview must use textContent."); },
        };
    }
    document = element();
    const pane = element();
    const scrollport = element();
    const ruler = element();
    const preview = element();
    const label = element();
    const copy = element();
    const frames = new Map();
    const timers = new Map();
    const observers = [];
    const scrolls = [];
    let identifier = 0;
    ruler.hidden = true;
    preview.hidden = true;
    preview.id = "browser_chat_preview";
    scrollport.scrollTop = 0;
    scrollport.scrollTo = (options) => {
        scrolls.push(options);
        scrollport.scrollTop = options.top;
        scrollport.emit("scroll");
    };
    const messages = Array.from({ length: count }, (_, index) => {
        const message = element();
        message.id = `message-${index}`;
        message.dataset = { chatAuthor: index % 2 ? "Assistant" : "User", chatNumber: `${index + 1}` };
        message.getBoundingClientRect = () => ({ top: 148 + index * 400 - scrollport.scrollTop, height: 350 });
        const content = element();
        content.textContent = text;
        message.selectors.set(".browser-chat-message-content", content);
        return message;
    });
    const markers = messages.map((message, index) => {
        const marker = element();
        marker.dataset.chatTarget = valid ? message.id : "missing";
        marker.getBoundingClientRect = () => ({ top: 108 + index * (224 / count), height: 224 / count });
        return marker;
    });
    markers[0]?.setAttribute("aria-describedby", "existing-description");
    pane.selectors.set("[data-chat-scrollport]", scrollport);
    pane.selectors.set(".browser-chat-ruler", ruler);
    pane.selectors.set("[data-chat-preview]", preview);
    preview.selectors.set("[data-chat-preview-label]", label);
    preview.selectors.set("[data-chat-preview-copy]", copy);
    scrollport.selectors.set("[data-chat-message-id][id]", messages);
    ruler.selectors.set("[data-chat-marker]", markers);
    document.selectors.set("[data-browser-chat-pane]", [pane]);
    const window = element();
    Object.assign(window, {
        getComputedStyle: () => ({ paddingTop: "48px" }),
        matchMedia: () => ({ matches: reducedMotion }),
        requestAnimationFrame: (callback) => { frames.set(++identifier, callback); return identifier; },
        setTimeout: (callback) => { timers.set(++identifier, callback); return identifier; },
        clearTimeout: (id) => timers.delete(id),
    });
    vm.runInNewContext(source, {
        document, window,
        ResizeObserver: class {
            constructor(callback) { observers.push(callback); }
            observe() {}
        },
    });
    function flush(queue) {
        const callbacks = Array.from(queue.values());
        queue.clear();
        callbacks.forEach((callback) => callback());
    }
    return {
        document, window, pane, scrollport, ruler, preview, label, copy, messages, markers,
        frames, observers, scrolls, flushFrames: () => flush(frames), flushTimers: () => flush(timers),
        key: (index, key) => markers[index].emit("keydown", { key }),
    };
}

test("a long session has one current marker and coalesces scroll and resize observations", () => {
    const f = fixture({ count: 100 });
    assert.equal(f.ruler.hidden, false);
    assert.equal(f.preview.hidden, true);
    assert.equal(f.markers.filter((marker) => marker.getAttribute("aria-current") === "true").length, 1);
    assert.equal(f.markers.filter((marker) => marker.tabIndex === 0).length, 1);
    f.scrollport.scrollTop = 39_600;
    for (let index = 0; index < 20; index++) {
        f.scrollport.emit("scroll");
        f.observers[0]();
    }
    assert.equal(f.frames.size, 1);
    f.flushFrames();
    assert.equal(f.markers[99].getAttribute("aria-current"), "true");
    assert.equal(f.markers[0].getAttribute("aria-current"), null);
    assert.equal(f.frames.size, 0);
});

test("keyboard boundaries preview without scrolling, while Space and Enter activate only the scrollport", () => {
    const f = fixture();
    f.markers[0].focus();
    f.key(0, "ArrowUp");
    assert.equal(f.document.activeElement, f.markers[0]);
    f.key(0, "End");
    f.key(2, "ArrowDown");
    assert.equal(f.document.activeElement, f.markers[2]);
    assert.equal(f.scrolls.length, 0);
    assert.equal(f.key(2, " ").prevented, true);
    assert.equal(f.scrolls.length, 1);
    assert.equal(f.scrolls[0].top, 800);
    assert.equal(f.scrolls[0].behavior, "instant");
    assert.equal(f.markers[2].focusOptions.preventScroll, true);
    f.key(2, "Home");
    f.key(0, "Enter");
    assert.equal(f.scrolls.length, 2);
    assert.equal(f.scrolls[1].top, 0);
    assert.equal(f.key(0, "Tab").prevented, false);
});

test("Escape preserves focus and description tokens, and scroll or resize does not reopen the preview", () => {
    const f = fixture();
    f.markers[0].focus();
    assert.equal(f.markers[0].getAttribute("aria-describedby"), "existing-description browser_chat_preview");
    f.document.emit("keydown", { key: "Escape" });
    f.scrollport.emit("scroll");
    f.window.emit("resize");
    f.flushFrames();
    assert.equal(f.preview.hidden, true);
    assert.equal(f.document.activeElement, f.markers[0]);
    assert.equal(f.markers[0].getAttribute("aria-describedby"), "existing-description");
    f.markers[0].emit("click");
    assert.equal(f.preview.hidden, false);
});

test("preview text truncates by Unicode characters, empty media is explicit, and tooltip stays inside the pane", () => {
    const f = fixture({ text: "🧪".repeat(241) });
    f.markers[2].focus();
    assert.equal(f.copy.textContent, `${"🧪".repeat(240)}…`);
    assert.equal(f.label.textContent, "User #3");
    assert.ok(Number.parseFloat(f.preview.style.top) >= 0);
    assert.ok(Number.parseFloat(f.preview.style.top) + f.preview.offsetHeight <= f.pane.clientHeight);
    f.messages[0].selectors.delete(".browser-chat-message-content");
    f.markers[0].focus();
    assert.equal(f.copy.textContent, "No text preview available.");
});

test("pointer can move from marker into tooltip, and click follows the motion preference", () => {
    const f = fixture({ reducedMotion: false });
    f.markers[0].emit("pointerenter");
    f.markers[0].emit("pointerleave");
    f.preview.emit("pointerenter");
    f.flushTimers();
    assert.equal(f.preview.hidden, false);
    f.preview.emit("pointerleave");
    f.flushTimers();
    assert.equal(f.preview.hidden, true);
    f.markers[1].emit("click");
    assert.equal(f.document.activeElement, f.markers[1]);
    assert.equal(f.scrolls[0].behavior, "smooth");
});

test("empty or invalid message targets keep the ruler hidden", () => {
    assert.equal(fixture({ count: 0 }).ruler.hidden, true);
    assert.equal(fixture({ valid: false }).ruler.hidden, true);
});
