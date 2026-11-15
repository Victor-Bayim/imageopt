**English** | [简体中文](ORIGINAL_PROJECT_README.zh-CN.md)

# imageopt

`imageopt` is a Python control plane for container-image cold-start optimization. It converts observed startup file accesses into eStargz prefetch hints and connects `sample -> analyze -> llm-optimize -> apply -> benchmark -> report` into a resumable workflow in one working directory.
The sole supported entry point is [`scripts/oneclick_product.py`](../scripts/oneclick_product.py). Historical documents under `docs/archive/` are archived rather than current references.

## Current behavior

- Sole supported entry point: `scripts/oneclick_product.py`
- Supported modes: `deterministic`, `llm-auto`, `llm-manual`
- Default mode: `deterministic`
- Sampling scope: file-level sampling; chunk-aware logic appears only in the analyze/apply/report audit layer
- LLM output contract: JSON-first, with strict JSON as final output
- The only allowed top-level JSON structure:
```json
{"P0":[],"P1":[],"P2":[],"P3":[]}
```
- `legacy YAML` remains only for manual-response import compatibility
- Analyze/apply quality judgments are advisories: low quality, budget trimming and low confidence do not immediately stop execution
- LLM output that leaves no usable paths after grounding is discarded in favor of deterministic hints
- Stages hard-fail only if consumable `hints.yaml` / `estargz.list` cannot be produced, execution fails, artifacts are missing/unusable, or benchmarks lack valid samples
- The main experiment uses the `ready-serving-layout-trap-v5` triple benchmark

## System capabilities

1. Run preflight checks on the target image to verify the environment and supported scope.
2. Start the container and capture startup file accesses in `raw_events.jsonl`.
3. Export and enrich OPEN/MMAP metadata into `events.jsonl` and `events_enriched.jsonl`.
4. Aggregate candidate paths, audit quality and generate deterministic hints.
5. Optionally use an LLM to rerank or conservatively trim the deterministic baseline.
6. Apply hints to the target image to generate a hinted eStargz image.
7. Run cold-start benchmarks and produce a unified report.

## Scope

- Experimental conclusions are limited to the `ready-serving-layout-trap-v5` triple benchmark.
- Sampling observations are file-level events; when an image TOC exists, chunk-aware processing only audits, explains or experimentally compares selected paths from a layer/chunk perspective.
- See [`docs/KNOWN_LIMITATIONS.md`](KNOWN_LIMITATIONS.md) for details.

## Supported workflow

One entry script drives all supported stages:

1. `preflight`
2. `sample`
3. `export-enrich`
4. `analyze`
5. `llm-optimize`, entered only for `llm-auto` or `llm-manual`
6. `apply`, explicitly enabled by the command
7. `benchmark`, explicitly enabled by the command
8. `report`

The most common commands are:
```bash
python3 scripts/oneclick_product.py run <IMAGE> --work-dir /tmp/imageopt_run
python3 scripts/oneclick_product.py report --work-dir /tmp/imageopt_run
```

`run` records passed stages in `manifest.json` and supports resuming in the same `work-dir`.

## Supported modes

### `deterministic`

- Default mode
- Depends only on sampling, aggregation, quality auditing and the deterministic selector
- Produces the system baseline
- A deterministic baseline is generated even when an LLM is enabled

### `llm-auto`

- Calls an LLM provider on top of the deterministic baseline
- Provider configuration is managed by `orchestrator/llm.py` and `orchestrator/llm_providers/`
- `run --mode llm-auto` requires a valid API key
- Retains deterministic hints if the upstream provider is unavailable, parsing/contract validation fails, or grounding leaves no usable paths
- `run --mode llm-auto` does not silently fall back to manual web mode

### `llm-manual`

- Exports a web prompt bundle
- The user obtains a web response and imports it with `--manual-response-file`
- Import first attempts strict JSON parsing
- Non-JSON input triggers legacy YAML compatibility parsing
- Grounding and post-processing validation still apply

## LLM contract

Key constraints for prompts and import logic:

- Final output must be strict JSON without Markdown, explanations or code fences
- Only `P0`, `P1`, `P2` and `P3` are allowed at the top level
- Every value must be an array of absolute file-path strings
- Paths must come from the input candidate list
- Invented paths are forbidden
- Directories are forbidden
- Wildcards are forbidden
- Noise paths such as `/proc`, `/sys`, `/dev` and `/run` must be excluded
- The prompt requests internal self-checks, which must not appear in the final output

