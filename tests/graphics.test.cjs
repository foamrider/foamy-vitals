const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const view = fs.readFileSync(path.join(__dirname, '../VitalsView.qml'), 'utf8');
const readings = fs.readFileSync(path.join(__dirname, '../VitalsReadings.qml'), 'utf8');

// Row clicks pin, unpin or move the pin through the view's save signal.
// A view function's source: its body on one line or several (as qmlformat
// writes it), up to the closing brace at its indent.
const viewFunction = (name, args) =>
  view.match(new RegExp('^  function ' + name + '\\(' + args + '\\) \\{(?:.*\\}$|[\\s\\S]*?\\n  \\}$)', 'm'));
const code = viewFunction('togglePin', 'id');
assert.ok(code, 'VitalsView has togglePin(id)');
function click(pinned, id) {
  const saved = [];
  const scope = vm.createContext({
    r: { pinnedGpu: pinned },
    saveRequested(key, value) { saved.push([key, value]); },
  });
  vm.runInContext(code[0], scope);
  scope.togglePin(id);
  return saved;
}
assert.deepEqual(click('', '0000:05:00.0'), [['pinnedGpu', '0000:05:00.0']]);
assert.deepEqual(click('0000:05:00.0', '0000:05:00.0'), [['pinnedGpu', '']]);
assert.deepEqual(click('0000:05:00.0', '0000:c1:00.0'), [['pinnedGpu', '0000:c1:00.0']]);

// A failed sample lists no GPUs: that is not a missing pin. A failed probe
// errors a row but never drops one, so it says nothing about the pin.
const missing = readings.match(/readonly property bool pinnedMissing: (.*(?:\n {4,}.*)*)/);
assert.ok(missing, 'VitalsReadings has pinnedMissing');
const failedRow = readings.match(/readonly property string gpuError: \{([\s\S]*?)\n  \}/);
assert.ok(failedRow, 'VitalsReadings has gpuError');
const Gpus = vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../Gpus.js'), 'utf8').replace(/^\.import .*$/gm, ''), Gpus);
function pinnedMissing(stats, telemetryError = '', pin = '0000:05:00.0') {
  const rows = Gpus.order(stats.gpus);
  const scope = vm.createContext({
    VitalsService: { telemetryError: telemetryError }, stats: stats, Gpus: Gpus,
    gpus: rows, gpuRows: rows, pinnedGpu: pin,
  });
  scope.gpuError = vm.runInContext('(function() {' + failedRow[1] + '\n})()', scope);
  return vm.runInContext(missing[1], scope);
}
const present = { id: '0000:05:00.0', type: 'discrete', error: '' };
const other = { id: '0000:c1:00.0', type: 'integrated', error: '' };
assert.equal(pinnedMissing({ gpus: [other] }, 'Telemetry unavailable. Retrying…'), false, 'telemetry error');
assert.equal(pinnedMissing({}), false, 'no gpus array');
assert.equal(pinnedMissing({ gpus: [Object.assign({}, other, { error: 'NVIDIA readings unavailable. Check nvidia-smi.' })] }), true, 'row error, pin absent');
assert.equal(pinnedMissing({ gpus: [other, Object.assign({}, present, { error: 'NVIDIA readings unavailable. Check nvidia-smi.' })] }), false, 'row error, pin present');
assert.equal(pinnedMissing({ gpus: [] }), true, 'no GPUs at all');
assert.equal(pinnedMissing({ gpus: [other] }), true, 'pin absent');
assert.equal(pinnedMissing({ gpus: [other, present] }), false, 'pin present');
assert.equal(pinnedMissing({ gpus: [other] }, '', ''), false, 'empty pin');

// One chart line per GPU: slot 0 keeps the single-GPU color, later slots cycle through the tints.
const tints = view.match(/  readonly property var gpuTints: (\[.*\])\n/);
assert.ok(tints, 'VitalsView has gpuTints');
const slotFn = viewFunction('gpuSlotColor', 'slot');
assert.ok(slotFn, 'VitalsView has gpuSlotColor(slot)');
const chartIds = view.match(/  readonly property var gpuChartIds: (Object[\s\S]*?\n  \}\))/);
assert.ok(chartIds, 'VitalsView has gpuChartIds');
const chartScope = vm.createContext({
  Qt: { rgba: (r, g, b, a) => ['rgba', r, g, b, a], tint: (base, tint) => ['tint', base, tint] },
  Commons: { Color: { accent: 'accent' } },
  gpuColor: 'gpuColor',
  Gpus: Gpus,
  r: { gpus: [{ id: 'b' }, { id: 'a' }] },
  history: { gpus: { gone: [], a: [], b: [] } },
});
vm.runInContext('var gpuTints = ' + tints[1], chartScope);
vm.runInContext(slotFn[0], chartScope);
const t = vm.runInContext('gpuTints', chartScope);
assert.equal(t.length, 3);
const colors = [0, 1, 2, 3, 4].map((s) => vm.runInContext('gpuSlotColor(' + s + ')', chartScope));
assert.equal(colors[0], 'gpuColor');
assert.deepEqual(colors.slice(1), [t[0], t[1], t[2], t[0]].map((x) => ['tint', 'accent', x]));
assert.deepEqual(vm.runInContext('JSON.stringify(' + chartIds[1] + ')', chartScope), JSON.stringify(['b', 'a', 'gone']), 'a vanished id sorts after the current rows');
console.log('Graphics pinning and strings passed');
