#!/usr/bin/env bash
# apply_hints_to_image.sh
# LEGACY
# READ-ONLY REFERENCE
# NOT THE FORMAL V1 ENTRYPOINT
# May still be used as an internal helper, but not the user-facing primary entrypoint.
# 使用 oneclick 产出的工作目录，将 hints.yaml/estargz.list 合并进镜像（ctr-remote 优先）

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

WORK_DIR=""
SRC_IMAGE=""
DST_IMAGE=""
NAMESPACE="default"
SELECTOR_MODE="file"
DRY_RUN=0

log_step() { printf '[apply] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
die() { printf '[ERR] %s\n' "$*" >&2; exit 1; }
need_cmd() { command -v "$1" >/dev/null 2>&1 || die "缺少命令：$1"; }
run_priv() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    "$@"
  fi
}
image_ref_registry_host() {
  python3 - "$1" <<'PY'
import sys

ref = sys.argv[1].strip()
name = ref.split("@", 1)[0]
slash = name.find("/")
if slash < 0:
    print("")
    raise SystemExit(0)
first = name[:slash]
has_registry = "." in first or ":" in first or first == "localhost"
print(first if has_registry else "")
PY
}
is_local_registry_ref() {
  local host=""
  host="$(image_ref_registry_host "$1")"
  [[ -n "${host}" ]] || return 1
  [[ "${host}" == "localhost" || "${host}" == localhost:* || "${host}" == "127.0.0.1" || "${host}" == 127.0.0.1:* ]]
}
canonicalize_image_ref() {
  python3 - "$1" <<'PY'
import sys

ref = sys.argv[1].strip()
name = ref
suffix = ""
if "@" in ref:
    name, digest = ref.split("@", 1)
    suffix = "@" + digest
else:
    slash = ref.rfind("/")
    colon = ref.rfind(":")
    if colon > slash:
        name = ref[:colon]
        suffix = ref[colon:]

first = name.split("/", 1)[0]
has_registry = "." in first or ":" in first or first == "localhost"
if not has_registry:
    if "/" not in name:
        name = f"library/{name}"
    name = f"docker.io/{name}"

print(name + suffix)
PY
}

json_field_or_default() {
  local path="$1"
  local key="$2"
  local default_value="${3:-}"
  python3 - "$path" "$key" "$default_value" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
key = sys.argv[2]
default_value = sys.argv[3]

if not path.is_file():
    print(default_value)
    raise SystemExit(0)

try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    print(default_value)
    raise SystemExit(0)

value = payload.get(key)
if value is None:
    print(default_value)
else:
    print(str(value))
PY
}

ensure_estargz_list_from_hints() {
  local hints_file="$1"
  local list_path="$2"
  [[ -s "${hints_file}" ]] || die "在 ${WORK_DIR} 未找到可用 hints：$(basename "${hints_file}")"
  need_cmd python3
  local hints_to_list="${REPO_ROOT}/scripts/hints_to_estargz_list.py"
  if [[ ! -f "${hints_to_list}" ]]; then
    hints_to_list="$(command -v hints_to_estargz_list.py || true)"
  fi
  [[ -n "${hints_to_list}" && -f "${hints_to_list}" ]] || die "找不到 hints_to_estargz_list.py"
  python3 "${hints_to_list}" --in "${hints_file}" --out "${list_path}" || die "无法从 ${hints_file} 生成 estargz.list"
}

usage() {
  cat <<EOF
用法：
  $(basename "$0") --work DIR --src IMAGE [--dst IMAGE] [--ns default] [--selector-mode file|chunk-shadow] [--dry-run]

说明：
  DIR        现有工作目录（优先来自 scripts/oneclick_product.py analyze/run，也兼容历史 oneclick work dir）
  IMAGE      原始 eStargz 镜像名
  --dst      输出镜像名（默认：<src>-stargz 或追加 :stargz-optimized 标签）
  --ns       containerd 命名空间（默认 default）
  --selector-mode  选择消费默认 hints/list 还是实验性的 chunk-shadow hints/list
  --dry-run  仅打印将执行的命令，不实际执行

  本脚本保留为 shell data-plane helper / 直接调试路径；
  正式推荐入口仍是: python3 scripts/oneclick_product.py apply --work-dir <DIR>

依赖：ctr-remote 优先；若缺失则尝试 estargzify/stargzify + nerdctl/ctr 组合。
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --work) WORK_DIR="${2:?}"; shift 2;;
    --src) SRC_IMAGE="${2:?}"; shift 2;;
    --dst) DST_IMAGE="${2:?}"; shift 2;;
    --ns)  NAMESPACE="${2:?}"; shift 2;;
    --selector-mode) SELECTOR_MODE="${2:?}"; shift 2;;
    --dry-run) DRY_RUN=1; shift;;
    -h|--help) usage; exit 0;;
    *) echo "[ERR] 未知参数: $1" >&2; usage; exit 1;;
  esac
