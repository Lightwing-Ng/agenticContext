/* Code version: v0.2.0-codex.0 */
import assert from 'node:assert/strict';
import test from 'node:test';
import { runExperiment } from '../app/web/static/beta/engines.mjs';

const ids = ['idea-collision', 'context-capsule', 'question-radar', 'memory-diff', 'decision-wind-tunnel', 'mission-forge', 'echo-atlas', 'curiosity-trail'];
const sample = {
    source: '  Budget: 120\nCan this work offline?\nTODO: verify latency\n缓存必须保留来源。',
    second: 'Budget: 150\nHuman approval before publication\nAcceptance: every output cites a source',
    objective: 'offline source provenance',
};
const metric = (output, label) => output.metrics.find((entry) => entry.label === label)?.value;

test('every experiment is deterministic, serializable, and leaves its inputs unchanged', () => {
    for (const id of ids) {
        const fields = Object.freeze({ ...sample });
        const output = runExperiment(id, fields);
        assert.deepEqual(output, runExperiment(id, fields));
        assert.deepEqual(fields, sample);
        assert.deepEqual(JSON.parse(JSON.stringify(output)), output);
        assert.equal(typeof output.title, 'string');
        assert.equal(typeof output.summary, 'string');
        assert.equal(typeof output.markdown, 'string');
        assert.ok(output.sections.every((section) => typeof section.title === 'string' && typeof section.body === 'string'));
        output.sections[0].body = 'caller modification';
        assert.notDeepEqual(output, runExperiment(id, fields));
    }
});

test('required input, unknown IDs, and invalid field types fail explicitly', () => {
    for (const id of ids) {
        assert.throws(() => runExperiment(id, { source: ' \n\t ' }), /source is required/);
    }
    assert.throws(() => runExperiment('idea-collision', { source: 'A' }), /second is required/);
    assert.throws(() => runExperiment('missing', sample), /Unknown Beta/);
    assert.throws(() => runExperiment('__proto__', sample), /Unknown Beta/);
    assert.throws(() => runExperiment('context-capsule', null), /must be an object/);
    assert.throws(() => runExperiment('context-capsule', []), /must be an object/);
    assert.throws(() => runExperiment('context-capsule', { source: 42 }), /source must be text/);
    assert.throws(() => runExperiment('context-capsule', { source: 'A', budget: {} }), /budget must be a number/);
});

test('the 60,000-character input boundary applies to every text field without silent truncation', () => {
    assert.doesNotThrow(() => runExperiment('context-capsule', { source: 'x'.repeat(60000) }));
    for (const key of ['source', 'second', 'objective', 'budget']) {
        assert.throws(() => runExperiment('context-capsule', { ...sample, [key]: 'x'.repeat(60001) }), new RegExp(`${key} exceeds`));
    }
});

test('HTML and fence-like adversarial source remains literal text with a safe export fence', () => {
    const hostile = '<img src=x onerror="globalThis.betaExecuted=true">\n````\n<script>throw Error("executed")</script>';
    const output = runExperiment('context-capsule', { source: hostile });
    assert.ok(output.sections[0].body.includes('<img src=x onerror="globalThis.betaExecuted=true">'));
    assert.ok(output.sections[0].body.includes('<script>throw Error("executed")</script>'));
    assert.ok(output.markdown.includes('`````text\n'));
    assert.equal(globalThis.betaExecuted, undefined);
});

test('Idea collision quotes actual source lines and offers three distinct, unexecuted recipes', () => {
    const output = runExperiment('idea-collision', { source: '  Alpha sensor  ', second: '乙侧约束', objective: 'inspect' });
    assert.equal(output.sections.length, 3);
    assert.equal(new Set(output.sections.map((section) => section.title)).size, 3);
    for (const section of output.sections) {
        assert.ok(section.body.includes('[A L1]\n  Alpha sensor  '));
        assert.ok(section.body.includes('[B L1]\n乙侧约束'));
        assert.ok(section.body.includes('Generated recipe'));
        assert.ok(section.body.includes('Smallest probe'));
    }
    assert.match(output.summary, /does not establish a semantic connection/);
});

