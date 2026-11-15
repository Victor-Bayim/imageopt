from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

from orchestrator.manifest import ManifestStore
from orchestrator.stages.benchmark import derive_benchmark_warnings, summarize_benchmark_rows


@dataclass
class ReportResult:
    report_json_path: Path
    report_md_path: Path
    summary_payload: dict


def _load_json(path: str | Path | None) -> dict | None:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _env_check_status(env_check: dict | None, check_id: str) -> str | None:
    data = env_check or {}
    for item in list(data.get("checks") or []):
        if not isinstance(item, dict):
            continue
        if item.get("id") == check_id:
            status = str(item.get("status") or "").strip()
            return status or None
    return None


def _summarize_preflight(env_check: dict | None, product_policy: dict | None) -> dict:
    policy = product_policy or {}
    image_shell_status = _env_check_status(env_check, "image_gated_startup_shell")
    host_launcher_status = _env_check_status(env_check, "host_gated_startup_launcher")
    host_launcher_static_status = _env_check_status(env_check, "host_gated_startup_launcher_static")
    host_launcher_dynamic_status = _env_check_status(env_check, "host_gated_startup_launcher_dynamic")
    gated_startup_available = any(
        status == "pass"
        for status in (
            image_shell_status,
            host_launcher_status,
            host_launcher_static_status,
            host_launcher_dynamic_status,
        )
    )
    return {
        "ok": policy.get("ok"),
        "startup_mode": policy.get("startup_mode"),
        "startup_backend_preference": policy.get("startup_backend_preference"),
        "env_check_exit_code": policy.get("env_check_exit_code"),
        "image_gated_startup_shell_status": image_shell_status,
        "host_gated_startup_launcher_status": host_launcher_status,
        "host_gated_startup_launcher_static_status": host_launcher_static_status,
        "host_gated_startup_launcher_dynamic_status": host_launcher_dynamic_status,
        "gated_startup_available": gated_startup_available,
        "warnings": list(policy.get("warnings") or []),
        "errors": list(policy.get("errors") or []),
    }


def _summarize_image_toc(payload: dict | None) -> dict:
    data = payload or {}
    layers = [row for row in list(data.get("layers") or []) if isinstance(row, dict)]
    inline_layers = sum(1 for row in layers if row.get("toc_source") == "inline")
    external_layers = sum(1 for row in layers if row.get("toc_source") == "external")
    verified_layers = sum(1 for row in layers if row.get("toc_digest_verified") is True)
    unverified_layers = sum(1 for row in layers if row.get("toc_digest_verified") is False)
    warnings = list(data.get("warnings") or [])
    return {
        "layers_total": len(layers),
        "entries_total": len(list(data.get("entries") or [])),
        "inline_layers": inline_layers,
        "external_layers": external_layers,
        "verified_layers": verified_layers,
        "unverified_layers": unverified_layers,
        "warnings": warnings,
    }


def _summarize_chunk_selector_shadow(payload: dict | None) -> dict:
    data = payload or {}
    shadow_alternative = dict(data.get("shadow_alternative") or {})
    return {
        "selected_paths_total": data.get("selected_paths_total"),
        "selected_paths_found": data.get("selected_paths_found"),
        "selected_paths_missing": data.get("selected_paths_missing"),
        "selected_path_sum_compressed_bytes": data.get("selected_path_sum_compressed_bytes"),
        "selected_incremental_compressed_bytes": data.get("selected_incremental_compressed_bytes"),
        "selected_overlap_compressed_bytes": data.get("selected_overlap_compressed_bytes"),
        "selected_overlap_compressed_ratio": data.get("selected_overlap_compressed_ratio"),
        "selected_zero_incremental_paths": data.get("selected_zero_incremental_paths"),
        "selected_overlap_heavy_paths": data.get("selected_overlap_heavy_paths"),
        "selected_missing_paths_preview": list(data.get("selected_missing_paths_preview") or []),
        "zero_incremental_paths_preview": list(data.get("zero_incremental_paths_preview") or []),
        "overlap_heavy_paths_preview": list(data.get("overlap_heavy_paths_preview") or []),
        "replacement_suggestions_preview": list(data.get("replacement_suggestions_preview") or []),
        "shadow_incremental_compressed_bytes_delta": data.get("shadow_incremental_compressed_bytes_delta"),
        "shadow_overlap_compressed_bytes_delta": data.get("shadow_overlap_compressed_bytes_delta"),
        "shadow_alternative": {
            "strategy": shadow_alternative.get("strategy"),
            "budget_path_sum_compressed_bytes": shadow_alternative.get("budget_path_sum_compressed_bytes"),
            "candidate_pool": shadow_alternative.get("candidate_pool"),
            "selected_paths_total": shadow_alternative.get("selected_paths_total"),
            "selected_path_sum_compressed_bytes": shadow_alternative.get("selected_path_sum_compressed_bytes"),
            "selected_incremental_compressed_bytes": shadow_alternative.get("selected_incremental_compressed_bytes"),
            "selected_overlap_compressed_bytes": shadow_alternative.get("selected_overlap_compressed_bytes"),
            "selected_overlap_compressed_ratio": shadow_alternative.get("selected_overlap_compressed_ratio"),
            "shadow_only_paths_preview": list(shadow_alternative.get("shadow_only_paths_preview") or []),
            "current_only_paths_preview": list(shadow_alternative.get("current_only_paths_preview") or []),
            "replacement_suggestions_preview": list(shadow_alternative.get("replacement_suggestions_preview") or []),
            "selected_preview": list(shadow_alternative.get("selected_preview") or []),
        },
        "warnings": list(data.get("warnings") or []),
    }


def _summarize_experimental_selector(payload: dict | None) -> dict:
    data = payload or {}
    return {
        "selector_mode": data.get("selector_mode"),
        "strategy": data.get("strategy"),
        "chosen": data.get("chosen"),
        "chosen_path_sum_compressed_bytes": data.get("chosen_path_sum_compressed_bytes"),
        "chosen_incremental_compressed_bytes": data.get("chosen_incremental_compressed_bytes"),
        "chosen_overlap_compressed_bytes": data.get("chosen_overlap_compressed_bytes"),
        "chosen_overlap_compressed_ratio": data.get("chosen_overlap_compressed_ratio"),
        "candidate_pool": data.get("candidate_pool"),
        "shadow_incremental_compressed_bytes_delta": data.get("shadow_incremental_compressed_bytes_delta"),
        "shadow_overlap_compressed_bytes_delta": data.get("shadow_overlap_compressed_bytes_delta"),
        "shadow_only_paths_preview": list(data.get("shadow_only_paths_preview") or []),
        "current_only_paths_preview": list(data.get("current_only_paths_preview") or []),
        "replacement_suggestions_preview": list(data.get("replacement_suggestions_preview") or []),
    }


