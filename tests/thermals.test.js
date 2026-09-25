const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const model = vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../Thermals.js'), 'utf8'), model);

test('warning follows each hardware limit with a 15 degree margin', () => {
  for (const critical of [95, 100, 108, 110]) {
    const warning = critical - 15;
    const status = temperature => model.summarize([{temperature, critical}], 15);
    assert.equal(status(warning - 1).hot, false);
    assert.equal(status(warning).hot, true);
    assert.equal(status(warning + 1).hot, true);
    assert.equal(status(warning).warning, warning);
    assert.equal(status(critical).meter, 100);
  }
});

test('GPU reports the sensor closest to its own limit, even when cooler', () => {
  const sensors = [
    {temperature: 55, critical: 110, label: 'Edge'},
    {temperature: 94, critical: 110, label: 'Hotspot'},
    {temperature: 93, critical: 108, label: 'Memory'}
  ];
  const status = model.summarize(sensors, 15);
  assert.equal(status.label, 'Memory');
  assert.equal(status.temperature, 93);
  assert.equal(status.warning, 93);
  assert.equal(status.hot, true);
  sensors[2].temperature = 70;
  assert.equal(model.summarize(sensors, 15).label, 'Hotspot');
  assert.equal(model.summarize(sensors, 15).hot, false);
});

test('missing or invalid limits never invent a threshold or suppress a known warning', () => {
  for (const critical of [null, undefined, NaN, Infinity, 0, -1, '110']) {
    const status = model.summarize([{temperature: 99, critical}], 15);
    assert.equal(status.temperature, 99);
    assert.equal(status.hot, false);
    assert.ok(Number.isNaN(status.warning));
    assert.ok(Number.isNaN(status.meter));
  }
  const status = model.summarize([
    {temperature: 105, critical: null},
    {temperature: 85, critical: 100}
  ], 15);
  assert.equal(status.temperature, 85);
  assert.equal(status.hot, true);
  assert.equal(model.summarize([{temperature: 50}, {temperature: 80}], 15).temperature, 80);
});

test('absent, malformed and failed sensors stay unavailable', () => {
  for (const sensors of [null, {}, [], [null], [{temperature: null, critical: 100}],
    [{temperature: '90', critical: 100}], [{temperature: Infinity, critical: 100}]]) {
    const status = model.summarize(sensors, 15);
    assert.ok(Number.isNaN(status.temperature));
    assert.equal(status.hot, false);
  }
});

test('custom margins change the warning without changing hardware limits', () => {
  const sensors = [{temperature: 80, critical: 100}];
  assert.equal(model.summarize(sensors, 15).hot, false);
  assert.equal(model.summarize(sensors, 20).hot, true);
  assert.equal(model.summarize(sensors, 0).warning, 100);
  assert.equal(model.summarize(sensors, 100).critical, 100);
  assert.equal(model.summarize(sensors, 100).warning, 0);
  assert.equal(model.summarize([{temperature: 12, critical: 15}], 20).critical, 15);
  assert.ok(Number.isNaN(model.summarize([{temperature: 90}], 50).warning));
});