test('Context capsule enforces the complete export budget including provenance and omission notice', () => {
    const source = 'cache '.repeat(10000);
    for (const budget of [500, 501, 1000, 4000, 20000]) {
        const output = runExperiment('context-capsule', { source, budget });
        assert.ok(output.markdown.length <= budget, `${output.markdown.length} exceeds ${budget}`);
        assert.equal(output.markdown.length, budget);
        assert.match(output.markdown, /prefix excerpt/);
        assert.match(output.markdown, /outside the quoted excerpts is omitted/);
        assert.equal(metric(output, 'Partial excerpts'), 1);
    }
    assert.ok(runExperiment('context-capsule', { source, budget: 1 }).markdown.length <= 500);
    assert.ok(runExperiment('context-capsule', { source, budget: 50000 }).markdown.length <= 20000);
    assert.ok(runExperiment('context-capsule', { source }).markdown.length <= 4000);
    assert.throws(() => runExperiment('context-capsule', { source, budget: 'not a number' }), /finite number/);
    assert.throws(() => runExperiment('context-capsule', { source, budget: Infinity }), /finite number/);
});

test('Context capsule handles hostile long fences and never splits an emoji surrogate pair', () => {
    for (const source of ['`'.repeat(10000), '🌱'.repeat(10000)]) {
        const output = runExperiment('context-capsule', { source, budget: 500 });
        assert.ok(output.markdown.length <= 500);
        assert.equal(output.markdown.isWellFormed(), true);
    }
});

test('Context capsule selects relevant CJK lines lexically and preserves CRLF line provenance and whitespace', () => {
    const source = `unrelated ${'x'.repeat(1000)}\r\n  缓存预算约束：120  \r\nOther content`;
    const output = runExperiment('context-capsule', { source, objective: '预算', budget: 500 });
    assert.ok(output.sections[0].body.startsWith('[L2]\n  缓存预算约束：120  '));
    assert.match(output.summary, /No generated summary/);
});

test('Question radar deduplicates explicit items, retains all occurrence refs, and prioritizes objective matches', () => {
    const output = runExperiment('question-radar', {
        source: 'What next?\n普通陈述。\n  TODO: latency  \nTODO: latency\n缓存是否可靠？\nWho owns it?\n如何恢复缓存',
        objective: '缓存',
    });
    assert.equal(metric(output, 'Distinct open items'), 5);
    assert.equal(metric(output, 'Source occurrences'), 6);
    assert.ok(output.sections[0].body.startsWith('[Source L5]\n缓存是否可靠？'));
    assert.ok(output.sections[0].body.includes('Occurrences: L3, L4'));
    assert.ok(output.sections[0].body.includes('  TODO: latency  '));
    assert.ok(!output.sections[0].body.includes('普通陈述。'));
    const empty = runExperiment('question-radar', { source: 'A normal statement.' });
    assert.equal(metric(empty, 'Distinct open items'), 0);
    assert.match(empty.sections[0].body, /does not establish/);
});

test('Memory diff counts repeated line occurrences and exposes numeric and text changes as candidates', () => {
    const output = runExperiment('memory-diff', {
        source: 'same\nsame\nBudget: 100\nStatus: draft\n移除',
        second: 'same\nBudget: 120\nStatus: reviewed\n新增',
    });
    assert.equal(metric(output, 'Added'), 3);
    assert.equal(metric(output, 'Removed'), 4);
    assert.equal(metric(output, 'Unchanged'), 1);
    assert.equal(metric(output, 'Review candidates'), 2);
    assert.ok(output.sections[2].body.includes('[Before L3]\nBudget: 100'));
    assert.ok(output.sections[2].body.includes('[After L2]\nBudget: 120'));
    assert.ok(output.sections[2].body.includes('Status: reviewed'));
    assert.match(output.summary, /not proven contradictions/);
});

test('Memory diff supports an empty after snapshot and explicitly treats reordering as unchanged', () => {
    const deleted = runExperiment('memory-diff', { source: 'one\ntwo', second: '' });
    assert.equal(metric(deleted, 'Removed'), 2);
    assert.equal(metric(deleted, 'Added'), 0);
    const reordered = runExperiment('memory-diff', { source: 'one\ntwo', second: 'two\none' });
    assert.equal(metric(reordered, 'Unchanged'), 2);
    assert.equal(metric(reordered, 'Removed'), 0);
    assert.equal(metric(reordered, 'Added'), 0);
    assert.match(reordered.summary, /reordering alone/);
});

test('Wind tunnel labels generated assumptions and produces recipes without predicted outcomes', () => {
    const output = runExperiment('decision-wind-tunnel', { source: 'Adopt the cache.' });
    assert.equal(output.sections.length, 3);
    assert.match(output.summary, /No simulation has run/);
    for (const section of output.sections) {
        assert.match(section.body, /Generated placeholder assumption, not supplied evidence/);
        assert.match(section.body, /Smallest reversible probe/);
        assert.match(section.body, /Evidence demand/);
    }
    const supplied = runExperiment('decision-wind-tunnel', { source: 'Adopt the cache.', second: '  The source remains available.  ' });
    assert.ok(supplied.sections[0].body.includes('[Assumption L1]\n  The source remains available.  '));
});

