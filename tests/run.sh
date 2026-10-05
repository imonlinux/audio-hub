#!/bin/bash
# Run the test harnesses from the repo root:
#   bash tests/run.sh
#
# - release-logic.sh / update-flow.sh: bash harnesses for the bootstrap and
#   self-update machinery (GitHub API and downloads mocked; no network,
#   no root).
# - test_device_selection.py / test_selection_reconcile.py: hubd device
#   selection unit + integration tests (need .venv with pulsectl,
#   paho-mqtt, evdev — the same system packages the Pi installs).
set -euo pipefail
cd "$(dirname "$0")/.."
rc=0
for t in tests/release-logic.sh tests/update-flow.sh; do
    echo "### $t"
    # pipefail: the harness's exit code must survive the tail pipe
    bash "$t" 2>/dev/null | tail -1 || rc=1
    echo
done
if [ -x .venv/bin/python ]; then
    for t in tests/test_device_selection.py tests/test_selection_reconcile.py; do
        echo "### $t"
        .venv/bin/python "$t" 2>&1 | tail -1 || rc=1
        echo
    done
else
    echo "(.venv missing — skipping hubd python tests)"
fi
exit $rc
