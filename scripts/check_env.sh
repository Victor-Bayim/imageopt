#!/usr/bin/env bash
# check_env.sh
# AUXILIARY HELPER
# SECONDARY ENTRYPOINT ONLY
# NOT THE PRIMARY PRODUCT ENTRYPOINT
# Prefer `scripts/oneclick_product.py preflight|run|benchmark` for the formal product flow.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

PROFILE="all"
SNAPSHOTTER="stargz"
REDIS_URL="redis://127.0.0.1:6379/0"
IMAGE=""
JSON_ONLY=0
JSON_OUT=""
RUNTIME_PROBES=1

usage() {
  cat <<'EOF'
用法：
  scripts/check_env.sh [--profile oneclick|apply|benchmark|build|all] [--snapshotter NAME]
                       [--redis-url URL] [--image IMAGE] [--json] [--json-out PATH]
                       [--no-runtime-probes]

说明：
  - 这是正式 Python orchestrator 的环境预检 helper，也可单独运行做辅助诊断。
  - 默认检查 all profile，并输出人类可读摘要。
  - `--json` 仅输出 JSON；`--json-out` 可将 JSON 同步写入文件。
  - `--no-runtime-probes` 会跳过 containerd / Redis / image shell 等主动探测，
    这适合做离线结构校验，但结果只代表“静态前提基本满足”。

profile 含义：
  - oneclick: 正式 `preflight/sample/export-enrich/analyze` 主链前提检查（profile 名沿用历史命名）
  - apply:    hints / estargz.list 应用到镜像
  - benchmark: 冷启动 benchmark
  - build:    BPF 对象、skeleton、用户态采样器构建
EOF
}

die() {
  echo "[ERR] $*" >&2
  exit 2
}

sanitize() {
  printf '%s' "${1:-}" | tr '\t\r\n' '   '
}

CHECKS_TSV="$(mktemp)"
JSON_TMP="$(mktemp)"
cleanup() {
  rm -f "${CHECKS_TSV}" "${JSON_TMP}"
}
trap cleanup EXIT

record_check() {
  local id="$1"
  local status="$2"
  local required_for="${3:-}"
  local advisory_for="${4:-}"
  local summary="$5"
  local detail="${6:-}"
  local value="${7:-}"

  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$(sanitize "${id}")" \
    "$(sanitize "${status}")" \
    "$(sanitize "${required_for}")" \
    "$(sanitize "${advisory_for}")" \
    "$(sanitize "${summary}")" \
    "$(sanitize "${detail}")" \
    "$(sanitize "${value}")" \
    >> "${CHECKS_TSV}"
}

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

cmd_path() {
  command -v "$1" 2>/dev/null || true
}

pick_formal_sampler_bin() {
  local override="${CPP_SAMPLER_BIN:-}"
  local candidate

  if [[ -n "${override}" ]]; then
    if [[ -x "${override}" ]]; then
      printf '%s\n' "${override}"
      return 0
    fi
    return 1
  fi

  for candidate in \
    "${ROOT_DIR}/dist/bin/sampler_trace_ext" \
    "${ROOT_DIR}/sampler/user/build/sampler_trace_ext"; do
    if [[ -x "${candidate}" ]]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done
  return 1
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

run_priv() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
  elif has_cmd sudo && sudo -n true >/dev/null 2>&1; then
    sudo -n "$@"
  else
    "$@"
  fi
}

check_cmd() {
  local cmd="$1"
  local missing_status="$2"
  local required_for="${3:-}"
  local advisory_for="${4:-}"
  local summary="$5"

  if has_cmd "${cmd}"; then
    record_check "cmd_${cmd}" "pass" "${required_for}" "${advisory_for}" \
      "${summary}" "found at $(cmd_path "${cmd}")" "$(cmd_path "${cmd}")"
  else
    record_check "cmd_${cmd}" "${missing_status}" "${required_for}" "${advisory_for}" \
      "${summary}" "not found in PATH" ""
  fi
}

