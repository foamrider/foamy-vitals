const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const en = require('../i18n/en.json');
const no = require('../i18n/no.json');
const source = fs.readFileSync(path.join(__dirname, '../Panel.qml'), 'utf8');

// Row clicks pin, unpin or move the pin through the existing preference save.
const code = source.match(/  function togglePin\(id\) \{.*\}\n/);
assert.ok(code, 'togglePin');
function click(pinned, id) {
  const saved = [];
  const scope = vm.createContext({
    preference(key) { assert.equal(key, 'pinnedGpu'); return pinned; },
    savePreference(key, value) { saved.push([key, value]); },
  });
  vm.runInContext(code[0], scope);
  scope.togglePin(id);
  return saved;
}
assert.deepEqual(click('', '0000:05:00.0'), [['pinnedGpu', '0000:05:00.0']]);
assert.deepEqual(click('0000:05:00.0', '0000:05:00.0'), [['pinnedGpu', '']]);
assert.deepEqual(click('0000:05:00.0', '0000:c1:00.0'), [['pinnedGpu', '0000:c1:00.0']]);

// A failed collector run empties gpus for one sample; that is not a missing pin.
const missing = source.match(/readonly property bool pinnedMissing: (.*)\n/);
assert.ok(missing, 'pinnedMissing');
const Gpus = vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../Gpus.js'), 'utf8').replace(/^\.import .*$/m, ''), Gpus);
function pinnedMissing(gpus, gpuError) {
  const scope = vm.createContext({stats: {gpus: gpus, gpuError: gpuError}, gpus: gpus, Gpus: Gpus,
    preference(key) { assert.equal(key, 'pinnedGpu'); return '0000:05:00.0'; }});
  return vm.runInContext(missing[1], scope);
}
assert.equal(pinnedMissing([], 'GPU readings unavailable. Retrying…'), false);
assert.equal(pinnedMissing([], ''), true);
assert.equal(pinnedMissing([{id: '0000:05:00.0'}], ''), false);

const strings = {
  'Graphics': 'Grafikk',
  '%1 GPUs': '%1 GPU-er',
  'Integrated': 'Integrert',
  'Discrete': 'Diskret',
  'BUSY': 'BRUK',
  'TEMP': 'TEMP',
  'Asleep': 'I dvale',
  'bar': 'linje',
  'Pinned GPU not connected': 'Festet GPU er ikke tilkoblet',
  'Click to pin to bar': 'Klikk for å feste til linjen',
  'Click to unpin': 'Klikk for å løsne',
};
for (const [key, norwegian] of Object.entries(strings)) {
  assert.equal(en[key], key, `en: ${key}`);
  assert.equal(no[key], norwegian, `no: ${key}`);
  assert.ok(source.includes('root.tr("' + key + '"'), `Panel uses ${key}`);
}
console.log('Graphics pinning and strings passed');
