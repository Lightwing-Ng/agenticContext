/* Code version: v1.4.0-codex.0 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../app/web/static/wechat-browser-read.js", import.meta.url), "utf8");
const flush = () => new Promise(resolve => setImmediate(resolve));

function fixture(state, fetch, { preview = false, accountPreview = false, accountRefreshing = false } = {}) {
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
        dataset: {
            state, statusUrl, retryUrl: statusUrl, taskId: "synthetic-task", generation: "1",
            wechatReadAccountPreview: String(accountPreview),
        },
        querySelector: selector => elements[selector],
        closest: () => ({ querySelectorAll: () => [placeholder, ...previewNodes] }),
    };
    let now = 0;
    const waits = [];
    const deadlines = [];
    const windowListeners = new Map();
    vm.runInNewContext(source, {
        fetch,
        URL,
        AbortController,
        AbortSignal: {},
        Date: { now: () => now },
        window: {
            location,
            history: { state: null, replaceState(state) { this.state = state; } },
            addEventListener(name, callback) { windowListeners.set(name, callback); },
            setTimeout(callback, delay) {
                const time = now + delay;
                const timer = {
                    delay,
                    canceled: false,
                    expire() {
                        assert.equal(timer.canceled, false);
                        now = Math.max(now, time);
                        callback();
                    },
                };
                if (delay === 10_000) deadlines.push(timer);
                else waits.push(timer.expire);
                return timer;
            },
            clearTimeout(timer) { timer.canceled = true; },
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

test("stalled status bodies trigger bounded Retry without AbortSignal.timeout or a reload", async () => {
    const page = fixture("running", async (_url, { method, signal }) => {
        assert.equal(method, "GET");
        return { ok: true, json: () => stall(signal) };
    });
    for (let attempt = 0; attempt < 4; attempt++) {
        await flush();
        assert.equal(page.root.dataset.state, "running");
        assert.equal(page.deadlines[attempt]?.delay, 10_000);
        page.deadlines[attempt].expire();
        await flush();
        if (attempt < 3) page.waits.shift()();
    }
    assert.equal(page.root.dataset.state, "failed");
    assert.equal(page.placeholder.hidden, true);
    assert.equal(page.retry.hidden, false);
    assert.match(page.copy.textContent, /Lost contact with the local service/);
    assert.equal(page.reloads(), 0);
});

test("five immediate saved-view status errors still allow a healthy retry", async () => {
    let attempts = 0;
    const page = fixture("running", async () => {
        if (++attempts <= 5) return { ok: false, status: 503 };
        return {
            ok: true,
            json: async () => ({ task_id: "synthetic-task", generation: 1, state: "succeeded" }),
        };
    });
    for (let attempt = 0; attempt < 5; attempt++) {
        await flush();
        assert.equal(page.root.dataset.state, "running");
        assert.equal(page.retry.hidden, true);
        assert.equal(page.deadlines[attempt].canceled, true);
        page.waits.shift()();
    }
    await flush();
    assert.equal(attempts, 6);
    assert.equal(page.reloads(), 1);
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

test("an expired status keeps the verified preview and Retry only reloads the current view", async () => {
    const methods = [];
    const page = fixture("running", async (_url, { method }) => {
        methods.push(method);
        return { ok: false, status: 404 };
    }, { preview: true });
    await flush();
    assert.equal(page.root.dataset.state, "failed");
    assert.equal(page.placeholder.hidden, true);
    assert.ok(page.previewNodes.every(node => !node.hidden));
    assert.match(page.copy.textContent, /Showing the last verified conversations/);
    assert.equal(page.retry.hidden, false);
    assert.equal(page.reloads(), 0);
    assert.equal(page.waits.length, 0);

    await page.retry.click();
    assert.deepEqual(methods, ["GET"]);
    assert.equal(page.reloads(), 1);
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

for (const initialState of ["running", "failed"]) {
    test(`a failed account read keeps verified navigation from an initially ${initialState} document`, async () => {
        const page = fixture(initialState, async () => ({
            ok: true,
            json: async () => ({
                task_id: "synthetic-task", generation: 1, state: "failed",
                message: "Saved records could not be checked. Try loading this view again.",
            }),
        }), { preview: true, accountPreview: true });
        await flush();
        assert.equal(page.root.dataset.state, "failed");
        assert.equal(page.placeholder.hidden, true);
        assert.ok(page.previewNodes.every(node => !node.hidden));
        assert.match(page.copy.textContent, /Showing the last verified conversations/);
        assert.equal(page.retry.hidden, false);
        assert.equal(page.reloads(), 0);
    });
}

test("a changed task identity still hides the earlier account navigation", async () => {
    const page = fixture("running", async () => ({
        ok: true,
        json: async () => ({ task_id: "different-task", generation: 1, state: "failed" }),
    }), { preview: true, accountPreview: true });
    await flush();
    assert.equal(page.root.dataset.state, "failed");
    assert.ok(page.previewNodes.every(node => node.hidden));
    assert.match(page.copy.textContent, /The saved-view task changed/);
});