check_host_gated_startup_launcher() {
  local compiler=""
  local source_path="${ROOT_DIR}/orchestrator/assets/startup_gate_launcher.c"
  local preferred="${CPP_SAMPLER_CC:-}"
  compiler="$(find_host_launcher_compiler || true)"

  if [[ -z "${compiler}" ]]; then
    local missing_detail="missing cc/gcc/clang; static host launcher build is unavailable"
    local missing_dynamic_detail="missing cc/gcc/clang; dynamic host launcher build is unavailable"
    local aggregate_detail="missing cc/gcc/clang; distroless gated startup cannot use host launcher backend"
    if [[ -n "${preferred}" ]]; then
      missing_detail="requested compiler via CPP_SAMPLER_CC is unavailable: ${preferred}; static host launcher build is unavailable"
      missing_dynamic_detail="requested compiler via CPP_SAMPLER_CC is unavailable: ${preferred}; dynamic host launcher build is unavailable"
      aggregate_detail="requested compiler via CPP_SAMPLER_CC is unavailable: ${preferred}; distroless gated startup cannot use host launcher backend"
    fi
    record_check "host_gated_startup_launcher_static" "warn" "" "oneclick" \
      "宿主侧 gated startup launcher 静态构建能力" "${missing_detail}" "${preferred}"
    record_check "host_gated_startup_launcher_dynamic" "warn" "" "oneclick" \
      "宿主侧 gated startup launcher 动态构建能力" "${missing_dynamic_detail}" "${preferred}"
    record_check "host_gated_startup_launcher" "warn" "" "oneclick" \
      "宿主侧 gated startup launcher 构建能力" "${aggregate_detail}" "${preferred}"
    return 0
  fi

  local build_dir
  build_dir="$(mktemp -d)"
  local static_bin="${build_dir}/startup_gate_launcher.static"
  local dynamic_bin="${build_dir}/startup_gate_launcher.dynamic"
  local static_log="${build_dir}/static.log"
  local dynamic_log="${build_dir}/dynamic.log"
  local static_status="warn"
  local dynamic_status="warn"
  local static_detail=""
  local dynamic_detail=""

  if "${compiler}" -O2 -static -s -o "${static_bin}" "${source_path}" >"${static_log}" 2>&1; then
    static_status="pass"
    static_detail="static build succeeded with ${compiler}"
  else
    static_detail="static build failed: $(tr '\n' ' ' < "${static_log}" | sed 's/[[:space:]]\\+/ /g' | sed 's/^ //; s/ $//')"
  fi

  if "${compiler}" -O2 -s -o "${dynamic_bin}" "${source_path}" >"${dynamic_log}" 2>&1; then
    dynamic_status="pass"
    dynamic_detail="dynamic build succeeded with ${compiler}"
  else
    dynamic_detail="dynamic build failed: $(tr '\n' ' ' < "${dynamic_log}" | sed 's/[[:space:]]\\+/ /g' | sed 's/^ //; s/ $//')"
  fi

  record_check "host_gated_startup_launcher_static" "${static_status}" "" "oneclick" \
    "宿主侧 gated startup launcher 静态构建能力" "${static_detail}" "${compiler}"
  record_check "host_gated_startup_launcher_dynamic" "${dynamic_status}" "" "oneclick" \
    "宿主侧 gated startup launcher 动态构建能力" "${dynamic_detail}" "${compiler}"

  local aggregate_status="warn"
  local aggregate_detail="compiler=${compiler}; static=${static_status}; dynamic=${dynamic_status}"
  if [[ "${static_status}" == "pass" || "${dynamic_status}" == "pass" ]]; then
    aggregate_status="pass"
  fi
  record_check "host_gated_startup_launcher" "${aggregate_status}" "" "oneclick" \
    "宿主侧 gated startup launcher 构建能力" "${aggregate_detail}" "${compiler}"

  rm -rf "${build_dir}"
}

