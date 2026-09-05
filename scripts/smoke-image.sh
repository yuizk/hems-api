#!/usr/bin/env bash
# Run an existing image against real Chrome without access to any device.
set -euo pipefail

if [[ $# != 1 || -z "$1" ]]; then
  echo 'usage: scripts/smoke-image.sh <image-ref>' >&2
  exit 1
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Avoid chrome/chromium in the target path: runtime scans process cmdlines.
docker run --rm --init --network none \
  --security-opt seccomp=unconfined --shm-size 256m \
  -v "${SCRIPT_DIR}/chrome-smoke.py:/tmp/hems-image-smoke.py:ro" \
  --entrypoint python3 "$1" /tmp/hems-image-smoke.py
