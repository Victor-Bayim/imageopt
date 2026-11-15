**English** | [简体中文](KNOWN_LIMITATIONS.zh-CN.md)

# Known Limitations

This document lists the scope supported by the current source and tests.

## 1. Sampling scope

- Sampling remains file-level observation collection.
- Image TOC, TOC shadow and chunk shadow appear only in post-processing/reporting when an image TOC is available.
- The implementation does not directly sample chunks.

## 2. Applicability

- Current evidence focuses on the `ready-serving-layout-trap-v5` main experiment.
- Results do not generalize to arbitrary workloads.

## 3. LLM scope

- An LLM is optional.
- The default mode remains `deterministic`.
- `llm-auto` falls back to deterministic hints when the provider is unavailable, the response is unparseable or violates the contract, or grounding leaves no usable paths.

## 4. Contract scope

- The LLM contract is strict JSON.
- `legacy YAML` exists only for manual-response import compatibility.
- The default manual-response filename may still end in `.yaml`; this does not make YAML the preferred contract.

## 5. Platform scope

- The product policy supports a single Linux host only.
- Runtime prerequisites are `containerd + nerdctl`.
- `root` or passwordless `sudo` is required.

## 6. Quality semantics

- Analyze/apply quality issues are primarily handled as advisories.
- `quality_status=warning` or `poor` does not automatically mean stage `failed`.
- If grounding leaves no usable LLM paths, retain deterministic hints instead.
- Stages fail only when consumable `hints.yaml` / `estargz.list` cannot be produced, execution fails, artifacts are missing/unusable, or benchmark results are invalid.

## 7. Benchmark scope

- Main conclusions use the `ready-serving-layout-trap-v5` triple benchmark.
- `runs=1 after advisory fix` is a smoke/regression check only.
- Lazy benchmarks use a full-pull guard; invalidation fails the benchmark rather than reporting a performance comparison.

## 8. `chunk-shadow` scope

- `chunk-shadow` remains an experimental parallel output.
- It uses post-processing audits with an image TOC and does not change the original file-level observations.
- It does not replace the default deterministic file selector.

## 9. Entry point

- The sole supported entry point is `scripts/oneclick_product.py`.