check_repo_files() {
  local missing=()
  local p
  for p in \
    "${ROOT_DIR}/scripts/oneclick_esgz_optimize_with_llm.sh" \
    "${ROOT_DIR}/scripts/apply_hints_to_image.sh" \
    "${ROOT_DIR}/scripts/coldstart_bench_strict.sh" \
    "${ROOT_DIR}/orchestrator/assets/startup_gate_launcher.c" \
    "${ROOT_DIR}/optimizer/aggregate_candidates.py" \
    "${ROOT_DIR}/optimizer/tools/export_events.py" \
    "${ROOT_DIR}/optimizer/tools/enrich_mmap_events.py" \
    "${ROOT_DIR}/optimizer/tools/select_prefetch_paths.py" \
    "${ROOT_DIR}/sampler/scripts/build_bpf.sh"; do
    [[ -e "${p}" ]] || missing+=("${p}")
  done

  if [[ "${#missing[@]}" -eq 0 ]]; then
    record_check "repo_layout" "pass" "oneclick,apply,benchmark,build" "" \
      "仓库关键脚本与工具文件存在" "${ROOT_DIR}" "${ROOT_DIR}"
  else
    record_check "repo_layout" "fail" "oneclick,apply,benchmark,build" "" \
      "仓库关键脚本与工具文件存在" "missing: ${missing[*]}" "${ROOT_DIR}"
  fi
}

check_writable_target() {
  local target="$1"
  if [[ -e "${target}" ]]; then
    [[ -w "${target}" ]]
  else
    [[ -w "$(dirname "${target}")" ]]
  fi
}

probe_runtime_or_warn() {
  local check_id="$1"
  local required_for="${2:-}"
  local advisory_for="${3:-}"
  local summary="$4"
  shift 4

  if [[ "${RUNTIME_PROBES}" -ne 1 ]]; then
    record_check "${check_id}" "warn" "${required_for}" "${advisory_for}" \
      "${summary}" "skipped by --no-runtime-probes" ""
    return 0
  fi

  if "$@"; then
    record_check "${check_id}" "pass" "${required_for}" "${advisory_for}" \
      "${summary}" "probe succeeded" ""
  else
    record_check "${check_id}" "fail" "${required_for}" "${advisory_for}" \
      "${summary}" "probe failed" ""
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="${2:?}"; shift 2 ;;
    --snapshotter) SNAPSHOTTER="${2:?}"; shift 2 ;;
    --redis-url) REDIS_URL="${2:?}"; shift 2 ;;
    --image) IMAGE="${2:?}"; shift 2 ;;
    --json) JSON_ONLY=1; shift ;;
    --json-out) JSON_OUT="${2:?}"; shift 2 ;;
    --no-runtime-probes) RUNTIME_PROBES=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown arg: $1" ;;
  esac
done

case "${PROFILE}" in
  all|oneclick|apply|benchmark|build) ;;
  *) die "--profile 仅支持 oneclick|apply|benchmark|build|all" ;;
esac

UNAME_S="$(uname -s 2>/dev/null || echo unknown)"
UNAME_R="$(uname -r 2>/dev/null || echo unknown)"
UNAME_M="$(uname -m 2>/dev/null || echo unknown)"

if [[ "${UNAME_S}" == "Linux" ]]; then
  record_check "os_linux" "pass" "oneclick,apply,benchmark,build" "" \
    "宿主机为 Linux" "${UNAME_S} ${UNAME_R}" "${UNAME_S}"
else
  record_check "os_linux" "fail" "oneclick,apply,benchmark,build" "" \
    "宿主机为 Linux" "${UNAME_S}" "${UNAME_S}"
fi

check_repo_files

if [[ "${EUID}" -eq 0 ]]; then
  record_check "privileged_user" "pass" "oneclick,benchmark" "apply,build" \
    "具备 root 或免密 sudo 能力" "running as root" "root"
elif has_cmd sudo && sudo -n true >/dev/null 2>&1; then
  record_check "privileged_user" "pass" "oneclick,benchmark" "apply,build" \
    "具备 root 或免密 sudo 能力" "passwordless sudo is available" "sudo -n"
else
  record_check "privileged_user" "fail" "oneclick,benchmark" "apply,build" \
    "具备 root 或免密 sudo 能力" "neither root nor passwordless sudo" ""
fi

