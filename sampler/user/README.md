**English** | [简体中文](README.zh-CN.md)

# sampler_trace_ext usage (matching the current code)

## 1. Build

Run from the repository root:

```bash
bash sampler/scripts/build_bpf.sh
```

The script:
- Compiles `sampler/BPF/file_access_ebpf_trace.bpf.c`
- Generates `sampler/user/file_access_ebpf_trace.skel.h`
- Builds `sampler/user/build/sampler_trace_ext`

---

## 2. Running

### 2.1 Run directly by PID (recommended)

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

### 2.2 Run by container name (wrapper)

`sampler_trace_ext` itself supports only `--pid`. Use the wrapper to pass a container name:

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

## 3. Parameters

- `--pid <PID>`: target process PID (required)
- `--prom-port <PORT>`: Prometheus port, default `8000`
- `--sampling-rate <0..100>`: sampling rate, default `100`
- `--mmap-window <SEC>`: LSM mmap capture window in seconds; `0` disables the window constraint
- `--file-sink <PATH>`: recommended; append raw events to a local JSONL file
- `--redis-url <URL>`: optionally mirror events to Redis list `file_open_events`
- `--cg-refresh <SEC>`: optional periodic cgroup allowlist refresh (`0` disables refresh)
- `--quiet <0|1>`: quiet logging, default `1`
- `--log-sample <N>`: log one sample per `N` events (`0` disables sample logging)
- `--log-file <PATH>`: sampler log output file

Raw `OPEN` event JSON additionally includes `dirfd`:
- `open()` is recorded as `AT_FDCWD`
- `openat/openat2` retain the actual `dirfd`
- Post-processing can distinguish cwd-relative paths from paths relative to an explicit directory fd

---

## 4. Metrics and observability

Visit:

```text
http://127.0.0.1:<prom-port>/metrics
```

Key metrics:
- `file_events_total`
- `open_events_total`
- `mmap_events_total`
- `ringbuf_drops_total`
- `filtered_out_total`
- `sampling_drops_total`
- `file_events_per_second`

---

## 5. Notes

- Root (or equivalent capabilities) is required to load eBPF and read relevant `/proc` data.
- Enable kernel BTF and BPF LSM support where possible; otherwise mmap falls back to a less accurate tracepoint path.
- For a single host, enable `--file-sink` by default and use Redis only as an optional mirror/export channel.
- With Redis configured, shutdown attempts to drain the queue; queue overflow prints a dropped-count warning.
