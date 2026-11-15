import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.stages.apply import default_destination_image, run_apply_stage


ROOT = Path(__file__).resolve().parents[1]


def build_apply_env_payload():
    checks = {
        "os_linux": "pass",
        "repo_layout": "pass",
        "privileged_user": "pass",
        "cmd_nerdctl": "pass",
        "cmd_python3": "pass",
        "nerdctl_runtime": "pass",
        "image_local": "pass",
        "ctr_remote_prefetch_list": "pass",
        "ctr_remote_runtime": "pass",
        "cmd_estargzify": "warn",
        "cmd_stargzify": "warn",
    }
    return {
        "schema_version": 1,
        "requested_profiles": ["apply"],
        "profiles": {"apply": {"status": "pass", "required_failures": [], "warnings": []}},
        "checks": [
            {
                "id": check_id,
                "status": status,
                "summary": check_id,
                "required_for": ["apply"],
                "advisory_for": [],
                "detail": "",
                "value": None,
            }
            for check_id, status in checks.items()
        ],
    }


class ApplyStageTests(unittest.TestCase):
    def test_default_destination_image(self):
        self.assertEqual(
            "docker.io/library/app:latest-stargz",
            default_destination_image("docker.io/library/app:latest"),
        )
        self.assertEqual(
            "docker.io/library/app:stargz-optimized",
            default_destination_image("docker.io/library/app"),
        )
        self.assertEqual(
            "docker.io/library/app:latest-stargz-chunk-shadow",
            default_destination_image("docker.io/library/app:latest", selector_mode="chunk-shadow"),
        )

    def test_apply_stage_dry_run_writes_plan_and_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            (work_dir / "estargz.list").write_text(
                "/app/a.so\n/dev/null\n/app/a.so\nrelative\n\n",
                encoding="utf-8",
            )

            env_payload = build_apply_env_payload()

            def fake_run(argv, **kwargs):
                if argv[1].endswith("check_env.sh"):
                    self.assertIn("--json", argv)
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(env_payload, ensure_ascii=False, indent=2) + "\n",
                        "",
                    )
                if argv[1].endswith("apply_hints_to_image.sh"):
                    self.assertIn("--dry-run", argv)
                    (work_dir / "estargz.list.cleaned").write_text("/app/a.so\n", encoding="utf-8")
                    return subprocess.CompletedProcess(argv, 0, "", "")
                raise AssertionError(f"unexpected argv: {argv}")

            with mock.patch("orchestrator.stages.apply.subprocess.run", side_effect=fake_run):
                result = run_apply_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    src_image="docker.io/library/app:latest",
                    namespace="test-ns",
                    dry_run=True,
                )

            cleaned = (work_dir / "estargz.list.cleaned").read_text(encoding="utf-8")
            payload = json.loads(result.apply_result_path.read_text(encoding="utf-8"))
            policy = json.loads(result.policy_path.read_text(encoding="utf-8"))

            self.assertEqual("/app/a.so\n", cleaned)
            self.assertEqual("docker.io/library/app:latest-stargz", result.dst_image)
            self.assertEqual("passed", payload["status"])
            self.assertEqual("dry_run", payload["result_kind"])
            self.assertTrue(payload["dry_run"])
            self.assertFalse(payload["optimized_image_written"])
            self.assertEqual("test-ns", payload["namespace"])
            self.assertEqual("unknown", payload["quality_status"])
            self.assertEqual("review_hints", payload["recommendation"])
            self.assertFalse(payload["result_contract"]["validation_inputs"]["ready_for_benchmark"])
            self.assertIn(
                "dry-run mode",
                payload["result_contract"]["validation_inputs"]["blockers"][0],
            )
            self.assertTrue(policy["ok"])

    def test_apply_stage_non_dry_run_requires_optimized_image_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            (work_dir / "estargz.list").write_text("/app/a.so\n", encoding="utf-8")
            env_payload = build_apply_env_payload()

            def fake_run(argv, **kwargs):
                if argv[1].endswith("check_env.sh"):
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(env_payload, ensure_ascii=False, indent=2) + "\n",
                        "",
                    )
                if argv[1].endswith("apply_hints_to_image.sh"):
                    return subprocess.CompletedProcess(argv, 0, "", "")
                raise AssertionError(f"unexpected argv: {argv}")

            with mock.patch("orchestrator.stages.apply.subprocess.run", side_effect=fake_run):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "optimized_image.txt is missing",
                ):
                    run_apply_stage(
                        repo_root=ROOT,
                        work_dir=work_dir,
                        src_image="docker.io/library/app:latest",
                        dry_run=False,
                    )

    def test_apply_stage_surfaces_stargz_smoke_check_failure_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            (work_dir / "estargz.list").write_text("/app/a.so\n", encoding="utf-8")
            env_payload = build_apply_env_payload()

            def fake_run(argv, **kwargs):
                if argv[1].endswith("check_env.sh"):
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(env_payload, ensure_ascii=False, indent=2) + "\n",
                        "",
                    )
                if argv[1].endswith("apply_hints_to_image.sh"):
                    log_fh = kwargs.get("stdout")
                    log_fh.write(
                        "[ERR] image optimize 成功，但对 nerdctl + stargz + pull=never 不可消费: "
                        "docker.io/library/app:latest-stargz\n"
                    )
                    log_fh.write(
                        "[ERR] inspect: nerdctl --namespace default image inspect "
                        "docker.io/library/app:latest-stargz\n"
                    )
                    log_fh.write(
                        "[ERR] run: nerdctl --namespace default run --rm --pull=never "
                        "--snapshotter stargz --platform linux/amd64 --entrypoint /bin/true "
                        "docker.io/library/app:latest-stargz\n"
                    )
                    log_fh.flush()
                    return subprocess.CompletedProcess(argv, 1, "", "")
                raise AssertionError(f"unexpected argv: {argv}")

            with mock.patch("orchestrator.stages.apply.subprocess.run", side_effect=fake_run):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "image optimize 成功，但对 nerdctl \\+ stargz \\+ pull=never 不可消费",
                ):
                    run_apply_stage(
                        repo_root=ROOT,
                        work_dir=work_dir,
                        src_image="docker.io/library/app:latest",
                        dry_run=False,
                    )

    def test_apply_stage_non_dry_run_records_optimized_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            (work_dir / "estargz.list").write_text("/app/a.so\n", encoding="utf-8")
            env_payload = build_apply_env_payload()

            def fake_run(argv, **kwargs):
                if argv[1].endswith("check_env.sh"):
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(env_payload, ensure_ascii=False, indent=2) + "\n",
                        "",
                    )
                if argv[1].endswith("apply_hints_to_image.sh"):
                    (work_dir / "optimized_image.txt").write_text(
                        "docker.io/library/app:latest-stargz\n",
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(argv, 0, "", "")
                raise AssertionError(f"unexpected argv: {argv}")

            with mock.patch("orchestrator.stages.apply.subprocess.run", side_effect=fake_run):
                result = run_apply_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    src_image="docker.io/library/app:latest",
                    dry_run=False,
                )

            payload = json.loads(result.apply_result_path.read_text(encoding="utf-8"))
            self.assertEqual("applied", payload["result_kind"])
            self.assertTrue(payload["optimized_image_written"])
            self.assertEqual(
                "docker.io/library/app:latest-stargz",
                payload["optimized_image"],
            )
            self.assertEqual("good", payload["quality_status"])
            self.assertEqual("proceed", payload["recommendation"])
            self.assertTrue(payload["result_contract"]["validation_inputs"]["ready_for_benchmark"])
            self.assertTrue(payload["result_contract"]["validation_inputs"]["ready_for_publish_evaluation"])
            self.assertEqual([], payload["result_contract"]["validation_inputs"]["blockers"])
            self.assertEqual(work_dir / "optimized_image.txt", result.optimized_image_path)

    def test_apply_stage_preserves_explicit_local_registry_destination_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            (work_dir / "estargz.list").write_text("/app/a.so\n", encoding="utf-8")
            env_payload = build_apply_env_payload()
            dst_image = "127.0.0.1:5002/abtest/ready-serving-layout-trap-v5:esgz-llm-manual-v1"

            def fake_run(argv, **kwargs):
                if argv[1].endswith("check_env.sh"):
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(env_payload, ensure_ascii=False, indent=2) + "\n",
                        "",
                    )
                if argv[1].endswith("apply_hints_to_image.sh"):
                    self.assertIn("--src", argv)
                    self.assertIn("127.0.0.1:5002/abtest/ready-serving-layout-trap-v5:src-raw", argv)
                    self.assertIn("--dst", argv)
                    self.assertIn(dst_image, argv)
                    (work_dir / "optimized_image.txt").write_text(f"{dst_image}\n", encoding="utf-8")
                    return subprocess.CompletedProcess(argv, 0, "", "")
                raise AssertionError(f"unexpected argv: {argv}")

            with mock.patch("orchestrator.stages.apply.subprocess.run", side_effect=fake_run):
                result = run_apply_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    src_image="127.0.0.1:5002/abtest/ready-serving-layout-trap-v5:src-raw",
                    dst_image=dst_image,
                    dry_run=False,
                )

            payload = json.loads(result.apply_result_path.read_text(encoding="utf-8"))
            self.assertEqual(dst_image, result.dst_image)
            self.assertEqual(dst_image, payload["dst_image"])
            self.assertEqual(dst_image, payload["optimized_image"])

    def test_apply_stage_keeps_passed_status_when_quality_is_advisory(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            (work_dir / "estargz.list").write_text("/app/a.so\n", encoding="utf-8")
            (work_dir / "quality_gate.json").write_text(
                json.dumps(
                    {
                        "ok": True,
                        "quality_status": "warning",
                        "recommendation": "benchmark_before_publish",
                        "warnings": ["candidate pool is small: 1"],
                        "reasons": [],
                        "blockers": [],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (work_dir / "selector_stats_hints.json").write_text(
                json.dumps(
                    {"budget_skipped": 1, "budget_bytes": 4096, "chosen_bytes": 4096},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (work_dir / "llm_grounding_audit.json").write_text(
                json.dumps(
                    {"accepted_total": 1, "invalid_path_skipped": 1, "special_path_skipped": 0},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            env_payload = build_apply_env_payload()

            def fake_run(argv, **kwargs):
                if argv[1].endswith("check_env.sh"):
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(env_payload, ensure_ascii=False, indent=2) + "\n",
                        "",
                    )
                if argv[1].endswith("apply_hints_to_image.sh"):
                    (work_dir / "optimized_image.txt").write_text(
                        "docker.io/library/app:latest-stargz\n",
                        encoding="utf-8",
                    )
                    (work_dir / "estargz.list.cleaned").write_text("/app/a.so\n", encoding="utf-8")
                    return subprocess.CompletedProcess(argv, 0, "", "")
                raise AssertionError(f"unexpected argv: {argv}")

            with mock.patch("orchestrator.stages.apply.subprocess.run", side_effect=fake_run):
                result = run_apply_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    src_image="docker.io/library/app:latest",
                    dry_run=False,
                )

            payload = json.loads(result.apply_result_path.read_text(encoding="utf-8"))
            self.assertEqual("passed", payload["status"])
            self.assertEqual("warning", payload["quality_status"])
            self.assertEqual("benchmark_before_publish", payload["recommendation"])
            self.assertEqual([], payload["blockers"])
            self.assertIn("candidate pool is small: 1", payload["warnings"])
            self.assertTrue(
                any("grounding discarded 1 illegal/special path" in item for item in payload["warnings"])
            )
            self.assertTrue(
                any("selector trimmed 1 lower-priority path" in item for item in payload["warnings"])
            )

    def test_apply_stage_chunk_shadow_uses_mode_specific_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            (work_dir / "estargz_chunk_shadow.list").write_text("/app/c.so\n", encoding="utf-8")
            env_payload = build_apply_env_payload()

            def fake_run(argv, **kwargs):
                if argv[1].endswith("check_env.sh"):
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(env_payload, ensure_ascii=False, indent=2) + "\n",
                        "",
                    )
                if argv[1].endswith("apply_hints_to_image.sh"):
                    self.assertIn("--selector-mode", argv)
                    self.assertIn("chunk-shadow", argv)
                    (work_dir / "optimized_image_chunk_shadow.txt").write_text(
                        "docker.io/library/app:latest-stargz-chunk-shadow\n",
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(argv, 0, "", "")
                raise AssertionError(f"unexpected argv: {argv}")

            with mock.patch("orchestrator.stages.apply.subprocess.run", side_effect=fake_run):
                result = run_apply_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    src_image="docker.io/library/app:latest",
                    selector_mode="chunk-shadow",
                    dry_run=False,
                )

            payload = json.loads(result.apply_result_path.read_text(encoding="utf-8"))
            self.assertEqual("chunk-shadow", result.selector_mode)
            self.assertEqual(work_dir / "optimized_image_chunk_shadow.txt", result.optimized_image_path)
            self.assertEqual(work_dir / "apply_result_chunk_shadow.json", result.apply_result_path)
            self.assertEqual(work_dir / "apply_chunk_shadow.log", result.stage_log_path)
            self.assertEqual("chunk-shadow", payload["selector_mode"])
            self.assertEqual(
                "docker.io/library/app:latest-stargz-chunk-shadow",
                payload["optimized_image"],
            )


if __name__ == "__main__":
    unittest.main()
