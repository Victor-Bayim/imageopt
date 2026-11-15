#!/usr/bin/env bash
# sampler/user/sampler_container.sh
# INTERNAL SAMPLER HELPER
# NOT A PRODUCT ENTRYPOINT
# 作用：对 sampler_trace_ext 做一层“容器友好”的包装：
# - 支持 --container <NAME>（用 docker/nerdctl 解析主进程 PID）
# - 自动过滤 sampler_trace_ext 不支持的参数（避免 usage 退出）
# - 最后用 --pid <PID> 调用真实二进制
set -Eeuo pipefail

SAMPLER_BIN_DEFAULT="$(dirname "$0")/build/sampler_trace_ext"
SAMPLER_BIN="${SAMPLER_BIN:-${SAMPLER_BIN_DEFAULT}}"

die() { echo -e "\033[1;31m[ERR] $*\033[0m" >&2; exit 1; }
warn(){ echo -e "\033[1;33m[WARN] $*\033[0m" >&2; }
log() { echo -e "\033[1;34m[+] $*\033[0m"; }

command -v "${SAMPLER_BIN}" >/dev/null 2>&1 || die "未找到 sampler 二进制：${SAMPLER_BIN}"

# 解析参数：可能包含 --container 或 --pid 以及其它键值
TARGET_CONTAINER=""
TARGET_PID=""
PASSTHRU_ARGS=()

# 把所有传入参数先收集，后面做支持性过滤
RAW_ARGS=("$@")

# 提前解析 --container / --pid
i=0
while [ $i -lt ${#RAW_ARGS[@]} ]; do
  a="${RAW_ARGS[$i]}"
  case "$a" in
    --container)
      ((i++)) || true
      TARGET_CONTAINER="${RAW_ARGS[$i]:-}"
      if [ -z "$TARGET_CONTAINER" ]; then die "--container 需要一个容器名/ID"; fi
      ;;
    --pid)
      ((i++)) || true
      TARGET_PID="${RAW_ARGS[$i]:-}"
      if [ -z "$TARGET_PID" ]; then die "--pid 需要一个 PID"; fi
      ;;
  esac
  ((i++)) || true
done

# 如指定了容器名而未显式给 pid，则解析容器 PID
resolve_pid_from_container() {
  local name="$1"
  local pid=""
  if command -v docker >/dev/null 2>&1; then
    pid="$(docker inspect -f '{{.State.Pid}}' "$name" 2>/dev/null || true)"
  elif command -v nerdctl >/dev/null 2>&1; then
    pid="$(nerdctl inspect -f '{{.State.Pid}}' "$name" 2>/dev/null || true)"
  else
    die "找不到 docker/nerdctl，无法从容器名解析 PID：$name"
  fi
  if ! [[ "$pid" =~ ^[0-9]+$ ]] || [ "$pid" -le 0 ]; then
    die "无法解析容器 [$name] 的主进程 PID（得到：$pid）"
  fi
  echo "$pid"
}

if [ -n "$TARGET_CONTAINER" ] && [ -z "$TARGET_PID" ]; then
  TARGET_PID="$(resolve_pid_from_container "$TARGET_CONTAINER")"
  log "容器 [$TARGET_CONTAINER] -> PID [$TARGET_PID]"
fi

# 读取 sampler 的 usage，识别它支持哪些可选项（老版本可能不支持某些新参数）
SAMPLER_USAGE="$("${SAMPLER_BIN}" --help 2>&1 || "${SAMPLER_BIN}" 2>&1 || true)"

supports() {
  local flag="$1"
  echo "$SAMPLER_USAGE" | grep -q -- "$flag"
}

# 遍历原始参数，把 sampler 不认识的键值过滤掉。
# 特别注意：我们最终一定会加上 --pid <PID>，因此若用户传了 --container，就不再把它传递给 sampler_trace_ext。
i=0
while [ $i -lt ${#RAW_ARGS[@]} ]; do
  a="${RAW_ARGS[$i]}"
  case "$a" in
    --container)
      # 吃掉 --container 及其值，不传给真实二进制
      ((i++)) || true  # skip value
      ;;
    --pid)
      # 如果用户显式传了 --pid，我们会保留；否则由容器解析得到的 PID 来补充
      if supports "--pid"; then
        PASSTHRU_ARGS+=(--pid "${RAW_ARGS[$((i+1))]:-}")
      else
        warn "sampler 不支持 --pid？这很不正常；usage:\n${SAMPLER_USAGE}"
      fi
      ((i++)) || true
      ;;
    --prom-port)
      if supports "--prom-port"; then PASSTHRU_ARGS+=(--prom-port "${RAW_ARGS[$((i+1))]:-}"); fi
      ((i++)) || true
      ;;
    --sampling-rate)
      if supports "--sampling-rate"; then PASSTHRU_ARGS+=(--sampling-rate "${RAW_ARGS[$((i+1))]:-}"); fi
      ((i++)) || true
      ;;
    --mmap-window)
      if supports "--mmap-window"; then PASSTHRU_ARGS+=(--mmap-window "${RAW_ARGS[$((i+1))]:-}"); fi
      ((i++)) || true
      ;;
    --mmap-qps)
      # 很多版本并不支持这个选项；仅当 usage 里出现时才透传
      if supports "--mmap-qps"; then PASSTHRU_ARGS+=(--mmap-qps "${RAW_ARGS[$((i+1))]:-}"); fi
      ((i++)) || true
      ;;
    --redis-url)
      if supports "--redis-url"; then PASSTHRU_ARGS+=(--redis-url "${RAW_ARGS[$((i+1))]:-}"); fi
      ((i++)) || true
      ;;
    --redis-key)
      if supports "--redis-key"; then PASSTHRU_ARGS+=(--redis-key "${RAW_ARGS[$((i+1))]:-}"); fi
      ((i++)) || true
      ;;
    --file-sink)
      if supports "--file-sink"; then PASSTHRU_ARGS+=(--file-sink "${RAW_ARGS[$((i+1))]:-}"); fi
      ((i++)) || true
      ;;
    --mount-prefix)
      if supports "--mount-prefix"; then PASSTHRU_ARGS+=(--mount-prefix "${RAW_ARGS[$((i+1))]:-}"); fi
      ((i++)) || true
      ;;
    *)
      # 单值开关或未知参数：如果 usage 里能看到，就透传
      if echo "$a" | grep -q '^--'; then
        if supports "$a"; then PASSTHRU_ARGS+=("$a"); fi
      else
        # 普通位置参数：不处理
        PASSTHRU_ARGS+=("$a")
      fi
      ;;
  esac
  ((i++)) || true
done

# 若未显式传 --pid，但我们已经解析出 TARGET_PID，则补上
if [ -n "$TARGET_PID" ]; then
  if ! printf '%s\n' "${PASSTHRU_ARGS[@]}" | grep -q -- '--pid'; then
    PASSTHRU_ARGS=(--pid "$TARGET_PID" "${PASSTHRU_ARGS[@]}")
  fi
fi

# 最终执行
exec "${SAMPLER_BIN}" "${PASSTHRU_ARGS[@]}"
