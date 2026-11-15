#!/usr/bin/env bash
# coldstart_bench_strict.sh
# LEGACY
# READ-ONLY REFERENCE
# NOT THE FORMAL V1 ENTRYPOINT
# May still be used as an internal helper, but not the user-facing primary entrypoint.
# 对比两张镜像的“严格冷启动”时间，确保尽量排除缓存影响并做稳定计时。
# 计时口径：从 run 发起到 HTTP ready 探针 200 的毫秒数；可选测首个 predict 的毫秒数。

set -Eeuo pipefail

# ============ 默认参数（可用 CLI 覆盖）============
NS="default"
RUNS=3
TIMEOUT_READY=180          # 秒
CSV_OUT="coldstart_compare.csv"
LOG_DIR=""

BASE_IMAGE=""
UNOPT_IMAGE=""
OPT_IMAGE=""
SNAPSHOTTER="stargz"
BASE_SNAPSHOTTER=""
OPT_SNAPSHOTTER=""
COMPARISON_MODE="same-snapshotter"

MODE="struct"             # struct|ready
READY_URL=""
READY_EXEC=""
READY_LOG_REGEX=""
READY_SHELL="sh"
DEBUG_STATE_URL=""
PREDICT_URL=""            # 为空则不测 predict；仅在 HTTP ready probe 下有意义
PREDICT_BODY=""

MODEL_HOST_DIR=""         # 可选，挂载主机模型目录到容器
MODEL_DST_DIR=""
MODEL_NAME=""
PLATFORM="linux/amd64"     # 可选，镜像平台，默认 linux/amd64，可用 --platform 覆盖
CONTAINER_GPUS=""
CONTAINER_IPC=""
CONTAINER_ENTRYPOINT=""
declare -a CONTAINER_ENVS=()
declare -a CONTAINER_MOUNTS=()
declare -a CONTAINER_CMD=()

HARD_COLD=1                # 1=硬冷（默认）；0=软冷
DROP_PAGE_CACHE=1          # sync; echo 3 > /proc/sys/vm/drop_caches
CLEAR_STARGZ_CACHE=1       # 清理 stargz snapshotter cache（保留 metadata/snapshots）
FORCE_RESET=0               # 1=运行前重置 snapshotter/containerd
FORCE_KILL_PORT=0           # 1=端口被占用时强制杀进程释放
ALLOW_BUSY_PORT=0           # 1=端口占用时仅警告并继续（可能有误判风险）
RECONCILE_STARGZ_DIRS=0     # 1=对齐 stargz 本地目录（默认关闭，除非必要）
SHUFFLE_ORDER=0             # 1=每轮随机 baseline/optimized 顺序（减少顺序偏差）
PULL_POLICY="never"        # never|missing|always
DROP_LOCAL_IMAGE_REF=0      # 1=每轮前移除本地同名 image ref，强制走 registry 解析路径
AUTO_PUBLISH_PORT_START=39000
AUTO_PUBLISH_PORT_END=49000
COLD_MODE="local-cold"     # local-cold | registry-cold-strict
STRICT_INPUT_JSON=""
PROOF_JSON=""

STRICT_BASE_REQUESTED_REF=""
STRICT_BASE_RESOLVED_REF=""
STRICT_UNOPT_REQUESTED_REF=""
STRICT_UNOPT_RESOLVED_REF=""
STRICT_OPT_REQUESTED_REF=""
STRICT_OPT_RESOLVED_REF=""
declare -a STRICT_BASE_CLOSURE_DIGESTS=()
declare -a STRICT_UNOPT_CLOSURE_DIGESTS=()
declare -a STRICT_OPT_CLOSURE_DIGESTS=()
declare -a STRICT_SHARED_CLOSURE_DIGESTS=()
declare -a STRICT_UNVERIFIABLE=()
STRICT_PAGE_CACHE_DROP_ATTEMPTED=0
STRICT_PAGE_CACHE_DROP_SUCCEEDED=0

declare -A LOOPBACK_PORT_MAP=()
declare -A USED_PUBLISHED_HOST_PORTS=()
declare -a PUBLISH_ARGS=()
RUNTIME_NETWORK_MODE="host"
EFFECTIVE_READY_URL=""
EFFECTIVE_DEBUG_STATE_URL=""
EFFECTIVE_PREDICT_URL=""
EFFECTIVE_READY_PORT=""
NETWORK_PREP_ERROR=""
READY_FAILURE_ERR=""
READY_HTTP_STATUS_OK_IS_READY=0
MEASURE_SEQ=0
CURRENT_RUN_INDEX=0
CURRENT_LAZY_LABEL=""
LAZY_METADATA_OK=0
LAZY_RESOLVED_REF=""
declare -a LAZY_CLOSURE_DIGESTS=()
declare -a LAZY_ALL_LAYER_DIGESTS=()
declare -a LAZY_LARGE_LAYER_DIGESTS=()
declare -A LAZY_LAYER_SIZE_BYTES=()
LAZY_TOTAL_LAYER_SIZE_BYTES=0
LAZY_MAX_LAYER_SIZE_BYTES=0
LAZY_LARGE_IMAGE_THRESHOLD_BYTES=$(( 6 * 1024 * 1024 * 1024 ))
LAZY_STARGZ_RESET_MODE="${LAZY_STARGZ_RESET_MODE:-hard-reset}"

declare -a STRICT_PRE_CLEANUP_RUNTIME_MOUNT_ENTRIES=()
declare -a STRICT_POST_CLEANUP_RUNTIME_MOUNT_ENTRIES=()
declare -a STRICT_PRE_CLEANUP_LOCAL_SNAPSHOT_DIRS=()
declare -a STRICT_POST_CLEANUP_LOCAL_SNAPSHOT_DIRS=()
STRICT_STARGZ_STATE_SOURCE="not-detected-before-cleanup"
STRICT_STARGZ_CLEANUP_STATUS="not-run"

stargz_root_dir() {
  printf '%s\n' "${STARGZ_GRPC_ROOT_DIR:-/var/lib/containerd-stargz-grpc}"
}

stargz_snapshotter_root_dir() {
  printf '%s/snapshotter\n' "$(stargz_root_dir)"
}

stargz_snapshots_dir() {
  printf '%s/snapshots\n' "$(stargz_snapshotter_root_dir)"
}

stargz_cache_dir() {
  printf '%s/cache\n' "$(stargz_snapshotter_root_dir)"
}

# ============ 帮助 ============
usage() {
  cat <<EOF
用法：
  sudo -E scripts/coldstart_bench_strict.sh \\
    --base docker.io/library/app:latest \\
    [--unopt-image docker.io/library/app:latest-estargz] \\
    --opt docker.io/library/app:latest-stargz \\
    [--runs 3] [--ns default] [--timeout 180] [--csv out.csv] \\
    [--snap stargz] [--base-snap overlayfs] [--opt-snap stargz] \\
    [--comparison-mode same-snapshotter|raw-vs-estargz|raw-full-vs-estargz-lazy|raw-full-vs-estargz-lazy-triple] \\
    [--mode struct|ready] [--ready-url URL | --ready-exec CMD | --ready-log-regex REGEX] [--ready-shell sh] \\
    [--debug-state-url URL] \\
    [--ready-http-status-ok-is-ready] \\
    [--predict-url URL] [--predict-body JSON] \\
    [--model-host /abs/model-dir] [--model-dst /models/app] [--model-name app] \\
    [--gpus all] [--ipc host] [--container-env KEY=VAL] [--container-mount RAW_SPEC] \\
    [--container-entrypoint /path/to/bin] [--container-cmd ARG] \\
    [--platform linux/amd64] [--hard-cold|--soft-cold] [--force-reset] \\
    [--force-kill-port|--allow-busy-port] [--reconcile-dirs] \\
    [--no-drop-cache] [--no-clear-stargz] [--shuffle] [--log-dir DIR] \\
    [--pull-policy never|missing|always] [--drop-local-image-ref] \\
    [--cold-mode local-cold|registry-cold-strict] [--strict-input-json PATH] [--proof-json PATH]

说明：
  - 本脚本是 strict benchmark helper，主要供 Python orchestrator benchmark stage 复用或手动排障。
  - 正式推荐入口：python3 scripts/oneclick_product.py benchmark --work-dir <DIR> 或 run --benchmark。
  - 计时（按 --mode）：
      * struct：显式 pull（若需要）+ --entrypoint /bin/true；ready_ms 保持为 run_ms，end_to_end_ready_ms 额外记录 pull+run。
      * ready：显式 pull -> nerdctl run -d -> ready probe 成功。
        其中 ready_ms 兼容映射为 end_to_end_ready_ms = pull_ms + run_ms + ready_wait_ms。
        可选 probe：
          - --ready-url：支持 http(s)://... 或 tcp://127.0.0.1:PORT
          - --ready-exec：容器内执行命令，返回码 0 视为 ready（依赖容器内 shell）
          - --ready-log-regex：容器日志命中扩展正则视为 ready
        对于 self-starting HTTP service，可选 --ready-http-status-ok-is-ready 将任意 HTTP 2xx 视为 ready。
        若提供 --predict-url，再测 ready 后首个 HTTP predict 成功的耗时（predict_extra_ms）。
  - “严格冷启动”：
      * 清 Linux 页缓存（需 root）
      * 若使用 stargz：清理 stargz-snapshotter 的 cache 目录（保留 metadata/snapshots，不重启服务）
      * 可选 --force-reset：一键 stop containerd/stargz → 清空 stargz 目录 + meta.db → restart → smoke check
    注：local-cold 默认不删除 content store；registry-cold-strict 会按 strict input 中的 platform closure 定向删除相关 digests。
  - 输出 CSV：保留 pull/run/ready/predict/proof 字段，并额外包含 lazy_prepare_ms、lazy_ready_ms、ready_ms_semantics 与 lazy correctness guard 字段。
  - 若 ready/predict 使用 loopback URL 且宿主端口已被占用，脚本会优先自动把容器端口重映射到空闲高位端口，避免 host network 端口冲突。
  - registry-based 实验可配合 --pull-policy=always 使用；若要尽量强制走 registry 解析，可追加 --drop-local-image-ref。
  - registry-cold-strict 会额外校验：ready-only、stargz-vs-stargz、pull=always、drop local refs、targeted content purge、blocking strict proof。
EOF
}

# ============ 参数解析 ============
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ns) NS="$2"; shift 2;;
    --runs) RUNS="$2"; shift 2;;
    --timeout) TIMEOUT_READY="$2"; shift 2;;
    --csv) CSV_OUT="$2"; shift 2;;

    --base) BASE_IMAGE="$2"; shift 2;;
    --unopt-image) UNOPT_IMAGE="$2"; shift 2;;
    --opt)  OPT_IMAGE="$2"; shift 2;;
    --snap) SNAPSHOTTER="$2"; shift 2;;
    --base-snap) BASE_SNAPSHOTTER="$2"; shift 2;;
    --opt-snap)  OPT_SNAPSHOTTER="$2"; shift 2;;
    --comparison-mode) COMPARISON_MODE="$2"; shift 2;;

    --mode) MODE="$2"; shift 2;;
    --ready-url) READY_URL="$2"; shift 2;;
    --ready-exec) READY_EXEC="$2"; shift 2;;
    --ready-log-regex) READY_LOG_REGEX="$2"; shift 2;;
    --ready-shell) READY_SHELL="$2"; shift 2;;
    --debug-state-url) DEBUG_STATE_URL="$2"; shift 2;;
    --ready-http-status-ok-is-ready) READY_HTTP_STATUS_OK_IS_READY=1; shift;;
    --predict-url) PREDICT_URL="$2"; shift 2;;
    --predict-body) PREDICT_BODY="$2"; shift 2;;

    --model-host) MODEL_HOST_DIR="$2"; shift 2;;
    --model-dst)  MODEL_DST_DIR="$2"; shift 2;;
    --model-name) MODEL_NAME="$2"; shift 2;;
    --gpus) CONTAINER_GPUS="$2"; shift 2;;
    --ipc) CONTAINER_IPC="$2"; shift 2;;
    --container-env) CONTAINER_ENVS+=("$2"); shift 2;;
    --container-mount) CONTAINER_MOUNTS+=("$2"); shift 2;;
    --container-entrypoint) CONTAINER_ENTRYPOINT="$2"; shift 2;;
    --container-cmd) CONTAINER_CMD+=("$2"); shift 2;;
    --platform) PLATFORM="$2"; shift 2;;

    --hard-cold) HARD_COLD=1; shift;;
    --soft-cold) HARD_COLD=0; shift;;
    --force-reset) FORCE_RESET=1; shift;;
    --force-kill-port) FORCE_KILL_PORT=1; shift;;
    --allow-busy-port) ALLOW_BUSY_PORT=1; shift;;
    --reconcile-dirs) RECONCILE_STARGZ_DIRS=1; shift;;
    --no-drop-cache) DROP_PAGE_CACHE=0; shift;;
    --no-clear-stargz) CLEAR_STARGZ_CACHE=0; shift;;
    --shuffle) SHUFFLE_ORDER=1; shift;;
    --log-dir) LOG_DIR="${2:?}"; shift 2;;
    --pull-policy) PULL_POLICY="$2"; shift 2;;
    --drop-local-image-ref) DROP_LOCAL_IMAGE_REF=1; shift;;
    --cold-mode) COLD_MODE="$2"; shift 2;;
    --strict-input-json) STRICT_INPUT_JSON="$2"; shift 2;;
    --proof-json) PROOF_JSON="$2"; shift 2;;

    -h|--help) usage; exit 0;;
    *) echo "[ERR] Unknown arg: $1" >&2; usage; exit 1;;
  esac
done

[[ -n "$BASE_IMAGE" && -n "$OPT_IMAGE" ]] || { echo "[ERR] --base/--opt 必填" >&2; usage; exit 1; }

# ============ 依赖检查 ============
die() { echo "[ERR] $*" >&2; exit 2; }
warn() { echo "[WARN] $*" >&2; }
log() { echo "$*" >&2; }
need() { command -v "$1" >/dev/null 2>&1 || die "缺少命令：$1"; }
need ctr-remote; need nerdctl; need curl; need awk; need date; need ss; need jq
[[ "$COLD_MODE" =~ ^(local-cold|registry-cold-strict)$ ]] || die "--cold-mode 仅支持 local-cold|registry-cold-strict"
if [[ "$COLD_MODE" == "registry-cold-strict" || "$COMPARISON_MODE" == "raw-full-vs-estargz-lazy" || "$COMPARISON_MODE" == "raw-full-vs-estargz-lazy-triple" ]]; then
  need ctr
fi

# 确保 namespace 存在
ctr-remote namespaces create "$NS" >/dev/null 2>&1 || true

# 断言 snapshotter 插件可用（ok）—同时校验 base/opt 两侧
check_snap_ok() {
  local s="$1"; [[ -z "$s" ]] && return 0
  ctr-remote plugins ls 2>/dev/null \
    | awk -v s="$s" '$1=="io.containerd.snapshotter.v1" && $2==s {print $4}' \
    | grep -qx 'ok'
}
if [[ -z "$BASE_SNAPSHOTTER" ]]; then
  if [[ "$COMPARISON_MODE" == "raw-full-vs-estargz-lazy" || "$COMPARISON_MODE" == "raw-full-vs-estargz-lazy-triple" ]]; then
    BASE_SNAPSHOTTER="overlayfs"
  else
    BASE_SNAPSHOTTER="$SNAPSHOTTER"
  fi
fi
if [[ -z "$OPT_SNAPSHOTTER" ]]; then
  if [[ "$COMPARISON_MODE" == "raw-full-vs-estargz-lazy" || "$COMPARISON_MODE" == "raw-full-vs-estargz-lazy-triple" ]]; then
    OPT_SNAPSHOTTER="stargz"
  else
    OPT_SNAPSHOTTER="$SNAPSHOTTER"
  fi
fi
check_snap_ok "$BASE_SNAPSHOTTER" || { echo "[ERR] snapshotter '$BASE_SNAPSHOTTER' 未处于 ok" >&2; exit 2; }
check_snap_ok "$OPT_SNAPSHOTTER"  || { echo "[ERR] snapshotter '$OPT_SNAPSHOTTER' 未处于 ok"  >&2; exit 2; }

# -------- 参数合法性校验 --------
[[ "$RUNS" =~ ^[0-9]+$ && "$RUNS" -gt 0 ]] || die "--runs 必须是正整数"
[[ "$TIMEOUT_READY" =~ ^[0-9]+$ && "$TIMEOUT_READY" -gt 0 ]] || die "--timeout 必须是正整数"
[[ "$MODE" =~ ^(struct|ready)$ ]] || die "--mode 仅支持 struct|ready"
[[ "$COMPARISON_MODE" =~ ^(same-snapshotter|raw-vs-estargz|raw-full-vs-estargz-lazy|raw-full-vs-estargz-lazy-triple)$ ]] || die "--comparison-mode 仅支持 same-snapshotter|raw-vs-estargz|raw-full-vs-estargz-lazy|raw-full-vs-estargz-lazy-triple"
if [[ "$COMPARISON_MODE" == "raw-full-vs-estargz-lazy-triple" && -z "$UNOPT_IMAGE" ]]; then
  die "--comparison-mode raw-full-vs-estargz-lazy-triple requires --unopt-image"
fi
if [[ "$COMPARISON_MODE" == "raw-full-vs-estargz-lazy-triple" ]]; then
  [[ "$BASE_SNAPSHOTTER" == "overlayfs" ]] || die "raw-full-vs-estargz-lazy-triple requires --base-snap overlayfs"
  [[ "$OPT_SNAPSHOTTER" == "stargz" ]] || die "raw-full-vs-estargz-lazy-triple requires --opt-snap stargz"
fi
[[ "$PULL_POLICY" =~ ^(never|missing|always)$ ]] || die "--pull-policy 仅支持 never|missing|always"
READY_PROBE_COUNT=0
[[ -n "$READY_URL" ]] && READY_PROBE_COUNT=$((READY_PROBE_COUNT + 1))
[[ -n "$READY_EXEC" ]] && READY_PROBE_COUNT=$((READY_PROBE_COUNT + 1))
[[ -n "$READY_LOG_REGEX" ]] && READY_PROBE_COUNT=$((READY_PROBE_COUNT + 1))
if [[ "$MODE" == "ready" && "$READY_PROBE_COUNT" -eq 0 ]]; then
  die "--mode=ready 需要 --ready-url、--ready-exec 或 --ready-log-regex 之一"
fi
if [[ "$READY_PROBE_COUNT" -gt 1 ]]; then
  die "ready probe 仅允许三选一：--ready-url、--ready-exec、--ready-log-regex"
