#!/usr/bin/env bash
# Offline tests for the read-only startup hook preflight.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/workspace"
start="$tmp/start.sh"
hook="$tmp/post_start.sh"
run() {
  RUNPOD_PREFLIGHT_START="$start" \
  RUNPOD_PREFLIGHT_HOOK="$hook" \
  RUNPOD_PREFLIGHT_WORKSPACE="$tmp/workspace" \
  bash "$root/bin/runpod-startup-preflight"
}
expect_status() {
  local expected="$1" status=0
  run > "$tmp/out" 2>&1 || status=$?
  if [[ "$status" -ne "$expected" ]]; then
    printf 'Expected status %s, got %s\n' "$expected" "$status" >&2
    cat "$tmp/out" >&2
    exit 1
  fi
}

expect_status 2
printf '# example only: %s\n' "$hook" > "$start"
expect_status 3
printf 'execute_script "%s"\n' "$hook" > "$start"
expect_status 0
touch "$hook"
expect_status 0
grep -q 'EXISTS: hook path' "$tmp/out"
rm -rf "$tmp/workspace"
expect_status 4
echo "PASS: startup preflight rejects missing/comment-only hook, detects existing hook, requires workspace."