check_cmd nerdctl fail "oneclick,benchmark" "apply" "nerdctl 已安装"
check_cmd curl fail "oneclick,benchmark" "" "curl 已安装"
check_cmd awk fail "oneclick,benchmark" "" "awk 已安装"
check_cmd sed fail "oneclick" "" "sed 已安装"
check_cmd python3 fail "oneclick,apply" "" "python3 已安装"
check_cmd ss fail "oneclick,benchmark" "" "ss 已安装"
check_cmd ctr-remote warn "" "apply,benchmark" "ctr-remote 已安装"
check_host_gated_startup_launcher
if has_cmd jq; then
  record_check "cmd_jq_optional" "pass" "" "oneclick" \
    "jq 已安装（LLM/JSON 处理增强）" "found at $(cmd_path jq)" "$(cmd_path jq)"
  record_check "cmd_jq_benchmark" "pass" "benchmark" "" \
    "jq 已安装（benchmark 需要）" "found at $(cmd_path jq)" "$(cmd_path jq)"
else
  record_check "cmd_jq_optional" "warn" "" "oneclick" \
    "jq 已安装（LLM/JSON 处理增强）" "not found in PATH" ""
  record_check "cmd_jq_benchmark" "fail" "benchmark" "" \
    "jq 已安装（benchmark 需要）" "not found in PATH" ""
fi
check_cmd redis-cli warn "" "oneclick" "redis-cli 已安装（可选）"
check_cmd ctr warn "" "apply,benchmark" "ctr 已安装（apply fallback / strict benchmark content purge 可用）"
check_cmd estargzify warn "" "apply" "estargzify 已安装（fallback 可用）"
check_cmd stargzify warn "" "apply" "stargzify 已安装（fallback 可用）"
check_cmd clang fail "build" "" "clang 已安装"
check_cmd cmake fail "build" "" "cmake 已安装"
check_cmd make fail "build" "" "make 已安装"
check_cmd pkg-config warn "" "build" "pkg-config 已安装（vendored bpftool 增强）"
check_cmd llvm-ar warn "" "build" "llvm-ar 已安装（vendored bpftool 增强）"
check_cmd bpftool warn "" "build" "bpftool 已安装"

SAMPLER_BIN="$(pick_formal_sampler_bin || true)"
if [[ -n "${SAMPLER_BIN}" ]]; then
  record_check "sampler_binary" "pass" "oneclick" "" \
    "用户态采样器已构建" "${SAMPLER_BIN}" "${SAMPLER_BIN}"
else
  SAMPLER_BIN="${ROOT_DIR}/dist/bin/sampler_trace_ext"
  if [[ -n "${CPP_SAMPLER_BIN:-}" ]]; then
    SAMPLER_BIN="${CPP_SAMPLER_BIN}"
  fi
  record_check "sampler_binary" "fail" "oneclick" "" \
    "用户态采样器已构建" "missing or not executable: ${SAMPLER_BIN}" "${SAMPLER_BIN}"
fi

BUILD_BPF_SCRIPT="${ROOT_DIR}/sampler/scripts/build_bpf.sh"
if [[ -x "${BUILD_BPF_SCRIPT}" || -f "${BUILD_BPF_SCRIPT}" ]]; then
  record_check "build_bpf_script" "pass" "build" "" \
    "BPF 构建脚本存在" "${BUILD_BPF_SCRIPT}" "${BUILD_BPF_SCRIPT}"
else
  record_check "build_bpf_script" "fail" "build" "" \
    "BPF 构建脚本存在" "missing: ${BUILD_BPF_SCRIPT}" "${BUILD_BPF_SCRIPT}"
fi

if [[ -r /sys/kernel/btf/vmlinux ]]; then
  record_check "kernel_btf" "pass" "oneclick" "build" \
    "内核导出 vmlinux BTF" "/sys/kernel/btf/vmlinux is readable" "/sys/kernel/btf/vmlinux"
else
  record_check "kernel_btf" "fail" "oneclick" "build" \
    "内核导出 vmlinux BTF" "missing /sys/kernel/btf/vmlinux" "/sys/kernel/btf/vmlinux"
fi

if [[ -r /sys/kernel/security/lsm ]]; then
  LSM_VALUE="$(tr ',' ' ' < /sys/kernel/security/lsm)"
  if grep -qw bpf <<<"${LSM_VALUE}"; then
    record_check "kernel_bpf_lsm" "pass" "" "oneclick" \
      "BPF LSM 已启用（MMAP 质量更高）" "${LSM_VALUE}" "${LSM_VALUE}"
  else
    record_check "kernel_bpf_lsm" "warn" "" "oneclick" \
      "BPF LSM 已启用（MMAP 质量更高）" "current LSM list: ${LSM_VALUE}" "${LSM_VALUE}"
  fi
