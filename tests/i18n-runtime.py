"""Check live Omarchy translation bindings, fallback and literal parameters in isolation."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

source = Path(__file__).resolve().parents[1]
omarchy = Path(os.environ.get('OMARCHY_PATH', '/usr/share/omarchy'))
native = (omarchy / 'shell/Commons/Translations.qml').exists()
catalog = json.loads((source / 'i18n/no.json').read_text())
key = next(k for k, v in catalog.items() if isinstance(v, str) and k != v and '%' not in k and '{' not in k)
with tempfile.TemporaryDirectory(prefix='foamy-language-test-') as directory:
    base = Path(directory)
    app = base / 'app'
    app.mkdir()
    plugin = app / 'plugin'
    plugin.mkdir()
    (plugin / 'kit').mkdir()
    # The kit's FoamyI18n resolves catalogs from ../i18n, beside kit/.
    shutil.copy(source / 'kit/FoamyI18n.qml', plugin / 'kit')
    shutil.copytree(source / 'i18n', plugin / 'i18n')
    (plugin / 'kit/qmldir').write_text('singleton FoamyI18n 1.0 FoamyI18n.qml\n')
    # The added catalog entry exists only in this fixture, never in a release.
    en = json.loads((plugin / 'i18n/en.json').read_text())
    en['English only'] = 'English only'
    (plugin / 'i18n/en.json').write_text(json.dumps(en))
    (app / 'Commons').symlink_to(omarchy / 'shell/Commons', target_is_directory=True)
    language = base / 'language'
    language.write_text('no')
    runtime = base / 'runtime'
    runtime.mkdir(mode=0o700)
    qml = '''import QtQuick
import Quickshell
import Quickshell.Io
import "plugin/kit" as Plugin
ShellRoot {
  property var translator: Plugin.FoamyI18n
  property string rendered: translator.t(KEY)
  FileView { id: choice; path: LANGUAGE_FILE }
  Timer {
    property int step: 0
    interval: 250; running: true; repeat: true
    onTriggered: {
      var expected = NATIVE && step === 0 ? TRANSLATED : KEY
      if (rendered !== expected) { console.error("Wrong translation", step, rendered, expected); Qt.exit(1); return }
      if (translator.t("English only") !== "English only" || translator.t("Unknown label") !== "Unknown label") { Qt.exit(2); return }
      if (translator.t("Name %1 {name}", ["$& %2"]) !== "Name $& %2 {name}") { Qt.exit(3); return }
      if (translator.t("Name {name}", {name: "$& {name}"}) !== "Name $& {name}") { Qt.exit(4); return }
      if (translator.rightToLeft !== (NATIVE && step === 1)) { Qt.exit(5); return }
      var threads = translator.tn("%1 threads", 1)
      if (threads !== (NATIVE && step === 0 ? "1 tråd" : "1 thread") || translator.tn("%1 threads", 8).indexOf("8") !== 0) { console.error("Wrong plural", step, threads); Qt.exit(6); return }
      if (step === 0) choice.setText("ar")
      else if (step === 1) choice.setText("en")
      else { console.log("I18N_RUNTIME_PASS", NATIVE ? "4.5 live no/ar/en" : "4.0 English fallback"); Qt.quit() }
      step++
    }
  }
}
'''
    qml = qml.replace('LANGUAGE_FILE', json.dumps(str(language))).replace('TRANSLATED', json.dumps(catalog[key])).replace('KEY', json.dumps(key)).replace('NATIVE', str(native).lower())
    (app / 'shell.qml').write_text(qml)
    env = dict(os.environ, OMARCHY_PATH=str(omarchy), OMARCHY_LANGUAGE_FILE=str(language),
               XDG_RUNTIME_DIR=str(runtime), QT_QPA_PLATFORM='offscreen',
               QT_QPA_PLATFORMTHEME='generic', QT_QUICK_BACKEND='software')
    # Isolated from the live session: temp HOME/XDG dirs, no compositor variables.
    for name in ('XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME'):
        env[name] = str(base / name.lower())
    env['HOME'] = str(base / 'home')
    for name in ('WAYLAND_DISPLAY', 'HYPRLAND_INSTANCE_SIGNATURE', 'DISPLAY'):
        env.pop(name, None)
    result = subprocess.run(['quickshell', '-p', str(app), '--no-color'], env=env,
                            capture_output=True, text=True, timeout=10)
    output = result.stdout + result.stderr
    if result.returncode or 'I18N_RUNTIME_PASS' not in output or any(e in output for e in ['ReferenceError', 'TypeError', 'Unable to assign']):
        raise AssertionError(output)
    print(source.name, '4.5 live no/ar/en' if native else '4.0 English fallback', 'passed')
