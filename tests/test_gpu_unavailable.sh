#!/usr/bin/env bash
# Keep the original offline entrypoint; shared Python client replaces mock curl.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m unittest discover -s tests -p test_controller.py -k capacity_exit_three_no_retry -v
printf '%s\n' 'PASS: unavailable GPU is reported; no retry or new Pod request.'