else
  record_check "kernel_bpf_lsm" "warn" "" "oneclick" \
    "BPF LSM 已启用（MMAP 质量更高）" "cannot read /sys/kernel/security/lsm" ""
fi

if grep -Eq '[[:space:]]/sys/fs/bpf[[:space:]]bpf[[:space:]]' /proc/mounts 2>/dev/null; then
  record_check "bpffs_mount" "pass" "" "oneclick" \
    "bpffs 已挂载到 /sys/fs/bpf" "/sys/fs/bpf" "/sys/fs/bpf"
else
  record_check "bpffs_mount" "warn" "" "oneclick" \
    "bpffs 已挂载到 /sys/fs/bpf" "not mounted at /sys/fs/bpf" "/sys/fs/bpf"
fi

BUILD_WRITES=(
  "${ROOT_DIR}/sampler/BPF/file_access_ebpf_trace.bpf.o"
  "${ROOT_DIR}/sampler/user/file_access_ebpf_trace.skel.h"
  "${ROOT_DIR}/sampler/user/build"
)
BUILD_BAD=()
for target in "${BUILD_WRITES[@]}"; do
  if ! check_writable_target "${target}"; then
    BUILD_BAD+=("${target}")
  fi
done
if [[ "${#BUILD_BAD[@]}" -eq 0 ]]; then
  record_check "build_outputs_writable" "pass" "build" "" \
    "BPF / 用户态构建输出路径可写" "${BUILD_WRITES[*]}" ""
else
  record_check "build_outputs_writable" "fail" "build" "" \
    "BPF / 用户态构建输出路径可写" "not writable: ${BUILD_BAD[*]}" ""
fi

if has_cmd bpftool; then
  record_check "bpftool_generation_path" "pass" "build" "" \
    "存在可用的 bpftool 生成路径" "system bpftool: $(cmd_path bpftool)" "$(cmd_path bpftool)"
elif [[ -x "${ROOT_DIR}/sampler/tools/bpftool/build_local.sh" && -d "${ROOT_DIR}/sampler/tools/bpftool/src" ]]; then
  record_check "bpftool_generation_path" "pass" "build" "" \
    "存在可用的 bpftool 生成路径" "vendored build_local.sh is available" \
    "${ROOT_DIR}/sampler/tools/bpftool/build_local.sh"
else
  record_check "bpftool_generation_path" "fail" "build" "" \
    "存在可用的 bpftool 生成路径" "missing system bpftool and vendored builder" ""
fi

if has_cmd nerdctl; then
  probe_runtime_or_warn "nerdctl_runtime" "oneclick,benchmark" "apply" \
    "nerdctl 能连接到运行时" run_priv nerdctl version >/dev/null 2>&1
else
  record_check "nerdctl_runtime" "warn" "oneclick,benchmark" "apply" \
    "nerdctl 能连接到运行时" "skipped because nerdctl is missing" ""
fi

if has_cmd ctr-remote; then
  probe_runtime_or_warn "ctr_remote_runtime" "benchmark" "apply" \
    "ctr-remote 能连接到 containerd" run_priv ctr-remote version >/dev/null 2>&1
else
  record_check "ctr_remote_runtime" "warn" "benchmark" "apply" \
    "ctr-remote 能连接到 containerd" "skipped because ctr-remote is missing" ""
fi

CTR_PREFETCH_PATH=""
if has_cmd ctr-remote; then
  for sub in image images; do
    if ctr-remote "${sub}" optimize -h 2>&1 | grep -q -- '--prefetch-list'; then
      CTR_PREFETCH_PATH="${sub}"
      break
    fi
  done
fi

if [[ -n "${CTR_PREFETCH_PATH}" ]]; then
  record_check "ctr_remote_prefetch_list" "pass" "" "apply" \
    "ctr-remote optimize 支持 --prefetch-list" "subcommand=${CTR_PREFETCH_PATH}" "${CTR_PREFETCH_PATH}"
else
  record_check "ctr_remote_prefetch_list" "warn" "" "apply" \
    "ctr-remote optimize 支持 --prefetch-list" "not detected" ""
