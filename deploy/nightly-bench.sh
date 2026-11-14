#!/usr/bin/env bash
# R7: run the quality bench for the primary with the live service paused, then
# check for a regression vs the best prior run. Meant for a weekly timer; NOT
# auto-enabled (it pauses magic.service for a few minutes while the model
# loads). Install the timer yourself when you want it.
#
#   deploy/nightly-bench.sh
set -uo pipefail
ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
cd "$ROOT" || exit 1

# the bench loads the model, so run it through heavy.sh (pauses the daemon)
scripts/heavy.sh venv/bin/python bench/quality.py --model "${1:-magic}" || true

# cheap, no model: compare the newest result to the best prior run
venv/bin/python bench/regression.py
rc=$?
if [ "$rc" -ne 0 ]; then
    echo "nightly-bench: regression recorded in data/bench-regression.txt (magic self-audit will show it)"
fi
exit 0