done

[[ -n "${WORK_DIR}" && -n "${SRC_IMAGE}" ]] || { usage; exit 1; }

WORK_DIR="$(cd "${WORK_DIR}" && pwd)"

case "${SELECTOR_MODE}" in
  file)
    ARTIFACT_SUFFIX=""
    ;;
  chunk-shadow)
    ARTIFACT_SUFFIX="_chunk_shadow"
    ;;
  *)
    die "不支持的 selector mode: ${SELECTOR_MODE}"
    ;;
esac

if [[ -n "${ARTIFACT_SUFFIX}" ]]; then
  ESTARGZ_LIST="${WORK_DIR}/estargz${ARTIFACT_SUFFIX}.list"
  HINTS_FILE="${WORK_DIR}/hints${ARTIFACT_SUFFIX}.yaml"
  RANKED_FILE="${WORK_DIR}/ranked${ARTIFACT_SUFFIX}.yaml"
  SELECTOR_STATS_FILE="${WORK_DIR}/selector_stats${ARTIFACT_SUFFIX}.json"
else
  ESTARGZ_LIST="${WORK_DIR}/estargz.list"
  HINTS_FILE="${WORK_DIR}/hints.yaml"
  RANKED_FILE="${WORK_DIR}/ranked.yaml"
  SELECTOR_STATS_FILE="${WORK_DIR}/selector_stats_hints.json"
fi

need_cmd python3
HINTS_SOURCE="$(json_field_or_default "${WORK_DIR}/llm_result.json" "final_hints_source" "deterministic_ranked")"
SELECTOR_SOURCE="$(json_field_or_default "${SELECTOR_STATS_FILE}" "selector_source" "unknown")"
LIST_REGENERATED=0
STALE_LIST_DETECTED=0
STALE_LIST_REASON="current"

# 若 estargz.list 不存在，或比当前 hints/ranked 旧，则由 hints.yaml 重新生成
if [[ ! -s "${ESTARGZ_LIST}" ]]; then
  LIST_REGENERATED=1
  STALE_LIST_REASON="missing_or_empty"
  ensure_estargz_list_from_hints "${HINTS_FILE}" "${ESTARGZ_LIST}"
else
  if [[ -s "${HINTS_FILE}" && "${HINTS_FILE}" -nt "${ESTARGZ_LIST}" ]]; then
    STALE_LIST_DETECTED=1
    LIST_REGENERATED=1
    STALE_LIST_REASON="hints_newer_than_estargz_list"
  elif [[ -s "${RANKED_FILE}" && "${RANKED_FILE}" -nt "${ESTARGZ_LIST}" ]]; then
    STALE_LIST_DETECTED=1
    LIST_REGENERATED=1
    STALE_LIST_REASON="ranked_newer_than_estargz_list"
  fi
  if [[ ${LIST_REGENERATED} -eq 1 ]]; then
    ensure_estargz_list_from_hints "${HINTS_FILE}" "${ESTARGZ_LIST}"
  fi
fi

if [[ ! -s "${ESTARGZ_LIST}" ]]; then
  die "estargz.list 为空"
fi

if [[ -z "${DST_IMAGE}" ]]; then
  if [[ "${SRC_IMAGE}" == *:* ]]; then
    DST_IMAGE="${SRC_IMAGE}-stargz"
  else
    DST_IMAGE="${SRC_IMAGE}:stargz-optimized"
  fi
  if [[ "${SELECTOR_MODE}" != "file" ]]; then
    DST_IMAGE="${DST_IMAGE}-${SELECTOR_MODE}"
  fi
fi

# 防止源/目标相同导致覆盖或困惑
if [[ "${SRC_IMAGE}" == "${DST_IMAGE}" ]]; then
  die "SRC_IMAGE 与 DST_IMAGE 相同：${SRC_IMAGE}"
fi

