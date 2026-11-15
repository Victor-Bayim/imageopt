import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.manifest import ManifestStore
from orchestrator.stages.report import run_report_stage


class ReportStageTests(unittest.TestCase):
    def test_report_stage_summarizes_preflight_gated_backend_capabilities(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            manifest = ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260325_090000",
                work_dir=work_dir,
                image="docker.io/library/app:latest",
                startup_mode="gated",
                llm_enabled=False,
            )
            (work_dir / "env_check.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "checks": [
                            {"id": "image_gated_startup_shell", "status": "warn"},
                            {"id": "host_gated_startup_launcher", "status": "pass"},
                            {"id": "host_gated_startup_launcher_static", "status": "pass"},
                            {"id": "host_gated_startup_launcher_dynamic", "status": "warn"},
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            product_policy = {
                "ok": True,
                "startup_mode": "gated",
                "startup_backend_preference": "auto",
                "env_check_exit_code": 0,
                "warnings": [
                    "gated startup will use the host launcher backend because /bin/sh is unavailable in the image",
                ],
                "errors": [],
            }
            (work_dir / "product_policy.json").write_text(
                json.dumps(product_policy, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            manifest.data["product_policy"] = product_policy
            manifest.stage_finish(
                "preflight",
                status="passed",
                summary="preflight ok",
                artifacts={
                    "env_check": str(work_dir / "env_check.json"),
                    "product_policy": str(work_dir / "product_policy.json"),
                },
            )

            result = run_report_stage(work_dir=work_dir, manifest=ManifestStore.load(work_dir / "manifest.json"))

            self.assertTrue(result.summary_payload["preflight"]["ok"])
            self.assertEqual(
                "pass",
                result.summary_payload["preflight"]["host_gated_startup_launcher_static_status"],
            )
            self.assertEqual(
                "warn",
                result.summary_payload["preflight"]["host_gated_startup_launcher_dynamic_status"],
            )
            report_md = result.report_md_path.read_text(encoding="utf-8")
            self.assertIn("## Preflight", report_md)
            self.assertIn("Host launcher static build status: `pass`", report_md)
            self.assertIn("Host launcher dynamic build status: `warn`", report_md)
            self.assertIn("Any gated startup backend available: `True`", report_md)

    def test_report_stage_summarizes_apply_and_benchmark_without_quality_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            manifest = ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260317_000000",
                work_dir=work_dir,
                image="docker.io/library/app:latest",
                startup_mode="gated",
                llm_enabled=False,
            )
            (work_dir / "runtime_state.json").write_text(
                json.dumps(
                    {
                        "startup_mode_used": "gated",
                        "startup_backend": "gated_host_launcher_static",
                        "workload_profile": "tf-serving-half-plus-two",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (work_dir / "quality_gate.json").write_text(
                json.dumps({"ok": True, "warnings": [], "reasons": []}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            (work_dir / "selector_stats_default.json").write_text(
                json.dumps({"chosen": 4, "budget_bytes": 1048576, "chosen_bytes": 65536}, ensure_ascii=False, indent=2)
                + "\n",
                encoding="utf-8",
            )
            (work_dir / "apply_result.json").write_text(
                json.dumps(
                    {
                        "status": "passed",
                        "selector_mode": "file",
                        "src_image": "docker.io/library/app:latest",
                        "dst_image": "docker.io/library/app:latest-stargz",
                        "dry_run": False,
                        "optimized_image_written": True,
                        "optimized_image": "docker.io/library/app:latest-stargz",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (work_dir / "benchmark_result.json").write_text(
                json.dumps(
                    {
                        "status": "passed",
                        "base_image": "docker.io/library/app:latest",
                        "opt_image": "docker.io/library/app:latest-stargz",
                        "comparison_mode": "same-snapshotter",
                        "ready_ms_semantics": "end_to_end_ready_ms",
                        "selector_mode": "file",
                        "summary": {
                            "rows_total": 6,
                            "labels": {
                                "baseline": {
                                    "median_ready_ms": 120.0,
                                    "median_end_to_end_ready_ms": 120.0,
                                    "median_post_pull_ready_ms": 80.0,
                                    "median_service_bootstrap_ms": 70.0,
                                },
                                "optimized": {
                                    "median_ready_ms": 90.0,
                                    "median_end_to_end_ready_ms": 90.0,
                                    "median_post_pull_ready_ms": 60.0,
                                    "median_service_bootstrap_ms": 50.0,
                                },
                            },
                            "comparison": {
                                "median_ready_delta_ms": -30.0,
                                "median_ready_improvement_pct": 25.0,
                                "median_end_to_end_improvement_pct": 25.0,
                                "median_post_pull_ready_delta_ms": -20.0,
                                "median_post_pull_ready_improvement_pct": 25.0,
                                "median_service_bootstrap_delta_ms": -20.0,
                            },
                            "valid": True,
                            "invalid_reasons": [],
                            "warnings": [],
                        },
                        "valid": True,
                        "invalid_reasons": [],
                        "warnings": [],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            manifest.stage_finish(
                "sample",
                status="passed",
                summary="sample ok",
                artifacts={"runtime_state": str(work_dir / "runtime_state.json")},
            )
            manifest.stage_finish(
                "analyze",
                status="passed",
                summary="analyze ok",
                artifacts={
                    "quality_gate": str(work_dir / "quality_gate.json"),
                    "selector_stats_default": str(work_dir / "selector_stats_default.json"),
                },
            )
            manifest.stage_finish(
                "apply",
                status="passed",
                summary="apply ok",
                artifacts={"apply_result": str(work_dir / "apply_result.json")},
            )
            manifest.stage_finish(
                "benchmark",
                status="passed",
                summary="benchmark ok",
                artifacts={"benchmark_result": str(work_dir / "benchmark_result.json")},
            )
            manifest.stage_begin("report", "Generate a unified run report from current stage artifacts")

            result = run_report_stage(work_dir=work_dir, manifest=ManifestStore.load(work_dir / "manifest.json"))
            payload = json.loads(result.report_json_path.read_text(encoding="utf-8"))
            markdown = result.report_md_path.read_text(encoding="utf-8")

            self.assertEqual("passed", payload["overall_status"])
            self.assertEqual("passed", payload["benchmark"]["status"])
            self.assertTrue(payload["benchmark"]["valid"])
            self.assertEqual(120.0, payload["benchmark"]["baseline_median_ready_ms"])
            self.assertEqual(90.0, payload["benchmark"]["optimized_median_ready_ms"])
            self.assertEqual(80.0, payload["benchmark"]["baseline_median_post_pull_ready_ms"])
            self.assertEqual(60.0, payload["benchmark"]["optimized_median_post_pull_ready_ms"])
            self.assertEqual(-30.0, payload["benchmark"]["comparison"]["median_ready_delta_ms"])
            self.assertNotIn("publish", payload)
            self.assertNotIn("publish_ready", payload)
            self.assertNotIn("publish_advice", payload)
            self.assertNotIn("prefetch_quality_status", payload["benchmark"])
            self.assertNotIn("prefetch_feedback", payload["benchmark"])
            self.assertNotIn("hot_path_feedback", payload["benchmark"])
            self.assertNotIn("feedback_warnings", payload["benchmark"])
            self.assertNotIn("result_contract", payload["benchmark"])
            self.assertIn("## Benchmark", markdown)
            self.assertIn("Improvement pct", markdown)
            self.assertIn("End-to-end ready: `pull + run + ready wait`", markdown)
            self.assertIn("Post-pull ready: `run + ready wait`", markdown)
            self.assertIn("Service bootstrap: `application-internal /debug/state value`", markdown)
            self.assertNotIn("Prefetch quality", markdown)
            self.assertNotIn("proof_exists", markdown)
            self.assertNotIn("## Publish", markdown)
            self.assertNotIn("## Publish Advice", markdown)

    def test_report_stage_warns_when_raw_vs_estargz_path_is_pull_dominated(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            manifest = ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260419_000100",
                work_dir=work_dir,
                image="127.0.0.1:5001/abtest/ready-serving-layout-trap-v5:src-raw",
                startup_mode="legacy",
                llm_enabled=False,
            )
            (work_dir / "benchmark_result.json").write_text(
                json.dumps(
                    {
                        "status": "passed",
                        "base_image": "127.0.0.1:5001/abtest/ready-serving-layout-trap-v5:src-raw",
                        "opt_image": "127.0.0.1:5001/abtest/ready-serving-layout-trap-v5:esgz-hinted-v1",
                        "cold_mode": "registry-cold-strict",
                        "comparison_mode": "raw-vs-estargz",
                        "ready_ms_semantics": "end_to_end_ready_ms",
                        "selector_mode": "file",
                        "summary": {
                            "rows_total": 2,
                            "labels": {
                                "baseline": {
                                    "median_ready_ms": 940.0,
                                    "median_pull_ms": 820.0,
                                    "median_run_ms": 40.0,
                                    "median_ready_wait_ms": 80.0,
                                    "median_post_pull_ready_ms": 120.0,
                                    "median_end_to_end_ready_ms": 940.0,
                                    "median_service_bootstrap_ms": 780.0,
                                },
                                "optimized": {
                                    "median_ready_ms": 820.0,
                                    "median_pull_ms": 760.0,
                                    "median_run_ms": 20.0,
                                    "median_ready_wait_ms": 40.0,
                                    "median_post_pull_ready_ms": 60.0,
                                    "median_end_to_end_ready_ms": 820.0,
                                    "median_service_bootstrap_ms": 530.0,
                                },
                            },
                            "comparison": {
                                "median_ready_delta_ms": -120.0,
                                "median_ready_improvement_pct": 12.7659574468,
                                "median_pull_delta_ms": -60.0,
                                "median_post_pull_ready_delta_ms": -60.0,
                                "median_post_pull_ready_improvement_pct": 50.0,
                                "median_end_to_end_delta_ms": -120.0,
                                "median_end_to_end_improvement_pct": 12.7659574468,
                                "median_service_bootstrap_delta_ms": -250.0,
                            },
                            "valid": True,
                            "invalid_reasons": [],
                            "warnings": [],
                        },
                        "valid": True,
                        "invalid_reasons": [],
                        "warnings": [],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            manifest.stage_finish(
                "benchmark",
                status="passed",
                summary="benchmark ok",
                artifacts={"benchmark_result": str(work_dir / "benchmark_result.json")},
            )
            manifest.stage_begin("report", "Generate a unified run report from current stage artifacts")

            result = run_report_stage(work_dir=work_dir, manifest=ManifestStore.load(work_dir / "manifest.json"))
            payload = json.loads(result.report_json_path.read_text(encoding="utf-8"))
            markdown = result.report_md_path.read_text(encoding="utf-8")

            self.assertIn(
                "The current runtime path appears pull-dominated; lazy pulling benefits may be masked by full image transfer behavior.",
                payload["benchmark"]["warnings"],
            )
            self.assertIn("Comparison mode: `raw-vs-estargz`", markdown)
            self.assertIn("Post-pull improvement pct", markdown)
            self.assertIn(
                "The current runtime path appears pull-dominated; lazy pulling benefits may be masked by full image transfer behavior.",
                markdown,
            )

    def test_report_stage_keeps_benchmark_blank_when_benchmark_not_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            manifest = ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260317_000001",
                work_dir=work_dir,
                image="docker.io/library/app:latest",
                startup_mode="legacy",
                llm_enabled=False,
            )
            manifest.data.setdefault("options", {})["benchmark_workload"] = "tf-serving-half-plus-two"
            manifest.save()
            manifest.stage_finish("analyze", status="passed", summary="analyze ok")
            manifest.stage_begin("report", "Generate a unified run report from current stage artifacts")

            result = run_report_stage(work_dir=work_dir, manifest=ManifestStore.load(work_dir / "manifest.json"))
            payload = json.loads(result.report_json_path.read_text(encoding="utf-8"))

            self.assertIsNone(payload["workloads"]["benchmark_profile"])
            self.assertEqual([], payload["workloads"]["benchmark_effective_capabilities"])
            self.assertEqual({}, payload["workloads"]["benchmark_effective_hooks"])
            self.assertEqual({}, payload["benchmark"])

    def test_report_stage_loads_chunk_shadow_apply_result_when_default_apply_result_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            manifest = ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260325_000003",
                work_dir=work_dir,
                image="docker.io/library/app:latest",
                startup_mode="legacy",
                llm_enabled=False,
            )
            (work_dir / "apply_result_chunk_shadow.json").write_text(
                json.dumps(
                    {
                        "selector_mode": "chunk-shadow",
                        "src_image": "docker.io/library/app:latest",
                        "dst_image": "docker.io/library/app:latest-stargz-chunk-shadow",
                        "dry_run": False,
                        "optimized_image_written": True,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (work_dir / "optimized_image_chunk_shadow.txt").write_text(
                "docker.io/library/app:latest-stargz-chunk-shadow\n",
                encoding="utf-8",
            )
            manifest.stage_finish(
                "apply",
                status="passed",
                summary="apply chunk-shadow ok",
                artifacts={
                    "apply_result_chunk_shadow": str(work_dir / "apply_result_chunk_shadow.json"),
                    "optimized_image_chunk_shadow": str(work_dir / "optimized_image_chunk_shadow.txt"),
                },
            )
            manifest.stage_begin("report", "Generate a unified run report from current stage artifacts")

            result = run_report_stage(work_dir=work_dir, manifest=ManifestStore.load(work_dir / "manifest.json"))
            payload = json.loads(result.report_json_path.read_text(encoding="utf-8"))
            markdown = result.report_md_path.read_text(encoding="utf-8")

            self.assertEqual("chunk-shadow", payload["apply"]["selector_mode"])
            self.assertEqual(
                "docker.io/library/app:latest-stargz-chunk-shadow",
                payload["apply"]["dst_image"],
            )
            self.assertEqual(
                str(work_dir / "apply_result_chunk_shadow.json"),
                payload["artifacts"]["apply_result_chunk_shadow"],
            )
            self.assertIn("Selector mode", markdown)
            self.assertIn("chunk-shadow", markdown)

    def test_report_stage_treats_cancelled_stage_as_failed_overall_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            manifest = ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260317_000010",
                work_dir=work_dir,
                image="docker.io/library/app:latest",
                startup_mode="legacy",
                llm_enabled=False,
            )
            manifest.stage_finish(
                "benchmark",
                status="cancelled",
                summary="benchmark cancelled",
                error="interrupted by user",
            )
            manifest.stage_begin("report", "Generate a unified run report from current stage artifacts")

            result = run_report_stage(work_dir=work_dir, manifest=ManifestStore.load(work_dir / "manifest.json"))
            payload = json.loads(result.report_json_path.read_text(encoding="utf-8"))

            self.assertEqual("failed", payload["overall_status"])
            benchmark_stage = next(stage for stage in payload["stages"] if stage["name"] == "benchmark")
            self.assertEqual("cancelled", benchmark_stage["status"])
            self.assertEqual("interrupted by user", benchmark_stage["error"])

    def test_report_stage_surfaces_sample_failure_runtime_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            manifest = ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260323_030001",
                work_dir=work_dir,
                image="docker.io/library/distroless-app:latest",
                startup_mode="gated",
                llm_enabled=False,
            )
            (work_dir / "runtime_state.json").write_text(
                json.dumps(
                    {
                        "status": "failed",
                        "startup_mode_used": "gated",
                        "startup_backend": "gated_host_launcher_dynamic",
                        "startup_backend_preference": "auto",
                        "startup_backend_selection_reason": "image lacks /bin/sh; host launcher is the only gated startup backend",
                        "startup": {
                            "requested_mode": "gated",
                            "used_mode": "gated",
                            "backend": "gated_host_launcher_dynamic",
                            "backend_preference": "auto",
                            "backend_selection_reason": "image lacks /bin/sh; host launcher is the only gated startup backend",
                            "backend_family": "host_launcher",
                            "gated_requested": True,
                            "gated_active": True,
                            "experimental_backend": True,
                            "fallback_applied": False,
                            "fallback_reason": "",
                        },
                        "failure": {
                            "error": "sampler /metrics did not become ready on port 18115 (startup_mode=gated, startup_backend=gated_host_launcher_dynamic)",
                            "recent_container_logs": "exec /__cpp_sampler_gate_launcher: no such file or directory\nloader error",
                            "category": "startup_attach",
                            "kind": "host_launcher_missing_runtime_dependency",
                            "evidence": "container log reports missing host launcher binary or dynamic loader",
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            manifest.stage_finish(
                "sample",
                status="failed",
                summary="Container launch or sampling failed",
                artifacts={"runtime_state": str(work_dir / "runtime_state.json")},
                error="sampler /metrics did not become ready on port 18115",
            )
            manifest.stage_begin("report", "Generate a unified run report from current stage artifacts")

            result = run_report_stage(work_dir=work_dir, manifest=ManifestStore.load(work_dir / "manifest.json"))
            payload = json.loads(result.report_json_path.read_text(encoding="utf-8"))
            markdown = result.report_md_path.read_text(encoding="utf-8")

            self.assertEqual("failed", payload["overall_status"])
            self.assertEqual("failed", payload["sample"]["status"])
            self.assertEqual("gated_host_launcher_dynamic", payload["sample"]["startup_backend"])
            self.assertIn("sampler /metrics did not become ready", payload["sample"]["failure"]["error"])
            self.assertEqual("startup_attach", payload["sample"]["failure"]["category"])
            self.assertEqual("host_launcher_missing_runtime_dependency", payload["sample"]["failure"]["kind"])
            self.assertIn("## Sample", markdown)
            self.assertIn("Startup backend selection reason", markdown)
            self.assertIn("Failure category", markdown)
            self.assertIn("Failure kind", markdown)
            self.assertIn("Recent log head", markdown)

    def test_report_stage_marks_invalid_benchmark_as_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            manifest = ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260317_000003",
                work_dir=work_dir,
                image="docker.io/library/app:latest",
                startup_mode="legacy",
                llm_enabled=False,
            )
            (work_dir / "benchmark_result.json").write_text(
                json.dumps(
                    {
                        "base_image": "docker.io/library/app:latest",
                        "opt_image": "docker.io/library/app:latest-stargz",
                        "summary": {
                            "rows_total": 6,
                            "labels": {
                                "baseline": {"total_rows": 3, "ok_rows": 0, "fail_rows": 3, "median_ready_ms": None},
                                "optimized": {"total_rows": 3, "ok_rows": 0, "fail_rows": 3, "median_ready_ms": None},
                            },
                            "comparison": {
                                "median_ready_delta_ms": None,
                                "median_ready_improvement_pct": None,
                            },
                            "valid": False,
                            "invalid_reasons": [
                                "baseline produced 0 successful benchmark rows",
                                "optimized produced 0 successful benchmark rows",
                            ],
                            "warnings": [],
                        },
                        "valid": False,
                        "invalid_reasons": [
                            "baseline produced 0 successful benchmark rows",
                            "optimized produced 0 successful benchmark rows",
                        ],
                        "warnings": [],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            manifest.stage_finish(
                "benchmark",
                status="passed",
                summary="benchmark ok",
                artifacts={"benchmark_result": str(work_dir / "benchmark_result.json")},
            )
            manifest.stage_begin("report", "Generate a unified run report from current stage artifacts")

            result = run_report_stage(work_dir=work_dir, manifest=ManifestStore.load(work_dir / "manifest.json"))
            payload = json.loads(result.report_json_path.read_text(encoding="utf-8"))

            self.assertEqual("failed", payload["overall_status"])
            self.assertFalse(payload["benchmark"]["valid"])
            self.assertEqual("failed", payload["benchmark"]["status"])
            self.assertIn(
                "baseline produced 0 successful benchmark rows",
                payload["benchmark"]["invalid_reasons"],
            )

    def test_report_stage_preserves_strict_proof_wording_without_claiming_all_caches_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            manifest = ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260317_000004",
                work_dir=work_dir,
                image="127.0.0.1:5001/demo/app:base",
                startup_mode="legacy",
                llm_enabled=False,
            )
            (work_dir / "benchmark_result.json").write_text(
                json.dumps(
                    {
                        "status": "passed",
                        "cold_mode": "registry-cold-strict",
                        "base_image": "127.0.0.1:5001/demo/app:base",
                        "opt_image": "127.0.0.1:5001/demo/app:opt",
                        "summary": {
                            "rows_total": 2,
                            "labels": {
                                "baseline": {"median_ready_ms": 120.0},
                                "optimized": {"median_ready_ms": 90.0},
                            },
                            "comparison": {
                                "median_ready_delta_ms": -30.0,
                                "median_ready_improvement_pct": 25.0,
                            },
                            "valid": True,
                            "invalid_reasons": [],
                            "warnings": [],
                        },
                        "valid": True,
                        "invalid_reasons": [],
                        "strict_proof": {
                            "strict_proof_zero": True,
                            "required_zero": {
                                "local_image_refs_remaining_base": 0,
                                "local_image_refs_remaining_opt": 0,
                                "stargz_snapshot_records_remaining": 0,
                                "stargz_runtime_mounts_remaining": 0,
                                "stargz_local_snapshot_entries_remaining": 0,
                                "stargz_cache_entries_remaining": 0,
                                "content_store_digests_remaining_base": 0,
                                "content_store_digests_remaining_opt": 0,
                            },
                            "best_effort": {
                                "page_cache_drop_attempted": True,
                                "page_cache_drop_succeeded": True,
                                "page_cache_proof_mode": "required-best-effort",
                            },
                            "unverifiable": [],
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            manifest.stage_finish(
                "benchmark",
                status="passed",
                summary="benchmark ok",
                artifacts={"benchmark_result": str(work_dir / "benchmark_result.json")},
            )
            manifest.stage_begin("report", "Generate a unified run report from current stage artifacts")

            result = run_report_stage(work_dir=work_dir, manifest=ManifestStore.load(work_dir / "manifest.json"))
            payload = json.loads(result.report_json_path.read_text(encoding="utf-8"))
            markdown = result.report_md_path.read_text(encoding="utf-8")

            self.assertEqual("registry-cold-strict", payload["benchmark"]["cold_mode"])
            self.assertTrue(payload["benchmark"]["strict_proof"]["strict_proof_zero"])
            self.assertEqual(
                "required-best-effort",
                payload["benchmark"]["strict_proof"]["best_effort"]["page_cache_proof_mode"],
            )
            self.assertIn("required-best-effort", markdown)
            self.assertIn("verifiable hard proofs", markdown)
            self.assertNotIn("all caches were proven zero", markdown)

    def test_report_stage_expresses_manual_pause_resume_contract_and_compat_reads_old_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            manifest = ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260327_000001",
                work_dir=work_dir,
                image="docker.io/library/app:latest",
                startup_mode="legacy",
                llm_enabled=False,
            )
            resume_script = work_dir / "resume_manual_web_llm.sh"
            resume_script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            manual_response_path = work_dir / "hints_raw.manual.yaml"
            (work_dir / "llm_result.json").write_text(
                json.dumps(
                    {
                        "requested_mode": "llm-manual",
                        "mode": "manual_export",
                        "mode_result": "llm-manual-paused",
                        "llm_applied": False,
                        "final_hints_source": "deterministic_ranked",
                        "resume_required": True,
                        "pause_reason": "awaiting_manual_llm_response",
                        "manual_response_default_path": str(manual_response_path),
                        "manual_response_format": "json",
                        "resume_command": (
                            "python3 scripts/oneclick_product.py llm-optimize "
                            f"--work-dir {work_dir} --manual-response-file {manual_response_path}"
                        ),
                        "warnings": [],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            manifest.data.setdefault("artifacts", {})["llm_result"] = str(work_dir / "llm_result.json")
            manifest.data["artifacts"]["resume_manual_web_script"] = str(resume_script)
            manifest.data.setdefault("stages", {})["llm_optimize"] = {
                "status": "awaiting_manual_response",
                "summary": "Prompt package exported for manual web-LLM input",
                "artifacts": {
                    "llm_result": str(work_dir / "llm_result.json"),
                    "resume_manual_web_script": str(resume_script),
                },
            }
            manifest.data["status"] = "llm_optimize_awaiting_manual_response"
            manifest.save()

            result = run_report_stage(work_dir=work_dir, manifest=ManifestStore.load(work_dir / "manifest.json"))
            payload = json.loads(result.report_json_path.read_text(encoding="utf-8"))
            markdown = result.report_md_path.read_text(encoding="utf-8")

            self.assertEqual("paused", payload["overall_status"])
            self.assertEqual("llm-manual", payload["mode"])
            self.assertEqual("llm-manual-paused", payload["mode_result"])
            self.assertTrue(payload["resume_required"])
            self.assertEqual("awaiting_manual_llm_response", payload["pause_reason"])
            self.assertEqual(
                str(manual_response_path),
                payload["resume_inputs"]["manual_response_file"]["path"],
            )
            self.assertEqual("json", payload["resume_inputs"]["manual_response_file"]["format"])
            self.assertIn("resume_manual_web_llm.sh", payload["resume_command"])
            self.assertEqual(payload["resume_command"], payload["next_step"])
            llm_stage = next(stage for stage in payload["stages"] if stage["name"] == "llm_optimize")
            self.assertEqual("paused", llm_stage["status"])
            self.assertEqual("awaiting_manual_llm_response", llm_stage["pause_reason"])
            self.assertIn("## Mode", markdown)
            self.assertIn("Resume command", markdown)

    def test_report_stage_surfaces_llm_auto_fallback_reason_from_llm_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            manifest = ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260328_000001",
                work_dir=work_dir,
                image="docker.io/library/app:latest",
                startup_mode="legacy",
                llm_enabled=True,
            )
            llm_result_path = work_dir / "llm_result.json"
            llm_result_path.write_text(
                json.dumps(
                    {
                        "requested_mode": "llm-auto",
                        "mode": "api_fallback",
                        "mode_result": "llm-auto-fallback",
                        "llm_applied": False,
                        "final_hints_source": "deterministic_ranked",
                        "fallback_kind": "external_dependency",
                        "fallback_reason": "schema validation failed: unsupported top-level keys: answer",
                        "warnings": [
                            "schema validation failed: unsupported top-level keys: answer; kept deterministic hints"
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            manifest.data.setdefault("artifacts", {})["llm_result"] = str(llm_result_path)
            manifest.data.setdefault("stages", {})["llm_optimize"] = {
                "status": "passed",
                "summary": "LLM optimization fell back to deterministic hints",
                "artifacts": {"llm_result": str(llm_result_path)},
                "warnings": [
                    "schema validation failed: unsupported top-level keys: answer; kept deterministic hints"
                ],
            }
            manifest.save()

            result = run_report_stage(work_dir=work_dir, manifest=ManifestStore.load(work_dir / "manifest.json"))
            payload = json.loads(result.report_json_path.read_text(encoding="utf-8"))
            markdown = result.report_md_path.read_text(encoding="utf-8")

            self.assertEqual("llm-auto", payload["mode"])
            self.assertEqual("llm-auto-fallback", payload["mode_result"])
            self.assertFalse(payload["llm"]["llm_applied"])
            self.assertEqual("deterministic_ranked", payload["llm"]["final_hints_source"])
            self.assertEqual("schema validation failed: unsupported top-level keys: answer", payload["llm"]["fallback_reason"])
            self.assertIn("## LLM", markdown)
            self.assertIn("Fallback reason", markdown)


if __name__ == "__main__":
    unittest.main()