def _load_benchmark_summary(
    *,
    work_dir: Path,
    artifacts: dict[str, str],
    benchmark_result: dict | None,
) -> dict:
    summary = dict((benchmark_result or {}).get("summary") or {})
    needs_backfill = (
        not summary
        or "valid" not in summary
        or "invalid_reasons" not in summary
        or not summary.get("labels")
        or not summary.get("comparison")
    )
    if not needs_backfill:
        return summary

    csv_candidates = [
        (benchmark_result or {}).get("csv"),
        artifacts.get("benchmark_csv"),
        str(work_dir / "coldstart_compare.csv"),
    ]
    for candidate in csv_candidates:
        if not candidate:
            continue
        csv_path = Path(candidate)
        if not csv_path.exists():
            continue
        try:
            with csv_path.open("r", encoding="utf-8", newline="") as fh:
                computed = summarize_benchmark_rows(list(csv.DictReader(fh)))
        except Exception:
            continue
        if not summary:
            return computed
        merged = dict(summary)
        merged.setdefault("rows_total", computed.get("rows_total"))
        if not merged.get("labels"):
            merged["labels"] = computed.get("labels") or {}
        else:
            computed_labels = computed.get("labels") or {}
            merged_labels = dict(merged.get("labels") or {})
            for label_name, computed_label in computed_labels.items():
                existing_label = dict(merged_labels.get(label_name) or {})
                for key, value in computed_label.items():
                    if key not in existing_label:
                        existing_label[key] = value
                merged_labels[label_name] = existing_label
            merged["labels"] = merged_labels
        if not merged.get("comparison"):
            merged["comparison"] = computed.get("comparison") or {}
        if "valid" not in merged:
            merged["valid"] = computed.get("valid")
        if "invalid_reasons" not in merged:
            merged["invalid_reasons"] = computed.get("invalid_reasons") or []
        return merged
    return summary


def _benchmark_validity_from_summary(summary: dict) -> tuple[bool, list[str]]:
    if "valid" in summary:
        return bool(summary.get("valid")), list(summary.get("invalid_reasons") or [])
    labels = summary.get("labels") or {}
    baseline_median = (labels.get("baseline") or {}).get("median_ready_ms")
    optimized_median = (labels.get("optimized") or {}).get("median_ready_ms")
    if baseline_median is not None and optimized_median is not None:
        return True, []
    reasons: list[str] = []
    if baseline_median is None:
        reasons.append("baseline lacks median ready_ms")
    if optimized_median is None:
        reasons.append("optimized lacks median ready_ms")
    return False, reasons


def _collect_stage_overview(manifest: ManifestStore) -> list[dict]:
    overview = []
    for name, payload in manifest.data.get("stages", {}).items():
        if name == "publish":
            continue
        status = payload.get("status")
        summary = payload.get("summary")
        if name == "report" and status == "in_progress":
            status = "passed"
            summary = "Unified run report generated"
        error = None if status == "passed" else payload.get("error")
        overview.append(
            {
                "name": name,
                "status": status,
                "summary": summary,
                "warnings": list(payload.get("warnings") or []),
                "error": error,
                "pause_reason": payload.get("pause_reason"),
                "resume_command": payload.get("resume_command"),
            }
        )
    return overview


def _overall_status(stage_overview: list[dict], quality_gate: dict | None) -> str:
    if any(stage.get("status") == "paused" for stage in stage_overview):
        return "paused"
    if any(stage.get("status") in {"failed", "cancelled"} for stage in stage_overview):
        return "failed"
    if quality_gate is not None and not quality_gate.get("ok", True):
        return "degraded"
    if any(stage.get("warnings") for stage in stage_overview):
        return "degraded"
    return "passed"


def _infer_mode(manifest: ManifestStore, llm_result: dict | None, llm_stage: dict | None) -> str:
    options = manifest.data.get("options", {}) or {}
    explicit_mode = str(options.get("mode") or "").strip()
    if explicit_mode in {"deterministic", "llm-auto", "llm-manual"}:
        return explicit_mode

    requested_mode = str((llm_result or {}).get("requested_mode") or "").strip()
    if requested_mode in {"deterministic", "llm-auto", "llm-manual"}:
        return requested_mode

    llm_stage_status = str((llm_stage or {}).get("status") or "").strip()
    if llm_stage_status in {"paused", "awaiting_manual_response"}:
        return "llm-manual"

    llm_mode = str((llm_result or {}).get("mode") or "").strip()
    if llm_mode in {"api", "api_fallback"}:
        return "llm-auto"
    if llm_mode in {"manual_export", "manual_response"}:
        return "llm-manual"

    if options.get("llm_enabled") is True:
        return "llm-auto"
    if options.get("manual_web") is True:
        return "llm-manual"
    return "deterministic"


def _infer_mode_result(mode: str, llm_result: dict | None, llm_stage: dict | None) -> str:
    explicit_result = str((llm_result or {}).get("mode_result") or "").strip()
    if explicit_result:
        return explicit_result

    llm_stage_status = str((llm_stage or {}).get("status") or "").strip()
    if mode == "deterministic":
        return "deterministic"
    if mode == "llm-manual":
        if llm_stage_status in {"paused", "awaiting_manual_response"}:
            return "llm-manual-paused"
        if llm_result or llm_stage_status == "passed":
            return "llm-manual-completed"
        return "llm-manual-pending"
    if (llm_result or {}).get("llm_applied") is True and str((llm_result or {}).get("mode") or "") == "api":
        return "llm-auto-success"
    if llm_result or llm_stage_status in {"passed", "failed"}:
        return "llm-auto-fallback"
    return "llm-auto-pending"


def _resume_state(*, work_dir: Path, llm_stage: dict | None, llm_result: dict | None) -> dict:
    stage_payload = llm_stage or {}
    result_payload = llm_result or {}
    stage_status = str(stage_payload.get("status") or "").strip()
    resume_required = stage_status in {"paused", "awaiting_manual_response"} or bool(
        result_payload.get("resume_required")
    )
    pause_reason = (
        stage_payload.get("pause_reason")
        or result_payload.get("pause_reason")
        or ("awaiting_manual_llm_response" if resume_required else None)
    )
    resume_inputs = dict(stage_payload.get("resume_inputs") or {})
    if not resume_inputs and resume_required:
        resume_inputs = {
            "manual_response_file": {
                "path": str(
                    result_payload.get("manual_response_default_path")
                    or (work_dir / "hints_raw.manual.yaml")
                ),
                "format": str(result_payload.get("manual_response_format") or "json"),
            }
        }
    resume_command = (
        stage_payload.get("resume_command")
        or result_payload.get("resume_command")
    )
    return {
        "resume_required": resume_required,
        "pause_reason": pause_reason,
        "resume_inputs": resume_inputs,
        "resume_command": resume_command,
        "next_step": resume_command if resume_required else None,
    }