fi
if [[ -n "$MODEL_HOST_DIR" && ! -d "$MODEL_HOST_DIR" ]]; then
  die "--model-host 目录不存在：$MODEL_HOST_DIR"
fi
if [[ "$MODE" != "ready" && -n "$PREDICT_URL" ]]; then
  warn "--predict-url 仅对 --mode=ready 生效，已忽略"
  PREDICT_URL=""
fi

# -------- sudo 能力检查（严格模式）--------
NEED_SUDO=0
if [[ "$HARD_COLD" -eq 1 || "$DROP_PAGE_CACHE" -eq 1 || "$CLEAR_STARGZ_CACHE" -eq 1 || "$FORCE_RESET" -eq 1 || "$RECONCILE_STARGZ_DIRS" -eq 1 ]]; then
  NEED_SUDO=1
fi
if [[ "$SNAPSHOTTER" == "stargz" || "$BASE_SNAPSHOTTER" == "stargz" || "$OPT_SNAPSHOTTER" == "stargz" ]]; then
  NEED_SUDO=1
fi
if [[ "$BASE_IMAGE" == 127.0.0.1:5001/* || "$OPT_IMAGE" == 127.0.0.1:5001/* ]]; then
  NEED_SUDO=1
fi
if [[ "$NEED_SUDO" -eq 1 ]]; then
  need sudo
  if [[ "$EUID" -ne 0 ]]; then
    if ! sudo -n true >/dev/null 2>&1; then
      die "需要 sudo 权限（请使用 sudo -E 运行或配置免密 sudo）"
    fi
  fi
fi

# -------- 日志目录 --------
if [[ -z "$LOG_DIR" ]]; then
  LOG_DIR="$(dirname "$CSV_OUT")/coldstart_logs"
fi
mkdir -p "$LOG_DIR" || die "无法创建日志目录: $LOG_DIR"
if [[ "$COLD_MODE" == "registry-cold-strict" && -z "$PROOF_JSON" ]]; then
  PROOF_JSON="$(dirname "$CSV_OUT")/benchmark_strict_proof.json"
fi

ms_now() { date +%s%3N; }

support_cni_flag() {
  nerdctl run --help 2>&1 | grep -q -- '--cni'
}

normalize_ref() {
  # 归一化镜像名：去掉 docker.io/index.docker.io 前缀，以便宽松匹配
  local r="$1"
  r="${r#index.docker.io/}"
  r="${r#docker.io/}"
  r="${r#library/}"
  echo "$r"
}

strict_mode_enabled() {
  [[ "$COLD_MODE" == "registry-cold-strict" ]]
}

record_unverifiable() {
  local item="$1" existing=""
  for existing in "${STRICT_UNVERIFIABLE[@]}"; do
    [[ "$existing" == "$item" ]] && return 0
  done
  STRICT_UNVERIFIABLE+=("$item")
}

json_array_from_words() {
  if [[ "$#" -eq 0 ]]; then
    printf '[]'
  else
    printf '%s\n' "$@" | jq -R . | jq -s .
  fi
}

strict_ref_has_explicit_registry() {
  local ref="${1:-}" name="" authority=""
  name="${ref%@*}"
  [[ "$name" == */* ]] || return 1
  authority="${name%%/*}"
  [[ "$authority" == "localhost" || "$authority" == *.* || "$authority" == *:* ]]
}

strict_ref_repo_key() {
  local ref="${1:-}" name="" authority="" remainder="" repo=""
  name="${ref%@*}"
  authority="${name%%/*}"
  remainder="${name#*/}"
  repo="$remainder"
  if [[ "${repo##*/}" == *:* ]]; then
    repo="${repo%:*}"
  fi
  printf '%s/%s\n' "${authority,,}" "${repo,,}"
}

strict_require_digest_ref() {
  local ref="${1:-}" context="${2:-image ref}"
  [[ "$ref" == *@sha256:* ]] || die "registry-cold-strict requires a digest-pinned ${context}: ${ref}"
}

triple_lazy_mode_enabled() {
  [[ "$COMPARISON_MODE" == "raw-full-vs-estargz-lazy-triple" ]]
}

load_strict_input() {
  [[ -n "$STRICT_INPUT_JSON" ]] || die "registry-cold-strict requires --strict-input-json"
  [[ -f "$STRICT_INPUT_JSON" ]] || die "registry-cold-strict strict input JSON does not exist: $STRICT_INPUT_JSON"
  STRICT_BASE_REQUESTED_REF="$(jq -er '.base.requested_ref' "$STRICT_INPUT_JSON")" || die "failed to parse base.requested_ref from strict input"
  STRICT_BASE_RESOLVED_REF="$(jq -er '.base.resolved_ref' "$STRICT_INPUT_JSON")" || die "failed to parse base.resolved_ref from strict input"
  if triple_lazy_mode_enabled; then
    STRICT_UNOPT_REQUESTED_REF="$(jq -er '.unoptimized.requested_ref' "$STRICT_INPUT_JSON")" || die "failed to parse unoptimized.requested_ref from strict input"
    STRICT_UNOPT_RESOLVED_REF="$(jq -er '.unoptimized.resolved_ref' "$STRICT_INPUT_JSON")" || die "failed to parse unoptimized.resolved_ref from strict input"
    mapfile -t STRICT_UNOPT_CLOSURE_DIGESTS < <(jq -r '.unoptimized.closure_digests[]?' "$STRICT_INPUT_JSON")
  else
    STRICT_UNOPT_REQUESTED_REF=""
    STRICT_UNOPT_RESOLVED_REF=""
    STRICT_UNOPT_CLOSURE_DIGESTS=()
  fi
  STRICT_OPT_REQUESTED_REF="$(jq -er '.optimized.requested_ref' "$STRICT_INPUT_JSON")" || die "failed to parse optimized.requested_ref from strict input"
  STRICT_OPT_RESOLVED_REF="$(jq -er '.optimized.resolved_ref' "$STRICT_INPUT_JSON")" || die "failed to parse optimized.resolved_ref from strict input"
  mapfile -t STRICT_BASE_CLOSURE_DIGESTS < <(jq -r '.base.closure_digests[]?' "$STRICT_INPUT_JSON")
  mapfile -t STRICT_OPT_CLOSURE_DIGESTS < <(jq -r '.optimized.closure_digests[]?' "$STRICT_INPUT_JSON")
  mapfile -t STRICT_SHARED_CLOSURE_DIGESTS < <(jq -r '.shared_closure_digests[]?' "$STRICT_INPUT_JSON")
  [[ "${#STRICT_BASE_CLOSURE_DIGESTS[@]}" -gt 0 ]] || die "registry-cold-strict requires a non-empty base closure digest set"
  if triple_lazy_mode_enabled; then
    [[ "${#STRICT_UNOPT_CLOSURE_DIGESTS[@]}" -gt 0 ]] || die "registry-cold-strict requires a non-empty unoptimized closure digest set"
  fi
  [[ "${#STRICT_OPT_CLOSURE_DIGESTS[@]}" -gt 0 ]] || die "registry-cold-strict requires a non-empty optimized closure digest set"
}

validate_registry_cold_strict_contract() {
  strict_mode_enabled || return 0
  [[ "$MODE" == "ready" ]] || die "registry-cold-strict requires --mode ready"
  if [[ "$COMPARISON_MODE" == "same-snapshotter" ]]; then
    [[ "$SNAPSHOTTER" == "stargz" && "$BASE_SNAPSHOTTER" == "stargz" && "$OPT_SNAPSHOTTER" == "stargz" ]] \
      || die "registry-cold-strict requires stargz for --snap/--base-snap/--opt-snap"
  fi
  [[ "$PULL_POLICY" == "always" ]] || die "registry-cold-strict requires --pull-policy always"
  [[ "$DROP_LOCAL_IMAGE_REF" -eq 1 ]] || die "registry-cold-strict requires --drop-local-image-ref"
  [[ "$HARD_COLD" -eq 1 ]] || die "registry-cold-strict requires --hard-cold"
  [[ "$DROP_PAGE_CACHE" -eq 1 ]] || die "registry-cold-strict requires page-cache drop to remain enabled"
  [[ "$CLEAR_STARGZ_CACHE" -eq 1 ]] || die "registry-cold-strict requires stargz cache cleanup to remain enabled"
  load_strict_input
  strict_ref_has_explicit_registry "$BASE_IMAGE" || die "registry-cold-strict requires explicit registry refs for baseline"
  if triple_lazy_mode_enabled; then
    strict_ref_has_explicit_registry "$UNOPT_IMAGE" || die "registry-cold-strict requires explicit registry refs for unoptimized"
  fi
  strict_ref_has_explicit_registry "$OPT_IMAGE" || die "registry-cold-strict requires explicit registry refs for optimized"
  strict_ref_has_explicit_registry "$STRICT_BASE_RESOLVED_REF" || die "registry-cold-strict resolved baseline ref is not an explicit registry ref"
  if triple_lazy_mode_enabled; then
    strict_ref_has_explicit_registry "$STRICT_UNOPT_RESOLVED_REF" || die "registry-cold-strict resolved unoptimized ref is not an explicit registry ref"
  fi
  strict_ref_has_explicit_registry "$STRICT_OPT_RESOLVED_REF" || die "registry-cold-strict resolved optimized ref is not an explicit registry ref"
  strict_require_digest_ref "$STRICT_BASE_RESOLVED_REF" "baseline resolved ref"
  if triple_lazy_mode_enabled; then
    strict_require_digest_ref "$STRICT_UNOPT_RESOLVED_REF" "unoptimized resolved ref"
  fi
  strict_require_digest_ref "$STRICT_OPT_RESOLVED_REF" "optimized resolved ref"
  [[ "$(strict_ref_repo_key "$BASE_IMAGE")" == "$(strict_ref_repo_key "$OPT_IMAGE")" ]] \
    || die "registry-cold-strict requires baseline and optimized refs to share the same registry/repository"
  if triple_lazy_mode_enabled; then
    [[ "$(strict_ref_repo_key "$BASE_IMAGE")" == "$(strict_ref_repo_key "$UNOPT_IMAGE")" ]] \
      || die "registry-cold-strict requires baseline and unoptimized refs to share the same registry/repository"
    [[ "$(strict_ref_repo_key "$UNOPT_IMAGE")" == "$(strict_ref_repo_key "$OPT_IMAGE")" ]] \
      || die "registry-cold-strict requires unoptimized and optimized refs to share the same registry/repository"
  fi
  [[ "$(strict_ref_repo_key "$BASE_IMAGE")" == "$(strict_ref_repo_key "$STRICT_BASE_RESOLVED_REF")" ]] \
    || die "registry-cold-strict base CLI ref does not match the strict resolved repository"
  if triple_lazy_mode_enabled; then
    [[ "$(strict_ref_repo_key "$UNOPT_IMAGE")" == "$(strict_ref_repo_key "$STRICT_UNOPT_RESOLVED_REF")" ]] \
      || die "registry-cold-strict unoptimized CLI ref does not match the strict resolved repository"
  fi
  [[ "$(strict_ref_repo_key "$OPT_IMAGE")" == "$(strict_ref_repo_key "$STRICT_OPT_RESOLVED_REF")" ]] \
    || die "registry-cold-strict optimized CLI ref does not match the strict resolved repository"
}

strict_label_ref_candidates() {
  local label="$1" cli_image="$2"
  case "$label" in
    baseline)
      printf '%s\n' "$cli_image" "$BASE_IMAGE" "$STRICT_BASE_REQUESTED_REF" "$STRICT_BASE_RESOLVED_REF"
      ;;
    unoptimized_lazy)
      printf '%s\n' "$cli_image" "$UNOPT_IMAGE" "$STRICT_UNOPT_REQUESTED_REF" "$STRICT_UNOPT_RESOLVED_REF"
      ;;
    *)
      printf '%s\n' "$cli_image" "$OPT_IMAGE" "$STRICT_OPT_REQUESTED_REF" "$STRICT_OPT_RESOLVED_REF"
      ;;
  esac | awk 'NF && !seen[$0]++'
}

strict_label_resolved_ref() {
  local label="$1"
  case "$label" in
    baseline) printf '%s\n' "$STRICT_BASE_RESOLVED_REF" ;;
    unoptimized_lazy) printf '%s\n' "$STRICT_UNOPT_RESOLVED_REF" ;;
    *) printf '%s\n' "$STRICT_OPT_RESOLVED_REF" ;;
  esac
}

strict_union_closure_digests() {
  if triple_lazy_mode_enabled; then
    printf '%s\n' "${STRICT_BASE_CLOSURE_DIGESTS[@]}" "${STRICT_UNOPT_CLOSURE_DIGESTS[@]}" "${STRICT_OPT_CLOSURE_DIGESTS[@]}" | awk 'NF && !seen[$0]++'
  else
    printf '%s\n' "${STRICT_BASE_CLOSURE_DIGESTS[@]}" "${STRICT_OPT_CLOSURE_DIGESTS[@]}" | awk 'NF && !seen[$0]++'
  fi
}

strict_ref_is_present() {
  local ref="$1" target_norm="" output="" name=""
  nerdctl --namespace "$NS" image inspect "$ref" >/dev/null 2>&1 && return 0
  if ! output="$(ctr-remote -n "$NS" images ls 2>/dev/null)"; then
    record_unverifiable "local_image_ref_probe"
    return 0
  fi
  target_norm="$(normalize_ref "$ref")"
  while read -r name; do
    [[ -z "$name" || "$name" == "REF" ]] && continue
    if [[ "$name" == "$ref" || "$(normalize_ref "$name")" == "$target_norm" ]]; then
      return 0
    fi
  done < <(printf '%s\n' "$output" | awk 'NR>1{print $1}')
  return 1
}

strict_count_remaining_local_refs() {
  local label="$1" cli_image="$2" count=0 ref=""
  while read -r ref; do
    [[ -n "$ref" ]] || continue
    if strict_ref_is_present "$ref"; then
      count=$((count + 1))
    fi
  done < <(strict_label_ref_candidates "$label" "$cli_image")
  printf '%s\n' "$count"
}

strict_drop_local_refs_for_label() {
  local label="$1" cli_image="$2" ref=""
  while read -r ref; do
    [[ -n "$ref" ]] || continue
    log "[i] registry-cold-strict: 移除本地 image ref（若存在）：${ref}"
    nerdctl --namespace "$NS" image rm "$ref" >/dev/null 2>&1 || true
  done < <(strict_label_ref_candidates "$label" "$cli_image")
}

strict_drop_all_local_refs() {
  strict_drop_local_refs_for_label baseline "$BASE_IMAGE"
  if triple_lazy_mode_enabled; then
    strict_drop_local_refs_for_label unoptimized_lazy "$UNOPT_IMAGE"
  fi
  strict_drop_local_refs_for_label optimized "$OPT_IMAGE"
}

strict_count_stargz_snapshot_records() {
  if [[ -n "${STRICT_TEST_GLOBAL_STARGZ_SNAPSHOT_RECORDS_REMAINING:-}" ]]; then
    printf '%s\n' "${STRICT_TEST_GLOBAL_STARGZ_SNAPSHOT_RECORDS_REMAINING}"
    return
  fi
  local output=""
  if ! output="$(ctr-remote -n "$NS" snapshots --snapshotter stargz ls 2>/dev/null)"; then
    record_unverifiable "stargz_snapshot_records"
    printf '1\n'
    return
  fi
  printf '%s\n' "$output" | awk 'NR>1{print $1}' | wc -l | tr -d ' '
}

strict_parse_runtime_mount_target_from_line() {
  local line="${1:-}"
  if [[ "$line" == *" on "* && "$line" == *" type "* ]]; then
    local rest="${line#* on }"
    printf '%s\n' "${rest%% type *}"
    return 0
  fi
  printf '%s\n' "${line%% *}"
}

strict_list_stargz_runtime_mount_entries() {
  local snapshots_dir="" output="" line="" count=0
  snapshots_dir="$(stargz_snapshots_dir)"
  if [[ -n "${STRICT_TEST_FINDMNT_OUTPUT:-}" ]]; then
    output="${STRICT_TEST_FINDMNT_OUTPUT}"
  elif [[ -n "${STRICT_TEST_STARGZ_RUNTIME_MOUNTS_REMAINING:-}" ]]; then
    count="${STRICT_TEST_STARGZ_RUNTIME_MOUNTS_REMAINING}"
    for ((i=0; i<count; i++)); do
      printf '%s/test-%s/fs containerd-stargz-grpc fuse.stargz rw,nosuid,nodev\n' "$snapshots_dir" "$i"
    done
    return 0
  elif command -v findmnt >/dev/null 2>&1; then
    if ! output="$(findmnt -rn -o TARGET,SOURCE,FSTYPE,OPTIONS 2>/dev/null)"; then
      record_unverifiable "stargz_runtime_mounts"
      return 0
    fi
  elif command -v mount >/dev/null 2>&1; then
    if ! output="$(mount 2>/dev/null)"; then
      record_unverifiable "stargz_runtime_mounts"
      return 0
    fi
  else
    record_unverifiable "stargz_runtime_mounts"
    return 0
  fi

  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    if [[ "$line" == *" on "* && "$line" == *" type "* ]]; then
      if [[ "$line" == *containerd-stargz-grpc* || "$line" == *fuse.stargz* ]]; then
        printf '%s\n' "$line"
      fi
      continue
    fi
    case "$line" in
      "${snapshots_dir}/"*"/fs "*|*" containerd-stargz-grpc "*|*" fuse.stargz "*)
        printf '%s\n' "$line"
        ;;
    esac
  done <<<"$output"
}

strict_list_stargz_local_snapshot_dirs() {
  local snapshots_dir="" count=0
  snapshots_dir="$(stargz_snapshots_dir)"
  if [[ -n "${STRICT_TEST_STARGZ_LOCAL_SNAPSHOT_DIRS:-}" ]]; then
    printf '%s\n' "${STRICT_TEST_STARGZ_LOCAL_SNAPSHOT_DIRS}" | awk 'NF'
    return 0
  fi
  if [[ -n "${STRICT_TEST_GLOBAL_STARGZ_LOCAL_SNAPSHOT_ENTRIES_REMAINING:-}" ]]; then
    count="${STRICT_TEST_GLOBAL_STARGZ_LOCAL_SNAPSHOT_ENTRIES_REMAINING}"
    for ((i=0; i<count; i++)); do
      printf '%s/test-%s\n' "$snapshots_dir" "$i"
    done
    return 0
  fi
  [[ -d "$snapshots_dir" ]] || return 0
  find "$snapshots_dir" -mindepth 1 -maxdepth 1 2>/dev/null | awk 'NF'
}

