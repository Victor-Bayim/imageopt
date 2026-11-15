# sampler_trace_ext 使用说明（与当前代码一致）

## 1. 构建

在仓库根目录执行：

```bash
bash sampler/scripts/build_bpf.sh
```

该脚本会完成：
- 编译 `sampler/BPF/file_access_ebpf_trace.bpf.c`
- 生成 `sampler/user/file_access_ebpf_trace.skel.h`
- 构建 `sampler/user/build/sampler_trace_ext`

---

## 2. 运行方式

### 2.1 直接按 PID 运行（推荐）

```bash
sudo ./sampler/user/build/sampler_trace_ext \
  --pid <PID> \
  --prom-port 8000 \
  --sampling-rate 100 \
  --mmap-window 120 \
  --cg-refresh 2 \
  --file-sink /tmp/raw_events.jsonl \
  --redis-url redis://127.0.0.1:6379/0
```

### 2.2 通过容器名运行（包装脚本）

`sampler_trace_ext` 本体只支持 `--pid`。如果你想传容器名，请使用包装脚本：

```bash
sudo sampler/user/sampler_container.sh \
  --container <container_name_or_id> \
  --prom-port 8000 \
  --sampling-rate 100 \
  --mmap-window 120 \
  --cg-refresh 2 \
  --file-sink /tmp/raw_events.jsonl \
  --redis-url redis://127.0.0.1:6379/0
```

---

## 3. 参数说明

- `--pid <PID>`：目标进程 PID（必填）
- `--prom-port <PORT>`：Prometheus 暴露端口，默认 `8000`
- `--sampling-rate <0..100>`：采样率，默认 `100`
- `--mmap-window <SEC>`：LSM mmap 捕获窗口（秒）；`0` 表示关闭窗口约束
- `--file-sink <PATH>`：推荐；原始事件追加写入本地 JSONL 文件
- `--redis-url <URL>`：可选，事件额外写入 Redis list `file_open_events`
- `--cg-refresh <SEC>`：可选，周期刷新 cgroup 白名单（0 表示不刷新）
- `--quiet <0|1>`：日志静默，默认 `1`
- `--log-sample <N>`：每 `N` 条打印 1 条采样日志（`0` 表示不打印）
- `--log-file <PATH>`：采样日志输出到文件

原始 `OPEN` 事件 JSON 现已额外带 `dirfd`：
- `open()` 会记为 `AT_FDCWD`
- `openat/openat2` 会保留真实 `dirfd`
- 这让后处理至少能区分“相对路径本来就是基于 cwd”还是“相对路径依赖某个显式目录 fd”

---

## 4. 指标与观测

访问：

```text
http://127.0.0.1:<prom-port>/metrics
```

关键指标：
- `file_events_total`
- `open_events_total`
- `mmap_events_total`
- `ringbuf_drops_total`
- `filtered_out_total`
- `sampling_drops_total`
- `file_events_per_second`

---

## 5. 注意事项

- 需要 root（或等效能力）以加载 eBPF 并读取相关 `/proc` 信息。
- 建议内核启用 BTF，且支持 BPF LSM；否则 mmap 会退化到 tracepoint 路径，准确性下降。
- 单机场景建议默认启用 `--file-sink`，把 Redis 仅作为可选镜像导出通道。
- 若配置 Redis，退出时会尽量 drain 队列；若队列溢出，会打印 dropped 计数告警。