fi

APPLY_FALLBACK_BIN=""
if has_cmd estargzify; then
  APPLY_FALLBACK_BIN="estargzify"
elif has_cmd stargzify; then
  APPLY_FALLBACK_BIN="stargzify"
fi

APPLY_IMPORT_BIN=""
if has_cmd ctr; then
  APPLY_IMPORT_BIN="ctr"
elif has_cmd nerdctl; then
  APPLY_IMPORT_BIN="nerdctl"
fi

if [[ -n "${CTR_PREFETCH_PATH}" ]]; then
  record_check "apply_toolchain" "pass" "apply" "" \
    "镜像优化工具链可用" "primary path: ctr-remote ${CTR_PREFETCH_PATH} optimize --prefetch-list" ""
elif [[ -n "${APPLY_FALLBACK_BIN}" && -n "${APPLY_IMPORT_BIN}" ]]; then
  record_check "apply_toolchain" "warn" "apply" "" \
    "镜像优化工具链可用" "fallback path: ${APPLY_FALLBACK_BIN} + ${APPLY_IMPORT_BIN}" ""
else
  record_check "apply_toolchain" "fail" "apply" "" \
    "镜像优化工具链可用" "need ctr-remote with --prefetch-list or estargzify/stargzify + ctr/nerdctl" ""
fi

if [[ "${RUNTIME_PROBES}" -ne 1 ]]; then
  record_check "snapshotter_plugin" "warn" "benchmark" "oneclick" \
    "目标 snapshotter 插件状态正常" "skipped by --no-runtime-probes" "${SNAPSHOTTER}"
elif has_cmd ctr-remote; then
  SNAP_STATE="$({ run_priv ctr-remote plugins ls 2>/dev/null || true; } | awk -v s="${SNAPSHOTTER}" '$1=="io.containerd.snapshotter.v1" && $2==s {print $4; exit}')"
  if [[ -z "${SNAP_STATE}" ]]; then
    record_check "snapshotter_plugin" "fail" "benchmark" "oneclick" \
      "目标 snapshotter 插件状态正常" "plugin io.containerd.snapshotter.v1 ${SNAPSHOTTER} not found" "${SNAPSHOTTER}"
  elif [[ "${SNAP_STATE}" == "ok" ]]; then
    record_check "snapshotter_plugin" "pass" "benchmark" "oneclick" \
      "目标 snapshotter 插件状态正常" "plugin ${SNAPSHOTTER} state=ok" "${SNAPSHOTTER}"
  else
    record_check "snapshotter_plugin" "fail" "benchmark" "oneclick" \
      "目标 snapshotter 插件状态正常" "plugin ${SNAPSHOTTER} state=${SNAP_STATE}" "${SNAPSHOTTER}"
  fi
else
  record_check "snapshotter_plugin" "warn" "benchmark" "oneclick" \
    "目标 snapshotter 插件状态正常" "skipped because ctr-remote is missing" "${SNAPSHOTTER}"
fi

CNI_BRIDGE_PATH="${CNI_PATH:-/opt/cni/bin}/bridge"
if [[ "${RUNTIME_PROBES}" -ne 1 ]]; then
  record_check "cni_bridge_plugin" "warn" "" "benchmark" \
    "CNI bridge 插件可用（busy loopback 端口 remap 需要）" "skipped by --no-runtime-probes" "${CNI_BRIDGE_PATH}"
elif [[ -x "${CNI_BRIDGE_PATH}" ]]; then
  record_check "cni_bridge_plugin" "pass" "" "benchmark" \
    "CNI bridge 插件可用（busy loopback 端口 remap 需要）" "${CNI_BRIDGE_PATH}" "${CNI_BRIDGE_PATH}"
else
  record_check "cni_bridge_plugin" "warn" "" "benchmark" \
    "CNI bridge 插件可用（busy loopback 端口 remap 需要）" "missing executable: ${CNI_BRIDGE_PATH}" "${CNI_BRIDGE_PATH}"
fi

if ! has_cmd redis-cli; then
  record_check "redis_ping" "warn" "" "oneclick" \
    "Redis 可连通（可选）" "redis-cli is missing; file sink remains primary path" "${REDIS_URL}"
