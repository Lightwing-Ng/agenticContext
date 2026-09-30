/* Code version: v0.2.0-codex.0 */
/* Pure, deterministic recipes: this module has no browser, storage, or network access. */

const TEXT_LIMIT = 60000;

function inputText(fields, key, required = false) {
    const value = fields[key] ?? '';
    if (typeof value !== 'string') throw new TypeError(`${key} must be text.`);
    if (value.length > TEXT_LIMIT) throw new RangeError(`${key} exceeds 60,000 characters.`);
    if (required && !value.trim()) throw new Error(`${key} is required.`);
    return value;
}

function prefix(text, limit) {
    let end = Math.max(0, Math.min(text.length, limit));
    if (end < text.length && /[\uD800-\uDBFF]/.test(text[end - 1] ?? '')) end -= 1;
    return text.slice(0, end);
}

function lines(text) {
    if (!text) return [];
    return text.split(/\r\n|\n|\r/).map((text, index) => ({ text, number: index + 1 }));
}

function normalized(text) {
    return text.normalize('NFKC').toLowerCase().replace(/\s+/gu, ' ').trim();
}

function keywords(objective) {
    const terms = new Set();
    for (const token of normalized(objective).match(/[\p{L}\p{N}]+/gu) ?? []) {
        if (terms.size >= 128) break;
        terms.add(token);
        for (const run of token.match(/\p{Script=Han}+/gu) ?? []) {
            const characters = Array.from(run);
            for (let index = 0; index + 1 < characters.length && terms.size < 128; index += 1) {
                terms.add(characters[index] + characters[index + 1]);
            }
        }
    }
    return [...terms];
}

function rank(sourceLines, objective) {
    const terms = keywords(objective);
    return sourceLines.map((line) => {
        const value = normalized(line.text);
        return { ...line, score: terms.reduce((score, term) => score + Number(value.includes(term)), 0) };
    }).sort((left, right) => right.score - left.score || left.number - right.number);
}

function quotedLine(line, label = 'Source', limit = 360) {
    const excerpt = prefix(line.text, limit);
    const extent = excerpt.length < line.text.length ? `; prefix excerpt, ${excerpt.length}/${line.text.length} characters` : '';
    return `[${label} L${line.number}${extent}]\n${excerpt}`;
}

function fragments(source, objective) {
    return rank(lines(source).filter((line) => line.text.trim()), objective);
}

