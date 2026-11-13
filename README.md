# imageopt

Container image cold-start optimization using eBPF-based file access sampling and eStargz prefetch hints.

## Overview

`imageopt` records real file accesses during container startup, turns them into ranked prefetch hints, and applies them to eStargz images. The pipeline is a recoverable single-work-directory flow:

`preflight → sample → export-enrich → analyze → (optional llm-optimize) → apply → benchmark → report`

Default mode is deterministic. LLM re-ranking is optional and falls back to the deterministic baseline on failure.

## Role

Independent implementation (sampling, selection, application, and evaluation).

## Key Results

On the `ready-serving-layout-trap-v5` workload (median of 3 runs):

- Service ready time: 2.360 s → 1.677 s (−28.94%)

Results are limited to this workload and comparison contract. Sampling is file-level; chunk-aware logic is used only for post-hoc audit.

## Structure

- `orchestrator/` — stage implementations, manifest, LLM providers
- `sampler/` — eBPF programs and user-space collector
- `scripts/` — main entry (`oneclick_product.py`) and helpers
- `optimizer/` — analysis and selection tools
- `docs/` — current architecture and usage notes

## Usage

```bash
python3 scripts/oneclick_product.py run <IMAGE> --work-dir /tmp/imageopt_run
python3 scripts/oneclick_product.py report --work-dir /tmp/imageopt_run
```

Requires Linux, containerd + nerdctl, and appropriate privileges for eBPF.

## Limitations

- File-level sampling only.
- Conclusions limited to the measured workload and triple-benchmark contract.
- LLM path is advisory and must produce strictly grounded JSON.

## License

MIT (or specify your preferred license).
