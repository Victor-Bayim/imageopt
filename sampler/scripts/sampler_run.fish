#!/usr/bin/env fish
# scripts/sampler_run.fish
# 统一入口：以容器名或PID运行采样器
# 用法示例：
#   sudo ./scripts/sampler_run.fish --container tfs-demo --prom-port 8000 --sampling-rate 100 --mmap-window 2
#   sudo ./scripts/sampler_run.fish --pid 12345         --prom-port 8000 --sampling-rate 100 --mmap-window 2
# 可选项：
#   --redis-host 127.0.0.1 --redis-port 6379 --redis-db 0   # 和 sampler 的 --redis-url 连用
#   --no-run   # 只打印命令不执行（用来检查参数）

function usage
    echo "Usage: sudo $argv[0] [--container NAME | --pid PID]"
    echo "                    [--prom-port 8000] [--sampling-rate 100]"
    echo "                    [--mmap-window SEC]"
    echo "                    [--redis-host HOST --redis-port PORT --redis-db DB]"
    echo "                    [--no-run]"
end

set -l ROOT (realpath (dirname (status -f))/..)
set -l BIN  "$ROOT/user/sampler_container.sh"

set -l CONTAINER ""
set -l PID ""
set -l PROM_PORT 8000
set -l SAMPLING  100
set -l MMAP_WIN  0
set -l REDIS_HOST ""
set -l REDIS_PORT 6379
set -l REDIS_DB   0
set -l NO_RUN 0

for i in (seq 1 (count $argv))
    set -l a $argv[$i]
    switch $a
        case --container
            set CONTAINER $argv[(math $i + 1)]
            set i (math $i + 1)
        case --pid
            set PID $argv[(math $i + 1)]
            set i (math $i + 1)
        case --prom-port
            set PROM_PORT $argv[(math $i + 1)]
            set i (math $i + 1)
        case --sampling-rate
            set SAMPLING $argv[(math $i + 1)]
            set i (math $i + 1)
        case --mmap-window
            set MMAP_WIN $argv[(math $i + 1)]
            set i (math $i + 1)
        case --redis-host
            set REDIS_HOST $argv[(math $i + 1)]
            set i (math $i + 1)
        case --redis-port
            set REDIS_PORT $argv[(math $i + 1)]
            set i (math $i + 1)
        case --redis-db
            set REDIS_DB $argv[(math $i + 1)]
            set i (math $i + 1)
        case --no-run
            set NO_RUN 1
        case '*'
            # ignore unknowns for now
    end
end

if not test -x $BIN
    echo "[ERR] binary not found: $BIN"
    echo "      build it first:  cmake -S user -B user/build && cmake --build user/build -j"
    exit 1
end

if test -z "$CONTAINER" -a -z "$PID"
    usage
    exit 1
end

set -l CMD $BIN
if test -n "$CONTAINER"
    set CMD $CMD --container $CONTAINER
else
    set CMD $CMD --pid $PID
end

set CMD $CMD --prom-port $PROM_PORT --sampling-rate $SAMPLING --mmap-window $MMAP_WIN

if test -n "$REDIS_HOST"
    set -l REDIS_URL "redis://$REDIS_HOST:$REDIS_PORT/$REDIS_DB"
    set CMD $CMD --redis-url $REDIS_URL
end

echo "[sampler] $CMD"
if test $NO_RUN -eq 0
    eval $CMD
else
    echo "[noop] not running (because --no-run)."
end
