#!/usr/bin/env bash
set -Eeuo pipefail

# ==== tiny logger ====
log()  { printf "\033[1;34m[+] %s\033[0m\n" "$*" >&2; }
ok()   { printf "\033[1;32m[OK] %s\033[0m\n" "$*" >&2; }
warn() { printf "\033[1;33m[WARN] %s\033[0m\n" "$*" >&2; }
err()  { printf "\033[1;31m[ERR] %s\033[0m\n" "$*" >&2; }

# ==== locate repo root ====
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
ROOT_DIR="$( cd "${SCRIPT_DIR}/../.." && pwd )"

BPF_SRC="${ROOT_DIR}/sampler/BPF/file_access_ebpf_trace.bpf.c"
BPF_OBJ="${ROOT_DIR}/sampler/BPF/file_access_ebpf_trace.bpf.o"
SKEL_H="${ROOT_DIR}/sampler/user/file_access_ebpf_trace.skel.h"

# ---- choose bpftool ----
VENDOR_BPFDIR="${ROOT_DIR}/sampler/tools/bpftool"
VENDOR_BIN="${VENDOR_BPFDIR}/src/bpftool"
BPFT_OVERRIDE="${BPFT_OVERRIDE:-}"         # allow manual override via env

bpftool_works() {
  local candidate="$1"
  [[ -x "${candidate}" ]] && "${candidate}" version >/dev/null 2>&1
}

pick_bpftool() {
  if [[ -n "${BPFT_OVERRIDE}" ]]; then
    if bpftool_works "${BPFT_OVERRIDE}"; then
      echo "${BPFT_OVERRIDE}"
      return 0
    fi
    warn "BPFT_OVERRIDE is set but not runnable: ${BPFT_OVERRIDE}"
  fi
  # Prefer vendored and build it if missing
  if ! bpftool_works "${VENDOR_BIN}"; then
    log "Vendored bpftool not built yet, building now..."
    if ! bash "${VENDOR_BPFDIR}/build_local.sh"; then
      warn "Vendored bpftool build failed."
    fi
  fi
  if bpftool_works "${VENDOR_BIN}"; then
    echo "${VENDOR_BIN}"
    return 0
  fi
  # fallback to system
  if command -v bpftool >/dev/null 2>&1; then
    local sys_bpftool
    sys_bpftool="$(command -v bpftool)"
    if bpftool_works "${sys_bpftool}"; then
      warn "use system bpftool at: ${sys_bpftool}"
      echo "${sys_bpftool}"
      return 0
    fi
    warn "system bpftool is present but not runnable: ${sys_bpftool}"
  fi
  err "bpftool not found and vendored build failed."
  exit 1
}

BPFT="$(pick_bpftool)"

# 检查 clang
if ! command -v clang >/dev/null 2>&1; then
  err "clang not found. Please install clang/llvm."
  exit 1
fi

# 选择 __TARGET_ARCH_*
UNAME_M="$(uname -m)"
case "${UNAME_M}" in
  x86_64)   TARGET_DEF="__TARGET_ARCH_x86" ;;
  aarch64|arm64) TARGET_DEF="__TARGET_ARCH_arm64" ;;
  *)        warn "unknown arch ${UNAME_M}, default to x86"; TARGET_DEF="__TARGET_ARCH_x86" ;;
esac

# libbpf 头文件（优先用 vendored）
LIBBPF_INC_VENDORED="${ROOT_DIR}/sampler/tools/bpftool/libbpf/src"
UAPI_INC_VENDORED="${ROOT_DIR}/sampler/tools/bpftool/libbpf/include/uapi"
ABI_INC="${ROOT_DIR}/sampler/common"

INCLUDES=(-I"${ROOT_DIR}/sampler/BPF" -I"${ABI_INC}")
if [[ -d "${LIBBPF_INC_VENDORED}" ]]; then
  INCLUDES+=(-I"${LIBBPF_INC_VENDORED}")
fi
if [[ -d "${UAPI_INC_VENDORED}" ]]; then
  INCLUDES+=(-I"${UAPI_INC_VENDORED}")
fi

# ==== 1) build .bpf.o ====
log "Building BPF object (arch=${UNAME_M} -> ${TARGET_DEF})"
clang -g -O2 -target bpf -D"${TARGET_DEF}" \
      -Wall -Wno-unused-value -Wno-pointer-sign -Wno-compare-distinct-pointer-types \
      "${INCLUDES[@]}" \
      -c "${BPF_SRC}" -o "${BPF_OBJ}"
ok "BPF object: ${BPF_OBJ}"

# ==== 2) gen skeleton (兼容新旧 bpftool) ====
log "Generating skeleton (using ${BPFT})"
SKEL_TMP="$(mktemp "${SKEL_H}.tmp.XXXXXX")"
rm -f "${SKEL_H}"
cleanup_skel_tmp() {
  rm -f "${SKEL_TMP}"
}
trap cleanup_skel_tmp EXIT

if "${BPFT}" gen skeleton "${BPF_OBJ}" --output "${SKEL_TMP}" 2>/dev/null; then
  mv "${SKEL_TMP}" "${SKEL_H}"
  ok "skeleton: ${SKEL_H}"
else
  if "${BPFT}" gen skeleton "${BPF_OBJ}" > "${SKEL_TMP}"; then
    mv "${SKEL_TMP}" "${SKEL_H}"
    ok "skeleton: ${SKEL_H} (fallback via stdout redirect)"
  else
    rm -f "${SKEL_TMP}"
    # 打印一下依赖提示
    warn "bpftool may miss runtime deps (e.g., libsframe). Try vendored static build:"
    echo "  bash ${VENDOR_BPFDIR}/build_local.sh"
    err "bpftool gen skeleton failed."
    exit 1
  fi
fi

# ==== 3) build user-space via CMake ====
BUILD_DIR="${ROOT_DIR}/sampler/user/build"
if [[ -f "${BUILD_DIR}/CMakeCache.txt" ]] && ! grep -Fq "${ROOT_DIR}/sampler/user" "${BUILD_DIR}/CMakeCache.txt"; then
  warn "Existing CMake cache points to a different source tree; clearing ${BUILD_DIR}"
  rm -rf "${BUILD_DIR}"
fi
log "Configuring CMake in ${BUILD_DIR}"
cmake -S "${ROOT_DIR}/sampler/user" -B "${BUILD_DIR}" -DENABLE_SYMRESOLVE=OFF
log "Building user space"
cmake --build "${BUILD_DIR}" --clean-first -j
ok "Built: ${BUILD_DIR}/sampler_trace_ext"

DIST_BIN_DIR="${ROOT_DIR}/dist/bin"
mkdir -p "${DIST_BIN_DIR}"
install -m 0755 "${BUILD_DIR}/sampler_trace_ext" "${DIST_BIN_DIR}/sampler_trace_ext"
ok "Synced formal delivery binary: ${DIST_BIN_DIR}/sampler_trace_ext"

echo
ok "All done."
echo "Run sampler:"
echo "  sudo ${BUILD_DIR}/sampler_trace_ext --pid \$PID --prom-port 8000 --sampling-rate 100 --mmap-window 5 --redis-url redis://127.0.0.1:6379/0"
