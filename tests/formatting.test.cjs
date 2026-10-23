const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../Panel.qml'), 'utf8');
let language = 'en';
const scope = vm.createContext({FoamyI18n: {number(value, decimals) {
  return new Intl.NumberFormat(language, decimals === undefined ? {} : {minimumFractionDigits: decimals, maximumFractionDigits: decimals}).format(value);
}}});
for (const name of ['diskCapacity', 'rate', 'percent', 'degrees', 'rpm', 'pair', 'processShare', 'memoryAmount']) {
  const code = source.match(new RegExp('  function ' + name + '\\([^]*?\\n  }'));
  assert.ok(code, name);
  vm.runInContext(code[0], scope);
}
for (language of ['en', 'no', 'ar']) {
  const n = scope.FoamyI18n.number;
  assert.equal(scope.rate(13.1 * 1024), n(13.1, 1) + ' KiB/s');
  assert.equal(scope.diskCapacity(2.5 * 1048576, 100 * 1048576), n(2.5, 1) + ' / ' + n(100, 1) + ' GiB');
  assert.equal(scope.pair(2.5, 100), n(2.5, 1) + ' / ' + n(100, 1) + ' GiB');
  assert.equal(scope.percent(13.1), n(13) + '%');
  assert.equal(scope.degrees(65.2), n(65) + '°C');
  assert.equal(scope.rpm(1200), n(1200) + ' rpm');
  assert.equal(scope.processShare(13.1), n(13.1, 1) + '%');
  assert.equal(scope.memoryAmount(13.1 * 1048576), n(13.1, 1) + ' GiB');
  assert.equal(scope.rate(NaN), '--');
  assert.equal(scope.percent(NaN), '--');
}
console.log('Localized Vitals readings passed');
