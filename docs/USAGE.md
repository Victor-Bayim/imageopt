**English** | [简体中文](USAGE.zh-CN.md)

# Usage

This document covers the supported usage of `scripts/oneclick_product.py`.

## 1. Single entry point

```bash
python3 scripts/oneclick_product.py <subcommand> ...
```

Supported subcommands:

- `preflight`
- `sample`
- `export-enrich`
- `analyze`
- `llm-optimize`
- `apply`
- `publish`
- `benchmark`
- `report`
- `run`

The most common commands are `run` and `report`.

## 2. `work-dir`

All supported stages operate within one `work-dir`.

Without an explicit path, the default directory resembles:

```text
optimizer/work/<timestamp>
```

A `work-dir` normally retains:

- `manifest.json`
- JSON artifacts from each stage
- `hints.yaml`
- `estargz.list`
- `run_report.json`
- `run_report.md`

## 3. Common commands

### Minimal deterministic baseline

```bash
python3 scripts/oneclick_product.py run <IMAGE> \
  --work-dir /tmp/imageopt_run \
  --mode deterministic
```

### Generate a report after execution

```bash
python3 scripts/oneclick_product.py report \
  --work-dir /tmp/imageopt_run
```

### Execute individual stages

```bash
python3 scripts/oneclick_product.py preflight <IMAGE> --work-dir /tmp/imageopt_stage
python3 scripts/oneclick_product.py sample <IMAGE> --work-dir /tmp/imageopt_stage
python3 scripts/oneclick_product.py export-enrich --work-dir /tmp/imageopt_stage
python3 scripts/oneclick_product.py analyze --work-dir /tmp/imageopt_stage
python3 scripts/oneclick_product.py report --work-dir /tmp/imageopt_stage
```

## 4. Parameter precedence for `run`

`run` supports `--config` and `--preset`.
Precedence is fixed:

```text
CLI explicit args > config > preset > built-in defaults
```

Notes:

- `preset` supplies defaults only
- `config` also supplies defaults only
- Neither implicitly enables `--apply` or `--benchmark`
- Explicit CLI arguments always take precedence

## 5. Modes

### `deterministic`

```bash
python3 scripts/oneclick_product.py run <IMAGE> \
  --work-dir /tmp/run_det \
  --mode deterministic
```

Characteristics:

- Skips the LLM stage
- Directly consumes deterministic hints from analyze

### `llm-auto`

```bash
python3 scripts/oneclick_product.py run <IMAGE> \
  --work-dir /tmp/run_auto \
  --mode llm-auto \
  --llm-provider openai
```

Characteristics:

- Requires valid provider configuration
- `run --mode llm-auto` reports an error if the API key is missing
- Retains deterministic hints when LLM output is unusable or grounding leaves no usable paths
- Does not automatically switch to manual web import

### `llm-manual`

```bash
python3 scripts/oneclick_product.py run <IMAGE> \
  --work-dir /tmp/run_manual \
  --mode llm-manual
```

Characteristics:

- Pauses in the manual web flow
- Exports a prompt bundle
- Waits for the user to import a manual response

## 6. `llm-manual` workflow

### Export web input

```bash
python3 scripts/oneclick_product.py run <IMAGE> \
  --work-dir /tmp/run_manual \
  --mode llm-manual
```

Output identifies:

- `web_llm_ready_to_paste.txt`
- `llm_manual_instructions.md`
- `resume_manual_web_llm.sh`

### Import the web response

The contract is JSON-first.
Recommended import command:

```bash
python3 scripts/oneclick_product.py llm-optimize \
  --work-dir /tmp/run_manual \
  --manual-response-file /tmp/run_manual/hints_raw.manual.yaml
```

Notes:

- The default filename remains `hints_raw.manual.yaml`
- Prefer saving strict JSON as its content
- Non-JSON content triggers legacy YAML compatibility parsing

The only allowed top-level structure is:

```json
{"P0":[],"P1":[],"P2":[],"P3":[]}
```

Requirements:

- Only top-level `P0/P1/P2/P3` keys are allowed
- Array elements must be absolute file paths
- Paths must come from the candidate list
- Markdown, explanations, code fences, directories and wildcards are forbidden

## 7. `run` and standalone subcommands

`run` connects these stages automatically:

1. `preflight`
2. `sample`
3. `export-enrich`
4. `analyze`
5. `llm-optimize`, only in `llm-auto` or `llm-manual`
6. `apply`, only with explicit `--apply`
7. `benchmark`, only with explicit `--benchmark`
8. `report`

Standalone subcommands are useful for:

- Debugging one stage
- Rerunning a failed stage
- Continuing manually in an existing `work-dir`

## 8. `sample`

Example:

```bash
python3 scripts/oneclick_product.py sample <IMAGE> \
  --work-dir /tmp/sample_only \
  --workload ready-serving-layout-trap-v5
```

Common parameters:

- `--startup-mode auto|legacy|gated`
- `--startup-backend-preference auto|prefer-host-launcher|prefer-image-shell`
- `--duration`
- `--workload`
- `--inject-io`
- `--ready-url`
- `--ready-exec`
- `--ready-log-regex`
- `--model-host`
- `--model-dst`
- `--model-name`

Container runtime parameters can also be forwarded directly:

- `--container-gpus`
- `--container-ipc`
- `--container-env`
- `--container-mount`
- `--container-entrypoint`
- `--container-cmd`

## 9. `analyze`

