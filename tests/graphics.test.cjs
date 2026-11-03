const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const en = require('../i18n/en.json');
const no = require('../i18n/no.json');
const view = fs.readFileSync(path.join(__dirname, '../VitalsView.qml'), 'utf8');
const readings = fs.readFileSync(path.join(__dirname, '../VitalsReadings.qml'), 'utf8');

// Row clicks pin, unpin or move the pin through the view's save signal.
const code = view.match(/  function togglePin\(id\) \{.*\}\n/);
assert.ok(code, 'VitalsView has a one-line togglePin(id)');
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

// A failed sample lists no GPUs, or a failed probe errors a row: neither is a missing pin.
const missing = readings.match(/readonly property bool pinnedMissing: (.*(?:\n {4,}.*)*)/);
assert.ok(missing, 'VitalsReadings has pinnedMissing');
const failedRow = readings.match(/readonly property string gpuError: \{([\s\S]*?)\n  \}/);
assert.ok(failedRow, 'VitalsReadings has gpuError');
const Gpus = vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../Gpus.js'), 'utf8').replace(/^\.import .*$/m, ''), Gpus);
function pinnedMissing(stats, telemetryError = '') {
  const rows = Gpus.order(stats.gpus);
  const scope = vm.createContext({
    VitalsService: { telemetryError: telemetryError }, stats: stats, Gpus: Gpus,
    gpus: rows, gpuRows: rows, pinnedGpu: '0000:05:00.0',
  });
  scope.gpuError = vm.runInContext('(function() {' + failedRow[1] + '\n})()', scope);
  return vm.runInContext(missing[1], scope);
}
const present = { id: '0000:05:00.0', type: 'discrete', error: '' };
const other = { id: '0000:c1:00.0', type: 'integrated', error: '' };
assert.equal(pinnedMissing({ gpus: [other] }, 'Telemetry unavailable. Retrying…'), false, 'telemetry error');
assert.equal(pinnedMissing({}), false, 'no gpus array');
assert.equal(pinnedMissing({ gpus: [Object.assign({}, other, { error: 'NVIDIA readings unavailable. Check nvidia-smi.' })] }), false, 'row error');
assert.equal(pinnedMissing({ gpus: [] }), true, 'no GPUs at all');
assert.equal(pinnedMissing({ gpus: [other] }), true, 'pin absent');
assert.equal(pinnedMissing({ gpus: [other, present] }), false, 'pin present');

// New strings: English is the key, Norwegian as agreed, and the view uses each one.
const strings = {
  'Graphics': 'Grafikk',
  '%1 GPUs': { one: '%1 GPU', other: '%1 GPU-er' },
  'Integrated': 'Integrert',
  'Discrete': 'Diskret',
  'Busy': 'Bruk',
  'Temp': 'Temp',
  'bar': 'linje',
  'Pinned GPU not connected': 'Festet GPU er ikke tilkoblet',
  'Click to pin to bar': 'Klikk for å feste til linjen',
  'Click to unpin': 'Klikk for å løsne',
};
for (const [key, norwegian] of Object.entries(strings)) {
  if (typeof norwegian === 'string') assert.equal(en[key], key, `en: ${key}`);
  else assert.deepEqual(en[key], { one: '%1 GPU', other: '%1 GPUs' }, `en: ${key}`);
  assert.deepEqual(no[key], norwegian, `no: ${key}`);
  assert.ok(view.includes('FoamyI18n.t("' + key + '"') || view.includes('FoamyI18n.tn("' + key + '"'), `VitalsView uses ${key}`);
}
for (const key of ['Sleeping', 'GPU', 'VRAM']) assert.ok(view.includes('FoamyI18n.t("' + key + '")'), `VitalsView reuses ${key}`);
console.log('Graphics pinning and strings passed');
