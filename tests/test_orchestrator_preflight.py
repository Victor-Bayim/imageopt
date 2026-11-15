import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.manifest import ManifestStore
from orchestrator.policy import (
    evaluate_apply_policy,
    evaluate_benchmark_policy,
    evaluate_oneclick_policy,
)
from orchestrator.product_policy import enforced_supports_payload


def build_env_payload(overrides=None):
    checks = {
        "os_linux": "pass",
        "repo_layout": "pass",
        "privileged_user": "pass",
        "cmd_nerdctl": "pass",
        "cmd_ctr": "pass",
        "cmd_curl": "pass",
        "cmd_awk": "pass",
        "cmd_ctr-remote": "pass",
        "cmd_sed": "pass",
        "cmd_jq_benchmark": "pass",
        "cmd_python3": "pass",
        "cmd_ss": "pass",
        "sampler_binary": "pass",
        "kernel_btf": "pass",
        "nerdctl_runtime": "pass",
        "image_gated_startup_shell": "pass",
        "host_gated_startup_launcher": "pass",
        "host_gated_startup_launcher_static": "pass",
        "host_gated_startup_launcher_dynamic": "pass",
        "image_local": "pass",
        "snapshotter_plugin": "pass",
        "cni_bridge_plugin": "pass",
        "redis_ping": "warn",
        "ctr_remote_prefetch_list": "pass",
        "ctr_remote_runtime": "pass",
        "cmd_estargzify": "warn",
        "cmd_stargzify": "warn",
    }
    checks.update(overrides or {})
    return {
        "schema_version": 1,
        "requested_profiles": ["oneclick"],
        "profiles": {"oneclick": {"status": "pass", "required_failures": [], "warnings": []}},
        "checks": [
            {
                "id": check_id,
                "status": status,
                "summary": check_id,
                "required_for": ["oneclick"],
                "advisory_for": [],
                "detail": "",
                "value": None,
            }
            for check_id, status in checks.items()
        ],
    }