Example:

```bash
python3 scripts/oneclick_product.py analyze \
  --work-dir /tmp/sample_only
```

Common parameters:

- `--min-events`
- `--max-mmap-maps-ratio`
- `--size-budget-percent`
- `--size-budget-mb`
- `--min-hits`
- `--p0-max`
- `--early-onehit-ratio`
- `--early-onehit-max`
- `--early-onehit-max-size-kb`
- `--selector-mode file|chunk-shadow`

`--allow-low-fidelity` remains a compatibility switch only.
Low-quality findings now populate advisory fields by default rather than stopping analyze immediately.
`chunk-shadow` is an experimental image-TOC-based shadow comparison; it neither changes file-level observations nor becomes the default path.

## 10. `llm-optimize`

Run the LLM stage directly:

```bash
python3 scripts/oneclick_product.py llm-optimize \
  --work-dir /tmp/run_after_analyze \
  --llm-provider openai
```

Manual web export:

```bash
python3 scripts/oneclick_product.py llm-optimize \
  --work-dir /tmp/run_after_analyze \
  --manual-web
```

Manual web import:

```bash
python3 scripts/oneclick_product.py llm-optimize \
  --work-dir /tmp/run_after_analyze \
  --manual-response-file /tmp/run_after_analyze/hints_raw.manual.yaml
```

If grounding leaves no usable imported paths, the implementation discards the LLM response and retains deterministic hints; this alone does not fail the whole pipeline.

## 11. `apply`

Example:

```bash
python3 scripts/oneclick_product.py apply \
  --work-dir /tmp/run_after_analyze \
  --src-image <IMAGE> \
  --dst-image <OPT_IMAGE>
```

Without `--dst-image`, `run --apply` generates:

- `<src-tag>-optimized`
- Or `:optimized`

Apply success criteria:

- The optimize command succeeds
- The target image artifact exists
- The smoke check can consume it

When these conditions hold, `apply` remains `passed` even with many quality warnings.

dry-run：

```bash
python3 scripts/oneclick_product.py apply \
  --work-dir /tmp/run_after_analyze \
  --src-image <IMAGE> \
  --dry-run
```

Dry-run does not generate a usable optimized image. Its result carries `quality_status=unknown` and recommendations rather than a publish-ready conclusion.

## 12. `benchmark`

### Direct two-group benchmark

```bash
python3 scripts/oneclick_product.py benchmark \
  --work-dir /tmp/bench_two_way \
  --base-image <BASE_IMAGE> \
  --opt-image <OPT_IMAGE>
```

### Main experiment triple benchmark

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

Required combinations:

- `--comparison-mode raw-full-vs-estargz-lazy-triple` requires `--unopt-image`
- `run --benchmark --bench-comparison-mode raw-full-vs-estargz-lazy-triple` requires `--bench-unopt-image`

Common benchmark parameters:

- `--mode struct|ready`
- `--workload`
- `--runs`
- `--snapshotter`
- `--base-snapshotter`
- `--opt-snapshotter`
- `--pull-policy`
- `--cold-mode local-cold|registry-cold-strict`

Benchmark also supports the corresponding container runtime parameters:

- `--container-gpus`
- `--container-ipc`
- `--container-env`
- `--container-mount`
- `--container-entrypoint`
- `--container-cmd`

## 13. `report`

```bash
python3 scripts/oneclick_product.py report \
  --work-dir /tmp/imageopt_run
```

The report collects:

- overall status
- mode / mode result
- startup backend
- Analyze advisory fields
- LLM fallback or grounding status
- Apply consumability
- Benchmark validity and invalid reasons

## 14. Recommended invocation for `ready-serving-layout-trap-v5`

```bash
python3 scripts/oneclick_product.py run <RAW_IMAGE> \
  --work-dir /tmp/layout_trap_v5_run \
  --workload ready-serving-layout-trap-v5 \
  --mode deterministic \
  --apply \
  --benchmark \
  --bench-mode ready \
  --bench-comparison-mode raw-full-vs-estargz-lazy-triple \
  --bench-unopt-image <UNOPT_ESTARGZ_IMAGE>
```

## 15. Presets and workloads

The following presets remain available in `run`:

- `tfpy-half-plus-two-io1g`
- `tf-serving-efficientnet-v2-b0`
- `tf-serving-half-plus-two`

They are collections of CLI defaults only.

Available workloads include:

- `none`
- `ready-serving-bootstrap-v2`
- `ready-serving-layout-trap-v3`
- `ready-serving-layout-trap-v4`
- `ready-serving-layout-trap-v5`
- `tfpy-half-plus-two-io1g`
- `tf-serving-efficientnet-v2-b0`
- `tf-serving-half-plus-two`

## 16. Resume and pause

When `llm-manual` pauses for web input:

- The `llm_optimize` stage in `manifest.json` becomes `paused`
- `resume_inputs.manual_response_file.format` is recorded as `json`
- `resume_command` points to `resume_manual_web_llm.sh` or an equivalent CLI command

When `run` restarts in the same `work-dir`:

- Stages marked `passed` are skipped
- Subsequent `paused` or `failed` stages continue processing

## 17. Files to check first

To check whether a run satisfies the current contract, inspect:

1. `manifest.json`
2. `quality_gate.json`
3. `selector_stats_hints.json`
4. `llm_result.json`
5. `apply_result.json`
6. `benchmark_result.json`
7. `run_report.json`
8. `run_report.md`