function fenced(text) {
    const runs = text.match(/`+/g) ?? [];
    const fence = '`'.repeat(Math.max(3, ...runs.map((run) => run.length + 1)));
    return `${fence}text\n${text}\n${fence}`;
}

function result(title, summary, sections, metrics = []) {
    return {
        title,
        summary,
        sections,
        markdown: [`# ${title}`, summary, ...sections.map((section) => `## ${section.title}\n\n${fenced(section.body)}`)].join('\n\n'),
        metrics,
    };
}

function objectiveText(objective) {
    if (!objective.trim()) return 'Explore a small, reversible experiment using the supplied fragments.';
    const excerpt = prefix(objective, 320);
    return `${excerpt}${excerpt.length < objective.length ? '\n[Objective excerpt; remaining text omitted.]' : ''}`;
}

function ideaCollision({ source, second, objective }) {
    if (!second.trim()) throw new Error('second is required for Idea collision.');
    const firstLines = fragments(source, objective);
    const secondLines = fragments(second, objective);
    const lenses = [
        {
            title: '1. Transfer a mechanism',
            recipe: 'Identify one concrete mechanism in A and try it against a use case in B. Name the mechanism yourself; the recipe does not infer it.',
            probe: 'Sketch one input, one transformation, and one observable output. Compare the sketch with the current approach on one supplied example.',
            evidence: 'Record the original example, both outputs, and a measurable difference. Reject the transfer if it adds work without a useful difference.',
        },
        {
            title: '2. Reverse a constraint',
            recipe: 'Choose a stated constraint in either fragment. Temporarily reverse it on paper and use the other fragment as a source of alternative constraints.',
            probe: 'Draw the current and reversed workflow for one case. Mark where the reversal fails before building anything.',
            evidence: 'Name the exact constraint, the changed step, and an example that would disprove the proposed advantage.',
        },
        {
            title: '3. Build the smallest bridge',
            recipe: 'Propose one explicit input/output contract between A and B. Treat compatibility as an open question.',
            probe: 'Create a disposable fixture and manually pass one example through the proposed contract. Keep the original inputs intact.',
            evidence: 'Save the fixture, expected output, actual output, and one deliberately incompatible input. Continue only if the bridge handles the stated case.',
        },
    ];
    return result('Idea collision', 'Generated experiment recipes, grounded in exact source excerpts. Lexical selection does not establish a semantic connection.', lenses.map((lens, index) => ({
        title: lens.title,
        body: [
            `Objective\n${objectiveText(objective)}`,
            quotedLine(firstLines[index % firstLines.length], 'A'),
            quotedLine(secondLines[index % secondLines.length], 'B'),
            `Generated recipe\n${lens.recipe}`,
            `Smallest probe\n${lens.probe}`,
            `Evidence and stop rule\n${lens.evidence}`,
        ].join('\n\n'),
    })), [{ label: 'Recipes', value: 3 }, { label: 'Source fragments', value: 2 }]);
}

function contextCapsule({ source, objective, budget }) {
    const parsed = budget === '' || budget === undefined ? 4000 : Number(budget);
    if (!Number.isFinite(parsed)) throw new Error('budget must be a finite number.');
    const limit = Math.max(500, Math.min(20000, Math.trunc(parsed)));
    const title = 'Context capsule';
    const summary = 'Extractive heuristic: objective keywords rank original lines; ties keep source order. No generated summary.';
    const footer = '[All source text outside the quoted excerpts is omitted.]';
    const ranked = fragments(source, objective);
    const selected = [];
    let partial = 0;
    const build = (body) => result(title, summary, [{ title: 'Source excerpts with provenance', body }]);
    // The export itself is measured so headings, fences, provenance, and notices consume the budget.
    for (const line of ranked) {
        const whole = `[L${line.number}]\n${line.text}`;
        const body = [...selected, whole, footer].join('\n\n');
        if (build(body).markdown.length <= limit) {
            selected.push(whole);
            continue;
        }
        const label = `[L${line.number}; prefix excerpt]\n`;
        const empty = [...selected, label, footer].join('\n\n');
        const remaining = limit - build(empty).markdown.length;
        if (remaining < 16) break;
        // A source containing long backtick runs can require a longer enclosing Markdown fence.
        let low = 0;
        let high = Math.min(remaining, line.text.length);
        while (low < high) {
            const middle = Math.ceil((low + high) / 2);
            const candidate = [...selected, label + prefix(line.text, middle), footer].join('\n\n');
            if (build(candidate).markdown.length <= limit) low = middle;
            else high = middle - 1;
        }
        if (low > 0) {
            selected.push(label + prefix(line.text, low));
            partial += 1;
        }
        break;
    }
    const output = build([...selected, footer].join('\n\n'));
    output.metrics = [
        { label: 'Export characters', value: `${output.markdown.length} / ${limit}` },
        { label: 'Selected lines', value: `${selected.length} / ${ranked.length}` },
        { label: 'Partial excerpts', value: partial },
    ];
    return output;
}

function questionRadar({ source, objective }) {
    const grouped = new Map();
    const openItem = /\b(?:todo|tbd|fixme|unknown|blocker|blocked|unresolved|unanswered|uncertain)\b|待办|待确认|未知|阻塞|待定|不清楚/iu;
    const question = /[?？]|(?:^|[。！!；;])\s*(?:如何|为何|为什么|是否|何时|哪里|谁)|[吗呢么]\s*[。！!]?$/u;
    for (const line of lines(source)) {
        if (!question.test(line.text) && !openItem.test(line.text)) continue;
        const key = normalized(line.text);
        const existing = grouped.get(key);
        if (existing) existing.references.push(line.number);
        else grouped.set(key, { ...line, references: [line.number] });
    }
    const ranked = rank([...grouped.values()], objective);
    const displayed = ranked.slice(0, 30);
    const body = displayed.length ? displayed.map((line) => {
        const refs = line.references.slice(0, 12).map((number) => `L${number}`).join(', ');
        const more = line.references.length > 12 ? `; ${line.references.length - 12} additional matching lines` : '';
        return `${quotedLine(line, 'Source', 500)}\n[Occurrences: ${refs}${more}; objective keyword matches: ${line.score}]`;
    }).join('\n\n') : 'No explicit question or open-item marker matched. This does not establish that the source has no open questions.';
    return result('Question radar', 'Exact source lines selected by question punctuation, Chinese question forms, and open-item markers. Keyword ranking is heuristic, not semantic certainty.', [
        { title: 'Questions and open items', body },
        { title: 'Selection boundary', body: `${ranked.length} distinct matching lines; ${displayed.length} shown. Duplicates use normalized case and whitespace; the first original line is quoted. Unmatched source is omitted.` },
    ], [{ label: 'Distinct open items', value: ranked.length }, { label: 'Source occurrences', value: [...grouped.values()].reduce((count, line) => count + line.references.length, 0) }]);
}

function buckets(sourceLines) {
    const map = new Map();
    for (const line of sourceLines) {
        if (!map.has(line.text)) map.set(line.text, []);
        map.get(line.text).push(line);
    }
    return map;
}

function labelledValues(sourceLines) {
    const values = new Map();
    for (const line of sourceLines) {
        const match = line.text.match(/^\s*(?:[-*]\s+)?([^:=：]{1,80}?)\s*[:=：]\s*(.*?)\s*$/u);
        if (!match) continue;
        const key = normalized(match[1]);
        if (!values.has(key)) values.set(key, []);
        values.get(key).push({ ...line, label: match[1], value: match[2] });
    }
    return values;
}

function memoryDiff({ source, second, objective }) {
    const before = lines(source);
    const after = lines(second);
    const beforeBuckets = buckets(before);
    const afterBuckets = buckets(after);
    const removed = [];
    const added = [];
    let unchanged = 0;
    for (const [text, entries] of beforeBuckets) {
        const matches = Math.min(entries.length, afterBuckets.get(text)?.length ?? 0);
        unchanged += matches;
        removed.push(...entries.slice(matches));
    }
    for (const [text, entries] of afterBuckets) {
        added.push(...entries.slice(beforeBuckets.get(text)?.length ?? 0));
    }
    const beforeValues = labelledValues(before);
    const afterValues = labelledValues(after);
    const changed = [];
    for (const [key, entries] of beforeValues) {
        const current = afterValues.get(key);
        if (!current) continue;
        const priorSet = [...new Set(entries.map((entry) => entry.value))].sort();
        const currentSet = [...new Set(current.map((entry) => entry.value))].sort();
        if (JSON.stringify(priorSet) === JSON.stringify(currentSet)) continue;
        changed.push({ ...entries[0], before: entries, after: current });
    }
    const list = (entries, label) => {
        const shown = rank(entries, objective).slice(0, 20);
        return shown.length ? `${shown.map((line) => quotedLine(line, label)).join('\n\n')}\n\n[${shown.length}/${entries.length} lines shown.]` : 'None.';
    };
    const candidates = rank(changed, objective).slice(0, 12).map((entry) => [
        `Shared label: ${entry.label}`,
        ...entry.before.slice(0, 2).map((line) => quotedLine(line, 'Before')),
        ...entry.after.slice(0, 2).map((line) => quotedLine(line, 'After')),
        `[Before occurrences: ${entry.before.length}; after occurrences: ${entry.after.length}. At most two of each shown.]`,
    ].join('\n\n'));
    return result('Memory diff', 'Exact line inventory comparison. Repeated lines count by occurrence; reordering alone is not a content change. Shared-label value changes are review candidates, not proven contradictions.', [
        { title: 'Added lines', body: list(added, 'After') },
        { title: 'Removed lines', body: list(removed, 'Before') },
        { title: 'Changed values under shared labels', body: candidates.length ? `${candidates.join('\n\n')}\n\n[${candidates.length}/${changed.length} candidate labels shown.]` : 'No shared-label value changes matched the colon or equals-sign heuristic.' },
    ], [{ label: 'Added', value: added.length }, { label: 'Removed', value: removed.length }, { label: 'Unchanged', value: unchanged }, { label: 'Review candidates', value: changed.length }]);
}

function decisionWindTunnel({ source, second, objective }) {
    const proposal = fragments(source, objective)[0];
    const assumptions = fragments(second, objective);
    const branches = [
        { title: '1. The assumption is false', condition: 'Treat this assumption as false for one concrete case.', probe: 'Find one supplied case that could violate the assumption. Walk through the proposal manually with that case.', evidence: 'Record the case, the failed step, and the observation that would distinguish true from false.' },
        { title: '2. The constraint tightens', condition: 'Halve the available time, budget, or capacity relevant to this assumption. Select and state one dimension yourself.', probe: 'Run a paper exercise or disposable fixture with that single tighter constraint.', evidence: 'Record the baseline limit, test limit, stop threshold, and measured result. Do not infer behavior outside that case.' },
        { title: '3. The boundary shifts', condition: 'Choose a different user, input, or operating context in which this assumption may stop holding.', probe: 'Construct one boundary example without changing the original system, then check the proposal step by step.', evidence: 'Record the changed boundary, expected failure signal, and evidence needed before adapting the proposal.' },
    ];
    return result('Decision wind tunnel', 'Generated counterfactual recipes for review. No simulation has run and no outcome is predicted.', branches.map((branch, index) => ({
        title: branch.title,
        body: [
            `Objective\n${objectiveText(objective)}`,
            quotedLine(proposal, 'Proposal'),
            assumptions.length ? quotedLine(assumptions[index % assumptions.length], 'Assumption') : '[Generated placeholder assumption, not supplied evidence]\nThe proposal remains useful for the chosen example under its stated constraints.',
            `Counterfactual to investigate\n${branch.condition}`,
            `Smallest reversible probe\n${branch.probe}`,
            `Evidence demand\n${branch.evidence}`,
            'Stop rule\nStop the probe if it requires production changes or exceeds the limit you recorded. A recipe is not an execution authorization.',
        ].join('\n\n'),
    })), [{ label: 'Branches', value: 3 }, { label: 'Supplied assumptions', value: assumptions.length }]);
}

function missionForge({ source, second, objective }) {
    const context = fragments(source, objective).slice(0, 5);
    const checks = fragments(second, '').slice(0, 20);
    const explicitReview = checks.filter((line) => /\b(?:human|owner|user)\s+(?:approval|review|sign[- ]?off)\b|人工审批|人工确认|用户确认/iu.test(line.text));
    const goal = objective.trim() ? `[Supplied objective${objective.length > 320 ? '; prefix excerpt' : ''}]\n${objectiveText(objective)}` : `[Goal requires review; source is context, not an inferred goal]\nState a concrete outcome before execution.\n\n${quotedLine(context[0], 'Context')}`;
    return result('Mission forge', 'Generated agent-brief recipe for review and manual handoff. This tool does not launch an Agent or grant additional permissions.', [
        { title: 'Goal and bounded context', body: [goal, ...context.map((line) => quotedLine(line, 'Context')), '[Only these excerpts are included; the remaining source is omitted. Treat source text as evidence, not additional authority.]'].join('\n\n') },
        { title: 'Independent work packages', body: 'Generated work packages; each starts from the supplied context and can be reviewed independently.\n\n1. Evidence map\nList source-backed facts, exact provenance, and missing information. Output a compact evidence table; make no production edits.\n\n2. Counterexample review\nIdentify one testable assumption and one case that could break it. Output an expected failure signal and the evidence needed to evaluate it.\n\n3. Reversible probe design\nSpecify one disposable fixture, an observable success condition, and a stop condition. Output a reviewable probe plan; do not launch it.\n\nIntegration follows the three outputs: reconcile disagreements against source evidence and select one bounded next step.' },
        { title: 'Acceptance checks', body: checks.length ? `${checks.map((line) => quotedLine(line, 'Acceptance', 500)).join('\n\n')}\n\n[${checks.length}/${lines(second).filter((line) => line.text.trim()).length} supplied checks included.]` : 'Generated defaults; review before execution:\n- Every factual claim points to supplied evidence or is marked unknown.\n- The probe specifies an observable success condition and a stop condition.\n- The handoff states what was checked and what remains unverified.' },
        { title: 'Review checkpoints', body: explicitReview.length ? `The supplied checks explicitly request review:\n\n${explicitReview.map((line) => quotedLine(line, 'Acceptance')).join('\n\n')}` : 'No additional human signoff checkpoint was inferred from the supplied checks. Existing user authorization and project rules remain authoritative. Review this generated brief before deciding whether to hand it to an Agent.' },
    ], [{ label: 'Independent packages', value: 3 }, { label: 'Supplied checks', value: lines(second).filter((line) => line.text.trim()).length }]);
}

const MOTIF_STOPWORDS = new Set([
    'about', 'after', 'again', 'also', 'and', 'are', 'because', 'been', 'before', 'being',
    'but', 'can', 'could', 'does', 'each', 'for', 'from', 'had', 'has', 'have', 'how',
    'into', 'its', 'just', 'may', 'more', 'most', 'not', 'only', 'other', 'our', 'out',
    'over', 'same', 'should', 'some', 'than', 'that', 'the', 'their', 'them', 'then',
    'there', 'these', 'they', 'this', 'those', 'through', 'too', 'under', 'use', 'was',
    'were', 'what', 'when', 'where', 'which', 'while', 'who', 'will', 'with', 'would',
    'you', 'your', '一个', '一些', '以及', '但是', '可以', '因为', '如何', '我们', '所以',
    '是否', '没有', '这个', '这是', '这些', '那个', '那么', '什么', '进行', '需要',
]);

function lexicalTerms(text) {
    const value = normalized(text);
    const terms = new Set();
    // The 60,000-character input limit bounds analysis; very long Latin runs are not motifs.
    for (const word of value.match(/[a-z]+(?:['’][a-z]+)*/gu) ?? []) {
        if (word.length >= 3 && word.length <= 32 && !MOTIF_STOPWORDS.has(word)) terms.add(word);
    }
    for (const run of value.match(/\p{Script=Han}+/gu) ?? []) {
        const characters = Array.from(run);
        for (let index = 0; index + 1 < characters.length; index += 1) {
            const pair = characters[index] + characters[index + 1];
            if (!MOTIF_STOPWORDS.has(pair)) terms.add(pair);
        }
    }
    return terms;
}

function distinctMaterial(source) {
    const grouped = new Map();
    for (const line of lines(source)) {
        const key = normalized(line.text);
        if (!key) continue;
        const existing = grouped.get(key);
        if (existing) existing.references.push(line.number);
        else grouped.set(key, { ...line, references: [line.number], terms: lexicalTerms(line.text) });
    }
    return [...grouped.values()];
}

function countText(value) {
    return value.toLocaleString('en-US');
}

function materialExcerpt(line, limit = 300) {
    const text = prefix(line.text, limit);
    const extent = text.length < line.text.length ? `; prefix excerpt, ${countText(text.length)}/${countText(line.text.length)} characters` : '';
    const excerpt = `[Source L${line.number}${extent}]\n${text}`;
    if (line.references.length === 1) return excerpt;
    const shown = line.references.slice(0, 8).map((number) => `L${number}`).join(', ');
    const omitted = line.references.length > 8 ? `; ${countText(line.references.length - 8)} more` : '';
    return `${excerpt}\n[Equivalent occurrences: ${shown}${omitted}; counted as one distinct line.]`;
}

function echoAtlas({ source, objective }) {
    const material = distinctMaterial(source);
    const objectiveTerms = lexicalTerms(objective);
    const motifs = new Map();
    for (const line of material) {
        for (const term of line.terms) {
            if (!motifs.has(term)) motifs.set(term, []);
            motifs.get(term).push(line);
        }
    }
    const recurring = [...motifs].filter(([, entries]) => entries.length >= 2).sort((left, right) => (
        right[1].length - left[1].length
        || Number(objectiveTerms.has(right[0])) - Number(objectiveTerms.has(left[0]))
        || left[1][0].number - right[1][0].number
        || (left[0] < right[0] ? -1 : left[0] > right[0] ? 1 : 0)
    ));
    const shown = recurring.slice(0, 8);
    const sections = shown.map(([term, entries]) => ({
        title: `Echo: ${term}`,
        body: [
            `${countText(entries.length)} distinct source lines contain this lexical motif. ${countText(entries.slice(0, 4).length)} line excerpts shown.`,
            ...entries.slice(0, 4).map((line) => materialExcerpt(line, 240)),
            'Small observation\nCompare the quoted contexts. Write one way this exact wording is used similarly and one way it differs. Keep the connection unconfirmed if the source does not support it.',
        ].join('\n\n'),
    }));
    if (!shown.length) sections.push({
        title: 'No recurring lexical motifs',
        body: material.length < 2
            ? 'At least two distinct nonempty source lines are needed. Add another excerpt to compare; repeating the same line does not create an echo.'
            : 'No eligible English word or Chinese bigram occurs in two distinct source lines. A missing lexical match does not mean the excerpts have no meaningful connection.',
    });
    sections.push({
        title: 'How to read this atlas',
        body: `${countText(material.length)} distinct nonempty lines examined. ${countText(recurring.length)} recurring motifs found; ${countText(shown.length)} shown. Counts use full source lines, while displayed excerpts may be prefixes. Repeated words within a line count once. Lines equivalent after Unicode normalization, lowercasing, and whitespace normalization count once; the first original line supplies the excerpt.\n\nEligible motifs are English words of 3–32 characters and adjacent Chinese character pairs, with a small stopword list removed. Ranking uses distinct-line count, then exact objective-term match, then first source occurrence, then Unicode string order. This is lexical recurrence, not semantic clustering, importance, chronology, or a profile of the person who supplied the text.`,
    });
    const output = result('Echo Atlas', 'Find recurring wording across your supplied excerpts, then inspect the original contexts. A repeated word is a starting point for curiosity, not a proven connection.', sections, [
        { label: 'Distinct lines', value: material.length },
        { label: 'Recurring motifs', value: recurring.length },
        { label: 'Motifs shown', value: shown.length },
    ]);
    output.signals = shown.map(([label, entries]) => ({ label, value: entries.length }));
    return output;
}

function lexicalOverlap(left, right) {
    let shared = 0;
    for (const term of left) if (right.has(term)) shared += 1;
    const total = left.size + right.size - shared;
    return total ? shared / total : 1;
}

function curiosityTrail({ source, objective }) {
    const material = distinctMaterial(source);
    const anchor = rank(material, objective)[0];
    const selected = [anchor];
    // Each next stop minimizes overlap with earlier stops, then maximizes source-line distance.
    while (selected.length < 3 && selected.length < material.length) {
        const remaining = material.filter((line) => !selected.some((prior) => prior.number === line.number));
        const scored = remaining.map((line) => ({
            line,
            overlap: Math.max(...selected.map((prior) => lexicalOverlap(line.terms, prior.terms))),
            distance: Math.min(...selected.map((prior) => Math.abs(line.number - prior.number))),
        }));
        scored.sort((left, right) => left.overlap - right.overlap || right.distance - left.distance || left.line.number - right.line.number);
        selected.push(scored[0].line);
    }
    const stops = [
        {
            title: '1. Notice one detail',
            prompt: 'Choose one concrete noun, action, or stated condition in this excerpt. Spend two minutes listing what the line directly says and one detail it leaves unknown.',
            check: 'Write one question answerable by a small observation. Reject any answer you cannot point to in the excerpt or a new observation; leave it unknown.',
        },
        {
            title: '2. Try a different context',
            prompt: 'Place this excerpt beside Stop 1. Name one visible difference in wording or stated conditions, then ask whether one explicitly stated detail transfers between them.',
            check: 'Record one example in each context. Abandon the proposed connection if the same detail has a different role, or if either excerpt supplies no evidence for it.',
        },
        {
            title: '3. Follow a small question',
            prompt: 'Use this further excerpt to turn the earlier question into a five-minute observation: name one thing to inspect, one expected observation, and one possible surprise.',
            check: 'Before observing, write a result that would contradict your expectation. Stop after five minutes; record what happened, including no result, without generalizing beyond the inspected case.',
        },
    ];
    const sections = selected.map((line, index) => ({
        title: stops[index].title,
        body: [
            materialExcerpt(line),
            `Observation prompt\n${stops[index].prompt}`,
            `Evidence and falsification\n${stops[index].check}`,
        ].join('\n\n'),
    }));
    sections.push({
        title: 'Route boundary',
        body: [
            `Objective\n${objectiveText(objective)}`,
            `${selected.length} of 3 possible stops drawn from ${countText(material.length)} distinct nonempty source lines.${selected.length < 3 ? ` Insufficient distinct material for a full trail: add ${3 - selected.length} more distinct line${3 - selected.length === 1 ? '' : 's'}. Missing stops are not invented.` : ''}`,
            'Stop 1 ranks exact objective-keyword matches; ties keep source order. Later stops minimize their greatest lexical overlap with previous stops, then prefer the greatest minimum source-line distance, then source order. Overlap uses English words and Chinese bigrams after a small stopword list; no recognized terms means lexical selection has little evidence.',
            'Duplicate lines are consolidated after Unicode, case, and whitespace normalization. Distance means line position, not time or conceptual novelty. These are generated observation prompts, not inferred personal interests, biography, semantic connections, or completed experiments.',
        ].join('\n\n'),
    });
    return result('Curiosity Trail', 'Take up to three small observation stops through supplied excerpts. Each stop keeps its source reference; lexical variety offers a prompt to explore, not an interpretation of your life.', sections, [
        { label: 'Exploration stops', value: selected.length },
        { label: 'Distinct lines', value: material.length },
    ]);
}

const EXPERIMENTS = new Map([
    ['idea-collision', ideaCollision],
    ['context-capsule', contextCapsule],
    ['question-radar', questionRadar],
    ['memory-diff', memoryDiff],
    ['decision-wind-tunnel', decisionWindTunnel],
    ['mission-forge', missionForge],
    ['echo-atlas', echoAtlas],
    ['curiosity-trail', curiosityTrail],
]);

export function runExperiment(id, fields = {}) {
    if (!EXPERIMENTS.has(id)) throw new Error('Unknown Beta experiment.');
    if (!fields || typeof fields !== 'object' || Array.isArray(fields)) throw new TypeError('Experiment fields must be an object.');
    const source = inputText(fields, 'source', true);
    const second = inputText(fields, 'second');
    const objective = inputText(fields, 'objective');
    const budget = fields.budget;
    if (typeof budget === 'string' && budget.length > TEXT_LIMIT) throw new RangeError('budget exceeds 60,000 characters.');
    if (budget !== undefined && typeof budget !== 'string' && typeof budget !== 'number') throw new TypeError('budget must be a number.');
    return EXPERIMENTS.get(id)({ source, second, objective, budget });
}
