#!/bin/bash
# foamy-vitals' own tests; tests/validate.sh runs this after the linters.
#   --ci    no Quickshell, no Omarchy host: node and the helper's Python tests
#   --fast  skip the offscreen runtime tests
# The other architecture's helper runs too when this machine can execute it
# (qemu-user binfmt); compare_stats.py runs only with FOAMY_VITALS_REFERENCE.
set -uo pipefail
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." || exit 1
ci=0 fast=0
for arg in "$@"; do
  case $arg in
    --ci) ci=1 ;;
    --fast) fast=1 ;;
    *)
      echo "suite.sh: unknown argument $arg" >&2
      exit 2
      ;;
  esac
done
status=0
run() {
  echo "== $*"
  "$@" || status=1
}
skip() { echo "SKIP $*"; }

for test in tests/*.test.js tests/*.test.cjs; do
  run node "$test"
done
run python3 -I tests/test_stats.py
run python3 -I tests/test_intel_stats.py
run python3 -I tests/compare_stats.py

case $(uname -m) in
  x86_64) other=aarch64 ;;
  aarch64) other=x86_64 ;;
  *) other="" ;;
esac
helper=bin/$other/foamy-vitals-helper
# An unknown argument exits 2 when the binary runs at all.
if [[ -n $other ]] && (
  ulimit -c 0
  "$helper" --probe >/dev/null 2>&1
  [[ $? == 2 ]]
); then
  # Emulated runs must not leave qemu core files in the checkout.
  ulimit -c 0
  run env FOAMY_HELPER="[\"$helper\", \"stats\"]" python3 -I tests/test_stats.py
  run env FOAMY_HELPER="[\"$helper\", \"stats\"]" python3 -I tests/test_intel_stats.py
else
  skip "the ${other:-other-arch} helper tests: this machine can't run that binary (qemu-user binfmt)"
fi

if ((ci || fast)); then
  skip "tests/runtime.py, tests/failure-runtime.py and the kit's i18n_runtime.py: offscreen Quickshell on the Omarchy 4.5 shell"
else
  run python3 tests/runtime.py
  run python3 tests/failure-runtime.py
  # The runtime tests find the kit the same way.
  run python3 "${FOAMY_KIT:-$HOME/GitHub/Personal/foamy-kit}/preview/i18n_runtime.py" .
fi
exit $status