def _report_markdown(payload: dict) -> str:
    lines = [
        f"# Run Report: {payload['run_id']}",
        "",
        f"- Overall: `{payload['overall_status']}`",
        f"- Work dir: `{payload['work_dir']}`",
        f"- Image: `{payload['image']}`",
        f"- Mode: `{payload.get('mode')}`",
        f"- Mode result: `{payload.get('mode_result')}`",
        f"- Resume required: `{payload.get('resume_required')}`",
        f"- Startup mode: `{payload['startup'].get('requested')}` -> `{payload['startup'].get('used')}`",
        f"- Startup backend: `{payload['startup'].get('backend')}`",
        f"- Startup backend selection reason: `{payload['startup'].get('backend_selection_reason')}`",
        f"- Sample workload: `{payload['workloads'].get('sample_profile')}`",
        f"- Sample capabilities: `{payload['workloads'].get('sample_effective_capabilities')}`",
        f"- Benchmark workload: `{payload['workloads'].get('benchmark_profile')}`",
        f"- Benchmark capabilities: `{payload['workloads'].get('benchmark_effective_capabilities')}`",
        "",
        "## Stages",
    ]
    for stage in payload["stages"]:
        line = f"- `{stage['name']}`: `{stage['status']}`"
        if stage.get("summary"):
            line += f" - {stage['summary']}"
        lines.append(line)
        for warning in stage.get("warnings") or []:
            lines.append(f"  - warning: {warning}")
        if stage.get("error"):
            lines.append(f"  - error: {stage['error']}")

    lines.extend(
        [
            "",
            "## Mode",
            f"- Current mode: `{payload.get('mode')}`",
            f"- Mode result: `{payload.get('mode_result')}`",
            f"- Resume required: `{payload.get('resume_required')}`",
        ]
    )
    if payload.get("pause_reason"):
        lines.append(f"- Pause reason: `{payload.get('pause_reason')}`")
    resume_inputs = payload.get("resume_inputs") or {}
    manual_response_contract = dict(resume_inputs.get("manual_response_file") or {})
    if manual_response_contract:
        lines.append(f"- Manual response file: `{manual_response_contract.get('path')}`")
        lines.append(f"- Manual response format: `{manual_response_contract.get('format')}`")
    if payload.get("resume_command"):
        lines.append(f"- Resume command: `{payload.get('resume_command')}`")

    preflight = payload.get("preflight") or {}
    if preflight:
        lines.extend(
            [
                "",
                "## Preflight",
                f"- Policy ok: `{preflight.get('ok')}`",
                f"- Env check exit code: `{preflight.get('env_check_exit_code')}`",
                f"- Startup mode: `{preflight.get('startup_mode')}`",
                f"- Startup backend preference: `{preflight.get('startup_backend_preference')}`",
                f"- Image-shell gated backend status: `{preflight.get('image_gated_startup_shell_status')}`",
                f"- Host launcher aggregate status: `{preflight.get('host_gated_startup_launcher_status')}`",
                f"- Host launcher static build status: `{preflight.get('host_gated_startup_launcher_static_status')}`",
                f"- Host launcher dynamic build status: `{preflight.get('host_gated_startup_launcher_dynamic_status')}`",
                f"- Any gated startup backend available: `{preflight.get('gated_startup_available')}`",
            ]
        )
        for error in preflight.get("errors") or []:
            lines.append(f"- Error: `{error}`")
        for warning in preflight.get("warnings") or []:
            lines.append(f"- Warning: `{warning}`")

    sample = payload.get("sample") or {}
    if sample:
        startup_state = sample.get("startup") or {}
        lines.extend(
            [
                "",
                "## Sample",
                f"- Status: `{sample.get('status')}`",
                f"- Startup mode used: `{sample.get('startup_mode_used')}`",
                f"- Startup backend: `{sample.get('startup_backend')}`",
                f"- Startup backend preference: `{startup_state.get('backend_preference')}`",
                f"- Startup backend selection reason: `{startup_state.get('backend_selection_reason')}`",
                f"- Startup fallback applied: `{startup_state.get('fallback_applied')}`",
                f"- Startup fallback reason: `{startup_state.get('fallback_reason')}`",
                f"- Ready observed: `{sample.get('ready_observed')}`",
            ]
        )
        for warning in sample.get("warnings") or []:
            lines.append(f"- Warning: `{warning}`")
        failure = sample.get("failure") or {}
        if failure.get("error"):
            lines.append(f"- Failure: `{failure.get('error')}`")
        if failure.get("category"):
            lines.append(f"- Failure category: `{failure.get('category')}`")
        if failure.get("kind"):
            lines.append(f"- Failure kind: `{failure.get('kind')}`")
        if failure.get("evidence"):
            lines.append(f"- Failure evidence: `{failure.get('evidence')}`")
        recent_logs = str(failure.get("recent_container_logs") or "").strip()
        if recent_logs:
            lines.append(f"- Recent log head: `{recent_logs.splitlines()[0]}`")

    export_payload = payload.get("export_enrich") or {}
    if export_payload:
        lines.extend(
            [
                "",
                "## Export",
                f"- Relative OPEN total: `{export_payload.get('relative_open_total')}`",
                f"- Relative OPEN resolved via legacy missing-dirfd cwd: `{export_payload.get('relative_open_resolved_legacy_missing_dirfd')}`",
                f"- Relative OPEN resolved via explicit AT_FDCWD cwd: `{export_payload.get('relative_open_resolved_at_fdcwd')}`",
                f"- Relative OPEN skipped due to explicit dirfd: `{export_payload.get('relative_open_explicit_dirfd_skipped')}`",
                f"- Relative OPEN unresolved without cwd snapshot: `{export_payload.get('relative_open_unresolved_no_cwd')}`",
                f"- OPEN stat misses: `{export_payload.get('open_stat_fail')}`",
                f"- OPEN stat misses after cwd resolution: `{export_payload.get('open_stat_fail_cwd_resolved')}`",
            ]
        )

    analyze = payload.get("analyze") or {}
    if analyze:
        lines.extend(
            [
                "",
                "## Analyze",
                f"- Quality gate ok: `{analyze.get('quality_gate_ok')}`",
                f"- Quality status: `{analyze.get('quality_status')}`",
                f"- Recommendation: `{analyze.get('recommendation')}`",
                f"- Chosen paths: `{analyze.get('chosen_paths')}`",
                f"- Identity guard: `{analyze.get('identity_guard_mode')}`",
                f"- Dev:ino coverage: `{analyze.get('identity_paths_with_devino_ratio')}`",
                f"- CWD-resolved OPEN paths: `{analyze.get('identity_paths_with_open_resolved_from_cwd')}`",
                f"- Dirfd-ambiguous OPEN paths: `{analyze.get('identity_paths_with_open_cwd_dirfd_ambiguous')}`",
                f"- OPEN stat-backed paths: `{analyze.get('identity_paths_with_open_stat_devino')}`",
                f"- OPEN stat-miss paths: `{analyze.get('identity_paths_with_open_stat_miss')}`",
                f"- MMAP raw-backed paths: `{analyze.get('identity_paths_with_mmap_raw_devino')}`",
                f"- Inject IO enabled: `{analyze.get('inject_io_enabled')}`",
                f"- Predict successes: `{analyze.get('predict_successes')}`",
            ]
        )
        for reason in analyze.get("quality_reasons") or []:
            lines.append(f"- Quality reason: `{reason}`")
        for warning in analyze.get("quality_warnings") or []:
            lines.append(f"- Quality warning: `{warning}`")
        for reason in analyze.get("quality_blockers") or []:
            lines.append(f"- Quality blocker: `{reason}`")

    image_toc = payload.get("image_toc") or {}
    if image_toc:
        lines.extend(
            [
                "",
                "## Image TOC",
                f"- Layers: `{image_toc.get('layers_total')}`",
                f"- Entries: `{image_toc.get('entries_total')}`",
                f"- Inline layers: `{image_toc.get('inline_layers')}`",
                f"- External layers: `{image_toc.get('external_layers')}`",
                f"- TOC digest verified layers: `{image_toc.get('verified_layers')}`",
                f"- TOC digest unverified layers: `{image_toc.get('unverified_layers')}`",
            ]
        )
        for warning in image_toc.get("warnings") or []:
            lines.append(f"- Warning: `{warning}`")

    toc_shadow = payload.get("toc_shadow") or {}
    if toc_shadow:
        lines.extend(
            [
                "",
                "## TOC Shadow",
                f"- TOC entries: `{toc_shadow.get('toc_entry_count')}`",
                f"- Candidate paths found in TOC: `{toc_shadow.get('candidate_paths_found')}` / `{toc_shadow.get('candidate_paths_total')}`",
                f"- Candidate paths missing in TOC: `{toc_shadow.get('candidate_paths_missing')}`",
                f"- Selected paths found in TOC: `{toc_shadow.get('selected_paths_found')}` / `{toc_shadow.get('selected_paths_total')}`",
                f"- Selected paths missing in TOC: `{toc_shadow.get('selected_paths_missing')}`",
                f"- Unique layers covered: `{toc_shadow.get('selected_unique_layer_count')}`",
                f"- Unique chunks covered: `{toc_shadow.get('selected_unique_chunk_count')}`",
                f"- Shared chunks: `{toc_shadow.get('selected_shared_chunk_count')}`",
                f"- Path-sum bytes: `{toc_shadow.get('selected_path_sum_bytes')}`",
                f"- Estimated prefetch bytes: `{toc_shadow.get('selected_estimated_bytes')}`",
                f"- Overlap bytes: `{toc_shadow.get('selected_overlap_bytes')}`",
                f"- Overlap ratio: `{toc_shadow.get('selected_overlap_ratio')}`",
                f"- Path-sum compressed bytes: `{toc_shadow.get('selected_path_sum_compressed_bytes')}`",
                f"- Estimated prefetch compressed bytes: `{toc_shadow.get('selected_estimated_compressed_bytes')}`",
                f"- Overlap compressed bytes: `{toc_shadow.get('selected_overlap_compressed_bytes')}`",
                f"- Overlap compressed ratio: `{toc_shadow.get('selected_overlap_compressed_ratio')}`",
            ]
        )
        for path in toc_shadow.get("selected_missing_paths_preview") or []:
            lines.append(f"- Missing selected path: `{path}`")
        for chunk in toc_shadow.get("shared_chunks_top") or []:
            layer_digest = chunk.get("layer_digest") or ""
            offset = chunk.get("offset")
            size = chunk.get("size")
            path_count = chunk.get("path_count")
            lines.append(
                f"- Shared chunk hotspot: `{layer_digest}@{offset}` size=`{size}` paths=`{path_count}`"
            )
        for warning in toc_shadow.get("warnings") or []:
            lines.append(f"- Warning: `{warning}`")

    chunk_selector_shadow = payload.get("chunk_selector_shadow") or {}
    if chunk_selector_shadow:
        shadow_alternative = chunk_selector_shadow.get("shadow_alternative") or {}
        lines.extend(
            [
                "",
                "## Chunk Selector Shadow",
                f"- Selected paths found in TOC: `{chunk_selector_shadow.get('selected_paths_found')}` / `{chunk_selector_shadow.get('selected_paths_total')}`",
                f"- Selected paths missing in TOC: `{chunk_selector_shadow.get('selected_paths_missing')}`",
                f"- Path-sum compressed bytes: `{chunk_selector_shadow.get('selected_path_sum_compressed_bytes')}`",
                f"- Incremental compressed bytes: `{chunk_selector_shadow.get('selected_incremental_compressed_bytes')}`",
                f"- Overlap compressed bytes: `{chunk_selector_shadow.get('selected_overlap_compressed_bytes')}`",
                f"- Overlap compressed ratio: `{chunk_selector_shadow.get('selected_overlap_compressed_ratio')}`",
                f"- Zero-incremental selected paths: `{chunk_selector_shadow.get('selected_zero_incremental_paths')}`",
                f"- Overlap-heavy selected paths: `{chunk_selector_shadow.get('selected_overlap_heavy_paths')}`",
                f"- Shadow incremental compressed byte delta: `{chunk_selector_shadow.get('shadow_incremental_compressed_bytes_delta')}`",
                f"- Shadow overlap compressed byte delta: `{chunk_selector_shadow.get('shadow_overlap_compressed_bytes_delta')}`",
            ]
        )
        for path in chunk_selector_shadow.get("selected_missing_paths_preview") or []:
            lines.append(f"- Missing selected path: `{path}`")
        for row in chunk_selector_shadow.get("zero_incremental_paths_preview") or []:
            lines.append(
                f"- Zero-incremental selected path: `{row.get('path')}` total_comp=`{row.get('total_compressed_bytes')}`"
            )
        for row in chunk_selector_shadow.get("overlap_heavy_paths_preview") or []:
            lines.append(
                f"- Overlap-heavy selected path: `{row.get('path')}` new_comp=`{row.get('incremental_compressed_bytes')}` total_comp=`{row.get('total_compressed_bytes')}` ratio=`{row.get('incremental_compressed_ratio')}`"
            )
        for row in chunk_selector_shadow.get("replacement_suggestions_preview") or []:
            lines.append(
                f"- Suggested replacement: `{row.get('replace_path')}` -> `{row.get('add_path')}` delta_new_comp=`{row.get('delta_incremental_compressed_bytes')}`"
            )
        if shadow_alternative:
            lines.extend(
                [
                    f"- Shadow strategy: `{shadow_alternative.get('strategy')}`",
                    f"- Shadow selected paths: `{shadow_alternative.get('selected_paths_total')}`",
                    f"- Shadow incremental compressed bytes: `{shadow_alternative.get('selected_incremental_compressed_bytes')}`",
                    f"- Shadow overlap compressed bytes: `{shadow_alternative.get('selected_overlap_compressed_bytes')}`",
                ]
            )
        for path in shadow_alternative.get("shadow_only_paths_preview") or []:
            lines.append(f"- Shadow-only path: `{path}`")
        for path in shadow_alternative.get("current_only_paths_preview") or []:
            lines.append(f"- Current-only path: `{path}`")
        for row in shadow_alternative.get("replacement_suggestions_preview") or []:
            lines.append(
                f"- Shadow replacement pair: `{row.get('replace_path')}` -> `{row.get('add_path')}` delta_new_comp=`{row.get('delta_incremental_compressed_bytes')}`"
            )
        for row in shadow_alternative.get("selected_preview") or []:
            lines.append(
                f"- Shadow selected path: `{row.get('path')}` new_comp=`{row.get('incremental_compressed_bytes')}` total_comp=`{row.get('total_compressed_bytes')}` density=`{row.get('shadow_density')}`"
            )
        for warning in chunk_selector_shadow.get("warnings") or []:
            lines.append(f"- Warning: `{warning}`")

    experimental_selector = payload.get("experimental_selector") or {}
    if experimental_selector:
        lines.extend(
            [
                "",
                "## Experimental Selector",
                f"- Mode: `{experimental_selector.get('selector_mode')}`",
                f"- Strategy: `{experimental_selector.get('strategy')}`",
                f"- Chosen paths: `{experimental_selector.get('chosen')}`",
                f"- Path-sum compressed bytes: `{experimental_selector.get('chosen_path_sum_compressed_bytes')}`",
                f"- Incremental compressed bytes: `{experimental_selector.get('chosen_incremental_compressed_bytes')}`",
                f"- Overlap compressed bytes: `{experimental_selector.get('chosen_overlap_compressed_bytes')}`",
                f"- Overlap compressed ratio: `{experimental_selector.get('chosen_overlap_compressed_ratio')}`",
                f"- Shadow incremental compressed byte delta: `{experimental_selector.get('shadow_incremental_compressed_bytes_delta')}`",
                f"- Shadow overlap compressed byte delta: `{experimental_selector.get('shadow_overlap_compressed_bytes_delta')}`",
            ]
        )
        for path in experimental_selector.get("shadow_only_paths_preview") or []:
            lines.append(f"- Experimental-only path: `{path}`")
        for path in experimental_selector.get("current_only_paths_preview") or []:
            lines.append(f"- Default-only path: `{path}`")
        for row in experimental_selector.get("replacement_suggestions_preview") or []:
            lines.append(
                f"- Experimental replacement: `{row.get('replace_path')}` -> `{row.get('add_path')}` delta_new_comp=`{row.get('delta_incremental_compressed_bytes')}`"
            )

    llm_payload = payload.get("llm") or {}
    if llm_payload:
        lines.extend(
            [
                "",
                "## LLM",
                f"- Requested mode: `{llm_payload.get('requested_mode')}`",
                f"- Mode: `{llm_payload.get('mode')}`",
                f"- Mode result: `{llm_payload.get('mode_result')}`",
                f"- Applied: `{llm_payload.get('llm_applied')}`",
                f"- Final hints source: `{llm_payload.get('final_hints_source')}`",
                f"- Model: `{llm_payload.get('model')}`",
                f"- Preferred count: `{llm_payload.get('preferred_count')}`",
                f"- Fallback kind: `{llm_payload.get('fallback_kind')}`",
                f"- Fallback reason: `{llm_payload.get('fallback_reason')}`",
            ]
        )
        for warning in llm_payload.get("warnings") or []:
            lines.append(f"- Warning: `{warning}`")

    apply = payload.get("apply") or {}
    if apply:
        apply_contract = dict(apply.get("result_contract") or {})
        apply_validation = dict(apply_contract.get("validation_inputs") or {})
        lines.extend(
            [
                "",
                "## Apply",
                f"- Selector mode: `{apply.get('selector_mode')}`",
                f"- Dry run: `{apply.get('dry_run')}`",
                f"- Source image: `{apply.get('src_image')}`",
                f"- Destination image: `{apply.get('dst_image')}`",
                f"- Optimized image written: `{apply.get('optimized_image_written')}`",
                f"- Quality status: `{apply.get('quality_status')}`",
                f"- Recommendation: `{apply.get('recommendation')}`",
            ]
        )
        if apply_contract:
            lines.extend(
                [
                    f"- Result kind: `{apply_contract.get('result_kind')}`",
                    f"- Ready for benchmark: `{apply_validation.get('ready_for_benchmark')}`",
                    f"- Optimized candidate image: `{apply_validation.get('optimized_candidate_image')}`",
                ]
            )
            for warning in apply_validation.get("warnings") or []:
                lines.append(f"- Validation warning: `{warning}`")
            for reason in apply_validation.get("blockers") or []:
                lines.append(f"- Validation blocker: `{reason}`")

    benchmark = payload.get("benchmark") or {}
    if benchmark:
        cmp_payload = benchmark.get("comparison") or {}
        strict_proof = benchmark.get("strict_proof") or {}
        strict_best_effort = strict_proof.get("best_effort") or {}
        lines.extend(
            [
                "",
                "## Benchmark",
                f"- Status: `{benchmark.get('status')}`",
                f"- Valid: `{benchmark.get('valid')}`",
                f"- Cold mode: `{benchmark.get('cold_mode')}`",
                f"- Selector mode: `{benchmark.get('selector_mode')}`",
                f"- Baseline image: `{benchmark.get('base_image')}`",
                f"- Optimized image: `{benchmark.get('opt_image')}`",
                f"- Comparison mode: `{benchmark.get('comparison_mode')}`",
                f"- Baseline snapshotter: `{benchmark.get('base_snapshotter')}`",
                f"- Optimized snapshotter: `{benchmark.get('opt_snapshotter')}`",
                f"- Pull policy: `{benchmark.get('pull_policy')}`",
                f"- Drop local image ref: `{benchmark.get('drop_local_image_ref')}`",
                f"- Ready ms semantics: `{benchmark.get('ready_ms_semantics')}`",
                "- End-to-end ready: `pull + run + ready wait`",
                "- Post-pull ready: `run + ready wait`",
                "- Service bootstrap: `application-internal /debug/state value`",
                f"- Baseline median ready ms: `{benchmark.get('baseline_median_ready_ms')}`",
                f"- Optimized median ready ms: `{benchmark.get('optimized_median_ready_ms')}`",
                f"- Median delta ms: `{cmp_payload.get('median_ready_delta_ms')}`",
                f"- Improvement pct: `{cmp_payload.get('median_ready_improvement_pct')}`",
                f"- Baseline median end-to-end ready ms: `{benchmark.get('baseline_median_end_to_end_ready_ms')}`",
                f"- Optimized median end-to-end ready ms: `{benchmark.get('optimized_median_end_to_end_ready_ms')}`",
                f"- End-to-end improvement pct: `{benchmark.get('end_to_end_improvement_pct')}`",
                f"- Baseline median post-pull ready ms: `{benchmark.get('baseline_median_post_pull_ready_ms')}`",
                f"- Optimized median post-pull ready ms: `{benchmark.get('optimized_median_post_pull_ready_ms')}`",
                f"- Post-pull delta ms: `{cmp_payload.get('median_post_pull_ready_delta_ms')}`",
                f"- Post-pull improvement pct: `{benchmark.get('post_pull_ready_improvement_pct')}`",
                f"- Median service bootstrap delta ms: `{cmp_payload.get('median_service_bootstrap_delta_ms')}`",
            ]
        )
        if strict_proof:
            lines.extend(
                [
                    f"- Strict proof zero: `{strict_proof.get('strict_proof_zero')}`",
                    f"- Strict proof interpretation: `verifiable hard proofs` must be zero; page cache remains `{strict_best_effort.get('page_cache_proof_mode')}`",
                ]
            )
        for warning in benchmark.get("warnings") or []:
            lines.append(f"- Warning: `{warning}`")
        for reason in benchmark.get("invalid_reasons") or []:
            lines.append(f"- Invalid: `{reason}`")

    return "\n".join(lines) + "\n"


