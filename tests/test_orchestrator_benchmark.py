import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.container_runtime import container_runtime_spec
from orchestrator.stages.benchmark import (
    parse_strict_registry_ref,
    run_benchmark_stage,
    validate_registry_cold_strict_pair,
)
from orchestrator.workloads.hooks import ExecReadyHook, LogRegexReadyHook, ModelMountHook


ROOT = Path(__file__).resolve().parents[1]


class StrictRegistryRefTests(unittest.TestCase):
    def test_parse_strict_registry_ref_accepts_explicit_registry_authorities(self):
        localhost_ref = parse_strict_registry_ref("localhost/demo/app:latest")
        self.assertEqual("localhost", localhost_ref.registry)
        self.assertEqual("demo/app", localhost_ref.repository)
        self.assertEqual("latest", localhost_ref.tag)
        self.assertIsNone(localhost_ref.digest)

        localhost_port_ref = parse_strict_registry_ref("localhost:5001/demo/app:opt")
        self.assertEqual("localhost:5001", localhost_port_ref.registry)
        self.assertEqual("demo/app", localhost_port_ref.repository)
        self.assertEqual("opt", localhost_port_ref.tag)

        docker_io_ref = parse_strict_registry_ref("docker.io/library/app@sha256:" + ("a" * 64))
        self.assertEqual("docker.io", docker_io_ref.registry)
        self.assertEqual("library/app", docker_io_ref.repository)
        self.assertIsNone(docker_io_ref.tag)
        self.assertEqual("sha256:" + ("a" * 64), docker_io_ref.digest)

    def test_parse_strict_registry_ref_rejects_bare_local_tag(self):
        with self.assertRaisesRegex(ValueError, "explicit registry"):
            parse_strict_registry_ref("app:latest")
        with self.assertRaisesRegex(ValueError, "explicit registry"):
            parse_strict_registry_ref("library/app:latest")

    def test_validate_registry_cold_strict_pair_requires_same_registry_repository(self):
        with self.assertRaisesRegex(ValueError, "same registry/repository"):
            validate_registry_cold_strict_pair(
                base_image="docker.io/library/app:base",
                opt_image="127.0.0.1:5001/library/app:opt",
            )
        with self.assertRaisesRegex(ValueError, "same registry/repository"):
            validate_registry_cold_strict_pair(
                base_image="docker.io/library/app:base",
                opt_image="docker.io/library/other:opt",
            )


def build_benchmark_env_payload(*, image_local_status: str = "pass"):
    checks = {
        "os_linux": "pass",
        "repo_layout": "pass",
        "privileged_user": "pass",
        "cmd_nerdctl": "pass",
        "cmd_ctr": "pass",
        "cmd_curl": "pass",
        "cmd_awk": "pass",
        "cmd_ss": "pass",
        "cmd_ctr-remote": "pass",
        "cmd_jq_benchmark": "pass",
        "nerdctl_runtime": "pass",
        "ctr_remote_runtime": "pass",
        "snapshotter_plugin": "pass",
        "cni_bridge_plugin": "pass",
        "image_local": image_local_status,
    }
    return {
        "schema_version": 1,
        "requested_profiles": ["benchmark"],
        "profiles": {"benchmark": {"status": "pass", "required_failures": [], "warnings": []}},
        "checks": [
            {
                "id": check_id,
                "status": status,
                "summary": check_id,
                "required_for": ["benchmark"],
                "advisory_for": [],
                "detail": "",
                "value": None,
            }
            for check_id, status in checks.items()
        ],
    }


