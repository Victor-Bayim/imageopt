import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from orchestrator.llm import ResolvedLlmProviderConfig
from orchestrator.manifest import ManifestStore
from orchestrator.workloads.hooks import ExecReadyHook, LogRegexReadyHook


ROOT = Path(__file__).resolve().parents[1]


def load_oneclick_product_module():
    module_path = ROOT / "scripts" / "oneclick_product.py"
    spec = importlib.util.spec_from_file_location("oneclick_product_under_test", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _touch(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _resolved_llm_config(
    *,
    provider: str = "openai",
    provider_type: str = "openai_compatible",
    api_base: str = "https://api.openai.com/v1",
    api_base_source: str = "provider_default:openai",
    api_key_env: str | None = "OPENAI_API_KEY",
    api_key_env_source: str | None = "provider_default:openai",
    api_key: str = "demo-key",
    api_key_source: str | None = "OPENAI_API_KEY",
    model: str = "gpt-4o-mini",
    model_source: str = "provider_default:openai",
    reasoning_effort: str | None = None,
    timeout_sec: int = 60,
) -> ResolvedLlmProviderConfig:
    return ResolvedLlmProviderConfig(
        provider=provider,
        provider_type=provider_type,
        provider_source="explicit" if provider != "openai" else "built_in_default",
        api_base=api_base,
        api_base_source=api_base_source,
        api_key_env=api_key_env,
        api_key_env_source=api_key_env_source,
        api_key=api_key,
        api_key_source=api_key_source,
        model=model,
        model_source=model_source,
        reasoning_effort=reasoning_effort,
        timeout_sec=timeout_sec,
    )


def _fake_preflight_result(work_dir: Path):
    env_check_path = _write_json(work_dir / "env_check.json", {"checks": []})
    policy_path = _write_json(
        work_dir / "product_policy.json",
        {"ok": True, "warnings": [], "errors": []},
    )
    return SimpleNamespace(
        env_check_path=env_check_path,
        policy_path=policy_path,
        policy_payload={"ok": True, "warnings": [], "errors": []},
    )


def _fake_sample_result(work_dir: Path):
    return SimpleNamespace(
        runtime_state_path=_write_json(work_dir / "runtime_state.json", {"status": "passed"}),
        raw_events_path=_touch(work_dir / "raw_events.ndjson", ""),
        sampler_log_path=_touch(work_dir / "sampler.log", ""),
        sampler_metrics_path=_touch(work_dir / "sampler.prom", ""),
    )


def _fake_export_result(work_dir: Path):
    return SimpleNamespace(
        events_path=_touch(work_dir / "events.jsonl", ""),
        export_stats_path=_write_json(work_dir / "export_events_stats.json", {"relative_open_total": 1}),
        cwd_snapshot_path=_write_json(work_dir / "cwd_snapshot.json", {}),
        maps_paths_path=_touch(work_dir / "maps_paths.txt", ""),
        mapfiles_devino_path=_touch(work_dir / "mapfiles_devino.txt", ""),
        devino_dict_path=_write_json(work_dir / "devino_dict.json", {}),
        events_enriched_path=_touch(work_dir / "events_enriched.jsonl", ""),
        stage_log_path=_touch(work_dir / "export_enrich.log", ""),
    )


def _fake_analyze_result(work_dir: Path, *, selector_mode: str = "file"):
    estargz_name = "estargz_chunk_shadow.list" if selector_mode == "chunk-shadow" else "estargz.list"
    selector_stats_name = (
        "selector_stats_chunk_shadow.json" if selector_mode == "chunk-shadow" else "selector_stats_default.json"
    )
    return SimpleNamespace(
        quality_gate_path=_write_json(work_dir / "quality_gate.json", {"ok": True, "warnings": []}),
        candidates_path=_write_json(work_dir / "candidates.json", []),
        aggregate_stats_path=_write_json(work_dir / "aggregate_stats.json", {}),
        ranked_path=_touch(work_dir / "ranked.yaml", "P0:\n  - /app/a.so\n"),
        estargz_list_path=_touch(work_dir / estargz_name, "/app/a.so\n"),
        selector_stats_path=_write_json(work_dir / selector_stats_name, {"chosen": 1}),
        selector_stats_hint_path=_write_json(work_dir / "selector_stats_hints.json", {}),
        hints_path=_touch(work_dir / "hints.yaml", "P0:\n  - /app/a.so\n"),
        image_toc_path=None,
        toc_shadow_path=None,
        chunk_selector_shadow_path=None,
        chunk_shadow_ranked_path=None,
        chunk_shadow_estargz_list_path=None,
        chunk_shadow_hints_path=None,
        chunk_shadow_selector_stats_path=None,
        stage_log_path=_touch(work_dir / "analyze.log", ""),
        quality_payload={"ok": True, "warnings": []},
        warnings=[],
    )


def _fake_llm_result(
    work_dir: Path,
    *,
    mode: str,
    llm_applied: bool,
    final_hints_source: str,
    warnings: list[str] | None = None,
):
    manual_response_default_path = work_dir / "hints_raw.manual.yaml"
    mode_result = {
        "api": "llm-auto-success",
        "api_fallback": "llm-auto-fallback",
        "manual_response": "llm-manual-completed",
        "manual_export": "llm-manual-paused",
    }.get(mode, mode)
    return SimpleNamespace(
        prompt_path=_touch(work_dir / "prompt.txt", "prompt"),
        web_input_path=_touch(work_dir / "web_llm_ready_to_paste.txt", "system\nuser\n"),
        body_path=_write_json(work_dir / "body.json", {"model": "demo"}),
        response_path=_touch(work_dir / "llm_response.json", "{}\n"),
        hints_raw_path=_touch(work_dir / "hints_raw.yaml", "P0:\n  - /app/a.so\n"),
        preferred_paths_path=_touch(work_dir / "preferred_paths.txt", "/app/a.so\n"),
        grounding_audit_path=_write_json(work_dir / "llm_grounding_audit.json", {}),
        hints_path=_touch(work_dir / "hints.yaml", "P0:\n  - /app/a.so\n"),
        estargz_list_path=_touch(work_dir / "estargz.list", "/app/a.so\n"),
        selector_stats_hint_path=_write_json(work_dir / "selector_stats_hints.json", {}),
        manual_instructions_path=_touch(work_dir / "llm_manual_instructions.md", "manual"),
        import_script_path=_touch(work_dir / "import_manual_web_llm.sh", "#!/usr/bin/env bash\n"),
        result_path=_write_json(
            work_dir / "llm_result.json",
            {
                "mode": mode,
                "mode_result": mode_result,
                "llm_applied": llm_applied,
                "final_hints_source": final_hints_source,
                "accepted_total": 1 if llm_applied else 0,
                "fallback_kind": "external_dependency" if mode == "api_fallback" else None,
                "fallback_reason": (
                    "http 429/5xx: LLM HTTP 429; kept deterministic hints"
                    if mode == "api_fallback"
                    else None
                ),
            },
        ),
        stage_log_path=_touch(work_dir / "llm_optimize.log", ""),
        warnings=list(warnings or []),
        mode=mode,
        llm_applied=llm_applied,
        final_hints_source=final_hints_source,
        pause_reason="awaiting_manual_llm_response" if mode == "manual_export" else None,
        manual_response_default_path=manual_response_default_path,
        manual_response_format="json",
        resume_command=(
            f"python3 {ROOT / 'scripts' / 'oneclick_product.py'} run --work-dir {work_dir} "
            f"--manual-response-file {manual_response_default_path}"
        ),
    )


def _fake_apply_result(
    work_dir: Path,
    *,
    src_image: str,
    dst_image: str,
    selector_mode: str = "file",
):
    optimized_image_name = "optimized_image_chunk_shadow.txt" if selector_mode == "chunk-shadow" else "optimized_image.txt"
    apply_result_name = "apply_result_chunk_shadow.json" if selector_mode == "chunk-shadow" else "apply_result.json"
    log_name = "apply_chunk_shadow.log" if selector_mode == "chunk-shadow" else "apply.log"
    optimized_image_path = _touch(work_dir / optimized_image_name, f"{dst_image}\n")
    apply_result_path = _write_json(
        work_dir / apply_result_name,
        {
            "status": "passed",
            "selector_mode": selector_mode,
            "src_image": src_image,
            "dst_image": dst_image,
            "optimized_image_written": True,
            "optimized_image": dst_image,
        },
    )
    return SimpleNamespace(
        env_check_path=_write_json(work_dir / f"env_check_apply_{selector_mode}.json", {}),
        policy_path=_write_json(work_dir / f"apply_policy_{selector_mode}.json", {"ok": True}),
        stage_log_path=_touch(work_dir / log_name, ""),
        apply_result_path=apply_result_path,
        optimized_image_path=optimized_image_path,
        selector_mode=selector_mode,
        src_image=src_image,
        dst_image=dst_image,
        namespace="default",
        dry_run=False,
        warnings=[],
    )


def _fake_benchmark_result(
    work_dir: Path,
    *,
    opt_image: str,
    selector_mode: str = "file",
    status: str = "passed",
    valid: bool = True,
    cold_mode: str = "local-cold",
):
    result_path = _write_json(
        work_dir / "benchmark_result.json",
        {
            "status": status,
            "valid": valid,
            "selector_mode": selector_mode,
            "opt_image": opt_image,
            "cold_mode": cold_mode,
            "summary": {"valid": valid, "invalid_reasons": [], "comparison": {}, "labels": {}},
        },
    )
    return SimpleNamespace(
        env_check_base_path=_write_json(work_dir / "env_check_benchmark_base.json", {}),
        env_check_opt_path=_write_json(work_dir / "env_check_benchmark_opt.json", {}),
        policy_path=_write_json(work_dir / "benchmark_policy.json", {"ok": True}),
        stage_log_path=_touch(work_dir / "benchmark.log", ""),
        csv_path=_touch(
            work_dir / "coldstart_compare.csv",
            "ts,label,snapshotter,mode,ready_ms,predict_extra_ms,proof_exists,proof_done,status,err\n",
        ),
        log_dir=work_dir / "bench_logs",
        result_path=result_path,
        valid=valid,
        invalid_reasons=[],
        warnings=[],
        summary_payload={"status": status, "cold_mode": cold_mode, "summary": {"comparison": {}, "labels": {}}},
    )


def _fake_report_result(
    work_dir: Path,
    *,
    overall_status: str = "passed",
    mode: str = "deterministic",
    mode_result: str = "deterministic-completed",
    llm_applied: bool = False,
    final_hints_source: str = "deterministic_ranked",
    optimized_image: str = "docker.io/library/app:opt",
    benchmark_status: str = "passed",
    baseline_median_ready_ms: float = 120.0,
    optimized_median_ready_ms: float = 90.0,
    improvement_pct: float = 25.0,
):
    summary_payload = {
        "overall_status": overall_status,
        "mode": mode,
        "mode_result": mode_result,
        "llm": {
            "mode_result": mode_result,
            "llm_applied": llm_applied,
            "final_hints_source": final_hints_source,
        },
        "apply": {
            "optimized_image_written": True,
            "optimized_image": optimized_image,
        },
        "benchmark": {
            "status": benchmark_status,
            "baseline_median_ready_ms": baseline_median_ready_ms,
            "optimized_median_ready_ms": optimized_median_ready_ms,
            "comparison": {
                "median_ready_improvement_pct": improvement_pct,
            },
        },
        "artifacts": {},
    }
    return SimpleNamespace(
        report_json_path=_write_json(work_dir / "run_report.json", summary_payload),
        report_md_path=_touch(work_dir / "run_report.md", "# Report\n"),
        summary_payload=summary_payload,
    )


class OneclickProductCliTests(unittest.TestCase):
    def test_analyze_cli_forwards_selector_mode_and_records_chunk_shadow_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            store = ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260323_010000",
                work_dir=work_dir,
                image="docker.io/library/app:base",
                startup_mode="legacy",
                llm_enabled=False,
            )
            store.stage_finish("export_enrich", status="passed", summary="export ok")

            module = load_oneclick_product_module()
            captured = {}

            def fake_run_analyze_stage(**kwargs):
                captured.update(kwargs)
                for name in (
                    "quality_gate.json",
                    "candidates.json",
                    "aggregate_stats.json",
                    "ranked.yaml",
                    "estargz.list",
                    "selector_stats_default.json",
                    "hints.yaml",
                    "selector_stats_hints.json",
                    "analyze.log",
                    "image_toc.json",
                    "toc_shadow.json",
                    "chunk_selector_shadow.json",
                    "ranked_chunk_shadow.yaml",
                    "estargz_chunk_shadow.list",
                    "hints_chunk_shadow.yaml",
                    "selector_stats_chunk_shadow.json",
                ):
                    (work_dir / name).write_text("{}\n", encoding="utf-8")
                (work_dir / "estargz.list").write_text("/app/a.so\n", encoding="utf-8")
                (work_dir / "estargz_chunk_shadow.list").write_text("/app/a.so\n", encoding="utf-8")
                return SimpleNamespace(
                    quality_gate_path=work_dir / "quality_gate.json",
                    candidates_path=work_dir / "candidates.json",
                    aggregate_stats_path=work_dir / "aggregate_stats.json",
                    ranked_path=work_dir / "ranked.yaml",
                    estargz_list_path=work_dir / "estargz.list",
                    selector_stats_path=work_dir / "selector_stats_default.json",
                    hints_path=work_dir / "hints.yaml",
                    selector_stats_hint_path=work_dir / "selector_stats_hints.json",
                    chunk_shadow_ranked_path=work_dir / "ranked_chunk_shadow.yaml",
                    chunk_shadow_estargz_list_path=work_dir / "estargz_chunk_shadow.list",
                    chunk_shadow_hints_path=work_dir / "hints_chunk_shadow.yaml",
                    chunk_shadow_selector_stats_path=work_dir / "selector_stats_chunk_shadow.json",
                    image_toc_path=work_dir / "image_toc.json",
                    toc_shadow_path=work_dir / "toc_shadow.json",
                    chunk_selector_shadow_path=work_dir / "chunk_selector_shadow.json",
                    stage_log_path=work_dir / "analyze.log",
                    quality_payload={"ok": True, "warnings": []},
                    warnings=["experimental chunk-shadow selector artifacts generated"],
                    size_budget_mb_effective=1,
                    filesystem_total_bytes=4096,
                )

            argv = [
                "oneclick_product.py",
                "analyze",
                "--work-dir",
                str(work_dir),
                "--selector-mode",
                "chunk-shadow",
            ]
            with mock.patch.object(module, "run_analyze_stage", side_effect=fake_run_analyze_stage):
                with mock.patch.object(module.sys, "argv", argv):
                    rc = module.main()

            self.assertEqual(0, rc)
            self.assertEqual("chunk-shadow", captured["selector_mode"])
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("chunk-shadow", manifest_payload["options"]["selector_mode"])
            self.assertEqual(
                str(work_dir / "selector_stats_chunk_shadow.json"),
                manifest_payload["artifacts"]["selector_stats_chunk_shadow"],
            )
            self.assertEqual(
                str(work_dir / "estargz_chunk_shadow.list"),
                manifest_payload["artifacts"]["estargz_list_chunk_shadow"],
            )

    def test_apply_cli_inherits_manifest_selector_mode_and_records_mode_specific_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            manifest = ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260325_000001",
                work_dir=work_dir,
                image="docker.io/library/app:base",
                startup_mode="legacy",
                llm_enabled=False,
            )
            manifest.data.setdefault("options", {})["selector_mode"] = "chunk-shadow"
            manifest.save()

            module = load_oneclick_product_module()
            captured = {}

            def fake_run_apply_stage(**kwargs):
                captured.update(kwargs)
                return SimpleNamespace(
                    env_check_path=work_dir / "env_check_apply_chunk_shadow.json",
                    policy_path=work_dir / "apply_policy_chunk_shadow.json",
                    stage_log_path=work_dir / "apply_chunk_shadow.log",
                    apply_result_path=work_dir / "apply_result_chunk_shadow.json",
                    optimized_image_path=work_dir / "optimized_image_chunk_shadow.txt",
                    selector_mode="chunk-shadow",
                    src_image="docker.io/library/app:base",
                    dst_image="docker.io/library/app:base-stargz-chunk-shadow",
                    namespace="default",
                    dry_run=False,
                    warnings=[],
                )

            argv = [
                "oneclick_product.py",
                "apply",
                "--work-dir",
                str(work_dir),
            ]
            with mock.patch.object(module, "run_apply_stage", side_effect=fake_run_apply_stage):
                with mock.patch.object(module.sys, "argv", argv):
                    rc = module.main()

            self.assertEqual(0, rc)
            self.assertEqual("chunk-shadow", captured["selector_mode"])
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                str(work_dir / "apply_result_chunk_shadow.json"),
                manifest_payload["artifacts"]["apply_result_chunk_shadow"],
            )
            self.assertEqual(
                str(work_dir / "optimized_image_chunk_shadow.txt"),
                manifest_payload["artifacts"]["optimized_image_chunk_shadow"],
            )

    def test_benchmark_cli_inherits_manifest_selector_mode_for_optimized_image_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            manifest = ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260325_000002",
                work_dir=work_dir,
                image="docker.io/library/app:base",
                startup_mode="legacy",
                llm_enabled=False,
            )
            manifest.data.setdefault("options", {})["selector_mode"] = "chunk-shadow"
            manifest.stage_finish(
                "apply",
                status="passed",
                summary="apply chunk-shadow ok",
                artifacts={"optimized_image_chunk_shadow": str(work_dir / "optimized_image_chunk_shadow.txt")},
            )
            (work_dir / "optimized_image_chunk_shadow.txt").write_text(
                "docker.io/library/app:opt-chunk-shadow\n",
                encoding="utf-8",
            )

            module = load_oneclick_product_module()
            benchmark_mock = mock.Mock(
                return_value=SimpleNamespace(
                    env_check_base_path=work_dir / "env_check_benchmark_base.json",
                    env_check_opt_path=work_dir / "env_check_benchmark_opt.json",
                    policy_path=work_dir / "benchmark_policy.json",
                    stage_log_path=work_dir / "benchmark.log",
                    csv_path=work_dir / "coldstart_compare.csv",
                    log_dir=work_dir / "bench_logs",
                    result_path=work_dir / "benchmark_result.json",
                    valid=True,
                    invalid_reasons=[],
                    warnings=[],
                    summary_payload={},
                )
            )

            argv = [
                "oneclick_product.py",
                "benchmark",
                "--work-dir",
                str(work_dir),
                "--base-image",
                "docker.io/library/app:base",
            ]
            with mock.patch.object(module, "run_benchmark_stage", benchmark_mock):
                with mock.patch.object(module.sys, "argv", argv):
                    rc = module.main()

            self.assertEqual(0, rc)
            self.assertEqual(
                "docker.io/library/app:opt-chunk-shadow",
                benchmark_mock.call_args.kwargs["opt_image"],
            )

    def test_benchmark_cli_forwards_exec_ready_probe_to_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260318_000000",
                work_dir=work_dir,
                image="docker.io/library/app:base",
                startup_mode="legacy",
                llm_enabled=False,
            )
            optimized_image_txt = work_dir / "optimized_image.txt"
            optimized_image_txt.write_text("docker.io/library/app:opt\n", encoding="utf-8")
            module = load_oneclick_product_module()

            captured = {}

            def fake_run_benchmark_stage(**kwargs):
                captured.update(kwargs)
                result_path = work_dir / "benchmark_result.json"
                result_path.write_text("{}\n", encoding="utf-8")
                csv_path = work_dir / "coldstart_compare.csv"
                csv_path.write_text(
                    "ts,label,snapshotter,mode,ready_ms,predict_extra_ms,proof_exists,proof_done,status,err\n",
                    encoding="utf-8",
                )
                log_dir = work_dir / "bench_logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                return SimpleNamespace(
                    env_check_base_path=work_dir / "env_check_benchmark_base.json",
                    env_check_opt_path=work_dir / "env_check_benchmark_opt.json",
                    policy_path=work_dir / "benchmark_policy.json",
                    stage_log_path=work_dir / "benchmark.log",
                    csv_path=csv_path,
                    log_dir=log_dir,
                    result_path=result_path,
                    valid=True,
                    invalid_reasons=[],
                    warnings=[],
                )

            argv = [
                "oneclick_product.py",
                "benchmark",
                "--work-dir",
                str(work_dir),
                "--base-image",
                "docker.io/library/app:base",
                "--opt-image",
                "docker.io/library/app:opt",
                "--mode",
                "ready",
                "--ready-exec",
                "test -f /tmp/ready",
                "--ready-shell",
                "bash",
                "--container-gpus",
                "all",
                "--container-ipc",
                "host",
                "--container-env",
                "HF_HUB_OFFLINE=1",
                "--container-mount",
                "type=bind,src=/models,dst=/models,readonly",
                "--container-entrypoint",
                "python3",
                "--container-cmd=-m",
                "--container-cmd=sglang.launch_server",
                "--pull-policy",
                "always",
                "--drop-local-image-ref",
                "--cold-mode",
                "registry-cold-strict",
            ]
            with mock.patch.object(module, "run_benchmark_stage", side_effect=fake_run_benchmark_stage):
                with mock.patch.object(module.sys, "argv", argv):
                    rc = module.main()

            self.assertEqual(0, rc)
            self.assertEqual("ready", captured["mode"])
            self.assertIsInstance(captured["ready_hook"], ExecReadyHook)
            self.assertEqual("test -f /tmp/ready", captured["ready_hook"].command)
            self.assertEqual("bash", captured["ready_hook"].shell)
            self.assertEqual("all", captured["container_runtime"].gpus)
            self.assertEqual("host", captured["container_runtime"].ipc)
            self.assertEqual("python3", captured["container_runtime"].entrypoint)
            self.assertEqual(("-m", "sglang.launch_server"), captured["container_runtime"].command)
            self.assertEqual("always", captured["pull_policy"])
            self.assertTrue(captured["drop_local_image_ref"])
            self.assertEqual("registry-cold-strict", captured["cold_mode"])
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("passed", manifest_payload["stages"]["benchmark"]["status"])
            self.assertEqual("registry-cold-strict", manifest_payload["options"]["benchmark_cold_mode"])
            self.assertEqual(
                str(work_dir / "benchmark_result.json"),
                manifest_payload["artifacts"]["benchmark_result"],
            )

    def test_run_cli_resumes_passed_stages_and_only_executes_remaining_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            store = ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260318_000001",
                work_dir=work_dir,
                image="docker.io/library/app:base",
                startup_mode="legacy",
                llm_enabled=False,
            )
            for stage_name in ("preflight", "sample", "export_enrich", "analyze"):
                store.stage_finish(stage_name, status="passed", summary=f"{stage_name} ok")
            optimized_image_txt = work_dir / "optimized_image.txt"
            optimized_image_txt.write_text("docker.io/library/app:opt\n", encoding="utf-8")
            store.stage_finish(
                "apply",
                status="passed",
                summary="apply ok",
                artifacts={"optimized_image": str(optimized_image_txt)},
            )

            module = load_oneclick_product_module()

            benchmark_result_path = work_dir / "benchmark_result.json"
            benchmark_result_path.write_text("{}\n", encoding="utf-8")
            report_json_path = work_dir / "run_report.json"
            report_md_path = work_dir / "run_report.md"
            report_json_path.write_text('{"overall_status":"passed"}\n', encoding="utf-8")
            report_md_path.write_text("# Report\n", encoding="utf-8")

            benchmark_mock = mock.Mock(
                return_value=SimpleNamespace(
                    env_check_base_path=work_dir / "env_check_benchmark_base.json",
                    env_check_opt_path=work_dir / "env_check_benchmark_opt.json",
                    policy_path=work_dir / "benchmark_policy.json",
                    stage_log_path=work_dir / "benchmark.log",
                    csv_path=work_dir / "coldstart_compare.csv",
                    log_dir=work_dir / "bench_logs",
                    result_path=benchmark_result_path,
                    valid=True,
                    invalid_reasons=[],
                    warnings=["variance warning"],
                )
            )
            report_mock = mock.Mock(
                return_value=SimpleNamespace(
                    report_json_path=report_json_path,
                    report_md_path=report_md_path,
                    summary_payload={"overall_status": "passed"},
                )
            )

            argv = [
                "oneclick_product.py",
                "run",
                "docker.io/library/app:base",
                "--work-dir",
                str(work_dir),
                "--apply",
                "--benchmark",
                "--bench-mode",
                "ready",
                "--bench-ready-log-regex",
                "server ready",
            ]
            with mock.patch.object(module, "run_preflight") as preflight_mock:
                with mock.patch.object(module, "run_sample_stage") as sample_mock:
                    with mock.patch.object(module, "run_export_enrich_stage") as export_mock:
                        with mock.patch.object(module, "run_analyze_stage") as analyze_mock:
                            with mock.patch.object(module, "run_apply_stage") as apply_mock:
                                with mock.patch.object(module, "run_benchmark_stage", benchmark_mock):
                                    with mock.patch.object(module, "run_report_stage", report_mock):
                                        with mock.patch.object(module.sys, "argv", argv):
                                            rc = module.main()

            self.assertEqual(0, rc)
            preflight_mock.assert_not_called()
            sample_mock.assert_not_called()
            export_mock.assert_not_called()
            analyze_mock.assert_not_called()
            apply_mock.assert_not_called()
            benchmark_mock.assert_called_once()
            report_mock.assert_called_once()
            self.assertEqual("docker.io/library/app:opt", benchmark_mock.call_args.kwargs["opt_image"])
            self.assertIsInstance(benchmark_mock.call_args.kwargs["ready_hook"], LogRegexReadyHook)
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("passed", manifest_payload["stages"]["apply"]["status"])
            self.assertEqual("passed", manifest_payload["stages"]["benchmark"]["status"])
            self.assertEqual("passed", manifest_payload["stages"]["report"]["status"])

    def test_run_cli_defaults_to_deterministic_and_skips_llm_optimize_after_analyze(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            store = ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260318_000010",
                work_dir=work_dir,
                image="docker.io/library/app:base",
                startup_mode="legacy",
                llm_enabled=False,
            )
            for stage_name in ("preflight", "sample", "export_enrich", "analyze"):
                store.stage_finish(stage_name, status="passed", summary=f"{stage_name} ok")

            module = load_oneclick_product_module()
            report_json_path = work_dir / "run_report.json"
            report_md_path = work_dir / "run_report.md"
            report_json_path.write_text('{"overall_status":"passed"}\n', encoding="utf-8")
            report_md_path.write_text("# Report\n", encoding="utf-8")

            llm_mock = mock.Mock()
            report_mock = mock.Mock(
                return_value=SimpleNamespace(
                    report_json_path=report_json_path,
                    report_md_path=report_md_path,
                    summary_payload={"overall_status": "passed"},
                )
            )

            argv = [
                "oneclick_product.py",
                "run",
                "docker.io/library/app:base",
                "--work-dir",
                str(work_dir),
            ]
            with mock.patch.object(module, "run_llm_optimize_stage", llm_mock):
                with mock.patch.object(module, "run_report_stage", report_mock):
                    with mock.patch.object(module.sys, "argv", argv):
                        rc = module.main()

            self.assertEqual(0, rc)
            llm_mock.assert_not_called()
            report_mock.assert_called_once()
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertNotIn("llm_optimize", manifest_payload["stages"])
            self.assertEqual("passed", manifest_payload["stages"]["report"]["status"])

    def test_run_cli_mode_llm_auto_executes_llm_optimize_after_analyze(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            store = ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260318_000011",
                work_dir=work_dir,
                image="docker.io/library/app:base",
                startup_mode="legacy",
                llm_enabled=False,
            )
            for stage_name in ("preflight", "sample", "export_enrich", "analyze"):
                store.stage_finish(stage_name, status="passed", summary=f"{stage_name} ok")

            module = load_oneclick_product_module()
            llm_result_path = work_dir / "llm_result.json"
            llm_result_path.write_text('{"mode":"api_fallback"}\n', encoding="utf-8")
            report_json_path = work_dir / "run_report.json"
            report_md_path = work_dir / "run_report.md"
            report_json_path.write_text('{"overall_status":"passed"}\n', encoding="utf-8")
            report_md_path.write_text("# Report\n", encoding="utf-8")

            llm_mock = mock.Mock(
                return_value=SimpleNamespace(
                    prompt_path=work_dir / "prompt.txt",
                    web_input_path=work_dir / "web_llm_ready_to_paste.txt",
                    body_path=work_dir / "body.json",
                    response_path=work_dir / "llm_response.json",
                    hints_raw_path=work_dir / "hints_raw.yaml",
                    preferred_paths_path=work_dir / "preferred_paths.txt",
                    grounding_audit_path=work_dir / "llm_grounding_audit.json",
                    hints_path=work_dir / "hints.yaml",
                    estargz_list_path=work_dir / "estargz.list",
                    selector_stats_hint_path=work_dir / "selector_stats_hints.json",
                    manual_instructions_path=work_dir / "llm_manual_instructions.md",
                    import_script_path=work_dir / "import_manual_web_llm.sh",
                    result_path=llm_result_path,
                    stage_log_path=work_dir / "llm_optimize.log",
                    warnings=["LLM API request failed; kept deterministic hints"],
                    mode="api_fallback",
                    llm_applied=False,
                    final_hints_source="deterministic_ranked",
                )
            )
            report_mock = mock.Mock(
                return_value=SimpleNamespace(
                    report_json_path=report_json_path,
                    report_md_path=report_md_path,
                    summary_payload={"overall_status": "passed"},
                )
            )

            argv = [
                "oneclick_product.py",
                "run",
                "docker.io/library/app:base",
                "--work-dir",
                str(work_dir),
                "--mode",
                "llm-auto",
            ]
            with mock.patch.object(module, "resolve_llm_provider_config", return_value=_resolved_llm_config()):
                with mock.patch.object(module, "run_llm_optimize_stage", llm_mock):
                    with mock.patch.object(module, "run_report_stage", report_mock):
                        with mock.patch.object(module.sys, "argv", argv):
                            rc = module.main()

            self.assertEqual(0, rc)
            llm_mock.assert_called_once()
            self.assertFalse(llm_mock.call_args.kwargs["force_manual_export"])
            self.assertIsNone(llm_mock.call_args.kwargs["manual_response_file"])
            report_mock.assert_called_once()
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(True, manifest_payload["options"]["llm_enabled"])
            self.assertEqual("passed", manifest_payload["stages"]["llm_optimize"]["status"])
            self.assertEqual(str(llm_result_path), manifest_payload["artifacts"]["llm_result"])

    def test_run_cli_deterministic_apply_benchmark_forms_minimal_product_closure(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            module = load_oneclick_product_module()

            preflight_mock = mock.Mock(return_value=_fake_preflight_result(work_dir))
            sample_mock = mock.Mock(return_value=_fake_sample_result(work_dir))
            export_mock = mock.Mock(return_value=_fake_export_result(work_dir))
            analyze_mock = mock.Mock(return_value=_fake_analyze_result(work_dir))
            apply_mock = mock.Mock(
                return_value=_fake_apply_result(
                    work_dir,
                    src_image="docker.io/library/app:base",
                    dst_image="docker.io/library/app:opt",
                )
            )
            benchmark_mock = mock.Mock(
                return_value=_fake_benchmark_result(
                    work_dir,
                    opt_image="docker.io/library/app:opt",
                )
            )
            report_mock = mock.Mock(
                return_value=_fake_report_result(
                    work_dir,
                    mode="deterministic",
                    mode_result="deterministic-completed",
                    llm_applied=False,
                    final_hints_source="deterministic_ranked",
                    optimized_image="docker.io/library/app:opt",
                )
            )

            argv = [
                "oneclick_product.py",
                "run",
                "docker.io/library/app:base",
                "--work-dir",
                str(work_dir),
                "--mode",
                "deterministic",
                "--apply",
                "--dst-image",
                "docker.io/library/app:opt",
                "--benchmark",
            ]
            stdout = io.StringIO()
            with mock.patch.object(module, "run_preflight", preflight_mock):
                with mock.patch.object(module, "run_sample_stage", sample_mock):
                    with mock.patch.object(module, "run_export_enrich_stage", export_mock):
                        with mock.patch.object(module, "run_analyze_stage", analyze_mock):
                            with mock.patch.object(module, "run_llm_optimize_stage") as llm_mock:
                                with mock.patch.object(module, "run_apply_stage", apply_mock):
                                    with mock.patch.object(module, "run_benchmark_stage", benchmark_mock):
                                        with mock.patch.object(module, "run_report_stage", report_mock):
                                            with mock.patch.object(module.sys, "argv", argv):
                                                with mock.patch.object(module.sys, "stdout", stdout):
                                                    rc = module.main()

            self.assertEqual(0, rc)
            llm_mock.assert_not_called()
            preflight_mock.assert_called_once()
            self.assertFalse(preflight_mock.call_args.kwargs["llm_enabled"])
            sample_mock.assert_called_once()
            export_mock.assert_called_once()
            analyze_mock.assert_called_once()
            apply_mock.assert_called_once()
            benchmark_mock.assert_called_once()
            report_mock.assert_called_once()
            self.assertEqual("docker.io/library/app:opt", benchmark_mock.call_args.kwargs["opt_image"])
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("deterministic", manifest_payload["options"]["mode"])
            for stage_name in ("preflight", "sample", "export_enrich", "analyze", "apply", "benchmark", "report"):
                self.assertEqual("passed", manifest_payload["stages"][stage_name]["status"])
            report_payload = json.loads((work_dir / "run_report.json").read_text(encoding="utf-8"))
            self.assertEqual("deterministic-completed", report_payload["mode_result"])
            self.assertEqual("docker.io/library/app:opt", report_payload["apply"]["optimized_image"])
            self.assertNotIn("recommended_publish=", stdout.getvalue())
            self.assertIn("stage-start preflight", stdout.getvalue())
            self.assertIn("stage-pass preflight", stdout.getvalue())
            self.assertIn("stage-start sample", stdout.getvalue())
            self.assertIn("stage-pass sample", stdout.getvalue())
            self.assertIn("stage-start export-enrich", stdout.getvalue())
            self.assertIn("stage-pass export-enrich", stdout.getvalue())
            self.assertIn("stage-start analyze", stdout.getvalue())
            self.assertIn("stage-pass analyze", stdout.getvalue())
            self.assertIn("stage-start apply", stdout.getvalue())
            self.assertIn("stage-pass apply", stdout.getvalue())
            self.assertIn("stage-start benchmark", stdout.getvalue())
            self.assertIn("stage-pass benchmark", stdout.getvalue())
            self.assertIn("stage-start report", stdout.getvalue())
            self.assertIn("stage-pass report", stdout.getvalue())
            self.assertIn("apply-summary status=passed optimized_image_written=True", stdout.getvalue())
            self.assertIn("benchmark-summary status=passed", stdout.getvalue())
            self.assertIn(
                "final-summary run_status=passed mode=deterministic mode_result=deterministic-completed",
                stdout.getvalue(),
            )
            self.assertIn("llm_applied=False", stdout.getvalue())
            self.assertIn("hints_source=deterministic_ranked", stdout.getvalue())
            self.assertIn(f"manifest={work_dir / 'manifest.json'}", stdout.getvalue())

    def test_run_cli_preset_tf_serving_supplies_defaults_and_works_with_inheritance(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            model_dir = work_dir / "model"
            model_dir.mkdir()
            module = load_oneclick_product_module()

            preflight_mock = mock.Mock(return_value=_fake_preflight_result(work_dir))
            sample_mock = mock.Mock(return_value=_fake_sample_result(work_dir))
            export_mock = mock.Mock(return_value=_fake_export_result(work_dir))
            analyze_mock = mock.Mock(return_value=_fake_analyze_result(work_dir))
            apply_mock = mock.Mock(
                return_value=_fake_apply_result(
                    work_dir,
                    src_image="docker.io/tensorflow/serving:latest",
                    dst_image="docker.io/tensorflow/serving:latest-optimized",
                )
            )
            benchmark_mock = mock.Mock(
                return_value=_fake_benchmark_result(
                    work_dir,
                    opt_image="docker.io/tensorflow/serving:latest-optimized",
                )
            )
            report_mock = mock.Mock(return_value=_fake_report_result(work_dir))

            argv = [
                "oneclick_product.py",
                "run",
                "docker.io/tensorflow/serving:latest",
                "--work-dir",
                str(work_dir),
                "--preset",
                "tf-serving-half-plus-two",
                "--model-host",
                str(model_dir),
                "--apply",
                "--benchmark",
            ]
            stdout = io.StringIO()
            with mock.patch.object(module, "run_preflight", preflight_mock):
                with mock.patch.object(module, "run_sample_stage", sample_mock):
                    with mock.patch.object(module, "run_export_enrich_stage", export_mock):
                        with mock.patch.object(module, "run_analyze_stage", analyze_mock):
                            with mock.patch.object(module, "run_apply_stage", apply_mock):
                                with mock.patch.object(module, "run_benchmark_stage", benchmark_mock):
                                    with mock.patch.object(module, "run_report_stage", report_mock):
                                        with mock.patch.object(module.sys, "argv", argv):
                                            with mock.patch.object(module.sys, "stdout", stdout):
                                                rc = module.main()

            self.assertEqual(0, rc)
            self.assertEqual("auto", preflight_mock.call_args.kwargs["startup_mode"])
            self.assertEqual("auto", sample_mock.call_args.kwargs["startup_mode"])
            self.assertEqual(
                "tf-serving-half-plus-two",
                sample_mock.call_args.kwargs["workload_profile"],
            )
            self.assertEqual(
                "docker.io/tensorflow/serving:latest-optimized",
                apply_mock.call_args.kwargs["dst_image"],
            )
            self.assertEqual("ready", benchmark_mock.call_args.kwargs["mode"])
            self.assertEqual(
                "tf-serving-half-plus-two",
                benchmark_mock.call_args.kwargs["workload_profile"],
            )
            self.assertEqual(str(model_dir), benchmark_mock.call_args.kwargs["model_host_dir"])
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("tf-serving-half-plus-two", manifest_payload["options"]["preset"])
            self.assertEqual("tf-serving-half-plus-two", manifest_payload["options"]["sample_workload"])
            self.assertEqual("tf-serving-half-plus-two", manifest_payload["options"]["benchmark_workload"])
            self.assertEqual(
                "tf-serving-half-plus-two",
                manifest_payload["options"]["run_parameter_resolution"]["workload"]["value"],
            )
            self.assertEqual(
                "auto",
                manifest_payload["options"]["run_parameter_resolution"]["startup_mode"]["value"],
            )
            self.assertEqual(
                "ready",
                manifest_payload["options"]["run_parameter_resolution"]["bench_mode"]["value"],
            )
            self.assertIn("workload=tf-serving-half-plus-two", stdout.getvalue())
            self.assertIn("startup_mode=auto", stdout.getvalue())
            self.assertIn("bench_mode=ready", stdout.getvalue())

    def test_run_cli_preset_tf_serving_efficientnet_embedded_supplies_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            module = load_oneclick_product_module()

            preflight_mock = mock.Mock(return_value=_fake_preflight_result(work_dir))
            sample_mock = mock.Mock(return_value=_fake_sample_result(work_dir))
            export_mock = mock.Mock(return_value=_fake_export_result(work_dir))
            analyze_mock = mock.Mock(return_value=_fake_analyze_result(work_dir))
            apply_mock = mock.Mock(
                return_value=_fake_apply_result(
                    work_dir,
                    src_image="docker.io/tensorflow/serving:latest",
                    dst_image="docker.io/tensorflow/serving:latest-optimized",
                )
            )
            benchmark_mock = mock.Mock(
                return_value=_fake_benchmark_result(
                    work_dir,
                    opt_image="docker.io/tensorflow/serving:latest-optimized",
                )
            )
            report_mock = mock.Mock(return_value=_fake_report_result(work_dir))

            argv = [
                "oneclick_product.py",
                "run",
                "docker.io/tensorflow/serving:latest",
                "--work-dir",
                str(work_dir),
                "--preset",
                "tf-serving-efficientnet-v2-b0",
                "--apply",
                "--benchmark",
            ]
            stdout = io.StringIO()
            with mock.patch.object(module, "run_preflight", preflight_mock):
                with mock.patch.object(module, "run_sample_stage", sample_mock):
                    with mock.patch.object(module, "run_export_enrich_stage", export_mock):
                        with mock.patch.object(module, "run_analyze_stage", analyze_mock):
                            with mock.patch.object(module, "run_apply_stage", apply_mock):
                                with mock.patch.object(module, "run_benchmark_stage", benchmark_mock):
                                    with mock.patch.object(module, "run_report_stage", report_mock):
                                        with mock.patch.object(module.sys, "argv", argv):
                                            with mock.patch.object(module.sys, "stdout", stdout):
                                                rc = module.main()

            self.assertEqual(0, rc)
            self.assertEqual("auto", preflight_mock.call_args.kwargs["startup_mode"])
            self.assertEqual("tf-serving-efficientnet-v2-b0", sample_mock.call_args.kwargs["workload_profile"])
            runtime_spec = sample_mock.call_args.kwargs["container_runtime"]
            self.assertFalse(
                any("/models/efficientnet_v2_b0" in mount for mount in runtime_spec.mounts),
            )
            self.assertFalse(any(env == "MODEL_NAME=efficientnet_v2_b0" for env in runtime_spec.env))
            self.assertFalse(any(env == "MODEL_BASE_PATH=/models" for env in runtime_spec.env))
            self.assertEqual("ready", benchmark_mock.call_args.kwargs["mode"])
            self.assertEqual(
                "tf-serving-efficientnet-v2-b0",
                benchmark_mock.call_args.kwargs["workload_profile"],
            )
            self.assertEqual("", benchmark_mock.call_args.kwargs["model_host_dir"])
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("tf-serving-efficientnet-v2-b0", manifest_payload["options"]["preset"])
            self.assertEqual(
                "tf-serving-efficientnet-v2-b0",
                manifest_payload["options"]["sample_workload"],
            )
            self.assertEqual(
                "tf-serving-efficientnet-v2-b0",
                manifest_payload["options"]["benchmark_workload"],
            )
            self.assertEqual(
                "tf-serving-efficientnet-v2-b0",
                manifest_payload["options"]["run_parameter_resolution"]["workload"]["value"],
            )
            self.assertIn("workload=tf-serving-efficientnet-v2-b0", stdout.getvalue())
            self.assertIn("bench_mode=ready", stdout.getvalue())

    def test_run_cli_preset_tfpy_embedded_supplies_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            module = load_oneclick_product_module()

            preflight_mock = mock.Mock(return_value=_fake_preflight_result(work_dir))
            sample_mock = mock.Mock(return_value=_fake_sample_result(work_dir))
            export_mock = mock.Mock(return_value=_fake_export_result(work_dir))
            analyze_mock = mock.Mock(return_value=_fake_analyze_result(work_dir))
            apply_mock = mock.Mock(
                return_value=_fake_apply_result(
                    work_dir,
                    src_image="127.0.0.1:5001/abtest/tfpy-half-plus-two-io1g:noopt",
                    dst_image="127.0.0.1:5001/abtest/tfpy-half-plus-two-io1g:optimized",
                )
            )
            benchmark_mock = mock.Mock(
                return_value=_fake_benchmark_result(
                    work_dir,
                    opt_image="127.0.0.1:5001/abtest/tfpy-half-plus-two-io1g:optimized",
                )
            )
            report_mock = mock.Mock(return_value=_fake_report_result(work_dir))

            argv = [
                "oneclick_product.py",
                "run",
                "127.0.0.1:5001/abtest/tfpy-half-plus-two-io1g:noopt",
                "--work-dir",
                str(work_dir),
                "--preset",
                "tfpy-half-plus-two-io1g",
                "--apply",
                "--benchmark",
            ]
            stdout = io.StringIO()
            with mock.patch.object(module, "run_preflight", preflight_mock):
                with mock.patch.object(module, "run_sample_stage", sample_mock):
                    with mock.patch.object(module, "run_export_enrich_stage", export_mock):
                        with mock.patch.object(module, "run_analyze_stage", analyze_mock):
                            with mock.patch.object(module, "run_apply_stage", apply_mock):
                                with mock.patch.object(module, "run_benchmark_stage", benchmark_mock):
                                    with mock.patch.object(module, "run_report_stage", report_mock):
                                        with mock.patch.object(module.sys, "argv", argv):
                                            with mock.patch.object(module.sys, "stdout", stdout):
                                                rc = module.main()

            self.assertEqual(0, rc)
            self.assertEqual("auto", preflight_mock.call_args.kwargs["startup_mode"])
            self.assertEqual("tfpy-half-plus-two-io1g", sample_mock.call_args.kwargs["workload_profile"])
            self.assertTrue(sample_mock.call_args.kwargs["self_starting_service"])
            runtime_spec = sample_mock.call_args.kwargs["container_runtime"]
            self.assertFalse(runtime_spec.mounts)
            self.assertFalse(any(env.startswith("MODEL_NAME=") for env in runtime_spec.env))
            self.assertFalse(any(env.startswith("MODEL_BASE_PATH=") for env in runtime_spec.env))
            self.assertEqual("ready", benchmark_mock.call_args.kwargs["mode"])
            self.assertEqual("tfpy-half-plus-two-io1g", benchmark_mock.call_args.kwargs["workload_profile"])
            self.assertTrue(benchmark_mock.call_args.kwargs["self_starting_service"])
            self.assertEqual("", benchmark_mock.call_args.kwargs["model_host_dir"])
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("tfpy-half-plus-two-io1g", manifest_payload["options"]["preset"])
            self.assertEqual("tfpy-half-plus-two-io1g", manifest_payload["options"]["sample_workload"])
            self.assertEqual("tfpy-half-plus-two-io1g", manifest_payload["options"]["benchmark_workload"])
            self.assertIn("workload=tfpy-half-plus-two-io1g", stdout.getvalue())
            self.assertIn("bench_mode=ready", stdout.getvalue())

    def test_run_cli_ready_serving_bootstrap_v2_defaults_benchmark_mode_to_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            module = load_oneclick_product_module()

            preflight_mock = mock.Mock(return_value=_fake_preflight_result(work_dir))
            sample_mock = mock.Mock(return_value=_fake_sample_result(work_dir))
            export_mock = mock.Mock(return_value=_fake_export_result(work_dir))
            analyze_mock = mock.Mock(return_value=_fake_analyze_result(work_dir))
            apply_mock = mock.Mock(
                return_value=_fake_apply_result(
                    work_dir,
                    src_image="127.0.0.1:5001/demo/ready-serving-bootstrap-v2:base",
                    dst_image="127.0.0.1:5001/demo/ready-serving-bootstrap-v2:base-optimized",
                )
            )
            benchmark_mock = mock.Mock(
                return_value=SimpleNamespace(
                    env_check_base_path=work_dir / "env_check_benchmark_base.json",
                    env_check_opt_path=work_dir / "env_check_benchmark_opt.json",
                    policy_path=work_dir / "benchmark_policy.json",
                    stage_log_path=work_dir / "benchmark.log",
                    csv_path=work_dir / "coldstart_compare.csv",
                    log_dir=work_dir / "bench_logs",
                    result_path=work_dir / "benchmark_result.json",
                    valid=True,
                    invalid_reasons=[],
                    warnings=[],
                    summary_payload={},
                )
            )
            report_mock = mock.Mock(return_value=_fake_report_result(work_dir))

            argv = [
                "oneclick_product.py",
                "run",
                "127.0.0.1:5001/demo/ready-serving-bootstrap-v2:base",
                "--work-dir",
                str(work_dir),
                "--workload",
                "ready-serving-bootstrap-v2",
                "--apply",
                "--benchmark",
            ]
            stdout = io.StringIO()
            with mock.patch.object(module, "run_preflight", preflight_mock):
                with mock.patch.object(module, "run_sample_stage", sample_mock):
                    with mock.patch.object(module, "run_export_enrich_stage", export_mock):
                        with mock.patch.object(module, "run_analyze_stage", analyze_mock):
                            with mock.patch.object(module, "run_apply_stage", apply_mock):
                                with mock.patch.object(module, "run_benchmark_stage", benchmark_mock):
                                    with mock.patch.object(module, "run_report_stage", report_mock):
                                        with mock.patch.object(module.sys, "argv", argv):
                                            with mock.patch.object(module.sys, "stdout", stdout):
                                                rc = module.main()

            self.assertEqual(0, rc)
            self.assertEqual("ready-serving-bootstrap-v2", sample_mock.call_args.kwargs["workload_profile"])
            self.assertTrue(sample_mock.call_args.kwargs["self_starting_service"])
            runtime_spec = sample_mock.call_args.kwargs["container_runtime"]
            self.assertFalse(runtime_spec.mounts)
            self.assertFalse(any(env.startswith("MODEL_NAME=") for env in runtime_spec.env))
            self.assertFalse(any(env.startswith("MODEL_BASE_PATH=") for env in runtime_spec.env))
            self.assertEqual("ready", benchmark_mock.call_args.kwargs["mode"])
            self.assertEqual("ready-serving-bootstrap-v2", benchmark_mock.call_args.kwargs["workload_profile"])
            self.assertTrue(benchmark_mock.call_args.kwargs["self_starting_service"])
            self.assertEqual("", benchmark_mock.call_args.kwargs["model_host_dir"])
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("ready-serving-bootstrap-v2", manifest_payload["options"]["sample_workload"])
            self.assertEqual("ready-serving-bootstrap-v2", manifest_payload["options"]["benchmark_workload"])
            self.assertEqual(
                "ready",
                manifest_payload["options"]["run_parameter_resolution"]["bench_mode"]["value"],
            )
            self.assertIn("workload=ready-serving-bootstrap-v2", stdout.getvalue())
            self.assertIn("bench_mode=ready", stdout.getvalue())

    def test_run_cli_ready_serving_layout_trap_v3_defaults_benchmark_mode_to_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            module = load_oneclick_product_module()

            preflight_mock = mock.Mock(return_value=_fake_preflight_result(work_dir))
            sample_mock = mock.Mock(return_value=_fake_sample_result(work_dir))
            export_mock = mock.Mock(return_value=_fake_export_result(work_dir))
            analyze_mock = mock.Mock(return_value=_fake_analyze_result(work_dir))
            apply_mock = mock.Mock(
                return_value=_fake_apply_result(
                    work_dir,
                    src_image="127.0.0.1:5001/abtest/ready-serving-layout-trap-v3:esgz",
                    dst_image="127.0.0.1:5001/abtest/ready-serving-layout-trap-v3:optimized",
                )
            )
            benchmark_mock = mock.Mock(
                return_value=SimpleNamespace(
                    env_check_base_path=work_dir / "env_check_benchmark_base.json",
                    env_check_opt_path=work_dir / "env_check_benchmark_opt.json",
                    policy_path=work_dir / "benchmark_policy.json",
                    stage_log_path=work_dir / "benchmark.log",
                    csv_path=work_dir / "coldstart_compare.csv",
                    log_dir=work_dir / "bench_logs",
                    result_path=work_dir / "benchmark_result.json",
                    valid=True,
                    invalid_reasons=[],
                    warnings=[],
                    summary_payload={},
                )
            )
            report_mock = mock.Mock(return_value=_fake_report_result(work_dir))

            argv = [
                "oneclick_product.py",
                "run",
                "127.0.0.1:5001/abtest/ready-serving-layout-trap-v3:esgz",
                "--work-dir",
                str(work_dir),
                "--workload",
                "ready-serving-layout-trap-v3",
                "--apply",
                "--benchmark",
            ]
            stdout = io.StringIO()
            with mock.patch.object(module, "run_preflight", preflight_mock):
                with mock.patch.object(module, "run_sample_stage", sample_mock):
                    with mock.patch.object(module, "run_export_enrich_stage", export_mock):
                        with mock.patch.object(module, "run_analyze_stage", analyze_mock):
                            with mock.patch.object(module, "run_apply_stage", apply_mock):
                                with mock.patch.object(module, "run_benchmark_stage", benchmark_mock):
                                    with mock.patch.object(module, "run_report_stage", report_mock):
                                        with mock.patch.object(module.sys, "argv", argv):
                                            with mock.patch.object(module.sys, "stdout", stdout):
                                                rc = module.main()

            self.assertEqual(0, rc)
            self.assertEqual("ready-serving-layout-trap-v3", sample_mock.call_args.kwargs["workload_profile"])
            self.assertTrue(sample_mock.call_args.kwargs["self_starting_service"])
            runtime_spec = sample_mock.call_args.kwargs["container_runtime"]
            self.assertFalse(runtime_spec.mounts)
            self.assertFalse(any(env.startswith("MODEL_NAME=") for env in runtime_spec.env))
            self.assertFalse(any(env.startswith("MODEL_BASE_PATH=") for env in runtime_spec.env))
            self.assertEqual("ready", benchmark_mock.call_args.kwargs["mode"])
            self.assertEqual("ready-serving-layout-trap-v3", benchmark_mock.call_args.kwargs["workload_profile"])
            self.assertTrue(benchmark_mock.call_args.kwargs["self_starting_service"])
            self.assertEqual("", benchmark_mock.call_args.kwargs["model_host_dir"])
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("ready-serving-layout-trap-v3", manifest_payload["options"]["sample_workload"])
            self.assertEqual("ready-serving-layout-trap-v3", manifest_payload["options"]["benchmark_workload"])
            self.assertEqual(
                "ready",
                manifest_payload["options"]["run_parameter_resolution"]["bench_mode"]["value"],
            )
            self.assertIn("workload=ready-serving-layout-trap-v3", stdout.getvalue())
            self.assertIn("bench_mode=ready", stdout.getvalue())

    def test_run_cli_ready_serving_layout_trap_v4_defaults_benchmark_mode_to_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            module = load_oneclick_product_module()

            preflight_mock = mock.Mock(return_value=_fake_preflight_result(work_dir))
            sample_mock = mock.Mock(return_value=_fake_sample_result(work_dir))
            export_mock = mock.Mock(return_value=_fake_export_result(work_dir))
            analyze_mock = mock.Mock(return_value=_fake_analyze_result(work_dir))
            apply_mock = mock.Mock(
                return_value=_fake_apply_result(
                    work_dir,
                    src_image="127.0.0.1:5001/abtest/ready-serving-layout-trap-v4:esgz-default",
                    dst_image="127.0.0.1:5001/abtest/ready-serving-layout-trap-v4:optimized",
                )
            )
            benchmark_mock = mock.Mock(
                return_value=SimpleNamespace(
                    env_check_base_path=work_dir / "env_check_benchmark_base.json",
                    env_check_opt_path=work_dir / "env_check_benchmark_opt.json",
                    policy_path=work_dir / "benchmark_policy.json",
                    stage_log_path=work_dir / "benchmark.log",
                    csv_path=work_dir / "coldstart_compare.csv",
                    log_dir=work_dir / "bench_logs",
                    result_path=work_dir / "benchmark_result.json",
                    valid=True,
                    invalid_reasons=[],
                    warnings=[],
                    summary_payload={},
                )
            )
            report_mock = mock.Mock(return_value=_fake_report_result(work_dir))

            argv = [
                "oneclick_product.py",
                "run",
                "127.0.0.1:5001/abtest/ready-serving-layout-trap-v4:esgz-default",
                "--work-dir",
                str(work_dir),
                "--workload",
                "ready-serving-layout-trap-v4",
                "--apply",
                "--benchmark",
            ]
            stdout = io.StringIO()
            with mock.patch.object(module, "run_preflight", preflight_mock):
                with mock.patch.object(module, "run_sample_stage", sample_mock):
                    with mock.patch.object(module, "run_export_enrich_stage", export_mock):
                        with mock.patch.object(module, "run_analyze_stage", analyze_mock):
                            with mock.patch.object(module, "run_apply_stage", apply_mock):
                                with mock.patch.object(module, "run_benchmark_stage", benchmark_mock):
                                    with mock.patch.object(module, "run_report_stage", report_mock):
                                        with mock.patch.object(module.sys, "argv", argv):
                                            with mock.patch.object(module.sys, "stdout", stdout):
                                                rc = module.main()

            self.assertEqual(0, rc)
            self.assertEqual("ready-serving-layout-trap-v4", sample_mock.call_args.kwargs["workload_profile"])
            self.assertTrue(sample_mock.call_args.kwargs["self_starting_service"])
            runtime_spec = sample_mock.call_args.kwargs["container_runtime"]
            self.assertFalse(runtime_spec.mounts)
            self.assertFalse(any(env.startswith("MODEL_NAME=") for env in runtime_spec.env))
            self.assertFalse(any(env.startswith("MODEL_BASE_PATH=") for env in runtime_spec.env))
            self.assertEqual("ready", benchmark_mock.call_args.kwargs["mode"])
            self.assertEqual("ready-serving-layout-trap-v4", benchmark_mock.call_args.kwargs["workload_profile"])
            self.assertTrue(benchmark_mock.call_args.kwargs["self_starting_service"])
            self.assertEqual(
                "http://127.0.0.1:8000/debug/state",
                benchmark_mock.call_args.kwargs["debug_state_url"],
            )
            self.assertEqual("", benchmark_mock.call_args.kwargs["model_host_dir"])
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("ready-serving-layout-trap-v4", manifest_payload["options"]["sample_workload"])
            self.assertEqual("ready-serving-layout-trap-v4", manifest_payload["options"]["benchmark_workload"])
            self.assertEqual(
                "ready",
                manifest_payload["options"]["run_parameter_resolution"]["bench_mode"]["value"],
            )
            self.assertIn("workload=ready-serving-layout-trap-v4", stdout.getvalue())
            self.assertIn("bench_mode=ready", stdout.getvalue())

    def test_run_cli_ready_serving_layout_trap_v5_defaults_benchmark_mode_to_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            module = load_oneclick_product_module()

            preflight_mock = mock.Mock(return_value=_fake_preflight_result(work_dir))
            sample_mock = mock.Mock(return_value=_fake_sample_result(work_dir))
            export_mock = mock.Mock(return_value=_fake_export_result(work_dir))
            analyze_mock = mock.Mock(return_value=_fake_analyze_result(work_dir))
            apply_mock = mock.Mock(
                return_value=_fake_apply_result(
                    work_dir,
                    src_image="127.0.0.1:5001/abtest/ready-serving-layout-trap-v5:src-raw",
                    dst_image="127.0.0.1:5001/abtest/ready-serving-layout-trap-v5:optimized",
                )
            )
            benchmark_mock = mock.Mock(
                return_value=SimpleNamespace(
                    env_check_base_path=work_dir / "env_check_benchmark_base.json",
                    env_check_opt_path=work_dir / "env_check_benchmark_opt.json",
                    policy_path=work_dir / "benchmark_policy.json",
                    stage_log_path=work_dir / "benchmark.log",
                    csv_path=work_dir / "coldstart_compare.csv",
                    log_dir=work_dir / "bench_logs",
                    result_path=work_dir / "benchmark_result.json",
                    valid=True,
                    invalid_reasons=[],
                    warnings=[],
                    summary_payload={},
                )
            )
            report_mock = mock.Mock(return_value=_fake_report_result(work_dir))

            argv = [
                "oneclick_product.py",
                "run",
                "127.0.0.1:5001/abtest/ready-serving-layout-trap-v5:src-raw",
                "--work-dir",
                str(work_dir),
                "--workload",
                "ready-serving-layout-trap-v5",
                "--apply",
                "--benchmark",
            ]
            stdout = io.StringIO()
            with mock.patch.object(module, "run_preflight", preflight_mock):
                with mock.patch.object(module, "run_sample_stage", sample_mock):
                    with mock.patch.object(module, "run_export_enrich_stage", export_mock):
                        with mock.patch.object(module, "run_analyze_stage", analyze_mock):
                            with mock.patch.object(module, "run_apply_stage", apply_mock):
                                with mock.patch.object(module, "run_benchmark_stage", benchmark_mock):
                                    with mock.patch.object(module, "run_report_stage", report_mock):
                                        with mock.patch.object(module.sys, "argv", argv):
                                            with mock.patch.object(module.sys, "stdout", stdout):
                                                rc = module.main()

            self.assertEqual(0, rc)
            self.assertEqual("ready-serving-layout-trap-v5", sample_mock.call_args.kwargs["workload_profile"])
            self.assertTrue(sample_mock.call_args.kwargs["self_starting_service"])
            runtime_spec = sample_mock.call_args.kwargs["container_runtime"]
            self.assertFalse(runtime_spec.mounts)
            self.assertFalse(any(env.startswith("MODEL_NAME=") for env in runtime_spec.env))
            self.assertFalse(any(env.startswith("MODEL_BASE_PATH=") for env in runtime_spec.env))
            self.assertEqual("ready", benchmark_mock.call_args.kwargs["mode"])
            self.assertEqual("ready-serving-layout-trap-v5", benchmark_mock.call_args.kwargs["workload_profile"])
            self.assertTrue(benchmark_mock.call_args.kwargs["self_starting_service"])
            self.assertEqual(
                "http://127.0.0.1:8000/debug/state",
                benchmark_mock.call_args.kwargs["debug_state_url"],
            )
            self.assertEqual("", benchmark_mock.call_args.kwargs["model_host_dir"])
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("ready-serving-layout-trap-v5", manifest_payload["options"]["sample_workload"])
            self.assertEqual("ready-serving-layout-trap-v5", manifest_payload["options"]["benchmark_workload"])
            self.assertEqual(
                "ready",
                manifest_payload["options"]["run_parameter_resolution"]["bench_mode"]["value"],
            )
            self.assertIn("workload=ready-serving-layout-trap-v5", stdout.getvalue())
            self.assertIn("bench_mode=ready", stdout.getvalue())

    def test_run_cli_bench_baseline_stargz_forwards_to_benchmark_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            module = load_oneclick_product_module()

            preflight_mock = mock.Mock(return_value=_fake_preflight_result(work_dir))
            sample_mock = mock.Mock(return_value=_fake_sample_result(work_dir))
            export_mock = mock.Mock(return_value=_fake_export_result(work_dir))
            analyze_mock = mock.Mock(return_value=_fake_analyze_result(work_dir))
            apply_mock = mock.Mock(
                return_value=_fake_apply_result(
                    work_dir,
                    src_image="docker.io/tensorflow/serving:latest",
                    dst_image="docker.io/tensorflow/serving:latest-optimized",
                )
            )
            benchmark_mock = mock.Mock(
                return_value=_fake_benchmark_result(
                    work_dir,
                    opt_image="docker.io/tensorflow/serving:latest-optimized",
                )
            )
            report_mock = mock.Mock(return_value=_fake_report_result(work_dir))

            argv = [
                "oneclick_product.py",
                "run",
                "docker.io/tensorflow/serving:latest",
                "--work-dir",
                str(work_dir),
                "--preset",
                "tf-serving-half-plus-two",
                "--apply",
                "--benchmark",
                "--bench-baseline-stargz",
            ]
            with mock.patch.object(module, "run_preflight", preflight_mock):
                with mock.patch.object(module, "run_sample_stage", sample_mock):
                    with mock.patch.object(module, "run_export_enrich_stage", export_mock):
                        with mock.patch.object(module, "run_analyze_stage", analyze_mock):
                            with mock.patch.object(module, "run_apply_stage", apply_mock):
                                with mock.patch.object(module, "run_benchmark_stage", benchmark_mock):
                                    with mock.patch.object(module, "run_report_stage", report_mock):
                                        with mock.patch.object(module.sys, "argv", argv):
                                            rc = module.main()

            self.assertEqual(0, rc)
            self.assertTrue(benchmark_mock.call_args.kwargs["baseline_stargz"])

    def test_run_cli_forwards_bench_cold_mode_to_benchmark_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            module = load_oneclick_product_module()

            preflight_mock = mock.Mock(return_value=_fake_preflight_result(work_dir))
            sample_mock = mock.Mock(return_value=_fake_sample_result(work_dir))
            export_mock = mock.Mock(return_value=_fake_export_result(work_dir))
            analyze_mock = mock.Mock(return_value=_fake_analyze_result(work_dir))
            apply_mock = mock.Mock(
                return_value=_fake_apply_result(
                    work_dir,
                    src_image="127.0.0.1:5001/demo/app:base",
                    dst_image="127.0.0.1:5001/demo/app:opt",
                )
            )
            benchmark_mock = mock.Mock(
                return_value=_fake_benchmark_result(
                    work_dir,
                    opt_image="127.0.0.1:5001/demo/app:opt",
                    cold_mode="registry-cold-strict",
                )
            )
            report_mock = mock.Mock(return_value=_fake_report_result(work_dir))

            argv = [
                "oneclick_product.py",
                "run",
                "127.0.0.1:5001/demo/app:base",
                "--work-dir",
                str(work_dir),
                "--benchmark",
                "--apply",
                "--bench-workload",
                "tf-serving-half-plus-two",
                "--bench-cold-mode",
                "registry-cold-strict",
            ]
            with mock.patch.object(module, "run_preflight", preflight_mock):
                with mock.patch.object(module, "run_sample_stage", sample_mock):
                    with mock.patch.object(module, "run_export_enrich_stage", export_mock):
                        with mock.patch.object(module, "run_analyze_stage", analyze_mock):
                            with mock.patch.object(module, "run_apply_stage", apply_mock):
                                with mock.patch.object(module, "run_benchmark_stage", benchmark_mock):
                                    with mock.patch.object(module, "run_report_stage", report_mock):
                                        with mock.patch.object(module.sys, "argv", argv):
                                            rc = module.main()

            self.assertEqual(0, rc)
            self.assertEqual("registry-cold-strict", benchmark_mock.call_args.kwargs["cold_mode"])

    def test_run_cli_raw_vs_estargz_defaults_snapshotters_for_benchmark_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            module = load_oneclick_product_module()

            preflight_mock = mock.Mock(return_value=_fake_preflight_result(work_dir))
            sample_mock = mock.Mock(return_value=_fake_sample_result(work_dir))
            export_mock = mock.Mock(return_value=_fake_export_result(work_dir))
            analyze_mock = mock.Mock(return_value=_fake_analyze_result(work_dir))
            apply_mock = mock.Mock(
                return_value=_fake_apply_result(
                    work_dir,
                    src_image="127.0.0.1:5001/abtest/ready-serving-layout-trap-v4:src-raw",
                    dst_image="127.0.0.1:5001/abtest/ready-serving-layout-trap-v4:esgz-hinted-v2",
                )
            )
            benchmark_mock = mock.Mock(
                return_value=_fake_benchmark_result(
                    work_dir,
                    opt_image="127.0.0.1:5001/abtest/ready-serving-layout-trap-v4:esgz-hinted-v2",
                )
            )
            report_mock = mock.Mock(return_value=_fake_report_result(work_dir))

            argv = [
                "oneclick_product.py",
                "run",
                "127.0.0.1:5001/abtest/ready-serving-layout-trap-v4:src-raw",
                "--work-dir",
                str(work_dir),
                "--workload",
                "ready-serving-layout-trap-v4",
                "--apply",
                "--benchmark",
                "--bench-comparison-mode",
                "raw-vs-estargz",
            ]
            with mock.patch.object(module, "run_preflight", preflight_mock):
                with mock.patch.object(module, "run_sample_stage", sample_mock):
                    with mock.patch.object(module, "run_export_enrich_stage", export_mock):
                        with mock.patch.object(module, "run_analyze_stage", analyze_mock):
                            with mock.patch.object(module, "run_apply_stage", apply_mock):
                                with mock.patch.object(module, "run_benchmark_stage", benchmark_mock):
                                    with mock.patch.object(module, "run_report_stage", report_mock):
                                        with mock.patch.object(module.sys, "argv", argv):
                                            rc = module.main()

            self.assertEqual(0, rc)
            self.assertEqual("raw-vs-estargz", benchmark_mock.call_args.kwargs["comparison_mode"])
            self.assertEqual("overlayfs", benchmark_mock.call_args.kwargs["base_snapshotter"])
            self.assertEqual("stargz", benchmark_mock.call_args.kwargs["opt_snapshotter"])
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("raw-vs-estargz", manifest_payload["options"]["benchmark_comparison_mode"])

    def test_run_cli_benchmark_triple_mode_forwards_bench_unopt_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260419_000101",
                work_dir=work_dir,
                image="127.0.0.1:5001/abtest/ready-serving-layout-trap-v4:src-raw",
                startup_mode="legacy",
                llm_enabled=False,
            )
            module = load_oneclick_product_module()

            preflight_mock = mock.Mock(return_value=_fake_preflight_result(work_dir))
            sample_mock = mock.Mock(return_value=_fake_sample_result(work_dir))
            export_mock = mock.Mock(return_value=_fake_export_result(work_dir))
            analyze_mock = mock.Mock(return_value=_fake_analyze_result(work_dir))
            apply_mock = mock.Mock(
                return_value=_fake_apply_result(
                    work_dir,
                    src_image="127.0.0.1:5001/abtest/ready-serving-layout-trap-v4:src-raw",
                    dst_image="127.0.0.1:5001/abtest/ready-serving-layout-trap-v4:esgz-hinted-v2",
                )
            )
            benchmark_mock = mock.Mock(
                return_value=_fake_benchmark_result(
                    work_dir,
                    opt_image="127.0.0.1:5001/abtest/ready-serving-layout-trap-v4:esgz-hinted-v2",
                )
            )
            report_mock = mock.Mock(return_value=_fake_report_result(work_dir))

            argv = [
                "oneclick_product.py",
                "run",
                "127.0.0.1:5001/abtest/ready-serving-layout-trap-v4:src-raw",
                "--work-dir",
                str(work_dir),
                "--workload",
                "ready-serving-layout-trap-v4",
                "--apply",
                "--benchmark",
                "--bench-comparison-mode",
                "raw-full-vs-estargz-lazy-triple",
                "--bench-unopt-image",
                "127.0.0.1:5001/abtest/ready-serving-layout-trap-v4:esgz-unopt-v2",
            ]
            with mock.patch.object(module, "run_preflight", preflight_mock):
                with mock.patch.object(module, "run_sample_stage", sample_mock):
                    with mock.patch.object(module, "run_export_enrich_stage", export_mock):
                        with mock.patch.object(module, "run_analyze_stage", analyze_mock):
                            with mock.patch.object(module, "run_apply_stage", apply_mock):
                                with mock.patch.object(module, "run_benchmark_stage", benchmark_mock):
                                    with mock.patch.object(module, "run_report_stage", report_mock):
                                        with mock.patch.object(module.sys, "argv", argv):
                                            rc = module.main()

            self.assertEqual(0, rc)
            self.assertEqual(
                "raw-full-vs-estargz-lazy-triple",
                benchmark_mock.call_args.kwargs["comparison_mode"],
            )
            self.assertEqual(
                "127.0.0.1:5001/abtest/ready-serving-layout-trap-v4:esgz-unopt-v2",
                benchmark_mock.call_args.kwargs["unopt_image"],
            )
            self.assertEqual("overlayfs", benchmark_mock.call_args.kwargs["base_snapshotter"])
            self.assertEqual("stargz", benchmark_mock.call_args.kwargs["opt_snapshotter"])
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                "raw-full-vs-estargz-lazy-triple",
                manifest_payload["options"]["benchmark_comparison_mode"],
            )

    def test_run_cli_config_supplies_defaults_and_works_with_preset_and_inheritance(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            model_dir = work_dir / "model"
            model_dir.mkdir()
            config_path = work_dir / "run.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "preset: tf-serving-half-plus-two",
                        f'model_host: "{model_dir}"',
                        "mode: deterministic",
                        "apply: true",
                        "benchmark: true",
                        "bench_runs: 4",
                        "duration: 123",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            module = load_oneclick_product_module()

            preflight_mock = mock.Mock(return_value=_fake_preflight_result(work_dir))
            sample_mock = mock.Mock(return_value=_fake_sample_result(work_dir))
            export_mock = mock.Mock(return_value=_fake_export_result(work_dir))
            analyze_mock = mock.Mock(return_value=_fake_analyze_result(work_dir))
            apply_mock = mock.Mock(
                return_value=_fake_apply_result(
                    work_dir,
                    src_image="docker.io/tensorflow/serving:latest",
                    dst_image="docker.io/tensorflow/serving:latest-optimized",
                )
            )
            benchmark_mock = mock.Mock(
                return_value=_fake_benchmark_result(
                    work_dir,
                    opt_image="docker.io/tensorflow/serving:latest-optimized",
                )
            )
            report_mock = mock.Mock(return_value=_fake_report_result(work_dir))

            argv = [
                "oneclick_product.py",
                "run",
                "docker.io/tensorflow/serving:latest",
                "--work-dir",
                str(work_dir),
                "--config",
                str(config_path),
            ]
            stdout = io.StringIO()
            with mock.patch.object(module, "run_preflight", preflight_mock):
                with mock.patch.object(module, "run_sample_stage", sample_mock):
                    with mock.patch.object(module, "run_export_enrich_stage", export_mock):
                        with mock.patch.object(module, "run_analyze_stage", analyze_mock):
                            with mock.patch.object(module, "run_apply_stage", apply_mock):
                                with mock.patch.object(module, "run_benchmark_stage", benchmark_mock):
                                    with mock.patch.object(module, "run_report_stage", report_mock):
                                        with mock.patch.object(module.sys, "argv", argv):
                                            with mock.patch.object(module.sys, "stdout", stdout):
                                                rc = module.main()

            self.assertEqual(0, rc)
            self.assertEqual("auto", preflight_mock.call_args.kwargs["startup_mode"])
            self.assertEqual(123, sample_mock.call_args.kwargs["duration"])
            self.assertEqual("tf-serving-half-plus-two", sample_mock.call_args.kwargs["workload_profile"])
            self.assertEqual("docker.io/tensorflow/serving:latest-optimized", apply_mock.call_args.kwargs["dst_image"])
            self.assertEqual(4, benchmark_mock.call_args.kwargs["runs"])
            self.assertEqual("ready", benchmark_mock.call_args.kwargs["mode"])
            self.assertEqual(str(model_dir), benchmark_mock.call_args.kwargs["model_host_dir"])
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(str(config_path.resolve()), manifest_payload["options"]["config"])
            self.assertEqual("tf-serving-half-plus-two", manifest_payload["options"]["preset"])
            self.assertEqual(
                "deterministic",
                manifest_payload["options"]["run_parameter_resolution"]["mode"]["value"],
            )
            self.assertEqual(
                "123",
                manifest_payload["options"]["run_parameter_resolution"]["duration"]["value"],
            )
            self.assertEqual(
                "4",
                manifest_payload["options"]["run_parameter_resolution"]["bench_runs"]["value"],
            )
            self.assertEqual(
                "ready",
                manifest_payload["options"]["run_parameter_resolution"]["bench_mode"]["value"],
            )
            self.assertIn('"config":', json.dumps(manifest_payload["options"], ensure_ascii=False))
            self.assertIn("duration=123", stdout.getvalue())
            self.assertIn("bench_runs=4", stdout.getvalue())

    def test_run_cli_config_can_override_workload_ready_timeouts(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            config_path = work_dir / "run.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "preset: tf-serving-half-plus-two",
                        "mode: deterministic",
                        "apply: true",
                        "benchmark: true",
                        "ready_timeout: 75",
                        "bench_timeout: 300",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            module = load_oneclick_product_module()

            preflight_mock = mock.Mock(return_value=_fake_preflight_result(work_dir))
            sample_mock = mock.Mock(return_value=_fake_sample_result(work_dir))
            export_mock = mock.Mock(return_value=_fake_export_result(work_dir))
            analyze_mock = mock.Mock(return_value=_fake_analyze_result(work_dir))
            apply_mock = mock.Mock(
                return_value=_fake_apply_result(
                    work_dir,
                    src_image="docker.io/tensorflow/serving:latest",
                    dst_image="docker.io/tensorflow/serving:latest-optimized",
                )
            )
            benchmark_mock = mock.Mock(
                return_value=_fake_benchmark_result(
                    work_dir,
                    opt_image="docker.io/tensorflow/serving:latest-optimized",
                )
            )
            report_mock = mock.Mock(return_value=_fake_report_result(work_dir))

            argv = [
                "oneclick_product.py",
                "run",
                "docker.io/tensorflow/serving:latest",
                "--work-dir",
                str(work_dir),
                "--config",
                str(config_path),
            ]
            with mock.patch.object(module, "run_preflight", preflight_mock):
                with mock.patch.object(module, "run_sample_stage", sample_mock):
                    with mock.patch.object(module, "run_export_enrich_stage", export_mock):
                        with mock.patch.object(module, "run_analyze_stage", analyze_mock):
                            with mock.patch.object(module, "run_apply_stage", apply_mock):
                                with mock.patch.object(module, "run_benchmark_stage", benchmark_mock):
                                    with mock.patch.object(module, "run_report_stage", report_mock):
                                        with mock.patch.object(module.sys, "argv", argv):
                                            rc = module.main()

            self.assertEqual(0, rc)
            self.assertEqual(75, sample_mock.call_args.kwargs["ready_timeout"])
            self.assertEqual(300, benchmark_mock.call_args.kwargs["timeout_ready"])
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                "75",
                manifest_payload["options"]["run_parameter_resolution"]["ready_timeout"]["value"],
            )
            self.assertEqual(
                "300",
                manifest_payload["options"]["run_parameter_resolution"]["bench_timeout"]["value"],
            )

    def test_run_cli_auto_inherits_benchmark_parameters_and_generates_dst_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            model_dir = work_dir / "model"
            model_dir.mkdir()
            module = load_oneclick_product_module()

            preflight_mock = mock.Mock(return_value=_fake_preflight_result(work_dir))
            sample_mock = mock.Mock(return_value=_fake_sample_result(work_dir))
            export_mock = mock.Mock(return_value=_fake_export_result(work_dir))
            analyze_mock = mock.Mock(return_value=_fake_analyze_result(work_dir))
            apply_mock = mock.Mock(
                return_value=_fake_apply_result(
                    work_dir,
                    src_image="docker.io/tensorflow/serving:latest",
                    dst_image="docker.io/tensorflow/serving:latest-optimized",
                )
            )
            benchmark_mock = mock.Mock(
                return_value=_fake_benchmark_result(
                    work_dir,
                    opt_image="docker.io/tensorflow/serving:latest-optimized",
                )
            )
            report_mock = mock.Mock(return_value=_fake_report_result(work_dir))

            argv = [
                "oneclick_product.py",
                "run",
                "docker.io/tensorflow/serving:latest",
                "--work-dir",
                str(work_dir),
                "--workload",
                "tf-serving-half-plus-two",
                "--model-host",
                str(model_dir),
                "--apply",
                "--benchmark",
            ]
            stdout = io.StringIO()
            with mock.patch.object(module, "run_preflight", preflight_mock):
                with mock.patch.object(module, "run_sample_stage", sample_mock):
                    with mock.patch.object(module, "run_export_enrich_stage", export_mock):
                        with mock.patch.object(module, "run_analyze_stage", analyze_mock):
                            with mock.patch.object(module, "run_apply_stage", apply_mock):
                                with mock.patch.object(module, "run_benchmark_stage", benchmark_mock):
                                    with mock.patch.object(module, "run_report_stage", report_mock):
                                        with mock.patch.object(module.sys, "argv", argv):
                                            with mock.patch.object(module.sys, "stdout", stdout):
                                                rc = module.main()

            self.assertEqual(0, rc)
            self.assertEqual(
                "docker.io/tensorflow/serving:latest-optimized",
                apply_mock.call_args.kwargs["dst_image"],
            )
            self.assertEqual("ready", benchmark_mock.call_args.kwargs["mode"])
            self.assertEqual(
                "tf-serving-half-plus-two",
                benchmark_mock.call_args.kwargs["workload_profile"],
            )
            self.assertEqual(str(model_dir), benchmark_mock.call_args.kwargs["model_host_dir"])
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("tf-serving-half-plus-two", manifest_payload["options"]["benchmark_workload"])
            self.assertEqual(
                "docker.io/tensorflow/serving:latest-optimized",
                manifest_payload["options"]["run_parameter_resolution"]["dst_image"]["value"],
            )
            self.assertEqual(
                "ready",
                manifest_payload["options"]["run_parameter_resolution"]["bench_mode"]["value"],
            )
            self.assertEqual(
                "tf-serving-half-plus-two",
                manifest_payload["options"]["run_parameter_resolution"]["bench_workload"]["value"],
            )
            self.assertEqual(
                str(model_dir),
                manifest_payload["options"]["run_parameter_resolution"]["bench_model_host"]["value"],
            )
            self.assertIn("run-args=normalized", stdout.getvalue())
            self.assertIn("bench_mode=ready", stdout.getvalue())
            self.assertIn("dst_image=docker.io/tensorflow/serving:latest-optimized", stdout.getvalue())

    def test_run_cli_explicit_parameters_override_preset_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            model_dir = work_dir / "model"
            model_dir.mkdir()
            module = load_oneclick_product_module()

            preflight_mock = mock.Mock(return_value=_fake_preflight_result(work_dir))
            sample_mock = mock.Mock(return_value=_fake_sample_result(work_dir))
            export_mock = mock.Mock(return_value=_fake_export_result(work_dir))
            analyze_mock = mock.Mock(return_value=_fake_analyze_result(work_dir))
            apply_mock = mock.Mock(
                return_value=_fake_apply_result(
                    work_dir,
                    src_image="docker.io/tensorflow/serving:latest",
                    dst_image="docker.io/custom/tf-serving:manual",
                )
            )
            benchmark_mock = mock.Mock(
                return_value=_fake_benchmark_result(
                    work_dir,
                    opt_image="docker.io/custom/tf-serving:manual",
                )
            )
            report_mock = mock.Mock(return_value=_fake_report_result(work_dir))

            argv = [
                "oneclick_product.py",
                "run",
                "docker.io/tensorflow/serving:latest",
                "--work-dir",
                str(work_dir),
                "--preset",
                "tf-serving-half-plus-two",
                "--workload",
                "none",
                "--startup-mode",
                "legacy",
                "--model-host",
                str(model_dir),
                "--apply",
                "--dst-image",
                "docker.io/custom/tf-serving:manual",
                "--benchmark",
                "--bench-workload",
                "none",
                "--bench-mode",
                "struct",
            ]
            stdout = io.StringIO()
            with mock.patch.object(module, "run_preflight", preflight_mock):
                with mock.patch.object(module, "run_sample_stage", sample_mock):
                    with mock.patch.object(module, "run_export_enrich_stage", export_mock):
                        with mock.patch.object(module, "run_analyze_stage", analyze_mock):
                            with mock.patch.object(module, "run_apply_stage", apply_mock):
                                with mock.patch.object(module, "run_benchmark_stage", benchmark_mock):
                                    with mock.patch.object(module, "run_report_stage", report_mock):
                                        with mock.patch.object(module.sys, "argv", argv):
                                            with mock.patch.object(module.sys, "stdout", stdout):
                                                rc = module.main()

            self.assertEqual(0, rc)
            self.assertEqual("legacy", preflight_mock.call_args.kwargs["startup_mode"])
            self.assertEqual("none", sample_mock.call_args.kwargs["workload_profile"])
            self.assertEqual("docker.io/custom/tf-serving:manual", apply_mock.call_args.kwargs["dst_image"])
            self.assertEqual("struct", benchmark_mock.call_args.kwargs["mode"])
            self.assertEqual("none", benchmark_mock.call_args.kwargs["workload_profile"])
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("tf-serving-half-plus-two", manifest_payload["options"]["preset"])
            self.assertEqual("none", manifest_payload["options"]["sample_workload"])
            self.assertEqual("none", manifest_payload["options"]["benchmark_workload"])
            self.assertNotIn("workload=", stdout.getvalue())
            self.assertNotIn("startup_mode=", stdout.getvalue())

    def test_run_cli_explicit_parameters_override_config_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            config_model_dir = work_dir / "config-model"
            cli_model_dir = work_dir / "cli-model"
            config_model_dir.mkdir()
            cli_model_dir.mkdir()
            config_path = work_dir / "run.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "preset: tf-serving-half-plus-two",
                        "workload: tf-serving-half-plus-two",
                        "startup_mode: auto",
                        f'model_host: "{config_model_dir}"',
                        "apply: true",
                        "benchmark: true",
                        "bench_runs: 4",
                        "bench_mode: ready",
                        'dst_image: "docker.io/config/tf-serving:from-config"',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            module = load_oneclick_product_module()

            preflight_mock = mock.Mock(return_value=_fake_preflight_result(work_dir))
            sample_mock = mock.Mock(return_value=_fake_sample_result(work_dir))
            export_mock = mock.Mock(return_value=_fake_export_result(work_dir))
            analyze_mock = mock.Mock(return_value=_fake_analyze_result(work_dir))
            apply_mock = mock.Mock(
                return_value=_fake_apply_result(
                    work_dir,
                    src_image="docker.io/tensorflow/serving:latest",
                    dst_image="docker.io/cli/tf-serving:manual",
                )
            )
            benchmark_mock = mock.Mock(
                return_value=_fake_benchmark_result(
                    work_dir,
                    opt_image="docker.io/cli/tf-serving:manual",
                )
            )
            report_mock = mock.Mock(return_value=_fake_report_result(work_dir))

            argv = [
                "oneclick_product.py",
                "run",
                "docker.io/tensorflow/serving:latest",
                "--work-dir",
                str(work_dir),
                "--config",
                str(config_path),
                "--workload",
                "none",
                "--startup-mode",
                "legacy",
                "--model-host",
                str(cli_model_dir),
                "--dst-image",
                "docker.io/cli/tf-serving:manual",
                "--bench-runs",
                "7",
                "--bench-mode",
                "struct",
                "--apply",
                "--benchmark",
            ]
            stdout = io.StringIO()
            with mock.patch.object(module, "run_preflight", preflight_mock):
                with mock.patch.object(module, "run_sample_stage", sample_mock):
                    with mock.patch.object(module, "run_export_enrich_stage", export_mock):
                        with mock.patch.object(module, "run_analyze_stage", analyze_mock):
                            with mock.patch.object(module, "run_apply_stage", apply_mock):
                                with mock.patch.object(module, "run_benchmark_stage", benchmark_mock):
                                    with mock.patch.object(module, "run_report_stage", report_mock):
                                        with mock.patch.object(module.sys, "argv", argv):
                                            with mock.patch.object(module.sys, "stdout", stdout):
                                                rc = module.main()

            self.assertEqual(0, rc)
            self.assertEqual("legacy", preflight_mock.call_args.kwargs["startup_mode"])
            self.assertEqual("none", sample_mock.call_args.kwargs["workload_profile"])
            self.assertEqual("docker.io/cli/tf-serving:manual", apply_mock.call_args.kwargs["dst_image"])
            self.assertEqual(7, benchmark_mock.call_args.kwargs["runs"])
            self.assertEqual("struct", benchmark_mock.call_args.kwargs["mode"])
            self.assertEqual(str(cli_model_dir), benchmark_mock.call_args.kwargs["model_host_dir"])
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(str(config_path.resolve()), manifest_payload["options"]["config"])
            self.assertNotIn("workload", manifest_payload["options"].get("run_parameter_resolution", {}))
            self.assertNotIn("startup_mode", manifest_payload["options"].get("run_parameter_resolution", {}))
            self.assertNotIn("dst_image", stdout.getvalue())

    def test_run_cli_config_can_supply_llm_provider_defaults_for_auto_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            config_path = work_dir / "run.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "mode: llm-auto",
                        "llm_provider: groq",
                        "llm_api_key_env: GROQ_API_KEY",
                        "llm_api_base: https://api.groq.com/openai/v1",
                        "llm_model: llama-3.1-8b-instant",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            module = load_oneclick_product_module()
            preflight_mock = mock.Mock(return_value=_fake_preflight_result(work_dir))
            sample_mock = mock.Mock(return_value=_fake_sample_result(work_dir))
            export_mock = mock.Mock(return_value=_fake_export_result(work_dir))
            analyze_mock = mock.Mock(return_value=_fake_analyze_result(work_dir))
            llm_mock = mock.Mock(
                return_value=_fake_llm_result(
                    work_dir,
                    mode="api",
                    llm_applied=True,
                    final_hints_source="grounded_llm",
                )
            )
            report_mock = mock.Mock(return_value=_fake_report_result(work_dir))
            argv = [
                "oneclick_product.py",
                "run",
                "docker.io/library/app:base",
                "--work-dir",
                str(work_dir),
                "--config",
                str(config_path),
            ]
            stdout = io.StringIO()
            with mock.patch.object(
                module,
                "resolve_llm_provider_config",
                return_value=_resolved_llm_config(
                    provider="groq",
                    api_base="https://api.groq.com/openai/v1",
                    api_base_source="provider_default:groq",
                    api_key_env="GROQ_API_KEY",
                    api_key_env_source="provider_default:groq",
                    api_key="demo-key",
                    api_key_source="GROQ_API_KEY",
                    model="llama-3.1-8b-instant",
                    model_source="provider_default:groq",
                ),
            ):
                with mock.patch.object(module, "run_preflight", preflight_mock):
                    with mock.patch.object(module, "run_sample_stage", sample_mock):
                        with mock.patch.object(module, "run_export_enrich_stage", export_mock):
                            with mock.patch.object(module, "run_analyze_stage", analyze_mock):
                                with mock.patch.object(module, "run_llm_optimize_stage", llm_mock):
                                    with mock.patch.object(module, "run_report_stage", report_mock):
                                        with mock.patch.object(module.sys, "argv", argv):
                                            with mock.patch.object(module.sys, "stdout", stdout):
                                                rc = module.main()

            self.assertEqual(0, rc)
            self.assertEqual("groq", llm_mock.call_args.kwargs["llm_provider"])
            self.assertEqual("GROQ_API_KEY", llm_mock.call_args.kwargs["llm_api_key_env"])
            self.assertEqual("https://api.groq.com/openai/v1", llm_mock.call_args.kwargs["llm_api_base"])
            self.assertEqual("llama-3.1-8b-instant", llm_mock.call_args.kwargs["llm_model"])
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("groq", manifest_payload["options"]["run_parameter_resolution"]["llm_provider"]["value"])
            self.assertEqual("GROQ_API_KEY", manifest_payload["options"]["run_parameter_resolution"]["llm_api_key_env"]["value"])
            self.assertEqual(
                "https://api.groq.com/openai/v1",
                manifest_payload["options"]["run_parameter_resolution"]["llm_api_base"]["value"],
            )
            self.assertEqual(
                "llama-3.1-8b-instant",
                manifest_payload["options"]["run_parameter_resolution"]["llm_model"]["value"],
            )
            self.assertIn("llm_provider=groq", stdout.getvalue())

    def test_run_cli_explicit_llm_provider_parameters_override_config_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            config_path = work_dir / "run.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "mode: llm-auto",
                        "llm_provider: groq",
                        "llm_api_key_env: GROQ_API_KEY",
                        "llm_api_base: https://api.groq.com/openai/v1",
                        "llm_model: llama-3.1-8b-instant",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            module = load_oneclick_product_module()
            preflight_mock = mock.Mock(return_value=_fake_preflight_result(work_dir))
            sample_mock = mock.Mock(return_value=_fake_sample_result(work_dir))
            export_mock = mock.Mock(return_value=_fake_export_result(work_dir))
            analyze_mock = mock.Mock(return_value=_fake_analyze_result(work_dir))
            llm_mock = mock.Mock(
                return_value=_fake_llm_result(
                    work_dir,
                    mode="api",
                    llm_applied=True,
                    final_hints_source="grounded_llm",
                )
            )
            report_mock = mock.Mock(return_value=_fake_report_result(work_dir))
            argv = [
                "oneclick_product.py",
                "run",
                "docker.io/library/app:base",
                "--work-dir",
                str(work_dir),
                "--config",
                str(config_path),
                "--llm-provider",
                "openrouter",
                "--llm-api-key-env",
                "OPENROUTER_API_KEY",
                "--llm-api-base",
                "https://openrouter.ai/api/v1",
                "--llm-model",
                "openrouter/auto",
            ]
            with mock.patch.object(
                module,
                "resolve_llm_provider_config",
                return_value=_resolved_llm_config(
                    provider="openrouter",
                    api_base="https://openrouter.ai/api/v1",
                    api_base_source="explicit",
                    api_key_env="OPENROUTER_API_KEY",
                    api_key_env_source="explicit",
                    api_key="demo-key",
                    api_key_source="OPENROUTER_API_KEY",
                    model="openrouter/auto",
                    model_source="explicit",
                ),
            ):
                with mock.patch.object(module, "run_preflight", preflight_mock):
                    with mock.patch.object(module, "run_sample_stage", sample_mock):
                        with mock.patch.object(module, "run_export_enrich_stage", export_mock):
                            with mock.patch.object(module, "run_analyze_stage", analyze_mock):
                                with mock.patch.object(module, "run_llm_optimize_stage", llm_mock):
                                    with mock.patch.object(module, "run_report_stage", report_mock):
                                        with mock.patch.object(module.sys, "argv", argv):
                                            rc = module.main()

            self.assertEqual(0, rc)
            self.assertEqual("openrouter", llm_mock.call_args.kwargs["llm_provider"])
            self.assertEqual("OPENROUTER_API_KEY", llm_mock.call_args.kwargs["llm_api_key_env"])
            self.assertEqual("https://openrouter.ai/api/v1", llm_mock.call_args.kwargs["llm_api_base"])
            self.assertEqual("openrouter/auto", llm_mock.call_args.kwargs["llm_model"])
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertNotIn("llm_provider", manifest_payload["options"].get("run_parameter_resolution", {}))
            self.assertNotIn("llm_api_key_env", manifest_payload["options"].get("run_parameter_resolution", {}))
            self.assertNotIn("llm_api_base", manifest_payload["options"].get("run_parameter_resolution", {}))
            self.assertNotIn("llm_model", manifest_payload["options"].get("run_parameter_resolution", {}))

    def test_run_cli_explicit_benchmark_parameters_override_inheritance_and_auto_dst_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            sample_model_dir = work_dir / "sample-model"
            bench_model_dir = work_dir / "bench-model"
            sample_model_dir.mkdir()
            bench_model_dir.mkdir()
            module = load_oneclick_product_module()

            preflight_mock = mock.Mock(return_value=_fake_preflight_result(work_dir))
            sample_mock = mock.Mock(return_value=_fake_sample_result(work_dir))
            export_mock = mock.Mock(return_value=_fake_export_result(work_dir))
            analyze_mock = mock.Mock(return_value=_fake_analyze_result(work_dir))
            apply_mock = mock.Mock(
                return_value=_fake_apply_result(
                    work_dir,
                    src_image="docker.io/tensorflow/serving:latest",
                    dst_image="docker.io/custom/tf-serving:manual",
                )
            )
            benchmark_mock = mock.Mock(
                return_value=_fake_benchmark_result(
                    work_dir,
                    opt_image="docker.io/custom/tf-serving:manual",
                )
            )
            report_mock = mock.Mock(return_value=_fake_report_result(work_dir))

            argv = [
                "oneclick_product.py",
                "run",
                "docker.io/tensorflow/serving:latest",
                "--work-dir",
                str(work_dir),
                "--workload",
                "tf-serving-half-plus-two",
                "--model-host",
                str(sample_model_dir),
                "--apply",
                "--dst-image",
                "docker.io/custom/tf-serving:manual",
                "--benchmark",
                "--bench-workload",
                "none",
                "--bench-model-host",
                str(bench_model_dir),
                "--bench-mode",
                "struct",
            ]
            stdout = io.StringIO()
            with mock.patch.object(module, "run_preflight", preflight_mock):
                with mock.patch.object(module, "run_sample_stage", sample_mock):
                    with mock.patch.object(module, "run_export_enrich_stage", export_mock):
                        with mock.patch.object(module, "run_analyze_stage", analyze_mock):
                            with mock.patch.object(module, "run_apply_stage", apply_mock):
                                with mock.patch.object(module, "run_benchmark_stage", benchmark_mock):
                                    with mock.patch.object(module, "run_report_stage", report_mock):
                                        with mock.patch.object(module.sys, "argv", argv):
                                            with mock.patch.object(module.sys, "stdout", stdout):
                                                rc = module.main()

            self.assertEqual(0, rc)
            self.assertEqual("docker.io/custom/tf-serving:manual", apply_mock.call_args.kwargs["dst_image"])
            self.assertEqual("struct", benchmark_mock.call_args.kwargs["mode"])
            self.assertEqual("none", benchmark_mock.call_args.kwargs["workload_profile"])
            self.assertEqual(str(bench_model_dir), benchmark_mock.call_args.kwargs["model_host_dir"])
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("none", manifest_payload["options"]["benchmark_workload"])
            self.assertNotIn("run_parameter_resolution", manifest_payload["options"])
            self.assertNotIn("run-args=normalized", stdout.getvalue())

    def test_run_cli_llm_auto_apply_benchmark_closure_succeeds_on_api_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            module = load_oneclick_product_module()

            preflight_mock = mock.Mock(return_value=_fake_preflight_result(work_dir))
            sample_mock = mock.Mock(return_value=_fake_sample_result(work_dir))
            export_mock = mock.Mock(return_value=_fake_export_result(work_dir))
            analyze_mock = mock.Mock(return_value=_fake_analyze_result(work_dir))
            llm_mock = mock.Mock(
                return_value=_fake_llm_result(
                    work_dir,
                    mode="api",
                    llm_applied=True,
                    final_hints_source="grounded_llm",
                )
            )
            apply_mock = mock.Mock(
                return_value=_fake_apply_result(
                    work_dir,
                    src_image="docker.io/library/app:base",
                    dst_image="docker.io/library/app:opt",
                )
            )
            benchmark_mock = mock.Mock(
                return_value=_fake_benchmark_result(work_dir, opt_image="docker.io/library/app:opt")
            )
            report_mock = mock.Mock(
                return_value=_fake_report_result(
                    work_dir,
                    mode="llm-auto",
                    mode_result="llm-auto-success",
                    llm_applied=True,
                    final_hints_source="grounded_llm",
                    optimized_image="docker.io/library/app:opt",
                )
            )

            argv = [
                "oneclick_product.py",
                "run",
                "docker.io/library/app:base",
                "--work-dir",
                str(work_dir),
                "--mode",
                "llm-auto",
                "--apply",
                "--dst-image",
                "docker.io/library/app:opt",
                "--benchmark",
            ]
            stdout = io.StringIO()
            with mock.patch.object(module, "resolve_llm_provider_config", return_value=_resolved_llm_config()):
                with mock.patch.object(module, "run_preflight", preflight_mock):
                    with mock.patch.object(module, "run_sample_stage", sample_mock):
                        with mock.patch.object(module, "run_export_enrich_stage", export_mock):
                            with mock.patch.object(module, "run_analyze_stage", analyze_mock):
                                with mock.patch.object(module, "run_llm_optimize_stage", llm_mock):
                                    with mock.patch.object(module, "run_apply_stage", apply_mock):
                                        with mock.patch.object(module, "run_benchmark_stage", benchmark_mock):
                                            with mock.patch.object(module, "run_report_stage", report_mock):
                                                with mock.patch.object(module.sys, "argv", argv):
                                                    with mock.patch.object(module.sys, "stdout", stdout):
                                                        rc = module.main()

            self.assertEqual(0, rc)
            self.assertTrue(preflight_mock.call_args.kwargs["llm_enabled"])
            llm_mock.assert_called_once()
            self.assertEqual("llm-auto", llm_mock.call_args.kwargs["requested_mode"])
            self.assertFalse(llm_mock.call_args.kwargs["force_manual_export"])
            apply_mock.assert_called_once()
            benchmark_mock.assert_called_once()
            report_mock.assert_called_once()
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest_payload["options"]["llm_enabled"])
            self.assertEqual("passed", manifest_payload["stages"]["llm_optimize"]["status"])
            self.assertEqual("passed", manifest_payload["stages"]["report"]["status"])
            report_payload = json.loads((work_dir / "run_report.json").read_text(encoding="utf-8"))
            self.assertEqual("llm-auto-success", report_payload["mode_result"])
            self.assertEqual("grounded_llm", report_payload["llm"]["final_hints_source"])
            self.assertIn("llm-summary mode_result=llm-auto-success llm_applied=True final_hints_source=grounded_llm", stdout.getvalue())

    def test_run_cli_llm_auto_apply_benchmark_closure_allows_external_dependency_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            store = ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260327_000101",
                work_dir=work_dir,
                image="docker.io/library/app:base",
                startup_mode="legacy",
                llm_enabled=False,
            )
            for stage_name in ("preflight", "sample", "export_enrich", "analyze"):
                store.stage_finish(stage_name, status="passed", summary=f"{stage_name} ok")

            module = load_oneclick_product_module()
            llm_mock = mock.Mock(
                return_value=_fake_llm_result(
                    work_dir,
                    mode="api_fallback",
                    llm_applied=False,
                    final_hints_source="deterministic_ranked",
                    warnings=["http 429/5xx: LLM HTTP 429; kept deterministic hints"],
                )
            )
            apply_mock = mock.Mock(
                return_value=_fake_apply_result(
                    work_dir,
                    src_image="docker.io/library/app:base",
                    dst_image="docker.io/library/app:opt",
                )
            )
            benchmark_mock = mock.Mock(
                return_value=_fake_benchmark_result(work_dir, opt_image="docker.io/library/app:opt")
            )
            report_mock = mock.Mock(
                return_value=_fake_report_result(
                    work_dir,
                )
            )

            argv = [
                "oneclick_product.py",
                "run",
                "docker.io/library/app:base",
                "--work-dir",
                str(work_dir),
                "--mode",
                "llm-auto",
                "--apply",
                "--dst-image",
                "docker.io/library/app:opt",
                "--benchmark",
            ]
            stdout = io.StringIO()
            with mock.patch.object(module, "resolve_llm_provider_config", return_value=_resolved_llm_config()):
                with mock.patch.object(module, "run_llm_optimize_stage", llm_mock):
                    with mock.patch.object(module, "run_apply_stage", apply_mock):
                        with mock.patch.object(module, "run_benchmark_stage", benchmark_mock):
                            with mock.patch.object(module, "run_report_stage", report_mock):
                                with mock.patch.object(module.sys, "argv", argv):
                                    with mock.patch.object(module.sys, "stdout", stdout):
                                        rc = module.main()

            self.assertEqual(0, rc)
            llm_mock.assert_called_once()
            apply_mock.assert_called_once()
            benchmark_mock.assert_called_once()
            report_mock.assert_called_once()
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("passed", manifest_payload["stages"]["llm_optimize"]["status"])
            self.assertIn(
                "http 429/5xx: LLM HTTP 429; kept deterministic hints",
                manifest_payload["stages"]["llm_optimize"]["warnings"],
            )
            report_payload = json.loads((work_dir / "run_report.json").read_text(encoding="utf-8"))
            self.assertEqual("deterministic_ranked", report_payload["llm"]["final_hints_source"])
            self.assertIn("llm-summary mode_result=llm-auto-fallback llm_applied=False final_hints_source=deterministic_ranked", stdout.getvalue())
            self.assertIn("fallback_to=deterministic kind=external_dependency", stdout.getvalue())

    def test_run_cli_llm_auto_does_not_mask_non_fallback_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            store = ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260327_000102",
                work_dir=work_dir,
                image="docker.io/library/app:base",
                startup_mode="legacy",
                llm_enabled=False,
            )
            for stage_name in ("preflight", "sample", "export_enrich", "analyze"):
                store.stage_finish(stage_name, status="passed", summary=f"{stage_name} ok")

            module = load_oneclick_product_module()
            argv = [
                "oneclick_product.py",
                "run",
                "docker.io/library/app:base",
                "--work-dir",
                str(work_dir),
                "--mode",
                "llm-auto",
                "--apply",
                "--benchmark",
            ]
            stderr = io.StringIO()
            with mock.patch.object(module, "resolve_llm_provider_config", return_value=_resolved_llm_config()):
                with mock.patch.object(
                    module,
                    "run_llm_optimize_stage",
                    side_effect=RuntimeError("internal llm failure"),
                ):
                    with mock.patch.object(module, "run_apply_stage") as apply_mock:
                        with mock.patch.object(module, "run_benchmark_stage") as benchmark_mock:
                            with mock.patch.object(module, "run_report_stage") as report_mock:
                                with mock.patch.object(module.sys, "argv", argv):
                                    with mock.patch.object(module.sys, "stderr", stderr):
                                        rc = module.main()

            self.assertEqual(1, rc)
            apply_mock.assert_not_called()
            benchmark_mock.assert_not_called()
            report_mock.assert_not_called()
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("failed", manifest_payload["stages"]["llm_optimize"]["status"])
            self.assertIn("internal llm failure", manifest_payload["stages"]["llm_optimize"]["error"])
            self.assertIn("stage-fail llm-optimize", stderr.getvalue())

    def test_run_cli_apply_failure_stops_before_benchmark(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            store = ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260327_000103",
                work_dir=work_dir,
                image="docker.io/library/app:base",
                startup_mode="legacy",
                llm_enabled=False,
            )
            for stage_name in ("preflight", "sample", "export_enrich", "analyze"):
                store.stage_finish(stage_name, status="passed", summary=f"{stage_name} ok")

            module = load_oneclick_product_module()
            argv = [
                "oneclick_product.py",
                "run",
                "docker.io/library/app:base",
                "--work-dir",
                str(work_dir),
                "--apply",
                "--dst-image",
                "docker.io/library/app:opt",
                "--benchmark",
            ]
            stderr = io.StringIO()
            apply_error = (
                "[ERR] image optimize 成功，但对 nerdctl + stargz + pull=never 不可消费: "
                "docker.io/library/app:opt; "
                "[ERR] inspect: nerdctl --namespace default image inspect docker.io/library/app:opt; "
                "[ERR] run: nerdctl --namespace default run --rm --pull=never --snapshotter stargz "
                "--platform linux/amd64 --entrypoint /bin/true docker.io/library/app:opt"
            )
            with mock.patch.object(module, "run_apply_stage", side_effect=RuntimeError(apply_error)):
                with mock.patch.object(module, "run_benchmark_stage") as benchmark_mock:
                    with mock.patch.object(module, "run_report_stage") as report_mock:
                        with mock.patch.object(module.sys, "argv", argv):
                            with mock.patch.object(module.sys, "stderr", stderr):
                                rc = module.main()

            self.assertEqual(1, rc)
            benchmark_mock.assert_not_called()
            report_mock.assert_not_called()
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("failed", manifest_payload["stages"]["apply"]["status"])
            self.assertIn("nerdctl + stargz + pull=never", manifest_payload["stages"]["apply"]["error"])
            self.assertNotIn("benchmark", manifest_payload["stages"])
            self.assertIn("stage-fail apply", stderr.getvalue())
            self.assertIn("nerdctl --namespace default image inspect docker.io/library/app:opt", stderr.getvalue())

    def test_run_cli_mode_llm_manual_pauses_and_generates_resume_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            store = ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260321_000200",
                work_dir=work_dir,
                image="docker.io/library/app:base",
                startup_mode="legacy",
                llm_enabled=False,
            )
            for stage_name in ("preflight", "sample", "export_enrich", "analyze"):
                store.stage_finish(stage_name, status="passed", summary=f"{stage_name} ok")

            module = load_oneclick_product_module()
            llm_result_path = work_dir / "llm_result.json"
            llm_result_path.write_text('{"mode":"manual_export"}\n', encoding="utf-8")

            llm_mock = mock.Mock(
                return_value=SimpleNamespace(
                    prompt_path=work_dir / "prompt.txt",
                    web_input_path=work_dir / "web_llm_ready_to_paste.txt",
                    body_path=work_dir / "body.json",
                    response_path=work_dir / "llm_response.json",
                    hints_raw_path=work_dir / "hints_raw.yaml",
                    preferred_paths_path=work_dir / "preferred_paths.txt",
                    grounding_audit_path=work_dir / "llm_grounding_audit.json",
                    hints_path=work_dir / "hints.yaml",
                    estargz_list_path=work_dir / "estargz.list",
                    selector_stats_hint_path=work_dir / "selector_stats_hints.json",
                    manual_instructions_path=work_dir / "llm_manual_instructions.md",
                    import_script_path=work_dir / "import_manual_web_llm.sh",
                    result_path=llm_result_path,
                    stage_log_path=work_dir / "llm_optimize.log",
                    warnings=[],
                    mode="manual_export",
                    llm_applied=False,
                    final_hints_source="deterministic_ranked",
                    pause_reason="awaiting_manual_llm_response",
                    manual_response_default_path=work_dir / "hints_raw.manual.yaml",
                    manual_response_format="json",
                )
            )

            argv = [
                "oneclick_product.py",
                "run",
                "docker.io/library/app:base",
                "--work-dir",
                str(work_dir),
                "--mode",
                "llm-manual",
                "--apply",
                "--dst-image",
                "docker.io/library/app:opt",
                "--benchmark",
            ]
            stdout = io.StringIO()
            with mock.patch.object(module, "run_llm_optimize_stage", llm_mock):
                with mock.patch.object(module, "run_apply_stage") as apply_mock:
                    with mock.patch.object(module, "run_benchmark_stage") as benchmark_mock:
                        with mock.patch.object(module, "run_report_stage") as report_mock:
                            with mock.patch.object(module.sys, "argv", argv):
                                with mock.patch.object(module.sys, "stdout", stdout):
                                    rc = module.main()

            self.assertEqual(0, rc)
            llm_mock.assert_called_once()
            apply_mock.assert_not_called()
            benchmark_mock.assert_not_called()
            report_mock.assert_not_called()
            resume_script = work_dir / "resume_manual_web_llm.sh"
            self.assertTrue(resume_script.exists())
            script_text = resume_script.read_text(encoding="utf-8")
            self.assertIn("expected strict JSON object with P0/P1/P2/P3 absolute-path arrays only", script_text)
            self.assertIn("--manual-response-file \"$MANUAL_RESPONSE_FILE\"", script_text)
            self.assertIn("--apply", script_text)
            self.assertIn("--benchmark", script_text)
            self.assertIn("llm-manual paused", stdout.getvalue())
            self.assertIn("copy_to_web: paste", stdout.getvalue())
            self.assertIn("manual_instructions: follow", stdout.getvalue())
            self.assertIn("save_response_to: write the strict JSON response to", stdout.getvalue())
            self.assertIn("response_must_be: strict JSON with only P0/P1/P2/P3 absolute-path arrays", stdout.getvalue())
            self.assertIn("resume_with: sudo -E bash", stdout.getvalue())
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                "paused",
                manifest_payload["stages"]["llm_optimize"]["status"],
            )
            self.assertEqual(
                "awaiting_manual_llm_response",
                manifest_payload["stages"]["llm_optimize"]["pause_reason"],
            )
            self.assertEqual(
                str(work_dir / "hints_raw.manual.yaml"),
                manifest_payload["stages"]["llm_optimize"]["resume_inputs"]["manual_response_file"]["path"],
            )
            self.assertEqual(
                str(resume_script),
                manifest_payload["artifacts"]["resume_manual_web_script"],
            )

    def test_run_cli_llm_manual_resume_closure_completes_apply_benchmark_and_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            store = ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260327_000103",
                work_dir=work_dir,
                image="docker.io/library/app:base",
                startup_mode="legacy",
                llm_enabled=False,
            )
            for stage_name in ("preflight", "sample", "export_enrich", "analyze"):
                store.stage_finish(stage_name, status="passed", summary=f"{stage_name} ok")

            module = load_oneclick_product_module()
            pause_result = _fake_llm_result(
                work_dir,
                mode="manual_export",
                llm_applied=False,
                final_hints_source="deterministic_ranked",
            )
            resume_result = _fake_llm_result(
                work_dir,
                mode="manual_response",
                llm_applied=True,
                final_hints_source="grounded_llm",
            )
            manual_response_path = work_dir / "manual_response.yaml"
            _touch(manual_response_path, "P0:\n  - /app/a.so\nP1: []\nP2: []\nP3: []\n")

            pause_argv = [
                "oneclick_product.py",
                "run",
                "docker.io/library/app:base",
                "--work-dir",
                str(work_dir),
                "--mode",
                "llm-manual",
                "--apply",
                "--dst-image",
                "docker.io/library/app:opt",
                "--benchmark",
            ]
            with mock.patch.object(module, "run_llm_optimize_stage", mock.Mock(return_value=pause_result)):
                with mock.patch.object(module, "run_apply_stage") as apply_mock:
                    with mock.patch.object(module, "run_benchmark_stage") as benchmark_mock:
                        with mock.patch.object(module, "run_report_stage") as report_mock:
                            with mock.patch.object(module.sys, "argv", pause_argv):
                                pause_rc = module.main()

            self.assertEqual(0, pause_rc)
            apply_mock.assert_not_called()
            benchmark_mock.assert_not_called()
            report_mock.assert_not_called()
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("paused", manifest_payload["stages"]["llm_optimize"]["status"])

            apply_result = _fake_apply_result(
                work_dir,
                src_image="docker.io/library/app:base",
                dst_image="docker.io/library/app:opt",
            )
            benchmark_result = _fake_benchmark_result(work_dir, opt_image="docker.io/library/app:opt")
            report_result = _fake_report_result(
                work_dir,
                mode="llm-manual",
                mode_result="llm-manual-completed",
                llm_applied=True,
                final_hints_source="grounded_llm",
                optimized_image="docker.io/library/app:opt",
            )
            resume_argv = [
                "oneclick_product.py",
                "run",
                "docker.io/library/app:base",
                "--work-dir",
                str(work_dir),
                "--mode",
                "llm-manual",
                "--manual-response-file",
                str(manual_response_path),
                "--apply",
                "--dst-image",
                "docker.io/library/app:opt",
                "--benchmark",
            ]
            llm_resume_mock = mock.Mock(return_value=resume_result)
            with mock.patch.object(module, "run_llm_optimize_stage", llm_resume_mock):
                with mock.patch.object(module, "run_apply_stage", mock.Mock(return_value=apply_result)) as apply_mock:
                    with mock.patch.object(module, "run_benchmark_stage", mock.Mock(return_value=benchmark_result)) as benchmark_mock:
                        with mock.patch.object(module, "run_report_stage", mock.Mock(return_value=report_result)) as report_mock:
                            with mock.patch.object(module.sys, "argv", resume_argv):
                                resume_rc = module.main()

            self.assertEqual(0, resume_rc)
            llm_resume_mock.assert_called_once()
            self.assertEqual(str(manual_response_path), llm_resume_mock.call_args.kwargs["manual_response_file"])
            apply_mock.assert_called_once()
            benchmark_mock.assert_called_once()
            report_mock.assert_called_once()
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("passed", manifest_payload["stages"]["llm_optimize"]["status"])
            self.assertEqual("passed", manifest_payload["stages"]["apply"]["status"])
            self.assertEqual("passed", manifest_payload["stages"]["benchmark"]["status"])
            self.assertEqual("passed", manifest_payload["stages"]["report"]["status"])
            report_payload = json.loads((work_dir / "run_report.json").read_text(encoding="utf-8"))
            self.assertEqual("llm-manual-completed", report_payload["mode_result"])
            self.assertEqual("grounded_llm", report_payload["llm"]["final_hints_source"])

    def test_parse_args_run_legacy_flags_map_to_formal_modes(self):
        module = load_oneclick_product_module()
        cases = [
            (["oneclick_product.py", "run", "docker.io/library/app:base"], "deterministic"),
            (["oneclick_product.py", "run", "docker.io/library/app:base", "--llm"], "llm-auto"),
            (["oneclick_product.py", "run", "docker.io/library/app:base", "--manual-web"], "llm-manual"),
            (
                [
                    "oneclick_product.py",
                    "run",
                    "docker.io/library/app:base",
                    "--manual-response-file",
                    "/tmp/hints_raw.manual.yaml",
                ],
                "llm-manual",
            ),
        ]
        for argv, expected_mode in cases:
            with self.subTest(argv=argv):
                with mock.patch.object(module.sys, "argv", argv):
                    args = module.parse_args()
                self.assertEqual(expected_mode, args.run_mode)

    def test_parse_args_run_rejects_conflicting_mode_and_legacy_flags(self):
        module = load_oneclick_product_module()
        argv = [
            "oneclick_product.py",
            "run",
            "docker.io/library/app:base",
            "--mode",
            "llm-auto",
            "--manual-web",
        ]
        with mock.patch.object(module.sys, "argv", argv):
            with self.assertRaises(SystemExit) as exc_info:
                module.parse_args()
        self.assertEqual(2, exc_info.exception.code)

    def test_parse_args_benchmark_keeps_direct_mode_default_without_run_level_inheritance(self):
        module = load_oneclick_product_module()
        argv = [
            "oneclick_product.py",
            "benchmark",
            "--work-dir",
            "/tmp/demo",
            "--workload",
            "tf-serving-half-plus-two",
        ]
        with mock.patch.object(module.sys, "argv", argv):
            args = module.parse_args()
        self.assertEqual("struct", args.mode)
        self.assertEqual("tf-serving-half-plus-two", args.workload)
        self.assertEqual("local-cold", args.cold_mode)

    def test_benchmark_cli_ready_serving_bootstrap_v2_defaults_mode_to_ready_in_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260418_000001",
                work_dir=work_dir,
                image="127.0.0.1:5001/demo/ready-serving-bootstrap-v2:base",
                startup_mode="legacy",
                llm_enabled=False,
            )
            module = load_oneclick_product_module()
            benchmark_mock = mock.Mock(
                return_value=SimpleNamespace(
                    env_check_base_path=work_dir / "env_check_benchmark_base.json",
                    env_check_opt_path=work_dir / "env_check_benchmark_opt.json",
                    policy_path=work_dir / "benchmark_policy.json",
                    stage_log_path=work_dir / "benchmark.log",
                    csv_path=work_dir / "coldstart_compare.csv",
                    log_dir=work_dir / "bench_logs",
                    result_path=work_dir / "benchmark_result.json",
                    valid=True,
                    invalid_reasons=[],
                    warnings=[],
                    summary_payload={},
                )
            )

            argv = [
                "oneclick_product.py",
                "benchmark",
                "--work-dir",
                str(work_dir),
                "--base-image",
                "127.0.0.1:5001/demo/ready-serving-bootstrap-v2:base",
                "--opt-image",
                "127.0.0.1:5001/demo/ready-serving-bootstrap-v2:opt",
                "--workload",
                "ready-serving-bootstrap-v2",
            ]
            with mock.patch.object(module, "run_benchmark_stage", benchmark_mock):
                with mock.patch.object(module, "nerdctl") as nerdctl_mock:
                    nerdctl_mock.return_value = subprocess.CompletedProcess(["nerdctl"], 1, "", "not found")
                    with mock.patch.object(module.sys, "argv", argv):
                        rc = module.main()

            self.assertEqual(0, rc)
            self.assertEqual("ready", benchmark_mock.call_args.kwargs["mode"])

    def test_benchmark_cli_ready_serving_layout_trap_v3_defaults_mode_to_ready_in_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260418_000001",
                work_dir=work_dir,
                image="127.0.0.1:5001/abtest/ready-serving-layout-trap-v3:esgz",
                startup_mode="legacy",
                llm_enabled=False,
            )
            module = load_oneclick_product_module()
            benchmark_mock = mock.Mock(
                return_value=SimpleNamespace(
                    env_check_base_path=work_dir / "env_check_benchmark_base.json",
                    env_check_opt_path=work_dir / "env_check_benchmark_opt.json",
                    policy_path=work_dir / "benchmark_policy.json",
                    stage_log_path=work_dir / "benchmark.log",
                    csv_path=work_dir / "coldstart_compare.csv",
                    log_dir=work_dir / "bench_logs",
                    result_path=work_dir / "benchmark_result.json",
                    valid=True,
                    invalid_reasons=[],
                    warnings=[],
                    summary_payload={},
                )
            )

            argv = [
                "oneclick_product.py",
                "benchmark",
                "--work-dir",
                str(work_dir),
                "--base-image",
                "127.0.0.1:5001/abtest/ready-serving-layout-trap-v3:esgz",
                "--opt-image",
                "127.0.0.1:5001/abtest/ready-serving-layout-trap-v3:opt",
                "--workload",
                "ready-serving-layout-trap-v3",
            ]
            with mock.patch.object(module, "run_benchmark_stage", benchmark_mock):
                with mock.patch.object(module, "nerdctl") as nerdctl_mock:
                    nerdctl_mock.return_value = subprocess.CompletedProcess(["nerdctl"], 1, "", "not found")
                    with mock.patch.object(module.sys, "argv", argv):
                        rc = module.main()

            self.assertEqual(0, rc)
            self.assertEqual("ready", benchmark_mock.call_args.kwargs["mode"])

    def test_benchmark_cli_ready_serving_layout_trap_v4_defaults_mode_to_ready_in_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260418_000001",
                work_dir=work_dir,
                image="127.0.0.1:5001/abtest/ready-serving-layout-trap-v4:esgz-default",
                startup_mode="legacy",
                llm_enabled=False,
            )
            module = load_oneclick_product_module()
            benchmark_mock = mock.Mock(
                return_value=SimpleNamespace(
                    env_check_base_path=work_dir / "env_check_benchmark_base.json",
                    env_check_opt_path=work_dir / "env_check_benchmark_opt.json",
                    policy_path=work_dir / "benchmark_policy.json",
                    stage_log_path=work_dir / "benchmark.log",
                    csv_path=work_dir / "coldstart_compare.csv",
                    log_dir=work_dir / "bench_logs",
                    result_path=work_dir / "benchmark_result.json",
                    valid=True,
                    invalid_reasons=[],
                    warnings=[],
                    summary_payload={},
                )
            )

            argv = [
                "oneclick_product.py",
                "benchmark",
                "--work-dir",
                str(work_dir),
                "--base-image",
                "127.0.0.1:5001/abtest/ready-serving-layout-trap-v4:esgz-default",
                "--opt-image",
                "127.0.0.1:5001/abtest/ready-serving-layout-trap-v4:esgz-hinted",
                "--workload",
                "ready-serving-layout-trap-v4",
            ]
            with mock.patch.object(module, "run_benchmark_stage", benchmark_mock):
                with mock.patch.object(module, "nerdctl") as nerdctl_mock:
                    nerdctl_mock.return_value = subprocess.CompletedProcess(["nerdctl"], 1, "", "not found")
                    with mock.patch.object(module.sys, "argv", argv):
                        rc = module.main()

            self.assertEqual(0, rc)
            self.assertEqual("ready", benchmark_mock.call_args.kwargs["mode"])
            self.assertEqual(
                "http://127.0.0.1:8000/debug/state",
                benchmark_mock.call_args.kwargs["debug_state_url"],
            )

    def test_benchmark_cli_ready_serving_layout_trap_v5_defaults_mode_to_ready_in_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260419_020001",
                work_dir=work_dir,
                image="127.0.0.1:5001/abtest/ready-serving-layout-trap-v5:src-raw",
                startup_mode="legacy",
                llm_enabled=False,
            )
            module = load_oneclick_product_module()
            benchmark_mock = mock.Mock(
                return_value=SimpleNamespace(
                    env_check_base_path=work_dir / "env_check_benchmark_base.json",
                    env_check_opt_path=work_dir / "env_check_benchmark_opt.json",
                    policy_path=work_dir / "benchmark_policy.json",
                    stage_log_path=work_dir / "benchmark.log",
                    csv_path=work_dir / "coldstart_compare.csv",
                    log_dir=work_dir / "bench_logs",
                    result_path=work_dir / "benchmark_result.json",
                    valid=True,
                    invalid_reasons=[],
                    warnings=[],
                    summary_payload={},
                )
            )

            argv = [
                "oneclick_product.py",
                "benchmark",
                "--work-dir",
                str(work_dir),
                "--base-image",
                "127.0.0.1:5001/abtest/ready-serving-layout-trap-v5:src-raw",
                "--opt-image",
                "127.0.0.1:5001/abtest/ready-serving-layout-trap-v5:esgz-hinted-v2",
                "--workload",
                "ready-serving-layout-trap-v5",
            ]
            with mock.patch.object(module, "run_benchmark_stage", benchmark_mock):
                with mock.patch.object(module, "nerdctl") as nerdctl_mock:
                    nerdctl_mock.return_value = subprocess.CompletedProcess(["nerdctl"], 1, "", "not found")
                    with mock.patch.object(module.sys, "argv", argv):
                        rc = module.main()

            self.assertEqual(0, rc)
            self.assertEqual("ready", benchmark_mock.call_args.kwargs["mode"])
            self.assertEqual(
                "http://127.0.0.1:8000/debug/state",
                benchmark_mock.call_args.kwargs["debug_state_url"],
            )

    def test_benchmark_cli_raw_vs_estargz_defaults_snapshotters_in_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260419_000001",
                work_dir=work_dir,
                image="127.0.0.1:5001/abtest/ready-serving-layout-trap-v4:src-raw",
                startup_mode="legacy",
                llm_enabled=False,
            )
            module = load_oneclick_product_module()
            benchmark_mock = mock.Mock(
                return_value=SimpleNamespace(
                    env_check_base_path=work_dir / "env_check_benchmark_base.json",
                    env_check_opt_path=work_dir / "env_check_benchmark_opt.json",
                    policy_path=work_dir / "benchmark_policy.json",
                    stage_log_path=work_dir / "benchmark.log",
                    csv_path=work_dir / "coldstart_compare.csv",
                    log_dir=work_dir / "bench_logs",
                    result_path=work_dir / "benchmark_result.json",
                    valid=True,
                    invalid_reasons=[],
                    warnings=[],
                    summary_payload={},
                )
            )

            argv = [
                "oneclick_product.py",
                "benchmark",
                "--work-dir",
                str(work_dir),
                "--base-image",
                "127.0.0.1:5001/abtest/ready-serving-layout-trap-v4:src-raw",
                "--opt-image",
                "127.0.0.1:5001/abtest/ready-serving-layout-trap-v4:esgz-hinted-v2",
                "--workload",
                "ready-serving-layout-trap-v4",
                "--comparison-mode",
                "raw-vs-estargz",
            ]
            with mock.patch.object(module, "run_benchmark_stage", benchmark_mock):
                with mock.patch.object(module, "nerdctl") as nerdctl_mock:
                    nerdctl_mock.return_value = subprocess.CompletedProcess(["nerdctl"], 1, "", "not found")
                    with mock.patch.object(module.sys, "argv", argv):
                        rc = module.main()

            self.assertEqual(0, rc)
            self.assertEqual("raw-vs-estargz", benchmark_mock.call_args.kwargs["comparison_mode"])
            self.assertEqual("overlayfs", benchmark_mock.call_args.kwargs["base_snapshotter"])
            self.assertEqual("stargz", benchmark_mock.call_args.kwargs["opt_snapshotter"])

    def test_benchmark_cli_forwards_lazy_comparison_mode_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            module = load_oneclick_product_module()
            benchmark_mock = mock.Mock(
                return_value=SimpleNamespace(
                    env_check_base_path=work_dir / "env_check_benchmark_base.json",
                    env_check_opt_path=work_dir / "env_check_benchmark_opt.json",
                    policy_path=work_dir / "benchmark_policy.json",
                    stage_log_path=work_dir / "benchmark.log",
                    csv_path=work_dir / "coldstart_compare.csv",
                    log_dir=work_dir / "bench_logs",
                    result_path=work_dir / "benchmark_result.json",
                    valid=True,
                    invalid_reasons=[],
                    warnings=[],
                    summary_payload={},
                )
            )

            argv = [
                "oneclick_product.py",
                "benchmark",
                "--work-dir",
                str(work_dir),
                "--base-image",
                "127.0.0.1:5001/abtest/ready-serving-layout-trap-v5:src-raw",
                "--opt-image",
                "127.0.0.1:5001/abtest/ready-serving-layout-trap-v5:esgz-hinted-v1",
                "--workload",
                "ready-serving-layout-trap-v5",
                "--comparison-mode",
                "raw-full-vs-estargz-lazy",
            ]
            with mock.patch.object(module, "run_benchmark_stage", benchmark_mock):
                with mock.patch.object(module, "nerdctl") as nerdctl_mock:
                    nerdctl_mock.return_value = subprocess.CompletedProcess(["nerdctl"], 1, "", "not found")
                    with mock.patch.object(module.sys, "argv", argv):
                        rc = module.main()

            self.assertEqual(0, rc)
            self.assertEqual("raw-full-vs-estargz-lazy", benchmark_mock.call_args.kwargs["comparison_mode"])
            self.assertEqual("overlayfs", benchmark_mock.call_args.kwargs["base_snapshotter"])
            self.assertEqual("stargz", benchmark_mock.call_args.kwargs["opt_snapshotter"])

    def test_benchmark_cli_forwards_lazy_triple_comparison_mode_with_unopt_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            module = load_oneclick_product_module()
            benchmark_mock = mock.Mock(
                return_value=SimpleNamespace(
                    env_check_base_path=work_dir / "env_check_benchmark_base.json",
                    env_check_opt_path=work_dir / "env_check_benchmark_opt.json",
                    policy_path=work_dir / "benchmark_policy.json",
                    stage_log_path=work_dir / "benchmark.log",
                    csv_path=work_dir / "coldstart_compare.csv",
                    log_dir=work_dir / "bench_logs",
                    result_path=work_dir / "benchmark_result.json",
                    valid=True,
                    invalid_reasons=[],
                    warnings=[],
                    summary_payload={},
                )
            )

            argv = [
                "oneclick_product.py",
                "benchmark",
                "--work-dir",
                str(work_dir),
                "--base-image",
                "127.0.0.1:5001/abtest/ready-serving-layout-trap-v5:src-raw",
                "--unopt-image",
                "127.0.0.1:5001/abtest/ready-serving-layout-trap-v5:esgz-unopt-v1",
                "--opt-image",
                "127.0.0.1:5001/abtest/ready-serving-layout-trap-v5:esgz-hinted-v1",
                "--workload",
                "ready-serving-layout-trap-v5",
                "--comparison-mode",
                "raw-full-vs-estargz-lazy-triple",
            ]
            with mock.patch.object(module, "run_benchmark_stage", benchmark_mock):
                with mock.patch.object(module, "nerdctl") as nerdctl_mock:
                    nerdctl_mock.return_value = subprocess.CompletedProcess(["nerdctl"], 1, "", "not found")
                    with mock.patch.object(module.sys, "argv", argv):
                        rc = module.main()

            self.assertEqual(0, rc)
            self.assertEqual(
                "raw-full-vs-estargz-lazy-triple",
                benchmark_mock.call_args.kwargs["comparison_mode"],
            )
            self.assertEqual(
                "127.0.0.1:5001/abtest/ready-serving-layout-trap-v5:esgz-unopt-v1",
                benchmark_mock.call_args.kwargs["unopt_image"],
            )
            self.assertEqual("overlayfs", benchmark_mock.call_args.kwargs["base_snapshotter"])
            self.assertEqual("stargz", benchmark_mock.call_args.kwargs["opt_snapshotter"])

    def test_parse_args_run_benchmark_defaults_bench_cold_mode_to_local_cold(self):
        module = load_oneclick_product_module()
        argv = [
            "oneclick_product.py",
            "run",
            "docker.io/library/app:base",
            "--benchmark",
        ]
        with mock.patch.object(module.sys, "argv", argv):
            args = module.parse_args()
        self.assertEqual("local-cold", args.bench_cold_mode)

    def test_parse_args_run_benchmark_accepts_bench_comparison_mode(self):
        module = load_oneclick_product_module()
        argv = [
            "oneclick_product.py",
            "run",
            "docker.io/library/app:base",
            "--benchmark",
            "--bench-comparison-mode",
            "raw-vs-estargz",
        ]
        with mock.patch.object(module.sys, "argv", argv):
            args = module.parse_args()
        self.assertEqual("raw-vs-estargz", args.bench_comparison_mode)

    def test_parse_args_run_benchmark_accepts_lazy_bench_comparison_mode(self):
        module = load_oneclick_product_module()
        argv = [
            "oneclick_product.py",
            "run",
            "docker.io/library/app:base",
            "--benchmark",
            "--bench-comparison-mode",
            "raw-full-vs-estargz-lazy",
        ]
        with mock.patch.object(module.sys, "argv", argv):
            args = module.parse_args()
        self.assertEqual("raw-full-vs-estargz-lazy", args.bench_comparison_mode)

    def test_parse_args_run_benchmark_accepts_lazy_triple_bench_comparison_mode_and_unopt_image(self):
        module = load_oneclick_product_module()
        argv = [
            "oneclick_product.py",
            "run",
            "docker.io/library/app:base",
            "--benchmark",
            "--bench-comparison-mode",
            "raw-full-vs-estargz-lazy-triple",
            "--bench-unopt-image",
            "docker.io/library/app:unopt",
        ]
        with mock.patch.object(module.sys, "argv", argv):
            args = module.parse_args()
        self.assertEqual("raw-full-vs-estargz-lazy-triple", args.bench_comparison_mode)
        self.assertEqual("docker.io/library/app:unopt", args.bench_unopt_image)

    def test_parse_args_benchmark_accepts_baseline_stargz_flag(self):
        module = load_oneclick_product_module()
        argv = [
            "oneclick_product.py",
            "benchmark",
            "--work-dir",
            "/tmp/demo",
            "--base-image",
            "docker.io/library/app:base",
            "--opt-image",
            "docker.io/library/app:opt",
            "--baseline-stargz",
        ]
        with mock.patch.object(module.sys, "argv", argv):
            args = module.parse_args()
        self.assertTrue(args.baseline_stargz)

    def test_parse_args_benchmark_accepts_comparison_mode(self):
        module = load_oneclick_product_module()
        argv = [
            "oneclick_product.py",
            "benchmark",
            "--work-dir",
            "/tmp/demo",
            "--base-image",
            "docker.io/library/app:base",
            "--opt-image",
            "docker.io/library/app:opt",
            "--comparison-mode",
            "raw-vs-estargz",
        ]
        with mock.patch.object(module.sys, "argv", argv):
            args = module.parse_args()
        self.assertEqual("raw-vs-estargz", args.comparison_mode)

    def test_parse_args_benchmark_accepts_lazy_comparison_mode(self):
        module = load_oneclick_product_module()
        argv = [
            "oneclick_product.py",
            "benchmark",
            "--work-dir",
            "/tmp/demo",
            "--base-image",
            "docker.io/library/app:base",
            "--opt-image",
            "docker.io/library/app:opt",
            "--comparison-mode",
            "raw-full-vs-estargz-lazy",
        ]
        with mock.patch.object(module.sys, "argv", argv):
            args = module.parse_args()
        self.assertEqual("raw-full-vs-estargz-lazy", args.comparison_mode)

    def test_parse_args_benchmark_accepts_lazy_triple_comparison_mode_and_unopt_image(self):
        module = load_oneclick_product_module()
        argv = [
            "oneclick_product.py",
            "benchmark",
            "--work-dir",
            "/tmp/demo",
            "--base-image",
            "docker.io/library/app:base",
            "--unopt-image",
            "docker.io/library/app:unopt",
            "--opt-image",
            "docker.io/library/app:opt",
            "--comparison-mode",
            "raw-full-vs-estargz-lazy-triple",
        ]
        with mock.patch.object(module.sys, "argv", argv):
            args = module.parse_args()
        self.assertEqual("raw-full-vs-estargz-lazy-triple", args.comparison_mode)
        self.assertEqual("docker.io/library/app:unopt", args.unopt_image)

    def test_parse_args_benchmark_rejects_run_only_config_flag(self):
        module = load_oneclick_product_module()
        argv = [
            "oneclick_product.py",
            "benchmark",
            "--work-dir",
            "/tmp/demo",
            "--config",
            "/tmp/run.yaml",
        ]
        with mock.patch.object(module.sys, "argv", argv):
            with self.assertRaises(SystemExit) as exc_info:
                module.parse_args()
        self.assertEqual(2, exc_info.exception.code)

    def test_benchmark_cli_triple_mode_requires_unopt_image(self):
        module = load_oneclick_product_module()
        argv = [
            "oneclick_product.py",
            "benchmark",
            "--work-dir",
            "/tmp/demo",
            "--comparison-mode",
            "raw-full-vs-estargz-lazy-triple",
        ]
        stderr = io.StringIO()
        with mock.patch.object(module.sys, "argv", argv):
            with mock.patch.object(module.sys, "stderr", stderr):
                rc = module.main()
        self.assertEqual(2, rc)
        self.assertIn(
            "--comparison-mode raw-full-vs-estargz-lazy-triple requires --unopt-image",
            stderr.getvalue(),
        )

    def test_run_cli_benchmark_triple_mode_requires_bench_unopt_image(self):
        module = load_oneclick_product_module()
        argv = [
            "oneclick_product.py",
            "run",
            "docker.io/library/app:base",
            "--benchmark",
            "--bench-comparison-mode",
            "raw-full-vs-estargz-lazy-triple",
        ]
        stderr = io.StringIO()
        with mock.patch.object(module.sys, "argv", argv):
            with mock.patch.object(module.sys, "stderr", stderr):
                rc = module.main()
        self.assertEqual(2, rc)
        self.assertIn(
            "--bench-comparison-mode raw-full-vs-estargz-lazy-triple requires --bench-unopt-image",
            stderr.getvalue(),
        )

    def test_help_text_describes_run_inheritance_and_direct_benchmark_behavior(self):
        module = load_oneclick_product_module()

        run_stdout = io.StringIO()
        with mock.patch.object(module.sys, "argv", ["oneclick_product.py", "run", "--help"]):
            with mock.patch.object(module.sys, "stdout", run_stdout):
                with self.assertRaises(SystemExit) as exc_info:
                    module.parse_args()
        self.assertEqual(0, exc_info.exception.code)
        self.assertIn("--config", run_stdout.getvalue())
        self.assertIn("priority is CLI explicit args > config > preset >", run_stdout.getvalue())
        self.assertIn("built-in defaults", run_stdout.getvalue())
        self.assertIn("named default-value set for common run scenarios", run_stdout.getvalue())
        self.assertIn("tfpy-half-plus-two-io1g", run_stdout.getvalue())
        self.assertIn("tf-serving-half-plus-two", run_stdout.getvalue())
        self.assertIn("CLI explicit", run_stdout.getvalue())
        self.assertIn("arguments still win", run_stdout.getvalue())
        self.assertIn("benchmark workload profile; if omitted, inherits", run_stdout.getvalue())
        self.assertIn("--workload", run_stdout.getvalue())
        self.assertIn("benchmark host model directory; if omitted, inherits", run_stdout.getvalue())
        self.assertIn("--model-host", run_stdout.getvalue())
        self.assertIn("recommendation (for example:", run_stdout.getvalue())
        self.assertIn("tfpy-half-plus-two-io1g", run_stdout.getvalue())
        self.assertIn("tf-serving-half-plus-two", run_stdout.getvalue())
        self.assertIn("destination optimized image reference for --apply; if", run_stdout.getvalue())
        self.assertIn("run auto-generates <src-tag>-optimized or", run_stdout.getvalue())
        self.assertIn(":optimized", run_stdout.getvalue())
        self.assertIn("P0/P1/P2/P3", run_stdout.getvalue())
        self.assertIn("absolute-path lists", run_stdout.getvalue())
        self.assertIn("--llm-provider", run_stdout.getvalue())
        self.assertIn("--llm-api-key-env", run_stdout.getvalue())
        self.assertIn("--llm-reasoning-effort", run_stdout.getvalue())
        self.assertIn("--llm-timeout-sec", run_stdout.getvalue())
        self.assertIn("provider-aware API defaults", run_stdout.getvalue())
        self.assertIn("upstream/unusable API responses", run_stdout.getvalue())

        benchmark_stdout = io.StringIO()
        with mock.patch.object(module.sys, "argv", ["oneclick_product.py", "benchmark", "--help"]):
            with mock.patch.object(module.sys, "stdout", benchmark_stdout):
                with self.assertRaises(SystemExit) as exc_info:
                    module.parse_args()
        self.assertEqual(0, exc_info.exception.code)
        self.assertIn("direct benchmark uses this value directly", benchmark_stdout.getvalue())
        self.assertIn("direct benchmark command", benchmark_stdout.getvalue())
        self.assertIn("--baseline-stargz", benchmark_stdout.getvalue())

        llm_stdout = io.StringIO()
        with mock.patch.object(module.sys, "argv", ["oneclick_product.py", "llm-optimize", "--help"]):
            with mock.patch.object(module.sys, "stdout", llm_stdout):
                with self.assertRaises(SystemExit) as exc_info:
                    module.parse_args()
        self.assertEqual(0, exc_info.exception.code)
        self.assertIn("force manual web-LLM export instead of attempting the", llm_stdout.getvalue())
        self.assertIn("P0/P1/P2/P3", llm_stdout.getvalue())
        self.assertIn("absolute file paths", llm_stdout.getvalue())
        self.assertIn("--llm-provider", llm_stdout.getvalue())
        self.assertIn("--llm-reasoning-effort", llm_stdout.getvalue())
        self.assertIn("--llm-timeout-sec", llm_stdout.getvalue())
        self.assertIn("custom provider requires this", llm_stdout.getvalue())

    def test_run_cli_reports_missing_config_file(self):
        module = load_oneclick_product_module()
        missing_path = Path("/tmp/does-not-exist-run.yaml")
        argv = [
            "oneclick_product.py",
            "run",
            "docker.io/library/app:base",
            "--config",
            str(missing_path),
        ]
        stderr = io.StringIO()
        with mock.patch.object(module.sys, "argv", argv):
            with mock.patch.object(module.sys, "stderr", stderr):
                rc = module.main()
        self.assertEqual(2, rc)
        self.assertIn("run config file does not exist", stderr.getvalue())

    def test_run_cli_reports_yaml_parse_failure_for_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "run.yaml"
            config_path.write_text('preset: "unterminated\n', encoding="utf-8")
            module = load_oneclick_product_module()
            argv = [
                "oneclick_product.py",
                "run",
                "docker.io/library/app:base",
                "--config",
                str(config_path),
            ]
            stderr = io.StringIO()
            with mock.patch.object(module.sys, "argv", argv):
                with mock.patch.object(module.sys, "stderr", stderr):
                    rc = module.main()
            self.assertEqual(2, rc)
            self.assertIn("failed to parse run config YAML", stderr.getvalue())

    def test_run_cli_reports_unsupported_fields_in_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "run.yaml"
            config_path.write_text("selector_mode: file\n", encoding="utf-8")
            module = load_oneclick_product_module()
            argv = [
                "oneclick_product.py",
                "run",
                "docker.io/library/app:base",
                "--config",
                str(config_path),
            ]
            stderr = io.StringIO()
            with mock.patch.object(module.sys, "argv", argv):
                with mock.patch.object(module.sys, "stderr", stderr):
                    rc = module.main()
            self.assertEqual(2, rc)
            self.assertIn("contains unsupported fields", stderr.getvalue())
            self.assertIn("selector_mode", stderr.getvalue())

    def test_run_cli_reports_type_errors_in_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "run.yaml"
            config_path.write_text("bench_runs: fast\n", encoding="utf-8")
            module = load_oneclick_product_module()
            argv = [
                "oneclick_product.py",
                "run",
                "docker.io/library/app:base",
                "--config",
                str(config_path),
            ]
            stderr = io.StringIO()
            with mock.patch.object(module.sys, "argv", argv):
                with mock.patch.object(module.sys, "stderr", stderr):
                    rc = module.main()
            self.assertEqual(2, rc)
            self.assertIn("run config field 'bench_runs' must be an integer", stderr.getvalue())

    def test_run_cli_mode_llm_auto_requires_api_key(self):
        module = load_oneclick_product_module()
        argv = [
            "oneclick_product.py",
            "run",
            "docker.io/library/app:base",
            "--mode",
            "llm-auto",
        ]
        stderr = io.StringIO()
        with mock.patch.object(
            module,
            "resolve_llm_provider_config",
            return_value=_resolved_llm_config(api_key="", api_key_source=None),
        ):
            with mock.patch.object(module.sys, "argv", argv):
                with mock.patch.object(module.sys, "stderr", stderr):
                    rc = module.main()

        self.assertEqual(2, rc)
        self.assertIn("--mode llm-auto requires an API key", stderr.getvalue())
        self.assertIn("OPENAI_API_KEY", stderr.getvalue())

    def test_run_cli_mode_llm_auto_reports_provider_specific_missing_key(self):
        module = load_oneclick_product_module()
        argv = [
            "oneclick_product.py",
            "run",
            "docker.io/library/app:base",
            "--mode",
            "llm-auto",
            "--llm-provider",
            "groq",
        ]
        stderr = io.StringIO()
        with mock.patch.object(
            module,
            "resolve_llm_provider_config",
            return_value=_resolved_llm_config(
                provider="groq",
                api_base="https://api.groq.com/openai/v1",
                api_base_source="provider_default:groq",
                api_key_env="GROQ_API_KEY",
                api_key_env_source="provider_default:groq",
                api_key="",
                api_key_source=None,
                model="llama-3.1-8b-instant",
                model_source="provider_default:groq",
            ),
        ):
            with mock.patch.object(module.sys, "argv", argv):
                with mock.patch.object(module.sys, "stderr", stderr):
                    rc = module.main()

        self.assertEqual(2, rc)
        self.assertIn("GROQ_API_KEY", stderr.getvalue())
        self.assertIn("llm provider 'groq'", stderr.getvalue())

    def test_run_cli_mode_llm_auto_reports_custom_provider_config_errors_clearly(self):
        module = load_oneclick_product_module()
        argv = [
            "oneclick_product.py",
            "run",
            "docker.io/library/app:base",
            "--mode",
            "llm-auto",
            "--llm-provider",
            "custom",
        ]
        stderr = io.StringIO()
        with mock.patch.object(
            module,
            "resolve_llm_provider_config",
            side_effect=ValueError("llm provider 'custom' requires --llm-api-base or config field llm_api_base"),
        ):
            with mock.patch.object(module.sys, "argv", argv):
                with mock.patch.object(module.sys, "stderr", stderr):
                    rc = module.main()

        self.assertEqual(2, rc)
        self.assertIn("llm provider 'custom' requires --llm-api-base", stderr.getvalue())

    def test_run_cli_mode_llm_auto_allows_mock_provider_without_api_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            module = load_oneclick_product_module()
            preflight_mock = mock.Mock(return_value=_fake_preflight_result(work_dir))
            sample_mock = mock.Mock(return_value=_fake_sample_result(work_dir))
            export_mock = mock.Mock(return_value=_fake_export_result(work_dir))
            analyze_mock = mock.Mock(return_value=_fake_analyze_result(work_dir))
            llm_mock = mock.Mock(
                return_value=_fake_llm_result(
                    work_dir,
                    mode="api",
                    llm_applied=True,
                    final_hints_source="grounded_llm",
                )
            )
            report_mock = mock.Mock(return_value=_fake_report_result(work_dir))
            argv = [
                "oneclick_product.py",
                "run",
                "docker.io/library/app:base",
                "--work-dir",
                str(work_dir),
                "--mode",
                "llm-auto",
                "--llm-provider",
                "mock",
            ]
            with mock.patch.object(
                module,
                "resolve_llm_provider_config",
                return_value=_resolved_llm_config(
                    provider="mock",
                    provider_type="mock",
                    api_base="",
                    api_base_source="provider_default:mock",
                    api_key_env=None,
                    api_key_env_source=None,
                    api_key="",
                    api_key_source=None,
                    model="mock-llm",
                    model_source="provider_default:mock",
                ),
            ):
                with mock.patch.object(module, "run_preflight", preflight_mock):
                    with mock.patch.object(module, "run_sample_stage", sample_mock):
                        with mock.patch.object(module, "run_export_enrich_stage", export_mock):
                            with mock.patch.object(module, "run_analyze_stage", analyze_mock):
                                with mock.patch.object(module, "run_llm_optimize_stage", llm_mock):
                                    with mock.patch.object(module, "run_report_stage", report_mock):
                                        with mock.patch.object(module.sys, "argv", argv):
                                            rc = module.main()

        self.assertEqual(0, rc)
        llm_mock.assert_called_once()

    def test_run_cli_passes_llm_reasoning_effort_and_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            module = load_oneclick_product_module()
            preflight_mock = mock.Mock(return_value=_fake_preflight_result(work_dir))
            sample_mock = mock.Mock(return_value=_fake_sample_result(work_dir))
            export_mock = mock.Mock(return_value=_fake_export_result(work_dir))
            analyze_mock = mock.Mock(return_value=_fake_analyze_result(work_dir))
            llm_mock = mock.Mock(
                return_value=_fake_llm_result(
                    work_dir,
                    mode="api",
                    llm_applied=True,
                    final_hints_source="grounded_llm",
                )
            )
            report_mock = mock.Mock(return_value=_fake_report_result(work_dir))
            argv = [
                "oneclick_product.py",
                "run",
                "docker.io/library/app:base",
                "--work-dir",
                str(work_dir),
                "--mode",
                "llm-auto",
                "--llm-reasoning-effort",
                "medium",
                "--llm-timeout-sec",
                "42",
            ]
            with mock.patch.object(module, "resolve_llm_provider_config", return_value=_resolved_llm_config()):
                with mock.patch.object(module, "run_preflight", preflight_mock):
                    with mock.patch.object(module, "run_sample_stage", sample_mock):
                        with mock.patch.object(module, "run_export_enrich_stage", export_mock):
                            with mock.patch.object(module, "run_analyze_stage", analyze_mock):
                                with mock.patch.object(module, "run_llm_optimize_stage", llm_mock):
                                    with mock.patch.object(module, "run_report_stage", report_mock):
                                        with mock.patch.object(module.sys, "argv", argv):
                                            rc = module.main()

        self.assertEqual(0, rc)
        self.assertEqual("medium", llm_mock.call_args.kwargs["llm_reasoning_effort"])
        self.assertEqual(42, llm_mock.call_args.kwargs["llm_timeout_sec"])

    def test_report_cli_no_longer_prints_or_records_publish_recommendation(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260325_000020",
                work_dir=work_dir,
                image="docker.io/library/app:base",
                startup_mode="legacy",
                llm_enabled=False,
            )
            module = load_oneclick_product_module()
            report_json_path = work_dir / "run_report.json"
            report_md_path = work_dir / "run_report.md"
            report_mock = mock.Mock(
                return_value=SimpleNamespace(
                    report_json_path=report_json_path,
                    report_md_path=report_md_path,
                    summary_payload=_fake_report_result(work_dir).summary_payload,
                )
            )
            argv = [
                "oneclick_product.py",
                "report",
                "--work-dir",
                str(work_dir),
            ]
            stdout = io.StringIO()
            with mock.patch.object(module, "run_report_stage", report_mock):
                with mock.patch.object(module.sys, "argv", argv):
                    with mock.patch.object(module.sys, "stdout", stdout):
                        rc = module.main()

            self.assertEqual(0, rc)
            self.assertNotIn("recommended_publish=", stdout.getvalue())
            self.assertNotIn("publish_recommended.sh", stdout.getvalue())
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("passed", manifest_payload["stages"]["report"]["status"])
            self.assertNotIn("publish_recommended_script", manifest_payload["artifacts"])

    def test_run_cli_prints_condensed_run_summary_from_report_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            store = ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260325_000021",
                work_dir=work_dir,
                image="docker.io/library/app:base",
                startup_mode="legacy",
                llm_enabled=False,
            )
            for stage_name in ("preflight", "sample", "export_enrich", "analyze", "llm_optimize"):
                store.stage_finish(stage_name, status="passed", summary=f"{stage_name} ok")

            module = load_oneclick_product_module()
            report_json_path = work_dir / "run_report.json"
            report_md_path = work_dir / "run_report.md"
            report_mock = mock.Mock(
                return_value=SimpleNamespace(
                    report_json_path=report_json_path,
                    report_md_path=report_md_path,
                    summary_payload=_fake_report_result(
                        work_dir,
                        overall_status="passed",
                        mode="llm-auto",
                        mode_result="llm-auto-success",
                        llm_applied=True,
                        final_hints_source="grounded_llm",
                        optimized_image="docker.io/library/app:opt",
                        benchmark_status="passed",
                        baseline_median_ready_ms=1985.0,
                        optimized_median_ready_ms=1714.5,
                        improvement_pct=13.63,
                    ).summary_payload,
                )
            )

            argv = [
                "oneclick_product.py",
                "run",
                "docker.io/library/app:base",
                "--work-dir",
                str(work_dir),
            ]
            stdout = io.StringIO()
            with mock.patch.object(module, "run_report_stage", report_mock):
                with mock.patch.object(module.sys, "argv", argv):
                    with mock.patch.object(module.sys, "stdout", stdout):
                        rc = module.main()

            self.assertEqual(0, rc)
            self.assertIn(
                "final-summary run_status=passed mode=llm-auto mode_result=llm-auto-success",
                stdout.getvalue(),
            )
            self.assertIn("llm_applied=True", stdout.getvalue())
            self.assertIn("hints_source=grounded_llm", stdout.getvalue())
            self.assertIn("baseline_median_ready_ms=1985.0", stdout.getvalue())
            self.assertIn("optimized_median_ready_ms=1714.5", stdout.getvalue())
            self.assertIn("optimized_image=docker.io/library/app:opt", stdout.getvalue())
            self.assertIn("benchmark_improvement_pct=13.63%", stdout.getvalue())
            self.assertIn("stage-start report", stdout.getvalue())
            self.assertIn("stage-pass report", stdout.getvalue())
            self.assertNotIn("recommended_publish=", stdout.getvalue())
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("passed", manifest_payload["stages"]["report"]["status"])
            self.assertNotIn("publish_recommended_script", manifest_payload["artifacts"])

    def test_print_benchmark_summary_includes_per_run_ready_comparison_from_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            csv_path = work_dir / "coldstart_compare.csv"
            csv_path.write_text(
                "\n".join(
                    [
                        "ts,label,snapshotter,mode,ready_ms,predict_extra_ms,proof_exists,proof_done,status,err",
                        "2026-04-11T05:03:56+00:00,baseline,overlayfs,ready,8705,-1,266,0,ok,",
                        "2026-04-11T05:05:24+00:00,optimized,stargz,ready,78089,-1,207,0,ok,",
                        "2026-04-11T05:05:36+00:00,baseline,overlayfs,ready,4718,-1,266,0,ok,",
                        "2026-04-11T05:05:50+00:00,optimized,stargz,ready,4691,-1,252,0,ok,",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            module = load_oneclick_product_module()
            stdout = io.StringIO()

            with mock.patch.object(module.sys, "stdout", stdout):
                module._print_benchmark_summary(
                    {
                        "status": "passed",
                        "csv": str(csv_path),
                        "summary": {
                            "labels": {
                                "baseline": {"median_ready_ms": 4718.0},
                                "optimized": {"median_ready_ms": 4699.0},
                            },
                            "comparison": {"median_ready_improvement_pct": 0.40},
                        },
                    }
                )

            output = stdout.getvalue()
            self.assertIn(
                "benchmark-summary status=passed baseline_median_ready_ms=4718.0 optimized_median_ready_ms=4699.0",
                output,
            )
            self.assertIn("improvement_pct=0.40%", output)
            self.assertIn("run1 baseline_ready_ms=8705 optimized_ready_ms=78089 delta_ms=+69384", output)
            self.assertIn("run2 baseline_ready_ms=4718 optimized_ready_ms=4691 delta_ms=-27", output)

    def test_print_benchmark_summary_prefers_post_pull_ready_metrics_when_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            csv_path = work_dir / "coldstart_compare.csv"
            csv_path.write_text(
                "\n".join(
                    [
                        "ts,label,snapshotter,mode,pull_ms,run_ms,ready_wait_ms,end_to_end_ready_ms,ready_ms,predict_extra_ms,status,err",
                        "2026-04-19T00:00:00+00:00,baseline,overlayfs,ready,85819,3353,0,89172,89172,-1,ok,",
                        "2026-04-19T00:00:01+00:00,optimized,stargz,ready,76215,2882,0,79097,79097,-1,ok,",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            module = load_oneclick_product_module()
            stdout = io.StringIO()

            with mock.patch.object(module.sys, "stdout", stdout):
                module._print_benchmark_summary(
                    {
                        "status": "passed",
                        "csv": str(csv_path),
                        "comparison_mode": "raw-vs-estargz",
                        "ready_ms_semantics": "end_to_end_ready_ms",
                        "summary": {
                            "labels": {
                                "baseline": {
                                    "median_ready_ms": 78633.0,
                                    "median_post_pull_ready_ms": 3353.0,
                                    "median_end_to_end_ready_ms": 78633.0,
                                },
                                "optimized": {
                                    "median_ready_ms": 79097.0,
                                    "median_post_pull_ready_ms": 3012.0,
                                    "median_end_to_end_ready_ms": 79097.0,
                                },
                            },
                            "comparison": {
                                "median_ready_improvement_pct": -0.59,
                                "median_post_pull_ready_improvement_pct": 10.17,
                                "median_end_to_end_improvement_pct": -0.59,
                            },
                        },
                    }
                )

            output = stdout.getvalue()
            self.assertIn(
                "benchmark-summary status=passed baseline_median_post_pull_ready_ms=3353.0 optimized_median_post_pull_ready_ms=3012.0",
                output,
            )
            self.assertIn("post_pull_ready_improvement_pct=10.17%", output)
            self.assertIn("metric=post_pull_ready_ms ready_ms_semantics=end_to_end_ready_ms", output)
            self.assertIn(
                "end_to_end_ready_ms: baseline_median=78633.0 optimized_median=79097.0 improvement_pct=-0.59%",
                output,
            )
            self.assertIn(
                "run1 baseline_post_pull_ready_ms=3353 baseline_ready_ms=89172 optimized_post_pull_ready_ms=2882 optimized_ready_ms=79097 delta_ms=-471",
                output,
            )

    def test_print_benchmark_summary_prefers_lazy_ready_metrics_for_raw_full_vs_estargz_lazy(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            csv_path = work_dir / "coldstart_compare.csv"
            csv_path.write_text(
                "\n".join(
                    [
                        "ts,label,snapshotter,mode,pull_ms,run_ms,ready_wait_ms,end_to_end_ready_ms,ready_ms,lazy_prepare_ms,lazy_ready_ms,ready_ms_semantics,status,err",
                        "2026-04-19T00:00:00+00:00,baseline,overlayfs,ready,85819,3353,0,89172,89172,-1,-1,end_to_end_ready_ms,ok,",
                        "2026-04-19T00:00:01+00:00,optimized,stargz,ready,0,2882,0,3125,3125,243,3125,lazy_ready_ms,ok,",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            module = load_oneclick_product_module()
            stdout = io.StringIO()

            with mock.patch.object(module.sys, "stdout", stdout):
                module._print_benchmark_summary(
                    {
                        "status": "passed",
                        "csv": str(csv_path),
                        "comparison_mode": "raw-full-vs-estargz-lazy",
                        "ready_ms_semantics": "baseline_full_pull_ready_ms_vs_optimized_lazy_ready_ms",
                        "summary": {
                            "labels": {
                                "baseline": {
                                    "median_pull_ms": 85819.0,
                                    "median_post_pull_ready_ms": 3353.0,
                                    "median_end_to_end_ready_ms": 89172.0,
                                    "median_full_pull_ready_ms": 89172.0,
                                },
                                "optimized": {
                                    "median_lazy_prepare_ms": 243.0,
                                    "median_lazy_ready_ms": 3125.0,
                                    "median_post_pull_ready_ms": 2882.0,
                                    "median_end_to_end_ready_ms": 3125.0,
                                },
                            },
                            "comparison": {
                                "median_lazy_ready_improvement_pct": 96.50,
                            },
                        },
                    }
                )

            output = stdout.getvalue()
            self.assertIn(
                "benchmark-summary status=passed baseline_median_full_pull_ready_ms=89172.0 optimized_median_lazy_ready_ms=3125.0",
                output,
            )
            self.assertIn("lazy_ready_improvement_pct=96.50%", output)
            self.assertIn("metric=baseline_full_pull_ready_ms_vs_optimized_lazy_ready_ms", output)
            self.assertIn("baseline median pull_ms=85819.0", output)
            self.assertIn("optimized median lazy_prepare_ms=243.0", output)
            self.assertIn("baseline median post_pull_ready_ms=3353.0", output)
            self.assertIn("optimized median post_pull_ready_ms/lazy_ready_ms=2882.0", output)
            self.assertIn(
                "run1 baseline_full_pull_ready_ms=89172 optimized_lazy_ready_ms=3125 optimized_lazy_prepare_ms=243 delta_ms=-86047",
                output,
            )
            self.assertNotIn("post_pull_ready_improvement_pct", output)

    def test_print_benchmark_summary_prefers_triple_lazy_ready_metrics_for_raw_full_vs_estargz_lazy_triple(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            csv_path = work_dir / "coldstart_compare.csv"
            csv_path.write_text(
                "\n".join(
                    [
                        "ts,label,snapshotter,mode,pull_ms,run_ms,ready_wait_ms,end_to_end_ready_ms,ready_ms,lazy_prepare_ms,lazy_ready_ms,ready_ms_semantics,status,err",
                        "2026-04-19T00:00:00+00:00,baseline,overlayfs,ready,85819,3353,0,89172,89172,-1,-1,end_to_end_ready_ms,ok,",
                        "2026-04-19T00:00:01+00:00,unoptimized_lazy,stargz,ready,0,3900,0,4210,4210,310,4210,lazy_ready_ms,ok,",
                        "2026-04-19T00:00:02+00:00,optimized,stargz,ready,0,2882,0,3125,3125,243,3125,lazy_ready_ms,ok,",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            module = load_oneclick_product_module()
            stdout = io.StringIO()

            with mock.patch.object(module.sys, "stdout", stdout):
                module._print_benchmark_summary(
                    {
                        "status": "passed",
                        "csv": str(csv_path),
                        "comparison_mode": "raw-full-vs-estargz-lazy-triple",
                        "ready_ms_semantics": "baseline_full_pull_ready_ms_vs_unoptimized_lazy_ready_ms_vs_optimized_lazy_ready_ms",
                        "summary": {
                            "labels": {
                                "baseline": {
                                    "median_pull_ms": 85819.0,
                                    "median_post_pull_ready_ms": 3353.0,
                                    "median_end_to_end_ready_ms": 89172.0,
                                    "median_full_pull_ready_ms": 89172.0,
                                },
                                "unoptimized_lazy": {
                                    "median_lazy_prepare_ms": 310.0,
                                    "median_lazy_ready_ms": 4210.0,
                                    "median_post_pull_ready_ms": 3900.0,
                                    "median_end_to_end_ready_ms": 4210.0,
                                },
                                "optimized": {
                                    "median_lazy_prepare_ms": 243.0,
                                    "median_lazy_ready_ms": 3125.0,
                                    "median_post_pull_ready_ms": 2882.0,
                                    "median_end_to_end_ready_ms": 3125.0,
                                },
                            },
                            "comparison": {
                                "unoptimized_vs_raw_improvement_pct": 95.28,
                                "optimized_vs_raw_improvement_pct": 96.50,
                                "optimized_vs_unoptimized_improvement_pct": 25.77,
                            },
                        },
                    }
                )

            output = stdout.getvalue()
            self.assertIn(
                "benchmark-summary status=passed baseline_median_full_pull_ready_ms=89172.0 unoptimized_median_lazy_ready_ms=4210.0 optimized_median_lazy_ready_ms=3125.0",
                output,
            )
            self.assertIn("unoptimized_vs_raw_improvement_pct=95.28%", output)
            self.assertIn("optimized_vs_raw_improvement_pct=96.50%", output)
            self.assertIn("optimized_vs_unoptimized_improvement_pct=25.77%", output)
            self.assertIn(
                "metric=baseline_full_pull_ready_ms_vs_unoptimized_lazy_ready_ms_vs_optimized_lazy_ready_ms",
                output,
            )
            self.assertIn("unoptimized_lazy median lazy_prepare_ms=310.0", output)
            self.assertIn("optimized median lazy_prepare_ms=243.0", output)
            self.assertIn(
                "run1 baseline_full_pull_ready_ms=89172 unoptimized_lazy_ready_ms=4210 unoptimized_lazy_prepare_ms=310 optimized_lazy_ready_ms=3125 optimized_lazy_prepare_ms=243 unoptimized_vs_baseline_delta_ms=-84962 optimized_vs_baseline_delta_ms=-86047 optimized_vs_unoptimized_delta_ms=-1085",
                output,
            )
            self.assertNotIn("lazy_ready_improvement_pct", output)

    def test_print_benchmark_summary_reports_invalid_lazy_result_without_lazy_metric(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            csv_path = work_dir / "coldstart_compare.csv"
            csv_path.write_text(
                "\n".join(
                    [
                        "ts,label,snapshotter,mode,pull_ms,run_ms,ready_wait_ms,end_to_end_ready_ms,ready_ms,lazy_prepare_ms,lazy_ready_ms,ready_ms_semantics,status,err",
                        "2026-04-19T00:00:00+00:00,baseline,overlayfs,ready,85819,3353,0,89172,89172,-1,-1,end_to_end_ready_ms,ok,",
                        "2026-04-19T00:00:01+00:00,optimized,stargz,ready,0,74900,0,74920,74920,20,74920,lazy_ready_ms,fail,optimized-lazy-path-performed-full-pull",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            module = load_oneclick_product_module()
            stdout = io.StringIO()

            with mock.patch.object(module.sys, "stdout", stdout):
                module._print_benchmark_summary(
                    {
                        "status": "failed",
                        "csv": str(csv_path),
                        "comparison_mode": "raw-full-vs-estargz-lazy",
                        "invalid_reasons": [
                            "optimized lazy path appears to have performed a full pull; result is invalid"
                        ],
                        "warnings": [
                            "optimized path appears to have performed a full pull; lazy-pull result may be invalid"
                        ],
                        "summary": {
                            "labels": {
                                "baseline": {
                                    "median_pull_ms": 85819.0,
                                    "median_post_pull_ready_ms": 3353.0,
                                    "median_end_to_end_ready_ms": 89172.0,
                                    "median_full_pull_ready_ms": 89172.0,
                                },
                                "optimized": {
                                    "median_lazy_prepare_ms": None,
                                    "median_lazy_ready_ms": None,
                                    "median_post_pull_ready_ms": None,
                                    "median_end_to_end_ready_ms": None,
                                },
                            },
                            "comparison": {},
                        },
                    }
                )

            output = stdout.getvalue()
            self.assertIn("benchmark-summary status=failed", output)
            self.assertIn(
                "optimized lazy path appears to have performed a full pull; result is invalid",
                output,
            )
            self.assertNotIn("lazy_ready_improvement_pct", output)

    def test_print_benchmark_summary_reports_invalid_triple_lazy_result_with_label_specific_reason(self):
        module = load_oneclick_product_module()
        stdout = io.StringIO()

        with mock.patch.object(module.sys, "stdout", stdout):
            module._print_benchmark_summary(
                {
                    "status": "failed",
                    "comparison_mode": "raw-full-vs-estargz-lazy-triple",
                    "invalid_reasons": [
                        "unoptimized_lazy lazy path appears to have performed a full pull; result is invalid"
                    ],
                    "warnings": [
                        "unoptimized_lazy path appears to have performed a full pull; lazy-pull result may be invalid"
                    ],
                    "summary": {
                        "labels": {
                            "baseline": {"median_full_pull_ready_ms": 89172.0},
                            "unoptimized_lazy": {"median_lazy_ready_ms": None},
                            "optimized": {"median_lazy_ready_ms": 3125.0},
                        },
                        "comparison": {},
                    },
                }
            )

        output = stdout.getvalue()
        self.assertIn("benchmark-summary status=failed", output)
        self.assertIn(
            "unoptimized_lazy lazy path appears to have performed a full pull; result is invalid",
            output,
        )
        self.assertNotIn(
            "optimized lazy path appears to have performed a full pull; result is invalid",
            output,
        )

    def test_print_benchmark_summary_keeps_legacy_ready_output_when_post_pull_missing(self):
        module = load_oneclick_product_module()
        stdout = io.StringIO()

        with mock.patch.object(module.sys, "stdout", stdout):
            module._print_benchmark_summary(
                {
                    "status": "passed",
                    "summary": {
                        "labels": {
                            "baseline": {"median_ready_ms": 120.0},
                            "optimized": {"median_ready_ms": 90.0},
                        },
                        "comparison": {"median_ready_improvement_pct": 25.0},
                    },
                }
            )

        output = stdout.getvalue()
        self.assertIn(
            "benchmark-summary status=passed baseline_median_ready_ms=120.0 optimized_median_ready_ms=90.0",
            output,
        )
        self.assertIn("improvement_pct=25.00%", output)
        self.assertNotIn("baseline_median_post_pull_ready_ms", output)
        self.assertNotIn("post_pull_ready_improvement_pct", output)

    def test_benchmark_cli_prints_per_run_ready_comparison_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260325_000022",
                work_dir=work_dir,
                image="docker.io/library/app:base",
                startup_mode="legacy",
                llm_enabled=False,
            )
            csv_path = work_dir / "coldstart_compare.csv"
            csv_path.write_text(
                "\n".join(
                    [
                        "ts,label,snapshotter,mode,ready_ms,predict_extra_ms,proof_exists,proof_done,status,err",
                        "2026-04-11T06:00:00+00:00,baseline,overlayfs,ready,4792,-1,10,0,ok,",
                        "2026-04-11T06:00:10+00:00,optimized,stargz,ready,4869,-1,10,0,ok,",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            module = load_oneclick_product_module()

            def fake_run_benchmark_stage(**_kwargs):
                result_path = work_dir / "benchmark_result.json"
                result_path.write_text("{}\n", encoding="utf-8")
                log_dir = work_dir / "bench_logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                return SimpleNamespace(
                    env_check_base_path=work_dir / "env_check_benchmark_base.json",
                    env_check_opt_path=work_dir / "env_check_benchmark_opt.json",
                    policy_path=work_dir / "benchmark_policy.json",
                    stage_log_path=work_dir / "benchmark.log",
                    csv_path=csv_path,
                    log_dir=log_dir,
                    result_path=result_path,
                    valid=True,
                    invalid_reasons=[],
                    warnings=[],
                    summary_payload={
                        "status": "passed",
                        "csv": str(csv_path),
                        "summary": {
                            "labels": {
                                "baseline": {"median_ready_ms": 4792.0},
                                "optimized": {"median_ready_ms": 4869.0},
                            },
                            "comparison": {"median_ready_improvement_pct": -1.61},
                        },
                    },
                )

            argv = [
                "oneclick_product.py",
                "benchmark",
                "--work-dir",
                str(work_dir),
                "--base-image",
                "docker.io/library/app:base",
                "--opt-image",
                "docker.io/library/app:opt",
            ]
            stdout = io.StringIO()
            with mock.patch.object(module, "run_benchmark_stage", side_effect=fake_run_benchmark_stage):
                with mock.patch.object(module.sys, "argv", argv):
                    with mock.patch.object(module.sys, "stdout", stdout):
                        rc = module.main()

            self.assertEqual(0, rc)
            output = stdout.getvalue()
            self.assertIn(
                "benchmark-summary status=passed baseline_median_ready_ms=4792.0 optimized_median_ready_ms=4869.0",
                output,
            )
            self.assertIn("run1 baseline_ready_ms=4792 optimized_ready_ms=4869 delta_ms=+77", output)

    def test_publish_cli_runs_stage_and_records_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260320_000100",
                work_dir=work_dir,
                image="docker.io/library/app:base",
                startup_mode="legacy",
                llm_enabled=False,
            )
            module = load_oneclick_product_module()
            publish_result = work_dir / "publish_result.json"
            publish_result.write_text("{}\n", encoding="utf-8")

            publish_mock = mock.Mock(
                return_value=SimpleNamespace(
                    stage_log_path=work_dir / "publish.log",
                    result_path=publish_result,
                    src_image="docker.io/library/app:base",
                    dst_image="127.0.0.1:5001/docker.io/library/app:base",
                    namespace="default",
                    plain_http=True,
                )
            )
            argv = [
                "oneclick_product.py",
                "publish",
                "--work-dir",
                str(work_dir),
                "--dst-image",
                "127.0.0.1:5001/docker.io/library/app:base",
            ]
            with mock.patch.object(module, "run_publish_stage", publish_mock):
                with mock.patch.object(module.sys, "argv", argv):
                    rc = module.main()

            self.assertEqual(0, rc)
            publish_mock.assert_called_once()
            self.assertEqual(
                "127.0.0.1:5001/docker.io/library/app:base",
                publish_mock.call_args.kwargs["dst_image"],
            )
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("passed", manifest_payload["stages"]["publish"]["status"])
            self.assertEqual(
                str(work_dir / "publish.log"),
                manifest_payload["artifacts"]["publish_log"],
            )
            publish_payload = json.loads(publish_result.read_text(encoding="utf-8"))
            self.assertEqual("manifest", publish_payload["source_kind"])
            self.assertFalse(publish_payload["optimized_publish"])
            self.assertNotIn("benchmark_gate", publish_payload)

    def test_publish_cli_optimized_uses_selector_mode_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            manifest = ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260325_000010",
                work_dir=work_dir,
                image="docker.io/library/app:base",
                startup_mode="legacy",
                llm_enabled=False,
            )
            manifest.data.setdefault("options", {})["selector_mode"] = "chunk-shadow"
            manifest.save()
            (work_dir / "optimized_image_chunk_shadow.txt").write_text(
                "docker.io/library/app:opt-chunk-shadow\n",
                encoding="utf-8",
            )
            module = load_oneclick_product_module()
            publish_result = work_dir / "publish_result.json"
            publish_result.write_text("{}\n", encoding="utf-8")

            publish_mock = mock.Mock(
                return_value=SimpleNamespace(
                    stage_log_path=work_dir / "publish.log",
                    result_path=publish_result,
                    src_image="docker.io/library/app:opt-chunk-shadow",
                    dst_image="127.0.0.1:5001/docker.io/library/app:opt-chunk-shadow",
                    namespace="default",
                    plain_http=True,
                )
            )
            argv = [
                "oneclick_product.py",
                "publish",
                "--work-dir",
                str(work_dir),
                "--optimized",
                "--selector-mode",
                "chunk-shadow",
                "--dst-image",
                "127.0.0.1:5001/docker.io/library/app:opt-chunk-shadow",
            ]
            with mock.patch.object(module, "run_publish_stage", publish_mock):
                with mock.patch.object(module.sys, "argv", argv):
                    rc = module.main()

            self.assertEqual(0, rc)
            publish_mock.assert_called_once()
            self.assertEqual(
                "docker.io/library/app:opt-chunk-shadow",
                publish_mock.call_args.kwargs["src_image"],
            )
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("passed", manifest_payload["stages"]["publish"]["status"])
            self.assertEqual(
                "Registry publish completed from optimized image",
                manifest_payload["stages"]["publish"]["summary"],
            )
            publish_payload = json.loads(publish_result.read_text(encoding="utf-8"))
            self.assertEqual("optimized", publish_payload["source_kind"])
            self.assertTrue(publish_payload["optimized_publish"])
            self.assertEqual("chunk-shadow", publish_payload["selector_mode"])
            self.assertNotIn("benchmark_gate", publish_payload)

    def test_benchmark_cli_rejects_invalid_comparison_even_with_result_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260318_000002",
                work_dir=work_dir,
                image="docker.io/library/app:base",
                startup_mode="legacy",
                llm_enabled=False,
            )
            module = load_oneclick_product_module()
            result_path = work_dir / "benchmark_result.json"
            result_path.write_text("{}\n", encoding="utf-8")
            csv_path = work_dir / "coldstart_compare.csv"
            csv_path.write_text(
                "ts,label,snapshotter,mode,ready_ms,predict_extra_ms,proof_exists,proof_done,status,err\n",
                encoding="utf-8",
            )
            log_dir = work_dir / "bench_logs"
            log_dir.mkdir(parents=True, exist_ok=True)

            benchmark_mock = mock.Mock(
                return_value=SimpleNamespace(
                    env_check_base_path=work_dir / "env_check_benchmark_base.json",
                    env_check_opt_path=work_dir / "env_check_benchmark_opt.json",
                    policy_path=work_dir / "benchmark_policy.json",
                    stage_log_path=work_dir / "benchmark.log",
                    csv_path=csv_path,
                    log_dir=log_dir,
                    result_path=result_path,
                    valid=False,
                    invalid_reasons=["baseline produced 0 successful benchmark rows"],
                    warnings=[],
                )
            )

            argv = [
                "oneclick_product.py",
                "benchmark",
                "--work-dir",
                str(work_dir),
                "--base-image",
                "docker.io/library/app:base",
                "--opt-image",
                "docker.io/library/app:opt",
                "--mode",
                "ready",
                "--ready-log-regex",
                "server ready",
            ]
            with mock.patch.object(module, "run_benchmark_stage", benchmark_mock):
                with mock.patch.object(module.sys, "argv", argv):
                    rc = module.main()

            self.assertEqual(1, rc)
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("failed", manifest_payload["stages"]["benchmark"]["status"])
            self.assertIn(
                "baseline produced 0 successful benchmark rows",
                manifest_payload["stages"]["benchmark"]["error"],
            )
            self.assertEqual(str(result_path), manifest_payload["artifacts"]["benchmark_result"])

    def test_benchmark_cli_creates_manifest_from_base_image_on_new_work_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            module = load_oneclick_product_module()
            result_path = work_dir / "benchmark_result.json"
            csv_path = work_dir / "coldstart_compare.csv"
            log_dir = work_dir / "bench_logs"

            benchmark_mock = mock.Mock(
                return_value=SimpleNamespace(
                    env_check_base_path=work_dir / "env_check_benchmark_base.json",
                    env_check_opt_path=work_dir / "env_check_benchmark_opt.json",
                    policy_path=work_dir / "benchmark_policy.json",
                    stage_log_path=work_dir / "benchmark.log",
                    csv_path=csv_path,
                    log_dir=log_dir,
                    result_path=result_path,
                    valid=True,
                    invalid_reasons=[],
                    warnings=[],
                )
            )

            argv = [
                "oneclick_product.py",
                "benchmark",
                "--work-dir",
                str(work_dir),
                "--base-image",
                "docker.io/library/app:base",
                "--opt-image",
                "docker.io/library/app:opt",
            ]
            with mock.patch.object(module, "run_benchmark_stage", benchmark_mock):
                with mock.patch.object(module.sys, "argv", argv):
                    rc = module.main()

            self.assertEqual(0, rc)
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("docker.io/library/app:base", manifest_payload["options"]["image"])
            self.assertEqual("passed", manifest_payload["stages"]["benchmark"]["status"])

    def test_apply_cli_marks_stage_cancelled_on_keyboard_interrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260318_000003",
                work_dir=work_dir,
                image="docker.io/library/app:base",
                startup_mode="legacy",
                llm_enabled=False,
            )
            module = load_oneclick_product_module()

            argv = [
                "oneclick_product.py",
                "apply",
                "--work-dir",
                str(work_dir),
                "--src-image",
                "docker.io/library/app:base",
                "--dst-image",
                "docker.io/library/app:opt",
            ]
            with mock.patch.object(module, "run_apply_stage", side_effect=KeyboardInterrupt):
                with mock.patch.object(module.sys, "argv", argv):
                    rc = module.main()

            self.assertEqual(130, rc)
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("cancelled", manifest_payload["stages"]["apply"]["status"])
            self.assertEqual("interrupted by user", manifest_payload["stages"]["apply"]["error"])

    def test_benchmark_cli_marks_stage_cancelled_on_keyboard_interrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260318_000004",
                work_dir=work_dir,
                image="docker.io/library/app:base",
                startup_mode="legacy",
                llm_enabled=False,
            )
            (work_dir / "optimized_image.txt").write_text("docker.io/library/app:opt\n", encoding="utf-8")
            module = load_oneclick_product_module()

            argv = [
                "oneclick_product.py",
                "benchmark",
                "--work-dir",
                str(work_dir),
                "--base-image",
                "docker.io/library/app:base",
                "--opt-image",
                "docker.io/library/app:opt",
            ]
            with mock.patch.object(module, "run_benchmark_stage", side_effect=KeyboardInterrupt):
                with mock.patch.object(module.sys, "argv", argv):
                    rc = module.main()

            self.assertEqual(130, rc)
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("cancelled", manifest_payload["stages"]["benchmark"]["status"])
            self.assertEqual("interrupted by user", manifest_payload["stages"]["benchmark"]["error"])

    def test_sample_cli_rejects_inject_io_without_effective_workload_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            module = load_oneclick_product_module()

            argv = [
                "oneclick_product.py",
                "sample",
                "docker.io/library/app:latest",
                "--work-dir",
                str(work_dir),
                "--inject-io",
            ]
            with mock.patch.object(module, "run_preflight") as preflight_mock:
                with mock.patch.object(module, "run_sample_stage") as sample_mock:
                    with mock.patch.object(module.sys, "argv", argv):
                        rc = module.main()

            self.assertEqual(1, rc)
            preflight_mock.assert_not_called()
            sample_mock.assert_not_called()
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual({}, manifest_payload["stages"])

    def test_sample_cli_merges_model_host_into_container_runtime_mounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            model_dir = Path(tmp) / "model"
            model_dir.mkdir()
            module = load_oneclick_product_module()

            preflight_result = SimpleNamespace(
                env_check_path=work_dir / "env_check.json",
                policy_path=work_dir / "product_policy.json",
                policy_payload={"ok": True, "warnings": []},
            )
            sample_result = SimpleNamespace(
                runtime_state_path=work_dir / "runtime_state.json",
                raw_events_path=work_dir / "raw_events.jsonl",
                sampler_log_path=work_dir / "sampler.log",
                sampler_metrics_path=work_dir / "sampler_metrics.prom",
            )

            argv = [
                "oneclick_product.py",
                "sample",
                "docker.io/tensorflow/serving:latest",
                "--work-dir",
                str(work_dir),
                "--workload",
                "tf-serving-half-plus-two",
                "--model-host",
                str(model_dir),
            ]
            with mock.patch.object(module, "run_preflight", return_value=preflight_result):
                with mock.patch.object(module, "run_sample_stage", return_value=sample_result) as sample_mock:
                    with mock.patch.object(module.sys, "argv", argv):
                        rc = module.main()

            self.assertEqual(0, rc)
            runtime_spec = sample_mock.call_args.kwargs["container_runtime"]
            self.assertIn(
                f"type=bind,src={model_dir.resolve()},dst=/models/half_plus_two,readonly",
                runtime_spec.mounts,
            )
            self.assertIn("MODEL_NAME=half_plus_two", runtime_spec.env)
            self.assertIn("MODEL_BASE_PATH=/models", runtime_spec.env)
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertIn(
                f"type=bind,src={model_dir.resolve()},dst=/models/half_plus_two,readonly",
                manifest_payload["options"]["sample_container_runtime"]["mounts"],
            )
            self.assertIn(
                "MODEL_NAME=half_plus_two",
                manifest_payload["options"]["sample_container_runtime"]["env"],
            )
            self.assertIn(
                "MODEL_BASE_PATH=/models",
                manifest_payload["options"]["sample_container_runtime"]["env"],
            )
            self.assertEqual("passed", manifest_payload["stages"]["sample"]["status"])

    def test_sample_cli_merges_model_host_into_container_runtime_mounts_for_efficientnet(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            model_dir = Path(tmp) / "model"
            model_dir.mkdir()
            module = load_oneclick_product_module()

            preflight_result = SimpleNamespace(
                env_check_path=work_dir / "env_check.json",
                policy_path=work_dir / "product_policy.json",
                policy_payload={"ok": True, "warnings": []},
            )
            sample_result = SimpleNamespace(
                runtime_state_path=work_dir / "runtime_state.json",
                raw_events_path=work_dir / "raw_events.jsonl",
                sampler_log_path=work_dir / "sampler.log",
                sampler_metrics_path=work_dir / "sampler_metrics.prom",
            )

            argv = [
                "oneclick_product.py",
                "sample",
                "docker.io/tensorflow/serving:latest",
                "--work-dir",
                str(work_dir),
                "--workload",
                "tf-serving-efficientnet-v2-b0",
                "--model-host",
                str(model_dir),
            ]
            with mock.patch.object(module, "run_preflight", return_value=preflight_result):
                with mock.patch.object(module, "run_sample_stage", return_value=sample_result) as sample_mock:
                    with mock.patch.object(module.sys, "argv", argv):
                        rc = module.main()

            self.assertEqual(0, rc)
            runtime_spec = sample_mock.call_args.kwargs["container_runtime"]
            self.assertIn(
                f"type=bind,src={model_dir.resolve()},dst=/models/efficientnet_v2_b0,readonly",
                runtime_spec.mounts,
            )
            self.assertIn("MODEL_NAME=efficientnet_v2_b0", runtime_spec.env)
            self.assertIn("MODEL_BASE_PATH=/models", runtime_spec.env)
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertIn(
                f"type=bind,src={model_dir.resolve()},dst=/models/efficientnet_v2_b0,readonly",
                manifest_payload["options"]["sample_container_runtime"]["mounts"],
            )
            self.assertIn(
                "MODEL_NAME=efficientnet_v2_b0",
                manifest_payload["options"]["sample_container_runtime"]["env"],
            )
            self.assertIn(
                "MODEL_BASE_PATH=/models",
                manifest_payload["options"]["sample_container_runtime"]["env"],
            )
            self.assertEqual("passed", manifest_payload["stages"]["sample"]["status"])

    def test_sample_cli_forwards_startup_backend_preference(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            module = load_oneclick_product_module()

            preflight_result = SimpleNamespace(
                env_check_path=work_dir / "env_check.json",
                policy_path=work_dir / "product_policy.json",
                policy_payload={"ok": True, "warnings": []},
            )
            sample_result = SimpleNamespace(
                runtime_state_path=work_dir / "runtime_state.json",
                raw_events_path=work_dir / "raw_events.jsonl",
                sampler_log_path=work_dir / "sampler.log",
                sampler_metrics_path=work_dir / "sampler_metrics.prom",
            )

            argv = [
                "oneclick_product.py",
                "sample",
                "docker.io/library/app:latest",
                "--work-dir",
                str(work_dir),
                "--startup-mode",
                "gated",
                "--startup-backend-preference",
                "prefer-host-launcher",
            ]
            with mock.patch.object(module, "run_preflight", return_value=preflight_result):
                with mock.patch.object(module, "run_sample_stage", return_value=sample_result) as sample_mock:
                    with mock.patch.object(module.sys, "argv", argv):
                        rc = module.main()

            self.assertEqual(0, rc)
            self.assertEqual(
                "prefer-host-launcher",
                sample_mock.call_args.kwargs["startup_backend_preference"],
            )
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                "prefer-host-launcher",
                manifest_payload["options"]["startup_backend_preference"],
            )

    def test_sample_cli_accepts_prefer_image_shell_backend_preference(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            module = load_oneclick_product_module()

            preflight_result = SimpleNamespace(
                env_check_path=work_dir / "env_check.json",
                policy_path=work_dir / "product_policy.json",
                policy_payload={"ok": True, "warnings": []},
            )
            sample_result = SimpleNamespace(
                runtime_state_path=work_dir / "runtime_state.json",
                raw_events_path=work_dir / "raw_events.jsonl",
                sampler_log_path=work_dir / "sampler.log",
                sampler_metrics_path=work_dir / "sampler_metrics.prom",
            )

            argv = [
                "oneclick_product.py",
                "sample",
                "docker.io/library/app:latest",
                "--work-dir",
                str(work_dir),
                "--startup-mode",
                "gated",
                "--startup-backend-preference",
                "prefer-image-shell",
            ]
            with mock.patch.object(module, "run_preflight", return_value=preflight_result):
                with mock.patch.object(module, "run_sample_stage", return_value=sample_result) as sample_mock:
                    with mock.patch.object(module.sys, "argv", argv):
                        rc = module.main()

            self.assertEqual(0, rc)
            self.assertEqual(
                "prefer-image-shell",
                sample_mock.call_args.kwargs["startup_backend_preference"],
            )
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                "prefer-image-shell",
                manifest_payload["options"]["startup_backend_preference"],
            )

    def test_sample_cli_records_failure_artifacts_when_sampling_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            module = load_oneclick_product_module()

            preflight_result = SimpleNamespace(
                env_check_path=work_dir / "env_check.json",
                policy_path=work_dir / "product_policy.json",
                policy_payload={"ok": True, "warnings": []},
            )

            def fail_sample_stage(**_kwargs):
                (work_dir / "runtime_state.json").write_text(
                    json.dumps(
                        {
                            "status": "failed",
                            "startup_mode_requested": "gated",
                            "startup_mode_used": "gated",
                            "startup_backend": "gated_host_launcher_dynamic",
                            "failure": {
                                "error": "sampler /metrics did not become ready",
                                "recent_container_logs": "exec /__cpp_sampler_gate_launcher: no such file or directory",
                            },
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                (work_dir / "raw_events.jsonl").write_text("", encoding="utf-8")
                (work_dir / "sampler.log").write_text("sample failed\n", encoding="utf-8")
                (work_dir / "sampler_metrics.prom").write_text("", encoding="utf-8")
                raise RuntimeError("sampler /metrics did not become ready")

            argv = [
                "oneclick_product.py",
                "sample",
                "docker.io/library/app:latest",
                "--work-dir",
                str(work_dir),
                "--startup-mode",
                "gated",
            ]
            with mock.patch.object(module, "run_preflight", return_value=preflight_result):
                with mock.patch.object(module, "run_sample_stage", side_effect=fail_sample_stage):
                    with mock.patch.object(module.sys, "argv", argv):
                        rc = module.main()

            self.assertEqual(1, rc)
            manifest_payload = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            sample_stage = manifest_payload["stages"]["sample"]
            self.assertEqual("failed", sample_stage["status"])
            self.assertEqual("sampler /metrics did not become ready", sample_stage["error"])
            self.assertEqual(str(work_dir / "runtime_state.json"), sample_stage["artifacts"]["runtime_state"])
            self.assertEqual(str(work_dir / "raw_events.jsonl"), sample_stage["artifacts"]["raw_events"])
            self.assertEqual(str(work_dir / "sampler.log"), sample_stage["artifacts"]["sampler_log"])
            self.assertEqual(str(work_dir / "sampler_metrics.prom"), sample_stage["artifacts"]["sampler_metrics"])

    def test_benchmark_cli_stops_sample_container_from_same_work_dir_before_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260320_000005",
                work_dir=work_dir,
                image="docker.io/tensorflow/serving:latest",
                startup_mode="legacy",
                llm_enabled=False,
            )
            (work_dir / "runtime_state.json").write_text(
                json.dumps(
                    {
                        "container_name": "demo-sample-container",
                        "container_pid": 1234,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (work_dir / "optimized_image.txt").write_text(
                "docker.io/tensorflow/serving:half-plus-two-stargz\n",
                encoding="utf-8",
            )
            module = load_oneclick_product_module()

            benchmark_mock = mock.Mock(
                return_value=SimpleNamespace(
                    env_check_base_path=work_dir / "env_check_benchmark_base.json",
                    env_check_opt_path=work_dir / "env_check_benchmark_opt.json",
                    policy_path=work_dir / "benchmark_policy.json",
                    stage_log_path=work_dir / "benchmark.log",
                    csv_path=work_dir / "coldstart_compare.csv",
                    log_dir=work_dir / "bench_logs",
                    result_path=work_dir / "benchmark_result.json",
                    valid=True,
                    invalid_reasons=[],
                    warnings=[],
                    summary_payload={},
                )
            )

            def fake_nerdctl(_repo_root, args, **kwargs):
                if args[:2] == ["inspect", "-f"]:
                    return subprocess.CompletedProcess(["nerdctl", *args], 0, "true\n", "")
                return subprocess.CompletedProcess(["nerdctl", *args], 0, "", "")

            argv = [
                "oneclick_product.py",
                "benchmark",
                "--work-dir",
                str(work_dir),
                "--base-image",
                "docker.io/tensorflow/serving:latest",
                "--opt-image",
                "docker.io/tensorflow/serving:half-plus-two-stargz",
                "--mode",
                "ready",
                "--workload",
                "tf-serving-half-plus-two",
            ]
            with mock.patch.object(module, "run_benchmark_stage", benchmark_mock):
                with mock.patch.object(module, "nerdctl", side_effect=fake_nerdctl) as nerdctl_mock:
                    with mock.patch.object(module.sys, "argv", argv):
                        rc = module.main()

            self.assertEqual(0, rc)
            call_args = [call.args[1] for call in nerdctl_mock.call_args_list]
            self.assertIn(["stop", "demo-sample-container"], call_args)
            self.assertIn(["rm", "demo-sample-container"], call_args)


if __name__ == "__main__":
    unittest.main()
