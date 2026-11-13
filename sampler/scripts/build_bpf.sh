#!/usr/bin/env bash
set -Eeuo pipefail

# ==== tiny logger ====
log()  { printf "\033[1;34m[+] %s\033[0m\n" "$*"; }
warn() { printf "\033[1;33m[!] %s\033[0m\n" "$*"; }
err()  { printf "\033[1;31m[x] %s\033[0m\n" "$*" >&2; }

# Build the eBPF program
log "Building eBPF tracer"
