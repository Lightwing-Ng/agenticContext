/* Code version: v1.0.0-codex.0 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../app/web/static/wechat-browser-read.js", import.meta.url), "utf8");
const flush = () => new Promise(resolve => setImmediate(resolve));

function fixture(state, fetch) {
    const statusUrl = "/api/browser/wechat/read/synthetic-task-identifier";
    const location = {
        href: "http://127.0.0.1:8666/browser?source=wechat",
        origin: "http://127.0.0.1:8666",
        reload() { assert.fail("A failed transport must not reload the page."); },
    };
    const title = { hidden: true };
    const copy = { textContent: "" };
    const retry = {
        hidden: true,
        disabled: false,
        addEventListener(_type, listener) { this.click = listener; },
    };
    const placeholder = { hidden: false, removeAttribute() {} };
    const elements = {
        "[data-wechat-read-title]": title,
        "[data-wechat-read-copy]": copy,
        "[data-wechat-read-retry]": retry,
    };
    const root = {
        dataset: { state, statusUrl, retryUrl: statusUrl },
        querySelector: selector => elements[selector],
        closest: () => ({ querySelectorAll: () => [placeholder] }),
    };
    const waits = [];
    const deadlines = [];
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
            addEventListener() {},
            setTimeout: callback => waits.push(callback),
        },
        document: {
            querySelector: () => root,
            documentElement: { hasAttribute: () => false },
        },
    });
    return { root, retry, copy, placeholder, waits, deadlines };
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
