**English** | [简体中文](ARCHITECTURE.zh-CN.md)

# Architecture

This document describes the control-plane structure supported by the current source and tests.
Historical documents under `docs/archive/` are archived and are not the current reference.

## 1. Single entry point

The sole supported entry point is:

- `scripts/oneclick_product.py`

It handles:

- Parsing subcommands and run modes
- Managing `work-dir`
- Maintaining `manifest.json`
- Consistent stage states across failure, pause and resume
- Connecting sample/analyze/llm/apply/benchmark/report

Helper scripts remain internal stage implementation details:

- `scripts/apply_hints_to_image.sh`
- `scripts/coldstart_bench_strict.sh`

## 2. Control-plane structure

Top-level responsibilities:

- `scripts/oneclick_product.py`
  CLI, run pipeline, resume behavior and stage completion
- `orchestrator/stages/`
  Supported stage implementations
- `orchestrator/manifest.py`
  Reading/writing `manifest.json` and defining state conventions
- `orchestrator/stages/report.py`
  Reading stage artifacts and collecting them in `run_report.json` / `run_report.md`
- `optimizer/tools/`
  Tools for analyze and LLM processing

## 3. Stage graph

Stage dependencies:

```text
preflight
  -> sample
  -> export-enrich
  -> analyze
  -> llm-optimize   (only for llm-auto / llm-manual)
  -> apply          (optional)
  -> benchmark      (optional)
  -> report
```

The `run` command reuses stages already marked `passed` in the same `work-dir`.

## 4. Stage responsibilities

### preflight

Responsibilities:

- Check the runtime environment
- Check whether the image and startup mode are supported
- Write `env_check.json` and `product_policy.json`

Key source files:

- `orchestrator/stages/preflight.py`
- `orchestrator/product_policy.py`

The product policy requires:

- A single Linux host
- `containerd + nerdctl`
- `root` or passwordless `sudo`
- File sink as the default source of truth

### sample

Responsibilities:

- Start the container
- Start the sampler
- Wait for readiness
- Capture raw file-access events
- Write runtime diagnostics

Main artifacts:

- `runtime_state.json`
- `raw_events.jsonl`
- `sampler.log`
- `sampler_metrics.prom`

Examples of hard failures:

- Container startup failure
- Sampler startup failure
- Readiness never achieved
- No usable sample output

### export-enrich

Responsibilities:

- Convert raw file-sink events into structured events
- Enrich OPEN/MMAP events with additional metadata
- Produce the enriched view required by analyze

Main artifacts:

- `events.jsonl`
- `events_enriched.jsonl`
- `cwd_snapshot.json`
- `maps_paths.txt`
- `mapfiles_devino.json`
- `devino_dict.json`

### analyze

Responsibilities:

- Aggregate candidate paths
- Run quality audits
- Execute the deterministic selector
- Generate `hints.yaml` and `estargz.list`
- Generate a chunk-aware audit view of selected file paths when an image TOC is available

Main artifacts:

- `candidates.json`
- `aggregate_stats.json`
- `quality_gate.json`
- `ranked.yaml`
- `hints.yaml`
- `estargz.list`
- `selector_stats_default.json`
- `selector_stats_hints.json`

Optional chunk-aware audit artifacts:

- `image_toc.json`
- `toc_shadow.json`
- `chunk_selector_shadow.json`
- `ranked_chunk_shadow.yaml`
- `hints_chunk_shadow.yaml`
- `estargz_chunk_shadow.list`
- `selector_stats_chunk_shadow.json`

Scope:

- Sampling observations remain file-level events
- The image TOC is used only for post-processing audits, explanatory views and experimental shadow comparisons of selected file paths

### llm-optimize

Responsibilities:

- Construct a prompt on top of the deterministic baseline
- Handle `llm-auto` and `llm-manual`
- Validate the LLM response structure, ground paths and select again
- Keep deterministic results if the LLM output is unusable or grounding leaves no usable paths

Main artifacts:

- `prompt.txt`
- `web_llm_ready_to_paste.txt`
- `body.json`
- `llm_response.json`
- `hints_raw.yaml`
- `preferred_paths.txt`
- `llm_grounding_audit.json`
- `llm_result.json`
- `llm_manual_instructions.md`
- `import_manual_web_llm.sh`

Pausing manual mode also produces:

- `resume_manual_web_llm.sh`

### apply

Responsibilities:

- Read the current `hints.yaml` / `estargz.list`
- Run the apply helper
- Check that the target image was generated
- Validate that the output is consumable
- Produce structured apply results

Main artifacts:

- `env_check_apply.json`
- `apply_policy.json`
- `apply.log`
- `apply_result.json`
- `optimized_image.txt`

The `chunk-shadow` mode generates parallel `_chunk_shadow` artifacts for experimental image-TOC-based comparisons without changing the default path.

### benchmark

Responsibilities:

- Call the benchmark helper
- Aggregate baseline/optimized results, or all three groups in a triple comparison
- Determine benchmark validity using valid sample counts and the lazy full-pull guard

Main artifacts:

- `env_check_benchmark_base.json`
- `env_check_benchmark_opt.json`
- `benchmark_policy.json`
- `coldstart_compare.csv`
- `benchmark_result.json`

### report

Responsibilities:

- Read the manifest and stage artifacts
- Infer the current mode and mode result
- Collect startup, quality, LLM, apply and benchmark information
- Produce the final report

Main artifacts:

- `run_report.json`
- `run_report.md`

## 5. Modes

Three modes are available:

- `deterministic`
- `llm-auto`
- `llm-manual`

Control-plane constraints:

- `deterministic` skips the LLM stage
- `llm-auto` must use the provider API
- `llm-manual` supports pausing and importing a response later
- Every LLM mode builds on the deterministic baseline

## 6. LLM contract and data flow

LLM data flow:

1. Analyze generates deterministic `hints.yaml` first
2. `build_llm_prompt.py` reads `candidates.json` and `selector_stats_default.json`
3. The prompt requires strict JSON output
4. The LLM output undergoes structural validation
5. Grounding removes invalid, non-candidate, noisy, nonexistent and duplicate paths
6. Usable grounded paths proceed to selection
7. If grounding leaves no usable paths, discard the LLM output and fall back to deterministic results

Public contract:

```json
{"P0":[],"P1":[],"P2":[],"P3":[]}
```

Key semantics:

- Only four top-level keys are allowed
- Each value is an array of absolute-path strings
- Paths must come from the candidate list
- `legacy YAML` is supported only for import compatibility and is not the preferred contract

## 7. Quality model

Analyze/apply quality issues fall into two categories:

### Advisory

These issues populate structured fields but do not by themselves set a stage to `failed`:

- Few events
- Few candidates
- Low coverage
- Too few or too many hints
- Budget trimming
- Grounding discards some invalid paths but usable paths remain
- Low selector confidence
- A high early-onehit proportion
- An unusual mmap/open evidence structure

Related fields:

- `quality_status`
- `warnings`
- `recommendation`
- `blockers`

### Hard fail

These issues still terminate a stage:

- Failure of a core command
- Missing or unparseable required inputs
- No usable sample output
- Analyze generates no valid hints
- No consumable `hints.yaml` / `estargz.list` can be produced
- The target image is missing or unusable after apply
- No valid benchmark results
- Results invalidated by the lazy full-pull guard

In the LLM stage, empty grounding discards that response and retains deterministic hints. The stage fails only if no consumable hints can ultimately be produced.

## 8. Manifest semantics

`manifest.json` is the main index shared across stages.

It stores:

- `run_id`
- `work_dir`
- `status`
- `options`
- `artifacts`
- `stages`

`ManifestStore` defines these stage states:

- `pending`
- `in_progress`
- `passed`
- `failed`
- `paused`
- `skipped`
- `cancelled`

For the manual web flow:

- Legacy `awaiting_manual_response` is normalized to `paused` on load
- `resume_inputs` records `manual_response_file` by default
- The default format is recorded as `json`

## 9. Report model

`run_report.json` / `run_report.md` collect:

- overall status
- mode / mode result
- startup requested/used/backend
- sample workload / benchmark workload
- Analyze advisory fields
- LLM fallback or grounding results
- Apply consumability and advisory fields
- Benchmark comparison mode, validity and invalid reasons

## 10. Chunk-aware information in the architecture

Chunk-aware functionality comes from TOC audits and the shadow selector in analyze/report when an image TOC is available.

It is used to:

- Estimate the layers/chunks covered by selected paths
- Observe chunk overlap between paths
- Generate parallel `chunk-shadow` artifacts for comparison
- Explain chunk-level path-selection costs in reports

These capabilities do not change the source of sampling observations or become the default path.

## 11. Main experiment in the architecture

The current experiment covers:

- workload：`ready-serving-layout-trap-v5`
- benchmark contract：`raw-full-vs-estargz-lazy-triple`
- Three groups: raw full pull baseline, unoptimized eStargz lazy and hinted eStargz lazy

This experiment is a benchmark/report comparison contract, rather than a new sampling observation model.