elif [[ "${RUNTIME_PROBES}" -ne 1 ]]; then
  record_check "redis_ping" "warn" "" "oneclick" \
    "Redis 可连通（可选）" "skipped by --no-runtime-probes" "${REDIS_URL}"
elif redis-cli -u "${REDIS_URL}" PING >/dev/null 2>&1; then
  record_check "redis_ping" "pass" "" "oneclick" \
    "Redis 可连通（可选）" "${REDIS_URL}" "${REDIS_URL}"
else
  record_check "redis_ping" "warn" "" "oneclick" \
    "Redis 可连通（可选）" "cannot reach ${REDIS_URL}; file sink remains primary path" "${REDIS_URL}"
fi

if [[ -z "${IMAGE}" ]]; then
  record_check "image_local" "warn" "" "oneclick,apply,benchmark" \
    "目标镜像已在本地可见" "not checked; use --image IMAGE to verify" ""
  record_check "image_gated_startup_shell" "warn" "" "oneclick" \
    "目标镜像支持 gated startup 所需的 /bin/sh" "not checked; use --image IMAGE to verify" ""
elif [[ "${RUNTIME_PROBES}" -ne 1 ]]; then
  record_check "image_local" "warn" "" "oneclick,apply,benchmark" \
    "目标镜像已在本地可见" "skipped by --no-runtime-probes" "${IMAGE}"
  record_check "image_gated_startup_shell" "warn" "" "oneclick" \
    "目标镜像支持 gated startup 所需的 /bin/sh" "skipped by --no-runtime-probes" "${IMAGE}"
elif ! has_cmd nerdctl; then
  record_check "image_local" "warn" "" "oneclick,apply,benchmark" \
    "目标镜像已在本地可见" "skipped because nerdctl is missing" "${IMAGE}"
  record_check "image_gated_startup_shell" "warn" "" "oneclick" \
    "目标镜像支持 gated startup 所需的 /bin/sh" "skipped because nerdctl is missing" "${IMAGE}"
else
  if run_priv nerdctl image inspect "${IMAGE}" >/dev/null 2>&1; then
    record_check "image_local" "pass" "" "oneclick,apply,benchmark" \
      "目标镜像已在本地可见" "${IMAGE}" "${IMAGE}"
    if run_priv nerdctl run --rm --pull=never --net=none --entrypoint /bin/sh "${IMAGE}" -lc 'test -x /bin/sh' >/dev/null 2>&1; then
      record_check "image_gated_startup_shell" "pass" "" "oneclick" \
        "目标镜像支持 gated startup 所需的 /bin/sh" "${IMAGE}" "${IMAGE}"
    else
      record_check "image_gated_startup_shell" "warn" "" "oneclick" \
        "目标镜像支持 gated startup 所需的 /bin/sh" "cannot verify /bin/sh in image ${IMAGE}; oneclick may fall back to legacy startup" "${IMAGE}"
    fi
  else
    record_check "image_local" "warn" "" "oneclick,apply,benchmark" \
      "目标镜像已在本地可见" "nerdctl image inspect failed for ${IMAGE}" "${IMAGE}"
    record_check "image_gated_startup_shell" "warn" "" "oneclick" \
      "目标镜像支持 gated startup 所需的 /bin/sh" "image not locally inspectable; gated startup cannot be pre-verified" "${IMAGE}"
  fi
fi

python3 - "${CHECKS_TSV}" "${JSON_TMP}" "${ROOT_DIR}" "${PROFILE}" "${SNAPSHOTTER}" "${REDIS_URL}" "${IMAGE}" "${UNAME_S}" "${UNAME_R}" "${UNAME_M}" "${EUID}" "${RUNTIME_PROBES}" <<'PY'
import datetime as dt
import json
import os
import sys

checks_tsv, out_path, root_dir, requested_profile, snapshotter, redis_url, image, uname_s, uname_r, uname_m, euid_s, runtime_probes_s = sys.argv[1:13]

profiles = ["oneclick", "apply", "benchmark", "build"]
requested_profiles = profiles if requested_profile == "all" else [requested_profile]
runtime_probes = bool(int(runtime_probes_s))

