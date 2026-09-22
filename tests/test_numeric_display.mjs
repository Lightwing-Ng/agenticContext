/* Code version: v1.1.0-codex.0 */

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

test("money display follows explicit minor-unit metadata", () => {
    assert.equal(numericDisplay.getCurrencyMinorUnitDigits("RMB"), 2);
    assert.equal(numericDisplay.getCurrencyMinorUnitDigits("USD"), 2);
    assert.equal(numericDisplay.getCurrencyMinorUnitDigits("JPY"), 0);
    assert.deepEqual(
        Array.from(
            numericDisplay.getMonetaryDisplayParts("RMB 5,440.00", "CNY"),
            part => ({...part}),
        ),
        [
            {className: "workspace-metric-value-major", text: "RMB 5,440"},
            {className: "workspace-metric-value-minor", text: ".00"},
        ],
    );
    assert.deepEqual(
        Array.from(
            numericDisplay.getMonetaryDisplayParts("JPY 5,440.00", "JPY"),
            part => ({...part}),
        ),
        [{className: "workspace-metric-value-major", text: "JPY 5,440.00"}],
    );
    assert.deepEqual(
        Array.from(numericDisplay.getNumericDisplayParts("JPY 5,440.00"), part => ({...part})),
        [{className: "workspace-metric-value-major", text: "JPY 5,440.00"}],
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

test("renderer changes currency mode without losing the accessible value", () => {
    class FakeElement {
        constructor() {
            this.attributes = {};
            this.children = [];
            this.dataset = {};
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
                append(element) { this.children.push(element); },
            };
        },
        createElement() {
            const element = new FakeElement();
            element.setAttribute = FakeElement.prototype.setAttribute;
            return element;
        },
    };
    const domContext = {document: fakeDocument, Element: FakeElement};
    vm.runInNewContext(source, domContext);
    const element = new FakeElement();

    domContext.SHARED_NUMERIC_DISPLAY.renderNumericDisplayElement(
        element,
        "JPY 5,440.00",
        "JPY",
    );

    assert.equal(element.attributes["aria-label"], "JPY 5,440.00");
    assert.equal(element.children.length, 1);
    assert.equal(element.children[0].textContent, "JPY 5,440.00");
});
