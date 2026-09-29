#!/bin/bash
# Run the release/update machinery test harnesses (no network, no root:
# GitHub API and downloads are mocked; paths are redirected to a temp dir).
# Run from the repo root:
#   bash tests/run.sh
set -u
cd "$(dirname "$0")/.."
rc=0
for t in tests/release-logic.sh tests/update-flow.sh; do
    echo "### $t"
    bash "$t" || rc=1
    echo
done
exit $rc