SRC_IMAGE_RESOLVED="$(canonicalize_image_ref "${SRC_IMAGE}")"
DST_IMAGE_RESOLVED="$(canonicalize_image_ref "${DST_IMAGE}")"
if [[ "${SRC_IMAGE_RESOLVED}" != "${SRC_IMAGE}" ]]; then
  log_step "规范化源镜像引用: ${SRC_IMAGE} -> ${SRC_IMAGE_RESOLVED}"
fi
if [[ "${DST_IMAGE_RESOLVED}" != "${DST_IMAGE}" ]]; then
  log_step "规范化目标镜像引用: ${DST_IMAGE} -> ${DST_IMAGE_RESOLVED}"
fi

# 统一清洗 estargz.list（不管来源）
if [[ -n "${ARTIFACT_SUFFIX}" ]]; then
  LIST_CLEAN="${WORK_DIR}/estargz${ARTIFACT_SUFFIX}.list.cleaned"
else
  LIST_CLEAN="${WORK_DIR}/estargz.list.cleaned"
fi
awk '
  BEGIN{FS=""; OFS=""}
  {
    gsub(/\r$/, "", $0);
    sub(/^[ \t]+/, "", $0);
    sub(/[ \t]+$/, "", $0);
    if ($0 == "") next;
    if ($0 !~ /^\//) next;
    if ($0 ~ /^\/(proc|sys|dev|run|var\/run)(\/|$)/) next;
    if ($0 ~ /\/$/) next;
    if (!seen[$0]++) print $0;
  }
' "${ESTARGZ_LIST}" > "${LIST_CLEAN}"

CANDIDATES_JSON="${WORK_DIR}/candidates.json"
if [[ -s "${CANDIDATES_JSON}" ]]; then
  LIST_FILTERED="${WORK_DIR}/estargz.list.filtered"
  python3 - "${LIST_CLEAN}" "${CANDIDATES_JSON}" "${LIST_FILTERED}" <<'PY'
import json
import sys
from pathlib import Path


def normalize_path_kind(item):
    raw = str(item.get("path_kind") or "").strip().lower()
    if raw in ("regular", "directory", "other"):
        return raw
    if "is_regular_file" in item:
        return "regular" if bool(item.get("is_regular_file")) else "other"
    return ""


list_path = Path(sys.argv[1])
candidates_path = Path(sys.argv[2])
out_path = Path(sys.argv[3])

try:
    candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
except Exception:
    candidates = []

rejected = set()
for item in candidates if isinstance(candidates, list) else []:
    if normalize_path_kind(item) in ("", "regular"):
        continue
    for path in [item.get("path")] + list(item.get("aliases") or []):
        if isinstance(path, str) and path.startswith("/"):
            rejected.add(path)

kept = []
removed = 0
for raw_line in list_path.read_text(encoding="utf-8").splitlines():
    path = raw_line.strip()
    if not path:
        continue
    if path in rejected:
        removed += 1
        continue
    kept.append(path)

out_path.write_text("".join(f"{path}\n" for path in kept), encoding="utf-8")
print(f"[apply] candidate metadata filter removed {removed} non-regular entries", file=sys.stderr)
PY
  mv "${LIST_FILTERED}" "${LIST_CLEAN}"
fi

if [[ ! -s "${LIST_CLEAN}" ]]; then
  die "清洗后 estargz.list 为空（原始：${ESTARGZ_LIST}）"
fi
log_step "hints source: ${HINTS_SOURCE}"
log_step "selector source: ${SELECTOR_SOURCE}"
log_step "input hints path: ${HINTS_FILE}"
log_step "generated prefetch list path: ${ESTARGZ_LIST}"
log_step "stale prefetch list detected: $([[ ${STALE_LIST_DETECTED} -eq 1 ]] && printf 'true' || printf 'false') (reason=${STALE_LIST_REASON})"
log_step "prefetch list: ${LIST_CLEAN} (lines=$(wc -l < "${LIST_CLEAN}" | tr -d ' '), regenerated=$([[ ${LIST_REGENERATED} -eq 1 ]] && printf 'true' || printf 'false'))"
log_step "selector mode: ${SELECTOR_MODE}"

OPTIMIZED=0

is_missing_parent_snapshot_error() {
  local err_file="$1"
  [[ -f "${err_file}" ]] || return 1
  grep -qE 'failed to stat parent: stat /var/lib/containerd-stargz-grpc/snapshotter/snapshots/[0-9]+/fs: no such file or directory' "${err_file}"
}

repair_stargz_namespace_snapshot_state() {
  local keys=""
  need_cmd ctr-remote
  log_step "检测到 stale stargz parent snapshot；尝试清理 namespace=${NAMESPACE} 的 stargz snapshot 记录并重建本地目录骨架"
  keys="$(ctr-remote -n "${NAMESPACE}" snapshots --snapshotter stargz ls 2>/dev/null | awk 'NR>1{print $1}' || true)"
  if [[ -n "${keys}" ]]; then
    while read -r key; do
      [[ -n "${key}" ]] || continue
      ctr-remote -n "${NAMESPACE}" snapshots --snapshotter stargz rm "${key}" >/dev/null 2>&1 || true
    done <<< "${keys}"
  else
    warn "未列出可清理的 namespace stargz snapshot keys (namespace=${NAMESPACE})"
  fi
  run_priv mkdir -p /var/lib/containerd-stargz-grpc/snapshotter/snapshots || true
  run_priv mkdir -p /var/lib/containerd-stargz-grpc/snapshotter/cache || true
  run_priv rm -rf /var/lib/containerd-stargz-grpc/snapshotter/snapshots/* 2>/dev/null || true
  run_priv rm -rf /var/lib/containerd-stargz-grpc/snapshotter/cache/* 2>/dev/null || true
}

stargz_consumable_smoke_check() {
  local image_ref="$1"
  local inspect_cmd=(nerdctl --namespace "${NAMESPACE}" image inspect "${image_ref}")
  local run_cmd=(
    nerdctl --namespace "${NAMESPACE}" run --rm --pull=never
    --snapshotter stargz --platform linux/amd64 --entrypoint /bin/true
    "${image_ref}"
  )
  local smoke_err=""
  smoke_err="$(mktemp /tmp/apply_stargz_smoke.XXXXXX.err)"

  need_cmd nerdctl
  log_step "验证 optimized image 的本地 stargz 可消费性"
  echo "+ ${inspect_cmd[*]}"
  if ! "${inspect_cmd[@]}" >/dev/null 2>"${smoke_err}"; then
    echo "[ERR] image optimize 成功，但对 nerdctl + stargz + pull=never 不可消费: ${image_ref}" >&2
    echo "[ERR] inspect: ${inspect_cmd[*]}" >&2
    echo "[ERR] run: ${run_cmd[*]}" >&2
    if [[ -s "${smoke_err}" ]]; then
      sed 's/^/[ERR] smoke stderr: /' "${smoke_err}" >&2 || true
    fi
    rm -f "${smoke_err}"
    return 1
  fi

  : > "${smoke_err}"
  echo "+ ${run_cmd[*]}"
  if ! "${run_cmd[@]}" >/dev/null 2>"${smoke_err}"; then
    if is_missing_parent_snapshot_error "${smoke_err}"; then
      repair_stargz_namespace_snapshot_state
      : > "${smoke_err}"
      echo "+ ${run_cmd[*]} # retry after stargz namespace repair"
      if "${run_cmd[@]}" >/dev/null 2>"${smoke_err}"; then
        rm -f "${smoke_err}"
        return 0
      fi
    fi
    echo "[ERR] image optimize 成功，但对 nerdctl + stargz + pull=never 不可消费: ${image_ref}" >&2
    echo "[ERR] inspect: ${inspect_cmd[*]}" >&2
    echo "[ERR] run: ${run_cmd[*]}" >&2
    if [[ -s "${smoke_err}" ]]; then
      sed 's/^/[ERR] smoke stderr: /' "${smoke_err}" >&2 || true
    fi
    rm -f "${smoke_err}"
    return 1
  fi
  rm -f "${smoke_err}"
}

if command -v ctr-remote >/dev/null 2>&1; then
  ctr_supports() { ctr-remote "$1" optimize -h 2>&1 | grep -q -- '--prefetch-list'; }
  ctr_supports_flag() {
    local sub="$1"
    local flag="$2"
    local help_txt=""
    help_txt="$(ctr-remote "${sub}" optimize -h 2>&1 || true)"
    printf '%s\n' "${help_txt}" \
      | tr ',()' '   ' \
      | tr '[:space:]' '\n' \
      | grep -Fxq -- "${flag}"
  }
  opt_sub=""
  if ctr_supports image; then
    opt_sub="image"
  elif ctr_supports images; then
    opt_sub="images"
  else
    warn "ctr-remote optimize 未发现 --prefetch-list；尝试使用 estargzify 路径"
    opt_sub=""
  fi

  if [[ -n "${opt_sub}" ]]; then
    OPT_FLAGS=()
    if ctr_supports_flag "${opt_sub}" "--oci"; then
      if is_local_registry_ref "${SRC_IMAGE_RESOLVED}" || is_local_registry_ref "${DST_IMAGE_RESOLVED}"; then
        if [[ "${IMAGEOPT_DISABLE_OCI_FOR_LOCAL_REGISTRY:-0}" == "1" ]]; then
          warn "检测到 IMAGEOPT_DISABLE_OCI_FOR_LOCAL_REGISTRY=1；对本地 registry 引用禁用 ctr-remote --oci（历史兼容模式）"
        else
          OPT_FLAGS+=(--oci)
          log_step "检测到本地 registry 引用；默认保留 ctr-remote --oci，以保留 eStargz TOC annotations"
        fi
      else
        OPT_FLAGS+=(--oci)
      fi
    fi
    if ctr_supports_flag "${opt_sub}" "--estargz"; then
      OPT_FLAGS+=(--estargz)
    fi
    CMD=(ctr-remote --namespace "${NAMESPACE}" "${opt_sub}" optimize "${OPT_FLAGS[@]}" --prefetch-list "${LIST_CLEAN}" "${SRC_IMAGE_RESOLVED}" "${DST_IMAGE_RESOLVED}")
    log_step "使用 ctr-remote 将 ${SRC_IMAGE_RESOLVED} -> ${DST_IMAGE_RESOLVED} (namespace=${NAMESPACE})"
    echo "+ ${CMD[*]}"
    if [[ ${DRY_RUN} -eq 0 ]]; then
      "${CMD[@]}"
    fi
    OPTIMIZED=1
  fi
fi

if [[ ${OPTIMIZED} -eq 0 ]]; then
  if command -v estargzify >/dev/null 2>&1 || command -v stargzify >/dev/null 2>&1; then
    BIN="$(command -v estargzify || command -v stargzify)"
    OUT_TAR=""
    cleanup_tmp() { [[ -n "${OUT_TAR}" ]] && rm -f "${OUT_TAR}"; }
    trap cleanup_tmp EXIT
    OUT_TAR="$(mktemp /tmp/estargz_opt.XXXXXX.tar)"
    CMD1=(${BIN} --prefetch "${LIST_CLEAN}" --input "${SRC_IMAGE_RESOLVED}" --output "${OUT_TAR}")
    log_step "使用 ${BIN} 生成临时 tar：${OUT_TAR}"
    echo "+ ${CMD1[*]}"
    if [[ ${DRY_RUN} -eq 0 ]]; then
      "${CMD1[@]}"
    fi
    if command -v ctr >/dev/null 2>&1; then
      CMD2=(ctr --namespace "${NAMESPACE}" images import --base-name "${DST_IMAGE_RESOLVED}" "${OUT_TAR}")
    elif command -v nerdctl >/dev/null 2>&1; then
      CMD2=(nerdctl --namespace "${NAMESPACE}" images import --base-name "${DST_IMAGE_RESOLVED}" "${OUT_TAR}")
    else
      die "缺少 ctr/nerdctl 命令以导入 tar"
    fi
    echo "+ ${CMD2[*]}"
    if [[ ${DRY_RUN} -eq 0 ]]; then
      "${CMD2[@]}"
    fi
    OPTIMIZED=1
  else
    die "需要 ctr-remote 或 estargzify/stargzify"
  fi
fi

[[ ${OPTIMIZED} -eq 1 ]] || die "镜像优化未执行"

if [[ ${DRY_RUN} -eq 0 ]]; then
  stargz_consumable_smoke_check "${DST_IMAGE_RESOLVED}" || exit 1
  if [[ -n "${ARTIFACT_SUFFIX}" ]]; then
    OPTIMIZED_IMAGE_RECORD="${WORK_DIR}/optimized_image${ARTIFACT_SUFFIX}.txt"
  else
    OPTIMIZED_IMAGE_RECORD="${WORK_DIR}/optimized_image.txt"
  fi
  printf '%s\n' "${DST_IMAGE_RESOLVED}" > "${OPTIMIZED_IMAGE_RECORD}"
  log_step "已生成镜像: ${DST_IMAGE_RESOLVED} (记录于 $(basename "${OPTIMIZED_IMAGE_RECORD}"))"
else
  log_step "Dry-run 完成，未执行实际导入"
fi
