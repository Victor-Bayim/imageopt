#!/usr/bin/env bash
# oneclick_esgz_optimize_with_llm.sh
# LEGACY
# READ-ONLY REFERENCE
# NOT THE FORMAL V1 ENTRYPOINT
# Historical shell oneclick kept for reference and compatibility only.

set -Eeuo pipefail

# ========= 基本参数 =========
IMAGE="${1:-}"
shift || true

PROM_PORT_DEFAULT=8000
PROM_SCAN_MAX=400
PROM_RANDOM_FALLBACK=1

DURATION=90
MMAP_WINDOW=120
SIZE_BUDGET_PERCENT=6.4
SIZE_BUDGET_MB=0
MIN_HITS=2
P0_MAX=24
CG_REFRESH=2
INJECT_IO=0
MIN_EVENTS=30
MAX_MMAP_MAPS_RATIO=0.50
STRICT_QUALITY=1
EARLY_ONEHIT_RATIO=0.12
EARLY_ONEHIT_MAX=8
EARLY_ONEHIT_MAX_SIZE_KB=512
REDIS_URL_DEFAULT="redis://127.0.0.1:6379/0"
API_BASE_DEFAULT="https://api.groq.com/openai/v1"
MODEL_DEFAULT="openai/gpt-oss-120b"
READY_URL_DEFAULT=""
READY_TIMEOUT_DEFAULT=30
PREDICT_URL_DEFAULT=""
PREDICT_BODY_DEFAULT=""
KEEP_CONTAINER=0
FORCE_PROM_KILL=0
USE_LLM=0
STARTUP_MODE="auto"
USE_DEFAULT_INJECT_IO_CMD=0
INJECT_IO_CMD=""
PREDICT_BODY_FILE=""
RUN_ENV_CHECK=1
WORK_DIR_OVERRIDE=""

# ========= 解析 flags =========
PROM_PORT="${PROM_PORT_DEFAULT}"
REDIS_URL="${REDIS_URL_DEFAULT}"
API_BASE="${API_BASE_DEFAULT}"
MODEL="${MODEL_DEFAULT}"
READY_URL="${READY_URL_DEFAULT}"
READY_TIMEOUT="${READY_TIMEOUT_DEFAULT}"
PREDICT_URL="${PREDICT_URL_DEFAULT}"
PREDICT_BODY="${PREDICT_BODY_DEFAULT}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prom-port) PROM_PORT="${2:?}"; shift 2 ;;
    --duration)  DURATION="${2:?}"; shift 2 ;;
    --mmap-window) MMAP_WINDOW="${2:?}"; shift 2 ;;
    --redis-url) REDIS_URL="${2:?}"; shift 2 ;;
    --api-base)  API_BASE="${2:?}"; shift 2 ;;
    --model)     MODEL="${2:?}"; shift 2 ;;
    --ready-url) READY_URL="${2:?}"; shift 2 ;;
    --ready-timeout) READY_TIMEOUT="${2:?}"; shift 2 ;;
    --no-ready-probe) READY_URL=""; shift ;;
    --predict-url) PREDICT_URL="${2:?}"; shift 2 ;;
    --predict-body) PREDICT_BODY="${2:?}"; shift 2 ;;
    --predict-body-file) PREDICT_BODY_FILE="${2:?}"; shift 2 ;;
    --inject-io-cmd) INJECT_IO_CMD="${2:?}"; shift 2 ;;
    --no-default-inject-io-cmd) USE_DEFAULT_INJECT_IO_CMD=0; shift ;;
    --no-env-check) RUN_ENV_CHECK=0; shift ;;
    --work-dir) WORK_DIR_OVERRIDE="${2:?}"; shift 2 ;;
    --size-budget-percent) SIZE_BUDGET_PERCENT="${2:?}"; shift 2 ;;
    --size-budget-mb) SIZE_BUDGET_MB="${2:?}"; shift 2 ;;
    --min-hits)  MIN_HITS="${2:?}"; shift 2 ;;
    --p0-max)    P0_MAX="${2:?}"; shift 2 ;;
    --cg-refresh) CG_REFRESH="${2:?}"; shift 2 ;;
    --inject-io) INJECT_IO=1; shift ;;
    --min-events) MIN_EVENTS="${2:?}"; shift 2 ;;
    --max-mmap-maps-ratio) MAX_MMAP_MAPS_RATIO="${2:?}"; shift 2 ;;
    --allow-low-fidelity) STRICT_QUALITY=0; shift ;;
    --early-onehit-ratio) EARLY_ONEHIT_RATIO="${2:?}"; shift 2 ;;
    --early-onehit-max) EARLY_ONEHIT_MAX="${2:?}"; shift 2 ;;
    --early-onehit-max-size-kb) EARLY_ONEHIT_MAX_SIZE_KB="${2:?}"; shift 2 ;;
    --keep-container) KEEP_CONTAINER=1; shift ;;
    --force-prom-port) FORCE_PROM_KILL=1; shift ;;
    --llm) USE_LLM=1; shift ;;
    --no-llm) USE_LLM=0; shift ;;
    --legacy-startup) STARTUP_MODE="legacy"; shift ;;
    --gated-startup) STARTUP_MODE="gated"; shift ;;
    *) echo "[ERR] Unknown arg: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "${IMAGE}" ]]; then
  cat >&2 <<USAGE
用法:
  sudo -E scripts/oneclick_esgz_optimize_with_llm.sh <eStargz镜像名> \\
      [--prom-port N] [--duration S] [--mmap-window S] \\
      [--redis-url URL] [--api-base URL] [--model ID] \\
      [--ready-url URL|--no-ready-probe] [--ready-timeout SEC] \\
      [--predict-url URL] [--predict-body JSON|--predict-body-file PATH] \\
      [--inject-io-cmd CMD] [--no-default-inject-io-cmd] [--no-env-check] [--work-dir DIR] \\
      [--size-budget-percent PCT] [--size-budget-mb MB] \\
      [--min-hits N] [--p0-max N] [--cg-refresh SEC] \\
      [--inject-io] [--min-events N] [--max-mmap-maps-ratio R] [--allow-low-fidelity] \\
      [--early-onehit-ratio R] [--early-onehit-max N] [--early-onehit-max-size-kb KB] \\
      [--keep-container] [--force-prom-port] [--llm|--no-llm] [--legacy-startup|--gated-startup]

说明:
  - 这是历史 oneclick shell，仅保留参考、兼容测试和旧 work dir 对照价值。
  - 正式推荐入口：sudo -E python3 scripts/oneclick_product.py run <IMAGE> [--work-dir DIR]
USAGE
  exit 1
fi

if [[ -n "${PREDICT_BODY_FILE}" ]]; then
  [[ -f "${PREDICT_BODY_FILE}" ]] || { err "predict body 文件不存在：${PREDICT_BODY_FILE}"; exit 1; }
  PREDICT_BODY="$(< "${PREDICT_BODY_FILE}")"
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -n "${WORK_DIR_OVERRIDE}" ]]; then
  WORK="${WORK_DIR_OVERRIDE}"
else
  WORK="${ROOT}/optimizer/work/$(date +%Y%m%d_%H%M%S)"