profile_state = {
    name: {"status": "pass", "required_failures": [], "warnings": []}
    for name in profiles
}
checks = []

with open(checks_tsv, "r", encoding="utf-8") as f:
    for raw in f:
        raw = raw.rstrip("\n")
        if not raw:
            continue
        parts = raw.split("\t")
        if len(parts) != 7:
            continue
        check_id, status, required_csv, advisory_csv, summary, detail, value = parts
        required_for = [item for item in required_csv.split(",") if item]
        advisory_for = [item for item in advisory_csv.split(",") if item]
        checks.append(
            {
                "id": check_id,
                "status": status,
                "required_for": required_for,
                "advisory_for": advisory_for,
                "summary": summary,
                "detail": detail,
                "value": value or None,
            }
        )

        for profile in required_for:
            if status == "fail":
                profile_state[profile]["required_failures"].append(check_id)
            elif status == "warn":
                profile_state[profile]["warnings"].append(check_id)

        for profile in advisory_for:
            if status in ("warn", "fail"):
                profile_state[profile]["warnings"].append(check_id)

for profile, data in profile_state.items():
    data["required_failures"] = sorted(set(data["required_failures"]))
    data["warnings"] = sorted(set(data["warnings"]))
    if data["required_failures"]:
        data["status"] = "fail"
    elif data["warnings"]:
        data["status"] = "warn"
    else:
        data["status"] = "pass"

requested_failures = [p for p in requested_profiles if profile_state[p]["status"] == "fail"]
requested_warnings = [p for p in requested_profiles if profile_state[p]["status"] == "warn"]
overall_status = "fail" if requested_failures else ("warn" if requested_warnings else "pass")

payload = {
    "schema_version": 1,
    "generated_at_utc": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
    "repo_root": os.path.abspath(root_dir),
    "requested_profiles": requested_profiles,
    "overall_status": overall_status,
    "host": {
        "os": uname_s,
        "kernel_release": uname_r,
        "arch": uname_m,
    },
    "user": {
        "euid": int(euid_s),
        "runtime_probes_enabled": runtime_probes,
    },
    "options": {
        "snapshotter": snapshotter,
        "redis_url": redis_url,
        "image": image or None,
    },
    "profiles": profile_state,
    "checks": checks,
}

with open(out_path, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)
PY

if [[ -n "${JSON_OUT}" ]]; then
  mkdir -p "$(dirname "${JSON_OUT}")"
  cp "${JSON_TMP}" "${JSON_OUT}"
fi

if [[ "${JSON_ONLY}" -eq 1 ]]; then
  cat "${JSON_TMP}"
else
  python3 - "${JSON_TMP}" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as f:
    data = json.load(f)

profiles = data["profiles"]
requested = data["requested_profiles"]
checks = data["checks"]

print("cpp-sampler env check")
print(f"overall={data['overall_status']} requested={','.join(requested)}")
print(f"host={data['host']['os']} {data['host']['kernel_release']} arch={data['host']['arch']}")
print(f"runtime_probes={'on' if data['user']['runtime_probes_enabled'] else 'off'} snapshotter={data['options']['snapshotter']}")
if data["options"].get("image"):
    print(f"image={data['options']['image']}")
print("")
print("Profiles:")
for name in ["oneclick", "apply", "benchmark", "build"]:
    state = profiles[name]
    marker = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}[state["status"]]
    extra = []
    if state["required_failures"]:
      extra.append("required_failures=" + ",".join(state["required_failures"]))
    if state["warnings"]:
      extra.append("warnings=" + ",".join(state["warnings"]))
    suffix = f" ({'; '.join(extra)})" if extra else ""
    print(f"  {marker:4} {name}{suffix}")

interesting = [c for c in checks if c["status"] != "pass"]
if interesting:
    print("")
    print("Non-pass checks:")
    for item in interesting:
        print(f"  {item['status'].upper():4} {item['id']}: {item['summary']}")
        if item["detail"]:
            print(f"        {item['detail']}")
else:
    print("")
    print("All checks passed.")
PY
fi

EXIT_CODE="$(python3 - "${JSON_TMP}" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as f:
    data = json.load(f)

print(1 if data["overall_status"] == "fail" else 0)
PY
)"

exit "${EXIT_CODE}"
