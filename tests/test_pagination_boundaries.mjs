/* Code version: v1.0.0-codex.0 */
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(
    new URL('../app/web/static/computer-use-agent.js', import.meta.url),
    'utf8',
);
const first = source.indexOf('    function buildAgentPaginationItems(');
const last = source.indexOf('    function agentPaginationRangeElements(', first);
assert.ok(first >= 0 && last > first, 'The production pagination functions exist.');
const runtime = vm.runInNewContext(
    source.slice(first, last)
        + '\n({buildAgentPaginationItems, buildAgentPaginationRanges})',
);
const itemsFor = (total, current) => JSON.parse(JSON.stringify(
    runtime.buildAgentPaginationItems(total, current),
));
const rangesFor = item => JSON.parse(JSON.stringify(
    runtime.buildAgentPaginationRanges(item.firstPage, item.lastPage),
));

test('one remaining page has a direct link and next arrow without a later menu', () => {
    for (const total of [6, 11, 16, 51]) {
        for (const current of [total - 5, total - 1]) {
            const items = itemsFor(total, current);
            assert.equal(
                items.some(item => item.kind === 'ellipsis' && item.position === 'trailing'),
                false,
            );
            assert.equal(
                items.filter(item => item.kind === 'page' && item.page === total).length,
                1,
            );
            assert.deepEqual(items.filter(item => item.kind === 'next').map(item => item.page), [total]);
            assert.deepEqual(items.filter(item => item.isActive).map(item => item.page), [current]);
        }
    }
});

test('every page remains reachable without duplicate direct pages or singleton ranges', () => {
    for (let total = 2; total <= 100; total += 1) {
        for (let current = 1; current <= total; current += 1) {
            const items = itemsFor(total, current);
            const direct = items.filter(item => item.kind === 'page').map(item => item.page);
            assert.equal(new Set(direct).size, direct.length);
            assert.deepEqual(items.filter(item => item.isActive).map(item => item.page), [current]);
            const reachable = new Set(direct);
            for (const item of items.filter(item => item.kind === 'ellipsis')) {
                const ranges = rangesFor(item);
                assert.ok(ranges.length > 0);
                for (const [start, end] of ranges) {
                    assert.ok(1 <= start && start < end && end <= total);
                    for (let page = start; page <= end; page += 1) reachable.add(page);
                }
            }
            assert.deepEqual([...reachable].sort((a, b) => a - b), Array.from({length: total}, (_, index) => index + 1));
        }
    }
});

test('multi-page tails retain existing five-page jump ranges', () => {
    for (const [total, expected] of [[7, [[6, 7]]], [12, [[6, 12]]]]) {
        const [trailing] = itemsFor(total, 1).filter(item => item.kind === 'ellipsis');
        assert.equal(trailing.position, 'trailing');
        assert.deepEqual(rangesFor(trailing), expected);
    }
    const [leading, trailing] = itemsFor(457, 53).filter(item => item.kind === 'ellipsis');
    assert.deepEqual(rangesFor(leading), Array.from({length: 10}, (_, index) => [index * 5 + 1, index * 5 + 5]));
    assert.deepEqual(rangesFor(trailing).slice(-2), [[446, 450], [451, 457]]);
    assert.equal(rangesFor(trailing).length, 80);
});