fi
TOOLS_DIR="${ROOT}/optimizer/tools"
mkdir -p "${WORK}"
RUN_ID="$(basename "${WORK}")"
REDIS_KEY="file_open_events:${RUN_ID}"
LOG="${WORK}/sampler.log"
SAMPLER_METRICS="${WORK}/sampler_metrics.prom"
RAW_EVENTS="${WORK}/raw_events.jsonl"
MANIFEST_JSON="${WORK}/manifest.json"
ENV_CHECK_JSON="${WORK}/env_check.json"
GATE_DIR="${WORK}/startup_gate"
GATE_RELEASE_FILE="${GATE_DIR}/release"
GATE_WRAPPER="${WORK}/startup_gate.sh"
GATE_LAUNCHER="${WORK}/startup_gate_launcher"
IMAGE_INSPECT_JSON="${WORK}/image_inspect.json"
STARTUP_MODE_USED="legacy"
STARTUP_BACKEND="legacy"
STARTUP_BACKEND_SELECTION_REASON="legacy startup mode requested"
EVENT_SINK_USED="none"

IMAGE_SAFE="$(echo "${IMAGE}" | sed 's,[:/],-,g')"
IMAGE_SAFE="${IMAGE_SAFE:0:40}"
CNAME="${IMAGE_SAFE}-${RUN_ID}"

log()  { echo -e "$*"; }
warn() { echo -e "[WARN] $*" >&2; }
err()  { echo -e "[ERR]  $*" >&2; }
need_cmd() { command -v "$1" >/dev/null 2>&1 || { err "缺少命令：$1"; exit 1; }; }

legacy_inject_io_cmd() {
  cat <<'EOF'
read_file() {
  local f="$1"
  if [ -f "$f" ] && [ -r "$f" ]; then
    dd if="$f" of=/dev/null bs=1M count=8 status=none 2>/dev/null || head -c 131072 "$f" >/dev/null 2>&1
  fi
}
for f in \
  /etc/ld.so.cache \
  /etc/os-release \
  /etc/nsswitch.conf \
  /etc/passwd \
  /lib/x86_64-linux-gnu/libc.so.6 \
  /lib/x86_64-linux-gnu/libdl.so.2 \
  /lib/x86_64-linux-gnu/libpthread.so.0 \
  /lib/x86_64-linux-gnu/libselinux.so.1 \
  /lib/x86_64-linux-gnu/libpcre2-8.so.0 \
  /usr/lib/x86_64-linux-gnu/libnss_files.so.2; do
  read_file "$f"
done
if [ -d /models/half_plus_two ]; then
  find /models/half_plus_two -maxdepth 2 -type f -print 2>/dev/null | head -n 32 | while read -r mf; do
    read_file "$mf"
  done
fi
EOF
}

run_in_container_sh() {
  local cmd="$1"
  nerdctl exec "${CNAME}" sh -lc "${cmd}" >/dev/null 2>&1
}

CHECK_ENV_SCRIPT="${ROOT}/scripts/check_env.sh"
if [[ "${RUN_ENV_CHECK}" -eq 1 ]]; then
  [[ -f "${CHECK_ENV_SCRIPT}" ]] || { err "缺少环境检查脚本：${CHECK_ENV_SCRIPT}"; exit 1; }
  log "[i] 运行环境预检查 -> ${ENV_CHECK_JSON}"
  if ! bash "${CHECK_ENV_SCRIPT}" --profile oneclick --image "${IMAGE}" --json-out "${ENV_CHECK_JSON}"; then
    err "环境预检查失败，详见 ${ENV_CHECK_JSON}"
    exit 1
  fi
else
  warn "已跳过环境预检查（--no-env-check）"
fi

need_cmd nerdctl; need_cmd curl; need_cmd awk; need_cmd sed; need_cmd python3; need_cmd ss

image_supports_gated_shell() {
  nerdctl run --rm --pull=never --net=none --entrypoint /bin/sh "${IMAGE}" -lc 'test -x /bin/sh' >/dev/null 2>&1
}

find_host_launcher_compiler() {
  local preferred="${CPP_SAMPLER_CC:-}"
  if [[ -n "${preferred}" ]]; then
    if [[ -x "${preferred}" ]]; then
      echo "${preferred}"
      return 0
    fi
    if command -v "${preferred}" >/dev/null 2>&1; then
      command -v "${preferred}"
      return 0
    fi
    return 1
  fi
  local cc
  for cc in cc gcc clang; do
    if command -v "${cc}" >/dev/null 2>&1; then
      command -v "${cc}"
      return 0
    fi
  done
  return 1
}

# ========= 选择工具脚本 =========
find_tool() {
  local name="$1"; shift || true
  local candidates=("$@")
  for p in "${candidates[@]}"; do
    if [[ -f "$p" ]]; then echo "$p"; return 0; fi
  done
  return 1
}

EXPORT_EVENTS="$(find_tool export_events.py \
  "${TOOLS_DIR}/export_events.py")" || { err "缺少工具：optimizer/tools/export_events.py"; exit 1; }
SNAPSHOT_MAPS="$(find_tool snapshot_maps.py \
  "${TOOLS_DIR}/snapshot_maps.py")" || { err "缺少工具：optimizer/tools/snapshot_maps.py"; exit 1; }
BUILD_DEVINO="$(find_tool build_devino_dict.py \
  "${TOOLS_DIR}/build_devino_dict.py")" || { err "缺少工具：optimizer/tools/build_devino_dict.py"; exit 1; }
ENRICH_MMAP="$(find_tool enrich_mmap_events.py \
  "${TOOLS_DIR}/enrich_mmap_events.py")" || { err "缺少工具：optimizer/tools/enrich_mmap_events.py"; exit 1; }
AGGREGATE="$(find_tool aggregate_candidates.py \
  "${ROOT}/optimizer/aggregate_candidates.py" \
  "${TOOLS_DIR}/aggregate_candidates.py")" || { err "缺少工具：optimizer/aggregate_candidates.py 或 tools/aggregate_candidates.py"; exit 1; }
SELECT_PREFETCH="$(find_tool select_prefetch_paths.py \
  "${TOOLS_DIR}/select_prefetch_paths.py")" || { err "缺少工具：optimizer/tools/select_prefetch_paths.py"; exit 1; }
QUALITY_GATE_TOOL="$(find_tool quality_gate.py \
  "${TOOLS_DIR}/quality_gate.py")" || { err "缺少工具：optimizer/tools/quality_gate.py"; exit 1; }
BUILD_LLM_PROMPT="$(find_tool build_llm_prompt.py \
  "${TOOLS_DIR}/build_llm_prompt.py")" || { err "缺少工具：optimizer/tools/build_llm_prompt.py"; exit 1; }
PREPARE_PREFERRED="$(find_tool prepare_preferred_paths.py \
  "${TOOLS_DIR}/prepare_preferred_paths.py")" || { err "缺少工具：optimizer/tools/prepare_preferred_paths.py"; exit 1; }

# ========= Redis：用传入的 REDIS_URL 探活 =========
USE_REDIS=0
if command -v redis-cli >/dev/null 2>&1; then
  if ! redis-cli -u "${REDIS_URL}" PING >/dev/null 2>&1; then
    warn "Redis 不可用：${REDIS_URL}（继续使用 file sink 主路径）"
  else
    USE_REDIS=1
    redis-cli -u "${REDIS_URL}" DEL "${REDIS_KEY}" >/dev/null || true
    log "[i] 已清空 Redis 列表 ${REDIS_KEY} ：${REDIS_URL}"
  fi
else
  warn "缺少 redis-cli；继续使用 file sink 主路径"
fi

