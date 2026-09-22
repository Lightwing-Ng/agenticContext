/* Code version: v1.0.0-codex.0 */

import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {test} from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../app/web/static/numeric-display.js", import.meta.url), "utf8");
const context = {};
vm.runInNewContext(source, context);
const numericDisplay = context.SHARED_NUMERIC_DISPLAY;

test("numeric parser preserves complete accessible values", () => {
    assert.deepEqual(
        Array.from(numericDisplay.getNumericDisplayParts("$7,089.68"), part => ({...part})),
        [
            {className: "workspace-metric-value-major", text: "$7,089"},
            {className: "workspace-metric-value-minor", text: ".68"},
        ],
    );
    assert.deepEqual(
        Array.from(numericDisplay.getNumericDisplayParts("30.51%"), part => ({...part})),
        [
            {className: "workspace-metric-value-major", text: "30"},
            {className: "workspace-metric-value-minor", text: ".51"},
            {className: "workspace-metric-value-suffix", text: "%"},
        ],
    );
});

test("integer and nonnumeric values keep their exact text", () => {
    assert.deepEqual(
        Array.from(numericDisplay.getNumericDisplayParts("2,032"), part => ({...part})),
        [{className: "workspace-metric-value-major", text: "2,032"}],
    );
    assert.deepEqual(
        Array.from(numericDisplay.getNumericDisplayParts("No session"), part => ({...part})),
        [{className: "workspace-metric-value-major", text: "No session"}],
    );
});

test("blank values use the shared missing-value marker", () => {
    assert.equal(numericDisplay.parseNumericDisplayValue(" ").raw, "--");
});

test("renderer preserves one complete accessible value while hiding visual fragments", () => {
    class FakeElement {
        constructor() {
            this.attributes = {};
            this.children = [];
            this.className = "";
            this.dataset = {};
            this.textContent = "";
        }

        replaceChildren(fragment) {
            this.children = fragment.children;
        }

        setAttribute(name, value) {
            this.attributes[name] = value;
        }
    }

    const fakeDocument = {
        readyState: "loading",
        addEventListener() {},
        createDocumentFragment() {
            return {
                children: [],
                append(element) {
                    this.children.push(element);
                },
            };
        },
        createElement() {
            return new FakeElement();
        },
    };
    const domContext = {document: fakeDocument, Element: FakeElement};
    vm.runInNewContext(source, domContext);
    const element = new FakeElement();

    domContext.SHARED_NUMERIC_DISPLAY.renderNumericDisplayElement(element, "30.51%");

    assert.equal(element.attributes["aria-label"], "30.51%");
    assert.equal(element.children.map(child => child.textContent).join(""), "30.51%");
    assert.ok(element.children.every(child => child.attributes["aria-hidden"] === "true"));
});