strict_count_stargz_runtime_mounts() {
  local count=0
  while IFS= read -r _line; do
    [[ -n "$_line" ]] || continue
    count=$((count + 1))
  done < <(strict_list_stargz_runtime_mount_entries)
  printf '%s\n' "$count"
}

strict_count_dir_entries() {
  local path="$1" probe_id="$2"
  case "$probe_id" in
    stargz_local_snapshot_entries)
      if [[ -n "${STRICT_TEST_GLOBAL_STARGZ_LOCAL_SNAPSHOT_ENTRIES_REMAINING:-}" ]]; then
        printf '%s\n' "${STRICT_TEST_GLOBAL_STARGZ_LOCAL_SNAPSHOT_ENTRIES_REMAINING}"
        return
      fi
      ;;
    stargz_cache_entries)
      if [[ -n "${STRICT_TEST_STARGZ_CACHE_ENTRIES_REMAINING:-}" ]]; then
        printf '%s\n' "${STRICT_TEST_STARGZ_CACHE_ENTRIES_REMAINING}"
        return
      fi
      ;;
  esac
  if [[ ! -d "$path" ]]; then
    printf '0\n'
    return
  fi
  if ! find "$path" -mindepth 1 -maxdepth 1 >/dev/null 2>&1; then
    if [[ "$probe_id" == "stargz_local_snapshot_entries" ]]; then
      printf '0\n'
      return
    fi
    record_unverifiable "$probe_id"
    printf '1\n'
    return
  fi
  find "$path" -mindepth 1 -maxdepth 1 2>/dev/null | wc -l | tr -d ' '
}

strict_count_content_remaining_for_digests() {
  local probe_id="$1"; shift
  local count=0 digest=""
  if ! ctr -n "$NS" content ls >/dev/null 2>&1; then
    record_unverifiable "$probe_id"
    printf '1\n'
    return
  fi
  for digest in "$@"; do
    [[ -n "$digest" ]] || continue
    if ctr -n "$NS" content get "$digest" >/dev/null 2>&1; then
      count=$((count + 1))
    fi
  done
  printf '%s\n' "$count"
}

strict_purge_content_union() {
  local digest=""
  while read -r digest; do
    [[ -n "$digest" ]] || continue
    log "[i] registry-cold-strict: 删除 content digest（若存在）：${digest}"
    ctr -n "$NS" content delete "$digest" >/dev/null 2>&1 || true
  done < <(strict_union_closure_digests)
}

strict_log_state_list() {
  local heading="$1"; shift || true
  local item=""
  log "[i] ${heading}: $#:"
  if [[ "$#" -eq 0 ]]; then
    log "[i]   (none)"
    return 0
  fi
  for item in "$@"; do
    log "[i]   ${item}"
  done
}

strict_cleanup_namespace_runtime_state() {
  local ctr_output="" name=""
  if ctr -n "$NS" tasks ls >/dev/null 2>&1; then
    ctr_output="$(ctr -n "$NS" tasks ls 2>/dev/null | awk 'NR>1{print $1}' || true)"
    while read -r name; do
      [[ -n "$name" ]] || continue
      ctr -n "$NS" tasks kill -s KILL "$name" >/dev/null 2>&1 || true
      ctr -n "$NS" tasks rm "$name" >/dev/null 2>&1 || true
    done <<<"$ctr_output"
  fi
  if ctr -n "$NS" containers ls >/dev/null 2>&1; then
    ctr_output="$(ctr -n "$NS" containers ls 2>/dev/null | awk 'NR>1{print $1}' || true)"
    while read -r name; do
      [[ -n "$name" ]] || continue
      ctr -n "$NS" containers rm "$name" >/dev/null 2>&1 || true
    done <<<"$ctr_output"
  fi
}

strict_unmount_stargz_runtime_mounts() {
  local entry="" target=""
  while IFS= read -r entry; do
    [[ -n "$entry" ]] || continue
    target="$(strict_parse_runtime_mount_target_from_line "$entry")"
    [[ -n "$target" ]] || continue
    log "[i] strict cleanup: 卸载 stargz runtime mount: ${target}"
    sudo umount -l "$target" >/dev/null 2>&1 || sudo umount "$target" >/dev/null 2>&1 || true
  done < <(strict_list_stargz_runtime_mount_entries)
}

write_strict_proof_json() {
  local base_local_remaining="$1" unopt_local_remaining="$2" opt_local_remaining="$3" snapshot_records_remaining="$4"
  local runtime_mounts_remaining="$5" local_snapshot_entries_remaining="$6" cache_entries_remaining="$7"
  local base_content_remaining="$8" unopt_content_remaining="$9" opt_content_remaining="${10}" strict_zero="${11}"
  local base_closure_json unopt_closure_json opt_closure_json shared_closure_json unverifiable_json strict_zero_json
  local pre_runtime_json post_runtime_json pre_local_snapshot_json post_local_snapshot_json
  base_closure_json="$(json_array_from_words "${STRICT_BASE_CLOSURE_DIGESTS[@]}")"
  unopt_closure_json="$(json_array_from_words "${STRICT_UNOPT_CLOSURE_DIGESTS[@]}")"
  opt_closure_json="$(json_array_from_words "${STRICT_OPT_CLOSURE_DIGESTS[@]}")"
  shared_closure_json="$(json_array_from_words "${STRICT_SHARED_CLOSURE_DIGESTS[@]}")"
  unverifiable_json="$(json_array_from_words "${STRICT_UNVERIFIABLE[@]}")"
  pre_runtime_json="$(json_array_from_words "${STRICT_PRE_CLEANUP_RUNTIME_MOUNT_ENTRIES[@]}")"
  post_runtime_json="$(json_array_from_words "${STRICT_POST_CLEANUP_RUNTIME_MOUNT_ENTRIES[@]}")"
  pre_local_snapshot_json="$(json_array_from_words "${STRICT_PRE_CLEANUP_LOCAL_SNAPSHOT_DIRS[@]}")"
  post_local_snapshot_json="$(json_array_from_words "${STRICT_POST_CLEANUP_LOCAL_SNAPSHOT_DIRS[@]}")"
  if [[ "$strict_zero" -eq 1 ]]; then
    strict_zero_json=true
  else
    strict_zero_json=false
  fi
  jq -n \
    --arg cold_mode "$COLD_MODE" \
    --arg base_requested_ref "$STRICT_BASE_REQUESTED_REF" \
    --arg base_resolved_ref "$STRICT_BASE_RESOLVED_REF" \
    --arg unopt_requested_ref "$STRICT_UNOPT_REQUESTED_REF" \
    --arg unopt_resolved_ref "$STRICT_UNOPT_RESOLVED_REF" \
    --arg opt_requested_ref "$STRICT_OPT_REQUESTED_REF" \
    --arg opt_resolved_ref "$STRICT_OPT_RESOLVED_REF" \
    --arg stargz_state_source "$STRICT_STARGZ_STATE_SOURCE" \
    --arg stargz_cleanup_status "$STRICT_STARGZ_CLEANUP_STATUS" \
    --argjson base_closure "$base_closure_json" \
    --argjson unopt_closure "$unopt_closure_json" \
    --argjson opt_closure "$opt_closure_json" \
    --argjson shared_closure "$shared_closure_json" \
    --argjson unverifiable "$unverifiable_json" \
    --argjson pre_runtime_mount_entries "$pre_runtime_json" \
    --argjson remaining_runtime_mount_entries "$post_runtime_json" \
    --argjson pre_local_snapshot_dirs "$pre_local_snapshot_json" \
    --argjson remaining_local_snapshot_dirs "$post_local_snapshot_json" \
    --argjson strict_proof_zero "$strict_zero_json" \
    --argjson page_cache_drop_attempted "$([[ "$STRICT_PAGE_CACHE_DROP_ATTEMPTED" -eq 1 ]] && echo true || echo false)" \
    --argjson page_cache_drop_succeeded "$([[ "$STRICT_PAGE_CACHE_DROP_SUCCEEDED" -eq 1 ]] && echo true || echo false)" \
    --argjson base_local_remaining "$base_local_remaining" \
    --argjson unopt_local_remaining "$unopt_local_remaining" \
    --argjson opt_local_remaining "$opt_local_remaining" \
    --argjson snapshot_records_remaining "$snapshot_records_remaining" \
    --argjson runtime_mounts_remaining "$runtime_mounts_remaining" \
    --argjson local_snapshot_entries_remaining "$local_snapshot_entries_remaining" \
    --argjson cache_entries_remaining "$cache_entries_remaining" \
    --argjson base_content_remaining "$base_content_remaining" \
    --argjson unopt_content_remaining "$unopt_content_remaining" \
    --argjson opt_content_remaining "$opt_content_remaining" \
    '{
      cold_mode: $cold_mode,
      strict_proof_zero: $strict_proof_zero,
      content_purge_scope: "targeted_closure_union",
      content_purge_broad_prune: false,
      overlayfs_relevant: false,
      base: {
        requested_ref: $base_requested_ref,
        resolved_ref: $base_resolved_ref,
        closure_digests: $base_closure
      },
      unoptimized: (
        if ($unopt_requested_ref | length) > 0 or ($unopt_resolved_ref | length) > 0 or ($unopt_closure | length) > 0 then
          {
            requested_ref: $unopt_requested_ref,
            resolved_ref: $unopt_resolved_ref,
            closure_digests: $unopt_closure
          }
        else
          null
        end
      ),
      optimized: {
        requested_ref: $opt_requested_ref,
        resolved_ref: $opt_resolved_ref,
        closure_digests: $opt_closure
      },
      shared_closure_digests: $shared_closure,
      required_zero: {
        local_image_refs_remaining_base: $base_local_remaining,
        local_image_refs_remaining_unoptimized: $unopt_local_remaining,
        local_image_refs_remaining_opt: $opt_local_remaining,
        stargz_runtime_mounts_remaining: $runtime_mounts_remaining,
        stargz_cache_entries_remaining: $cache_entries_remaining,
        content_store_digests_remaining_base: $base_content_remaining,
        content_store_digests_remaining_unoptimized: $unopt_content_remaining,
        content_store_digests_remaining_opt: $opt_content_remaining
      },
      required_true: {
        base_local_ref_deleted: ($base_local_remaining == 0),
        unoptimized_local_ref_deleted: ($unopt_local_remaining == 0),
        opt_local_ref_deleted: ($opt_local_remaining == 0)
      },
      diagnostic: {
        global_stargz_snapshot_records_remaining: $snapshot_records_remaining,
        global_stargz_local_snapshot_entries_remaining: $local_snapshot_entries_remaining,
        stargz_state_source: $stargz_state_source,
        stargz_cleanup_status: $stargz_cleanup_status,
        stargz_runtime_mount_entries_before_cleanup: $pre_runtime_mount_entries,
        stargz_runtime_mount_entries_remaining: $remaining_runtime_mount_entries,
        diagnostic_only_stargz_local_snapshot_dirs_before_cleanup: $pre_local_snapshot_dirs,
        diagnostic_only_stargz_local_snapshot_dirs_remaining_after_cleanup: $remaining_local_snapshot_dirs
      },
      best_effort: {
        page_cache_drop_attempted: $page_cache_drop_attempted,
        page_cache_drop_succeeded: $page_cache_drop_succeeded,
        page_cache_proof_mode: "required-best-effort"
      },
      unverifiable: $unverifiable
    }' > "$PROOF_JSON"
}

strict_cleanup_and_verify() {
  local label="$1" cli_image="$2"
  local base_local_remaining unopt_local_remaining opt_local_remaining snapshot_records_remaining runtime_mounts_remaining
  local local_snapshot_entries_remaining cache_entries_remaining base_content_remaining unopt_content_remaining opt_content_remaining
  local strict_zero=1
  local snapshots_dir cache_dir

  snapshots_dir="$(stargz_snapshots_dir)"
  cache_dir="$(stargz_cache_dir)"

  STRICT_UNVERIFIABLE=()
  mapfile -t STRICT_PRE_CLEANUP_RUNTIME_MOUNT_ENTRIES < <(strict_list_stargz_runtime_mount_entries)
  mapfile -t STRICT_PRE_CLEANUP_LOCAL_SNAPSHOT_DIRS < <(strict_list_stargz_local_snapshot_dirs)
  STRICT_STARGZ_STATE_SOURCE="not-detected-before-cleanup"
  STRICT_STARGZ_CLEANUP_STATUS="cleanup-in-progress"
  if [[ "${#STRICT_PRE_CLEANUP_RUNTIME_MOUNT_ENTRIES[@]}" -gt 0 || "${#STRICT_PRE_CLEANUP_LOCAL_SNAPSHOT_DIRS[@]}" -gt 0 ]]; then
    STRICT_STARGZ_STATE_SOURCE="precheck/env_check/smoke before strict cleanup"
    log "[i] strict cleanup: 发现 strict proof 前残留 stargz state，疑似由 precheck/env_check/smoke 产生"
    strict_log_state_list "strict cleanup pre-state runtime mounts" "${STRICT_PRE_CLEANUP_RUNTIME_MOUNT_ENTRIES[@]}"
    strict_log_state_list "strict cleanup pre-state local snapshot dirs" "${STRICT_PRE_CLEANUP_LOCAL_SNAPSHOT_DIRS[@]}"
  fi

  strict_cleanup_namespace_runtime_state
  strict_unmount_stargz_runtime_mounts
  drop_snapshot_records stargz
  clear_stargz_cache_if_needed stargz
  strict_drop_all_local_refs
  strict_purge_content_union

  mapfile -t STRICT_POST_CLEANUP_RUNTIME_MOUNT_ENTRIES < <(strict_list_stargz_runtime_mount_entries)
  mapfile -t STRICT_POST_CLEANUP_LOCAL_SNAPSHOT_DIRS < <(strict_list_stargz_local_snapshot_dirs)
  if [[ "${#STRICT_POST_CLEANUP_RUNTIME_MOUNT_ENTRIES[@]}" -gt 0 ]]; then
    STRICT_STARGZ_CLEANUP_STATUS="cleanup-left-state-behind"
    warn "strict cleanup 未能清空全部 stargz state；strict proof 将失败"
    strict_log_state_list "strict cleanup remaining runtime mounts" "${STRICT_POST_CLEANUP_RUNTIME_MOUNT_ENTRIES[@]}"
    strict_log_state_list "strict cleanup remaining local snapshot dirs (diagnostic-only)" "${STRICT_POST_CLEANUP_LOCAL_SNAPSHOT_DIRS[@]}"
  elif [[ "${#STRICT_POST_CLEANUP_LOCAL_SNAPSHOT_DIRS[@]}" -gt 0 ]]; then
    STRICT_STARGZ_CLEANUP_STATUS="cleanup-left-diagnostic-snapshot-dirs-only"
    strict_log_state_list "strict cleanup remaining local snapshot dirs (diagnostic-only)" "${STRICT_POST_CLEANUP_LOCAL_SNAPSHOT_DIRS[@]}"
  else
    STRICT_STARGZ_CLEANUP_STATUS="cleanup-cleared-state"
  fi

  base_local_remaining="$(strict_count_remaining_local_refs baseline "$BASE_IMAGE")"
  if triple_lazy_mode_enabled; then
    unopt_local_remaining="$(strict_count_remaining_local_refs unoptimized_lazy "$UNOPT_IMAGE")"
    unopt_content_remaining="$(strict_count_content_remaining_for_digests unoptimized_content_store "${STRICT_UNOPT_CLOSURE_DIGESTS[@]}")"
  else
    unopt_local_remaining=0
    unopt_content_remaining=0
  fi
  opt_local_remaining="$(strict_count_remaining_local_refs optimized "$OPT_IMAGE")"
  snapshot_records_remaining="$(strict_count_stargz_snapshot_records)"
  runtime_mounts_remaining="$(strict_count_stargz_runtime_mounts)"
  local_snapshot_entries_remaining="$(strict_count_dir_entries "$snapshots_dir" stargz_local_snapshot_entries)"
  cache_entries_remaining="$(strict_count_dir_entries "$cache_dir" stargz_cache_entries)"
  base_content_remaining="$(strict_count_content_remaining_for_digests base_content_store "${STRICT_BASE_CLOSURE_DIGESTS[@]}")"
  opt_content_remaining="$(strict_count_content_remaining_for_digests opt_content_store "${STRICT_OPT_CLOSURE_DIGESTS[@]}")"

  if [[ "$base_local_remaining" -ne 0 || "$unopt_local_remaining" -ne 0 || "$opt_local_remaining" -ne 0 || "$runtime_mounts_remaining" -ne 0 || "$cache_entries_remaining" -ne 0 || "$base_content_remaining" -ne 0 || "$unopt_content_remaining" -ne 0 || "$opt_content_remaining" -ne 0 ]]; then
    strict_zero=0
  fi
  if [[ "$STRICT_PAGE_CACHE_DROP_ATTEMPTED" -ne 1 || "$STRICT_PAGE_CACHE_DROP_SUCCEEDED" -ne 1 ]]; then
    strict_zero=0
  fi
  if [[ "${#STRICT_UNVERIFIABLE[@]}" -gt 0 ]]; then
    strict_zero=0
  fi
  write_strict_proof_json \
    "$base_local_remaining" \
    "$unopt_local_remaining" \
    "$opt_local_remaining" \
    "$snapshot_records_remaining" \
    "$runtime_mounts_remaining" \
    "$local_snapshot_entries_remaining" \
    "$cache_entries_remaining" \
    "$base_content_remaining" \
    "$unopt_content_remaining" \
    "$opt_content_remaining" \
    "$strict_zero"
  if [[ "$strict_zero" -ne 1 ]]; then
    echo "[ERR] strict proof failed before ${label} measurement; see ${PROOF_JSON}" >&2
    return 11
  fi
  return 0
}

validate_registry_cold_strict_contract

lazy_mode_enabled() {
  [[ "$COMPARISON_MODE" == "raw-full-vs-estargz-lazy" || "$COMPARISON_MODE" == "raw-full-vs-estargz-lazy-triple" ]]
}

measure_uses_lazy_run() {
  local label="${1:-}"
  if ! lazy_mode_enabled; then
    return 1
  fi
  if triple_lazy_mode_enabled; then
    [[ "$label" == "unoptimized_lazy" || "$label" == "optimized" ]]
  else
    [[ "$label" == "optimized" ]]
  fi
}

lazy_error_prefix() {
  local label="${1:-optimized}"
  printf '%s-lazy\n' "$label"
}

current_lazy_measure_label() {
  printf '%s\n' "${CURRENT_LAZY_LABEL:-optimized}"
}

looks_like_insecure_registry_ref() {
  local ref="${1:-}"
  [[ "$ref" == localhost/* || "$ref" == localhost:*/* || "$ref" == 127.0.0.1/* || "$ref" == 127.0.0.1:*/* ]]
}

