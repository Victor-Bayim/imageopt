**English** | [简体中文](README.zh-CN.md)

# imageopt: eBPF-Driven Container Cold-Start Optimization

Observes real container file-access patterns with eBPF and generates high-quality eStargz prefetch hints. On controlled workloads, lazy cold-start latency is reduced by **23–29%**.

## What It Does

During container startup, many layers are pulled lazily. imageopt captures the actual OPEN and MMAP events that occur inside the container namespace:

- eBPF-based sampling of container-scoped file access events with low overhead.
- Deterministic aggregation and quality-gated selection of the most valuable files to prefetch.
- Optional LLM-assisted refinement of preferred paths when an external model is available.
- A complete apply-and-benchmark pipeline that injects the generated hints into an eStargz image and measures cold-start time under stargz-snapshotter.

The tool produces both the hint files and a reproducible measurement workflow. Sampling is file-level; chunk-aware post-processing is available for finer analysis.

## Measured Results

| Batch         | Unhinted lazy | Hinted lazy | Reduction |
|---------------|---------------|-------------|-----------|
| Deterministic | 2.360 s       | 1.677 s     | **28.94%**|
| LLM-assisted  | 2.763 s       | 2.101 s     | **23.96%**|

Results come from independent batches on a controlled workload and are independently verifiable. The LLM batch used a manually imported response (no live API call).

## Usage

```bash
# Portable validation (no containers or API keys required)
python tools/validate_portable.py
python tools/demo_selector.py

# Full Linux workflow
python3 scripts/oneclick_product.py run <IMAGE> --work-dir /tmp/run
python3 scripts/oneclick_product.py report --work-dir /tmp/run
```

Built for the Tencent Rhino-bird Open Source Program. Full execution requires Linux, containerd/nerdctl, stargz-snapshotter, and an eBPF-capable kernel.