# ========= 端口探测 =========
port_in_use() {
  local p="$1"
  if ss -ltnp | awk -v pat=":$p" '$4 ~ pat {found=1} END{exit found?0:1}'; then
    return 0
  else
    return 1
  fi
}
kill_port_owner() {
  local p="$1"
  local pid
  pid="$(ss -ltnp | awk -v pat=":$p" '$4 ~ pat {print $6}' | sed -n 's/.*pid=\([0-9]\+\).*/\1/p' | head -1 || true)"
  if [[ -n "${pid}" ]]; then
    warn "强制释放端口 ${p}，kill PID=${pid}"
    kill -TERM "${pid}" 2>/dev/null || true
    sleep 0.5
    if port_in_use "${p}"; then
      kill -KILL "${pid}" 2>/dev/null || true
      sleep 0.3
    fi
  fi
}
choose_prom_port() {
  local start="${1}" maxscan="${2}" p
  for ((i=0;i<maxscan;i++)); do
    p=$((start+i))
    if ! port_in_use "${p}"; then echo "${p}"; return 0; fi
  done
  if [[ "${PROM_RANDOM_FALLBACK}" -eq 1 ]]; then
    for _ in {1..100}; do
      p=$((15000 + (RANDOM % 10000)))
      if ! port_in_use "${p}"; then echo "${p}"; return 0; fi
    done
  fi
  return 1
}
if [[ "${FORCE_PROM_KILL}" -eq 1 ]] && port_in_use "${PROM_PORT}"; then
  kill_port_owner "${PROM_PORT}"
fi
PROM_CAND="$(choose_prom_port "${PROM_PORT}" "${PROM_SCAN_MAX}")" || { err "找不到可用 Prometheus 端口"; exit 1; }
PROM_PORT="${PROM_CAND}"

prepare_gated_wrapper() {
  mkdir -p "${GATE_DIR}"
  rm -f "${GATE_RELEASE_FILE}"
  nerdctl image inspect "${IMAGE}" > "${IMAGE_INSPECT_JSON}" 2>> "${LOG}" || return 1
  python3 - "${IMAGE_INSPECT_JSON}" "${GATE_WRAPPER}" <<'PY'
import json
import os
import shlex
import sys

inspect_path, wrapper_path = sys.argv[1:3]
with open(inspect_path, "r", encoding="utf-8") as f:
    data = json.load(f)
obj = data[0] if isinstance(data, list) else data
cfg = obj.get("Config") or {}
entry = cfg.get("Entrypoint") or []
cmd = cfg.get("Cmd") or []
if isinstance(entry, str):
    entry = [entry]
if isinstance(cmd, str):
    cmd = [cmd]
argv = list(entry) + list(cmd)
if not argv:
    raise SystemExit("image has empty Entrypoint+Cmd; gated startup unsupported")

with open(wrapper_path, "w", encoding="utf-8") as out:
    out.write("#!/bin/sh\n")
    out.write("set -eu\n")
    out.write("RELEASE_FILE=/__cpp_sampler_gate/release\n")
    out.write("while [ ! -f \"$RELEASE_FILE\" ]; do sleep 0.05; done\n")
    out.write("exec " + " ".join(shlex.quote(arg) for arg in argv) + "\n")
os.chmod(wrapper_path, 0o755)
PY
}

load_gated_entry_argv() {
  nerdctl image inspect "${IMAGE}" > "${IMAGE_INSPECT_JSON}" 2>> "${LOG}" || return 1
  python3 - "${IMAGE_INSPECT_JSON}" <<'PY'
import json
import sys

inspect_path = sys.argv[1]
with open(inspect_path, "r", encoding="utf-8") as f:
    data = json.load(f)
obj = data[0] if isinstance(data, list) else data
cfg = obj.get("Config") or {}
entry = cfg.get("Entrypoint") or []
cmd = cfg.get("Cmd") or []
if isinstance(entry, str):
    entry = [entry]
if isinstance(cmd, str):
    cmd = [cmd]
argv = list(entry) + list(cmd)
if not argv:
    raise SystemExit("image has empty Entrypoint+Cmd; gated startup unsupported")
for arg in argv:
    print(arg)
PY
}

build_host_gate_launcher() {
  local allow_dynamic="${1:-1}"
  local quiet_failure="${2:-0}"
  local compiler source
  source="${ROOT}/orchestrator/assets/startup_gate_launcher.c"
  [[ -f "${source}" ]] || {
    [[ "${quiet_failure}" -eq 1 ]] || warn "宿主侧 gated startup launcher 源码缺失：${source}"
    return 1
  }
  compiler="$(find_host_launcher_compiler)" || {
    [[ "${quiet_failure}" -eq 1 ]] || warn "宿主侧 gated startup launcher 不可用：缺少 cc/gcc/clang"
    return 1
  }

  if "${compiler}" -O2 -static -s -o "${GATE_LAUNCHER}" "${source}" >/dev/null 2>> "${LOG}"; then
    chmod +x "${GATE_LAUNCHER}"
    STARTUP_BACKEND="gated_host_launcher_static"
    [[ "${quiet_failure}" -eq 1 ]] || warn "gated startup 使用宿主侧 launcher backend（static）"
    return 0
  fi
  if [[ "${allow_dynamic}" -eq 1 ]] && "${compiler}" -O2 -s -o "${GATE_LAUNCHER}" "${source}" >/dev/null 2>> "${LOG}"; then
    chmod +x "${GATE_LAUNCHER}"
    STARTUP_BACKEND="gated_host_launcher_dynamic"
    warn "gated startup 使用实验性的宿主侧 launcher backend（dynamic）；要求容器运行时 libc 兼容"
    return 0
  fi
  [[ "${quiet_failure}" -eq 1 ]] || warn "宿主侧 gated startup launcher 构建失败；详见 ${LOG}"
  return 1
}

start_container_legacy() {
  STARTUP_MODE_USED="legacy"
  STARTUP_BACKEND="legacy"
  STARTUP_BACKEND_SELECTION_REASON="legacy startup mode requested"
  if [[ -n "${READY_URL}" ]]; then
    log "[+] [1/7] 冷启动 eStargz 容器：${CNAME}（镜像=${IMAGE}，host 网络，ready probe=${READY_URL}）"
  else
    log "[+] [1/7] 冷启动 eStargz 容器：${CNAME}（镜像=${IMAGE}，host 网络，无 ready probe）"
  fi
  nerdctl run -d --name "${CNAME}" --network=host "${IMAGE}" >/dev/null
}

