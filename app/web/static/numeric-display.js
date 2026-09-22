/* Code version: v1.0.0-codex.0 */

(function bootstrapNumericDisplay(globalScope) {
    "use strict";

    const NUMERIC_DISPLAY_PATTERN = /^([+\-]?\*?(?:(?:[A-Z]{3}|\$)\s*)?)(\d[\d,]*)(?:\.(\d+))?(%?)$/;

    function parseNumericDisplayValue(value) {
        const raw = String(value ?? "").trim();
        const normalized = raw || "--";
        const match = normalized.match(NUMERIC_DISPLAY_PATTERN);
        if (!match) {
            return {
                raw: normalized,
                isNumeric: false,
                prefix: "",
                integerPart: "",
                decimalPart: "",
                suffix: "",
            };
        }
        const [, prefix, integerPart, decimalPart = "", suffix = ""] = match;
        return {raw: normalized, isNumeric: true, prefix, integerPart, decimalPart, suffix};
    }

    function getNumericDisplayParts(value) {
        const parsed = parseNumericDisplayValue(value);
        if (!parsed.isNumeric) {
            return [{className: "workspace-metric-value-major", text: parsed.raw}];
        }
        if (!parsed.decimalPart) {
            return [{
                className: "workspace-metric-value-major",
                text: `${parsed.prefix}${parsed.integerPart}${parsed.suffix}`,
            }];
        }
        return [
            {className: "workspace-metric-value-major", text: `${parsed.prefix}${parsed.integerPart}`},
            {className: "workspace-metric-value-minor", text: `.${parsed.decimalPart}`},
            ...(parsed.suffix
                ? [{className: "workspace-metric-value-suffix", text: parsed.suffix}]
                : []),
        ];
    }

    function renderNumericDisplayElement(element, value) {
        if (!(element instanceof Element)) return;
        const normalized = String(value ?? "").trim() || "--";
        if (element.dataset.numericDisplayRendered === normalized) return;
        const fragment = document.createDocumentFragment();
        getNumericDisplayParts(normalized).forEach((part) => {
            const span = document.createElement("span");
            span.className = part.className;
            span.textContent = part.text;
            span.setAttribute("aria-hidden", "true");
            fragment.append(span);
        });
        element.replaceChildren(fragment);
        element.setAttribute("aria-label", normalized);
        element.dataset.numericDisplayValue = normalized;
        element.dataset.numericDisplayRendered = normalized;
    }

    function collectMatchingElements(root, selector) {
        if (!root || typeof root.querySelectorAll !== "function") return [];
        const elements = [];
        if (typeof root.matches === "function" && root.matches(selector)) elements.push(root);
        elements.push(...root.querySelectorAll(selector));
        return elements;
    }

    function enhanceNumericDisplayElements(root = document) {
        collectMatchingElements(root, "[data-numeric-display-value]").forEach((element) => {
            renderNumericDisplayElement(
                element,
                element.dataset.numericDisplayValue ?? element.textContent ?? "",
            );
        });
        collectMatchingElements(root, "[data-numeric-display-cell]").forEach((element) => {
            if (element.dataset.numericDisplayRendered === "cell") return;
            const parsed = parseNumericDisplayValue(element.textContent);
            if (!parsed.isNumeric || !parsed.decimalPart) return;
            renderNumericDisplayElement(element, parsed.raw);
            element.dataset.numericDisplayRendered = "cell";
        });
    }

    const api = Object.freeze({
        enhanceNumericDisplayElements,
        getNumericDisplayParts,
        parseNumericDisplayValue,
        renderNumericDisplayElement,
    });
    globalScope.SHARED_NUMERIC_DISPLAY = api;

    if (typeof document !== "undefined") {
        const initialize = () => enhanceNumericDisplayElements(document);
        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", initialize, {once: true});
        } else {
            initialize();
        }
    }
}(typeof window !== "undefined" ? window : globalThis));