Priority semantics:

- `P0`: required at the earliest startup phase; few and critical
- `P1`: accessed soon after startup, with a high impact on readiness latency
- `P2`: useful, but must not displace the `P0/P1` budget
- `P3`: low priority or currently not recommended for prefetching

Selection favors:

- Earlier access
- Higher hit counts
- Agreement across OPEN/MMAP/mapfiles/dict/open_devino signals
- Higher confidence
- Conservative handling of very large files

## Quality model

Analyze/apply quality is split into two layers:

### Advisory

These conditions populate structured `quality_status`, `warnings`, `recommendation` and `blockers` fields without automatically setting the stage to `failed`:

- Few sampled events
- Few candidates
- Low coverage
- Too few or too many hints
- Budget overflow that can be trimmed while continuing
- Grounding removes invalid paths but retains consumable results
- Low selector confidence
- An unusual early-onehit proportion
- An unusual mmap/open ratio

### Hard fail

These conditions still terminate execution:

- Command execution failure
- Required inputs are missing or unparseable
- No usable sample output
- Analyze cannot generate valid hints
- No consumable `hints.yaml` / `estargz.list` can be produced
- Apply execution failure
- The applied image is missing or unusable
- No valid benchmark results
- The lazy benchmark full-pull guard invalidates the result

In the LLM stage, output with no usable grounded paths is discarded in favor of deterministic hints; this alone does not fail the entire pipeline.

## Key artifacts

Each run uses a `work-dir` organized around at least these files:

- `manifest.json`
  Stage states, artifact paths and pause/resume information for the current run
- `raw_events.jsonl`
  Raw file-access events
- `events.jsonl`
  Exported events
- `events_enriched.jsonl`
  Events with enriched OPEN/MMAP metadata
- `quality_gate.json`
  Analyze quality audits and advisory fields
- `selector_stats_hints.json`
  Selector statistics and quality recommendations for current hints
- `hints.yaml`
  Hints actually consumed by apply
- `estargz.list`
  Prefetch list actually consumed by apply
- `llm_result.json`
  LLM results, fallback information and grounded status
- `apply_result.json`
  Apply results and consumability validation summary
- `benchmark_result.json`
  Aggregated benchmark results
- `run_report.json`
  Unified structured report
- `run_report.md`
  Human-readable Markdown report

## Supported environment

The product policy is defined by `enforced_supports_payload()` in the source:

- A single Linux host
- `containerd + nerdctl`
- `root` or passwordless `sudo`
- File sink as the default source of truth
- In `auto` mode, gated startup prefers host launcher static, then image shell, then experimental host launcher dynamic

## Quick start

### 1. Run preflight first

```bash
python3 scripts/oneclick_product.py preflight <IMAGE> --work-dir /tmp/imageopt_run
```

### 2. Run the complete deterministic baseline

```bash
python3 scripts/oneclick_product.py run <IMAGE> \
  --work-dir /tmp/imageopt_run \
  --mode deterministic
```

### 3. Generate a report

```bash
python3 scripts/oneclick_product.py report --work-dir /tmp/imageopt_run
```

### 4. Enable `llm-auto`

```bash
python3 scripts/oneclick_product.py run <IMAGE> \
  --work-dir /tmp/imageopt_llm_auto \
  --mode llm-auto \
  --llm-provider openai
```

Notes:
- Requires working provider configuration and an API key
- Unusable LLM responses fall back to deterministic hints
- Does not automatically switch to manual web mode

### 5. Enable `llm-manual`

```bash
python3 scripts/oneclick_product.py run <IMAGE> \
  --work-dir /tmp/imageopt_llm_manual \
  --mode llm-manual
```

The following files are generated:
- `prompt.txt`
- `web_llm_ready_to_paste.txt`
- `llm_manual_instructions.md`
- `import_manual_web_llm.sh`
- `resume_manual_web_llm.sh`

After obtaining the web response, resume with:
```bash
python3 scripts/oneclick_product.py llm-optimize \
  --work-dir /tmp/imageopt_llm_manual \
  --manual-response-file /tmp/imageopt_llm_manual/hints_raw.manual.yaml
```

Although the default filename remains `hints_raw.manual.yaml`, the contract is JSON-first.

### 6. Add apply and benchmark to the same run

