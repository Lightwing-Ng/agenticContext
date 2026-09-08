/* Code version: v0.1.0-codex.1 */

const root = document.querySelector("[data-beta-root]");
if (root) initializeBeta(root);

function initializeBeta(root) {
    const id = root.dataset.betaExperiment;
    const form = root.querySelector("[data-beta-form]");
    const status = root.querySelector("[data-beta-status]");
    const run = root.querySelector("[data-beta-run]");
    const resultPanel = root.querySelector("[data-beta-result]");
    const fileInput = root.querySelector("[data-beta-file]");
    const storageKey = `agenticcontext:beta:v1:draft:${id}`;
    const fields = ["source", "second", "objective", "budget"];
    const fieldLimits = { source: 60000, second: 60000, objective: 4000, budget: 32 };
    // One UTF-16 code unit can occupy six JSON characters after escaping.
    const serializedDraftLimit = Object.values(fieldLimits).reduce((total, limit) => total + limit, 0) * 6 + 256;
    let output = null;
    let revision = 0;
    let saveTimer = 0;
    let isBusy = false;

    const examples = {
        "idea-collision": {
            objective: "Make a personal knowledge collection spark new projects",
            source: "Saved conversations contain ideas that never became projects.\nPeople revisit the same questions across several AI providers.",
            second: "A museum changes its exhibition by pairing objects from unrelated periods.\nA small placard explains a surprising connection and asks a question.",
        },
        "context-capsule": {
            objective: "Design a reversible experiment using cached conversations",
            source: "Goal: turn saved conversations into a useful weekly research habit.\nConstraint: preserve original cached messages.\nObservation: interesting questions recur across providers.\nEvidence: users already save prompts for reuse.\nOpen question: which question is worth one hour of investigation?\nUnrelated note: replace a desk lamp.\nAcceptance: produce an editable brief with source references.\nUnknown: whether resurfacing old questions is useful.",
            budget: "1000",
        },
        "question-radar": {
            objective: "Find a small project about context reuse",
            source: "We have a large collection of cached conversations.\nHow can old context reveal a new project?\nTODO: compare notes from two different providers.\nWhich recurring question still lacks evidence?\nThe current cache should remain untouched.\nUnknown: whether users prefer a daily or weekly review.\n有哪些被反复提及、但从未验证的问题？",
        },
        "memory-diff": {
            objective: "Review a changing experiment brief",
            source: "Goal: create a research brief.\nBudget: 4 hours\nMode: automatic publishing\nEvidence: one source\nKeep the original cache unchanged.",
            second: "Goal: create a research brief.\nBudget: 1 hour\nMode: manual review\nEvidence: three sources\nKeep the original cache unchanged.\nQuestion: can we test this with a single user?",
        },
        "decision-wind-tunnel": {
            objective: "Choose a useful first experiment",
            source: "Create a weekly brief that connects saved conversations and suggests one small project. Start with copied excerpts and a manually reviewed draft.",
            second: "Older conversations still contain relevant questions.\nA weekly brief is frequent enough.\nOne useful connection is more valuable than a long summary.",
        },
        "mission-forge": {
            objective: "Build a small evidence-backed idea brief",
            source: "Work from three copied conversation excerpts. Preserve source references. Keep the first prototype local and reversible. Use existing UI components.",
            second: "Every proposal cites a source excerpt.\nAt least one assumption has a concrete verification step.\nA user can export and edit the brief.\nExisting Agent and Cache workflows remain unchanged.",
        },
    };

    function announce(message, state = "") {
        status.textContent = message;
        status.dataset.state = state;
    }

    function values() {
        return Object.fromEntries(fields.map((name) => [name, (form.elements.namedItem(name)?.value || "").slice(0, fieldLimits[name])]));
    }

    function writeDraft() {
        try {
            const draft = values();
            if (![draft.source, draft.second, draft.objective].some((value) => value.trim())) {
                window.sessionStorage.removeItem(storageKey);
                return;
            }
            window.sessionStorage.setItem(storageKey, JSON.stringify(draft));
            root.querySelector("[data-beta-draft-status]").textContent = "Draft kept in this tab";
        } catch (_error) {
            root.querySelector("[data-beta-draft-status]").textContent = "Tab storage unavailable; export to keep your work";
        }
    }

    function invalidate() {
        revision += 1;
        output = null;
        resultPanel.hidden = true;
        announce("");
        window.clearTimeout(saveTimer);
        saveTimer = window.setTimeout(writeDraft, 200);
    }

    function setValues(next) {
        fields.forEach((name) => {
            const field = form.elements.namedItem(name);
            if (field && typeof next[name] === "string") field.value = next[name].slice(0, fieldLimits[name]);
        });
    }

    function renderResult(result) {
        root.querySelector("[data-beta-result-summary]").textContent = result.summary;
        const metrics = root.querySelector("[data-beta-metrics]");
        metrics.replaceChildren(...result.metrics.map((metric) => {
            const pair = document.createElement("div");
            const term = document.createElement("dt");
            const value = document.createElement("dd");
            term.textContent = metric.label;
            value.textContent = typeof metric.value === "number" ? metric.value.toLocaleString("en-US") : String(metric.value);
            pair.append(term, value);
            return pair;
        }));
        root.querySelector("[data-beta-sections]").replaceChildren(...result.sections.map((section) => {
            const card = document.createElement("section");
            const heading = document.createElement("h4");
            const body = document.createElement("pre");
            heading.textContent = section.title;
            body.textContent = section.body;
            card.append(heading, body);
            return card;
        }));
        output = result;
        resultPanel.hidden = false;
    }

    try {
        const saved = window.sessionStorage.getItem(storageKey);
        if (saved && saved.length <= serializedDraftLimit) {
            const parsed = JSON.parse(saved);
            if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
                setValues(parsed);
                root.querySelector("[data-beta-draft-status]").textContent = "Draft restored in this tab";
            }
        }
    } catch (_error) {
        root.querySelector("[data-beta-draft-status]").textContent = "Start a fresh draft or import a file";
    }

    form.addEventListener("input", invalidate);
    window.addEventListener("pagehide", () => { window.clearTimeout(saveTimer); writeDraft(); });
    root.querySelector("[data-beta-example]").addEventListener("click", () => {
        form.reset();
        setValues(examples[id] || {});
        invalidate();
        writeDraft();
        announce("Example loaded. Edit the material, then run the experiment.");
    });
    root.querySelector("[data-beta-clear]").addEventListener("click", () => {
        revision += 1;
        window.clearTimeout(saveTimer);
        form.reset();
        output = null;
        resultPanel.hidden = true;
        try { window.sessionStorage.removeItem(storageKey); } catch (_error) { /* An in-memory clear remains available. */ }
        root.querySelector("[data-beta-draft-status]").textContent = "Only this experiment";
        announce("This experiment's draft is cleared.");
    });

    root.querySelector("[data-beta-import]").addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", async () => {
        const file = fileInput.files?.[0];
        if (!file) return;
        const importRevision = ++revision;
        fileInput.value = "";
        if (!/\.(txt|md|json)$/i.test(file.name) || file.size > 240000) {
            announce("Choose a .txt, .md, or .json file no larger than 240 KB.", "error");
            return;
        }
        announce("Reading the selected file…");
        try {
            const text = await file.text();
            if (importRevision !== revision) return;
            if (text.length > 60000) throw new Error("The file exceeds 60,000 characters. Import a smaller excerpt.");
            if (text.includes("\u0000")) throw new Error("This file contains binary data. Choose a text export.");
            form.elements.namedItem("source").value = text;
            invalidate();
            writeDraft();
            announce(`Imported ${file.name} locally. Review the material before running.`, "success");
        } catch (error) {
            if (importRevision === revision) announce(error.message || "The file could not be read.", "error");
        }
    });

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (isBusy || !form.reportValidity()) return;
        isBusy = true;
        const runRevision = revision;
        const input = values();
        run.disabled = true;
        run.classList.add("is-pending");
        run.textContent = "Working locally…";
        form.setAttribute("aria-busy", "true");
        announce("Preparing this experiment…");
        try {
            const { runExperiment } = await import(`./engines.mjs?v=${encodeURIComponent(root.dataset.betaVersion)}`);
            if (runRevision !== revision) return;
            const result = runExperiment(id, input);
            if (runRevision !== revision) return;
            renderResult(result);
            writeDraft();
            announce("Output ready. Review it before using it in another task.", "success");
            resultPanel.focus({ preventScroll: true });
            resultPanel.scrollIntoView({ behavior: "instant", block: "nearest" });
        } catch (error) {
            if (runRevision === revision) announce(error.message || "This experiment could not run. Your draft is still available.", "error");
        } finally {
            isBusy = false;
            run.disabled = false;
            run.classList.remove("is-pending");
            run.textContent = run.dataset.idleLabel;
            form.removeAttribute("aria-busy");
        }
    });

    root.querySelector("[data-beta-copy]").addEventListener("click", async () => {
        if (!output) return;
        const copiedRevision = revision;
        try {
            await navigator.clipboard.writeText(output.markdown);
            if (copiedRevision === revision) announce("Copied the complete experiment output.", "success");
        } catch (_error) {
            if (copiedRevision === revision) announce("Clipboard access is unavailable. Use Export .md to keep the result.", "error");
        }
    });
    root.querySelector("[data-beta-export]").addEventListener("click", () => {
        if (!output) return;
        const url = URL.createObjectURL(new Blob([output.markdown], { type: "text/markdown;charset=utf-8" }));
        const link = document.createElement("a");
        link.href = url;
        link.download = `beta-${id}.md`;
        document.body.append(link);
        link.click();
        link.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 1000);
        announce("Markdown export prepared.");
    });
    // Native submission stays disabled unless the local-only handler initialized completely.
    run.disabled = false;
}