def run_report_stage(
    *,
    work_dir: Path,
    manifest: ManifestStore,
) -> ReportResult:
    report_json_path = work_dir / "run_report.json"
    report_md_path = work_dir / "run_report.md"
    artifacts = dict(manifest.data.get("artifacts", {}))
    artifacts["run_report_json"] = str(report_json_path)
    artifacts["run_report_md"] = str(report_md_path)
    env_check_path = Path(artifacts.get("env_check") or (work_dir / "env_check.json"))
    env_check = _load_json(env_check_path)
    if env_check is not None:
        artifacts.setdefault("env_check", str(env_check_path))
    product_policy_path = Path(artifacts.get("product_policy") or (work_dir / "product_policy.json"))
    product_policy = manifest.data.get("product_policy") or _load_json(product_policy_path) or {}
    if product_policy and product_policy_path.exists():
        artifacts.setdefault("product_policy", str(product_policy_path))
    runtime_state = _load_json(artifacts.get("runtime_state") or (work_dir / "runtime_state.json"))
    export_events_stats_path = Path(artifacts.get("export_events_stats") or (work_dir / "export_events_stats.json"))
    export_events_stats = _load_json(export_events_stats_path)
    if export_events_stats is not None:
        artifacts.setdefault("export_events_stats", str(export_events_stats_path))
    image_toc_path = Path(artifacts.get("image_toc") or (work_dir / "image_toc.json"))
    image_toc = _load_json(image_toc_path)
    if image_toc is not None:
        artifacts.setdefault("image_toc", str(image_toc_path))
    toc_shadow_path = Path(artifacts.get("toc_shadow") or (work_dir / "toc_shadow.json"))
    toc_shadow = _load_json(toc_shadow_path)
    if toc_shadow is not None:
        artifacts.setdefault("toc_shadow", str(toc_shadow_path))
    chunk_selector_shadow_path = Path(
        artifacts.get("chunk_selector_shadow") or (work_dir / "chunk_selector_shadow.json")
    )
    chunk_selector_shadow = _load_json(chunk_selector_shadow_path)
    if chunk_selector_shadow is not None:
        artifacts.setdefault("chunk_selector_shadow", str(chunk_selector_shadow_path))
    quality_gate = _load_json(artifacts.get("quality_gate") or (work_dir / "quality_gate.json"))
    export_summary = dict(((quality_gate or {}).get("export") or {}))
    if not export_summary and export_events_stats is not None:
        export_summary = dict(export_events_stats)
    selector_stats = _load_json(artifacts.get("selector_stats_default") or (work_dir / "selector_stats_default.json"))
    selector_stats_chunk_shadow_path = Path(
        artifacts.get("selector_stats_chunk_shadow") or (work_dir / "selector_stats_chunk_shadow.json")
    )
    selector_stats_chunk_shadow = _load_json(selector_stats_chunk_shadow_path)
    if selector_stats_chunk_shadow is not None:
        artifacts.setdefault("selector_stats_chunk_shadow", str(selector_stats_chunk_shadow_path))
    llm_result = _load_json(artifacts.get("llm_result") or (work_dir / "llm_result.json"))
    apply_result_path = Path(
        artifacts.get("apply_result")
        or artifacts.get("apply_result_chunk_shadow")
        or (work_dir / "apply_result.json")
    )
    if (
        not apply_result_path.exists()
        and not artifacts.get("apply_result")
        and artifacts.get("apply_result_chunk_shadow") is None
    ):
        candidate = work_dir / "apply_result_chunk_shadow.json"
        if candidate.exists():
            apply_result_path = candidate
    apply_result = _load_json(apply_result_path)
    if apply_result is not None:
        if apply_result.get("selector_mode") == "chunk-shadow":
            artifacts.setdefault("apply_result_chunk_shadow", str(apply_result_path))
            optimized_image_candidate = artifacts.get("optimized_image_chunk_shadow") or str(
                work_dir / "optimized_image_chunk_shadow.txt"
            )
            if Path(optimized_image_candidate).exists():
                artifacts.setdefault("optimized_image_chunk_shadow", optimized_image_candidate)
        else:
            artifacts.setdefault("apply_result", str(apply_result_path))
            optimized_image_candidate = artifacts.get("optimized_image") or str(work_dir / "optimized_image.txt")
            if Path(optimized_image_candidate).exists():
                artifacts.setdefault("optimized_image", optimized_image_candidate)
    benchmark_result = _load_json(artifacts.get("benchmark_result") or (work_dir / "benchmark_result.json"))
    benchmark_stage = (manifest.data.get("stages", {}) or {}).get("benchmark") or {}
    sample_stage = (manifest.data.get("stages", {}) or {}).get("sample") or {}
    llm_stage = (manifest.data.get("stages", {}) or {}).get("llm_optimize") or {}
    benchmark_summary = _load_benchmark_summary(
        work_dir=work_dir,
        artifacts=artifacts,
        benchmark_result=benchmark_result,
    )

    stages = _collect_stage_overview(manifest)
    mode = _infer_mode(manifest, llm_result, llm_stage)
    mode_result = _infer_mode_result(mode, llm_result, llm_stage)
    resume_state = _resume_state(work_dir=work_dir, llm_stage=llm_stage, llm_result=llm_result)
    llm_summary_payload = dict(llm_result or {})
    if llm_result is not None or llm_stage:
        llm_summary_payload.setdefault("requested_mode", mode if mode != "deterministic" else None)
        llm_summary_payload.setdefault("mode_result", mode_result)
        llm_summary_payload.setdefault("resume_required", resume_state["resume_required"])
        llm_summary_payload.setdefault("pause_reason", resume_state["pause_reason"])
        llm_summary_payload.setdefault("resume_command", resume_state["resume_command"])
    payload = {
        "report_version": 1,
        "run_id": manifest.data.get("run_id"),
        "work_dir": manifest.data.get("work_dir"),
        "image": manifest.data.get("options", {}).get("image") or manifest.data.get("image"),
        "overall_status": _overall_status(stages, quality_gate),
        "mode": mode,
        "mode_result": mode_result,
        "resume_required": resume_state["resume_required"],
        "pause_reason": resume_state["pause_reason"],
        "resume_inputs": resume_state["resume_inputs"],
        "resume_command": resume_state["resume_command"],
        "next_step": resume_state["next_step"],
        "control_plane": manifest.data.get("control_plane") or {},
        "product_policy": product_policy,
        "preflight": _summarize_preflight(env_check, product_policy) if env_check is not None or product_policy else {},
        "startup": {
            "requested": manifest.data.get("options", {}).get("startup_mode"),
            "backend_preference": (
                ((runtime_state or {}).get("startup") or {}).get("backend_preference")
                or manifest.data.get("options", {}).get("startup_backend_preference")
            ),
            "backend_selection_reason": (
                ((runtime_state or {}).get("startup") or {}).get("backend_selection_reason")
                or (runtime_state or {}).get("startup_backend_selection_reason")
            ),
            "used": (runtime_state or {}).get("startup_mode_used"),
            "backend": (
                (runtime_state or {}).get("startup_backend")
                or (quality_gate or {}).get("startup_backend")
                or ((quality_gate or {}).get("startup") or {}).get("backend")
            ),
        },
        "workloads": {
            "sample_profile": (runtime_state or {}).get("workload_profile")
            or manifest.data.get("options", {}).get("sample_workload"),
            "sample_profile_capabilities": list((runtime_state or {}).get("workload_profile_capabilities") or []),
            "sample_effective_capabilities": list((runtime_state or {}).get("workload_effective_capabilities") or []),
            "sample_profile_hooks": dict((runtime_state or {}).get("workload_profile_hooks") or {}),
            "sample_effective_hooks": dict((runtime_state or {}).get("workload_effective_hooks") or {}),
            "benchmark_profile": (
                (benchmark_result or {}).get("workload_profile")
                or manifest.data.get("options", {}).get("benchmark_workload")
                if benchmark_result is not None or benchmark_stage
                else None
            ),
            "benchmark_profile_capabilities": list((benchmark_result or {}).get("workload_profile_capabilities") or []),
            "benchmark_effective_capabilities": list((benchmark_result or {}).get("workload_effective_capabilities") or []),
            "benchmark_profile_hooks": dict((benchmark_result or {}).get("workload_profile_hooks") or {}),
            "benchmark_effective_hooks": dict((benchmark_result or {}).get("workload_effective_hooks") or {}),
        },
        "sample": {
            "status": (runtime_state or {}).get("status") or sample_stage.get("status"),
            "container_name": (runtime_state or {}).get("container_name"),
            "container_pid": (runtime_state or {}).get("container_pid"),
            "startup_mode_used": (runtime_state or {}).get("startup_mode_used"),
            "startup_backend": (runtime_state or {}).get("startup_backend"),
            "startup_backend_preference": (runtime_state or {}).get("startup_backend_preference"),
            "startup_backend_selection_reason": (runtime_state or {}).get("startup_backend_selection_reason"),
            "startup": dict((runtime_state or {}).get("startup") or {}),
            "ready_observed": (runtime_state or {}).get("ready_observed"),
            "warnings": list((runtime_state or {}).get("warnings") or []),
            "failure": dict((runtime_state or {}).get("failure") or {}),
        }
        if runtime_state is not None or sample_stage
        else {},
        "export_enrich": {
            "relative_open_total": export_summary.get("relative_open_total"),
            "relative_open_resolved_total": export_summary.get("relative_open_resolved_total"),
            "relative_open_resolved_legacy_missing_dirfd": (
                export_summary.get("relative_open_resolved_legacy_missing_dirfd")
            ),
            "relative_open_resolved_at_fdcwd": export_summary.get("relative_open_resolved_at_fdcwd"),
            "relative_open_explicit_dirfd_skipped": export_summary.get("relative_open_explicit_dirfd_skipped"),
            "relative_open_unresolved_no_cwd": export_summary.get("relative_open_unresolved_no_cwd"),
            "open_stat_fail": export_summary.get("open_stat_fail"),
            "open_stat_fail_cwd_resolved": export_summary.get("open_stat_fail_cwd_resolved"),
            "stats_present": export_summary.get("stats_present"),
        }
        if export_summary
        else {},
        "stages": stages,
        "analyze": {
            "quality_gate_ok": None if quality_gate is None else bool(quality_gate.get("ok")),
            "quality_status": (quality_gate or {}).get("quality_status"),
            "recommendation": (quality_gate or {}).get("recommendation"),
            "quality_reasons": list((quality_gate or {}).get("reasons") or []),
            "quality_warnings": list((quality_gate or {}).get("warnings") or []),
            "quality_blockers": list((quality_gate or {}).get("blockers") or []),
            "chosen_paths": (selector_stats or {}).get("chosen"),
            "budget_bytes": (selector_stats or {}).get("budget_bytes"),
            "chosen_bytes": (selector_stats or {}).get("chosen_bytes"),
            "identity_guard_mode": (selector_stats or {}).get("identity_guard_mode"),
            "identity_guard_reason": (selector_stats or {}).get("identity_guard_reason"),
            "identity_paths_with_devino_ratio": (selector_stats or {}).get("identity_paths_with_devino_ratio"),
            "identity_paths_with_open_resolved_from_cwd": (
                ((quality_gate or {}).get("identity") or {}).get("paths_with_open_resolved_from_cwd")
            ),
            "identity_paths_with_open_cwd_dirfd_ambiguous": (
                ((quality_gate or {}).get("identity") or {}).get("paths_with_open_cwd_dirfd_ambiguous")
            ),
            "identity_paths_with_open_stat_devino": (
                ((quality_gate or {}).get("identity") or {}).get("paths_with_open_stat_devino")
            ),
            "identity_paths_with_open_stat_miss": (
                ((quality_gate or {}).get("identity") or {}).get("paths_with_open_stat_miss")
            ),
            "identity_paths_with_mmap_raw_devino": (
                ((quality_gate or {}).get("identity") or {}).get("paths_with_mmap_raw_devino")
            ),
            "inject_io_enabled": (runtime_state or {}).get("inject_io_enabled"),
            "predict_successes": ((runtime_state or {}).get("injection") or {}).get("predict_successes"),
        }
        if quality_gate is not None or selector_stats is not None
        else {},
        "image_toc": _summarize_image_toc(image_toc) if image_toc is not None else {},
        "toc_shadow": {
            "toc_entry_count": toc_shadow.get("toc_entry_count"),
            "candidate_paths_total": toc_shadow.get("candidate_paths_total"),
            "candidate_paths_found": toc_shadow.get("candidate_paths_found"),
            "candidate_paths_missing": toc_shadow.get("candidate_paths_missing"),
            "candidate_paths_found_ratio": toc_shadow.get("candidate_paths_found_ratio"),
            "selected_paths_total": toc_shadow.get("selected_paths_total"),
            "selected_paths_found": toc_shadow.get("selected_paths_found"),
            "selected_paths_missing": toc_shadow.get("selected_paths_missing"),
            "selected_paths_found_ratio": toc_shadow.get("selected_paths_found_ratio"),
            "selected_unique_layer_count": toc_shadow.get("selected_unique_layer_count"),
            "selected_unique_chunk_count": toc_shadow.get("selected_unique_chunk_count"),
            "selected_shared_chunk_count": toc_shadow.get("selected_shared_chunk_count"),
            "selected_path_sum_bytes": toc_shadow.get("selected_path_sum_bytes"),
            "selected_path_sum_compressed_bytes": toc_shadow.get("selected_path_sum_compressed_bytes"),
            "selected_estimated_bytes": toc_shadow.get("selected_estimated_bytes"),
            "selected_estimated_compressed_bytes": toc_shadow.get("selected_estimated_compressed_bytes"),
            "selected_shared_chunk_bytes": toc_shadow.get("selected_shared_chunk_bytes"),
            "selected_shared_chunk_compressed_bytes": toc_shadow.get("selected_shared_chunk_compressed_bytes"),
            "selected_overlap_bytes": toc_shadow.get("selected_overlap_bytes"),
            "selected_overlap_compressed_bytes": toc_shadow.get("selected_overlap_compressed_bytes"),
            "selected_overlap_ratio": toc_shadow.get("selected_overlap_ratio"),
            "selected_overlap_compressed_ratio": toc_shadow.get("selected_overlap_compressed_ratio"),
            "selected_missing_paths_preview": list(toc_shadow.get("selected_missing_paths_preview") or []),
            "shared_chunks_top": list(toc_shadow.get("shared_chunks_top") or []),
            "warnings": list(toc_shadow.get("warnings") or []),
        }
        if toc_shadow is not None
        else {},
        "chunk_selector_shadow": _summarize_chunk_selector_shadow(chunk_selector_shadow)
        if chunk_selector_shadow is not None
        else {},
        "experimental_selector": _summarize_experimental_selector(selector_stats_chunk_shadow)
        if selector_stats_chunk_shadow is not None
        else {},
        "llm": llm_summary_payload,
        "apply": apply_result or {},
        "benchmark": {},
        "artifacts": artifacts,
    }
    for stage in payload["stages"]:
        if stage.get("name") == "report" and stage.get("status") == "passed":
            stage["summary"] = f"Unified run report generated ({payload['overall_status']})"
    if benchmark_result is not None:
        labels = benchmark_summary.get("labels") or {}
        benchmark_valid, benchmark_invalid_reasons = _benchmark_validity_from_summary(benchmark_summary)
        if "valid" in (benchmark_result or {}):
            benchmark_valid = bool(benchmark_result.get("valid"))
            benchmark_invalid_reasons = list(benchmark_result.get("invalid_reasons") or benchmark_invalid_reasons)
        benchmark_status = benchmark_result.get("status")
        if not benchmark_status:
            benchmark_status = "passed" if benchmark_valid else "failed"
        benchmark_warnings = list(benchmark_result.get("warnings") or [])
        benchmark_comparison_mode = str(benchmark_result.get("comparison_mode") or "")
        if not benchmark_warnings:
            benchmark_warnings = derive_benchmark_warnings(
                benchmark_summary,
                comparison_mode=benchmark_comparison_mode,
            )
        payload["benchmark"] = {
            "status": benchmark_status,
            "base_image": benchmark_result.get("base_image"),
            "opt_image": benchmark_result.get("opt_image"),
            "cold_mode": benchmark_result.get("cold_mode"),
            "selector_mode": benchmark_result.get("selector_mode"),
            "comparison_mode": benchmark_comparison_mode,
            "ready_ms_semantics": benchmark_result.get("ready_ms_semantics"),
            "base_snapshotter": benchmark_result.get("base_snapshotter"),
            "opt_snapshotter": benchmark_result.get("opt_snapshotter"),
            "pull_policy": benchmark_result.get("pull_policy"),
            "drop_local_image_ref": benchmark_result.get("drop_local_image_ref"),
            "valid": benchmark_valid,
            "invalid_reasons": benchmark_invalid_reasons,
            "warnings": benchmark_warnings,
            "rows_total": benchmark_summary.get("rows_total"),
            "baseline_median_ready_ms": (labels.get("baseline") or {}).get("median_ready_ms"),
            "optimized_median_ready_ms": (labels.get("optimized") or {}).get("median_ready_ms"),
            "baseline_median_post_pull_ready_ms": (labels.get("baseline") or {}).get("median_post_pull_ready_ms"),
            "optimized_median_post_pull_ready_ms": (labels.get("optimized") or {}).get("median_post_pull_ready_ms"),
            "post_pull_ready_improvement_pct": (benchmark_summary.get("comparison") or {}).get(
                "median_post_pull_ready_improvement_pct"
            ),
            "baseline_median_end_to_end_ready_ms": (labels.get("baseline") or {}).get("median_end_to_end_ready_ms"),
            "optimized_median_end_to_end_ready_ms": (labels.get("optimized") or {}).get("median_end_to_end_ready_ms"),
            "end_to_end_improvement_pct": (benchmark_summary.get("comparison") or {}).get(
                "median_end_to_end_improvement_pct"
            ),
            "comparison": benchmark_summary.get("comparison") or {},
            "strict_proof": dict(benchmark_result.get("strict_proof") or {}),
        }
        if payload["benchmark"]["status"] == "failed" and payload["overall_status"] == "passed":
            payload["overall_status"] = "failed"
    for stage in payload["stages"]:
        if stage.get("name") == "report" and stage.get("status") == "passed":
            stage["summary"] = f"Unified run report generated ({payload['overall_status']})"

    report_json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report_md_path.write_text(_report_markdown(payload), encoding="utf-8")
    return ReportResult(
        report_json_path=report_json_path,
        report_md_path=report_md_path,
        summary_payload=payload,
    )
