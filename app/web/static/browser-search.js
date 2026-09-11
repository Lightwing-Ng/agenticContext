/* Code version: v2.2.1-codex.1 */

import Fuse from "./vendor/fuse.min.mjs?v=fuse-js-v7.3.0";

(function initializeBrowserSearch() {
    "use strict";

    const searchRoot = document.querySelector("[data-browser-search]");
    const input = searchRoot?.querySelector("[data-browser-search-input]");
    const menu = searchRoot?.querySelector("[data-browser-search-menu]");
    const form = input
        ? document.getElementById(input.getAttribute("form") || "") || input.form
        : null;
    const dataNode = document.getElementById("browser_search_suggestion_data");
    if (!searchRoot || !input || !menu || !form) return;

    const storageKey = "cachelikes:browser-search-history:v1";
    const maximumHistory = 8;
    const maximumSuggestions = 8;
    const fuzzyScoreThreshold = 0.2;
    const fuseSearchThreshold = 0.6;
    let activeIndex = -1;
    let visibleCandidates = [];

    function normalizeDisplay(value) {
        return String(value || "").replace(/\s+/g, " ").trim().slice(0, 120);
    }

    function normalizeSearch(value) {
        return normalizeDisplay(value)
            .normalize("NFKC")
            .toLocaleLowerCase()
            .replace(/[^\p{L}\p{N}]+/gu, " ")
            .trim();
    }

    function parseServerCandidates() {
        if (!dataNode) return [];
        try {
            const parsed = JSON.parse(dataNode.textContent || "[]");
            if (!Array.isArray(parsed)) return [];
            return parsed
                .map((item) => ({
                    value: normalizeDisplay(item?.value),
                    detail: normalizeDisplay(item?.detail) || "Cached resource",
                    kind: "recommended",
                }))
                .filter((item) => item.value);
        } catch (_error) {
            return [];
        }
    }

    function readHistory() {
        try {
            const parsed = JSON.parse(window.localStorage.getItem(storageKey) || "[]");
            return Array.isArray(parsed)
                ? parsed.map(normalizeDisplay).filter(Boolean).slice(0, maximumHistory)
                : [];
        } catch (_error) {
            return [];
        }
    }

    function rememberSearch(value) {
        const normalized = normalizeDisplay(value);
        if (!normalized) return;
        const normalizedKey = normalizeSearch(normalized);
        const nextHistory = [
            normalized,
            ...readHistory().filter((item) => normalizeSearch(item) !== normalizedKey),
        ];
        try {
            window.localStorage.setItem(storageKey, JSON.stringify(nextHistory.slice(0, maximumHistory)));
        } catch (_error) {
        }
    }

    function collectCandidates() {
        const candidates = [
            ...readHistory().map((value) => ({ value, detail: "Recent search", kind: "history" })),
            ...parseServerCandidates(),
        ];
        document.querySelectorAll(
            ".browser-media-card-title, .browser-session-table-title, .browser-chat-message-title",
        ).forEach((node) => {
            const value = normalizeDisplay(node.textContent);
            if (value) candidates.push({ value, detail: "Cached resource", kind: "recommended" });
        });

        const seen = new Set();
        return candidates.filter((candidate) => {
            const key = normalizeSearch(candidate.value);
            if (!key || seen.has(key)) return false;
            seen.add(key);
            return true;
        });
    }

    function createSearchIndex(candidates) {
        return new Fuse(candidates, {
            ignoreDiacritics: true,
            ignoreLocation: true,
            includeMatches: true,
            includeScore: true,
            keys: [
                { name: "value", weight: 3 },
                { name: "detail", weight: 1 },
            ],
            minMatchCharLength: 1,
            threshold: fuseSearchThreshold,
            tokenMatch: "all",
            useTokenSearch: true,
        });
    }

    function findLiteralMatches(candidates, query) {
        const normalizedQuery = normalizeSearch(query);
        if (!normalizedQuery) return [];
        return candidates
            .filter((candidate) => (
                normalizeSearch(candidate.value).includes(normalizedQuery)
                || normalizeSearch(candidate.detail).includes(normalizedQuery)
            ))
            .map((candidate) => ({
                ...candidate,
                matches: [],
                score: 0,
            }));
    }

    function searchCandidates(query) {
        const candidates = collectCandidates();
        const queryText = normalizeDisplay(query);
        if (!queryText) {
            return candidates.slice(0, maximumSuggestions).map((item) => ({
                ...item,
                matches: [],
                score: 0,
            }));
        }

        const literalMatches = findLiteralMatches(candidates, queryText);
        const literalKeys = new Set(literalMatches.map((candidate) => normalizeSearch(candidate.value)));
        const fuzzyMatches = createSearchIndex(candidates)
            .search(queryText)
            .filter((result) => result.score == null || result.score <= fuzzyScoreThreshold)
            .map((result) => ({
                ...result.item,
                matches: result.matches || [],
                score: result.score,
            }));
        return [
            ...literalMatches,
            ...fuzzyMatches.filter((candidate) => !literalKeys.has(normalizeSearch(candidate.value))),
        ].slice(0, maximumSuggestions);
    }

    function appendHighlightedText(parent, text, matches, key) {
        const ranges = [];
        (matches || [])
            .filter((match) => match.key === key)
            .flatMap((match) => match.indices || [])
            .sort((left, right) => left[0] - right[0])
            .forEach(([start, end]) => {
                const previous = ranges[ranges.length - 1];
                if (previous && start <= previous[1] + 1) {
                    previous[1] = Math.max(previous[1], end);
                } else {
                    ranges.push([start, end]);
                }
            });

        let cursor = 0;
        ranges.forEach(([start, end]) => {
            const safeStart = Math.max(cursor, start);
            const safeEnd = Math.min(text.length - 1, end);
            if (safeStart > cursor) parent.append(document.createTextNode(text.slice(cursor, safeStart)));
            if (safeEnd >= safeStart) {
                const mark = document.createElement("mark");
                mark.className = "browser-search-match";
                mark.textContent = text.slice(safeStart, safeEnd + 1);
                parent.append(mark);
                cursor = safeEnd + 1;
            }
        });
        if (cursor < text.length) parent.append(document.createTextNode(text.slice(cursor)));
    }

    function setMenuOpen(isOpen) {
        menu.hidden = !isOpen;
        searchRoot.classList.toggle("is-browser-search-open", isOpen);
        input.setAttribute("aria-expanded", String(isOpen));
        if (!isOpen) {
            activeIndex = -1;
            input.removeAttribute("aria-activedescendant");
        }
    }

    function setActiveIndex(nextIndex) {
        if (!visibleCandidates.length) return;
        activeIndex = (nextIndex + visibleCandidates.length) % visibleCandidates.length;
        const options = Array.from(menu.querySelectorAll("[data-browser-search-option]"));
        options.forEach((option, index) => {
            const isActive = index === activeIndex;
            option.classList.toggle("is-active", isActive);
            option.setAttribute("aria-selected", String(isActive));
        });
        const activeOption = options[activeIndex];
        if (activeOption) {
            input.setAttribute("aria-activedescendant", activeOption.id);
            activeOption.scrollIntoView({ block: "nearest" });
        }
    }

    function applySearchScope() {
        if (input.dataset.browserSearchGlobalScope !== "true") return;
        for (const name of ["session", "session_page"]) {
            const field = form.querySelector(`input[name="${name}"]`);
            if (field) field.disabled = true;
        }
        if (input.dataset.browserSearchScope === "answerer") {
            const answererField = form.querySelector('[name="answerer"]');
            if (answererField) answererField.value = "";
            const viewField = form.querySelector('[name="session_view"]');
            if (viewField) viewField.value = "1";
            return;
        }
        const viewField = form.querySelector('[name="session_view"]');
        if (viewField) viewField.value = "0";
    }

    // Remove the obsolete shortcut from pages served by a warm template cache.
    document.querySelector("[data-browser-search-focus]")?.remove();
    searchRoot.querySelector("[data-browser-session-scope-remove]")?.addEventListener("click", () => {
        searchRoot.querySelector("[data-browser-session-tag]")?.remove();
        input.dataset.browserSearchGlobalScope = "true";
        input.dataset.browserSearchSubmitCopy = input.dataset.browserSearchGlobalSubmitCopy
            || "Press Enter to search all cached text.";
        input.focus();
        submitSearch();
    });

    function submitSearch() {
        applySearchScope();
        rememberSearch(input.value);
        setMenuOpen(false);
        form.requestSubmit();
    }

    function selectCandidate(candidate) {
        input.value = candidate.value;
        submitSearch();
    }

    function renderMenu(query) {
        const queryText = normalizeDisplay(query);
        const matches = searchCandidates(queryText);
        visibleCandidates = matches;
        activeIndex = -1;
        menu.replaceChildren();

        const heading = document.createElement("div");
        heading.className = "browser-search-suggestions-heading";
        heading.textContent = queryText ? "Search recommendations" : "Recent and recommended";
        menu.append(heading);

        if (!matches.length) {
            const emptyState = document.createElement("div");
            emptyState.className = "browser-search-suggestions-empty";
            emptyState.textContent = input.dataset.browserSearchSubmitCopy || "Press Enter to search this cache.";
            menu.append(emptyState);
        } else {
            matches.forEach((candidate, index) => {
                const option = document.createElement("button");
                option.type = "button";
                option.className = "browser-search-suggestion";
                option.id = `browser_search_option_${index}`;
                option.dataset.browserSearchOption = "true";
                option.setAttribute("role", "option");
                option.setAttribute("aria-selected", "false");

                const value = document.createElement("span");
                value.className = "browser-search-suggestion-value";
                appendHighlightedText(value, candidate.value, candidate.matches, "value");
                const detail = document.createElement("span");
                detail.className = "browser-search-suggestion-detail";
                detail.textContent = candidate.detail;
                option.append(value, detail);
                option.addEventListener("mousedown", (event) => event.preventDefault());
                option.addEventListener("click", () => selectCandidate(candidate));
                menu.append(option);
            });
        }
        setMenuOpen(true);
    }

    input.addEventListener("focus", () => renderMenu(input.value));
    input.addEventListener("click", () => renderMenu(input.value));
    input.addEventListener("input", () => renderMenu(input.value));
    input.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            if (menu.hidden) return;
            event.preventDefault();
            setMenuOpen(false);
            return;
        }
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            if (menu.hidden) renderMenu(input.value);
            if (!visibleCandidates.length) return;
            event.preventDefault();
            setActiveIndex(activeIndex + (event.key === "ArrowDown" ? 1 : -1));
            return;
        }
        if (event.key === "Enter") {
            event.preventDefault();
            if (!menu.hidden && activeIndex >= 0) {
                selectCandidate(visibleCandidates[activeIndex]);
                return;
            }
            submitSearch();
            return;
        }
        if (event.key === "Tab") setMenuOpen(false);
    });

    form.addEventListener("submit", () => {
        rememberSearch(input.value);
        setMenuOpen(false);
    });
    document.addEventListener("pointerdown", (event) => {
        if (!searchRoot.contains(event.target)) setMenuOpen(false);
    });
})();
