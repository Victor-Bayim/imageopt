**English** | [简体中文](README.zh-CN.md)

# imageopt: eBPF container cold start

eBPF watches OPEN/MMAP inside the container and writes eStargz prefetch hints. Controlled workload, lazy cold start:

| Path | Before | After | Cut |
| --- | ---: | ---: | ---: |
| Deterministic | 2.360 s | 1.677 s | **28.9%** |
| LLM rerank | 2.763 s | 2.101 s | **24.0%** |

The sampler attaches a BPF program scoped to the container namespace. Events are grouped by path, passed through a quality gate, and emitted as a prefetch list. Default selection is deterministic. An LLM stage can rerank the list when a model is wired in. The apply script writes the hints into an eStargz image; the strict bench measures cold start under stargz-snapshotter.

Sampling is file-level. Chunk-aware post-processing is in the tree for a second pass. Portable checks (selector, quality gate, provider contracts, record hashes) run with Python 3.10 and the standard library. The full path needs Linux, containerd/nerdctl, stargz-snapshotter, and an eBPF kernel.

```bash
python tools/validate_portable.py
python tools/demo_selector.py
python tools/verify_records.py

python3 scripts/oneclick_product.py run <IMAGE> --work-dir /tmp/run
python3 scripts/oneclick_product.py report --work-dir /tmp/run
```

Tencent Rhino-bird Open Source Program.
