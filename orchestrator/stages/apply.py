from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from orchestrator.policy import evaluate_apply_policy
from orchestrator.stages.envcheck import run_check_env_json, write_env_check_payload


@dataclass
class ApplyResult:
    env_check_path: Path
    policy_path: Path
    stage_log_path: Path
    apply_result_path: Path
    optimized_image_path: Path | None
    selector_mode: str
    src_image: str
    dst_image: str
    namespace: str
    dry_run: bool
    warnings: list[str]
    quality_status: str
    recommendation: str
    blockers: list[str]


def selector_mode_suffix(selector_mode: str) -> str:
    return "" if selector_mode == "file" else f"_{selector_mode.replace('-', '_')}"


def default_destination_image(src_image: str, *, selector_mode: str = "file") -> str:
    if ":" in src_image:
        dst = f"{src_image}-stargz"
    else:
        dst = f"{src_image}:stargz-optimized"
    if selector_mode != "file":
        dst = f"{dst}-{selector_mode}"
    return dst


def _write_policy(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _load_optional_json(path: Path | None) -> dict | None:
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


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


def _nonempty_lines(path: Path | None) -> list[str]:
    if path is None or not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _apply_quality_assessment(
    *,
    work_dir: Path,
    selector_mode: str,
    dry_run: bool,
    optimized_image_value: str | None,
    policy_warnings: list[str],
) -> tuple[str, list[str], str, list[str]]:
    mode_suffix = selector_mode_suffix(selector_mode)
    warnings = list(policy_warnings)
    blockers: list[str] = []
    quality_gate = _load_optional_json(work_dir / "quality_gate.json") or {}
    selector_stats = _load_optional_json(work_dir / "selector_stats_hints.json") or {}
    grounding_audit = _load_optional_json(work_dir / "llm_grounding_audit.json") or {}
    cleaned_list_path = work_dir / f"estargz{mode_suffix}.list.cleaned"
    cleaned_paths = _nonempty_lines(cleaned_list_path)

    if dry_run:
        blockers.append("apply stage ran in dry-run mode; no optimized image was produced")

    analyze_quality_status = str(quality_gate.get("quality_status") or "").strip()
    analyze_recommendation = str(quality_gate.get("recommendation") or "").strip()
    warnings.extend(str(item).strip() for item in list(quality_gate.get("warnings") or []) if str(item).strip())
    if quality_gate and not quality_gate.get("ok", True):
        reasons = [str(item).strip() for item in list(quality_gate.get("reasons") or []) if str(item).strip()]
        if reasons:
            warnings.extend(f"quality gate advisory: {reason}" for reason in reasons)
        else:
            warnings.append("quality gate reported low-fidelity sampling quality")

    invalid_grounded = int(grounding_audit.get("invalid_path_skipped") or 0) + int(
        grounding_audit.get("special_path_skipped") or 0
    )
    if int(grounding_audit.get("accepted_total") or 0) > 0 and invalid_grounded > 0:
        warnings.append(
            f"grounding discarded {invalid_grounded} illegal/special path(s) but retained a usable grounded prefetch set"
        )

    budget_skipped = int(selector_stats.get("budget_skipped") or 0)
    if budget_skipped > 0:
        warnings.append(f"selector trimmed {budget_skipped} lower-priority path(s) to satisfy budget constraints")

    if bool(selector_stats.get("forced_first")):
        budget_bytes = int(selector_stats.get("budget_bytes") or 0)
        chosen_bytes = int(selector_stats.get("chosen_bytes") or 0)
        if budget_bytes > 0 and chosen_bytes > budget_bytes:
            warnings.append(
                f"selector kept a forced-first result despite budget pressure (budget_bytes={budget_bytes}, chosen_bytes={chosen_bytes})"
            )

    low_confidence_skipped = int(selector_stats.get("low_confidence_skipped") or 0) + int(
        selector_stats.get("preferred_low_confidence_skipped") or 0
    )
    if low_confidence_skipped > 0:
        warnings.append(f"selector filtered {low_confidence_skipped} low-confidence path(s)")

    if cleaned_paths and len(cleaned_paths) < 2:
        warnings.append(f"cleaned prefetch list is small: {len(cleaned_paths)} path(s)")

    warnings = _dedupe_strings(warnings)
    if dry_run:
        return "unknown", warnings, "review_hints", blockers
    if not optimized_image_value:
        return "poor", warnings, "skip_publish", blockers
    if analyze_recommendation in {
        "rerun_sampling",
        "review_hints",
        "benchmark_before_publish",
        "skip_publish",
    }:
        recommendation = analyze_recommendation
    elif warnings:
        recommendation = "benchmark_before_publish"
    else:
        recommendation = "proceed"

    if analyze_quality_status in {"warning", "poor", "unknown"}:
        quality_status = analyze_quality_status
    elif warnings:
        quality_status = "warning"
    else:
        quality_status = "good"
    return quality_status, warnings, recommendation, blockers


def _summarize_apply_failure(stage_log_path: Path) -> str:
    fallback = f"apply stage failed, see {stage_log_path}"
    if not stage_log_path.exists():
        return fallback
    try:
        lines = stage_log_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return fallback
    marker = "image optimize 成功，但对 nerdctl + stargz + pull=never 不可消费"
    if any(marker in line for line in lines):
        err_lines = [
            line.strip()
            for line in lines
            if line.strip().startswith("[ERR] image optimize 成功")
            or line.strip().startswith("[ERR] inspect:")
            or line.strip().startswith("[ERR] run:")
            or line.strip().startswith("[ERR] smoke stderr:")
        ]
        if err_lines:
            return "; ".join(err_lines)
    return fallback


def run_apply_stage(
    *,
    repo_root: Path,
    work_dir: Path,
    src_image: str,
    selector_mode: str = "file",
    dst_image: str | None = None,
    namespace: str = "default",
    dry_run: bool = False,
    runtime_probes: bool = True,
) -> ApplyResult:
    mode_suffix = selector_mode_suffix(selector_mode)
    env_check_path = work_dir / f"env_check_apply{mode_suffix}.json"
    policy_path = work_dir / f"apply_policy{mode_suffix}.json"
    stage_log_path = work_dir / ("apply.log" if not mode_suffix else f"apply{mode_suffix}.log")
    apply_result_path = work_dir / f"apply_result{mode_suffix}.json"
    optimized_image_file = work_dir / f"optimized_image{mode_suffix}.txt"

    apply_script = repo_root / "scripts" / "apply_hints_to_image.sh"
    effective_dst_image = dst_image or default_destination_image(src_image, selector_mode=selector_mode)

    env_result = run_check_env_json(
        repo_root=repo_root,
        profile="apply",
        image=src_image,
        runtime_probes=runtime_probes,
    )
    write_env_check_payload(env_check_path, env_result.payload)
    preflight = env_result.process
    env_payload = env_result.payload
    policy_payload = evaluate_apply_policy(env_payload, dry_run=dry_run)
    policy_payload["env_check_exit_code"] = preflight.returncode
    policy_payload["env_check_stdout"] = (preflight.stdout or "").strip()
    policy_payload["env_check_stderr"] = (preflight.stderr or "").strip()
    policy_payload["src_image"] = src_image
    policy_payload["dst_image"] = effective_dst_image
    policy_payload["namespace"] = namespace
    policy_payload["selector_mode"] = selector_mode
    _write_policy(policy_path, policy_payload)
    if not policy_payload["ok"]:
        raise RuntimeError("; ".join(policy_payload.get("errors") or ["apply preflight failed"]))

    stage_log_path.write_text("", encoding="utf-8")
    apply_cmd = [
        "bash",
        str(apply_script),
        "--work",
        str(work_dir),
        "--selector-mode",
        selector_mode,
        "--src",
        src_image,
        "--dst",
        effective_dst_image,
        "--ns",
        namespace,
    ]
    if dry_run:
        apply_cmd.append("--dry-run")

    with stage_log_path.open("a", encoding="utf-8") as log_fh:
        proc = subprocess.run(
            apply_cmd,
            cwd=repo_root,
            text=True,
            stdout=log_fh,
            stderr=log_fh,
            check=False,
        )
    if proc.returncode != 0:
        raise RuntimeError(_summarize_apply_failure(stage_log_path))

    optimized_image_path: Path | None = None
    optimized_image_value = None
    if not dry_run:
        if not optimized_image_file.exists():
            raise RuntimeError(f"apply stage succeeded but {optimized_image_file.name} is missing")
        optimized_image_value = optimized_image_file.read_text(encoding="utf-8").strip()
        if not optimized_image_value:
            raise RuntimeError(f"{optimized_image_file.name} is empty after apply stage")
        optimized_image_path = optimized_image_file

    quality_status, quality_warnings, recommendation, validation_blockers = _apply_quality_assessment(
        work_dir=work_dir,
        selector_mode=selector_mode,
        dry_run=dry_run,
        optimized_image_value=optimized_image_value,
        policy_warnings=list(policy_payload.get("warnings") or []),
    )

    result_payload = {
        "status": "passed",
        "result_kind": "dry_run" if dry_run else "applied",
        "selector_mode": selector_mode,
        "src_image": src_image,
        "dst_image": effective_dst_image,
        "namespace": namespace,
        "dry_run": dry_run,
        "optimized_image_written": bool(optimized_image_path),
        "optimized_image": optimized_image_value,
        "env_check": str(env_check_path),
        "apply_policy": str(policy_path),
        "apply_log": str(stage_log_path),
        "quality_status": quality_status,
        "warnings": quality_warnings,
        "recommendation": recommendation,
        "blockers": validation_blockers,
        "result_contract": {
            "stage": "apply",
            "status": "passed",
            "result_kind": "dry_run" if dry_run else "applied",
            "validation_inputs": {
                "optimized_candidate_image": optimized_image_value,
                "selector_mode": selector_mode,
                "ready_for_benchmark": bool(optimized_image_value) and not dry_run,
                "ready_for_publish_evaluation": bool(optimized_image_value) and not dry_run,
                "quality_status": quality_status,
                "warnings": quality_warnings,
                "recommendation": recommendation,
                "blockers": validation_blockers,
            },
        },
    }
    apply_result_path.write_text(
        json.dumps(result_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    return ApplyResult(
        env_check_path=env_check_path,
        policy_path=policy_path,
        stage_log_path=stage_log_path,
        apply_result_path=apply_result_path,
        optimized_image_path=optimized_image_path,
        selector_mode=selector_mode,
        src_image=src_image,
        dst_image=effective_dst_image,
        namespace=namespace,
        dry_run=dry_run,
        warnings=quality_warnings,
        quality_status=quality_status,
        recommendation=recommendation,
        blockers=validation_blockers,
    )
