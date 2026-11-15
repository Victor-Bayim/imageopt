#!/usr/bin/env bash
# sampler_run.sh
# DEVELOPER HELPER
# NOT THE PRIMARY PRODUCT ENTRYPOINT
# Prefer `scripts/oneclick_product.py sample|run` for formal product flows.
set -euo pipefail
# 示例运行采样器脚本
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BIN="$ROOT/sampler/user/sampler_container.sh"
if [[ ! -x "$BIN" ]]; then
  echo "sampler binary not found, run build_bpf.sh first"; exit 2; fi

usage() {
  cat <<EOF
Usage: $(basename "$0") (--container NAME | --pid PID) [options]
Options:
  --sampling-rate N     (0..100, default 100)
  --mmap-window SEC     (default 0=disable)
  --prom-port PORT      (default 8000)
  --redis-url URL       (default redis://127.0.0.1:6379/0)
EOF
}
# defaults
CID=""; PID=0; RATE=100; MWIN=0; PPORT=8000; REDIS="redis://127.0.0.1:6379/0"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --container) CID="$2"; shift 2;;
    --pid) PID="$2"; shift 2;;
    --sampling-rate) RATE="$2"; shift 2;;
    --mmap-window) MWIN="$2"; shift 2;;
    --prom-port) PPORT="$2"; shift 2;;
    --redis-url) REDIS="$2"; shift 2;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown arg: $1"; usage; exit 1;;
  esac
done
[[ -n "$CID" || "$PID" -gt 0 ]] || { usage; exit 1; }

OPTS=()
[[ -n "$CID" ]] && OPTS+=(--container "$CID")
[[ "$PID" -gt 0 ]] && OPTS+=(--pid "$PID")
OPTS+=(--sampling-rate "$RATE" --mmap-window "$MWIN" --prom-port "$PPORT" --redis-url "$REDIS")
exec sudo "$BIN" "${OPTS[@]}"