test('Mission forge preserves requested checks and only extracts explicit human review checkpoints', () => {
    const output = runExperiment('mission-forge', { ...sample, second: 'Keep all originals\nHuman approval before publication' });
    assert.ok(output.sections[2].body.includes('[Acceptance L1]\nKeep all originals'));
    assert.ok(output.sections[3].body.includes('[Acceptance L2]\nHuman approval before publication'));
    assert.equal(metric(output, 'Independent packages'), 3);
    assert.match(output.summary, /does not launch/);
    const defaults = runExperiment('mission-forge', { source: '  Context only  ' });
    assert.match(defaults.sections[0].body, /not an inferred goal/);
    assert.match(defaults.sections[2].body, /Generated defaults/);
    assert.match(defaults.sections[3].body, /No additional human signoff/);
});

test('Echo Atlas counts distinct bilingual source lines, not repeated words or duplicate occurrences', () => {
    const output = runExperiment('echo-atlas', {
        source: '\n  Cache cache meets memory.  \r\nCACHE CACHE MEETS MEMORY.\r\n缓存连接记忆。\r\n缓存留下线索。\r\nCache traces a river.\r\nThe and with because.\r\nTHE AND WITH BECAUSE.',
        objective: '缓存',
    });
    assert.equal(metric(output, 'Distinct lines'), 5);
    assert.deepEqual(output.signals, [{ label: '缓存', value: 2 }, { label: 'cache', value: 2 }]);
    const cache = output.sections.find((section) => section.title === 'Echo: cache');
    assert.ok(cache.body.includes('[Source L2]\n  Cache cache meets memory.  '));
    assert.ok(cache.body.includes('Equivalent occurrences: L2, L3; counted as one distinct line.'));
    assert.ok(cache.body.includes('[Source L6]\nCache traces a river.'));
    assert.ok(output.sections[0].body.includes('[Source L4]\n缓存连接记忆。'));
    assert.match(output.sections.at(-1).body, /not semantic clustering/);
});

test('Echo Atlas bounds its output while counting complete lines and preserves Unicode excerpts', () => {
    const words = 'amber breeze cedar drift ember forest grove harbor island jasmine';
    const output = runExperiment('echo-atlas', {
        source: `${'🌱'.repeat(121)} ${words}\n${words} pond\n${words} marsh\n${words} ridge\n${words} river`,
    });
    assert.equal(metric(output, 'Recurring motifs'), 10);
    assert.equal(output.signals.length, 8);
    assert.ok(output.signals.every((signal) => signal.value === 5));
    assert.ok(output.sections[0].body.includes('4 line excerpts shown.'));
    assert.ok(output.sections[0].body.includes('prefix excerpt, 240/'));
    assert.equal(output.markdown.isWellFormed(), true);
    assert.match(output.sections.at(-1).body, /Counts use full source lines/);
    const single = runExperiment('echo-atlas', { source: 'ＣＡＣＨＥ cache\ncache cache' });
    assert.equal(metric(single, 'Distinct lines'), 1);
    assert.deepEqual(single.signals, []);
    assert.match(single.sections[0].body, /repeating the same line does not create an echo/);
    const noMotifs = runExperiment('echo-atlas', { source: 'a'.repeat(33) + ' north\n' + 'a'.repeat(33) + ' south' });
    assert.deepEqual(noMotifs.signals, []);
    assert.match(noMotifs.sections[0].body, /does not mean/);
});

test('Echo Atlas ranks counts before objective matches and resolves ties without host locale', () => {
    const output = runExperiment('echo-atlas', {
        source: 'river zebra apple\nriver zebra apple stone\nriver quiet',
        objective: 'zebra',
    });
    assert.deepEqual(output.signals, [
        { label: 'river', value: 3 },
        { label: 'zebra', value: 2 },
        { label: 'apple', value: 2 },
    ]);
    const tied = runExperiment('echo-atlas', { source: 'zebra apple\napple zebra stone' });
    assert.deepEqual(tied.signals.map((signal) => signal.label), ['apple', 'zebra']);
});