repo_ref_without_tag_or_digest() {
  local ref="${1:-}" name="" last=""
  name="${ref%@*}"
  last="${name##*/}"
  if [[ "$last" == *:* ]]; then
    name="${name%:*}"
  fi
  printf '%s\n' "$name"
}

manifest_inspect_payload() {
  local ref="${1:-}"
  local args=( manifest inspect "$ref" --verbose )
  if looks_like_insecure_registry_ref "$ref"; then
    args+=( --insecure )
  fi
  nerdctl "${args[@]}"
}

stargz_cache_size_bytes() {
  local cache_dir
  cache_dir="$(stargz_cache_dir)"
  if [[ ! -d "$cache_dir" ]]; then
    printf '0\n'
    return 0
  fi
  du -sb "$cache_dir" 2>/dev/null | awk 'NR==1{print $1}' || printf '0\n'
}

content_store_has_any_digest() {
  local digest=""
  for digest in "$@"; do
    [[ -n "$digest" ]] || continue
    if ctr -n "$NS" content get "$digest" >/dev/null 2>&1; then
      printf '1\n'
      return 0
    fi
  done
  printf '0\n'
}

purge_content_store_digests() {
  local digest=""
  for digest in "$@"; do
    [[ -n "$digest" ]] || continue
    ctr -n "$NS" content delete "$digest" >/dev/null 2>&1 || true
  done
}

transfer_log_looks_like_full_pull() {
  local logfile="${1:-}"
  [[ -f "$logfile" ]] || return 1
  grep -Eqi 'total:[[:space:]]*[0-9]+(\.[0-9]+)?[[:space:]]*Gi(B)?|(^|[^0-9])([789](\.[0-9]+)?|8\.1)[[:space:]]*GiB([^[:alpha:]]|$)' "$logfile"
}

log_mentions_large_layer_done() {
  local logfile="${1:-}" digest="" escaped=""
  [[ -f "$logfile" ]] || return 1
  for digest in "${LAZY_LARGE_LAYER_DIGESTS[@]}"; do
    [[ -n "$digest" ]] || continue
    escaped="$(printf '%s\n' "$digest" | sed 's/[.[\*^$()+?{}|\\]/\\&/g')"
    if grep -Eqi "${escaped}.*(done|100%|complete)" "$logfile"; then
      return 0
    fi
  done
  return 1
}

content_store_present_layer_bytes() {
  local digest="" total=0 size=0
  for digest in "${LAZY_ALL_LAYER_DIGESTS[@]}"; do
    [[ -n "$digest" ]] || continue
    if ctr -n "$NS" content get "$digest" >/dev/null 2>&1; then
      size="${LAZY_LAYER_SIZE_BYTES[$digest]:-0}"
      [[ "$size" =~ ^[0-9]+$ ]] || size=0
      total=$(( total + size ))
    fi
  done
  printf '%s\n' "$total"
}

command_to_string() {
  local out="" arg=""
  for arg in "$@"; do
    printf -v out '%s%q ' "$out" "$arg"
  done
  printf '%s\n' "${out% }"
}

measure_run_index() {
  if [[ "${CURRENT_RUN_INDEX:-0}" =~ ^[0-9]+$ && "${CURRENT_RUN_INDEX:-0}" -gt 0 ]]; then
    printf '%s\n' "${CURRENT_RUN_INDEX}"
    return 0
  fi
  MEASURE_SEQ=$(( MEASURE_SEQ + 1 ))
  printf '%s\n' "${MEASURE_SEQ}"
}

measure_artifact_name() {
  local label="$1" run_idx="$2"
  local safe_label
  safe_label="$(printf '%s\n' "$label" | tr ':/@' '_')"
  printf 'cs-%s-%s\n' "$safe_label" "$run_idx"
}

