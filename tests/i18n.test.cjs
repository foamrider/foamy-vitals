const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const en = require('../i18n/en.json');
const no = require('../i18n/no.json');
const manifest = require('../manifest.json');
const schema = require('../Preferences.js');
const root = path.join(__dirname, '..');
const tokens = s => [...String(s).matchAll(/%[1-9][0-9]*|\{\w+\}/g)].map(m => m[0]).sort();
const forms = v => (typeof v === 'string' ? [v] : Object.values(v));

// English source keys: plain entries repeat the key, plural entries carry one/other.
for (const [key, value] of Object.entries(en)) {
  if (typeof value === 'string') assert.equal(value, key, `English source key: ${key}`);
  else assert.ok(typeof value.one === 'string' && typeof value.other === 'string', `English plural: ${key}`);
  assert.ok(Object.hasOwn(no, key), `Norwegian entry: ${key}`);
  assert.equal(typeof no[key], typeof value, `same entry shape: ${key}`);
  for (const form of forms(no[key])) assert.deepEqual(tokens(form), tokens(key), `placeholders: ${key}`);
}
assert.deepEqual(Object.keys(no).sort(), Object.keys(en).sort());

// Every literal the plugin and the kit pass to FoamyI18n.t/tn has an entry; tn keys are plurals.
const files = [...fs.readdirSync(root).filter(f => f.endsWith('.qml')), ...fs.readdirSync(path.join(root, 'kit')).filter(f => /\.(qml|js)$/.test(f)).map(f => 'kit/' + f)];
for (const file of files) {
  // Kit files document their API with example strings in comments; skip comment lines.
  const source = fs.readFileSync(path.join(root, file), 'utf8').split('\n').filter(line => !/^\s*\/\//.test(line)).join('\n');
  for (const [, fn, key] of source.matchAll(/FoamyI18n\.(tn?)\("((?:[^"\\]|\\.)*)"/g)) {
    assert.ok(Object.hasOwn(en, key), `missing catalog key in ${file}: ${key}`);
    if (fn === 'tn') assert.equal(typeof en[key], 'object', `tn key needs a plural entry: ${key}`);
  }
}
// Strings that reach t() indirectly: schema labels, groups and options, helper and status messages.
const indirect = [
  ...schema.fields.flatMap(f => [f.label, f.group, f.description].filter(Boolean)),
  ...schema.fields.flatMap(f => (f.options || []).map(schema.optionLabel)),
  // GPU messages of the stats helper (native/src): `error = "…"`, NVIDIA_ERROR and the Intel UNAVAILABLE text.
  ...fs.readdirSync(path.join(root, 'native/src')).filter(f => f.endsWith('.rs')).flatMap(f =>
    [...fs.readFileSync(path.join(root, 'native/src', f), 'utf8').replace(/\\u\{2026\}/g, '…')
      .matchAll(/(?:error = |(?:UNAVAILABLE|NVIDIA_ERROR): &str =\s*)"([A-Z][^"]+)"/g)].map(m => m[1])),
  // Every capitalised literal on a fail(…) or `diskStatus = …` line (ternaries included).
  ...[...fs.readFileSync(path.join(root, 'VitalsService.qml'), 'utf8').matchAll(/(?:fail\(|diskStatus = )([^\n]*)/g)]
    .flatMap(m => [...m[1].matchAll(/"([A-Z][^"]+)"/g)].map(literal => literal[1])),
  'Reading disk capacity', 'Install btop to use this shortcut.', 'Edge', 'Hotspot', 'Memory', 'GPU',
  'Undo', 'Redo', 'Cut', 'Copy', 'Paste', 'Select all', "That value isn't allowed.", "Settings can't be saved: this widget has no bar entry.",
];
assert.ok(indirect.includes('Telemetry unavailable. Retrying…') && indirect.includes('NVIDIA readings unavailable. Check nvidia-smi.'));
assert.ok(indirect.includes('Intel GPU readings unavailable. Retrying…') && indirect.includes("This plugin's helper isn't available for this CPU."));
assert.ok(indirect.includes('No mounted local disks') && indirect.includes("Some disks couldn't be read") && indirect.includes('Disk capacity unavailable'));
for (const key of indirect) assert.ok(Object.hasOwn(en, key), `missing indirect catalog key: ${key}`);

assert.equal(manifest.barWidget?.defaults?.language, undefined);
assert.equal((manifest.barWidget?.schema || []).some(f => f.key === 'language'), false);
console.log('i18n catalogs ok:', Object.keys(en).length, 'keys');
