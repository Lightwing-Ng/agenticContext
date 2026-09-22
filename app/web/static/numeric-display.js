/* Code version: v1.1.0-codex.0 */

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

    function getCurrencyCodeFromNumericPrefix(prefix) {
        const normalizedPrefix = String(prefix ?? "")
            .replace(/^[+\-]?\*?/, "")
            .trim()
            .toUpperCase();
        if (normalizedPrefix === "$") return "USD";
        return /^[A-Z]{3}$/.test(normalizedPrefix) ? normalizedPrefix : "";
    }

    function getNumericDisplayPartsFromParsed(parsed, keepWholeValue = false) {
        if (!parsed.isNumeric) {
            return [{className: "workspace-metric-value-major", text: parsed.raw}];
        }
        if (keepWholeValue || !parsed.decimalPart) {
            return [{
                className: "workspace-metric-value-major",
                text: keepWholeValue
                    ? parsed.raw
                    : `${parsed.prefix}${parsed.integerPart}${parsed.suffix}`,
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

    function getNumericDisplayParts(value) {
        const parsed = parseNumericDisplayValue(value);
        const currencyCode = getCurrencyCodeFromNumericPrefix(parsed.prefix);
        return getNumericDisplayPartsFromParsed(
            parsed,
            getCurrencyMinorUnitDigits(currencyCode) === 0,
        );
    }

    const CURRENCY_CODE_ALIASES = Object.freeze({
        CNH: "CNY",
        RMB: "CNY",
    });

    function getCurrencyMinorUnitDigits(currencyCode) {
        const rawCode = String(currencyCode ?? "").trim().toUpperCase();
        const normalizedCode = CURRENCY_CODE_ALIASES[rawCode] ?? rawCode;
        if (!/^[A-Z]{3}$/.test(normalizedCode)) return null;
        try {
            return new Intl.NumberFormat("en-US", {
                style: "currency",
                currency: normalizedCode,
            }).resolvedOptions().maximumFractionDigits;
        } catch {
            return null;
        }
    }

    function getMonetaryDisplayParts(value, currencyCode) {
        const parsed = parseNumericDisplayValue(value);
        return getNumericDisplayPartsFromParsed(
            parsed,
            getCurrencyMinorUnitDigits(currencyCode) === 0,
        );
    }

    function renderNumericDisplayElement(element, value, currencyCode = "") {
        if (!(element instanceof Element)) return;
        const normalized = String(value ?? "").trim() || "--";
        const normalizedCurrencyCode = String(currencyCode ?? "").trim().toUpperCase();
        if (
            element.dataset.numericDisplayRendered === normalized
            && (element.dataset.numericDisplayCurrency ?? "") === normalizedCurrencyCode
        ) return;
        const fragment = document.createDocumentFragment();
        const parts = normalizedCurrencyCode
            ? getMonetaryDisplayParts(normalized, normalizedCurrencyCode)
            : getNumericDisplayParts(normalized);
        parts.forEach((part) => {
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
        element.dataset.numericDisplayCurrency = normalizedCurrencyCode;
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
                element.dataset.currencyCode ?? "",
            );
        });
        collectMatchingElements(root, "[data-numeric-display-cell]").forEach((element) => {
            if (element.dataset.numericDisplayRendered === "cell") return;
            const parsed = parseNumericDisplayValue(element.textContent);
            if (!parsed.isNumeric || !parsed.decimalPart) return;
            renderNumericDisplayElement(
                element,
                parsed.raw,
                element.dataset.currencyCode ?? "",
            );
            element.dataset.numericDisplayRendered = "cell";
        });
    }

    const api = Object.freeze({
        enhanceNumericDisplayElements,
        getCurrencyMinorUnitDigits,
        getMonetaryDisplayParts,
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