start_container_gated() {
  STARTUP_MODE_USED="gated"
  log "[+] [1/7] 冷启动 eStargz 容器：${CNAME}（镜像=${IMAGE}，gated startup，host 网络）"
  if image_supports_gated_shell; then
    if build_host_gate_launcher 0 1; then
      mkdir -p "${GATE_DIR}"
      rm -f "${GATE_RELEASE_FILE}"
      local -a image_argv=()
      mapfile -t image_argv < <(load_gated_entry_argv) || return 1
      [[ "${#image_argv[@]}" -gt 0 ]] || return 1
      warn "gated startup 自动优先使用宿主侧 launcher backend（static）"
      STARTUP_BACKEND_SELECTION_REASON="auto selected static host launcher before image-shell gating"
      nerdctl create --name "${CNAME}" --network=host \
        --mount "type=bind,src=${GATE_DIR},dst=/__cpp_sampler_gate,readonly" \
        --mount "type=bind,src=${GATE_LAUNCHER},dst=/__cpp_sampler_gate_launcher,readonly" \
        --entrypoint /__cpp_sampler_gate_launcher "${IMAGE}" /__cpp_sampler_gate/release "${image_argv[@]}" >/dev/null 2>> "${LOG}" || return 1
    else
      STARTUP_BACKEND="gated_shell"
      STARTUP_BACKEND_SELECTION_REASON="auto kept image-shell gating because static host launcher is unavailable"
      prepare_gated_wrapper || return 1
      nerdctl create --name "${CNAME}" --network=host \
        --mount "type=bind,src=${GATE_DIR},dst=/__cpp_sampler_gate,readonly" \
        --mount "type=bind,src=${GATE_WRAPPER},dst=/__cpp_sampler_gate.sh,readonly" \
        --entrypoint /bin/sh "${IMAGE}" /__cpp_sampler_gate.sh >/dev/null 2>> "${LOG}" || return 1
    fi
  else
    mkdir -p "${GATE_DIR}"
    rm -f "${GATE_RELEASE_FILE}"
    build_host_gate_launcher 1 0 || return 1
    STARTUP_BACKEND_SELECTION_REASON="image lacks /bin/sh; host launcher is the only gated startup backend"
    local -a image_argv=()
    mapfile -t image_argv < <(load_gated_entry_argv) || return 1
    [[ "${#image_argv[@]}" -gt 0 ]] || return 1
    nerdctl create --name "${CNAME}" --network=host \
      --mount "type=bind,src=${GATE_DIR},dst=/__cpp_sampler_gate,readonly" \
      --mount "type=bind,src=${GATE_LAUNCHER},dst=/__cpp_sampler_gate_launcher,readonly" \
      --entrypoint /__cpp_sampler_gate_launcher "${IMAGE}" /__cpp_sampler_gate/release "${image_argv[@]}" >/dev/null 2>> "${LOG}" || return 1
  fi
  nerdctl start "${CNAME}" >/dev/null 2>> "${LOG}" || return 1
  sleep 0.2
  [[ "$(nerdctl inspect -f '{{.State.Running}}' "${CNAME}" 2>/dev/null || echo false)" == "true" ]] || return 1
}

launch_container() {
  case "${STARTUP_MODE}" in
    legacy)
      start_container_legacy
      ;;
    gated)
      start_container_gated || { err "gated startup 失败；可改用 --legacy-startup"; return 1; }
      ;;
    auto)
      if ! start_container_gated; then
        warn "gated startup 不可用，回退 legacy startup"
        nerdctl stop "${CNAME}" >/dev/null 2>&1 || true
        nerdctl rm   "${CNAME}" >/dev/null 2>&1 || true
        start_container_legacy
        STARTUP_BACKEND_SELECTION_REASON="auto startup mode fell back to legacy because gated startup was unavailable"
      fi
      ;;
    *)
      err "未知 startup mode: ${STARTUP_MODE}"
      return 1
      ;;
  esac
}

