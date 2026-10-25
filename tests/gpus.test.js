const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const context = vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../History.js'), 'utf8') + '\nthis.History = {append: append}', context);
const source = fs.readFileSync(path.join(__dirname, '../Gpus.js'), 'utf8').replace(/^\.import .*$/m, '');
vm.runInContext(source, context);
const G = context;

const A = '0000:03:00.0', B = '0000:05:00.0', C = '0000:c1:00.0';
const ids = list => Array.from(list, g => g.id);

test('order puts integrated, discrete, then unknown, ties by address', () => {
  const list = [{id: B, type: 'discrete'}, {id: C, type: 'integrated'}, {id: A, type: null}, {id: '0000:01:00.0', type: 'discrete'}, {type: 'discrete'}, null];
  assert.deepEqual(ids(G.order(list)), [C, '0000:01:00.0', B, A]);
});

test('barGpu picks the busiest, treating asleep as 0 and no reading as lowest', () => {
  const pick = list => G.barGpu(G.order(list), '').id;
  assert.equal(pick([{id: A, busy: 0}, {id: B, busy: 12}]), B);
  assert.equal(pick([{id: A, asleep: true, busy: null}, {id: B, busy: 3}]), B);
  assert.equal(pick([{id: A, busy: null}, {id: B, busy: 0}]), B);
  assert.equal(pick([{id: A, busy: 5}, {id: B, busy: 5}]), A);
  assert.equal(G.barGpu([], ''), null);
});

test('barGpu follows the pin, including asleep, and falls back when absent', () => {
  const list = [{id: A, busy: 50}, {id: B, busy: 1}];
  assert.equal(G.barGpu(G.order(list), B).id, B);
  assert.equal(G.barGpu(G.order([{id: A, busy: 50}, {id: B, asleep: true, busy: null}]), B).id, B);
  const absent = G.order([list[0]]);
  assert.equal(G.pinnedMissing(absent, B), true);
  assert.equal(G.barGpu(absent, B).id, A);
  const back = G.order(list);
  assert.equal(G.pinnedMissing(back, B), false);
  assert.equal(G.barGpu(back, B).id, B);
});

test('pinnedMissing', () => {
  const list = G.order([{id: A}]);
  assert.equal(G.pinnedMissing(list, ''), false);
  assert.equal(G.pinnedMissing(list, B), true);
  assert.equal(G.pinnedMissing(list, A), false);
});

test('summarize flags busy, vram, and heat; asleep clears all', () => {
  const t = {gpu: 95, vram: 95};
  const cool = {hot: false};
  assert.equal(G.summarize({busy: 95}, cool, t).busyWarning, true);
  assert.equal(G.summarize({busy: 94}, cool, t).warning, false);
  const v = G.summarize({busy: 1, vramUsed: 15.9 * 1024 ** 3, vramTotal: 16 * 1024 ** 3}, cool, t);
  assert.equal(v.vramWarning, true);
  assert.equal(v.warning, true);
  const h = G.summarize({busy: 1}, {hot: true}, t);
  assert.equal(h.hot, true);
  assert.equal(h.warning, true);
  const s = G.summarize({asleep: true, busy: 99, vramUsed: 16, vramTotal: 16}, {hot: true}, t);
  assert.deepEqual({...s}, {busyWarning: false, vramWarning: false, hot: false, warning: false});
  assert.ok(Number.isNaN(G.vram({vramUsed: null, vramTotal: 0})));
  assert.equal(G.vram({vramUsed: 4, vramTotal: 8}), 50);
});

test('appendHistory nulls a vanished GPU, never zero, then drops it', () => {
  let h = G.appendHistory({}, [{id: A, busy: 10}, {id: B, busy: 20}], 1000);
  h = G.appendHistory(h, [{id: A, busy: 11}, {id: B, asleep: true, busy: null}], 2000);
  assert.equal(h[B][1].value, 0);
  h = G.appendHistory(h, [{id: A, busy: 12}], 3000);
  assert.equal(h[B][h[B].length - 1].value, null);
  assert.ok(h[B].every(p => p.value !== 0 || p.time === 2000));
  h = G.appendHistory(h, [{id: A, busy: 13}], 3000 + 121000);
  assert.equal(h[B], undefined);
  assert.ok(h[A]);
});

test('colorSlot follows row order, then history-only ids by address', () => {
  const rows = G.order([{id: A, type: 'discrete'}, {id: C, type: 'integrated'}]);
  const history = {[A]: [], [C]: [], '0000:02:00.0': [], '0000:01:00.0': []};
  assert.equal(G.colorSlot(rows, history, C), 0);
  assert.equal(G.colorSlot(rows, history, A), 1);
  assert.equal(G.colorSlot(rows, history, '0000:01:00.0'), 2);
  assert.equal(G.colorSlot(rows, history, '0000:02:00.0'), 3);
  assert.equal(G.colorSlot(rows, {}, B), -1);
});
