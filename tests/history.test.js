const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const model = vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../History.js'), 'utf8'), model);

test('history expires samples, replaces duplicates and preserves missing values', () => {
  let points = [];
  for (let time = 0; time <= 300000; time += 1000) points = model.append(points, time, 25);
  assert.equal(points.length, 120);
  points = model.append(points, 300000, NaN);
  assert.equal(points.length, 120);
  assert.equal(points.at(-1).value, null);
  assert.equal(model.peak(points), 25);
  assert.equal(model.append([], 0, null)[0].value, null);
});

test('network rates use elapsed sample time and preserve idle zero readings', () => {
  const first = model.networkRate(null, {interface: 'br0', rxBytes: 100, txBytes: 200}, 1000);
  assert.ok(Number.isNaN(first.up));
  const second = model.networkRate(first.snapshot, {interface: 'br0', rxBytes: 6100, txBytes: 200}, 4000);
  assert.equal(second.down, 2000);
  assert.equal(second.up, 0);
});

test('route changes, counter reset, missing readings and suspend gaps do not spike', () => {
  const previous = {interface: 'br0', rxBytes: 100, txBytes: 200, time: 1000};
  for (const [current, time] of [
    [{interface: 'wlan0', rxBytes: 900000, txBytes: 900000}, 4000],
    [{interface: 'br0', rxBytes: 0, txBytes: 0}, 4000],
    [null, 4000],
    [{interface: 'br0', rxBytes: null, txBytes: null}, 4000],
    [{interface: 'br0', rxBytes: 900000, txBytes: 900000}, 90000],
    [{interface: 'br0', rxBytes: 200, txBytes: 400}, 1000]
  ]) {
    const result = model.networkRate(previous, current, time);
    assert.ok(Number.isNaN(result.up));
    assert.ok(Number.isNaN(result.down));
  }
});
