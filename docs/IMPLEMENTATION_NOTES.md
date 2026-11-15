**English** | [简体中文](IMPLEMENTATION_NOTES.zh-CN.md)

# Implementation Notes

This document records the key decisions in the current implementation.

## 1. Foundations

The implementation follows three principles:

1. Use `scripts/oneclick_product.py` as the single entry point
2. Use deterministic results as the baseline for every LLM flow
3. Record quality issues as structured advisories before considering termination

## 2. Sampling and post-processing scope

The observation pipeline is:

1. `raw_events.jsonl`
2. `events.jsonl`
3. `events_enriched.jsonl`
4. `candidates.json`
5. Selector and LLM post-processing artifacts

The implementation distinguishes two responsibilities:

- Sampling collects file-access observations only
- Chunk-aware logic appears only in post-processing views of selected file paths when an image TOC exists

Consequently:

- The sampler does not directly produce layer/chunk access observations
- Image-TOC processing does not rewrite raw sample observations

## 3. Analyze implementation

`run_analyze_stage()` performs four tasks:

1. Aggregate candidates
2. Run quality audits
3. Run the deterministic selector
4. Generate additional chunk-aware audit artifacts when an image TOC exists

Main tools:

- `optimizer/aggregate_candidates.py`
- `optimizer/tools/quality_gate.py`
- `optimizer/tools/select_prefetch_paths.py`
- `optimizer/tools/export_image_toc.py`
- `optimizer/tools/build_toc_shadow.py`
- `optimizer/tools/build_chunk_selector_shadow.py`

The core analyze outputs remain:

- `hints.yaml`
- `estargz.list`
- `selector_stats_hints.json`

## 4. Quality advisory structure

Analyze and apply use these fields:

```json
{
  "quality_status": "good|warning|poor|unknown",
  "warnings": [],
  "recommendation": "proceed|proceed_with_warning|rerun_sampling|review_hints|benchmark_before_publish|skip_publish",
  "blockers": []
}
```

Implementation constraints:

- `warnings` contains quality risks
- `blockers` contains only issues that actually block execution
- Stage `status` remains `passed` if core artifacts exist and are consumable
- A recommendation is advice and does not automatically replace stage status

## 5. Why low quality no longer immediately fails analyze

Analyze treats these conditions as advisories:

- A small candidate pool
- Too few or too many selected hints
- Quality gate `ok=false` while usable hints remain
- Low identity coverage
- Low OPEN identity coverage
- A high mmap via=maps ratio
- budget boundary trimming
- Budget pressure caused by forced-first selection
- low-confidence filtering
- A high early-onehit share

Only the absence of usable hints becomes a blocker and fails the stage.

## 6. LLM prompt contract

Prompts are constructed in:

- `optimizer/tools/build_llm_prompt.py`
- `orchestrator/stages/llm_optimize.py`

The public contract is JSON-first:

```json
{"P0":[],"P1":[],"P2":[],"P3":[]}
```

The prompt explicitly forbids:

- Markdown
- Code fences
- Explanatory text
- Extra top-level fields
- Non-absolute paths
- Directories
- Wildcards
- Noise paths

Manual and automatic prompts differ as follows:

- Manual prompts may include more background information
- Automatic prompts emphasize strict, conservative JSON-only output

Both use the same final contract.

## 7. Role of `legacy YAML`

`legacy YAML` remains only in the manual-response import compatibility layer.

Implementation:

- `_load_manual_response_payload()` tries JSON first
- YAML compatibility is attempted only after JSON parsing fails
- `_write_manual_instructions()` asks users to save strict JSON first

Therefore:

- The documented contract is JSON-first
- JSON is the preferred contract; YAML is an import compatibility format only

## 8. Grounding and fallback

An LLM response does not directly overwrite the final hints.
The implementation first grounds the response.

Grounding filters:

- Non-candidate paths
- Non-absolute paths
- Non-regular candidates
- Paths skipped by guards
- Paths absent inside the container
- Duplicate paths
- Special noise paths

Behavior:

- If grounded paths remain, continue post-processing and record discards in `llm_grounding_audit.json`
- If grounding leaves no usable paths, discard the LLM response and fall back to deterministic hints

Fallback itself does not fail the pipeline. Failure occurs only when consumable `hints.yaml` / `estargz.list` cannot be produced, execution fails, or artifacts are missing or unusable.

## 9. Implementation differences between `llm-auto` and `llm-manual`

### `llm-auto`

- Construct a provider request
- Read the API response
- Validate the structure
- grounding
- selector
- On success, emit grounded LLM hints
- On failure, retain deterministic hints

### `llm-manual`

- Export `prompt.txt`
- Export the web input bundle
- Set the stage to `paused` during `run`
- Wait for import through `--manual-response-file`
- Repeat structural validation, grounding and selection after import

## 10. Apply success criteria

`run_apply_stage()` requires:

- A successful apply helper exit
- An existing `optimized_image.txt`
- A nonempty target image reference in that file
- Successful smoke consumption

After successful apply, these issues still leave the stage `passed`:

- quality gate warning
- budget trimming
- Grounding discards invalid paths but retains valid results
- selector filtered low-confidence paths
- A small cleaned prefetch list

These issues populate only:

- `quality_status`
- `warnings`
- `recommendation`
- `blockers`

## 11. Apply dry-run semantics

Dry-run does not generate a real optimized image.

Its result contract is:

- `status = passed`
- `result_kind = dry_run`
- `quality_status = unknown`
- `recommendation = review_hints`
- `blockers` explains that no optimized image was actually produced

This is an intentional omission of output, rather than an execution failure.

## 12. Benchmark validity rules

`benchmark_result.json` requires more than the presence of a CSV:

- Sufficient valid samples
- A valid comparison mode
- No invalidation of the lazy path by the full-pull guard

Benchmark fails when:

- All rows fail
- Baseline or optimized groups lack sufficient successful samples
- Any required label in the triple benchmark lacks valid results
- The lazy path performs a full pull, invalidating the comparison

Benchmark `valid=false` is a failure signal, not an advisory.

## 13. Triple benchmark summary

For comparison mode `raw-full-vs-estargz-lazy-triple`, the summary uses:

- `baseline_median_full_pull_ready_ms`
- `unoptimized_median_lazy_ready_ms`
- `optimized_median_lazy_ready_ms`
- `unoptimized_vs_raw_improvement_pct`
- `optimized_vs_raw_improvement_pct`
- `optimized_vs_unoptimized_improvement_pct`

Both reports and CLI summaries interpret results using these three groups.

## 14. Manifest pause/resume behavior

Loading the manifest normalizes legacy `awaiting_manual_response` to `paused`.

It also fills in:

- `pause_reason`
- `resume_inputs.manual_response_file`
- `resume_command`

The manual response format defaults to `json`.

## 15. Report responsibilities

`run_report.json` and `run_report.md` aggregate results rather than create new observations.

They collect distributed artifacts into:

- run overall status
- stage overview
- mode / mode result
- Startup backend selection results
- analyze quality summary
- LLM fallback summary
- apply result summary
- benchmark comparison summary

Reports neither redefine benchmark quality nor promote analyze/apply advisories to hard failures.

## 16. Role of `chunk-shadow`

The `chunk-shadow` selector mode remains an experimental parallel output.

It supports:

- Image-TOC-based experimental shadow comparisons of selected file paths
- Side-by-side inspection against the default file selector
- Explicit use of parallel artifacts during apply/benchmark/publish

It does not replace the default deterministic file selector.