```bash
python3 scripts/oneclick_product.py run <IMAGE> \
  --work-dir /tmp/imageopt_full \
  --mode deterministic \
  --apply \
  --benchmark
```

### 7. Run the main experiment triple benchmark directly

The main experiment compares three groups:
1. raw full pull baseline
2. unoptimized eStargz lazy
3. hinted eStargz lazy

Direct benchmark invocation:

```bash
python3 scripts/oneclick_product.py benchmark \
  --work-dir /tmp/layout_trap_v5_bench \
  --base-image <RAW_IMAGE> \
  --unopt-image <UNOPT_ESTARGZ_IMAGE> \
  --opt-image <HINTED_ESTARGZ_IMAGE> \
  --workload ready-serving-layout-trap-v5 \
  --mode ready \
  --comparison-mode raw-full-vs-estargz-lazy-triple \
  --runs 3
```

Using a triple benchmark inside `run` requires explicit `--bench-unopt-image`.

## Main experimental conclusions

The current experiment centers on the `ready-serving-layout-trap-v5` triple benchmark.

Main conclusions use `runs=3`:
- `baseline_median_full_pull_ready_ms = 453634.0`
- `unoptimized_median_lazy_ready_ms = 2360.0`
- `optimized_median_lazy_ready_ms = 1677.0`
- `unoptimized_vs_raw_improvement_pct = 99.4797568083521`
- `optimized_vs_raw_improvement_pct = 99.6303187150875`
- `optimized_vs_unoptimized_improvement_pct = 28.940677966101696`

These main results correspond to `453.634 s`, `2.360 s` and `1.677 s`. With `runs=3`, hinted eStargz has a lower median lazy readiness time than unoptimized eStargz for this workload and benchmark contract.

`runs=1 after advisory fix` is a smoke/regression check only:
- `baseline_median_full_pull_ready_ms = 157743.0`
- `unoptimized_median_lazy_ready_ms = 2550.0`
- `optimized_median_lazy_ready_ms = 1175.0`
- `unoptimized_vs_raw_improvement_pct = 98.38344649207889`
- `optimized_vs_raw_improvement_pct = 99.25511750125203`
- `optimized_vs_unoptimized_improvement_pct = 53.92156862745098`

These single-run regression results correspond to `157.743 s`, `2.550 s` and `1.175 s`. They confirm that the main experiment still produces interpretable results after the fix, rather than replacing the `runs=3` conclusions.

Together, the datasets show:

- Lazy eStargz differs substantially from raw full pull in this scenario
- Hinted eStargz further reduces lazy readiness time in this scenario
- These values describe only this workload, image combination and benchmark contract

## Repository map

- `scripts/oneclick_product.py`
  Sole supported CLI entry point
- `orchestrator/stages/`
  Supported stage implementations
- `orchestrator/llm.py`
  Provider resolution and configuration
- `orchestrator/llm_providers/`
  Provider adapters
- `optimizer/tools/build_llm_prompt.py`
  Prompt construction
- `optimizer/tools/prepare_preferred_paths.py`
  Grounding preprocessing
- `optimizer/tools/select_prefetch_paths.py`
  selector
- `scripts/coldstart_bench_strict.sh`
  Benchmark helper called by the supported benchmark stage
- `scripts/apply_hints_to_image.sh`
  Apply helper called by the supported apply stage

## Documentation

- [`docs/ARCHITECTURE.md`](ARCHITECTURE.md)
  Relationships among control plane, stages, manifest, artifacts and reports
- [`docs/USAGE.md`](USAGE.md)
  CLI usage, modes, resume behavior and benchmark/apply invocation rules
- [`docs/EXPERIMENTS.md`](EXPERIMENTS.md)
  Main experiment, metric semantics, results and interpretation scope
- [`docs/IMPLEMENTATION_NOTES.md`](IMPLEMENTATION_NOTES.md)
  Prompt, grounding, selector, quality advisories and benchmark aggregation details
- [`docs/KNOWN_LIMITATIONS.md`](KNOWN_LIMITATIONS.md)
  Current scope, non-goals and limits on generalization

## Current scope at a glance

An image cold-start optimization control plane with `scripts/oneclick_product.py` as its sole supported entry point, file-level observations as its foundation, a deterministic baseline, and evidence from the `ready-serving-layout-trap-v5` triple benchmark.
