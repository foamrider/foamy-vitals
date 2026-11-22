const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const en = require('../i18n/en.json');
const no = require('../i18n/no.json');
const schema = require('../Preferences.js');
const loader = require('../kit/node-loader.cjs');
const root = path.join(__dirname, '..');
const tokens = s => [...String(s).matchAll(/%[1-9][0-9]*|\{\w+\}/g)].map(m => m[0]).sort();
const forms = v => (typeof v === 'string' ? [v] : Object.values(v));
const read = file => fs.readFileSync(path.join(root, file), 'utf8');
const literals = (text, pattern) => [...text.matchAll(pattern)].map(m => m[1]);

test('every English key has a Norwegian entry of the same shape and placeholders', () => {
  // English source keys: plain entries repeat the key, plural entries carry one/other.
  for (const [key, value] of Object.entries(en)) {
    if (typeof value === 'string') assert.equal(value, key, `English source key: ${key}`);
    else assert.ok(typeof value.one === 'string' && typeof value.other === 'string', `English plural: ${key}`);
    assert.ok(Object.hasOwn(no, key), `Norwegian entry: ${key}`);
    assert.equal(typeof no[key], typeof value, `same entry shape: ${key}`);
    for (const form of forms(no[key])) assert.deepEqual(tokens(form), tokens(key), `placeholders: ${key}`);
  }
  assert.deepEqual(Object.keys(no).sort(), Object.keys(en).sort());
});

test('every literal passed to FoamyI18n.t or tn has an entry', () => {
  const files = [...fs.readdirSync(root).filter(f => f.endsWith('.qml')), ...fs.readdirSync(path.join(root, 'kit')).filter(f => /\.(qml|js)$/.test(f)).map(f => 'kit/' + f)];
  for (const file of files) {
    // Kit files document their API with example strings in comments; skip comment lines.
    const source = read(file).split('\n').filter(line => !/^\s*\/\//.test(line)).join('\n');
    for (const [, fn, key] of source.matchAll(/FoamyI18n\.(tn?)\("((?:[^"\\]|\\.)*)"/g)) {
      assert.ok(Object.hasOwn(en, key), `missing catalog key in ${file}: ${key}`);
      if (fn === 'tn') assert.equal(typeof en[key], 'object', `tn key needs a plural entry: ${key}`);
    }
  }
});

test('strings that reach FoamyI18n as values have entries', () => {
  // Each string is read from where production writes it, so a changed
  // string is checked in its new form. Every route must still find some.
  const kitPrefs = loader.load('FoamyPrefs');
  const kitTexts = {
    NOT_ALLOWED: kitPrefs.NOT_ALLOWED,
    NO_ENTRY: kitPrefs.NO_ENTRY,
    UNAVAILABLE_TEXT: loader.load('FoamyNative').UNAVAILABLE_TEXT,
  };
  for (const [name, text] of Object.entries(kitTexts)) assert.equal(typeof text, 'string', `the kit still has ${name}`);
  const derived = {
    // Every capitalised literal on a fail(…) or `diskStatus = …` line (ternaries included) and the initial diskStatus.
    service: [...read('VitalsService.qml').matchAll(/(?:fail\(|diskStatus(?::| =) )([^\n]*)/g)]
      .flatMap(m => literals(m[1], /"([A-Z][^"]+)"/g)),
    // GPU messages of the stats helper (native/src): `error = "…"`, NVIDIA_ERROR and the Intel UNAVAILABLE text.
    helper: fs.readdirSync(path.join(root, 'native/src')).filter(f => f.endsWith('.rs')).flatMap(f =>
      literals(read('native/src/' + f).replace(/\\u\{2026\}/g, '…'), /(?:error = |(?:UNAVAILABLE|NVIDIA_ERROR): &str =\s*)"([A-Z][^"]+)"/g)),
    // Launch errors the Panel shows (the btop hint).
    panel: literals(read('Panel.qml'), /launchError = "([^"]+)"/g),
    // GPU sensor labels the helper names itself (Edge, Hotspot, Memory).
    sensors: literals(read('native/src/gpus.rs'), /=> b"([A-Z][A-Za-z]+)"\.to_vec\(\)/g),
    // Its label for any other sensor, "Sensor <N>": the view asks for "Sensor %1".
    numbered: literals(read('native/src/gpus.rs'), /\[&b"([A-Z][A-Za-z]+ )"\[\.\.\], number\]/g).map(prefix => prefix + '%1'),
    // The context-menu labels of the kit's text field.
    menu: fs.readdirSync(path.join(root, 'kit')).filter(f => f.endsWith('.qml'))
      .flatMap(f => literals(read('kit/' + f), /translate\("((?:[^"\\]|\\.)*)"\)/g)),
    // Messages of the kit's settings engine and native helper loader, and of its save queue.
    kit: Object.values(kitTexts),
    queue: literals(read('kit/FoamyPrefsQueue.qml'), /error = "([^"]+)"/g),
  };
  for (const [route, least] of Object.entries({service: 1, helper: 1, panel: 1, sensors: 3, numbered: 1, menu: 6, queue: 1})) {
    assert.ok(derived[route].length >= least, `the ${route} strings are still found (${derived[route].length})`);
  }
  const indirect = [
    // Schema labels, groups, descriptions and option labels.
    ...schema.fields.flatMap(f => [f.label, f.group, f.description].filter(Boolean)),
    ...schema.fields.flatMap(f => (f.options || []).map(schema.optionLabel)),
    ...Object.values(derived).flat(),
    'GPU',
  ];
  for (const key of indirect) assert.ok(Object.hasOwn(en, key), `missing indirect catalog key: ${key}`);
  // The routes still find the strings they were written for.
  assert.ok(indirect.includes('Telemetry unavailable. Retrying…') && indirect.includes('NVIDIA readings unavailable. Check nvidia-smi.'));
  assert.ok(indirect.includes('Intel GPU readings unavailable. Retrying…') && indirect.includes("This plugin's helper isn't available for this CPU."));
  assert.ok(indirect.includes('No mounted local disks') && indirect.includes("Some disks couldn't be read") && indirect.includes('Disk capacity unavailable'));
  assert.ok(indirect.includes('Reading disk capacity') && indirect.includes('Install btop to use this shortcut.'));
});
