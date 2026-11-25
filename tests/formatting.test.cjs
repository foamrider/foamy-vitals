const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../VitalsView.qml'), 'utf8');
let language = 'en';
const scope = vm.createContext({FoamyI18n: {t(key, values) { return key.replace(/%(\d)/g, (_, i) => scope.FoamyI18n.number(values[i - 1])); }, number(value, decimals) {
  return new Intl.NumberFormat(language, decimals === undefined ? {} : {minimumFractionDigits: decimals, maximumFractionDigits: decimals}).format(value);
}}});
test('readings are formatted in the Omarchy language', () => {
  for (const name of ['diskCapacity', 'rate', 'percent', 'degrees', 'pair', 'processShare', 'memoryAmount', 'uptimeText', 'loadText']) {
    const code = source.match(new RegExp('  function ' + name + '\\([^]*?\\n  }'));
    assert.ok(code, name);
    vm.runInContext(code[0], scope);
  }
  for (language of ['en', 'no', 'ar']) {
    const n = scope.FoamyI18n.number;
    assert.equal(scope.rate(13.1 * 1024), n(13.1, 1) + ' KiB/s');
    assert.equal(scope.rate(1048576), n(1, 1) + ' MiB/s');
    assert.ok(scope.rate(1048575).endsWith(' KiB/s'), scope.rate(1048575));
    assert.equal(scope.diskCapacity(2.5 * 1048576, 100 * 1048576), n(2.5, 1) + ' / ' + n(100, 1) + ' GiB');
    assert.equal(scope.diskCapacity(1073741824, 2 * 1073741824), n(1, 1) + ' / ' + n(2, 1) + ' TiB');
    assert.equal(scope.pair(2.5, 100), n(2.5, 1) + ' / ' + n(100, 1) + ' GiB');
    assert.equal(scope.pair(1, 0), '--');
    assert.equal(scope.percent(13.1), n(13) + '%');
    assert.equal(scope.degrees(65.2), n(65) + '°C');
    assert.equal(scope.degrees(NaN), '--');
    assert.equal(scope.uptimeText(125), n(2) + 'h ' + n(5) + 'm');
    assert.equal(scope.uptimeText(-1), '--');
    assert.equal(scope.loadText([0.5, 1.25]), n(0.5, 2) + ' / ' + n(1.25, 2));
    assert.equal(scope.loadText([]), '--');
    assert.equal(scope.processShare(13.1), n(13.1, 1) + '%');
    // From 100 % up a process share has no decimal.
    assert.equal(scope.processShare(150), n(150, 0) + '%');
    assert.equal(scope.memoryAmount(13.1 * 1048576), n(13.1, 1) + ' GiB');
    assert.equal(scope.memoryAmount(512 * 1024), n(512) + ' MiB');
    assert.equal(scope.memoryAmount(-1), '--');
    assert.equal(scope.rate(NaN), '--');
    assert.equal(scope.percent(NaN), '--');
  }
});

test('GPU sensor labels are catalog keys, an unnamed sensor with its number', () => {
  const en = require('../i18n/en.json');
  const asked = [];
  const labels = vm.createContext({FoamyI18n: {t(key, values) {
    asked.push(key);
    return key.replace(/%(\d)/g, (_, i) => values[i - 1]);
  }}});
  vm.runInContext(source.match(/ {2}function temperatureDetail\([^]*?\n {2}}/)[0], labels);
  assert.equal(labels.temperatureDetail({temperature: 50, critical: 90, label: 'Sensor 3'}), 'Sensor 3');
  assert.equal(labels.temperatureDetail({temperature: 50, critical: NaN, label: 'Edge'}), 'Edge · Limit unknown');
  assert.equal(labels.temperatureDetail({temperature: 50, critical: 90, label: ''}), '');
  assert.equal(labels.temperatureDetail({temperature: NaN, critical: 90, label: 'Sensor 3'}), '');
  assert.ok(asked.includes('Sensor %1'), asked.join(', '));
  // Labels the Intel xe driver writes itself: each is asked for by its catalog key.
  for (const [label, key, shown] of [
    ['pkg', 'Package', 'Package'],
    ['vram', 'VRAM', 'VRAM'],
    ['mctrl', 'Memory controller', 'Memory controller'],
    ['pcie', 'PCIe', 'PCIe'],
    ['vram_ch_2', 'VRAM channel %1', 'VRAM channel 2'],
  ]) {
    const from = asked.length;
    assert.equal(labels.temperatureDetail({temperature: 50, critical: 90, label}), shown, label);
    assert.deepEqual(asked.slice(from), [key], label);
  }
  for (const key of asked) assert.ok(Object.hasOwn(en, key), `catalog key: ${key}`);
  // Any other driver label has no catalog entry and is shown as it is.
  assert.equal(labels.temperatureDetail({temperature: 50, critical: 90, label: 'tile0'}), 'tile0');
});
