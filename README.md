**English** | [简体中文](README.zh-CN.md)

# imageopt: eBPF-Driven Container Cold-Start Optimization

Observes real container file-access patterns with eBPF and generates high-quality eStargz prefetch hints. On controlled workloads, lazy cold-start latency is reduced by **23–29%**.

## Capabilities

- eBPF sampling of container-scoped OPEN/MMAP events
- Deterministic candidate aggregation with quality gates
- Optional LLM-assisted path refinement
- One-click apply + strict cold-start benchmark pipeline

## Measured Results

| Batch         | Unhinted | Hinted | Reduction |
|---------------|----------|--------|-----------|
| Deterministic | 2.360 s  | 1.677 s| **28.94%**|
| LLM-assisted  | 2.763 s  | 2.101 s| **23.96%**|

All records are independently verifiable. Sampling operates at file level; chunk-aware post-processing is available.

## Usage

```bash
# Portable validation (no containers required)
python tools/validate_portable.py
python tools/demo_selector.py

# Full Linux workflow
python3 scripts/oneclick_product.py run <IMAGE> --work-dir /tmp/run
python3 scripts/oneclick_product.py report --work-dir /tmp/run
```

Built for the Tencent Rhino-bird Open Source Program. Requires Linux, containerd, stargz-snapshotter, and an eBPF-capable kernel.