class BenchmarkStageTests(unittest.TestCase):
    def test_registry_cold_strict_requires_ready_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            with self.assertRaisesRegex(ValueError, "mode=ready"):
                run_benchmark_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    base_image="docker.io/library/app:base",
                    opt_image="docker.io/library/app:opt",
                    cold_mode="registry-cold-strict",
                    mode="struct",
                )

    def test_registry_cold_strict_rejects_bare_local_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            with self.assertRaisesRegex(ValueError, "explicit registry"):
                run_benchmark_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    base_image="app:base",
                    opt_image="app:opt",
                    cold_mode="registry-cold-strict",
                    mode="ready",
                    ready_url="http://127.0.0.1:9000/ready",
                )

    def test_registry_cold_strict_resolves_platform_closures_and_records_auditable_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            env_payload = build_benchmark_env_payload(image_local_status="warn")
            root_index_digest = "sha256:" + ("1" * 64)
            base_manifest_digest = "sha256:" + ("2" * 64)
            opt_manifest_digest = "sha256:" + ("3" * 64)
            base_config_digest = "sha256:" + ("4" * 64)
            opt_config_digest = "sha256:" + ("5" * 64)
            shared_layer_digest = "sha256:" + ("6" * 64)
            base_only_layer_digest = "sha256:" + ("7" * 64)
            opt_only_layer_digest = "sha256:" + ("8" * 64)

            def _manifest_payload(root_digest: str, child_digest: str) -> str:
                return json.dumps(
                    {
                        "Descriptor": {"digest": root_digest},
                        "OCIManifest": {
                            "schemaVersion": 2,
                            "mediaType": "application/vnd.oci.image.index.v1+json",
                            "manifests": [
                                {
                                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                                    "digest": child_digest,
                                    "platform": {"os": "linux", "architecture": "amd64"},
                                },
                                {
                                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                                    "digest": "sha256:" + ("9" * 64),
                                    "platform": {"os": "linux", "architecture": "arm64"},
                                },
                            ],
                        },
                    }
                )

            def _child_payload(manifest_digest: str, config_digest: str, layer_digests: list[str]) -> str:
                return json.dumps(
                    {
                        "Descriptor": {"digest": manifest_digest},
                        "OCIManifest": {
                            "schemaVersion": 2,
                            "mediaType": "application/vnd.oci.image.manifest.v1+json",
                            "config": {
                                "mediaType": "application/vnd.oci.image.config.v1+json",
                                "digest": config_digest,
                                "size": 123,
                            },
                            "layers": [
                                {
                                    "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                                    "digest": digest,
                                    "size": 456,
                                }
                                for digest in layer_digests
                            ],
                        },
                    }
                )

            def fake_run(argv, **kwargs):
                if argv[0] == "nerdctl" and argv[1:3] == ["manifest", "inspect"]:
                    ref = argv[3]
                    if ref == "127.0.0.1:5001/demo/app:base":
                        return subprocess.CompletedProcess(argv, 0, _manifest_payload(root_index_digest, base_manifest_digest), "")
                    if ref == "127.0.0.1:5001/demo/app:opt":
                        return subprocess.CompletedProcess(argv, 0, _manifest_payload(root_index_digest, opt_manifest_digest), "")
                    if ref == f"127.0.0.1:5001/demo/app@{base_manifest_digest}":
                        return subprocess.CompletedProcess(
                            argv,
                            0,
                            _child_payload(base_manifest_digest, base_config_digest, [shared_layer_digest, base_only_layer_digest]),
                            "",
                        )
                    if ref == f"127.0.0.1:5001/demo/app@{opt_manifest_digest}":
                        return subprocess.CompletedProcess(
                            argv,
                            0,
                            _child_payload(opt_manifest_digest, opt_config_digest, [shared_layer_digest, opt_only_layer_digest]),
                            "",
                        )
                    raise AssertionError(f"unexpected manifest ref: {ref}")
                if argv[1].endswith("check_env.sh"):
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(env_payload, ensure_ascii=False, indent=2) + "\n",
                        "",
                    )
                if argv[1].endswith("coldstart_bench_strict.sh"):
                    self.assertIn("--cold-mode", argv)
                    self.assertIn("registry-cold-strict", argv)
                    self.assertEqual("stargz", argv[argv.index("--base-snap") + 1])
                    self.assertEqual("stargz", argv[argv.index("--opt-snap") + 1])
                    self.assertIn("--pull-policy", argv)
                    self.assertIn("always", argv)
                    self.assertIn("--drop-local-image-ref", argv)
                    self.assertIn("--strict-input-json", argv)
                    self.assertIn("--proof-json", argv)
                    strict_input_path = Path(argv[argv.index("--strict-input-json") + 1])
                    strict_input_payload = json.loads(strict_input_path.read_text(encoding="utf-8"))
                    self.assertEqual(
                        f"127.0.0.1:5001/demo/app@{base_manifest_digest}",
                        strict_input_payload["base"]["resolved_ref"],
                    )
                    self.assertEqual(
                        f"127.0.0.1:5001/demo/app@{opt_manifest_digest}",
                        strict_input_payload["optimized"]["resolved_ref"],
                    )
                    self.assertIn(root_index_digest, strict_input_payload["base"]["closure_digests"])
                    self.assertIn(shared_layer_digest, strict_input_payload["optimized"]["closure_digests"])
                    proof_path = Path(argv[argv.index("--proof-json") + 1])
                    proof_path.write_text(
                        json.dumps(
                            {
                                "strict_proof_zero": True,
                                "required_zero": {
                                    "local_image_refs_remaining_base": 0,
                                    "local_image_refs_remaining_opt": 0,
                                    "stargz_runtime_mounts_remaining": 0,
                                    "stargz_cache_entries_remaining": 0,
                                    "content_store_digests_remaining_base": 0,
                                    "content_store_digests_remaining_opt": 0,
                                },
                                "required_true": {
                                    "base_local_ref_deleted": True,
                                    "opt_local_ref_deleted": True,
                                },
                                "diagnostic": {
                                    "global_stargz_snapshot_records_remaining": 273,
                                    "global_stargz_local_snapshot_entries_remaining": 273,
                                },
                                "best_effort": {
                                    "page_cache_drop_attempted": True,
                                    "page_cache_drop_succeeded": True,
                                    "page_cache_proof_mode": "required-best-effort",
                                },
                                "unverifiable": [],
                            }
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    csv_path = Path(argv[argv.index("--csv") + 1])
                    log_dir = Path(argv[argv.index("--log-dir") + 1])
                    log_dir.mkdir(parents=True, exist_ok=True)
                    csv_path.write_text(
                        "ts,label,snapshotter,mode,ready_ms,predict_extra_ms,proof_exists,proof_done,status,err\n"
                        "2026-03-17T00:00:00+00:00,baseline,stargz,ready,120,-1,0,0,ok,\n"
                        "2026-03-17T00:00:01+00:00,optimized,stargz,ready,90,-1,0,0,ok,\n",
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(argv, 0, "", "")
                raise AssertionError(f"unexpected argv: {argv}")

            with mock.patch("orchestrator.stages.benchmark.subprocess.run", side_effect=fake_run):
                result = run_benchmark_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    base_image="127.0.0.1:5001/demo/app:base",
                    opt_image="127.0.0.1:5001/demo/app:opt",
                    runs=1,
                    mode="ready",
                    ready_url="http://127.0.0.1:9000/ready",
                    cold_mode="registry-cold-strict",
                    pull_policy="always",
                    drop_local_image_ref=True,
                )

            payload = json.loads(result.result_path.read_text(encoding="utf-8"))
            self.assertEqual("registry-cold-strict", payload["cold_mode"])
            self.assertEqual("stargz", payload["base_snapshotter"])
            self.assertEqual("stargz", payload["opt_snapshotter"])
            self.assertEqual("always", payload["pull_policy"])
            self.assertTrue(payload["drop_local_image_ref"])
            self.assertTrue(payload["strict_proof"]["strict_proof_zero"])
            self.assertEqual(
                f"127.0.0.1:5001/demo/app@{base_manifest_digest}",
                payload["strict_registry"]["base"]["resolved_ref"],
            )
            self.assertIn(shared_layer_digest, payload["strict_registry"]["shared_closure_digests"])

    def test_benchmark_stage_defaults_baseline_snapshotter_to_overlayfs_for_stargz_benchmarks(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            env_payload = build_benchmark_env_payload()
            env_check_snapshotters: list[str] = []

            def fake_run(argv, **kwargs):
                if argv[1].endswith("check_env.sh"):
                    env_check_snapshotters.append(argv[argv.index("--snapshotter") + 1])
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(env_payload, ensure_ascii=False, indent=2) + "\n",
                        "",
                    )
                if argv[1].endswith("coldstart_bench_strict.sh"):
                    self.assertEqual("stargz", argv[argv.index("--snap") + 1])
                    self.assertEqual("overlayfs", argv[argv.index("--base-snap") + 1])
                    self.assertEqual("stargz", argv[argv.index("--opt-snap") + 1])
                    self.assertNotIn("--ready-http-status-ok-is-ready", argv)
                    csv_path = Path(argv[argv.index("--csv") + 1])
                    log_dir = Path(argv[argv.index("--log-dir") + 1])
                    log_dir.mkdir(parents=True, exist_ok=True)
                    csv_path.write_text(
                        "ts,label,snapshotter,mode,ready_ms,predict_extra_ms,proof_exists,proof_done,status,err\n"
                        "2026-03-17T00:00:00+00:00,baseline,overlayfs,struct,120,-1,0,0,ok,\n"
                        "2026-03-17T00:00:01+00:00,optimized,stargz,struct,90,-1,0,0,ok,\n",
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(argv, 0, "", "")
                raise AssertionError(f"unexpected argv: {argv}")

            with mock.patch("orchestrator.stages.benchmark.subprocess.run", side_effect=fake_run):
                result = run_benchmark_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    base_image="docker.io/library/app:base",
                    opt_image="docker.io/library/app:opt",
                    runs=1,
                    mode="struct",
                )

            payload = json.loads(result.result_path.read_text(encoding="utf-8"))
            self.assertEqual(["overlayfs", "stargz"], env_check_snapshotters)
            self.assertEqual("stargz", payload["snapshotter"])
            self.assertEqual("overlayfs", payload["base_snapshotter"])
            self.assertEqual("stargz", payload["opt_snapshotter"])
            self.assertTrue(payload["valid"])
            self.assertEqual("passed", payload["status"])

    def test_benchmark_stage_allows_explicit_baseline_stargz_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            env_payload = build_benchmark_env_payload()
            env_check_snapshotters: list[str] = []

            def fake_run(argv, **kwargs):
                if argv[1].endswith("check_env.sh"):
                    env_check_snapshotters.append(argv[argv.index("--snapshotter") + 1])
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(env_payload, ensure_ascii=False, indent=2) + "\n",
                        "",
                    )
                if argv[1].endswith("coldstart_bench_strict.sh"):
                    self.assertEqual("stargz", argv[argv.index("--snap") + 1])
                    self.assertEqual("stargz", argv[argv.index("--base-snap") + 1])
                    self.assertEqual("stargz", argv[argv.index("--opt-snap") + 1])
                    self.assertIn("--ready-http-status-ok-is-ready", argv)
                    csv_path = Path(argv[argv.index("--csv") + 1])
                    log_dir = Path(argv[argv.index("--log-dir") + 1])
                    log_dir.mkdir(parents=True, exist_ok=True)
                    csv_path.write_text(
                        "ts,label,snapshotter,mode,ready_ms,predict_extra_ms,proof_exists,proof_done,status,err\n"
                        "2026-03-17T00:00:00+00:00,baseline,stargz,ready,120,-1,0,0,ok,\n"
                        "2026-03-17T00:00:01+00:00,optimized,stargz,ready,90,-1,0,0,ok,\n",
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(argv, 0, "", "")
                raise AssertionError(f"unexpected argv: {argv}")

            with mock.patch("orchestrator.stages.benchmark.subprocess.run", side_effect=fake_run):
                result = run_benchmark_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    base_image="127.0.0.1:5001/abtest/tfpy-half-plus-two-io1g:noopt",
                    opt_image="127.0.0.1:5001/abtest/tfpy-half-plus-two-io1g:opt",
                    runs=1,
                    mode="ready",
                    ready_url="http://127.0.0.1:8000/ready",
                    snapshotter="stargz",
                    baseline_stargz=True,
                    self_starting_service=True,
                )

            payload = json.loads(result.result_path.read_text(encoding="utf-8"))
            self.assertEqual(["stargz", "stargz"], env_check_snapshotters)
            self.assertEqual("stargz", payload["snapshotter"])
            self.assertEqual("stargz", payload["base_snapshotter"])
            self.assertEqual("stargz", payload["opt_snapshotter"])
            self.assertTrue(payload["baseline_stargz"])
            self.assertTrue(payload["self_starting_service"])
            self.assertTrue(payload["valid"])

    def test_benchmark_stage_forwards_raw_vs_estargz_comparison_mode_and_end_to_end_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            env_payload = build_benchmark_env_payload(image_local_status="warn")

            def fake_run(argv, **kwargs):
                if argv[1].endswith("check_env.sh"):
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(env_payload, ensure_ascii=False, indent=2) + "\n",
                        "",
                    )
                if argv[1].endswith("coldstart_bench_strict.sh"):
                    self.assertIn("--comparison-mode", argv)
                    self.assertIn("raw-vs-estargz", argv)
                    self.assertEqual("overlayfs", argv[argv.index("--base-snap") + 1])
                    self.assertEqual("stargz", argv[argv.index("--opt-snap") + 1])
                    csv_path = Path(argv[argv.index("--csv") + 1])
                    log_dir = Path(argv[argv.index("--log-dir") + 1])
                    log_dir.mkdir(parents=True, exist_ok=True)
                    csv_path.write_text(
                        "ts,label,snapshotter,mode,pull_ms,run_ms,ready_wait_ms,end_to_end_ready_ms,ready_ms,predict_extra_ms,service_bootstrap_ms,hot_files_loaded,hot_bytes_loaded,loaded_paths_count,proof_exists,proof_done,status,err\n"
                        "2026-03-17T00:00:00+00:00,baseline,overlayfs,ready,800,50,120,970,970,-1,780,128,268435456,128,0,0,ok,\n"
                        "2026-03-17T00:00:01+00:00,optimized,stargz,ready,250,40,90,380,380,-1,520,128,268435456,128,0,0,ok,\n",
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(argv, 0, "", "")
                raise AssertionError(f"unexpected argv: {argv}")

            with mock.patch("orchestrator.stages.benchmark.subprocess.run", side_effect=fake_run):
                result = run_benchmark_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    base_image="127.0.0.1:5001/abtest/ready-serving-layout-trap-v4:src-raw",
                    opt_image="127.0.0.1:5001/abtest/ready-serving-layout-trap-v4:esgz-hinted-v2",
                    runs=1,
                    mode="ready",
                    ready_url="http://127.0.0.1:8000/ready",
                    comparison_mode="raw-vs-estargz",
                    pull_policy="always",
                    drop_local_image_ref=True,
                )

            payload = json.loads(result.result_path.read_text(encoding="utf-8"))
            self.assertEqual("raw-vs-estargz", payload["comparison_mode"])
            self.assertEqual("end_to_end_ready_ms", payload["ready_ms_semantics"])
            self.assertEqual("overlayfs", payload["base_snapshotter"])
            self.assertEqual("stargz", payload["opt_snapshotter"])
            self.assertEqual(970.0, payload["baseline_median_ready_ms"])
            self.assertEqual(170.0, payload["baseline_median_post_pull_ready_ms"])
            self.assertEqual(130.0, payload["optimized_median_post_pull_ready_ms"])
            self.assertEqual(970.0, payload["baseline_median_end_to_end_ready_ms"])
            self.assertEqual(380.0, payload["optimized_median_end_to_end_ready_ms"])
            self.assertAlmostEqual((970.0 - 380.0) * 100.0 / 970.0, payload["end_to_end_improvement_pct"], places=6)
            self.assertAlmostEqual((170.0 - 130.0) * 100.0 / 170.0, payload["post_pull_ready_improvement_pct"], places=6)
            self.assertEqual(800.0, payload["summary"]["labels"]["baseline"]["median_pull_ms"])
            self.assertEqual(40.0, payload["summary"]["labels"]["optimized"]["median_run_ms"])
            self.assertEqual(90.0, payload["summary"]["labels"]["optimized"]["median_ready_wait_ms"])
            self.assertEqual(170.0, payload["summary"]["labels"]["baseline"]["median_post_pull_ready_ms"])
            self.assertEqual(130.0, payload["summary"]["labels"]["optimized"]["median_post_pull_ready_ms"])
            self.assertEqual(-550.0, payload["summary"]["comparison"]["median_pull_delta_ms"])
            self.assertEqual(-40.0, payload["summary"]["comparison"]["median_post_pull_ready_delta_ms"])
            self.assertAlmostEqual(
                (170.0 - 130.0) * 100.0 / 170.0,
                payload["summary"]["comparison"]["median_post_pull_ready_improvement_pct"],
                places=6,
            )
            self.assertEqual(-590.0, payload["summary"]["comparison"]["median_end_to_end_delta_ms"])
            self.assertEqual(170, payload["rows"][0]["post_pull_ready_ms"])
            self.assertEqual(130, payload["rows"][1]["post_pull_ready_ms"])

    def test_benchmark_stage_lazy_mode_skips_optimized_image_runtime_env_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            env_payload = build_benchmark_env_payload(image_local_status="warn")
            check_env_calls: list[list[str]] = []

            def fake_run(argv, **kwargs):
                if argv[1].endswith("check_env.sh"):
                    check_env_calls.append(list(argv))
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(env_payload, ensure_ascii=False, indent=2) + "\n",
                        "",
                    )
                if argv[1].endswith("coldstart_bench_strict.sh"):
                    self.assertEqual(2, len(check_env_calls))
                    base_call, opt_call = check_env_calls
                    self.assertIn("--image", base_call)
                    self.assertIn("127.0.0.1:5001/demo/app:raw", base_call)
                    self.assertIn("--snapshotter", opt_call)
                    self.assertIn("stargz", opt_call)
                    self.assertNotIn("--image", opt_call)
                    self.assertNotIn("127.0.0.1:5001/demo/app:esgz", opt_call)
                    csv_path = Path(argv[argv.index("--csv") + 1])
                    log_dir = Path(argv[argv.index("--log-dir") + 1])
                    log_dir.mkdir(parents=True, exist_ok=True)
                    csv_path.write_text(
                        "ts,label,snapshotter,mode,pull_ms,run_ms,ready_wait_ms,end_to_end_ready_ms,ready_ms,predict_extra_ms,service_bootstrap_ms,hot_files_loaded,hot_bytes_loaded,loaded_paths_count,proof_exists,proof_done,lazy_prepare_ms,lazy_ready_ms,ready_ms_semantics,stargz_cache_bytes,stargz_cache_bytes_before,content_large_blob_present_before,content_large_blob_present_after,content_store_present_bytes_before,content_store_present_bytes_after,explicit_pull_executed,full_pull_evidence,lazy_guard_failed,lazy_full_pull_warning,status,err\n"
                        "2026-03-17T00:00:00+00:00,baseline,overlayfs,ready,800,50,120,970,970,-1,780,128,268435456,128,0,0,-1,-1,end_to_end_ready_ms,-1,-1,-1,-1,-1,-1,1,0,0,0,ok,\n"
                        "2026-03-17T00:00:01+00:00,optimized,stargz,ready,0,40,90,150,150,-1,520,128,268435456,128,0,0,20,150,lazy_ready_ms,4096,0,0,0,0,0,0,0,0,0,ok,\n",
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(argv, 0, "", "")
                raise AssertionError(f"unexpected argv: {argv}")

            with mock.patch("orchestrator.stages.benchmark.subprocess.run", side_effect=fake_run):
                result = run_benchmark_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    base_image="127.0.0.1:5001/demo/app:raw",
                    opt_image="127.0.0.1:5001/demo/app:esgz",
                    runs=1,
                    mode="ready",
                    ready_url="http://127.0.0.1:8000/ready",
                    comparison_mode="raw-full-vs-estargz-lazy",
                    pull_policy="always",
                    drop_local_image_ref=True,
                )

            payload = json.loads(result.result_path.read_text(encoding="utf-8"))
            self.assertTrue(payload["valid"])

    def test_benchmark_stage_supports_raw_full_vs_estargz_lazy_summary_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            env_payload = build_benchmark_env_payload(image_local_status="warn")

            def fake_run(argv, **kwargs):
                if argv[1].endswith("check_env.sh"):
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(env_payload, ensure_ascii=False, indent=2) + "\n",
                        "",
                    )
                if argv[1].endswith("coldstart_bench_strict.sh"):
                    self.assertIn("--comparison-mode", argv)
                    self.assertIn("raw-full-vs-estargz-lazy", argv)
                    self.assertEqual("overlayfs", argv[argv.index("--base-snap") + 1])
                    self.assertEqual("stargz", argv[argv.index("--opt-snap") + 1])
                    csv_path = Path(argv[argv.index("--csv") + 1])
                    log_dir = Path(argv[argv.index("--log-dir") + 1])
                    log_dir.mkdir(parents=True, exist_ok=True)
                    csv_path.write_text(
                        "ts,label,snapshotter,mode,pull_ms,run_ms,ready_wait_ms,end_to_end_ready_ms,ready_ms,predict_extra_ms,service_bootstrap_ms,hot_files_loaded,hot_bytes_loaded,loaded_paths_count,proof_exists,proof_done,lazy_prepare_ms,lazy_ready_ms,ready_ms_semantics,stargz_cache_bytes,stargz_cache_bytes_before,content_large_blob_present_before,content_large_blob_present_after,content_store_present_bytes_before,content_store_present_bytes_after,explicit_pull_executed,full_pull_evidence,lazy_guard_failed,lazy_full_pull_warning,status,err\n"
                        "2026-03-17T00:00:00+00:00,baseline,overlayfs,ready,800,50,120,970,970,-1,780,128,268435456,128,0,0,-1,-1,end_to_end_ready_ms,-1,-1,-1,-1,-1,-1,1,0,0,0,ok,\n"
                        "2026-03-17T00:00:01+00:00,optimized,stargz,ready,0,40,90,150,150,-1,520,128,268435456,128,0,0,20,150,lazy_ready_ms,4096,0,0,0,0,0,0,0,0,0,ok,\n",
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(argv, 0, "", "")
                raise AssertionError(f"unexpected argv: {argv}")

            with mock.patch("orchestrator.stages.benchmark.subprocess.run", side_effect=fake_run):
                result = run_benchmark_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    base_image="127.0.0.1:5001/abtest/ready-serving-layout-trap-v5:src-raw",
                    opt_image="127.0.0.1:5001/abtest/ready-serving-layout-trap-v5:esgz-hinted-v1",
                    runs=1,
                    mode="ready",
                    ready_url="http://127.0.0.1:8000/ready",
                    comparison_mode="raw-full-vs-estargz-lazy",
                    pull_policy="always",
                    drop_local_image_ref=True,
                )

            payload = json.loads(result.result_path.read_text(encoding="utf-8"))
            self.assertTrue(payload["lazy_mode_enabled"])
            self.assertEqual("raw-full-vs-estargz-lazy", payload["comparison_mode"])
            self.assertEqual(
                "baseline_full_pull_ready_ms_vs_optimized_lazy_ready_ms",
                payload["ready_ms_semantics"],
            )
            self.assertEqual(800.0, payload["baseline_full_pull_ms"])
            self.assertEqual(20.0, payload["optimized_lazy_prepare_ms"])
            self.assertEqual(150.0, payload["optimized_lazy_ready_ms"])
            self.assertEqual(970.0, payload["baseline_median_full_pull_ready_ms"])
            self.assertEqual(150.0, payload["optimized_median_lazy_ready_ms"])
            self.assertAlmostEqual((970.0 - 150.0) * 100.0 / 970.0, payload["lazy_ready_improvement_pct"], places=6)
            self.assertEqual(20.0, payload["summary"]["labels"]["optimized"]["median_lazy_prepare_ms"])
            self.assertEqual(150.0, payload["summary"]["labels"]["optimized"]["median_lazy_ready_ms"])
            self.assertAlmostEqual(
                (970.0 - 150.0) * 100.0 / 970.0,
                payload["summary"]["comparison"]["median_lazy_ready_improvement_pct"],
                places=6,
            )
            self.assertEqual(150, payload["rows"][1]["lazy_ready_ms"])

    def test_benchmark_stage_fails_when_lazy_path_appears_to_full_pull(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            env_payload = build_benchmark_env_payload(image_local_status="warn")

            def fake_run(argv, **kwargs):
                if argv[1].endswith("check_env.sh"):
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(env_payload, ensure_ascii=False, indent=2) + "\n",
                        "",
                    )
                if argv[1].endswith("coldstart_bench_strict.sh"):
                    csv_path = Path(argv[argv.index("--csv") + 1])
                    log_dir = Path(argv[argv.index("--log-dir") + 1])
                    log_dir.mkdir(parents=True, exist_ok=True)
                    csv_path.write_text(
                        "ts,label,snapshotter,mode,pull_ms,run_ms,ready_wait_ms,end_to_end_ready_ms,ready_ms,predict_extra_ms,service_bootstrap_ms,hot_files_loaded,hot_bytes_loaded,loaded_paths_count,proof_exists,proof_done,lazy_prepare_ms,lazy_ready_ms,ready_ms_semantics,stargz_cache_bytes,stargz_cache_bytes_before,content_large_blob_present_before,content_large_blob_present_after,content_store_present_bytes_before,content_store_present_bytes_after,explicit_pull_executed,full_pull_evidence,lazy_guard_failed,lazy_full_pull_warning,status,err\n"
                        "2026-03-17T00:00:00+00:00,baseline,overlayfs,ready,800,50,120,970,970,-1,780,128,268435456,128,0,0,-1,-1,end_to_end_ready_ms,-1,-1,-1,-1,-1,-1,1,0,0,0,ok,\n"
                        "2026-03-17T00:00:01+00:00,optimized,stargz,ready,0,40050,90,40140,40140,-1,520,128,268435456,128,0,0,20,40140,lazy_ready_ms,8589934592,0,0,1,0,8589934592,0,1,1,1,fail,optimized-lazy-path-performed-full-pull\n",
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(argv, 0, "", "")
                raise AssertionError(f"unexpected argv: {argv}")

            with mock.patch("orchestrator.stages.benchmark.subprocess.run", side_effect=fake_run):
                result = run_benchmark_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    base_image="127.0.0.1:5001/abtest/ready-serving-layout-trap-v5:src-raw",
                    opt_image="127.0.0.1:5001/abtest/ready-serving-layout-trap-v5:esgz-hinted-v1",
                    runs=1,
                    mode="ready",
                    ready_url="http://127.0.0.1:8000/ready",
                    comparison_mode="raw-full-vs-estargz-lazy",
                    pull_policy="always",
                    drop_local_image_ref=True,
                )

            payload = json.loads(result.result_path.read_text(encoding="utf-8"))
            self.assertEqual("failed", payload["status"])
            self.assertFalse(payload["valid"])
            self.assertIn(
                "optimized path appears to have performed a full pull; lazy-pull result may be invalid",
                payload["warnings"],
            )
            self.assertIn(
                "optimized path appears to have performed a full pull; lazy-pull result may be invalid",
                payload["summary"]["warnings"],
            )
            self.assertIn(
                "optimized lazy path appears to have performed a full pull; result is invalid",
                payload["invalid_reasons"],
            )

    def test_benchmark_stage_supports_triple_lazy_summary_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            env_payload = build_benchmark_env_payload(image_local_status="warn")

            def fake_run(argv, **kwargs):
                if argv[1].endswith("check_env.sh"):
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(env_payload, ensure_ascii=False, indent=2) + "\n",
                        "",
                    )
                if argv[1].endswith("coldstart_bench_strict.sh"):
                    self.assertIn("--comparison-mode", argv)
                    self.assertIn("raw-full-vs-estargz-lazy-triple", argv)
                    self.assertIn("--unopt-image", argv)
                    self.assertEqual("127.0.0.1:5001/demo/app:esgz-unopt", argv[argv.index("--unopt-image") + 1])
                    self.assertEqual("overlayfs", argv[argv.index("--base-snap") + 1])
                    self.assertEqual("stargz", argv[argv.index("--opt-snap") + 1])
                    csv_path = Path(argv[argv.index("--csv") + 1])
                    log_dir = Path(argv[argv.index("--log-dir") + 1])
                    log_dir.mkdir(parents=True, exist_ok=True)
                    csv_path.write_text(
                        "ts,label,snapshotter,mode,pull_ms,run_ms,ready_wait_ms,end_to_end_ready_ms,ready_ms,predict_extra_ms,service_bootstrap_ms,hot_files_loaded,hot_bytes_loaded,loaded_paths_count,proof_exists,proof_done,lazy_prepare_ms,lazy_ready_ms,ready_ms_semantics,stargz_cache_bytes,stargz_cache_bytes_before,content_large_blob_present_before,content_large_blob_present_after,content_store_present_bytes_before,content_store_present_bytes_after,explicit_pull_executed,full_pull_evidence,lazy_guard_failed,lazy_full_pull_warning,status,err\n"
                        "2026-03-17T00:00:00+00:00,baseline,overlayfs,ready,800,50,120,970,970,-1,780,128,268435456,128,0,0,-1,-1,end_to_end_ready_ms,-1,-1,-1,-1,-1,-1,1,0,0,0,ok,\n"
                        "2026-03-17T00:00:01+00:00,unoptimized_lazy,stargz,ready,0,40,90,130,130,-1,520,128,268435456,128,0,0,15,130,lazy_ready_ms,4096,0,0,0,0,0,0,0,0,0,ok,\n"
                        "2026-03-17T00:00:02+00:00,optimized,stargz,ready,0,35,75,110,110,-1,500,128,268435456,128,0,0,12,110,lazy_ready_ms,4096,0,0,0,0,0,0,0,0,0,ok,\n",
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(argv, 0, "", "")
                raise AssertionError(f"unexpected argv: {argv}")

            with mock.patch("orchestrator.stages.benchmark.subprocess.run", side_effect=fake_run):
                result = run_benchmark_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    base_image="127.0.0.1:5001/demo/app:raw",
                    unopt_image="127.0.0.1:5001/demo/app:esgz-unopt",
                    opt_image="127.0.0.1:5001/demo/app:esgz-opt",
                    runs=1,
                    mode="ready",
                    ready_url="http://127.0.0.1:8000/ready",
                    comparison_mode="raw-full-vs-estargz-lazy-triple",
                    pull_policy="always",
                    drop_local_image_ref=True,
                )

            payload = json.loads(result.result_path.read_text(encoding="utf-8"))
            self.assertTrue(payload["valid"])
            self.assertEqual("raw-full-vs-estargz-lazy-triple", payload["comparison_mode"])
            self.assertEqual(
                "baseline_full_pull_ready_ms_vs_unoptimized_lazy_ready_ms_vs_optimized_lazy_ready_ms",
                payload["ready_ms_semantics"],
            )
            self.assertEqual(970.0, payload["baseline_median_full_pull_ready_ms"])
            self.assertEqual(130.0, payload["unoptimized_median_lazy_ready_ms"])
            self.assertEqual(110.0, payload["optimized_median_lazy_ready_ms"])
            self.assertAlmostEqual((970.0 - 130.0) * 100.0 / 970.0, payload["unoptimized_vs_raw_improvement_pct"], places=6)
            self.assertAlmostEqual((970.0 - 110.0) * 100.0 / 970.0, payload["optimized_vs_raw_improvement_pct"], places=6)
            self.assertAlmostEqual((130.0 - 110.0) * 100.0 / 130.0, payload["optimized_vs_unoptimized_improvement_pct"], places=6)
            self.assertEqual(130.0, payload["summary"]["labels"]["unoptimized_lazy"]["median_lazy_ready_ms"])
            self.assertEqual(110.0, payload["summary"]["labels"]["optimized"]["median_lazy_ready_ms"])
            self.assertAlmostEqual(
                (130.0 - 110.0) * 100.0 / 130.0,
                payload["summary"]["comparison"]["optimized_vs_unoptimized_improvement_pct"],
                places=6,
            )
            self.assertEqual("127.0.0.1:5001/demo/app:esgz-unopt", payload["rows"][1]["image"])

    def test_benchmark_stage_triple_mode_allows_negative_optimized_vs_unoptimized_delta(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            env_payload = build_benchmark_env_payload(image_local_status="warn")

            def fake_run(argv, **kwargs):
                if argv[1].endswith("check_env.sh"):
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(env_payload, ensure_ascii=False, indent=2) + "\n",
                        "",
                    )
                if argv[1].endswith("coldstart_bench_strict.sh"):
                    csv_path = Path(argv[argv.index("--csv") + 1])
                    log_dir = Path(argv[argv.index("--log-dir") + 1])
                    log_dir.mkdir(parents=True, exist_ok=True)
                    csv_path.write_text(
                        "ts,label,snapshotter,mode,pull_ms,run_ms,ready_wait_ms,end_to_end_ready_ms,ready_ms,predict_extra_ms,service_bootstrap_ms,hot_files_loaded,hot_bytes_loaded,loaded_paths_count,proof_exists,proof_done,lazy_prepare_ms,lazy_ready_ms,ready_ms_semantics,stargz_cache_bytes,stargz_cache_bytes_before,content_large_blob_present_before,content_large_blob_present_after,content_store_present_bytes_before,content_store_present_bytes_after,explicit_pull_executed,full_pull_evidence,lazy_guard_failed,lazy_full_pull_warning,status,err\n"
                        "2026-03-17T00:00:00+00:00,baseline,overlayfs,ready,800,50,120,970,970,-1,780,128,268435456,128,0,0,-1,-1,end_to_end_ready_ms,-1,-1,-1,-1,-1,-1,1,0,0,0,ok,\n"
                        "2026-03-17T00:00:01+00:00,unoptimized_lazy,stargz,ready,0,40,60,100,100,-1,520,128,268435456,128,0,0,15,100,lazy_ready_ms,4096,0,0,0,0,0,0,0,0,0,ok,\n"
                        "2026-03-17T00:00:02+00:00,optimized,stargz,ready,0,50,70,120,120,-1,500,128,268435456,128,0,0,12,120,lazy_ready_ms,4096,0,0,0,0,0,0,0,0,0,ok,\n",
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(argv, 0, "", "")
                raise AssertionError(f"unexpected argv: {argv}")

            with mock.patch("orchestrator.stages.benchmark.subprocess.run", side_effect=fake_run):
                result = run_benchmark_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    base_image="127.0.0.1:5001/demo/app:raw",
                    unopt_image="127.0.0.1:5001/demo/app:esgz-unopt",
                    opt_image="127.0.0.1:5001/demo/app:esgz-opt",
                    runs=1,
                    mode="ready",
                    ready_url="http://127.0.0.1:8000/ready",
                    comparison_mode="raw-full-vs-estargz-lazy-triple",
                    pull_policy="always",
                    drop_local_image_ref=True,
                )

            payload = json.loads(result.result_path.read_text(encoding="utf-8"))
            self.assertEqual("passed", payload["status"])
            self.assertTrue(payload["valid"])
            self.assertAlmostEqual(-20.0, payload["optimized_vs_unoptimized_improvement_pct"], places=6)

    def test_benchmark_stage_triple_mode_reports_unoptimized_full_pull_with_label_specific_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            env_payload = build_benchmark_env_payload(image_local_status="warn")

            def fake_run(argv, **kwargs):
                if argv[1].endswith("check_env.sh"):
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(env_payload, ensure_ascii=False, indent=2) + "\n",
                        "",
                    )
                if argv[1].endswith("coldstart_bench_strict.sh"):
                    csv_path = Path(argv[argv.index("--csv") + 1])
                    log_dir = Path(argv[argv.index("--log-dir") + 1])
                    log_dir.mkdir(parents=True, exist_ok=True)
                    csv_path.write_text(
                        "ts,label,snapshotter,mode,pull_ms,run_ms,ready_wait_ms,end_to_end_ready_ms,ready_ms,predict_extra_ms,service_bootstrap_ms,hot_files_loaded,hot_bytes_loaded,loaded_paths_count,proof_exists,proof_done,lazy_prepare_ms,lazy_ready_ms,ready_ms_semantics,stargz_cache_bytes,stargz_cache_bytes_before,content_large_blob_present_before,content_large_blob_present_after,content_store_present_bytes_before,content_store_present_bytes_after,explicit_pull_executed,full_pull_evidence,lazy_guard_failed,lazy_full_pull_warning,status,err\n"
                        "2026-03-17T00:00:00+00:00,baseline,overlayfs,ready,800,50,120,970,970,-1,780,128,268435456,128,0,0,-1,-1,end_to_end_ready_ms,-1,-1,-1,-1,-1,-1,1,0,0,0,ok,\n"
                        "2026-03-17T00:00:01+00:00,unoptimized_lazy,stargz,ready,0,40050,90,40140,40140,-1,520,128,268435456,128,0,0,20,40140,lazy_ready_ms,8589934592,0,0,1,0,8589934592,0,1,1,1,fail,unoptimized_lazy-lazy-path-performed-full-pull\n"
                        "2026-03-17T00:00:02+00:00,optimized,stargz,ready,0,35,75,110,110,-1,500,128,268435456,128,0,0,12,110,lazy_ready_ms,4096,0,0,0,0,0,0,0,0,0,ok,\n",
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(argv, 0, "", "")
                raise AssertionError(f"unexpected argv: {argv}")

            with mock.patch("orchestrator.stages.benchmark.subprocess.run", side_effect=fake_run):
                result = run_benchmark_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    base_image="127.0.0.1:5001/demo/app:raw",
                    unopt_image="127.0.0.1:5001/demo/app:esgz-unopt",
                    opt_image="127.0.0.1:5001/demo/app:esgz-opt",
                    runs=1,
                    mode="ready",
                    ready_url="http://127.0.0.1:8000/ready",
                    comparison_mode="raw-full-vs-estargz-lazy-triple",
                    pull_policy="always",
                    drop_local_image_ref=True,
                )

            payload = json.loads(result.result_path.read_text(encoding="utf-8"))
            self.assertEqual("failed", payload["status"])
            self.assertFalse(payload["valid"])
            self.assertIn(
                "unoptimized_lazy path appears to have performed a full pull; lazy-pull result may be invalid",
                payload["warnings"],
            )
            self.assertIn(
                "unoptimized_lazy lazy path appears to have performed a full pull; result is invalid",
                payload["invalid_reasons"],
            )

    def test_benchmark_stage_triple_mode_requires_two_successes_per_label_when_runs_is_three(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            env_payload = build_benchmark_env_payload(image_local_status="warn")

            def fake_run(argv, **kwargs):
                if argv[1].endswith("check_env.sh"):
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(env_payload, ensure_ascii=False, indent=2) + "\n",
                        "",
                    )
                if argv[1].endswith("coldstart_bench_strict.sh"):
                    csv_path = Path(argv[argv.index("--csv") + 1])
                    log_dir = Path(argv[argv.index("--log-dir") + 1])
                    log_dir.mkdir(parents=True, exist_ok=True)
                    csv_path.write_text(
                        "ts,label,snapshotter,mode,pull_ms,run_ms,ready_wait_ms,end_to_end_ready_ms,ready_ms,predict_extra_ms,service_bootstrap_ms,hot_files_loaded,hot_bytes_loaded,loaded_paths_count,proof_exists,proof_done,lazy_prepare_ms,lazy_ready_ms,ready_ms_semantics,stargz_cache_bytes,stargz_cache_bytes_before,content_large_blob_present_before,content_large_blob_present_after,content_store_present_bytes_before,content_store_present_bytes_after,explicit_pull_executed,full_pull_evidence,lazy_guard_failed,lazy_full_pull_warning,status,err\n"
                        "2026-03-17T00:00:00+00:00,baseline,overlayfs,ready,800,50,120,970,970,-1,780,128,268435456,128,0,0,-1,-1,end_to_end_ready_ms,-1,-1,-1,-1,-1,-1,1,0,0,0,ok,\n"
                        "2026-03-17T00:00:01+00:00,baseline,overlayfs,ready,820,52,118,990,990,-1,780,128,268435456,128,0,0,-1,-1,end_to_end_ready_ms,-1,-1,-1,-1,-1,-1,1,0,0,0,ok,\n"
                        "2026-03-17T00:00:02+00:00,baseline,overlayfs,ready,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,0,0,-1,-1,end_to_end_ready_ms,-1,-1,-1,-1,-1,-1,1,0,0,0,fail,ready-timeout\n"
                        "2026-03-17T00:00:03+00:00,unoptimized_lazy,stargz,ready,0,40,90,130,130,-1,520,128,268435456,128,0,0,15,130,lazy_ready_ms,4096,0,0,0,0,0,0,0,0,0,ok,\n"
                        "2026-03-17T00:00:04+00:00,unoptimized_lazy,stargz,ready,0,-1,-1,-1,-1,-1,-1,-1,-1,-1,0,0,15,-1,lazy_ready_ms,4096,0,0,0,0,0,0,0,0,0,fail,ready-timeout\n"
                        "2026-03-17T00:00:05+00:00,unoptimized_lazy,stargz,ready,0,-1,-1,-1,-1,-1,-1,-1,-1,-1,0,0,15,-1,lazy_ready_ms,4096,0,0,0,0,0,0,0,0,0,fail,ready-timeout\n"
                        "2026-03-17T00:00:06+00:00,optimized,stargz,ready,0,35,75,110,110,-1,500,128,268435456,128,0,0,12,110,lazy_ready_ms,4096,0,0,0,0,0,0,0,0,0,ok,\n"
                        "2026-03-17T00:00:07+00:00,optimized,stargz,ready,0,37,73,112,112,-1,500,128,268435456,128,0,0,13,112,lazy_ready_ms,4096,0,0,0,0,0,0,0,0,0,ok,\n"
                        "2026-03-17T00:00:08+00:00,optimized,stargz,ready,0,-1,-1,-1,-1,-1,-1,-1,-1,-1,0,0,12,-1,lazy_ready_ms,4096,0,0,0,0,0,0,0,0,0,fail,ready-timeout\n",
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(argv, 0, "", "")
                raise AssertionError(f"unexpected argv: {argv}")

            with mock.patch("orchestrator.stages.benchmark.subprocess.run", side_effect=fake_run):
                result = run_benchmark_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    base_image="127.0.0.1:5001/demo/app:raw",
                    unopt_image="127.0.0.1:5001/demo/app:esgz-unopt",
                    opt_image="127.0.0.1:5001/demo/app:esgz-opt",
                    runs=3,
                    mode="ready",
                    ready_url="http://127.0.0.1:8000/ready",
                    comparison_mode="raw-full-vs-estargz-lazy-triple",
                    pull_policy="always",
                    drop_local_image_ref=True,
                )

            payload = json.loads(result.result_path.read_text(encoding="utf-8"))
            self.assertEqual("failed", payload["status"])
            self.assertIn(
                "unoptimized_lazy produced only 1 successful benchmark rows; need at least 2",
                payload["invalid_reasons"],
            )

    def test_benchmark_stage_adds_pull_dominated_warning_for_raw_vs_estargz(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            env_payload = build_benchmark_env_payload(image_local_status="warn")

            def fake_run(argv, **kwargs):
                if argv[1].endswith("check_env.sh"):
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(env_payload, ensure_ascii=False, indent=2) + "\n",
                        "",
                    )
                if argv[1].endswith("coldstart_bench_strict.sh"):
                    csv_path = Path(argv[argv.index("--csv") + 1])
                    log_dir = Path(argv[argv.index("--log-dir") + 1])
                    log_dir.mkdir(parents=True, exist_ok=True)
                    csv_path.write_text(
                        "ts,label,snapshotter,mode,pull_ms,run_ms,ready_wait_ms,end_to_end_ready_ms,ready_ms,predict_extra_ms,service_bootstrap_ms,hot_files_loaded,hot_bytes_loaded,loaded_paths_count,proof_exists,proof_done,status,err\n"
                        "2026-03-17T00:00:00+00:00,baseline,overlayfs,ready,820,40,80,940,940,-1,780,32,67108864,32,0,0,ok,\n"
                        "2026-03-17T00:00:01+00:00,optimized,stargz,ready,760,20,40,820,820,-1,530,32,67108864,32,0,0,ok,\n",
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(argv, 0, "", "")
                raise AssertionError(f"unexpected argv: {argv}")

            with mock.patch("orchestrator.stages.benchmark.subprocess.run", side_effect=fake_run):
                result = run_benchmark_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    base_image="127.0.0.1:5001/abtest/ready-serving-layout-trap-v5:src-raw",
                    opt_image="127.0.0.1:5001/abtest/ready-serving-layout-trap-v5:esgz-hinted-v1",
                    runs=1,
                    mode="ready",
                    ready_url="http://127.0.0.1:8000/ready",
                    comparison_mode="raw-vs-estargz",
                    pull_policy="always",
                    drop_local_image_ref=True,
                )

            payload = json.loads(result.result_path.read_text(encoding="utf-8"))
            self.assertIn(
                "The current runtime path appears pull-dominated; lazy pulling benefits may be masked by full image transfer behavior.",
                payload["warnings"],
            )
            self.assertIn(
                "The current runtime path appears pull-dominated; lazy pulling benefits may be masked by full image transfer behavior.",
                payload["summary"]["warnings"],
            )

    def test_benchmark_stage_allows_registry_pull_mode_without_local_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            env_payload = build_benchmark_env_payload(image_local_status="warn")

            def fake_run(argv, **kwargs):
                if argv[1].endswith("check_env.sh"):
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(env_payload, ensure_ascii=False, indent=2) + "\n",
                        "",
                    )
                if argv[1].endswith("coldstart_bench_strict.sh"):
                    self.assertIn("--pull-policy", argv)
                    self.assertIn("always", argv)
                    self.assertIn("--drop-local-image-ref", argv)
                    csv_path = Path(argv[argv.index("--csv") + 1])
                    log_dir = Path(argv[argv.index("--log-dir") + 1])
                    log_dir.mkdir(parents=True, exist_ok=True)
                    csv_path.write_text(
                        "ts,label,snapshotter,mode,ready_ms,predict_extra_ms,proof_exists,proof_done,status,err\n"
                        "2026-03-17T00:00:00+00:00,baseline,stargz,ready,120,-1,0,0,ok,\n"
                        "2026-03-17T00:00:01+00:00,optimized,stargz,ready,90,-1,0,0,ok,\n",
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(argv, 0, "", "")
                raise AssertionError(f"unexpected argv: {argv}")

            with mock.patch("orchestrator.stages.benchmark.subprocess.run", side_effect=fake_run):
                result = run_benchmark_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    base_image="127.0.0.1:5001/docker.io/library/app:base",
                    opt_image="127.0.0.1:5001/docker.io/library/app:opt",
                    runs=1,
                    mode="ready",
                    ready_url="http://127.0.0.1:9000/ready",
                    pull_policy="always",
                    drop_local_image_ref=True,
                )

            payload = json.loads(result.result_path.read_text(encoding="utf-8"))
            self.assertTrue(result.valid)
            self.assertTrue(payload["valid"])
            self.assertEqual("always", payload["pull_policy"])
            self.assertTrue(payload["drop_local_image_ref"])
            self.assertEqual([], payload["warnings"])

    def test_benchmark_stage_writes_performance_summary_and_no_quality_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            env_payload = build_benchmark_env_payload()

            def fake_run(argv, **kwargs):
                if argv[1].endswith("check_env.sh"):
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(env_payload, ensure_ascii=False, indent=2) + "\n",
                        "",
                    )
                if argv[1].endswith("coldstart_bench_strict.sh"):
                    csv_path = Path(argv[argv.index("--csv") + 1])
                    log_dir = Path(argv[argv.index("--log-dir") + 1])
                    log_dir.mkdir(parents=True, exist_ok=True)
                    csv_path.write_text(
                        "ts,label,snapshotter,mode,ready_ms,predict_extra_ms,proof_exists,proof_done,status,err\n"
                        "2026-03-17T00:00:00+00:00,baseline,stargz,ready,120,-1,1,2,ok,\n"
                        "2026-03-17T00:00:01+00:00,optimized,stargz,ready,90,-1,9,10,ok,\n",
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(argv, 0, "", "")
                raise AssertionError(f"unexpected argv: {argv}")

            with mock.patch("orchestrator.stages.benchmark.subprocess.run", side_effect=fake_run):
                result = run_benchmark_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    base_image="docker.io/library/app:base",
                    opt_image="docker.io/library/app:opt",
                    runs=1,
                    mode="ready",
                    ready_url="http://127.0.0.1:9000/ready",
                )

            payload = json.loads(result.result_path.read_text(encoding="utf-8"))
            self.assertEqual("passed", payload["status"])
            self.assertTrue(payload["valid"])
            self.assertEqual(120.0, payload["baseline_median_ready_ms"])
            self.assertEqual(90.0, payload["optimized_median_ready_ms"])
            self.assertEqual(25.0, payload["improvement_pct"])
            self.assertEqual(2, payload["summary"]["rows_total"])
            self.assertEqual(120.0, payload["summary"]["labels"]["baseline"]["median_ready_ms"])
            self.assertEqual(90.0, payload["summary"]["labels"]["optimized"]["median_ready_ms"])
            self.assertEqual(-30.0, payload["summary"]["comparison"]["median_ready_delta_ms"])
            self.assertEqual(25.0, payload["summary"]["comparison"]["median_ready_improvement_pct"])
            self.assertNotIn("prefetch_quality_status", payload)
            self.assertNotIn("prefetch_feedback", payload)
            self.assertNotIn("hot_path_feedback", payload)
            self.assertNotIn("feedback_warnings", payload)
            self.assertNotIn("result_contract", payload)
            self.assertNotIn("median_proof_exists", payload["summary"]["labels"]["baseline"])
            self.assertNotIn("median_proof_done", payload["summary"]["labels"]["optimized"])

    def test_benchmark_stage_records_debug_state_rows_and_service_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            env_payload = build_benchmark_env_payload()

            def fake_run(argv, **kwargs):
                if argv[1].endswith("check_env.sh"):
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(env_payload, ensure_ascii=False, indent=2) + "\n",
                        "",
                    )
                if argv[1].endswith("coldstart_bench_strict.sh"):
                    self.assertIn("--debug-state-url", argv)
                    self.assertIn("http://127.0.0.1:8000/debug/state", argv)
                    csv_path = Path(argv[argv.index("--csv") + 1])
                    log_dir = Path(argv[argv.index("--log-dir") + 1])
                    log_dir.mkdir(parents=True, exist_ok=True)
                    csv_path.write_text(
                        "ts,label,snapshotter,mode,ready_ms,predict_extra_ms,service_bootstrap_ms,hot_files_loaded,hot_bytes_loaded,loaded_paths_count,proof_exists,proof_done,status,err\n"
                        "2026-03-17T00:00:00+00:00,baseline,stargz,ready,120,-1,786,128,268435456,128,0,0,ok,\n"
                        "2026-03-17T00:00:01+00:00,optimized,stargz,ready,90,-1,512,128,268435456,128,0,0,ok,\n",
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(argv, 0, "", "")
                raise AssertionError(f"unexpected argv: {argv}")

            with mock.patch("orchestrator.stages.benchmark.subprocess.run", side_effect=fake_run):
                result = run_benchmark_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    base_image="docker.io/library/app:base",
                    opt_image="docker.io/library/app:opt",
                    runs=1,
                    mode="ready",
                    ready_url="http://127.0.0.1:8000/ready",
                    debug_state_url="http://127.0.0.1:8000/debug/state",
                )

            payload = json.loads(result.result_path.read_text(encoding="utf-8"))
            self.assertEqual(786.0, payload["summary"]["labels"]["baseline"]["median_service_bootstrap_ms"])
            self.assertEqual(786, payload["summary"]["labels"]["baseline"]["min_service_bootstrap_ms"])
            self.assertEqual(786, payload["summary"]["labels"]["baseline"]["max_service_bootstrap_ms"])
            self.assertEqual(512.0, payload["summary"]["labels"]["optimized"]["median_service_bootstrap_ms"])
            self.assertEqual(786, payload["rows"][0]["service_bootstrap_ms"])
            self.assertEqual(128, payload["rows"][0]["hot_files_loaded"])
            self.assertEqual(268435456, payload["rows"][0]["hot_bytes_loaded"])
            self.assertEqual(128, payload["rows"][0]["loaded_paths_count"])

    def test_registry_cold_strict_result_serializes_top_level_ready_summary_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            env_payload = build_benchmark_env_payload()
            root_index_digest = "sha256:" + ("a" * 64)
            base_manifest_digest = "sha256:" + ("b" * 64)
            opt_manifest_digest = "sha256:" + ("c" * 64)
            base_config_digest = "sha256:" + ("d" * 64)
            opt_config_digest = "sha256:" + ("e" * 64)

            def _manifest_payload(index_digest: str, child_digest: str) -> str:
                return json.dumps(
                    {
                        "Descriptor": {"digest": index_digest},
                        "mediaType": "application/vnd.oci.image.index.v1+json",
                        "digest": index_digest,
                        "manifests": [
                            {
                                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                                "digest": child_digest,
                                "platform": {"os": "linux", "architecture": "amd64"},
                            }
                        ],
                    },
                    ensure_ascii=False,
                )

            def _child_payload(manifest_digest: str, config_digest: str) -> str:
                return json.dumps(
                    {
                        "Descriptor": {"digest": manifest_digest},
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "digest": manifest_digest,
                        "config": {
                            "mediaType": "application/vnd.oci.image.config.v1+json",
                            "digest": config_digest,
                        },
                        "layers": [],
                    },
                    ensure_ascii=False,
                )

            def fake_run(argv, **kwargs):
                if argv[0] == "nerdctl" and argv[1:3] == ["manifest", "inspect"]:
                    ref = argv[3]
                    if ref == "docker.io/library/app:base":
                        return subprocess.CompletedProcess(argv, 0, _manifest_payload(root_index_digest, base_manifest_digest), "")
                    if ref == "docker.io/library/app:opt":
                        return subprocess.CompletedProcess(argv, 0, _manifest_payload(root_index_digest, opt_manifest_digest), "")
                    if ref == f"docker.io/library/app@{base_manifest_digest}":
                        return subprocess.CompletedProcess(argv, 0, _child_payload(base_manifest_digest, base_config_digest), "")
                    if ref == f"docker.io/library/app@{opt_manifest_digest}":
                        return subprocess.CompletedProcess(argv, 0, _child_payload(opt_manifest_digest, opt_config_digest), "")
                    raise AssertionError(f"unexpected manifest ref: {ref}")
                if argv[1].endswith("check_env.sh"):
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(env_payload, ensure_ascii=False, indent=2) + "\n",
                        "",
                    )
                if argv[1].endswith("coldstart_bench_strict.sh"):
                    csv_path = Path(argv[argv.index("--csv") + 1])
                    log_dir = Path(argv[argv.index("--log-dir") + 1])
                    strict_input = Path(argv[argv.index("--strict-input-json") + 1])
                    strict_proof = Path(argv[argv.index("--proof-json") + 1])
                    log_dir.mkdir(parents=True, exist_ok=True)
                    csv_path.write_text(
                        "ts,label,snapshotter,mode,ready_ms,predict_extra_ms,proof_exists,proof_done,status,err\n"
                        "2026-03-17T00:00:00+00:00,baseline,stargz,ready,4905,-1,0,0,ok,\n"
                        "2026-03-17T00:00:01+00:00,baseline,stargz,ready,5715,-1,0,0,ok,\n"
                        "2026-03-17T00:00:02+00:00,baseline,stargz,ready,5733,-1,0,0,ok,\n"
                        "2026-03-17T00:00:03+00:00,optimized,stargz,ready,6303,-1,0,0,ok,\n"
                        "2026-03-17T00:00:04+00:00,optimized,stargz,ready,7119,-1,0,0,ok,\n"
                        "2026-03-17T00:00:05+00:00,optimized,stargz,ready,5624,-1,0,0,ok,\n",
                        encoding="utf-8",
                    )
                    strict_input.write_text(
                        json.dumps(
                            {
                                "base": {"requested_ref": "docker.io/library/app:base", "resolved_ref": f"docker.io/library/app@{base_manifest_digest}", "closure_digests": [root_index_digest, base_manifest_digest, base_config_digest]},
                                "optimized": {"requested_ref": "docker.io/library/app:opt", "resolved_ref": f"docker.io/library/app@{opt_manifest_digest}", "closure_digests": [root_index_digest, opt_manifest_digest, opt_config_digest]},
                                "shared_closure_digests": [],
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    strict_proof.write_text(
                        json.dumps(
                            {
                                "strict_proof_zero": True,
                                "required_zero": {
                                    "local_image_refs_remaining_base": 0,
                                    "local_image_refs_remaining_opt": 0,
                                    "stargz_runtime_mounts_remaining": 0,
                                    "stargz_cache_entries_remaining": 0,
                                    "content_store_digests_remaining_base": 0,
                                    "content_store_digests_remaining_opt": 0,
                                },
                                "required_true": {
                                    "base_local_ref_deleted": True,
                                    "opt_local_ref_deleted": True,
                                },
                                "best_effort": {
                                    "page_cache_drop_attempted": True,
                                    "page_cache_drop_succeeded": True,
                                    "page_cache_proof_mode": "required-best-effort",
                                },
                                "diagnostic": {
                                    "global_stargz_snapshot_records_remaining": 42,
                                    "global_stargz_local_snapshot_entries_remaining": 42,
                                },
                                "unverifiable": [],
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(argv, 0, "", "")
                raise AssertionError(f"unexpected argv: {argv}")

            with mock.patch("orchestrator.stages.benchmark.subprocess.run", side_effect=fake_run):
                result = run_benchmark_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    base_image="docker.io/library/app:base",
                    opt_image="docker.io/library/app:opt",
                    runs=3,
                    mode="ready",
                    ready_url="http://127.0.0.1:9000/ready",
                    cold_mode="registry-cold-strict",
                    pull_policy="always",
                    drop_local_image_ref=True,
                    base_snapshotter="stargz",
                    opt_snapshotter="stargz",
                )

            payload = json.loads(result.result_path.read_text(encoding="utf-8"))
            self.assertEqual(5715.0, payload["baseline_median_ready_ms"])
            self.assertEqual(6303.0, payload["optimized_median_ready_ms"])
            self.assertAlmostEqual((5715.0 - 6303.0) * 100.0 / 5715.0, payload["improvement_pct"], places=6)
            self.assertEqual(5715.0, payload["summary"]["labels"]["baseline"]["median_ready_ms"])
            self.assertEqual(6303.0, payload["summary"]["labels"]["optimized"]["median_ready_ms"])
            self.assertAlmostEqual(
                (5715.0 - 6303.0) * 100.0 / 5715.0,
                payload["summary"]["comparison"]["median_ready_improvement_pct"],
                places=6,
            )

    def test_benchmark_stage_marks_all_failed_rows_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            env_payload = build_benchmark_env_payload()

            def fake_run(argv, **kwargs):
                if argv[1].endswith("check_env.sh"):
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(env_payload, ensure_ascii=False, indent=2) + "\n",
                        "",
                    )
                if argv[1].endswith("coldstart_bench_strict.sh"):
                    csv_path = Path(argv[argv.index("--csv") + 1])
                    log_dir = Path(argv[argv.index("--log-dir") + 1])
                    log_dir.mkdir(parents=True, exist_ok=True)
                    csv_path.write_text(
                        "ts,label,snapshotter,mode,ready_ms,predict_extra_ms,proof_exists,proof_done,status,err\n"
                        "2026-03-17T00:00:00+00:00,baseline,stargz,ready,-1,-1,0,0,fail,network-prep-failed\n"
                        "2026-03-17T00:00:01+00:00,optimized,stargz,ready,-1,-1,0,0,fail,network-prep-failed\n",
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(argv, 0, "", "")
                raise AssertionError(f"unexpected argv: {argv}")

            with mock.patch("orchestrator.stages.benchmark.subprocess.run", side_effect=fake_run):
                result = run_benchmark_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    base_image="docker.io/library/app:base",
                    opt_image="docker.io/library/app:opt",
                    runs=1,
                    mode="ready",
                    ready_url="http://127.0.0.1:9000/ready",
                )

            payload = json.loads(result.result_path.read_text(encoding="utf-8"))
            self.assertFalse(result.valid)
            self.assertEqual("failed", payload["status"])
            self.assertIn("baseline produced 0 successful benchmark rows", payload["invalid_reasons"])
            self.assertIn("optimized produced 0 successful benchmark rows", payload["invalid_reasons"])
            self.assertTrue(any("ready-network preparation was impossible" in item for item in payload["invalid_reasons"]))

    def test_benchmark_stage_does_not_enable_http_200_ready_for_non_self_starting_http_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            env_payload = build_benchmark_env_payload()

            def fake_run(argv, **kwargs):
                if argv[1].endswith("check_env.sh"):
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(env_payload, ensure_ascii=False, indent=2) + "\n",
                        "",
                    )
                if argv[1].endswith("coldstart_bench_strict.sh"):
                    self.assertNotIn("--ready-http-status-ok-is-ready", argv)
                    csv_path = Path(argv[argv.index("--csv") + 1])
                    log_dir = Path(argv[argv.index("--log-dir") + 1])
                    log_dir.mkdir(parents=True, exist_ok=True)
                    csv_path.write_text(
                        "ts,label,snapshotter,mode,ready_ms,predict_extra_ms,proof_exists,proof_done,status,err\n"
                        "2026-03-17T00:00:00+00:00,baseline,overlayfs,ready,120,-1,0,0,ok,\n"
                        "2026-03-17T00:00:01+00:00,optimized,stargz,ready,90,-1,0,0,ok,\n",
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(argv, 0, "", "")
                raise AssertionError(f"unexpected argv: {argv}")

            with mock.patch("orchestrator.stages.benchmark.subprocess.run", side_effect=fake_run):
                result = run_benchmark_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    base_image="docker.io/library/app:base",
                    opt_image="docker.io/library/app:opt",
                    runs=1,
                    mode="ready",
                    ready_url="http://127.0.0.1:9000/ready",
                    snapshotter="stargz",
                )

            self.assertTrue(result.valid)

    def test_benchmark_stage_passes_exec_ready_probe_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            env_payload = build_benchmark_env_payload()

            def fake_run(argv, **kwargs):
                if argv[1].endswith("check_env.sh"):
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(env_payload, ensure_ascii=False, indent=2) + "\n",
                        "",
                    )
                if argv[1].endswith("coldstart_bench_strict.sh"):
                    self.assertIn("--ready-exec", argv)
                    self.assertIn("test -f /tmp/ready", argv)
                    self.assertIn("--ready-shell", argv)
                    self.assertIn("bash", argv)
                    csv_path = Path(argv[argv.index("--csv") + 1])
                    log_dir = Path(argv[argv.index("--log-dir") + 1])
                    log_dir.mkdir(parents=True, exist_ok=True)
                    csv_path.write_text(
                        "ts,label,snapshotter,mode,ready_ms,predict_extra_ms,proof_exists,proof_done,status,err\n"
                        "2026-03-17T00:00:00+00:00,baseline,stargz,ready,120,-1,0,0,ok,\n"
                        "2026-03-17T00:00:01+00:00,optimized,stargz,ready,90,-1,0,0,ok,\n",
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(argv, 0, "", "")
                raise AssertionError(f"unexpected argv: {argv}")

            with mock.patch("orchestrator.stages.benchmark.subprocess.run", side_effect=fake_run):
                result = run_benchmark_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    base_image="docker.io/library/app:base",
                    opt_image="docker.io/library/app:opt",
                    runs=1,
                    mode="ready",
                    ready_hook=ExecReadyHook("test -f /tmp/ready", shell="bash"),
                )

            self.assertTrue(result.valid)

    def test_benchmark_stage_passes_log_regex_ready_probe_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            env_payload = build_benchmark_env_payload()

            def fake_run(argv, **kwargs):
                if argv[1].endswith("check_env.sh"):
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(env_payload, ensure_ascii=False, indent=2) + "\n",
                        "",
                    )
                if argv[1].endswith("coldstart_bench_strict.sh"):
                    self.assertIn("--ready-log-regex", argv)
                    self.assertIn("server ready", argv)
                    self.assertNotIn("--ready-url", argv)
                    csv_path = Path(argv[argv.index("--csv") + 1])
                    log_dir = Path(argv[argv.index("--log-dir") + 1])
                    log_dir.mkdir(parents=True, exist_ok=True)
                    csv_path.write_text(
                        "ts,label,snapshotter,mode,ready_ms,predict_extra_ms,proof_exists,proof_done,status,err\n"
                        "2026-03-17T00:00:00+00:00,baseline,stargz,ready,120,-1,0,0,ok,\n"
                        "2026-03-17T00:00:01+00:00,optimized,stargz,ready,90,-1,0,0,ok,\n",
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(argv, 0, "", "")
                raise AssertionError(f"unexpected argv: {argv}")

            with mock.patch("orchestrator.stages.benchmark.subprocess.run", side_effect=fake_run):
                result = run_benchmark_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    base_image="docker.io/library/app:base",
                    opt_image="docker.io/library/app:opt",
                    runs=1,
                    mode="ready",
                    ready_hook=LogRegexReadyHook("server ready"),
                )

            self.assertTrue(result.valid)

    def test_benchmark_stage_passes_container_runtime_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            env_payload = build_benchmark_env_payload()
            runtime_spec = container_runtime_spec(
                gpus="all",
                ipc="host",
                env=("HTTP_PROXY=http://proxy.local:8080",),
                mounts=("type=bind,src=/srv/model,dst=/models/model,readonly",),
                entrypoint="/entrypoint.sh",
                command=("serve", "--port", "9000"),
            )

            def fake_run(argv, **kwargs):
                if argv[1].endswith("check_env.sh"):
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(env_payload, ensure_ascii=False, indent=2) + "\n",
                        "",
                    )
                if argv[1].endswith("coldstart_bench_strict.sh"):
                    self.assertIn("--gpus", argv)
                    self.assertIn("all", argv)
                    self.assertIn("--ipc", argv)
                    self.assertIn("host", argv)
                    self.assertIn("--container-env", argv)
                    self.assertIn("HTTP_PROXY=http://proxy.local:8080", argv)
                    self.assertIn("--container-mount", argv)
                    self.assertIn("type=bind,src=/srv/model,dst=/models/model,readonly", argv)
                    self.assertIn("--container-entrypoint", argv)
                    self.assertIn("/entrypoint.sh", argv)
                    self.assertIn("--container-cmd", argv)
                    self.assertIn("serve", argv)
                    csv_path = Path(argv[argv.index("--csv") + 1])
                    log_dir = Path(argv[argv.index("--log-dir") + 1])
                    log_dir.mkdir(parents=True, exist_ok=True)
                    csv_path.write_text(
                        "ts,label,snapshotter,mode,ready_ms,predict_extra_ms,proof_exists,proof_done,status,err\n"
                        "2026-03-17T00:00:00+00:00,baseline,stargz,ready,120,-1,0,0,ok,\n"
                        "2026-03-17T00:00:01+00:00,optimized,stargz,ready,90,-1,0,0,ok,\n",
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(argv, 0, "", "")
                raise AssertionError(f"unexpected argv: {argv}")

            with mock.patch("orchestrator.stages.benchmark.subprocess.run", side_effect=fake_run):
                result = run_benchmark_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    base_image="docker.io/library/app:base",
                    opt_image="docker.io/library/app:opt",
                    runs=1,
                    mode="ready",
                    ready_exec="test -f /tmp/ready",
                    container_runtime=runtime_spec,
                )

            self.assertTrue(result.valid)

    def test_benchmark_stage_merges_model_mount_into_container_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            env_payload = build_benchmark_env_payload()
            model_dir = work_dir / "model"
            model_dir.mkdir()
            runtime_spec = container_runtime_spec(
                env=("EXISTING_ENV=1",),
                mounts=("type=bind,src=/data/cache,dst=/cache,readonly",),
            )

            def fake_run(argv, **kwargs):
                if argv[1].endswith("check_env.sh"):
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(env_payload, ensure_ascii=False, indent=2) + "\n",
                        "",
                    )
                if argv[1].endswith("coldstart_bench_strict.sh"):
                    mounts = [
                        argv[index + 1]
                        for index, value in enumerate(argv)
                        if value == "--container-mount"
                    ]
                    env_vars = [
                        argv[index + 1]
                        for index, value in enumerate(argv)
                        if value == "--container-env"
                    ]
                    self.assertIn("type=bind,src=/data/cache,dst=/cache,readonly", mounts)
                    self.assertIn(
                        f"type=bind,src={model_dir.resolve()},dst=/models/demo,readonly",
                        mounts,
                    )
                    self.assertIn("EXISTING_ENV=1", env_vars)
                    self.assertIn("MODEL_NAME=demo-model", env_vars)
                    self.assertIn("MODEL_BASE_PATH=/models/demo", env_vars)
                    self.assertNotIn("--model-host", argv)
                    self.assertNotIn("--model-dst", argv)
                    csv_path = Path(argv[argv.index("--csv") + 1])
                    log_dir = Path(argv[argv.index("--log-dir") + 1])
                    log_dir.mkdir(parents=True, exist_ok=True)
                    csv_path.write_text(
                        "ts,label,snapshotter,mode,ready_ms,predict_extra_ms,proof_exists,proof_done,status,err\n"
                        "2026-03-17T00:00:00+00:00,baseline,stargz,ready,120,-1,0,0,ok,\n"
                        "2026-03-17T00:00:01+00:00,optimized,stargz,ready,90,-1,0,0,ok,\n",
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(argv, 0, "", "")
                raise AssertionError(f"unexpected argv: {argv}")

            with mock.patch("orchestrator.stages.benchmark.subprocess.run", side_effect=fake_run):
                result = run_benchmark_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    base_image="docker.io/library/app:base",
                    opt_image="docker.io/library/app:opt",
                    runs=1,
                    mode="ready",
                    ready_exec="test -f /tmp/ready",
                    model_host_dir=str(model_dir),
                    model_mount_hook=ModelMountHook("/models/demo", "demo-model"),
                    container_runtime=runtime_spec,
                )

            self.assertTrue(result.valid)

    def test_benchmark_stage_requires_csv_output_from_shell_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            env_payload = build_benchmark_env_payload()

            def fake_run(argv, **kwargs):
                if argv[1].endswith("check_env.sh"):
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(env_payload, ensure_ascii=False, indent=2) + "\n",
                        "",
                    )
                if argv[1].endswith("coldstart_bench_strict.sh"):
                    log_dir = Path(argv[argv.index("--log-dir") + 1])
                    log_dir.mkdir(parents=True, exist_ok=True)
                    return subprocess.CompletedProcess(argv, 0, "", "")
                raise AssertionError(f"unexpected argv: {argv}")

            with mock.patch("orchestrator.stages.benchmark.subprocess.run", side_effect=fake_run):
                with self.assertRaisesRegex(RuntimeError, "coldstart_compare.csv"):
                    run_benchmark_stage(
                        repo_root=ROOT,
                        work_dir=work_dir,
                        base_image="docker.io/library/app:base",
                        opt_image="docker.io/library/app:opt",
                    )


if __name__ == "__main__":
    unittest.main()
