from __future__ import annotations

import json
import math
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from orchestrator.manifest import ManifestStore


DROP_PREFIXES = ("/proc/", "/sys/", "/dev/", "/run/", "/var/run/")


@dataclass
class AnalyzeResult:
    quality_gate_path: Path
    candidates_path: Path
    aggregate_stats_path: Path
    ranked_path: Path
    estargz_list_path: Path
    selector_stats_path: Path
    hints_path: Path
    selector_stats_hint_path: Path
    chunk_shadow_ranked_path: Path | None
    chunk_shadow_estargz_list_path: Path | None
    chunk_shadow_hints_path: Path | None
    chunk_shadow_selector_stats_path: Path | None
    image_toc_path: Path | None
    toc_shadow_path: Path | None
    chunk_selector_shadow_path: Path | None
    stage_log_path: Path
    quality_payload: dict
    warnings: list[str]
    size_budget_mb_effective: int
    filesystem_total_bytes: int
    quality_status: str
    recommendation: str
    blockers: list[str]


def _load_runtime_state(path: Path) -> dict:
    if not path.exists():
        raise RuntimeError(f"runtime state is missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _runtime_root_pid(runtime_state: dict) -> int:
    live_container_pid = int(runtime_state.get("live_container_pid") or 0)
    if live_container_pid > 0:
        return live_container_pid
    return int(runtime_state.get("container_pid") or 0)


def _load_optional_json(path: Path | None) -> dict | None:
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in values:
        text = str(raw or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        ordered.append(text)
    return ordered


def _nonempty_lines(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _ensure_file(path: Path, label: str) -> None:
    if not path.exists():
        raise RuntimeError(f"{label} is missing: {path}")


def _run_tool(argv: list[str], *, repo_root: Path, stage_log_path: Path) -> subprocess.CompletedProcess[str]:
    with stage_log_path.open("a", encoding="utf-8") as log_fh:
        proc = subprocess.run(
            argv,
            cwd=repo_root,
            text=True,
            stdout=log_fh,
            stderr=log_fh,
            check=False,
    )
    return proc


def _manifest_image(work_dir: Path) -> str | None:
    manifest_path = work_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = ManifestStore.load(manifest_path)
    except Exception:
        return None
    image = manifest.data.get("options", {}).get("image") or manifest.data.get("image")
    return str(image) if image else None


def _write_ranked_outputs(*, paths: list[str], ranked_path: Path, list_path: Path, p0_max: int) -> None:
    p0 = paths[: max(int(p0_max), 0)]
    rest = paths[max(int(p0_max), 0) :]
    p1 = rest[0::3]
    p2 = rest[1::3]
    p3 = rest[2::3]
    with ranked_path.open("w", encoding="utf-8") as fh:
        fh.write("P0:\n")
        for path in p0:
            fh.write(f"  - {path}\n")
        fh.write("P1:\n")
        for path in p1:
            fh.write(f"  - {path}\n")
        fh.write("P2:\n")
        for path in p2:
            fh.write(f"  - {path}\n")
        fh.write("P3:\n")
        for path in p3:
            fh.write(f"  - {path}\n")
    list_path.write_text("".join(f"{path}\n" for path in paths), encoding="utf-8")


def _materialize_chunk_shadow_selector(
    *,
    work_dir: Path,
    chunk_selector_shadow_payload: dict,
    p0_max: int,
) -> tuple[Path | None, Path | None, Path | None, Path | None, str | None]:
    shadow = dict(chunk_selector_shadow_payload.get("shadow_alternative") or {})
    selected_rows = [row for row in list(shadow.get("selected_contributions") or []) if isinstance(row, dict)]
    paths = [str(row.get("path") or "").strip() for row in selected_rows if str(row.get("path") or "").strip()]
    if not paths:
        return None, None, None, None, "chunk-shadow selector mode requested but shadow alternative produced no selected paths"
    ranked_path = work_dir / "ranked_chunk_shadow.yaml"
    estargz_list_path = work_dir / "estargz_chunk_shadow.list"
    hints_path = work_dir / "hints_chunk_shadow.yaml"
    selector_stats_path = work_dir / "selector_stats_chunk_shadow.json"
    _write_ranked_outputs(
        paths=paths,
        ranked_path=ranked_path,
        list_path=estargz_list_path,
        p0_max=p0_max,
    )
    shutil.copyfile(ranked_path, hints_path)
    selector_stats = {
        "selector_mode": "chunk-shadow",
        "strategy": shadow.get("strategy"),
        "chosen": len(paths),
        "chosen_preview": selected_rows[:12],
        "chosen_path_sum_compressed_bytes": shadow.get("selected_path_sum_compressed_bytes"),
        "chosen_incremental_compressed_bytes": shadow.get("selected_incremental_compressed_bytes"),
        "chosen_overlap_compressed_bytes": shadow.get("selected_overlap_compressed_bytes"),
        "chosen_overlap_compressed_ratio": shadow.get("selected_overlap_compressed_ratio"),
        "candidate_pool": shadow.get("candidate_pool"),
        "shadow_only_paths_preview": list(shadow.get("shadow_only_paths_preview") or []),
        "current_only_paths_preview": list(shadow.get("current_only_paths_preview") or []),
        "replacement_suggestions_preview": list(shadow.get("replacement_suggestions_preview") or []),
        "shadow_incremental_compressed_bytes_delta": chunk_selector_shadow_payload.get(
            "shadow_incremental_compressed_bytes_delta"
        ),
        "shadow_overlap_compressed_bytes_delta": chunk_selector_shadow_payload.get(
            "shadow_overlap_compressed_bytes_delta"
        ),
    }
    selector_stats_path.write_text(
        json.dumps(selector_stats, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return ranked_path, estargz_list_path, hints_path, selector_stats_path, None


def _maybe_materialize_image_toc(*, repo_root: Path, work_dir: Path, stage_log_path: Path) -> tuple[Path | None, str | None]:
    toc_input_path = work_dir / "image_toc.json"
    if toc_input_path.exists():
        return toc_input_path, None
    image = _manifest_image(work_dir)
    if not image:
        return None, None
    export_toc_tool = repo_root / "optimizer" / "tools" / "export_image_toc.py"
    proc = _run_tool(
        [
            "python3",
            str(export_toc_tool),
            "--image",
            image,
            "--out",
            str(toc_input_path),
        ],
        repo_root=repo_root,
        stage_log_path=stage_log_path,
    )
    if proc.returncode == 0 and toc_input_path.exists():
        return toc_input_path, None
    return None, "image TOC export failed; TOC shadow audit skipped"


def _container_root_size_bytes(container_pid: int) -> int:
    if container_pid <= 0 or not os.path.exists(f"/proc/{container_pid}"):
        return 0
    root = f"/proc/{container_pid}/root"
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        rel = dirpath[len(root) :] if dirpath.startswith(root) else dirpath
        if not rel.startswith("/"):
            rel = "/" + rel
        if any(rel.startswith(prefix) for prefix in DROP_PREFIXES):
            dirnames[:] = []
            continue
        dirnames[:] = [
            name
            for name in dirnames
            if not any(f"{rel.rstrip('/')}/{name}".startswith(prefix) for prefix in DROP_PREFIXES)
        ]
        for name in filenames:
            relp = f"{rel.rstrip('/')}/{name}" if rel != "/" else f"/{name}"
            if any(relp.startswith(prefix) for prefix in DROP_PREFIXES):
                continue
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                total += int(st.st_size)
    return total


def _percent_budget_to_mb(total_bytes: int, budget_percent: float) -> int:
    if total_bytes <= 0 or budget_percent <= 0:
        return 0
    value = (float(total_bytes) * float(budget_percent) / 100.0) / 1048576.0
    if 0.0 < value < 1.0:
        return 1
    return int(math.ceil(value))


def _build_quality_advisory(
    *,
    quality_payload: dict,
    selector_stats: dict,
    candidate_count: int,
    selected_paths: list[str],
) -> tuple[str, list[str], str, list[str]]:
    warnings: list[str] = []
    blockers: list[str] = []
    selected_count = len(selected_paths)
    quality_ok = bool(quality_payload.get("ok", True))
    reasons = [str(item).strip() for item in list(quality_payload.get("reasons") or []) if str(item).strip()]
    ratios = dict(quality_payload.get("ratios") or {})
    identity = dict(quality_payload.get("identity") or {})

    if selected_count <= 0:
        blockers.append("analyze did not generate any valid hints")

    if candidate_count > 0 and candidate_count < 4:
        warnings.append(f"candidate pool is small: {candidate_count}")
    if 0 < selected_count < 2:
        warnings.append(f"selected hint count is low: {selected_count}")
    if selected_count > 128:
        warnings.append(f"selected hint count is unusually high: {selected_count}")

    if not quality_ok:
        if reasons:
            warnings.extend(f"quality gate advisory: {reason}" for reason in reasons)
        else:
            warnings.append("quality gate reported low-fidelity sampling quality")

    unique_paths = int(identity.get("unique_paths") or 0)
    devino_ratio = float(ratios.get("aggregate_paths_with_devino_ratio") or 0.0)
    if unique_paths >= 10 and devino_ratio < 0.30:
        warnings.append(f"identity coverage is low: aggregate_paths_with_devino_ratio={devino_ratio:.4f}")

    open_devino_ratio = float(ratios.get("open_with_devino_ratio") or 0.0)
    if open_devino_ratio > 0.0 and open_devino_ratio < 0.60:
        warnings.append(f"OPEN identity coverage is low: open_with_devino_ratio={open_devino_ratio:.4f}")

    mmap_via_maps_ratio = float(ratios.get("mmap_via_maps_ratio") or 0.0)
    if mmap_via_maps_ratio > 0.35:
        warnings.append(f"mmap via=maps ratio is elevated: {mmap_via_maps_ratio:.4f}")

    budget_skipped = int(selector_stats.get("budget_skipped") or 0)
    if budget_skipped > 0:
        warnings.append(f"selector trimmed {budget_skipped} lower-priority path(s) at the budget boundary")

    budget_bytes = int(selector_stats.get("budget_bytes") or 0)
    chosen_bytes = int(selector_stats.get("chosen_bytes") or 0)
    if bool(selector_stats.get("forced_first")) and budget_bytes > 0 and chosen_bytes > budget_bytes:
        warnings.append(
            f"selector kept a forced-first path despite budget pressure (budget_bytes={budget_bytes}, chosen_bytes={chosen_bytes})"
        )

    low_confidence_skipped = int(selector_stats.get("low_confidence_skipped") or 0) + int(
        selector_stats.get("preferred_low_confidence_skipped") or 0
    )
    if low_confidence_skipped > 0:
        warnings.append(f"selector filtered {low_confidence_skipped} low-confidence path(s)")

    early_onehit_chosen = int(selector_stats.get("early_onehit_chosen") or 0)
    if selected_count > 0 and (float(early_onehit_chosen) / float(selected_count)) > 0.40:
        warnings.append(
            f"early-onehit share is high: {early_onehit_chosen}/{selected_count}"
        )

    warnings = _dedupe_strings(warnings)
    if blockers:
        return "poor", warnings, "skip_publish", blockers
    if not warnings:
        return "good", warnings, "proceed", blockers
    if not quality_ok:
        return "poor", warnings, "rerun_sampling", blockers
    if budget_skipped > 0 or (budget_bytes > 0 and chosen_bytes > budget_bytes):
        return "warning", warnings, "review_hints", blockers
    return "warning", warnings, "benchmark_before_publish", blockers


def run_analyze_stage(
    *,
    repo_root: Path,
    work_dir: Path,
    min_events: int = 30,
    max_mmap_maps_ratio: float = 0.50,
    size_budget_percent: float = 6.4,
    size_budget_mb: int = 0,
    min_hits: int = 2,
    p0_max: int = 24,
    early_onehit_ratio: float = 0.12,
    early_onehit_max: int = 8,
    early_onehit_max_size_kb: int = 512,
    selector_mode: str = "file",
    allow_low_fidelity: bool = False,
) -> AnalyzeResult:
    runtime_state = _load_runtime_state(work_dir / "runtime_state.json")
    raw_events_path = Path(runtime_state.get("raw_events") or work_dir / "raw_events.jsonl")
    sampler_log_path = Path(runtime_state.get("sampler_log") or work_dir / "sampler.log")
    sampler_metrics_path = Path(runtime_state.get("sampler_metrics") or work_dir / "sampler_metrics.prom")
    export_stats_path = work_dir / "export_events_stats.json"
    events_path = work_dir / "events.jsonl"
    events_enriched_path = work_dir / "events_enriched.jsonl"

    _ensure_file(raw_events_path, "raw events")
    _ensure_file(events_path, "exported events")
    _ensure_file(events_enriched_path, "enriched events")
    _ensure_file(sampler_log_path, "sampler log")
    _ensure_file(sampler_metrics_path, "sampler metrics")

    stage_log_path = work_dir / "analyze.log"
    stage_log_path.write_text("", encoding="utf-8")

    candidates_path = work_dir / "candidates.json"
    aggregate_stats_path = work_dir / "aggregate_stats.json"
    quality_gate_path = work_dir / "quality_gate.json"
    ranked_path = work_dir / "ranked.yaml"
    estargz_list_path = work_dir / "estargz.list"
    selector_stats_path = work_dir / "selector_stats_default.json"
    hints_path = work_dir / "hints.yaml"
    selector_stats_hint_path = work_dir / "selector_stats_hints.json"
    chunk_shadow_ranked_path = work_dir / "ranked_chunk_shadow.yaml"
    chunk_shadow_estargz_list_path = work_dir / "estargz_chunk_shadow.list"
    chunk_shadow_hints_path = work_dir / "hints_chunk_shadow.yaml"
    chunk_shadow_selector_stats_path = work_dir / "selector_stats_chunk_shadow.json"
    toc_shadow_path = work_dir / "toc_shadow.json"
    chunk_selector_shadow_path = work_dir / "chunk_selector_shadow.json"

    aggregate_tool = repo_root / "optimizer" / "aggregate_candidates.py"
    quality_gate_tool = repo_root / "optimizer" / "tools" / "quality_gate.py"
    select_tool = repo_root / "optimizer" / "tools" / "select_prefetch_paths.py"
    toc_shadow_tool = repo_root / "optimizer" / "tools" / "build_toc_shadow.py"
    chunk_selector_shadow_tool = repo_root / "optimizer" / "tools" / "build_chunk_selector_shadow.py"

    proc = _run_tool(
        [
            "python3",
            str(aggregate_tool),
            str(events_enriched_path),
            str(candidates_path),
            "--stats-json",
            str(aggregate_stats_path),
        ],
        repo_root=repo_root,
        stage_log_path=stage_log_path,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"aggregate stage failed, see {stage_log_path}")

    startup_mode_used = str(
        runtime_state.get("startup_mode_used")
        or runtime_state.get("startup_mode_requested")
        or "legacy"
    )
    startup_backend = str(runtime_state.get("startup_backend") or "unknown")
    event_sink_used = str(runtime_state.get("event_sink") or "file_sink")
    proc = _run_tool(
        [
            "python3",
            str(quality_gate_tool),
            "--raw",
            str(raw_events_path),
            "--events",
            str(events_path),
            "--enriched",
            str(events_enriched_path),
            "--metrics",
            str(sampler_metrics_path),
            "--sampler-log",
            str(sampler_log_path),
            "--out",
            str(quality_gate_path),
            "--min-events",
            str(min_events),
            "--max-mmap-maps-ratio",
            str(max_mmap_maps_ratio),
            "--run-id",
            work_dir.name,
            "--startup-mode",
            startup_mode_used,
            "--startup-backend",
            startup_backend,
            "--self-starting-service",
            "1" if bool(runtime_state.get("self_starting_service")) else "0",
            "--event-sink-used",
            event_sink_used,
            "--redis-enabled",
            "0",
            "--export-stats",
            str(export_stats_path),
            "--aggregate-stats",
            str(aggregate_stats_path),
        ],
        repo_root=repo_root,
        stage_log_path=stage_log_path,
    )
    quality_payload = {}
    if quality_gate_path.exists():
        quality_payload = json.loads(quality_gate_path.read_text(encoding="utf-8"))
    if proc.returncode != 0 and not quality_payload:
        raise RuntimeError(f"quality gate failed without a readable report, see {stage_log_path}")

    warnings: list[str] = []

    filesystem_total_bytes = 0
    effective_size_budget_mb = int(size_budget_mb or 0)
    if effective_size_budget_mb <= 0:
        filesystem_total_bytes = _container_root_size_bytes(_runtime_root_pid(runtime_state))
        converted = _percent_budget_to_mb(filesystem_total_bytes, size_budget_percent)
        if converted > 0:
            effective_size_budget_mb = converted
        else:
            warnings.append("filesystem root size unavailable; selector will fall back to candidate-percent budget")

    proc = _run_tool(
        [
            "python3",
            str(select_tool),
            "--candidates",
            str(candidates_path),
            "--aggregate-stats",
            str(aggregate_stats_path),
            "--out-yaml",
            str(ranked_path),
            "--out-list",
            str(estargz_list_path),
            "--stats-json",
            str(selector_stats_path),
            "--budget-percent",
            str(size_budget_percent),
            "--budget-mb",
            str(effective_size_budget_mb),
            "--min-hits",
            str(min_hits),
            "--p0-max",
            str(p0_max),
            "--early-onehit-ratio",
            str(early_onehit_ratio),
            "--early-onehit-max",
            str(early_onehit_max),
            "--early-onehit-max-size-kb",
            str(early_onehit_max_size_kb),
        ],
        repo_root=repo_root,
        stage_log_path=stage_log_path,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"selector stage failed, see {stage_log_path}")

    shutil.copyfile(ranked_path, hints_path)
    shutil.copyfile(selector_stats_path, selector_stats_hint_path)
    selector_stats = _load_optional_json(selector_stats_path) or {}
    selected_paths = _nonempty_lines(estargz_list_path)
    candidate_count = 0
    try:
        candidate_count = len(list(_load_json(candidates_path) or []))
    except Exception:
        candidate_count = 0
    quality_status, quality_warnings, recommendation, blockers = _build_quality_advisory(
        quality_payload=quality_payload,
        selector_stats=selector_stats,
        candidate_count=candidate_count,
        selected_paths=selected_paths,
    )
    quality_payload["quality_status"] = quality_status
    quality_payload["warnings"] = _dedupe_strings(list(quality_payload.get("warnings") or []) + quality_warnings)
    quality_payload["recommendation"] = recommendation
    quality_payload["blockers"] = list(blockers)
    quality_gate_path.write_text(
        json.dumps(quality_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    selector_stats_hint_payload = _load_optional_json(selector_stats_hint_path) or {}
    selector_stats_hint_payload["quality_status"] = quality_status
    selector_stats_hint_payload["recommendation"] = recommendation
    selector_stats_hint_payload["blockers"] = list(blockers)
    selector_stats_hint_path.write_text(
        json.dumps(selector_stats_hint_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if blockers:
        raise RuntimeError("; ".join(blockers))

    toc_input_path, toc_warning = _maybe_materialize_image_toc(
        repo_root=repo_root,
        work_dir=work_dir,
        stage_log_path=stage_log_path,
    )
    if toc_warning:
        warnings.append(toc_warning)
    image_toc_payload = _load_optional_json(toc_input_path)
    if image_toc_payload is not None:
        image_toc_warnings = list(image_toc_payload.get("warnings") or [])
        if image_toc_warnings:
            warnings.append(f"image TOC contains {len(image_toc_warnings)} warning(s)")
        layers = [row for row in list(image_toc_payload.get("layers") or []) if isinstance(row, dict)]
        unverified_layers = sum(1 for row in layers if row.get("toc_digest_verified") is False)
        if unverified_layers > 0:
            warnings.append(f"image TOC contains {unverified_layers} layer(s) with unverified TOC digest")

    toc_shadow_result: Path | None = None
    chunk_selector_shadow_result: Path | None = None
    chunk_shadow_ranked_result: Path | None = None
    chunk_shadow_estargz_result: Path | None = None
    chunk_shadow_hints_result: Path | None = None
    chunk_shadow_selector_stats_result: Path | None = None
    if toc_input_path is not None and toc_input_path.exists():
        proc = _run_tool(
            [
                "python3",
                str(toc_shadow_tool),
                "--toc-json",
                str(toc_input_path),
                "--candidates",
                str(candidates_path),
                "--selected-list",
                str(estargz_list_path),
                "--out",
                str(toc_shadow_path),
            ],
            repo_root=repo_root,
            stage_log_path=stage_log_path,
        )
        if proc.returncode == 0 and toc_shadow_path.exists():
            toc_shadow_result = toc_shadow_path
            toc_shadow_payload = _load_optional_json(toc_shadow_path) or {}
            selected_missing = int(toc_shadow_payload.get("selected_paths_missing") or 0)
            if selected_missing > 0:
                selected_total = int(toc_shadow_payload.get("selected_paths_total") or 0)
                warnings.append(
                    f"TOC shadow could not map {selected_missing}/{selected_total} selected path(s) into image TOC"
                )
            overlap_ratio = float(toc_shadow_payload.get("selected_overlap_ratio") or 0.0)
            if (
                int(toc_shadow_payload.get("selected_paths_found") or 0) >= 2
                and overlap_ratio >= 0.25
            ):
                warnings.append(f"TOC shadow reports high chunk overlap across selected paths ({overlap_ratio:.2%})")
        else:
            warnings.append("TOC shadow audit failed; see analyze.log")

        proc = _run_tool(
            [
                "python3",
                str(chunk_selector_shadow_tool),
                "--toc-json",
                str(toc_input_path),
                "--candidates",
                str(candidates_path),
                "--selected-list",
                str(estargz_list_path),
                "--out",
                str(chunk_selector_shadow_path),
            ],
            repo_root=repo_root,
            stage_log_path=stage_log_path,
        )
        if proc.returncode == 0 and chunk_selector_shadow_path.exists():
            chunk_selector_shadow_result = chunk_selector_shadow_path
            chunk_selector_shadow_payload = _load_optional_json(chunk_selector_shadow_path) or {}
            overlap_heavy = int(chunk_selector_shadow_payload.get("selected_overlap_heavy_paths") or 0)
            if overlap_heavy > 0:
                warnings.append(f"chunk selector shadow found {overlap_heavy} overlap-heavy selected path(s)")
            zero_incremental = int(chunk_selector_shadow_payload.get("selected_zero_incremental_paths") or 0)
            if zero_incremental > 0:
                warnings.append(f"chunk selector shadow found {zero_incremental} zero-incremental selected path(s)")
            shadow_delta = int(chunk_selector_shadow_payload.get("shadow_incremental_compressed_bytes_delta") or 0)
            if shadow_delta > 0:
                warnings.append(
                    f"chunk selector shadow could recover {shadow_delta} more compressed TOC bytes under the same path-sum budget"
                )
            replacements = list(chunk_selector_shadow_payload.get("replacement_suggestions_preview") or [])
            if replacements:
                top = replacements[0]
                warnings.append(
                    "chunk selector shadow suggests replacing "
                    f"{top.get('replace_path')} with {top.get('add_path')} "
                    f"(+{top.get('delta_incremental_compressed_bytes')} incremental compressed TOC bytes)"
                )
            if selector_mode == "chunk-shadow":
                (
                    chunk_shadow_ranked_result,
                    chunk_shadow_estargz_result,
                    chunk_shadow_hints_result,
                    chunk_shadow_selector_stats_result,
                    chunk_shadow_warning,
                ) = _materialize_chunk_shadow_selector(
                    work_dir=work_dir,
                    chunk_selector_shadow_payload=chunk_selector_shadow_payload,
                    p0_max=p0_max,
                )
                if chunk_shadow_warning:
                    warnings.append(chunk_shadow_warning)
                else:
                    warnings.append("experimental chunk-shadow selector artifacts generated")
        else:
            warnings.append("chunk selector shadow audit failed; see analyze.log")

    return AnalyzeResult(
        quality_gate_path=quality_gate_path,
        candidates_path=candidates_path,
        aggregate_stats_path=aggregate_stats_path,
        ranked_path=ranked_path,
        estargz_list_path=estargz_list_path,
        selector_stats_path=selector_stats_path,
        hints_path=hints_path,
        selector_stats_hint_path=selector_stats_hint_path,
        chunk_shadow_ranked_path=chunk_shadow_ranked_result,
        chunk_shadow_estargz_list_path=chunk_shadow_estargz_result,
        chunk_shadow_hints_path=chunk_shadow_hints_result,
        chunk_shadow_selector_stats_path=chunk_shadow_selector_stats_result,
        image_toc_path=toc_input_path if toc_input_path is not None and toc_input_path.exists() else None,
        toc_shadow_path=toc_shadow_result,
        chunk_selector_shadow_path=chunk_selector_shadow_result,
        stage_log_path=stage_log_path,
        quality_payload=quality_payload,
        warnings=warnings,
        size_budget_mb_effective=effective_size_budget_mb,
        filesystem_total_bytes=filesystem_total_bytes,
        quality_status=quality_status,
        recommendation=recommendation,
        blockers=blockers,
    )
