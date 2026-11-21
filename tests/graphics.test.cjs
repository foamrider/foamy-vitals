const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const readings = fs.readFileSync(path.join(__dirname, '../VitalsReadings.qml'), 'utf8');

test('a missing pin shows unless the sample failed or has no GPU list', () => {
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
});