test('Curiosity Trail selects an objective anchor, a distant low-overlap contrast, and a further unique line', () => {
    const output = runExperiment('curiosity-trail', {
        source: '\nPaper boats follow rivers.\nCACHE MEMORY stays local.\ncache memory stays local.\nCache memory remains nearby.\n\nBirds trace the horizon.\nClouds drift slowly.',
        objective: 'cache memory',
    });
    assert.equal(metric(output, 'Distinct lines'), 5);
    assert.equal(metric(output, 'Exploration stops'), 3);
    assert.ok(output.sections[0].body.startsWith('[Source L3]\nCACHE MEMORY stays local.'));
    assert.ok(output.sections[0].body.includes('Equivalent occurrences: L3, L4; counted as one distinct line.'));
    assert.ok(output.sections[1].body.startsWith('[Source L8]\nClouds drift slowly.'));
    assert.ok(output.sections[2].body.startsWith('[Source L2]\nPaper boats follow rivers.'));
    for (const section of output.sections.slice(0, 3)) {
        assert.match(section.body, /Observation prompt/);
        assert.match(section.body, /Evidence and falsification/);
    }
    assert.match(output.sections.at(-1).body, /not inferred personal interests, biography/);
});

test('Curiosity Trail resolves equal overlap and distance by original line order and supports Chinese objectives', () => {
    const output = runExperiment('curiosity-trail', {
        source: 'Ocean tides\n缓存来源保持可见。\nBird flight',
        objective: '来源',
    });
    assert.ok(output.sections[0].body.startsWith('[Source L2]\n缓存来源保持可见。'));
    assert.ok(output.sections[1].body.startsWith('[Source L1]\nOcean tides'));
    assert.ok(output.sections[2].body.startsWith('[Source L3]\nBird flight'));
});

test('sparse and token-free material yields honest partial trails without fabricated source references', () => {
    const one = runExperiment('curiosity-trail', { source: '  A small observation.  \nA SMALL OBSERVATION.' });
    assert.equal(metric(one, 'Exploration stops'), 1);
    assert.equal(one.sections.length, 2);
    assert.match(one.sections.at(-1).body, /add 2 more distinct lines/);
    assert.match(one.sections.at(-1).body, /Missing stops are not invented/);
    const two = runExperiment('curiosity-trail', { source: '🌱\n🌧' });
    assert.equal(metric(two, 'Exploration stops'), 2);
    assert.ok(two.sections[0].body.startsWith('[Source L1]\n🌱'));
    assert.ok(two.sections[1].body.startsWith('[Source L2]\n🌧'));
    assert.match(two.sections.at(-1).body, /add 1 more distinct line\./);
    assert.match(two.sections.at(-1).body, /no recognized terms means lexical selection has little evidence/);
});

test('new experiments honor input limits and keep hostile evidence literal inside safe Markdown fences', () => {
    const hostile = '<img src=x onerror="globalThis.betaExecuted=true"> ````` cache';
    for (const id of ['echo-atlas', 'curiosity-trail']) {
        assert.doesNotThrow(() => runExperiment(id, { source: '🌱'.repeat(30000) }));
        for (const field of ['source', 'objective']) {
            assert.throws(() => runExperiment(id, { source: 'A supplied line', [field]: 'x'.repeat(60001) }), new RegExp(`${field} exceeds`));
        }
        const output = runExperiment(id, { source: `${hostile}\ncache observation`, objective: '<script>globalThis.betaExecuted=true</script>' });
        const raw = output.sections.filter((section) => section.body.includes(hostile));
        assert.ok(raw.length > 0);
        for (const section of raw) assert.ok(output.markdown.includes(`\`\`\`\`\`\`text\n${section.body}\n\`\`\`\`\`\``));
        assert.equal(globalThis.betaExecuted, undefined);
    }
});

test('new experiment prose groups large counts while numeric signals and line identifiers remain exact', () => {
    const source = Array.from({ length: 1234 }, (_, index) => `cache ${index}`).join('\n');
    const atlas = runExperiment('echo-atlas', { source });
    assert.deepEqual(atlas.signals, [{ label: 'cache', value: 1234 }]);
    assert.match(atlas.sections[0].body, /1,234 distinct source lines/);
    assert.match(atlas.sections.at(-1).body, /1,234 distinct nonempty lines/);
    const trail = runExperiment('curiosity-trail', { source, objective: '1233' });
    assert.ok(trail.sections[0].body.startsWith('[Source L1234]\ncache 1233'));
    assert.match(trail.sections.at(-1).body, /1,234 distinct nonempty source lines/);
    const longLine = runExperiment('curiosity-trail', { source: 'cache '.repeat(2000) });
    assert.match(longLine.sections[0].body, /prefix excerpt, 300\/12,000 characters/);
});

test('malformed Unicode remains source evidence and does not remove the required anchor', () => {
    for (const source of ['\ud800', '\udfff', '\u3164', '\uffa0', '\u200b']) {
        const trail = runExperiment('curiosity-trail', { source });
        assert.equal(metric(trail, 'Exploration stops'), 1);
        assert.ok(trail.sections[0].body.startsWith(`[Source L1]\n${source}`));
    }
});
