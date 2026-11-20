/* Code version: v1.1.0-codex.0 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../app/web/static/wechat-browser-read.js", import.meta.url), "utf8");
const flush = () => new Promise(resolve => setImmediate(resolve));

function fixture(state, fetch, { preview = false, accountRefreshing = false } = {}) {
    const statusUrl = "/api/browser/wechat/read/synthetic-task-identifier";
    let reloads = 0;
    const location = {
        href: "http://127.0.0.1:8666/browser?source=wechat",
        origin: "http://127.0.0.1:8666",
        reload() { reloads += 1; },
    };
    const title = { hidden: true };
    const copy = { textContent: "" };
    const retry = {
        hidden: true,
        disabled: false,
        addEventListener(_type, listener) { this.click = listener; },
    };
    const placeholder = { hidden: false, hasAttribute: () => false, removeAttribute() {} };
    const previewNodes = preview ? Array.from({ length: 2 }, () => ({
        hidden: false,
        hasAttribute: name => name === "data-wechat-read-preview",
        removeAttribute() {},
    })) : [];
    const elements = {
        "[data-wechat-read-title]": title,
        "[data-wechat-read-copy]": copy,
        "[data-wechat-read-retry]": retry,
    };
    const root = {
        dataset: { state, statusUrl, retryUrl: statusUrl, taskId: "synthetic-task", generation: "1" },
        querySelector: selector => elements[selector],
        closest: () => ({ querySelectorAll: () => [placeholder, ...previewNodes] }),
    };
    const waits = [];
    const deadlines = [];
    const windowListeners = new Map();
    vm.runInNewContext(source, {
        fetch,
        URL,
        AbortSignal: {
            timeout(delay) {
                const controller = new AbortController();
                deadlines.push({ delay, expire: () => controller.abort() });
                return controller.signal;
            },
        },
        window: {
            location,
            history: { state: null, replaceState(state) { this.state = state; } },
            addEventListener(name, callback) { windowListeners.set(name, callback); },
            setTimeout: callback => waits.push(callback),
        },
        document: {
            querySelector: (selector) => selector === "[data-wechat-read-pending]"
                ? root : accountRefreshing ? {} : null,
            documentElement: { hasAttribute: () => false },
        },
    });
    return {
        root, retry, copy, placeholder, previewNodes, waits, deadlines,
        reloads: () => reloads,
        settleAccountRefresh() {
            accountRefreshing = false;
            windowListeners.get("wechat:account-refresh-settled")();
        },
    };
}

function stall(signal) {
    return new Promise((_resolve, reject) => {
        signal?.addEventListener("abort", () => reject(signal.reason), { once: true });
    });
}

test("five stalled status bodies reveal Retry without reloading", async () => {
    const page = fixture("running", async (_url, { method, signal }) => {
        assert.equal(method, "GET");
        return { ok: true, json: () => stall(signal) };
    });
    for (let attempt = 0; attempt < 5; attempt++) {
        await flush();
        assert.equal(page.root.dataset.state, "running");
        assert.equal(page.deadlines[attempt]?.delay, 10_000);
        page.deadlines[attempt].expire();
        await flush();
        if (attempt < 4) page.waits.shift()();
    }
    assert.equal(page.root.dataset.state, "failed");
    assert.equal(page.placeholder.hidden, true);
    assert.equal(page.retry.hidden, false);
    assert.match(page.copy.textContent, /Lost contact with the local service/);
    assert.equal(page.reloads(), 0);
});

test("a ready saved view waits for active account refresh to settle before reloading", async () => {
    const page = fixture("running", async () => ({
        ok: true,
        json: async () => ({ task_id: "synthetic-task", generation: 1, state: "succeeded" }),
    }), { accountRefreshing: true });
    await flush();
    assert.equal(page.reloads(), 0);
    page.settleAccountRefresh();
    assert.equal(page.reloads(), 1);
    page.settleAccountRefresh();
    assert.equal(page.reloads(), 1);
});

test("a stalled Retry request re-enables the button", async () => {
    const page = fixture("failed", (_url, { method, signal }) => {
        assert.equal(method, "POST");
        return stall(signal);
    });
    const retry = page.retry.click();
    assert.equal(page.retry.disabled, true);
    assert.equal(page.deadlines[0]?.delay, 10_000);
    page.deadlines[0].expire();
    await retry;
    assert.equal(page.retry.disabled, false);
    assert.match(page.copy.textContent, /The local service is unavailable/);
});

test("the polling limit keeps verified navigation visible and exposes Retry", async () => {
    const page = fixture("running", async () => ({
        ok: true,
        json: async () => ({ task_id: "synthetic-task", generation: 1, state: "running" }),
    }), { preview: true });
    for (let attempt = 0; attempt < 1_800; attempt++) {
        await flush();
        page.waits.shift()();
    }
    await flush();
    assert.equal(page.root.dataset.state, "failed");
    assert.equal(page.placeholder.hidden, true);
    assert.ok(page.previewNodes.every(node => !node.hidden));
    assert.equal(page.retry.hidden, false);
    assert.match(page.copy.textContent, /Showing the last verified conversations/);
});

test("a rejected saved view still hides the pending content", async () => {
    const page = fixture("running", async () => ({
        ok: true,
        json: async () => ({ task_id: "synthetic-task", generation: 1, state: "failed" }),
    }), { preview: true });
    await flush();
    assert.equal(page.root.dataset.state, "failed");
    assert.ok(page.previewNodes.every(node => node.hidden));
    assert.equal(page.retry.hidden, false);
});