# ========= 启动容器（host 网络，冷启动） =========
cleanup() {
  if [[ -n "${SAMPLER_PID:-}" ]] && kill -0 "${SAMPLER_PID}" 2>/dev/null; then
    kill "${SAMPLER_PID}" 2>/dev/null || true
  fi
  if [[ "${KEEP_CONTAINER}" -ne 1 ]] && nerdctl ps -a --format '{{.Names}}' | grep -qx "${CNAME}"; then
    nerdctl stop "${CNAME}" >/dev/null 2>&1 || true
    nerdctl rm   "${CNAME}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

if nerdctl ps -a --format '{{.Names}}' | grep -qx "${CNAME}"; then
  nerdctl stop "${CNAME}" >/dev/null 2>&1 || true
  nerdctl rm   "${CNAME}" >/dev/null 2>&1 || true
fi

launch_container || { err "容器启动失败：${CNAME}"; exit 1; }

PID="$(nerdctl inspect -f '{{.State.Pid}}' "${CNAME}" 2>/dev/null || echo "")"
if [[ -z "${PID}" || "${PID}" = "0" ]]; then
  err "容器 PID 获取失败：${CNAME}"; exit 1
fi
log "[+] 容器 PID = ${PID}  运行时=nerdctl  网络=host  startup=${STARTUP_MODE_USED}"

# ========= 启动采样器 =========
SAMPLER_BIN="${CPP_SAMPLER_BIN:-${ROOT}/sampler/user/build/sampler_trace_ext}"
[[ -x "${SAMPLER_BIN}" ]] || { err "采样器不存在或不可执行：${SAMPLER_BIN}"; exit 1; }
: > "${RAW_EVENTS}"

log "[+] [2/7] 启动采样器（startup truth-first：优先覆盖最早期启动访问）"
log "    sampler args: --pid ${PID} --prom-port ${PROM_PORT} --sampling-rate 100 --mmap-window ${MMAP_WINDOW} --cg-refresh ${CG_REFRESH} --file-sink ${RAW_EVENTS}"
if [[ "${USE_REDIS}" -eq 1 ]]; then
  "${SAMPLER_BIN}" \
    --pid "${PID}" \
    --prom-port "${PROM_PORT}" \
    --sampling-rate 100 \
    --mmap-window "${MMAP_WINDOW}" \
    --cg-refresh "${CG_REFRESH}" \
    --file-sink "${RAW_EVENTS}" \
    --redis-url "${REDIS_URL}" \
    --redis-key "${REDIS_KEY}" >> "${LOG}" 2>&1 &
else
  "${SAMPLER_BIN}" \
    --pid "${PID}" \
    --prom-port "${PROM_PORT}" \
    --sampling-rate 100 \
    --mmap-window "${MMAP_WINDOW}" \
    --cg-refresh "${CG_REFRESH}" \
    --file-sink "${RAW_EVENTS}" >> "${LOG}" 2>&1 &
fi
SAMPLER_PID=$!

# /metrics 就绪检测
SAMPLER_READY=0
for _ in {1..60}; do
  if ! kill -0 "${SAMPLER_PID}" 2>/dev/null; then
    err "采样器进程提前退出，日志见：${LOG}"
    exit 1
  fi
  if curl -fsS --noproxy 127.0.0.1 "http://127.0.0.1:${PROM_PORT}/metrics" >/dev/null 2>&1; then
    log "[i] /metrics 就绪"
    SAMPLER_READY=1
    break
  fi
  sleep 1
done
if [[ "${SAMPLER_READY}" -ne 1 ]]; then
  err "采样器 /metrics 未在 60s 内就绪，日志见：${LOG}"
  exit 1
fi

# ========= gated startup 释放 =========
if [[ "${STARTUP_MODE_USED}" == "gated" ]]; then
  : > "${GATE_RELEASE_FILE}"
  log "[i] gated startup 已释放：${GATE_RELEASE_FILE}"
fi

# ========= workload readiness 探活（采样器就绪后） =========
if [[ -n "${READY_URL}" ]]; then
  READY_OK=0
  for ((i=0;i<READY_TIMEOUT;i++)); do
    if curl -fsS --max-time 1 "${READY_URL}" >/dev/null 2>&1; then
      log "[+] workload ready: ${READY_URL}"
      READY_OK=1
      break
    fi
    sleep 1
  done
  if [[ "${READY_OK}" -ne 1 ]]; then
    warn "未在 ${READY_TIMEOUT}s 内观察到 ready URL：${READY_URL}"
  fi
else
  log "[i] 未配置 ready probe：跳过 workload ready 检测"
fi

# ========= 可选注入 I/O（默认关闭：truth-first） =========
if [[ "${INJECT_IO}" -eq 1 ]]; then
  log "[+] [3/7] 注入 I/O：按当前 workload 配置执行"
  DID_IO=0
  if [[ -n "${PREDICT_URL}" ]]; then
    for _ in {1..3}; do
      curl -fsS -X POST "${PREDICT_URL}" \
        -H 'content-type: application/json' \
        --data "${PREDICT_BODY}" >> "${WORK}/inference.json" 2>/dev/null || true
    done
    DID_IO=1
  fi

  if [[ "${USE_DEFAULT_INJECT_IO_CMD}" -eq 1 ]]; then
    run_in_container_sh "$(legacy_inject_io_cmd)" || true
    DID_IO=1
  fi

  if [[ -n "${INJECT_IO_CMD}" ]]; then
    run_in_container_sh "${INJECT_IO_CMD}" || true
    DID_IO=1
  fi

  if [[ "${DID_IO}" -ne 1 ]]; then
    warn "已启用 --inject-io，但未配置 predict URL / 默认容器读 / 自定义注入命令"
  fi
else
  log "[i] truth-first 模式：跳过人工 I/O 注入（可用 --inject-io 启用旧行为）"
fi

# ========= 采样窗口 =========
log "[+] [4/7] 采样 ${DURATION}s ..."
sleep "${DURATION}"

# 采样结束快照
if curl -fsS --noproxy 127.0.0.1 "http://127.0.0.1:${PROM_PORT}/metrics" >/dev/null 2>&1; then
  curl -fsS --noproxy 127.0.0.1 "http://127.0.0.1:${PROM_PORT}/metrics" > "${SAMPLER_METRICS}"
  log "------ Prometheus (${PROM_PORT})（采样结束快照） ------"
  grep -E 'file_events_total|open_events_total|mmap_events_total|ringbuf_drops_total|filtered_out_total|sampling_drops_total|file_events_per_second|file_sink_lines_total|file_sink_bytes_total|file_sink_write_failures_total|redis_queue_dropped_total|redis_publish_failures_total' "${SAMPLER_METRICS}" \
    | sed 's/^/# /' || true
else
  : > "${SAMPLER_METRICS}"
  warn "无法获取 /metrics（端口=${PROM_PORT}）。日志见：${LOG}"
fi

# 停采样器
if kill -0 "${SAMPLER_PID}" 2>/dev/null; then
  kill "${SAMPLER_PID}" 2>/dev/null || true
  wait "${SAMPLER_PID}" 2>/dev/null || true
fi
log "[+] [6/7] 停止采样器 (pid=${SAMPLER_PID})"

# ========= 导出 =========
EVENTS="${WORK}/events.jsonl"
EVENTS_ENRICH="${WORK}/events_enriched.jsonl"
MAPS_PATHS="${WORK}/maps_paths.txt"
MAPFILES_JSON="${WORK}/mapfiles_devino.json"
CWDS_JSON="${WORK}/cwd_snapshot.json"
DEVINO_JSON="${WORK}/devino_dict.json"
CAND="${WORK}/candidates.json"
AGG_STATS="${WORK}/aggregate_stats.json"
RANKED="${WORK}/ranked.yaml"
ESTARGZ_LIST="${WORK}/estargz.list"
SELECTOR_STATS_BASE="${WORK}/selector_stats_default.json"
SELECTOR_STATS_HINT="${WORK}/selector_stats_hints.json"
PREFERRED="${WORK}/preferred_paths.txt"
GROUNDING_AUDIT="${WORK}/llm_grounding_audit.json"
PROMPT="${WORK}/prompt.txt"
LLM_BODY="${WORK}/body.json"
LLM_RESP="${WORK}/llm_response.json"
HINTS_RAW="${WORK}/hints_raw.yaml"
HINTS="${WORK}/hints.yaml"
QUALITY_JSON="${WORK}/quality_gate.json"
printf '{}\n' > "${GROUNDING_AUDIT}"

# 先做进程/映射快照，供相对路径 OPEN 解析与 mmap 回填使用
python3 "${SNAPSHOT_MAPS}" --container-pid "${PID}" --out "${MAPS_PATHS}" --mapfiles-json "${MAPFILES_JSON}" --cwd-json "${CWDS_JSON}" 2>> "${LOG}" || true

# 保存原始事件（未过滤，用于回填）
if [[ -s "${RAW_EVENTS}" ]]; then
  EVENT_SINK_USED="file_sink"
  if [[ "${USE_REDIS}" -eq 1 ]]; then
    redis-cli -u "${REDIS_URL}" DEL "${REDIS_KEY}" >/dev/null 2>&1 || true
  fi
elif [[ "${USE_REDIS}" -eq 1 ]]; then
  warn "file sink 为空，回退 Redis 导出原始事件"
  redis-cli -u "${REDIS_URL}" --raw LRANGE "${REDIS_KEY}" 0 -1 > "${RAW_EVENTS}" || : > "${RAW_EVENTS}"
  redis-cli -u "${REDIS_URL}" DEL "${REDIS_KEY}" >/dev/null 2>&1 || true
  if [[ -s "${RAW_EVENTS}" ]]; then
    EVENT_SINK_USED="redis_sink"
  fi
fi

if [[ -s "${RAW_EVENTS}" ]]; then
  cat "${RAW_EVENTS}" | python3 "${EXPORT_EVENTS}" --cwd-json "${CWDS_JSON}" --container-pid "${PID}" "${EVENTS}"
else
  warn "raw events 为空：file sink / Redis 均未提供有效事件"
  : > "${EVENTS}"
fi

# ======== mmap 回填 A/B/C ========
python3 "${BUILD_DEVINO}"  --container-pid "${PID}" --events "${EVENTS}" --out "${DEVINO_JSON}" 2>> "${LOG}" || true
python3 "${ENRICH_MMAP}"   --raw "${RAW_EVENTS}" --events "${EVENTS}" --devino "${DEVINO_JSON}" --maps "${MAPS_PATHS}" --mapfiles "${MAPFILES_JSON}" \
                           --out "${EVENTS_ENRICH}" --container-pid "${PID}" 2>> "${LOG}" \
  || { warn "enrich 失败，回退到原 events.jsonl"; cp -f "${EVENTS}" "${EVENTS_ENRICH}"; }

if [[ ! -s "${MAPFILES_JSON}" ]]; then
  warn "mapfiles_devino.json 缺失或为空：${MAPFILES_JSON}（MMAP 回填将更多依赖 maps 兜底）"
fi

# 聚合（保频次与先后，优先按 dev:ino 合并 alias）
python3 "${AGGREGATE}" "${EVENTS_ENRICH}" "${CAND}" --stats-json "${AGG_STATS}"

# ======== 采样质量门槛（truth-first） ========
if ! python3 "${QUALITY_GATE_TOOL}" \
  --raw "${RAW_EVENTS}" \
  --events "${EVENTS}" \
  --enriched "${EVENTS_ENRICH}" \
  --metrics "${SAMPLER_METRICS}" \
  --sampler-log "${LOG}" \
  --out "${QUALITY_JSON}" \
  --min-events "${MIN_EVENTS}" \
  --max-mmap-maps-ratio "${MAX_MMAP_MAPS_RATIO}" \
  --run-id "${RUN_ID}" \
  --startup-mode "${STARTUP_MODE_USED}" \
  --startup-backend "${STARTUP_BACKEND}" \
  --event-sink-used "${EVENT_SINK_USED}" \
  --redis-enabled "${USE_REDIS}" \
  --aggregate-stats "${AGG_STATS}" \
  2>> "${LOG}"
then
  if [[ "${STRICT_QUALITY}" -eq 1 ]]; then
    err "采样质量门槛未通过（truth-first）。详情：${QUALITY_JSON}"
    err "可选：提高 --duration，或临时加 --allow-low-fidelity 继续流程。"
    exit 2
  fi
  warn "采样质量门槛未通过，但按 --allow-low-fidelity 继续。详情：${QUALITY_JSON}"
fi

# ===== 计算镜像（容器根）文件总大小，用于换算“6.4%预算” =====
# 若用户显式提供 --size-budget-mb 则尊重用户值；否则用容器视角统计所有常规文件（排除 /proc /sys /dev /run /var/run）字节数
if [[ "${SIZE_BUDGET_MB}" = "0" ]]; then
  FS_TOTAL_BYTES_STR="$(
    ONECLICK_PID="${PID}" python3 - <<'PY'
import os, stat
import sys
pid = os.environ.get('ONECLICK_PID')
if not pid or not pid.isdigit():
    print(0)
    sys.exit(0)
root = f"/proc/{pid}/root"
skip = ("/proc/","/sys/","/dev/","/run/","/var/run/")
total = 0
for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
    # container path for skip check
    rel = dirpath[len(root):] if dirpath.startswith(root) else dirpath
    if not rel.startswith("/"): rel = "/" + rel
    # prune skip dirs
    if any(rel.startswith(s) for s in skip):
        dirnames[:] = []
        continue
    # also prune child dirs about to enter
    dirnames[:] = [d for d in dirnames if not any((rel+"/"+d).startswith(s) for s in skip)]
    for name in filenames:
        relp = rel + ("/" if not rel.endswith("/") else "") + name
        if any(relp.startswith(s) for s in skip):
            continue
        p = os.path.join(dirpath, name)
        try:
            st = os.lstat(p)
            if stat.S_ISREG(st.st_mode):
                total += int(st.st_size)
        except Exception:
            pass
print(total)
PY
  )"
  if [[ -n "${FS_TOTAL_BYTES_STR}" && "${FS_TOTAL_BYTES_STR}" =~ ^[0-9]+$ ]]; then
    FS_TOTAL_BYTES="${FS_TOTAL_BYTES_STR}"
    # 将百分比预算折算为 MB（向上取整，避免预算为 0）
    calc_mb=$(awk -v b="${FS_TOTAL_BYTES}" -v p="${SIZE_BUDGET_PERCENT}" 'BEGIN{mb = (b*p/100.0)/1048576.0; if (mb<1 && mb>0) mb=1; printf("%d", (mb==int(mb)?mb:int(mb)+0)) }')
    if [[ -n "${calc_mb}" && "${calc_mb}" -gt 0 ]]; then
      SIZE_BUDGET_MB="${calc_mb}"
      log "[i] 容器根文件总大小=${FS_TOTAL_BYTES} bytes，预算=${SIZE_BUDGET_MB} MB (~${SIZE_BUDGET_PERCENT}%)"
    else
      warn "预算换算失败，继续使用百分比预算 ${SIZE_BUDGET_PERCENT}%"
    fi
  else
    warn "容器根文件大小统计失败，继续使用百分比预算 ${SIZE_BUDGET_PERCENT}%"
  fi