resolve_lazy_reference_metadata() {
  local ref="${1:-}"
  local root_payload root_doc root_digest manifest_digest child_ref child_payload child_doc
  local platform_os platform_arch platform_variant rest

  LAZY_RESOLVED_REF="$ref"
  LAZY_METADATA_OK=0
  LAZY_CLOSURE_DIGESTS=()
  LAZY_ALL_LAYER_DIGESTS=()
  LAZY_LARGE_LAYER_DIGESTS=()
  LAZY_LAYER_SIZE_BYTES=()
  LAZY_TOTAL_LAYER_SIZE_BYTES=0
  LAZY_MAX_LAYER_SIZE_BYTES=0

  root_payload="$(manifest_inspect_payload "$ref" 2>/dev/null)" || return 1
  root_doc="$(jq -c '
    if (.OCIManifest | type) == "object" then .OCIManifest
    elif (.SchemaV2Manifest | type) == "object" then .SchemaV2Manifest
    elif (.manifest | type) == "object" then .manifest
    else .
    end
  ' <<<"$root_payload" 2>/dev/null)" || return 1
  root_digest="$(jq -r '.Descriptor.digest // .descriptor.digest // empty' <<<"$root_payload" 2>/dev/null)"

  platform_os="${PLATFORM%%/*}"
  rest="${PLATFORM#*/}"
  platform_arch="${rest%%/*}"
  if [[ "$rest" == */* ]]; then
    platform_variant="${rest#*/}"
  else
    platform_variant=""
  fi

  child_doc="$root_doc"
  manifest_digest="$root_digest"
  if jq -e '.manifests | type == "array" and length > 0' >/dev/null 2>&1 <<<"$root_doc"; then
    manifest_digest="$(jq -r \
      --arg os "$platform_os" \
      --arg arch "$platform_arch" \
      --arg variant "$platform_variant" '
        def matches_platform:
          (.platform.os // "") == $os and
          (.platform.architecture // "") == $arch and
          (($variant == "") or ((.platform.variant // "") == $variant));
        [(.manifests // [])[] | select(matches_platform)] as $candidates
        | if ($candidates | length) == 1 then
            ($candidates[0].digest // empty)
          elif $variant == "" then
            ([ $candidates[] | select((.platform.variant // "") == "") ] | if length == 1 then (.[0].digest // empty) else empty end)
          else
            empty
          end
      ' <<<"$root_doc" 2>/dev/null)"
    [[ -n "$manifest_digest" ]] || return 1
    child_ref="$(repo_ref_without_tag_or_digest "$ref")@${manifest_digest}"
    child_payload="$(manifest_inspect_payload "$child_ref" 2>/dev/null)" || return 1
    child_doc="$(jq -c '
      if (.OCIManifest | type) == "object" then .OCIManifest
      elif (.SchemaV2Manifest | type) == "object" then .SchemaV2Manifest
      elif (.manifest | type) == "object" then .manifest
      else .
      end
    ' <<<"$child_payload" 2>/dev/null)" || return 1
    manifest_digest="$(jq -r '.Descriptor.digest // .descriptor.digest // empty' <<<"$child_payload" 2>/dev/null || true)"
    [[ -n "$manifest_digest" ]] || manifest_digest="${child_ref##*@}"
    LAZY_RESOLVED_REF="$child_ref"
  elif [[ -n "$manifest_digest" ]]; then
    LAZY_RESOLVED_REF="$(repo_ref_without_tag_or_digest "$ref")@${manifest_digest}"
  fi

  mapfile -t LAZY_CLOSURE_DIGESTS < <(
    {
      printf '%s\n' "$root_digest"
      printf '%s\n' "$manifest_digest"
      jq -r '.config.digest // empty' <<<"$child_doc" 2>/dev/null
      jq -r '.layers[]?.digest // empty' <<<"$child_doc" 2>/dev/null
    } | awk 'NF && !seen[$0]++'
  )
  while IFS=$'\t' read -r digest size; do
    [[ -n "$digest" ]] || continue
    [[ "$size" =~ ^[0-9]+$ ]] || size=0
    LAZY_ALL_LAYER_DIGESTS+=("$digest")
    LAZY_LAYER_SIZE_BYTES["$digest"]="$size"
    LAZY_TOTAL_LAYER_SIZE_BYTES=$(( LAZY_TOTAL_LAYER_SIZE_BYTES + size ))
    if (( size > LAZY_MAX_LAYER_SIZE_BYTES )); then
      LAZY_MAX_LAYER_SIZE_BYTES="$size"
    fi
  done < <(jq -r '.layers[]? | [.digest // empty, ((.size // 0) | tostring)] | @tsv' <<<"$child_doc" 2>/dev/null)
  mapfile -t LAZY_LARGE_LAYER_DIGESTS < <(
    jq -r '.layers[]? | select((.size // 0) >= 1073741824) | .digest // empty' <<<"$child_doc" 2>/dev/null \
      | awk 'NF && !seen[$0]++'
  )
  LAZY_METADATA_OK=1
  return 0
}

drop_local_image_ref_force() {
  local img="$1"
  [[ -n "$img" ]] || return 0
  log "[i] 移除本地 image ref（若存在）：${img}"
  nerdctl --namespace "$NS" image rm "$img" >/dev/null 2>&1 || true
}

extract_port_from_url() {
  local url="$1"
  [[ "$url" == *"://"* ]] || return 1
  local rest="${url#*://}"
  rest="${rest%%/*}"
  local host port
  if [[ "$rest" =~ ^\\[.*\\](:[0-9]+)?$ ]]; then
    host="${rest%%]*}"; host="${host#[}"
    port="${rest#*]:}"; port="${port#:}"
  else
    host="${rest%%:*}"
    port="${rest#*:}"
    [[ "$host" == "$rest" ]] && port=""
  fi
  case "$host" in
    127.0.0.1|localhost|::1) ;;
    *) return 1;;
  esac
  [[ -n "$port" && "$port" =~ ^[0-9]+$ ]] || return 1
  echo "$port"
}

port_in_use() {
  local port="$1"
  ss -ltnp | awk -v pat=":${port}" '$4 ~ pat {found=1} END{exit found ? 0 : 1}'
}

host_port_reserved() {
  local port="$1" value=""
  for value in "${LOOPBACK_PORT_MAP[@]}"; do
    [[ "$value" == "$port" ]] && return 0
  done
  [[ -n "${USED_PUBLISHED_HOST_PORTS[$port]:-}" ]] && return 0
  return 1
}

choose_free_host_port() {
  local port
  for (( port=AUTO_PUBLISH_PORT_START; port<=AUTO_PUBLISH_PORT_END; port++ )); do
    if ! host_port_reserved "$port" && ! port_in_use "$port"; then
      echo "$port"
      return 0
    fi
  done
  return 1
}

kill_port_occupants() {
  local port="$1" attempts=2
  while (( attempts-- >= 0 )); do
    local pid
    pid="$(ss -ltnp | awk -v pat=":${port}" '$4 ~ pat {print $6}' | sed -n 's/.*pid=\([0-9]\+\).*/\1/p' | head -1 || true)"
    [[ -n "$pid" ]] || return 0
    log "[i] 释放端口 ${port}: kill PID=${pid}"
    kill -TERM "${pid}" 2>/dev/null || true
    sleep 0.5
    if port_in_use "$port"; then
      kill -KILL "${pid}" 2>/dev/null || true
      sleep 0.3
    else
      return 0
    fi
  done
  if port_in_use "$port"; then
    return 1
  fi
  return 0
}

rewrite_loopback_url_port() {
  local url="$1" new_port="$2"
  [[ "$url" == *"://"* ]] || return 1
  local scheme="${url%%://*}"
  local rest="${url#*://}"
  local suffix=""
  if [[ "$rest" == */* ]]; then
    suffix="/${rest#*/}"
  fi
  printf '%s://127.0.0.1:%s%s\n' "$scheme" "$new_port" "$suffix"
}

default_debug_state_url_from_ready() {
  local ready_url="$1"
  [[ "$ready_url" =~ ^https?://(127\.0\.0\.1|localhost)(:[0-9]+)?(/.*)?$ ]] || return 1
  local scheme="${ready_url%%://*}"
  local rest="${ready_url#*://}"
  local authority="$rest"
  if [[ "$authority" == */* ]]; then
    authority="${authority%%/*}"
  fi
  printf '%s://%s/debug/state\n' "$scheme" "$authority"
}

resolve_effective_debug_state_url() {
  local candidate="$DEBUG_STATE_URL"
  if [[ -z "$candidate" ]]; then
    candidate="$(default_debug_state_url_from_ready "$EFFECTIVE_READY_URL" 2>/dev/null || true)"
  fi
  if [[ -z "$candidate" ]]; then
    printf '\n'
    return 0
  fi
  local container_port=""
  container_port="$(extract_port_from_url "$candidate" 2>/dev/null || true)"
  if [[ -n "$container_port" && -n "${LOOPBACK_PORT_MAP[$container_port]:-}" ]]; then
    rewrite_loopback_url_port "$candidate" "${LOOPBACK_PORT_MAP[$container_port]}"
    return 0
  fi
  printf '%s\n' "$candidate"
}

map_loopback_container_port() {
  local container_port="$1" preferred_host_port="$2"
  local mapped="${LOOPBACK_PORT_MAP[$container_port]:-}"
  if [[ -n "$mapped" ]]; then
    echo "$mapped"
    return 0
  fi

  local host_port="$preferred_host_port"
  if [[ -n "$host_port" ]]; then
    if host_port_reserved "$host_port"; then
      host_port=""
    elif port_in_use "$host_port"; then
      if [[ "$FORCE_KILL_PORT" -eq 1 ]]; then
        if kill_port_occupants "$host_port"; then
          log "[i] 端口 ${host_port} 已释放"
        else
          warn "无法释放端口 ${host_port}，将尝试自动重映射"
          host_port=""
        fi
      else
        host_port=""
      fi
    fi
  fi

  if [[ -z "$host_port" ]]; then
    host_port="$(choose_free_host_port)" || return 1
    warn "宿主端口 ${preferred_host_port} 已被占用，改用 published 端口 ${host_port}->${container_port}"
  fi

  LOOPBACK_PORT_MAP["$container_port"]="$host_port"
  USED_PUBLISHED_HOST_PORTS["$host_port"]=1
  PUBLISH_ARGS+=( -p "127.0.0.1:${host_port}:${container_port}" )
  echo "$host_port"
}

bridge_cni_available() {
  local cni_path="${CNI_PATH:-/opt/cni/bin}"
  [[ -x "${cni_path}/bridge" ]]
}

prepare_service_network() {
  LOOPBACK_PORT_MAP=()
  PUBLISH_ARGS=()
  RUNTIME_NETWORK_MODE="host"
  EFFECTIVE_READY_URL="$READY_URL"
  EFFECTIVE_DEBUG_STATE_URL="$DEBUG_STATE_URL"
  EFFECTIVE_PREDICT_URL="$PREDICT_URL"
  EFFECTIVE_READY_PORT="$(extract_port_from_url "$READY_URL" 2>/dev/null || true)"
  NETWORK_PREP_ERROR=""

  [[ "$MODE" == "ready" ]] || return 0

  if [[ -n "$READY_EXEC" || -n "$READY_LOG_REGEX" ]]; then
    RUNTIME_NETWORK_MODE="none"
    EFFECTIVE_READY_PORT=""
    EFFECTIVE_DEBUG_STATE_URL=""
    return 0
  fi

  local ready_container_port=""
  ready_container_port="$(extract_port_from_url "$READY_URL" 2>/dev/null || true)"
  if [[ -z "$ready_container_port" ]]; then
    return 0
  fi

  if ! port_in_use "$ready_container_port"; then
    RUNTIME_NETWORK_MODE="host"
    EFFECTIVE_READY_PORT="$ready_container_port"
    EFFECTIVE_DEBUG_STATE_URL="$(resolve_effective_debug_state_url)"
    return 0
  fi

  if [[ "$FORCE_KILL_PORT" -eq 1 ]]; then
    if kill_port_occupants "$ready_container_port"; then
      log "[i] 端口 ${ready_container_port} 已释放"
      RUNTIME_NETWORK_MODE="host"
      EFFECTIVE_READY_PORT="$ready_container_port"
      EFFECTIVE_DEBUG_STATE_URL="$(resolve_effective_debug_state_url)"
      return 0
    fi
  fi

  if [[ "$ALLOW_BUSY_PORT" -eq 1 ]]; then
    warn "端口 ${ready_container_port} 被占用，按用户请求继续使用 host network（结果可能不可靠）"
    RUNTIME_NETWORK_MODE="host"
    EFFECTIVE_READY_PORT="$ready_container_port"
    EFFECTIVE_DEBUG_STATE_URL="$(resolve_effective_debug_state_url)"
    return 0
  fi

  if ! bridge_cni_available; then
    NETWORK_PREP_ERROR="loopback ready-url port ${ready_container_port} is busy, but CNI bridge plugin is unavailable for published-port remap"
    return 1
  fi

  map_loopback_container_port "$ready_container_port" "$ready_container_port" >/dev/null || return 1
  local mapped_ready_port="${LOOPBACK_PORT_MAP[$ready_container_port]:-}"
  [[ -n "$mapped_ready_port" ]] || return 1
  EFFECTIVE_READY_PORT="$mapped_ready_port"
  EFFECTIVE_READY_URL="$(rewrite_loopback_url_port "$READY_URL" "$mapped_ready_port")"
  RUNTIME_NETWORK_MODE="publish"

  if [[ -n "$PREDICT_URL" ]]; then
    local predict_container_port=""
    predict_container_port="$(extract_port_from_url "$PREDICT_URL" 2>/dev/null || true)"
    if [[ -n "$predict_container_port" ]]; then
      map_loopback_container_port "$predict_container_port" "$predict_container_port" >/dev/null || return 1
      local mapped_predict_port="${LOOPBACK_PORT_MAP[$predict_container_port]:-}"
      [[ -n "$mapped_predict_port" ]] || return 1
      EFFECTIVE_PREDICT_URL="$(rewrite_loopback_url_port "$PREDICT_URL" "$mapped_predict_port")"
    fi
  fi

  EFFECTIVE_DEBUG_STATE_URL="$(resolve_effective_debug_state_url)"

  log "[i] ready probe URL: ${READY_URL} -> ${EFFECTIVE_READY_URL}"
  if [[ "$EFFECTIVE_DEBUG_STATE_URL" != "$DEBUG_STATE_URL" && -n "$EFFECTIVE_DEBUG_STATE_URL" && -n "$DEBUG_STATE_URL" ]]; then
    log "[i] debug state URL: ${DEBUG_STATE_URL} -> ${EFFECTIVE_DEBUG_STATE_URL}"
  fi
  if [[ "$EFFECTIVE_PREDICT_URL" != "$PREDICT_URL" && -n "$PREDICT_URL" ]]; then
    log "[i] predict URL: ${PREDICT_URL} -> ${EFFECTIVE_PREDICT_URL}"
  fi
}

exists_image() {
  local img="$1"; local target_norm; target_norm="$(normalize_ref "$img")"
  local found=1 dg=""
  # 若是 digest 形式，先走直查
  if [[ "$img" == *@sha256:* ]]; then
    dg="${img##*@}"
    if ctr-remote -n "$NS" content get "$dg" >/dev/null 2>&1; then
      return 0
    fi
  fi
  found=1
  while read -r name; do
    [[ -z "$name" ]] && continue
    if [[ "$name" == "$img" ]]; then found=0; break; fi
    local n; n="$(normalize_ref "$name")"
    if [[ "$n" == "$target_norm" ]]; then found=0; break; fi
  done < <(ctr-remote -n "$NS" images ls 2>/dev/null | awk 'NR>1{print $1}')
  return $found
}

ensure_local_image() {
  local img="$1" label="${2:-image}"
  if ! nerdctl --namespace "$NS" image inspect "$img" >/dev/null 2>&1; then
    echo "[ERR] ${label} image is not available to nerdctl in namespace ${NS}: ${img}" >&2
    echo "[ERR] 请先确认: nerdctl --namespace ${NS} image inspect ${img}" >&2
    exit 3
  fi
}

preflight_local_images_if_needed() {
  [[ "$PULL_POLICY" == "never" ]] || return 0
  lazy_mode_enabled && return 0
  log "[i] 预检查本地镜像可用性（nerdctl --namespace ${NS} image inspect）"
  ensure_local_image "$BASE_IMAGE" "baseline"
  ensure_local_image "$OPT_IMAGE" "optimized"
}

drop_local_image_ref_if_needed() {
  local img="$1"
  [[ "$DROP_LOCAL_IMAGE_REF" -eq 1 ]] || return 0
  drop_local_image_ref_force "$img"
}

perform_explicit_pull_if_needed() {
  local label="$1" img="$2" snap="$3"
  if measure_uses_lazy_run "$label"; then
    printf '%s\n' "0"
    return 0
  fi
  if triple_lazy_mode_enabled && [[ "$label" == "baseline" ]]; then
    :
  elif ! lazy_mode_enabled; then
    if [[ "$PULL_POLICY" == "never" ]]; then
      printf '%s\n' "0"
      return 0
    fi
    if [[ "$PULL_POLICY" == "missing" ]] && exists_image "$img"; then
      printf '%s\n' "0"
      return 0
    fi
  elif [[ "$label" != "baseline" ]]; then
    printf '%s\n' "0"
    return 0
  fi
  local args=( pull --snapshotter "$snap" --platform "$PLATFORM" )
  local t0 t1
  t0=$(ms_now)
  if ! nerdctl --namespace "$NS" "${args[@]}" "$img" >/dev/null 2>&1; then
    return 1
  fi
  t1=$(ms_now)
  printf '%s\n' "$(( t1 - t0 ))"
}

# ============ 代理与 hosts.toml ============
ensure_hosts_toml() {
  local d="/etc/containerd/certs.d/127.0.0.1:5001"
  local f="${d}/hosts.toml"
  if [[ ! -f "$f" ]]; then
    log "[i] 写入 hosts.toml -> $f"
    sudo mkdir -p "$d"
    sudo bash -c "cat > '$f'" <<'TOML'
server = "http://127.0.0.1:5001"

[host."http://127.0.0.1:5001"]
  capabilities = ["pull", "resolve"]
  skip_verify = true
TOML
  fi
}

probe_proxy_path() {
  # 仅在镜像走 127.0.0.1:5001 时进行基本探活与 Range 206 校验
  local ref="$1"
  if [[ "$ref" != 127.0.0.1:5001/* ]]; then
    return 0
  fi
  log "[i] 代理探活: /v2/"
  curl -sS --noproxy 127.0.0.1,localhost,::1 -D- http://127.0.0.1:5001/v2/ -o /dev/null | sed -n '1,5p' >&2 || true

  # 解析 repo 与 tag
  local path tag repo
  path="${ref#127.0.0.1:5001/}"
  repo="${path%:*}"
  tag="${path##*:}"
  log "[i] manifest 头: repo=${repo} tag=${tag}"
  curl -sS --noproxy 127.0.0.1,localhost,::1 -D- -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json;q=0.9' \
    "http://127.0.0.1:5001/v2/${repo}/manifests/${tag}" -o /dev/null | sed -n '1,20p' >&2 || true

  # 取第一层 digest 做 0-4095 Range 测试：
  # 若返回的是 index（manifests 字段），则按 --platform 选中对应 manifest，再取 layers[0]
  local raw dg
  raw=$(curl -sS --noproxy 127.0.0.1,localhost,::1 -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json;q=0.9' \
      "http://127.0.0.1:5001/v2/${repo}/manifests/${tag}" || true)
  if jq -e '.manifests' >/dev/null 2>&1 <<<"$raw"; then
    local arch os child_digest
    arch="${PLATFORM#*/}"; os="${PLATFORM%%/*}"
    child_digest=$(jq -r --arg os "$os" --arg arch "$arch" \
      '.manifests[] | select(.platform.os==$os and .platform.architecture==$arch) | .digest' <<<"$raw" | head -1)
    if [[ -n "$child_digest" ]]; then
      raw=$(curl -sS --noproxy 127.0.0.1,localhost,::1 -H 'Accept: application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json' \
        "http://127.0.0.1:5001/v2/${repo}/manifests/${child_digest}" || true)
    fi
  fi
  dg=$(jq -r '.layers[0].digest // empty' <<<"$raw" 2>/dev/null)
  if [[ -n "$dg" ]]; then
    log "[i] blob Range 校验: ${dg}"
    curl -sS --noproxy 127.0.0.1,localhost,::1 -D- -H 'Range: bytes=0-4095' \
      "http://127.0.0.1:5001/v2/${repo}/blobs/${dg}" -o /dev/null | sed -n '1,20p' >&2 || true
  fi
}

# ============ stargz 目录/元数据维护 ============
reset_snapshotter() {
  [[ "${SNAPSHOTTER:-}" == "stargz" ]] || { log "[i] --force-reset 仅适用于 stargz"; return 0; }

  local snapper
  snapper="$(stargz_snapshotter_service_name)"
  local backup="/var/lib/containerd-stargz-grpc-backup-$(date +%s)"
  log "[i] 停止 ${snapper}"
  sudo systemctl stop "${snapper}" || true
  log "[i] 停止 containerd.service"
  sudo systemctl stop containerd.service || true
  sudo mkdir -p "${backup}"
  if [[ -d /var/lib/containerd-stargz-grpc ]]; then
    sudo mv /var/lib/containerd-stargz-grpc "${backup}/stargz" || true
  fi
  sudo mkdir -p /var/lib/containerd-stargz-grpc/snapshotter/snapshots
  sudo mkdir -p /var/lib/containerd-stargz-grpc/snapshotter/cache
  local meta=/var/lib/containerd/io.containerd.metadata.v1.bolt/meta.db
  if [[ -f "${meta}" ]]; then
    sudo mv "${meta}" "${backup}/meta.db" || true
  fi
    log "[i] 启动 containerd"
    sudo systemctl start containerd

  log "[i] 启动 ${snapper}"
  sudo systemctl start "${snapper}" || true
}

stargz_snapshotter_service_name() {
  printf '%s\n' "${STARGZ_SERVICE:-containerd-stargz-grpc.service}"
}

run_systemctl_logged() {
  local tracef="$1" name="$2" action="$3"; shift 3
  local logfile="${LOG_DIR}/${name}.lazy.reset.${action}.log"
  local rc=0

  "$@" >"$logfile" 2>&1 || rc=$?

  if [[ -n "$tracef" ]]; then
    printf 'lazy_reset_%s_command=%s\n' "$action" "$(command_to_string "$@")" >>"$tracef"
    printf 'lazy_reset_%s_exit_code=%s\n' "$action" "$rc" >>"$tracef"
    printf 'lazy_reset_%s_log=%s\n' "$action" "$logfile" >>"$tracef"
  fi

  return "$rc"
}

record_stargz_service_show_state() {
  local tracef="$1" name="$2" snapper="$3"
  local show_log="${LOG_DIR}/${name}.lazy.reset.show.log"
  if command -v systemctl >/dev/null 2>&1; then
    systemctl show -p LoadState -p ActiveState -p UnitFileState "$snapper" >"$show_log" 2>&1 || true
  else
    printf 'systemctl not found\n' >"$show_log"
  fi
  [[ -n "$tracef" ]] && printf 'lazy_reset_show_log=%s\n' "$show_log" >>"$tracef"
}

restart_stargz_snapshotter_service() {
  local snapper name tracef
  snapper="$(stargz_snapshotter_service_name)"
  name="$(measure_artifact_name "$(current_lazy_measure_label)" "${CURRENT_RUN_INDEX:-0}")"
  tracef=""
  command -v systemctl >/dev/null 2>&1 || return 1
  log "[i] lazy reset: restart ${snapper}"
  run_systemctl_logged "$tracef" "$name" "restart.stop" sudo systemctl stop "$snapper" || true
  run_systemctl_logged "$tracef" "$name" "restart.start" sudo systemctl start "$snapper" || return 1
  run_systemctl_logged "$tracef" "$name" "restart.is_active" systemctl is-active --quiet "$snapper" || return 1
  return 0
}

prepare_stargz_for_lazy_run() {
  local run_idx="${1:-0}" tracef="${2:-}"
  local snapper snapshotter_root mount_count_after_stop mount_count_after_start
  local name stop_rc is_active_after_stop_rc start_rc is_active_after_start_rc
  [[ "$run_idx" =~ ^[0-9]+$ ]] || run_idx=0
  log "[i] lazy reset: run=${run_idx} mode=${LAZY_STARGZ_RESET_MODE}"
  snapper="$(stargz_snapshotter_service_name)"
  name="$(measure_artifact_name "$(current_lazy_measure_label)" "$run_idx")"
  snapshotter_root="$(stargz_snapshotter_root_dir)"
  if [[ -n "$tracef" ]]; then
    printf 'lazy_reset_service=%s\n' "$snapper" >>"$tracef"
    printf 'lazy_reset_snapshotter_root=%s\n' "$snapshotter_root" >>"$tracef"
  fi
  if ! command -v systemctl >/dev/null 2>&1; then
    warn "systemctl 不可用，无法执行 stargz lazy hard reset"
    record_stargz_service_show_state "$tracef" "$name" "$snapper"
    return 1
  fi
  strict_cleanup_namespace_runtime_state
  strict_unmount_stargz_runtime_mounts
  case "$LAZY_STARGZ_RESET_MODE" in
    hard-reset|auto|"")
      log "[i] lazy reset: stop ${snapper}"
      stop_rc=0
      run_systemctl_logged "$tracef" "$name" "stop" sudo systemctl stop "$snapper" || stop_rc=$?
      is_active_after_stop_rc=0
      run_systemctl_logged "$tracef" "$name" "is_active_after_stop" systemctl is-active --quiet "$snapper" || is_active_after_stop_rc=$?
      if [[ "$stop_rc" -ne 0 && "$is_active_after_stop_rc" -eq 0 ]]; then
        warn "lazy reset: stop ${snapper} 失败且服务仍处于 active"
        record_stargz_service_show_state "$tracef" "$name" "$snapper"
        return 1
      fi
      strict_unmount_stargz_runtime_mounts
      mount_count_after_stop="$(strict_count_stargz_runtime_mounts)"
      if [[ "$mount_count_after_stop" -ne 0 ]]; then
        warn "lazy reset: stop ${snapper} 后仍残留 ${mount_count_after_stop} 个 stargz runtime mounts，继续尝试 lazy umount"
        strict_unmount_stargz_runtime_mounts
      fi
      mount_count_after_stop="$(strict_count_stargz_runtime_mounts)"
      if [[ "$mount_count_after_stop" -ne 0 ]]; then
        warn "lazy reset: 无法在 hard reset 前清空全部 stargz runtime mounts"
        [[ -n "$tracef" ]] && printf 'lazy_reset_mounts_after_stop=%s\n' "$mount_count_after_stop" >>"$tracef"
        record_stargz_service_show_state "$tracef" "$name" "$snapper"
        return 1
      fi
      log "[i] lazy reset: rm -rf ${snapshotter_root}"
      sudo rm -rf "${snapshotter_root}" >/dev/null 2>&1 || true
      log "[i] lazy reset: start ${snapper}"
      start_rc=0
      run_systemctl_logged "$tracef" "$name" "start" sudo systemctl start "$snapper" || start_rc=$?
      is_active_after_start_rc=0
      run_systemctl_logged "$tracef" "$name" "is_active_after_start" systemctl is-active --quiet "$snapper" || is_active_after_start_rc=$?
      mount_count_after_start="$(strict_count_stargz_runtime_mounts)"
      [[ -n "$tracef" ]] && {
        printf 'lazy_reset_mounts_after_start=%s\n' "$mount_count_after_start"
      } >>"$tracef"
      if [[ "$start_rc" -ne 0 || "$is_active_after_start_rc" -ne 0 ]]; then
        warn "lazy reset: start ${snapper} 或 is-active 校验失败"
        record_stargz_service_show_state "$tracef" "$name" "$snapper"
        return 1
      fi
      if [[ "$mount_count_after_start" -ne 0 ]]; then
        warn "lazy reset: start ${snapper} 后仍残留 ${mount_count_after_start} 个 stargz runtime mounts"
        record_stargz_service_show_state "$tracef" "$name" "$snapper"
        return 1
      fi
      ;;
    restart-snapshotter)
      restart_stargz_snapshotter_service || return 1
      ;;
    none)
      ;;
    *)
      warn "unknown LAZY_STARGZ_RESET_MODE=${LAZY_STARGZ_RESET_MODE}; falling back to hard-reset"
      LAZY_STARGZ_RESET_MODE="hard-reset"
      prepare_stargz_for_lazy_run "$run_idx" "$tracef"
      return $?
      ;;
  esac
  ensure_stargz_dirs || true
  capture_stargz_runtime_mounts_snapshot "${LOG_DIR}/$(measure_artifact_name "$(current_lazy_measure_label)" "$run_idx").lazy.reset.findmnt.log"
  [[ -n "$tracef" ]] && printf 'lazy_reset_mode=%s\n' "$LAZY_STARGZ_RESET_MODE" >>"$tracef"
}

capture_stargz_runtime_mounts_snapshot() {
  local outfile="$1"
  if command -v findmnt >/dev/null 2>&1; then
    findmnt -rn -o TARGET,SOURCE,FSTYPE,OPTIONS 2>/dev/null \
      | grep -E 'containerd-stargz-grpc|fuse\.stargz' >"$outfile" || true
  else
    strict_list_stargz_runtime_mount_entries >"$outfile" || true
  fi
  if [[ ! -s "$outfile" ]]; then
    printf '(no stargz runtime mounts)\n' >"$outfile"
  fi
}

capture_stargz_snapshot_dir_listing() {
  local outfile="$1" snapshots_dir=""
  snapshots_dir="$(stargz_snapshots_dir)"
  if [[ -d "$snapshots_dir" ]]; then
    find "$snapshots_dir" -mindepth 1 -maxdepth 2 2>/dev/null | sort >"$outfile" || true
  fi
  if [[ ! -s "$outfile" ]]; then
    printf '(no snapshot dirs under %s)\n' "$snapshots_dir" >"$outfile"
  fi
}

capture_stargz_journal_window() {
  local outfile="$1" since_ms="$2" until_ms="$3"
  local since_epoch until_epoch
  since_epoch=$(( since_ms / 1000 ))
  until_epoch=$(( (until_ms + 999) / 1000 ))
  if command -v journalctl >/dev/null 2>&1; then
    journalctl -u "$(stargz_snapshotter_service_name)" --since "@${since_epoch}" --until "@${until_epoch}" --no-pager >"$outfile" 2>&1 || true
  fi
  if [[ ! -s "$outfile" ]]; then
    printf 'journalctl unavailable or no logs in window [%s, %s]\n' "$since_epoch" "$until_epoch" >"$outfile"
  fi
}

capture_lazy_failure_diagnostics() {
  local name="$1" tracef="$2" since_ms="$3" until_ms="$4"
  local findmntf snapshotsf journalf
  findmntf="${LOG_DIR}/${name}.lazy.findmnt.log"
  snapshotsf="${LOG_DIR}/${name}.lazy.snapshots.log"
  journalf="${LOG_DIR}/${name}.lazy.journal.log"
  capture_stargz_runtime_mounts_snapshot "$findmntf"
  capture_stargz_snapshot_dir_listing "$snapshotsf"
  capture_stargz_journal_window "$journalf" "$since_ms" "$until_ms"
  {
    printf 'failure_findmnt_log=%s\n' "$findmntf"
    printf 'failure_snapshot_listing_log=%s\n' "$snapshotsf"
    printf 'failure_journal_log=%s\n' "$journalf"
  } >>"$tracef"
}

lazy_run_failure_error_code() {
  local runerr_file="$1"
  if [[ -f "$runerr_file" ]] && grep -Fq 'stat /usr/local/bin/server: no such file or directory' "$runerr_file"; then
    printf '%s\n' "stargz-rootfs-entrypoint-missing"
    return 0
  fi
  printf '%s\n' "nerdctl-run-failed"
}

smoke_check() {
  log "[i] smoke check: nerdctl --net=none /bin/true"
  if exists_image "$BASE_IMAGE"; then
    sudo nerdctl --namespace "$NS" run --rm --net=none --pull=never --snapshotter "$SNAPSHOTTER" --entrypoint /bin/true "$BASE_IMAGE" >/dev/null 2>&1 || true
  else
    warn "跳过 smoke check（本地找不到 BASE_IMAGE）"
  fi
}


ensure_stargz_dirs() {
  # stargz-snapshotter 运行所需的目录骨架（幂等）
  sudo mkdir -p "$(stargz_snapshots_dir)" || true
  sudo mkdir -p "$(stargz_cache_dir)" || true
}

reconcile_stargz_snapshots() {
  local snap="$1"
  [[ "$snap" == "stargz" ]] || return 0
  local base
  base="$(stargz_snapshots_dir)"
  [[ -d "$base" ]] || return 0
  log "[i] 对齐 stargz 快照目录：为元数据中存在的快照补建目录"
  local keys
  if ! keys=$(ctr-remote -n "$NS" snapshots --snapshotter stargz ls 2>/dev/null | awk 'NR>1{print $1}'); then
    return 0
  fi
  while read -r k; do
    [[ -z "$k" ]] && continue
    local dir="$base/$k"
    if [[ ! -d "$dir" ]]; then
      if [[ "$k" =~ ^[0-9]+$ ]]; then
        sudo mkdir -p "$dir/fs" || true
      else
        sudo mkdir -p "$dir" || true
      fi
      log "[i]  -> 补建目录 ${dir}"
    fi
  done <<< "$keys"
}

# ============ 冷启动清理 ============
clear_page_cache() {
  [[ "$DROP_PAGE_CACHE" -eq 1 ]] || return 0
  STRICT_PAGE_CACHE_DROP_ATTEMPTED=1
  STRICT_PAGE_CACHE_DROP_SUCCEEDED=0
  log "[i] 清理 Linux 页缓存 (echo 3 > drop_caches)"
  sync || true
  if echo 3 | sudo tee /proc/sys/vm/drop_caches >/dev/null; then
    STRICT_PAGE_CACHE_DROP_SUCCEEDED=1
  else
    warn "drop_caches 执行失败"
  fi
}

clear_stargz_cache_if_needed() {
  local snap="$1"
  [[ "$snap" == "stargz" && "$CLEAR_STARGZ_CACHE" -eq 1 ]] || return 0
  local stargz_dir cache_dir
  stargz_dir="$(stargz_root_dir)"
  # Clear only snapshotter/cache; metadata/snapshots must remain intact.
  cache_dir="$(stargz_cache_dir)"
  if [[ -d "${stargz_dir}" ]]; then
    log "[i] 清理 ${cache_dir}（保留 metadata/snapshots）"
    sudo rm -rf "${cache_dir}"/* 2>/dev/null || true
  fi
  ensure_stargz_dirs
}

drop_snapshot_records() {
  local snap="$1"
  [[ -n "$snap" ]] || return 0
  local keys
  keys=$(ctr-remote -n "$NS" snapshots --snapshotter "$snap" ls 2>/dev/null | awk 'NR>1{print $1}' || true)
  [[ -n "$keys" ]] || return 0
  log "[i] 删除 snapshot 层 (snapshotter=${snap})"
  while read -r key; do
    [[ -n "$key" ]] || continue
    ctr-remote -n "$NS" snapshots --snapshotter "$snap" rm "$key" >/dev/null 2>&1 || true
  done <<< "$keys"
}

stop_rm_container() {
  local name="$1"
  nerdctl --namespace "$NS" stop "$name" >/dev/null 2>&1 || true
  nerdctl --namespace "$NS" rm -f "$name" >/dev/null 2>&1 || true
}

hard_cold_reset() {
  if [[ "${FORCE_RESET:-0}" == "1" ]]; then
    reset_snapshotter
    smoke_check
  fi

  local img="$1" snap="$2"
  log "[i] 硬冷清理(安全)：image=${img} snapshotter=${snap}"
  drop_snapshot_records "$snap"
  clear_stargz_cache_if_needed "$snap"
  if [[ "$snap" == "stargz" ]]; then
    ensure_stargz_dirs || true
    if [[ "$RECONCILE_STARGZ_DIRS" -eq 1 ]]; then
      reconcile_stargz_snapshots "$snap" || true
    fi
  fi
  clear_page_cache
}

verify_cold_proof() {
  # 简要证明：无 stargz mount；stargz 快照目录为空（若存在）。
  if strict_count_stargz_runtime_mounts | grep -qv '^0$'; then
    warn "存在 stargz 挂载(非致命)"
  else
    log "[i] 无 stargz 挂载"
  fi
  local snap_dir="/var/lib/containerd/io.containerd.snapshotter.v1.stargz/snapshots"
  if [[ -d "$snap_dir" ]]; then
    local cnt
    cnt=$(find "$snap_dir" -mindepth 1 -maxdepth 1 2>/dev/null | wc -l | tr -d ' ')
    log "[i] stargz snapshots entries (${snap_dir}): ${cnt}"
  fi
  local local_dir
  local_dir="$(stargz_snapshots_dir)"
  if [[ -d "$local_dir" ]]; then
    local cnt2
    cnt2=$(find "$local_dir" -mindepth 1 -maxdepth 1 2>/dev/null | wc -l | tr -d ' ')
    log "[i] stargz local entries (${local_dir}): ${cnt2}"
  fi
}

snapshot_count() {
  local snap="$1"
  if ctr-remote -n "$NS" snapshots --snapshotter "$snap" ls >/dev/null 2>&1; then
    ctr-remote -n "$NS" snapshots --snapshotter "$snap" ls | awk 'NR>1{print $1}' | wc -l | tr -d ' '
  else
    echo 0
  fi
}

ready_state_ok() {
  local body="$1"
  local state
  state=$(jq -r '.model_version_status[0].state // .status.state // .state // empty' <<<"$body" 2>/dev/null | tr '[:lower:]' '[:upper:]')
  case "$state" in
    AVAILABLE|READY|SERVING|SERVING_STATE_SERVING) return 0;;
  esac
  if grep -q '"AVAILABLE"' <<<"$body" 2>/dev/null; then
    return 0
  fi
  return 1
}

wait_ready() {
  local url="$1" ready_exec="$2" ready_shell="$3" ready_log_regex="$4" timeout_s="$5" cname="$6" ready_port="$7"
  local deadline=$(( $(date +%s) + timeout_s ))
  local warned_parse=0
  local polls=0
  READY_FAILURE_ERR=""

  startup_failure_reason_from_logs() {
    local log_chunk="$1"
    [[ -n "$log_chunk" ]] || return 1
    if grep -qiE 'CUDA error: out of memory|CUDA out of memory|torch\.AcceleratorError:.*out of memory|CUBLAS_STATUS_ALLOC_FAILED|CUDNN_STATUS_ALLOC_FAILED' <<<"$log_chunk"; then
      echo "startup-gpu-oom"
      return 0
    fi
    if grep -qiE 'ProxyError|MaxRetryError|ConnectTimeoutError|Connection to huggingface\.co timed out|HTTPSConnectionPool|HTTPConnectionPool' <<<"$log_chunk"; then
      echo "startup-model-fetch-failed"
      return 0
    fi
    if grep -qiE 'Engine process failed to start|EngineCore.*failed|RuntimeError: Engine .* failed|RuntimeError:.*engine.*failed' <<<"$log_chunk"; then
      echo "startup-runtime-error"
      return 0
    fi
    return 1
  }

  detect_startup_failure() {
    local container_name="$1" log_chunk="${2:-}" reason=""
    if [[ -z "$log_chunk" && -n "$container_name" ]]; then
      log_chunk="$(nerdctl --namespace "$NS" logs --tail 200 "$container_name" 2>&1 || true)"
    fi
    if reason="$(startup_failure_reason_from_logs "$log_chunk")"; then
      READY_FAILURE_ERR="$reason"
      return 0
    fi
    return 1
  }

  if [[ -n "$ready_exec" ]]; then
    while (( $(date +%s) < deadline )); do
      if nerdctl --namespace "$NS" exec "$cname" "$ready_shell" -lc "$ready_exec" >/dev/null 2>&1; then
        return 0
      fi
      polls=$((polls + 1))
      if (( polls % 5 == 0 )) && detect_startup_failure "$cname"; then
        return 1
      fi
      sleep 0.2
    done
    return 1
  fi
  if [[ -n "$ready_log_regex" ]]; then
    while (( $(date +%s) < deadline )); do
      local log_chunk=""
      log_chunk="$(nerdctl --namespace "$NS" logs --tail 200 "$cname" 2>&1 || true)"
      if [[ -n "$log_chunk" ]] && grep -qE -- "$ready_log_regex" <<<"$log_chunk"; then
        return 0
      fi
      if detect_startup_failure "$cname" "$log_chunk"; then
        return 1
      fi
      sleep 0.2
    done
    return 1
  fi
  if [[ "$url" == tcp://* ]]; then
    while (( $(date +%s) < deadline )); do
      if [[ -n "$ready_port" ]] && ( : >"/dev/tcp/127.0.0.1/${ready_port}" ) >/dev/null 2>&1; then
        return 0
      fi
      polls=$((polls + 1))
      if (( polls % 5 == 0 )) && detect_startup_failure "$cname"; then
        return 1
      fi
      sleep 0.2
    done
    return 1
  fi
  while (( $(date +%s) < deadline )); do
    local body
    if body=$(curl -fsS --max-time 1 --noproxy 127.0.0.1,localhost,::1 "$url" 2>/dev/null); then
      if [[ "$READY_HTTP_STATUS_OK_IS_READY" -eq 1 ]]; then
        return 0
      fi
      if ready_state_ok "$body" || grep -qiE 'AVAILABLE|SERVING|READY' <<<"$body" 2>/dev/null; then
        return 0
      fi
      if (( warned_parse == 0 )); then
        echo "[WARN] 就绪响应缺少可识别的 state 字段，继续轮询 ..." >&2
        warned_parse=1
      fi
    fi
    if [[ -n "$cname" && -n "$ready_port" ]]; then
      if nerdctl --namespace "$NS" logs --tail 60 "$cname" 2>/dev/null | grep -q "Exporting HTTP/REST API at:localhost:${ready_port}"; then
        return 0
      fi
    fi
    polls=$((polls + 1))
    if (( polls % 5 == 0 )) && detect_startup_failure "$cname"; then
      return 1
    fi
    sleep 0.2
  done
  return 1
}

fetch_debug_state_metrics() {
  local url="$1"
  local body="" parsed=""
  if [[ -z "$url" ]]; then
    printf '%s\t%s\t%s\t%s\n' "-1" "-1" "-1" "-1"
    return 0
  fi
  if ! body=$(curl -fsS --max-time 2 --noproxy 127.0.0.1,localhost,::1 "$url" 2>/dev/null); then
    warn "debug/state fetch failed: ${url}"
    printf '%s\t%s\t%s\t%s\n' "-1" "-1" "-1" "-1"
    return 0
  fi
  if ! parsed=$(jq -r '
      [
        (.bootstrap_duration_ms // -1),
        (.hot_files_loaded // -1),
        (.hot_bytes_loaded // -1),
        (if (.loaded_paths | type) == "array" then (.loaded_paths | length) else -1 end)
      ] | @tsv
    ' <<<"$body" 2>/dev/null); then
    warn "debug/state parse failed: ${url}"
    printf '%s\t%s\t%s\t%s\n' "-1" "-1" "-1" "-1"
    return 0
  fi
  printf '%s\n' "$parsed"
}

lazy_prepare_command() {
  local ref="$1" snap="$2"
  local args=( ctr-remote -n "$NS" images rpull --snapshotter "$snap" )
  if looks_like_insecure_registry_ref "$ref"; then
    args+=( --plain-http )
  fi
  args+=( "$ref" )
  printf '%s\n' "$(command_to_string "${args[@]}")"
}

run_log_has_full_pull_evidence() {
  local logfile="${1:-}"
  transfer_log_looks_like_full_pull "$logfile" || log_mentions_large_layer_done "$logfile"
}

lazy_guard_should_fail() {
  local prepare_log="$1" run_err_log="$2" container_log="$3" run_ms="$4"
  local cache_before="$5" cache_after="$6" content_before="$7" content_after="$8" content_large_after="$9"
  local cache_growth=0
  LAZY_GUARD_REASON=""
  LAZY_FULL_PULL_EVIDENCE=0

  if run_log_has_full_pull_evidence "$prepare_log" || run_log_has_full_pull_evidence "$run_err_log" || run_log_has_full_pull_evidence "$container_log"; then
    LAZY_GUARD_REASON="transfer-log-indicates-full-pull"
    LAZY_FULL_PULL_EVIDENCE=1
    return 0
  fi
  if [[ "$content_large_after" == "1" ]]; then
    LAZY_GUARD_REASON="content-store-has-large-layer-blob"
    LAZY_FULL_PULL_EVIDENCE=1
    return 0
  fi
  if [[ "$content_after" =~ ^[0-9]+$ ]] && (( content_after > 0 )); then
    LAZY_GUARD_REASON="content-store-has-layer-bytes"
    LAZY_FULL_PULL_EVIDENCE=1
    return 0
  fi
  if [[ "$cache_before" =~ ^[0-9]+$ && "$cache_after" =~ ^[0-9]+$ ]]; then
    cache_growth=$(( cache_after - cache_before ))
    if (( cache_growth < 0 )); then
      cache_growth=0
    fi
    if (( LAZY_TOTAL_LAYER_SIZE_BYTES > 0 && cache_growth * 100 >= LAZY_TOTAL_LAYER_SIZE_BYTES * 80 )); then
      LAZY_GUARD_REASON="stargz-cache-growth-near-full-image"
      LAZY_FULL_PULL_EVIDENCE=1
      return 0
    fi
  fi
  if [[ "$run_ms" =~ ^[0-9]+$ ]] && (( run_ms > 30000 )) && (( LAZY_TOTAL_LAYER_SIZE_BYTES >= LAZY_LARGE_IMAGE_THRESHOLD_BYTES )); then
    LAZY_GUARD_REASON="run-ms-too-large-for-lazy-large-image"
    LAZY_FULL_PULL_EVIDENCE=1
    return 0
  fi
  return 1
}

run_optimized_estargz_lazy() {
  local label="$1" image="$2" snap="$3" run_image="$4" proof_exists="$5" proof_done="$6" requested_ready_port="$7"
  local pull_ms=0 run_ms=-1 ready_wait_ms=-1 end_to_end_ready_ms=-1 ready_ms=-1 predict_extra_ms=-1
  local service_bootstrap_ms=-1 hot_files_loaded=-1 hot_bytes_loaded=-1 loaded_paths_count=-1
  local lazy_prepare_ms=-1 lazy_ready_ms=-1 ready_ms_semantics="lazy_ready_ms"
  local stargz_cache_bytes=-1 stargz_cache_bytes_before=-1 content_large_blob_present_before=-1
  local content_large_blob_present_after=-1 content_store_present_bytes_before=-1 content_store_present_bytes_after=-1
  local explicit_pull_executed=0 full_pull_evidence=0 lazy_guard_failed=0 lazy_full_pull_warning=0
  local err="" status="ok"
  local run_idx name runerr logf preparelog tracef run_cmdf prepare_cmdf diag_start_ms error_prefix

  run_idx="$(measure_run_index)"
  name="$(measure_artifact_name "$label" "$run_idx")"
  runerr="${LOG_DIR}/${name}.runerr"
  logf="${LOG_DIR}/${name}.log"
  preparelog="${LOG_DIR}/${name}.lazy.prepare.log"
  tracef="${LOG_DIR}/${name}.lazy.trace.log"
  run_cmdf="${LOG_DIR}/${name}.run.cmd"
  prepare_cmdf="${LOG_DIR}/${name}.lazy.prepare.cmd"
  diag_start_ms="$(ms_now)"
  error_prefix="$(lazy_error_prefix "$label")"
  : >"$tracef"
  stop_rm_container "$name"

  {
    printf 'label=%s\n' "$label"
    printf 'run_index=%s\n' "$run_idx"
    printf 'comparison_mode=%s\n' "$COMPARISON_MODE"
    printf 'explicit_pull_executed=0\n'
    printf 'image=%s\n' "$image"
    printf 'resolved_image=%s\n' "$run_image"
    printf 'snapshotter=%s\n' "$snap"
  } >>"$tracef"

  if ! resolve_lazy_reference_metadata "$run_image"; then
    err="${error_prefix}-prepare-failed"; status="fail"
    printf 'metadata_resolve=failed\n' >>"$tracef"
    emit_measure_row "$label" "$snap" "$pull_ms" "$run_ms" "$ready_wait_ms" "$end_to_end_ready_ms" "$ready_ms" \
      "$predict_extra_ms" "$service_bootstrap_ms" "$hot_files_loaded" "$hot_bytes_loaded" "$loaded_paths_count" \
      "$proof_exists" "$proof_done" "$lazy_prepare_ms" "$lazy_ready_ms" "$ready_ms_semantics" \
      "$stargz_cache_bytes" "$stargz_cache_bytes_before" "$content_large_blob_present_before" "$content_large_blob_present_after" \
      "$content_store_present_bytes_before" "$content_store_present_bytes_after" "$explicit_pull_executed" "$full_pull_evidence" "$lazy_guard_failed" "$lazy_full_pull_warning" "$status" "$err"
    return
  fi

  run_image="$LAZY_RESOLVED_REF"
  CURRENT_LAZY_LABEL="$label"
  if ! prepare_stargz_for_lazy_run "$run_idx" "$tracef"; then
    err="${error_prefix}-reset-failed"; status="fail"
    capture_lazy_failure_diagnostics "$name" "$tracef" "$diag_start_ms" "$(ms_now)"
    emit_measure_row "$label" "$snap" "$pull_ms" "$run_ms" "$ready_wait_ms" "$end_to_end_ready_ms" "$ready_ms" \
      "$predict_extra_ms" "$service_bootstrap_ms" "$hot_files_loaded" "$hot_bytes_loaded" "$loaded_paths_count" \
      "$proof_exists" "$proof_done" "$lazy_prepare_ms" "$lazy_ready_ms" "$ready_ms_semantics" \
      "$stargz_cache_bytes" "$stargz_cache_bytes_before" "$content_large_blob_present_before" "$content_large_blob_present_after" \
      "$content_store_present_bytes_before" "$content_store_present_bytes_after" "$explicit_pull_executed" "$full_pull_evidence" "$lazy_guard_failed" "$lazy_full_pull_warning" "$status" "$err"
    return
  fi
  if ! strict_mode_enabled; then
    purge_content_store_digests "${LAZY_CLOSURE_DIGESTS[@]}"
  fi
  stargz_cache_bytes_before="$(stargz_cache_size_bytes)"
  content_large_blob_present_before="$(content_store_has_any_digest "${LAZY_LARGE_LAYER_DIGESTS[@]}")"
  content_store_present_bytes_before="$(content_store_present_layer_bytes)"
  {
    printf 'metadata_resolve=ok\n'
    printf 'resolved_image=%s\n' "$run_image"
    printf 'image_total_layer_bytes=%s\n' "$LAZY_TOTAL_LAYER_SIZE_BYTES"
    printf 'stargz_cache_bytes_before=%s\n' "$stargz_cache_bytes_before"
    printf 'content_large_blob_present_before=%s\n' "$content_large_blob_present_before"
    printf 'content_store_present_bytes_before=%s\n' "$content_store_present_bytes_before"
  } >>"$tracef"

  local -a prepare_cmd=( ctr-remote -n "$NS" images rpull --snapshotter "$snap" )
  if looks_like_insecure_registry_ref "$run_image"; then
    prepare_cmd+=( --plain-http )
  fi
  prepare_cmd+=( "$run_image" )
  printf '%s\n' "$(command_to_string "${prepare_cmd[@]}")" >"$prepare_cmdf"
  printf 'lazy_prepare_command=%s\n' "$(command_to_string "${prepare_cmd[@]}")" >>"$tracef"
  printf 'lazy_prepare_command_file=%s\n' "$prepare_cmdf" >>"$tracef"

  local prep_t0 prep_t1
  prep_t0=$(ms_now)
  if ! "${prepare_cmd[@]}" >"$preparelog" 2>&1; then
    prep_t1=$(ms_now)
    lazy_prepare_ms=$(( prep_t1 - prep_t0 ))
    stargz_cache_bytes="$(stargz_cache_size_bytes)"
    content_large_blob_present_after="$(content_store_has_any_digest "${LAZY_LARGE_LAYER_DIGESTS[@]}")"
    content_store_present_bytes_after="$(content_store_present_layer_bytes)"
    if lazy_guard_should_fail "$preparelog" "$runerr" "$logf" 0 "$stargz_cache_bytes_before" "$stargz_cache_bytes" "$content_store_present_bytes_before" "$content_store_present_bytes_after" "$content_large_blob_present_after"; then
      err="${error_prefix}-path-performed-full-pull"; lazy_guard_failed=1; lazy_full_pull_warning=1; full_pull_evidence="$LAZY_FULL_PULL_EVIDENCE"
    else
      err="${error_prefix}-prepare-failed"
    fi
    capture_lazy_failure_diagnostics "$name" "$tracef" "$diag_start_ms" "$prep_t1"
    status="fail"
    printf 'lazy_prepare_result=failed\n' >>"$tracef"
    emit_measure_row "$label" "$snap" "$pull_ms" "$run_ms" "$ready_wait_ms" "$end_to_end_ready_ms" "$ready_ms" \
      "$predict_extra_ms" "$service_bootstrap_ms" "$hot_files_loaded" "$hot_bytes_loaded" "$loaded_paths_count" \
      "$proof_exists" "$proof_done" "$lazy_prepare_ms" "$lazy_ready_ms" "$ready_ms_semantics" \
      "$stargz_cache_bytes" "$stargz_cache_bytes_before" "$content_large_blob_present_before" "$content_large_blob_present_after" \
      "$content_store_present_bytes_before" "$content_store_present_bytes_after" "$explicit_pull_executed" "$full_pull_evidence" "$lazy_guard_failed" "$lazy_full_pull_warning" "$status" "$err"
    return
  fi
  prep_t1=$(ms_now)
  lazy_prepare_ms=$(( prep_t1 - prep_t0 ))
  printf 'lazy_prepare_ms=%s\n' "$lazy_prepare_ms" >>"$tracef"

  stargz_cache_bytes="$(stargz_cache_size_bytes)"
  content_large_blob_present_after="$(content_store_has_any_digest "${LAZY_LARGE_LAYER_DIGESTS[@]}")"
  content_store_present_bytes_after="$(content_store_present_layer_bytes)"
  if lazy_guard_should_fail "$preparelog" "$runerr" "$logf" 0 "$stargz_cache_bytes_before" "$stargz_cache_bytes" "$content_store_present_bytes_before" "$content_store_present_bytes_after" "$content_large_blob_present_after"; then
    err="${error_prefix}-path-performed-full-pull"; status="fail"; lazy_guard_failed=1; lazy_full_pull_warning=1; full_pull_evidence="$LAZY_FULL_PULL_EVIDENCE"
    printf 'lazy_guard_failed_after_prepare=%s\n' "$LAZY_GUARD_REASON" >>"$tracef"
    capture_lazy_failure_diagnostics "$name" "$tracef" "$diag_start_ms" "$(ms_now)"
    emit_measure_row "$label" "$snap" "$pull_ms" "$run_ms" "$ready_wait_ms" "$end_to_end_ready_ms" "$ready_ms" \
      "$predict_extra_ms" "$service_bootstrap_ms" "$hot_files_loaded" "$hot_bytes_loaded" "$loaded_paths_count" \
      "$proof_exists" "$proof_done" "$lazy_prepare_ms" "$lazy_ready_ms" "$ready_ms_semantics" \
      "$stargz_cache_bytes" "$stargz_cache_bytes_before" "$content_large_blob_present_before" "$content_large_blob_present_after" \
      "$content_store_present_bytes_before" "$content_store_present_bytes_after" "$explicit_pull_executed" "$full_pull_evidence" "$lazy_guard_failed" "$lazy_full_pull_warning" "$status" "$err"
    return
  fi

  if [[ "$MODE" == "struct" ]]; then
    local -a run_args=( run --rm --net=none --pull=never --snapshotter "$snap" --platform "$PLATFORM" --entrypoint /bin/true )
    if support_cni_flag; then run_args+=( --cni=false ); fi
    run_args+=( "$run_image" )
    printf 'lazy_run_command=%s\n' "nerdctl --namespace $NS $(command_to_string "${run_args[@]}")" >>"$tracef"
    local t0 t1
    t0=$(ms_now)
    if ! nerdctl --namespace "$NS" "${run_args[@]}" >/dev/null 2>"$runerr"; then
      t1=$(ms_now)
      run_ms=$(( t1 - t0 ))
      stargz_cache_bytes="$(stargz_cache_size_bytes)"
      content_large_blob_present_after="$(content_store_has_any_digest "${LAZY_LARGE_LAYER_DIGESTS[@]}")"
      content_store_present_bytes_after="$(content_store_present_layer_bytes)"
      if lazy_guard_should_fail "$preparelog" "$runerr" "$logf" "$run_ms" "$stargz_cache_bytes_before" "$stargz_cache_bytes" "$content_store_present_bytes_before" "$content_store_present_bytes_after" "$content_large_blob_present_after"; then
        err="${error_prefix}-path-performed-full-pull"; lazy_guard_failed=1; lazy_full_pull_warning=1; full_pull_evidence="$LAZY_FULL_PULL_EVIDENCE"
      else
        err="nerdctl-run-failed"
      fi
      status="fail"
      emit_measure_row "$label" "$snap" "$pull_ms" "$run_ms" "$ready_wait_ms" "$end_to_end_ready_ms" "$ready_ms" \
        "$predict_extra_ms" "$service_bootstrap_ms" "$hot_files_loaded" "$hot_bytes_loaded" "$loaded_paths_count" \
        "$proof_exists" "$proof_done" "$lazy_prepare_ms" "$lazy_ready_ms" "$ready_ms_semantics" \
        "$stargz_cache_bytes" "$stargz_cache_bytes_before" "$content_large_blob_present_before" "$content_large_blob_present_after" \
        "$content_store_present_bytes_before" "$content_store_present_bytes_after" "$explicit_pull_executed" "$full_pull_evidence" "$lazy_guard_failed" "$lazy_full_pull_warning" "$status" "$err"
      return
    fi
    t1=$(ms_now)
    run_ms=$(( t1 - t0 ))
    stargz_cache_bytes="$(stargz_cache_size_bytes)"
    content_large_blob_present_after="$(content_store_has_any_digest "${LAZY_LARGE_LAYER_DIGESTS[@]}")"
    content_store_present_bytes_after="$(content_store_present_layer_bytes)"
    if lazy_guard_should_fail "$preparelog" "$runerr" "$logf" "$run_ms" "$stargz_cache_bytes_before" "$stargz_cache_bytes" "$content_store_present_bytes_before" "$content_store_present_bytes_after" "$content_large_blob_present_after"; then
      err="${error_prefix}-path-performed-full-pull"; status="fail"; lazy_guard_failed=1; lazy_full_pull_warning=1; full_pull_evidence="$LAZY_FULL_PULL_EVIDENCE"
    else
      end_to_end_ready_ms=$(( lazy_prepare_ms + run_ms ))
      lazy_ready_ms=$end_to_end_ready_ms
      ready_ms=$lazy_ready_ms
    fi
    emit_measure_row "$label" "$snap" "$pull_ms" "$run_ms" "$ready_wait_ms" "$end_to_end_ready_ms" "$ready_ms" \
      "$predict_extra_ms" "$service_bootstrap_ms" "$hot_files_loaded" "$hot_bytes_loaded" "$loaded_paths_count" \
      "$proof_exists" "$proof_done" "$lazy_prepare_ms" "$lazy_ready_ms" "$ready_ms_semantics" \
      "$stargz_cache_bytes" "$stargz_cache_bytes_before" "$content_large_blob_present_before" "$content_large_blob_present_after" \
      "$content_store_present_bytes_before" "$content_store_present_bytes_after" "$explicit_pull_executed" "$full_pull_evidence" "$lazy_guard_failed" "$lazy_full_pull_warning" "$status" "$err"
    return
  fi

  if ! prepare_service_network; then
    if [[ -n "$NETWORK_PREP_ERROR" ]]; then
      echo "[ERR] ${NETWORK_PREP_ERROR} (container=${name} port=${requested_ready_port:-n/a} snapshotter=${snap} image=${run_image})" >&2
      err="network-prep-failed"
    else
      echo "[ERR] 无法为 ready/predict probe 选择可用 published 端口 (container=${name} port=${requested_ready_port:-n/a} snapshotter=${snap} image=${run_image})" >&2
      err="publish-port-unavailable"
    fi
    status="fail"
    emit_measure_row "$label" "$snap" "$pull_ms" "$run_ms" "$ready_wait_ms" "$end_to_end_ready_ms" "$ready_ms" \
      "$predict_extra_ms" "$service_bootstrap_ms" "$hot_files_loaded" "$hot_bytes_loaded" "$loaded_paths_count" \
      "$proof_exists" "$proof_done" "$lazy_prepare_ms" "$lazy_ready_ms" "$ready_ms_semantics" \
      "$stargz_cache_bytes" "$stargz_cache_bytes_before" "$content_large_blob_present_before" "$content_large_blob_present_after" \
      "$content_store_present_bytes_before" "$content_store_present_bytes_after" "$explicit_pull_executed" "$full_pull_evidence" "$lazy_guard_failed" "$lazy_full_pull_warning" "$status" "$err"
    return
  fi

  local ready_port="$EFFECTIVE_READY_PORT"
  local -a args=( run -d --name "$name" --pull=never )
  if [[ "$RUNTIME_NETWORK_MODE" == "host" ]]; then
    args+=( --network=host )
  elif [[ "$RUNTIME_NETWORK_MODE" == "none" ]]; then
    args+=( --network=none )
  elif [[ "${#PUBLISH_ARGS[@]}" -gt 0 ]]; then
    args+=( "${PUBLISH_ARGS[@]}" )
  fi
  if [[ -n "$CONTAINER_GPUS" ]]; then
    args+=( --gpus "$CONTAINER_GPUS" )
  fi
  if [[ -n "$CONTAINER_IPC" ]]; then
    args+=( --ipc "$CONTAINER_IPC" )
  fi
  args+=( --snapshotter "$snap" )
  args+=( --platform "$PLATFORM" )
  args+=( -e MODEL_NAME="$MODEL_NAME" )
  local item
  for item in "${CONTAINER_ENVS[@]}"; do
    args+=( -e "$item" )
  done
  if [[ -n "$MODEL_HOST_DIR" ]]; then
    args+=( --mount "type=bind,src=${MODEL_HOST_DIR},dst=${MODEL_DST_DIR},readonly" )
  fi
  for item in "${CONTAINER_MOUNTS[@]}"; do
    args+=( --mount "$item" )
  done
  if [[ -n "$CONTAINER_ENTRYPOINT" ]]; then
    args+=( --entrypoint "$CONTAINER_ENTRYPOINT" )
  fi
  args+=( "$run_image" )
  if [[ "${#CONTAINER_CMD[@]}" -gt 0 ]]; then
    args+=( "${CONTAINER_CMD[@]}" )
  fi
  printf '%s\n' "nerdctl --namespace $NS $(command_to_string "${args[@]}")" >"$run_cmdf"
  printf 'lazy_run_command=%s\n' "nerdctl --namespace $NS $(command_to_string "${args[@]}")" >>"$tracef"
  printf 'lazy_run_command_file=%s\n' "$run_cmdf" >>"$tracef"

  local t0 t1
  t0=$(ms_now)
  if ! nerdctl --namespace "$NS" "${args[@]}" >/dev/null 2>"${runerr}"; then
    t1=$(ms_now)
    run_ms=$(( t1 - t0 ))
    nerdctl --namespace "$NS" logs "$name" > "${logf}" 2>&1 || true
    stargz_cache_bytes="$(stargz_cache_size_bytes)"
    content_large_blob_present_after="$(content_store_has_any_digest "${LAZY_LARGE_LAYER_DIGESTS[@]}")"
    content_store_present_bytes_after="$(content_store_present_layer_bytes)"
    if lazy_guard_should_fail "$preparelog" "$runerr" "$logf" "$run_ms" "$stargz_cache_bytes_before" "$stargz_cache_bytes" "$content_store_present_bytes_before" "$content_store_present_bytes_after" "$content_large_blob_present_after"; then
      err="${error_prefix}-path-performed-full-pull"; lazy_guard_failed=1; lazy_full_pull_warning=1; full_pull_evidence="$LAZY_FULL_PULL_EVIDENCE"
    else
      err="$(lazy_run_failure_error_code "$runerr")"
    fi
    capture_lazy_failure_diagnostics "$name" "$tracef" "$diag_start_ms" "$t1"
    status="fail"
    echo "[ERR] nerdctl run 启动失败：container=${name} port=${ready_port:-${requested_ready_port:-n/a}} snapshotter=${snap} image=${run_image}. 见：${runerr}" >&2
    stop_rm_container "$name"
    emit_measure_row "$label" "$snap" "$pull_ms" "$run_ms" "$ready_wait_ms" "$end_to_end_ready_ms" "$ready_ms" \
      "$predict_extra_ms" "$service_bootstrap_ms" "$hot_files_loaded" "$hot_bytes_loaded" "$loaded_paths_count" \
      "$proof_exists" "$proof_done" "$lazy_prepare_ms" "$lazy_ready_ms" "$ready_ms_semantics" \
      "$stargz_cache_bytes" "$stargz_cache_bytes_before" "$content_large_blob_present_before" "$content_large_blob_present_after" \
      "$content_store_present_bytes_before" "$content_store_present_bytes_after" "$explicit_pull_executed" "$full_pull_evidence" "$lazy_guard_failed" "$lazy_full_pull_warning" "$status" "$err"
    return
  fi
  t1=$(ms_now)
  run_ms=$(( t1 - t0 ))

  if wait_ready "$EFFECTIVE_READY_URL" "$READY_EXEC" "$READY_SHELL" "$READY_LOG_REGEX" "$TIMEOUT_READY" "$name" "$ready_port"; then
    local t2
    t2=$(ms_now)
    ready_wait_ms=$(( t2 - t1 ))
    end_to_end_ready_ms=$(( lazy_prepare_ms + run_ms + ready_wait_ms ))
    lazy_ready_ms=$end_to_end_ready_ms
    ready_ms=$lazy_ready_ms
    local debug_metrics=""
    debug_metrics="$(fetch_debug_state_metrics "$EFFECTIVE_DEBUG_STATE_URL")"
    IFS=$'\t' read -r service_bootstrap_ms hot_files_loaded hot_bytes_loaded loaded_paths_count <<< "$debug_metrics"
    if [[ -n "$EFFECTIVE_PREDICT_URL" ]]; then
      local t3 t4
      t3=$(ms_now)
      if curl -fsS --noproxy 127.0.0.1,localhost,::1 -X POST "$EFFECTIVE_PREDICT_URL" -H 'content-type: application/json' -d "${PREDICT_BODY}" >/dev/null 2>&1; then
        t4=$(ms_now)
        predict_extra_ms=$(( t4 - t3 ))
      fi
    fi
  else
    if [[ -n "$READY_FAILURE_ERR" ]]; then
      echo "[WARN] 容器 ${name} 在 ready 前出现硬失败：${READY_FAILURE_ERR} (container=${name} port=${ready_port:-${requested_ready_port:-n/a}} snapshotter=${snap} image=${run_image})" >&2
      err="$READY_FAILURE_ERR"
    else
      echo "[WARN] 容器 ${name} 在 ${TIMEOUT_READY}s 内未观察到 ready (container=${name} port=${ready_port:-${requested_ready_port:-n/a}} snapshotter=${snap} image=${run_image} url=${EFFECTIVE_READY_URL:-<none>} exec=${READY_EXEC:+set} log_regex=${READY_LOG_REGEX:-<none>})" >&2
      err="ready-timeout"
    fi
    nerdctl --namespace "$NS" logs "$name" 2>/dev/null | tail -n 80 >&2 || true
    status="fail"
  fi

  nerdctl --namespace "$NS" logs "$name" > "${logf}" 2>&1 || true
  stargz_cache_bytes="$(stargz_cache_size_bytes)"
  content_large_blob_present_after="$(content_store_has_any_digest "${LAZY_LARGE_LAYER_DIGESTS[@]}")"
  content_store_present_bytes_after="$(content_store_present_layer_bytes)"
  if lazy_guard_should_fail "$preparelog" "$runerr" "$logf" "$run_ms" "$stargz_cache_bytes_before" "$stargz_cache_bytes" "$content_store_present_bytes_before" "$content_store_present_bytes_after" "$content_large_blob_present_after"; then
    err="${error_prefix}-path-performed-full-pull"; status="fail"; lazy_guard_failed=1; lazy_full_pull_warning=1; full_pull_evidence="$LAZY_FULL_PULL_EVIDENCE"
    printf 'lazy_guard_failed=%s\n' "$LAZY_GUARD_REASON" >>"$tracef"
  fi
  if [[ "$status" == "fail" ]]; then
    capture_lazy_failure_diagnostics "$name" "$tracef" "$diag_start_ms" "$(ms_now)"
  fi
  stop_rm_container "$name"
  emit_measure_row "$label" "$snap" "$pull_ms" "$run_ms" "$ready_wait_ms" "$end_to_end_ready_ms" "$ready_ms" \
    "$predict_extra_ms" "$service_bootstrap_ms" "$hot_files_loaded" "$hot_bytes_loaded" "$loaded_paths_count" \
    "$proof_exists" "$proof_done" "$lazy_prepare_ms" "$lazy_ready_ms" "$ready_ms_semantics" \
    "$stargz_cache_bytes" "$stargz_cache_bytes_before" "$content_large_blob_present_before" "$content_large_blob_present_after" \
    "$content_store_present_bytes_before" "$content_store_present_bytes_after" "$explicit_pull_executed" "$full_pull_evidence" "$lazy_guard_failed" "$lazy_full_pull_warning" "$status" "$err"
}

run_estargz_lazy_measure() {
  run_optimized_estargz_lazy "$@"
}

emit_measure_row() {
  local label="$1" snap="$2" pull_ms="$3" run_ms="$4" ready_wait_ms="$5" end_to_end_ready_ms="$6"
  local ready_ms="$7" predict_extra_ms="$8" service_bootstrap_ms="$9" hot_files_loaded="${10}"
  local hot_bytes_loaded="${11}" loaded_paths_count="${12}" proof_exists="${13}" proof_done="${14}" lazy_prepare_ms="${15}"
  local lazy_ready_ms="${16}" ready_ms_semantics="${17}" stargz_cache_bytes="${18}" stargz_cache_bytes_before="${19}"
  local content_large_blob_present_before="${20}" content_large_blob_present_after="${21}" content_store_present_bytes_before="${22}"
  local content_store_present_bytes_after="${23:-1}" explicit_pull_executed="${24:-0}" full_pull_evidence="${25:-0}" lazy_guard_failed="${26:-0}"
  local lazy_full_pull_warning="${27:-0}" status="${28:-fail}" err="${29:-unknown-error}"
  printf "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n" \
    "$(date -Iseconds)" "$label" "$snap" "$MODE" \
    "$pull_ms" "$run_ms" "$ready_wait_ms" "$end_to_end_ready_ms" "$ready_ms" "$predict_extra_ms" \
    "$service_bootstrap_ms" "$hot_files_loaded" "$hot_bytes_loaded" "$loaded_paths_count" \
    "$proof_exists" "$proof_done" "$lazy_prepare_ms" "$lazy_ready_ms" "$ready_ms_semantics" \
    "$stargz_cache_bytes" "$stargz_cache_bytes_before" "$content_large_blob_present_before" "$content_large_blob_present_after" \
    "$content_store_present_bytes_before" "$content_store_present_bytes_after" "$explicit_pull_executed" "$full_pull_evidence" "$lazy_guard_failed" \
    "$lazy_full_pull_warning" "$status" "$err"
}

measure_one() {
  local label="$1" snap="$2" image="$3"
  local run_image="$image"
  local requested_ready_port=""
  local run_idx=""
  requested_ready_port="$(extract_port_from_url "$READY_URL" 2>/dev/null || true)"
  run_idx="$(measure_run_index)"
  if strict_mode_enabled; then
    run_image="$(strict_label_resolved_ref "$label")"
  fi
  if [[ "$PULL_POLICY" == "never" ]] && ! lazy_mode_enabled; then
    ensure_local_image "$run_image"
  fi

  if [[ "$image" == 127.0.0.1:5001/* || "$run_image" == 127.0.0.1:5001/* ]]; then
    ensure_hosts_toml || true
    probe_proxy_path "$image" || true
  fi

  if [[ "$HARD_COLD" -eq 1 ]]; then
    hard_cold_reset "$run_image" "$snap"
  else
    clear_page_cache
    clear_stargz_cache_if_needed "$snap"
  fi

  # ✅ 兜底：清理后再确保目录骨架存在
  if [[ "$snap" == "stargz" ]]; then
    ensure_stargz_dirs
  fi

  verify_cold_proof || true
  if strict_mode_enabled; then
    if strict_cleanup_and_verify "$label" "$image"; then
      :
    else
      return $?
    fi
  elif measure_uses_lazy_run "$label"; then
    drop_local_image_ref_force "$image"
  else
    drop_local_image_ref_if_needed "$image"
  fi

  local proof_exists=0 proof_done=0
  proof_exists=$(snapshot_count "$snap" || echo 0)
  local snap_dir="/var/lib/containerd/io.containerd.snapshotter.v1.stargz/snapshots"
  if [[ "$snap" == "stargz" && -d "$snap_dir" ]]; then
    proof_done=$(find "$snap_dir" -mindepth 1 -maxdepth 1 2>/dev/null | wc -l | tr -d ' ')
  else
    proof_done=0
  fi

  if measure_uses_lazy_run "$label"; then
    run_estargz_lazy_measure "$label" "$image" "$snap" "$run_image" "$proof_exists" "$proof_done" "$requested_ready_port"
    return
  fi

  local pull_ms=-1 run_ms=-1 ready_wait_ms=-1 end_to_end_ready_ms=-1 ready_ms=-1 predict_extra_ms=-1
  local service_bootstrap_ms=-1 hot_files_loaded=-1 hot_bytes_loaded=-1 loaded_paths_count=-1
  local lazy_prepare_ms=-1 lazy_ready_ms=-1
  local ready_ms_semantics="run_ms"
  local stargz_cache_bytes=-1
  local stargz_cache_bytes_before=-1
  local content_large_blob_present_before=-1
  local content_large_blob_present_after=-1
  local content_store_present_bytes_before=-1
  local content_store_present_bytes_after=-1
  local explicit_pull_executed=0
  local full_pull_evidence=0
  local lazy_guard_failed=0
  local lazy_full_pull_warning=0
  local err="" status="ok"
  if [[ "$MODE" == "ready" ]]; then
    ready_ms_semantics="end_to_end_ready_ms"
  fi
  if pull_ms="$(perform_explicit_pull_if_needed "$label" "$run_image" "$snap")"; then
    :
  else
    err="nerdctl-pull-failed"; status="fail"
    emit_measure_row "$label" "$snap" "-1" "-1" "-1" "-1" "-1" "$predict_extra_ms" \
      "$service_bootstrap_ms" "$hot_files_loaded" "$hot_bytes_loaded" "$loaded_paths_count" \
      "$proof_exists" "$proof_done" "$lazy_prepare_ms" "$lazy_ready_ms" "$ready_ms_semantics" \
      "$stargz_cache_bytes" "$stargz_cache_bytes_before" "$content_large_blob_present_before" "$content_large_blob_present_after" \
      "$content_store_present_bytes_before" "$content_store_present_bytes_after" "$explicit_pull_executed" "$full_pull_evidence" "$lazy_guard_failed" \
      "$lazy_full_pull_warning" "$status" "$err"
    return
  fi
  if [[ "$pull_ms" =~ ^[0-9]+$ && "$pull_ms" -gt 0 ]]; then
    explicit_pull_executed=1
  fi
  if [[ "$MODE" == "struct" ]]; then
    # 结构冷启动：仅测解包/挂载路径所需时间
    local run_args=( run --rm --net=none --pull=never --snapshotter "$snap" --platform "$PLATFORM" --entrypoint /bin/true )
    if support_cni_flag; then run_args+=( --cni=false ); fi
    local t0; t0=$(ms_now)
    nerdctl --namespace "$NS" "${run_args[@]}" "$run_image" >/dev/null 2>&1 || true
    local t1; t1=$(ms_now)
    run_ms=$(( t1 - t0 ))
    end_to_end_ready_ms=$(( pull_ms + run_ms ))
    ready_ms=$run_ms
    ready_ms_semantics="run_ms"
  else
    # 服务就绪：--network=host，轮询 ready
    local name
    name="$(measure_artifact_name "$label" "$run_idx")"
    local runerr="${LOG_DIR}/${name}.runerr"
    local logf="${LOG_DIR}/${name}.log"
    stop_rm_container "$name"

    if ! prepare_service_network; then
      if [[ -n "$NETWORK_PREP_ERROR" ]]; then
        echo "[ERR] ${NETWORK_PREP_ERROR} (container=${name} port=${requested_ready_port:-n/a} snapshotter=${snap} image=${run_image})" >&2
        err="network-prep-failed"
      else
        echo "[ERR] 无法为 ready/predict probe 选择可用 published 端口 (container=${name} port=${requested_ready_port:-n/a} snapshotter=${snap} image=${run_image})" >&2
        err="publish-port-unavailable"
      fi
      stop_rm_container "$name"
      status="fail"
      emit_measure_row "$label" "$snap" "$pull_ms" "$run_ms" "$ready_wait_ms" "$end_to_end_ready_ms" "$ready_ms" \
        "$predict_extra_ms" "$service_bootstrap_ms" "$hot_files_loaded" "$hot_bytes_loaded" "$loaded_paths_count" \
        "$proof_exists" "$proof_done" "$lazy_prepare_ms" "$lazy_ready_ms" "$ready_ms_semantics" \
        "$stargz_cache_bytes" "$content_large_blob_present_before" "$content_large_blob_present_after" \
        "$lazy_full_pull_warning" "$status" "$err"
      return
    fi

    local ready_port="$EFFECTIVE_READY_PORT"
    local args=( run -d --name "$name" --pull=never )
    if [[ "$RUNTIME_NETWORK_MODE" == "host" ]]; then
      args+=( --network=host )
    elif [[ "$RUNTIME_NETWORK_MODE" == "none" ]]; then
      args+=( --network=none )
    elif [[ "${#PUBLISH_ARGS[@]}" -gt 0 ]]; then
      args+=( "${PUBLISH_ARGS[@]}" )
    fi
    if [[ -n "$CONTAINER_GPUS" ]]; then
      args+=( --gpus "$CONTAINER_GPUS" )
    fi
    if [[ -n "$CONTAINER_IPC" ]]; then
      args+=( --ipc "$CONTAINER_IPC" )
    fi
    args+=( --snapshotter "$snap" )
    args+=( --platform "$PLATFORM" )
    args+=( -e MODEL_NAME="$MODEL_NAME" )
    for item in "${CONTAINER_ENVS[@]}"; do
      args+=( -e "$item" )
    done
    if [[ -n "$MODEL_HOST_DIR" ]]; then
      args+=( --mount "type=bind,src=${MODEL_HOST_DIR},dst=${MODEL_DST_DIR},readonly" )
    fi
    for item in "${CONTAINER_MOUNTS[@]}"; do
      args+=( --mount "$item" )
    done
    if [[ -n "$CONTAINER_ENTRYPOINT" ]]; then
      args+=( --entrypoint "$CONTAINER_ENTRYPOINT" )
    fi
    args+=( "$run_image" )
    if [[ "${#CONTAINER_CMD[@]}" -gt 0 ]]; then
      args+=( "${CONTAINER_CMD[@]}" )
    fi
    local t0; t0=$(ms_now)
    if ! nerdctl --namespace "$NS" "${args[@]}" >/dev/null 2>"${runerr}"; then
      echo "[ERR] nerdctl run 启动失败：container=${name} port=${ready_port:-${requested_ready_port:-n/a}} snapshotter=${snap} image=${run_image}. 见：${runerr}" >&2
      stop_rm_container "$name"
      err="nerdctl-run-failed"; status="fail"
      emit_measure_row "$label" "$snap" "$pull_ms" "$run_ms" "$ready_wait_ms" "$end_to_end_ready_ms" "$ready_ms" \
        "$predict_extra_ms" "$service_bootstrap_ms" "$hot_files_loaded" "$hot_bytes_loaded" "$loaded_paths_count" \
        "$proof_exists" "$proof_done" "$lazy_prepare_ms" "$lazy_ready_ms" "$ready_ms_semantics" \
        "$stargz_cache_bytes" "$stargz_cache_bytes_before" "$content_large_blob_present_before" "$content_large_blob_present_after" \
        "$content_store_present_bytes_before" "$content_store_present_bytes_after" "$explicit_pull_executed" "$full_pull_evidence" "$lazy_guard_failed" \
        "$lazy_full_pull_warning" "$status" "$err"
      return
    fi
    local t1; t1=$(ms_now)
    run_ms=$(( t1 - t0 ))
    if wait_ready "$EFFECTIVE_READY_URL" "$READY_EXEC" "$READY_SHELL" "$READY_LOG_REGEX" "$TIMEOUT_READY" "$name" "$ready_port"; then
      local t2; t2=$(ms_now)
      ready_wait_ms=$(( t2 - t1 ))
      if measure_uses_lazy_run "$label"; then
        end_to_end_ready_ms=$(( lazy_prepare_ms + run_ms + ready_wait_ms ))
        lazy_ready_ms=$end_to_end_ready_ms
        ready_ms=$lazy_ready_ms
        ready_ms_semantics="lazy_ready_ms"
      else
        end_to_end_ready_ms=$(( pull_ms + run_ms + ready_wait_ms ))
        ready_ms=$end_to_end_ready_ms
        ready_ms_semantics="end_to_end_ready_ms"
      fi
      local debug_metrics=""
      debug_metrics="$(fetch_debug_state_metrics "$EFFECTIVE_DEBUG_STATE_URL")"
      IFS=$'\t' read -r service_bootstrap_ms hot_files_loaded hot_bytes_loaded loaded_paths_count <<< "$debug_metrics"
      if [[ -n "$EFFECTIVE_PREDICT_URL" ]]; then
        local t3; t3=$(ms_now)
        if curl -fsS --noproxy 127.0.0.1,localhost,::1 -X POST "$EFFECTIVE_PREDICT_URL" -H 'content-type: application/json' -d "${PREDICT_BODY}" >/dev/null 2>&1; then
          local t4; t4=$(ms_now)
          predict_extra_ms=$(( t4 - t3 ))
        fi
      fi
    else
      if [[ -n "$READY_FAILURE_ERR" ]]; then
        echo "[WARN] 容器 ${name} 在 ready 前出现硬失败：${READY_FAILURE_ERR} (container=${name} port=${ready_port:-${requested_ready_port:-n/a}} snapshotter=${snap} image=${run_image})" >&2
        err="$READY_FAILURE_ERR"
      else
        echo "[WARN] 容器 ${name} 在 ${TIMEOUT_READY}s 内未观察到 ready (container=${name} port=${ready_port:-${requested_ready_port:-n/a}} snapshotter=${snap} image=${run_image} url=${EFFECTIVE_READY_URL:-<none>} exec=${READY_EXEC:+set} log_regex=${READY_LOG_REGEX:-<none>})" >&2
        err="ready-timeout"
      fi
      nerdctl --namespace "$NS" logs "$name" 2>/dev/null | tail -n 80 >&2 || true
    fi
    nerdctl --namespace "$NS" logs "$name" > "${logf}" 2>&1 || true
    stop_rm_container "$name"
  fi

  if [[ "$ready_ms" -lt 0 ]]; then status="fail"; fi
  emit_measure_row "$label" "$snap" "$pull_ms" "$run_ms" "$ready_wait_ms" "$end_to_end_ready_ms" "$ready_ms" \
    "$predict_extra_ms" "$service_bootstrap_ms" "$hot_files_loaded" "$hot_bytes_loaded" "$loaded_paths_count" \
    "$proof_exists" "$proof_done" "$lazy_prepare_ms" "$lazy_ready_ms" "$ready_ms_semantics" \
    "$stargz_cache_bytes" "$stargz_cache_bytes_before" "$content_large_blob_present_before" "$content_large_blob_present_after" \
    "$content_store_present_bytes_before" "$content_store_present_bytes_after" "$explicit_pull_executed" "$full_pull_evidence" "$lazy_guard_failed" \
    "$lazy_full_pull_warning" "$status" "$err"
}

append_measure() {
  local output rc result
  if output="$("$@")"; then
    rc=0
  else
    rc=$?
  fi
  result="$(printf '%s\n' "$output" | tail -n 1)"
  if [[ -z "$result" ]]; then
    result="$(emit_measure_row "unknown" "unknown" "-1" "-1" "-1" "-1" "-1" "-1" "-1" "-1" "-1" "-1" "0" "0" "-1" "-1" "unknown" "-1" "-1" "-1" "-1" "-1" "0" "0" "0" "0" "fail" "empty-result")"
  fi
  printf '%s\n' "$result" | tee -a "$CSV_OUT"
  if strict_mode_enabled && [[ "$rc" -ne 0 ]]; then
    return "$rc"
  fi
}

echo "ts,label,snapshotter,mode,pull_ms,run_ms,ready_wait_ms,end_to_end_ready_ms,ready_ms,predict_extra_ms,service_bootstrap_ms,hot_files_loaded,hot_bytes_loaded,loaded_paths_count,proof_exists,proof_done,lazy_prepare_ms,lazy_ready_ms,ready_ms_semantics,stargz_cache_bytes,stargz_cache_bytes_before,content_large_blob_present_before,content_large_blob_present_after,content_store_present_bytes_before,content_store_present_bytes_after,explicit_pull_executed,full_pull_evidence,lazy_guard_failed,lazy_full_pull_warning,status,err" > "$CSV_OUT"
preflight_local_images_if_needed
for i in $(seq 1 "$RUNS"); do
  CURRENT_RUN_INDEX="$i"
  if triple_lazy_mode_enabled; then
    log "[RUN $i/$RUNS] baseline: $BASE_IMAGE @ $BASE_SNAPSHOTTER"
    append_measure measure_one "baseline" "$BASE_SNAPSHOTTER" "$BASE_IMAGE"
    log "[RUN $i/$RUNS] unoptimized_lazy: $UNOPT_IMAGE @ $OPT_SNAPSHOTTER"
    append_measure measure_one "unoptimized_lazy" "$OPT_SNAPSHOTTER" "$UNOPT_IMAGE"
    log "[RUN $i/$RUNS] optimized: $OPT_IMAGE @ $OPT_SNAPSHOTTER"
    append_measure measure_one "optimized" "$OPT_SNAPSHOTTER" "$OPT_IMAGE"
  elif [[ "$SHUFFLE_ORDER" -eq 1 ]]; then
    if (( RANDOM % 2 )); then
      log "[RUN $i/$RUNS] optimized: $OPT_IMAGE @ $OPT_SNAPSHOTTER"
      append_measure measure_one "optimized" "$OPT_SNAPSHOTTER" "$OPT_IMAGE"
      log "[RUN $i/$RUNS] baseline: $BASE_IMAGE @ $BASE_SNAPSHOTTER"
      append_measure measure_one "baseline" "$BASE_SNAPSHOTTER" "$BASE_IMAGE"
    else
      log "[RUN $i/$RUNS] baseline: $BASE_IMAGE @ $BASE_SNAPSHOTTER"
      append_measure measure_one "baseline" "$BASE_SNAPSHOTTER" "$BASE_IMAGE"
      log "[RUN $i/$RUNS] optimized: $OPT_IMAGE @ $OPT_SNAPSHOTTER"
      append_measure measure_one "optimized" "$OPT_SNAPSHOTTER" "$OPT_IMAGE"
    fi
  else
    log "[RUN $i/$RUNS] baseline: $BASE_IMAGE @ $BASE_SNAPSHOTTER"
    append_measure measure_one "baseline" "$BASE_SNAPSHOTTER" "$BASE_IMAGE"
    log "[RUN $i/$RUNS] optimized: $OPT_IMAGE @ $OPT_SNAPSHOTTER"
    append_measure measure_one "optimized" "$OPT_SNAPSHOTTER" "$OPT_IMAGE"
  fi
done

log "[OK] 结果已写入 $CSV_OUT"
log "[OK] 日志目录：$LOG_DIR"
