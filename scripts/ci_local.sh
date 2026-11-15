#!/usr/bin/env bash

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

export PYTHONDONTWRITEBYTECODE=1

log() {
  printf '[ci] %s\n' "$*"
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    printf '[ci] missing required command: %s\n' "$1" >&2
    exit 1
  }
}

collect_python_files() {
  find orchestrator optimizer scripts tests -type f -name '*.py' | sort
}

collect_shell_files() {
  find scripts sampler/scripts sampler/user -type f -name '*.sh' | sort
}

need_cmd python3
need_cmd bash
need_cmd find

log "Python bytecode compilation"
mapfile -t py_files < <(collect_python_files)
if ((${#py_files[@]} > 0)); then
  python3 -m py_compile "${py_files[@]}"
fi

log "Shell syntax checks"
mapfile -t sh_files < <(collect_shell_files)
for sh_file in "${sh_files[@]}"; do
  bash -n "${sh_file}"
done

if command -v shellcheck >/dev/null 2>&1; then
  log "ShellCheck"
  if ((${#sh_files[@]} > 0)); then
    shellcheck -x "${sh_files[@]}"
  fi
else
  log "shellcheck not installed; skipping"
fi

log "Unit tests"
python3 -m unittest discover -s tests -v

log "CI checks completed"

