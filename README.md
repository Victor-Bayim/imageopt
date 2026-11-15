**English** | [简体中文](README.zh-CN.md)

# imageopt: eBPF-Driven Container Cold-Start Optimization

Container cold-start optimization that records real OPEN/MMAP patterns with eBPF and turns them into eStargz prefetch hints. On the controlled workload, lazy cold-start time drops from 2.360 s to 1.677 s (deterministic) and from 2.763 s to 2.101 s (LLM-assisted batch) — **28.9%** and **24.0%** reductions.

The sampler attaches an eBPF program that sees file-access events inside the container namespace. Those events are aggregated by path, filtered through a quality gate, and turned into a prefetch list. An optional LLM stage can re-rank preferred paths when a model is supplied; the default path is fully deterministic. The apply script injects the hints into an eStargz image, and the strict benchmark measures cold-start under stargz-snapshotter.

Portable checks (no container, no API key) cover the selector, quality gate, provider contracts, and record verification. The full Linux flow needs containerd/nerdctl, stargz-snapshotter, and an eBPF-capable kernel. Sampling is file-level; chunk-aware post-processing is available for later analysis. The LLM batch in the archived results used a manually imported response rather than a live API call.

```bash
# Portable checks
python tools/validate_portable.py
python tools/demo_selector.py
python tools/verify_records.py

# Full workflow
python3 scripts/oneclick_product.py run <IMAGE> --work-dir /tmp/run
python3 scripts/oneclick_product.py report --work-dir /tmp/run
```

Built for the Tencent Rhino-bird Open Source Program.
