#!/usr/bin/env bash
# Offline regression gate: synthetic assets, fake credentials, mocked cloud API.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ "$(uname -s)" == Linux ]] || {
  echo "The atomic restore and process supervision tests require Linux."
  exit 2
}
command -v python3 >/dev/null
command -v jq >/dev/null
for script in bin/* tests/*.sh; do
  if head -n 1 "$script" | grep -q 'env bash'; then
    bash -n "$script"
  fi
done
python3 -m py_compile bin/*.py tests/*.py
python3 -m unittest discover -s tests -p test_watchdog.py -v
python3 -m unittest discover -s tests -p test_archive_safety.py -v
python3 -m unittest discover -s tests -p test_pod_supervisor.py -v
python3 -m unittest discover -s tests -p test_controller.py -v
bash tests/test_gpu_unavailable.sh
bash tests/test_startup_preflight.sh
python3 tests/test_migration.py
printf '\nPASS: offline regression gate completed. No live cloud actions were used.\n'