class ProductPolicyTests(unittest.TestCase):
    def test_legacy_policy_passes_with_llm_disabled(self):
        payload = evaluate_oneclick_policy(
            build_env_payload(),
            startup_mode="legacy",
            llm_enabled=False,
        )
        self.assertTrue(payload["ok"])
        self.assertEqual([], payload["warnings"])
        self.assertEqual(enforced_supports_payload(), payload["enforced_supports"])

    def test_llm_enabled_without_key_warns_but_does_not_fail(self):
        with mock.patch.dict(
            os.environ,
            {
                "LLMAPIKEY": "",
                "LLM_API_KEY": "",
                "llmapikey": "",
                "llm_api_key": "",
            },
            clear=False,
        ):
            payload = evaluate_oneclick_policy(
                build_env_payload(),
                startup_mode="legacy",
                llm_enabled=True,
            )
        self.assertTrue(payload["ok"])
        self.assertTrue(any("web-LLM fallback" in item for item in payload["warnings"]))

    def test_gated_policy_rejects_missing_shell(self):
        payload = evaluate_oneclick_policy(
            build_env_payload(
                {
                    "image_gated_startup_shell": "warn",
                    "host_gated_startup_launcher": "warn",
                    "host_gated_startup_launcher_static": "warn",
                    "host_gated_startup_launcher_dynamic": "warn",
                }
            ),
            startup_mode="gated",
            llm_enabled=False,
        )
        self.assertFalse(payload["ok"])
        self.assertIn("gated startup requires either /bin/sh", payload["errors"][0])

    def test_gated_policy_allows_host_launcher_backend_when_shell_is_unavailable(self):
        payload = evaluate_oneclick_policy(
            build_env_payload({"image_gated_startup_shell": "warn"}),
            startup_mode="gated",
            llm_enabled=False,
        )
        self.assertTrue(payload["ok"])
        self.assertTrue(any("host launcher backend" in item for item in payload["warnings"]))

    def test_gated_policy_can_prefer_host_launcher_before_shell(self):
        payload = evaluate_oneclick_policy(
            build_env_payload(),
            startup_mode="gated",
            startup_backend_preference="prefer-host-launcher",
            llm_enabled=False,
        )
        self.assertTrue(payload["ok"])
        self.assertEqual("prefer-host-launcher", payload["startup_backend_preference"])
        self.assertTrue(any("prefer the host launcher backend" in item for item in payload["warnings"]))

    def test_gated_policy_warns_when_preferred_host_launcher_is_unavailable_but_shell_exists(self):
        payload = evaluate_oneclick_policy(
            build_env_payload(
                {
                    "host_gated_startup_launcher": "warn",
                    "host_gated_startup_launcher_static": "warn",
                    "host_gated_startup_launcher_dynamic": "warn",
                }
            ),
            startup_mode="gated",
            startup_backend_preference="prefer-host-launcher",
            llm_enabled=False,
        )
        self.assertTrue(payload["ok"])
        self.assertTrue(any("fall back to image shell" in item for item in payload["warnings"]))

    def test_gated_policy_can_prefer_image_shell_when_available(self):
        payload = evaluate_oneclick_policy(
            build_env_payload(),
            startup_mode="gated",
            startup_backend_preference="prefer-image-shell",
            llm_enabled=False,
        )
        self.assertTrue(payload["ok"])
        self.assertEqual("prefer-image-shell", payload["startup_backend_preference"])
        self.assertEqual([], payload["warnings"])

    def test_gated_policy_warns_when_preferred_image_shell_is_unavailable(self):
        payload = evaluate_oneclick_policy(
            build_env_payload({"image_gated_startup_shell": "warn", "host_gated_startup_launcher_static": "pass"}),
            startup_mode="gated",
            startup_backend_preference="prefer-image-shell",
            llm_enabled=False,
        )
        self.assertTrue(payload["ok"])
        self.assertTrue(any("fall back to the host launcher backend" in item for item in payload["warnings"]))

    def test_gated_policy_warns_when_only_dynamic_host_launcher_is_available(self):
        payload = evaluate_oneclick_policy(
            build_env_payload(
                {
                    "image_gated_startup_shell": "warn",
                    "host_gated_startup_launcher_static": "warn",
                    "host_gated_startup_launcher_dynamic": "pass",
                }
            ),
            startup_mode="gated",
            llm_enabled=False,
        )
        self.assertTrue(payload["ok"])
        self.assertTrue(any("experimental dynamic host launcher backend" in item for item in payload["warnings"]))

    def test_apply_policy_allows_fallback_toolchain_in_dry_run(self):
        payload = evaluate_apply_policy(
            build_env_payload(
                {
                    "image_local": "warn",
                    "ctr_remote_prefetch_list": "warn",
                    "ctr_remote_runtime": "warn",
                    "cmd_estargzify": "pass",
                }
            ),
            dry_run=True,
        )
        self.assertTrue(payload["ok"])
        self.assertTrue(any("dry-run" in item for item in payload["warnings"]))
        self.assertTrue(any("fallback" in item for item in payload["warnings"]))

    def test_apply_policy_requires_local_image_for_real_apply(self):
        payload = evaluate_apply_policy(
            build_env_payload({"image_local": "warn"}),
            dry_run=False,
        )
        self.assertFalse(payload["ok"])
        self.assertTrue(any("source image" in item for item in payload["errors"]))

    def test_benchmark_policy_requires_both_images_and_runtime_checks(self):
        payload = evaluate_benchmark_policy(
            build_env_payload(),
            build_env_payload({"image_local": "warn"}),
        )
        self.assertFalse(payload["ok"])
        self.assertTrue(any(item.startswith("optimized:") for item in payload["errors"]))

    def test_benchmark_policy_warns_when_cni_bridge_plugin_is_missing(self):
        payload = evaluate_benchmark_policy(
            build_env_payload({"cni_bridge_plugin": "warn"}),
            build_env_payload(),
        )
        self.assertTrue(payload["ok"])
        self.assertTrue(any("CNI bridge plugin is unavailable" in item for item in payload["warnings"]))

    def test_benchmark_policy_requires_ctr_for_strict_content_purge(self):
        payload = evaluate_benchmark_policy(
            build_env_payload({"cmd_ctr": "warn", "image_local": "warn"}),
            build_env_payload({"image_local": "warn"}),
            require_local_image=False,
            require_content_store_purge=True,
        )
        self.assertFalse(payload["ok"])
        self.assertTrue(any("content store purge requires ctr" in item for item in payload["errors"]))


class ManifestStoreTests(unittest.TestCase):
    def test_manifest_tracks_stage_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            manifest_path = work_dir / "manifest.json"
            store = ManifestStore.create(
                manifest_path,
                run_id="20260317_000000",
                work_dir=work_dir,
                image="docker.io/example/image:latest",
                startup_mode="legacy",
                llm_enabled=False,
            )
            store.stage_begin("preflight", "Validate fixed support boundary")
            store.stage_finish(
                "preflight",
                status="passed",
                summary="Preflight accepted",
                artifacts={"env_check": str(work_dir / "env_check.json")},
                warnings=["deterministic mode only"],
            )

            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(4, payload["manifest_version"])
            self.assertEqual("ready", payload["status"])
            self.assertEqual("python-orchestrator", payload["control_plane"]["engine"])
            self.assertEqual(enforced_supports_payload(), payload["product_policy"])
            self.assertEqual("passed", payload["stages"]["preflight"]["status"])
            self.assertIn("env_check", payload["artifacts"])

    def test_stage_rerun_clears_stale_error_and_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            manifest_path = work_dir / "manifest.json"
            store = ManifestStore.create(
                manifest_path,
                run_id="20260317_000001",
                work_dir=work_dir,
                image="docker.io/example/image:latest",
                startup_mode="legacy",
                llm_enabled=False,
            )
            store.stage_begin("apply", "apply begin")
            store.stage_finish(
                "apply",
                status="failed",
                summary="apply failed",
                warnings=["warn-before"],
                error="boom",
            )
            store.stage_begin("apply", "apply retry")
            store.stage_finish(
                "apply",
                status="passed",
                summary="apply ok",
            )

            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual("passed", payload["stages"]["apply"]["status"])
            self.assertNotIn("warnings", payload["stages"]["apply"])
            self.assertNotIn("error", payload["stages"]["apply"])


if __name__ == "__main__":
    unittest.main()