fi

# 预算内基础排序（用于兜底/提示）
python3 "${SELECT_PREFETCH}" \
  --candidates "${CAND}" \
  --aggregate-stats "${AGG_STATS}" \
  --out-yaml "${RANKED}" \
  --out-list "${ESTARGZ_LIST}" \
  --stats-json "${SELECTOR_STATS_BASE}" \
  --budget-percent "${SIZE_BUDGET_PERCENT}" \
  --budget-mb "${SIZE_BUDGET_MB}" \
  --min-hits "${MIN_HITS}" \
  --p0-max "${P0_MAX}" \
  --early-onehit-ratio "${EARLY_ONEHIT_RATIO}" \
  --early-onehit-max "${EARLY_ONEHIT_MAX}" \
  --early-onehit-max-size-kb "${EARLY_ONEHIT_MAX_SIZE_KB}"

readarray -t SELECTOR_GUARD_INFO < <(
  python3 - "${SELECTOR_STATS_BASE}" <<'PY'
import json
import os
import sys

path = sys.argv[1]
mode = "unknown"
reason = ""
ratio = ""
if os.path.exists(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        mode = str(data.get("identity_guard_mode") or "unknown")
        reason = str(data.get("identity_guard_reason") or "")
        ratio_v = data.get("identity_paths_with_devino_ratio")
        if ratio_v is not None:
            ratio = f"{float(ratio_v):.3f}"
    except Exception:
        pass
print(mode)
print(reason)
print(ratio)
PY
)
SELECTOR_GUARD_MODE="${SELECTOR_GUARD_INFO[0]:-unknown}"
SELECTOR_GUARD_REASON="${SELECTOR_GUARD_INFO[1]:-}"
SELECTOR_GUARD_DEVINO_RATIO="${SELECTOR_GUARD_INFO[2]:-}"
log "[i] selector guard=${SELECTOR_GUARD_MODE}${SELECTOR_GUARD_DEVINO_RATIO:+ devino_ratio=${SELECTOR_GUARD_DEVINO_RATIO}}${SELECTOR_GUARD_REASON:+ reason=${SELECTOR_GUARD_REASON}}"

# ========= Prompt & 调 LLM（可选，deterministic 为主路径） =========
LLM_ENABLED="${USE_LLM}"
if [[ "${LLM_ENABLED}" -eq 1 && -z "${LLM_API_KEY:-}" ]]; then
  warn "未设置 LLM_API_KEY，回退 deterministic ranked.yaml"
  LLM_ENABLED=0
fi
if [[ "${LLM_ENABLED}" -eq 1 ]] && ! command -v jq >/dev/null 2>&1; then
  warn "缺少 jq，回退 deterministic ranked.yaml"
  LLM_ENABLED=0
fi

if [[ "${LLM_ENABLED}" -eq 0 ]]; then
  cp -f "${RANKED}" "${HINTS}"
  cp -f "${SELECTOR_STATS_BASE}" "${SELECTOR_STATS_HINT}" 2>/dev/null || true
  : > "${PROMPT}"
  : > "${LLM_BODY}"
  : > "${LLM_RESP}"
  : > "${HINTS_RAW}"
else
  if [[ "${SIZE_BUDGET_MB}" != "0" ]]; then
    BUDGET_DESC="${SIZE_BUDGET_MB} MB"
  else
    BUDGET_DESC="${SIZE_BUDGET_PERCENT}% 的候选体积"
  fi

  python3 "${BUILD_LLM_PROMPT}" \
    --candidates "${CAND}" \
    --selector-stats "${SELECTOR_STATS_BASE}" \
    --out "${PROMPT}" \
    --budget-desc "${BUDGET_DESC}" \
    --min-hits "${MIN_HITS}" \
    --p0-max "${P0_MAX}" \
    --topn 400

  cat > "${LLM_BODY}" <<EOF
{
  "model": "${MODEL}",
  "temperature": 0,
  "messages": [
    {"role": "system", "content": "You are a precise system that outputs only YAML with P0,P1,P2,P3 lists of absolute file paths. No explanations."},
    {"role": "user", "content": $(jq -Rs '.' < "${PROMPT}")}
  ]
}
EOF

  LLM_OK=1
  HTTP_CODE="$(curl -sS -o "${LLM_RESP}" -w "%{http_code}" \
    -H "Authorization: Bearer ${LLM_API_KEY}" \
    -H "Content-Type: application/json" \
    -X POST "${API_BASE}/chat/completions" \
    --data @"${LLM_BODY}")"

  if [[ "${HTTP_CODE}" != "200" ]]; then
    warn "LLM HTTP ${HTTP_CODE}（详情见：${LLM_RESP}），回退使用 ${RANKED}"
    cp -f "${RANKED}" "${HINTS}"
    cp -f "${SELECTOR_STATS_BASE}" "${SELECTOR_STATS_HINT}" 2>/dev/null || true
    : > "${HINTS_RAW}"
    LLM_OK=0
  else
    jq -r '.choices[0].message.content // ""' "${LLM_RESP}" > "${HINTS_RAW}" || : > "${HINTS_RAW}"
  fi

  if [[ "${LLM_OK}" -eq 1 ]]; then
    # ========= Grounding =========
    python3 "${PREPARE_PREFERRED}" \
      --hints-raw "${HINTS_RAW}" \
      --candidates "${CAND}" \
      --selector-stats "${SELECTOR_STATS_BASE}" \
      --container-pid "${PID}" \
      --out "${PREFERRED}" \
      --audit-json "${GROUNDING_AUDIT}"

    if [[ ! -s "${PREFERRED}" ]]; then
      warn "LLM Grounding 无有效路径，回退 ${RANKED}"
      cp -f "${RANKED}" "${HINTS}"
      cp -f "${SELECTOR_STATS_BASE}" "${SELECTOR_STATS_HINT}" 2>/dev/null || true
    else
      python3 "${SELECT_PREFETCH}" \
        --candidates "${CAND}" \
        --aggregate-stats "${AGG_STATS}" \
        --out-yaml "${HINTS}" \
        --out-list "${ESTARGZ_LIST}" \
        --stats-json "${SELECTOR_STATS_HINT}" \
        --budget-percent "${SIZE_BUDGET_PERCENT}" \
        --budget-mb "${SIZE_BUDGET_MB}" \
        --min-hits "${MIN_HITS}" \
        --p0-max "${P0_MAX}" \
        --early-onehit-ratio "${EARLY_ONEHIT_RATIO}" \
        --early-onehit-max "${EARLY_ONEHIT_MAX}" \
        --early-onehit-max-size-kb "${EARLY_ONEHIT_MAX_SIZE_KB}" \
        --preferred "${PREFERRED}"
    fi
  fi
fi

SELECTOR_HINT_GUARD_ACTIVE=0
SELECTOR_HINT_FALLBACK_USED=0
SELECTOR_HINT_LOW_CONF_SKIPPED=0
SELECTOR_HINT_MAPS_ONLY_SKIPPED=0
if [[ -s "${SELECTOR_STATS_HINT}" ]]; then
  mapfile -t SELECTOR_HINT_INFO < <(
    python3 - "${SELECTOR_STATS_HINT}" <<'PY'
import json
import sys

path = sys.argv[1]
try:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
except Exception:
    print("0")
    print("0")
    print("0")
    print("0")
else:
    print("1" if data.get("preferred_guard_active") else "0")
    print("1" if data.get("preferred_fallback_used") else "0")
    print(int(data.get("preferred_low_confidence_skipped") or 0))
    print(int(data.get("preferred_maps_only_skipped") or 0))
PY
  )
  SELECTOR_HINT_GUARD_ACTIVE="${SELECTOR_HINT_INFO[0]:-0}"
  SELECTOR_HINT_FALLBACK_USED="${SELECTOR_HINT_INFO[1]:-0}"
  SELECTOR_HINT_LOW_CONF_SKIPPED="${SELECTOR_HINT_INFO[2]:-0}"
  SELECTOR_HINT_MAPS_ONLY_SKIPPED="${SELECTOR_HINT_INFO[3]:-0}"
  if [[ "${SELECTOR_HINT_GUARD_ACTIVE}" -eq 1 || "${SELECTOR_HINT_FALLBACK_USED}" -eq 1 ]]; then
    log "[i] hint selector guard_backstop active=${SELECTOR_HINT_GUARD_ACTIVE} fallback=${SELECTOR_HINT_FALLBACK_USED} low_conf_skipped=${SELECTOR_HINT_LOW_CONF_SKIPPED} maps_only_skipped=${SELECTOR_HINT_MAPS_ONLY_SKIPPED}"
  fi
fi

python3 - "${MANIFEST_JSON}" "${WORK}" "${RUN_ID}" "${IMAGE}" "${CNAME}" "${STARTUP_MODE}" "${STARTUP_MODE_USED}" "${STARTUP_BACKEND}" "${STARTUP_BACKEND_SELECTION_REASON}" "${EVENT_SINK_USED}" "${USE_REDIS}" "${PROM_PORT}" "${READY_URL}" "${READY_TIMEOUT}" "${PREDICT_URL}" "${INJECT_IO}" "${USE_DEFAULT_INJECT_IO_CMD}" "${INJECT_IO_CMD}" "${SIZE_BUDGET_PERCENT}" "${SIZE_BUDGET_MB}" "${MIN_HITS}" "${P0_MAX}" "${LLM_ENABLED}" "${MODEL}" "${API_BASE}" "${RUN_ENV_CHECK}" <<'PY'
import json
import os
import sys

(
    out_path,
    work_dir,
    run_id,
    image,
    container_name,
    startup_requested,
    startup_used,
    startup_backend,
    startup_backend_selection_reason,
    event_sink_used,
    redis_enabled_s,
    prom_port_s,
    ready_url,
    ready_timeout_s,
    predict_url,
    inject_io_s,
    default_inject_io_s,
    inject_io_cmd,
    size_budget_percent_s,
    size_budget_mb_s,
    min_hits_s,
    p0_max_s,
    llm_enabled_s,
    model,
    api_base,
    env_check_enabled_s,
) = sys.argv[1:27]

work_dir = os.path.abspath(work_dir)
redis_enabled = bool(int(redis_enabled_s))
inject_io = bool(int(inject_io_s))
default_inject_io = bool(int(default_inject_io_s))
llm_enabled = bool(int(llm_enabled_s))
env_check_enabled = bool(int(env_check_enabled_s))

artifacts = {
    "sampler_log": os.path.join(work_dir, "sampler.log"),
    "sampler_metrics": os.path.join(work_dir, "sampler_metrics.prom"),
    "env_check": os.path.join(work_dir, "env_check.json"),
    "raw_events": os.path.join(work_dir, "raw_events.jsonl"),
    "events": os.path.join(work_dir, "events.jsonl"),
    "cwd_snapshot": os.path.join(work_dir, "cwd_snapshot.json"),
    "maps_paths": os.path.join(work_dir, "maps_paths.txt"),
    "mapfiles_devino": os.path.join(work_dir, "mapfiles_devino.json"),
    "devino_dict": os.path.join(work_dir, "devino_dict.json"),
    "events_enriched": os.path.join(work_dir, "events_enriched.jsonl"),
    "quality_gate": os.path.join(work_dir, "quality_gate.json"),
    "candidates": os.path.join(work_dir, "candidates.json"),
    "aggregate_stats": os.path.join(work_dir, "aggregate_stats.json"),
    "ranked_yaml": os.path.join(work_dir, "ranked.yaml"),
    "estargz_list": os.path.join(work_dir, "estargz.list"),
    "llm_grounding_audit": os.path.join(work_dir, "llm_grounding_audit.json"),
    "prompt": os.path.join(work_dir, "prompt.txt"),
    "llm_body": os.path.join(work_dir, "body.json"),
    "llm_response": os.path.join(work_dir, "llm_response.json"),
    "hints_raw": os.path.join(work_dir, "hints_raw.yaml"),
    "hints": os.path.join(work_dir, "hints.yaml"),
    "selector_stats_default": os.path.join(work_dir, "selector_stats_default.json"),
    "selector_stats_hints": os.path.join(work_dir, "selector_stats_hints.json"),
}

quality_gate = None
quality_path = artifacts["quality_gate"]
if os.path.exists(quality_path):
    try:
        with open(quality_path, "r", encoding="utf-8") as f:
            quality_gate = json.load(f)
    except Exception:
        quality_gate = None

selector_stats_default = None
selector_stats_default_path = artifacts["selector_stats_default"]
if os.path.exists(selector_stats_default_path):
    try:
        with open(selector_stats_default_path, "r", encoding="utf-8") as f:
            selector_stats_default = json.load(f)
    except Exception:
        selector_stats_default = None

selector_stats_hints = None
selector_stats_hints_path = artifacts["selector_stats_hints"]
if os.path.exists(selector_stats_hints_path):
    try:
        with open(selector_stats_hints_path, "r", encoding="utf-8") as f:
            selector_stats_hints = json.load(f)
    except Exception:
        selector_stats_hints = None

grounding_audit = None
grounding_audit_path = artifacts["llm_grounding_audit"]
if os.path.exists(grounding_audit_path):
    try:
        with open(grounding_audit_path, "r", encoding="utf-8") as f:
            grounding_audit = json.load(f)
    except Exception:
        grounding_audit = None

manifest = {
    "manifest_version": 3,
    "run_id": run_id,
    "work_dir": work_dir,
    "status": "success",
    "image": image,
    "container_name": container_name,
    "startup": {
        "requested": startup_requested,
        "used": startup_used,
        "backend": startup_backend,
        "selection_reason": startup_backend_selection_reason or None,
    },
    "event_sink": {
        "used": event_sink_used,
        "redis_enabled": redis_enabled,
    },
    "sampling": {
        "prom_port": int(prom_port_s),
        "env_check_enabled": env_check_enabled,
    },
    "workload": {
        "ready_url": ready_url or None,
        "ready_timeout_sec": int(ready_timeout_s),
        "predict_url": predict_url or None,
        "inject_io_enabled": inject_io,
        "default_inject_io_cmd_enabled": default_inject_io,
        "custom_inject_io_cmd_present": bool(inject_io_cmd),
    },
    "selection": {
        "size_budget_percent": float(size_budget_percent_s),
        "size_budget_mb": float(size_budget_mb_s),
        "min_hits": int(min_hits_s),
        "p0_max": int(p0_max_s),
        "identity_guard_mode": (selector_stats_default or {}).get("identity_guard_mode"),
        "identity_guard_reason": (selector_stats_default or {}).get("identity_guard_reason"),
        "identity_paths_with_devino_ratio": (selector_stats_default or {}).get("identity_paths_with_devino_ratio"),
        "llm_grounding": {
            "prepared_candidate_paths": int((grounding_audit or {}).get("accepted_total", 0) or 0),
            "existing_in_container_total": int((grounding_audit or {}).get("existing_in_container_total", 0) or 0),
            "missing_in_container_skipped": int((grounding_audit or {}).get("missing_in_container_skipped", 0) or 0),
            "unknown_candidate_skipped": int((grounding_audit or {}).get("unknown_candidate_skipped", 0) or 0),
            "prepare_guard_low_confidence_skipped": int((grounding_audit or {}).get("guard_low_confidence_skipped", 0) or 0),
            "prepare_guard_maps_only_skipped": int((grounding_audit or {}).get("guard_maps_only_skipped", 0) or 0),
            "preferred_guard_active": bool((selector_stats_hints or {}).get("preferred_guard_active", False)),
            "preferred_fallback_used": bool((selector_stats_hints or {}).get("preferred_fallback_used", False)),
            "preferred_low_confidence_skipped": int((selector_stats_hints or {}).get("preferred_low_confidence_skipped", 0) or 0),
            "preferred_maps_only_skipped": int((selector_stats_hints or {}).get("preferred_maps_only_skipped", 0) or 0),
        },
    },
    "llm": {
        "enabled": llm_enabled,
        "model": model,
        "api_base": api_base,
    },
    "quality_gate": quality_gate,
    "artifacts": artifacts,
}

with open(out_path, "w", encoding="utf-8") as f:
    json.dump(manifest, f, ensure_ascii=False, indent=2)
PY

# ========= 输出汇总 =========
log ""
log "====== 产出 ======"
log "[*] 工作目录：${WORK}"
log "    - 运行ID：${RUN_ID}"
log "    - startup 模式：requested=${STARTUP_MODE} used=${STARTUP_MODE_USED} backend=${STARTUP_BACKEND}"
log "    - startup 选择：${STARTUP_BACKEND_SELECTION_REASON}"
log "    - event sink：${EVENT_SINK_USED} (redis_enabled=${USE_REDIS})"
log "    - selector guard：${SELECTOR_GUARD_MODE}${SELECTOR_GUARD_DEVINO_RATIO:+ (devino_ratio=${SELECTOR_GUARD_DEVINO_RATIO})}"
log "    - hint guard backstop：active=${SELECTOR_HINT_GUARD_ACTIVE} fallback=${SELECTOR_HINT_FALLBACK_USED} low_conf=${SELECTOR_HINT_LOW_CONF_SKIPPED} maps_only=${SELECTOR_HINT_MAPS_ONLY_SKIPPED}"
log "    - workload ready：${READY_URL:-<disabled>}"
log "    - workload predict：${PREDICT_URL:-<disabled>}"
log "    - sampler 日志：${LOG}"
log "    - sampler metrics：${SAMPLER_METRICS}"
log "    - manifest：${MANIFEST_JSON}"
log "    - env check：${ENV_CHECK_JSON}"
log "    - 原始事件（未过滤）：${WORK}/raw_events.jsonl"
log "    - 事件（过滤后）：${WORK}/events.jsonl"
log "    - cwd 快照：${WORK}/cwd_snapshot.json"
log "    - maps 快照：${WORK}/maps_paths.txt"
log "    - mapfiles dev:ino：${WORK}/mapfiles_devino.json"
log "    - devino 字典：${WORK}/devino_dict.json"
log "    - 事件（回填后）：${WORK}/events_enriched.jsonl"
log "    - 质量门槛：${WORK}/quality_gate.json"
log "    - 候选：${WORK}/candidates.json"
log "    - 聚合统计：${WORK}/aggregate_stats.json"
log "    - ranked.yaml（预算兜底）：${WORK}/ranked.yaml"
log "    - estargz.list：${WORK}/estargz.list"
log "    - LLM grounding 审计：${GROUNDING_AUDIT}"
log "    - 提示词：${WORK}/prompt.txt"
log "    - LLM 请求：${WORK}/body.json"
log "    - LLM 返回：${WORK}/llm_response.json"
log "    - LLM 初稿：${WORK}/hints_raw.yaml"
log "    - Grounding 后：${WORK}/hints.yaml"
log "    - selector stats：${SELECTOR_STATS_BASE} / ${SELECTOR_STATS_HINT}"
log "    - LLM 模式：$([[ "${LLM_ENABLED}" -eq 1 ]] && echo enabled || echo deterministic)"
log ""
log "[OK] 全流程完成（镜像=${IMAGE}，容器=${CNAME}，ready=${READY_URL:-disabled}，Prometheus=${PROM_PORT})"

if [[ "${KEEP_CONTAINER}" -ne 1 ]]; then
  nerdctl stop "${CNAME}" >/dev/null 2>&1 || true
  nerdctl rm   "${CNAME}" >/dev/null 2>&1 || true
  log "[i] 已清理容器: ${CNAME}"
fi
