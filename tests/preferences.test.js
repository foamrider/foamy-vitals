const test = require('node:test');
const assert = require('node:assert/strict');
const preferences = require('../Preferences.js');
const manifest = require('../manifest.json');

test('manifest and runtime use the same setting defaults and constraints', () => {
  for (const field of preferences.fields) {
    assert.equal(preferences.value({}, field.key), manifest.barWidget.defaults[field.key]);
    const schema = manifest.barWidget.schema.find(item => item.key === field.key);
    for (const key of ['type', 'defaultValue', 'min', 'max', 'step', 'options']) {
      assert.deepEqual(schema[key], field[key]);
    }
  }
});

test('warnings reject missing, coerced, fractional and out-of-range values', () => {
  for (const key of ['cpuWarning', 'memoryWarning', 'gpuWarning', 'vramWarning']) {
    for (const value of [null, undefined, NaN, Infinity, '95', true, 0, 101, 94.5]) {
      assert.equal(preferences.valid(key, value), false);
      assert.equal(preferences.value({[key]: value}, key), 95);
    }
    for (const value of [1, 94, 95, 100]) assert.equal(preferences.valid(key, value), true);
  }
  for (const key of ['cpuTemperatureMargin', 'gpuTemperatureMargin']) {
    assert.equal(preferences.valid(key, 0), true);
    assert.equal(preferences.valid(key, 100), true);
    assert.equal(preferences.valid(key, -1), false);
    assert.equal(preferences.valid(key, 101), false);
  }
});

test('individual warning values and bar selection stay independent', () => {
  const settings = {cpuWarning: 70, gpuWarning: 99, cpuTemperatureMargin: 20, barDisplay: 'gpu'};
  assert.equal(preferences.value(settings, 'cpuWarning'), 70);
  assert.equal(preferences.value(settings, 'gpuWarning'), 99);
  assert.equal(preferences.value(settings, 'memoryWarning'), 95);
  assert.equal(preferences.value(settings, 'cpuTemperatureMargin'), 20);
  assert.equal(preferences.value(settings, 'gpuTemperatureMargin'), 15);
  assert.equal(preferences.value(settings, 'barDisplay'), 'gpu');
  assert.equal(preferences.value({barDisplay: 'vram'}, 'barDisplay'), 'adaptive');
  assert.equal(preferences.valid('unknownSetting', 1), false);
});

test('language follows the shell locale unless explicitly selected', () => {
  assert.equal(preferences.language('system', 'nb_NO'), 'nb');
  assert.equal(preferences.language('system', 'en_US'), 'en');
  assert.equal(preferences.language('en', 'nb_NO'), 'en');
  assert.equal(preferences.text('Settings', 'nb'), 'Innstillinger');
});
