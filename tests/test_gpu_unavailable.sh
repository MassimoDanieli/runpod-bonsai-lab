#!/usr/bin/env bash
# Offline regression test: a rejected start must never create a replacement Pod.
set -euo pipefail
source_script="$(cd "$(dirname "$0")/.." && pwd)/bin/runpod-bonsai"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin"
cat > "$tmp/runpod.env" <<'EOF'
RUNPOD_API_KEY=test-only
RUNPOD_POD_ID=offline-test
EOF
cat > "$tmp/bin/curl" <<'EOF'
#!/usr/bin/env bash
out=""
code=false
post=false
while (($#)); do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -w) code=true; shift 2 ;;
    -X) [[ "$2" == POST ]] && post=true; shift 2 ;;
    *) shift ;;
  esac
done
if "$post"; then
  printf '%s\n' '{"detail":"There are not enough free GPUs on the host machine to start this pod.","status":400}' > "$out"
  echo 400
else
  echo '{"id":"offline-test","name":"test","status":"EXITED"}'
fi
EOF
chmod +x "$tmp/bin/curl"
set +e
output="$(printf 'yes\n' | RUNPOD_BONSAI_CONFIG="$tmp/runpod.env" PATH="$tmp/bin:$PATH" bash "$source_script" start 2>&1)"
result=$?
set -e
[[ $result -eq 3 ]] || { printf 'Unexpected exit code: %s\n%s\n' "$result" "$output"; exit 1; }
[[ "$output" == *"GPU_UNAVAILABLE:"* ]] || { echo "Missing GPU_UNAVAILABLE diagnostic"; exit 1; }
[[ "$output" == *"No additional Pod was created."* ]] || { echo "Missing safety diagnostic"; exit 1; }
printf '%s\n' "PASS: unavailable GPU is reported; no retry or new Pod request."
