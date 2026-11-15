import os
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CheckEnvScriptTest(unittest.TestCase):
    def _run_check_env(self, *args: str, env_overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            env = dict(os.environ)
            env["TMPDIR"] = tmp
            if env_overrides:
                env.update(env_overrides)
            return subprocess.run(
                [
                    "bash",
                    "scripts/check_env.sh",
                    *args,
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

    def test_check_env_json_shape(self):
        proc = self._run_check_env(
            "--json",
            "--no-runtime-probes",
            "--profile",
            "oneclick",
        )

        self.assertIn(proc.returncode, (0, 1), proc.stderr)
        self.assertEqual("", proc.stderr)

        payload = json.loads(proc.stdout)
        self.assertEqual(1, payload["schema_version"])
        self.assertEqual(["oneclick"], payload["requested_profiles"])
        self.assertIn(payload["overall_status"], {"pass", "warn", "fail"})
        self.assertIn("oneclick", payload["profiles"])
        self.assertIn("checks", payload)

        check_ids = {item["id"] for item in payload["checks"]}
        self.assertIn("os_linux", check_ids)
        self.assertIn("sampler_binary", check_ids)
        self.assertIn("privileged_user", check_ids)
        self.assertIn("cni_bridge_plugin", check_ids)
        self.assertIn("host_gated_startup_launcher", check_ids)
        self.assertIn("host_gated_startup_launcher_static", check_ids)
        self.assertIn("host_gated_startup_launcher_dynamic", check_ids)

    def test_check_env_no_runtime_probes_marks_runtime_dependent_checks_as_warn(self):
        proc = self._run_check_env(
            "--json",
            "--no-runtime-probes",
            "--profile",
            "oneclick",
            "--image",
            "docker.io/library/alpine:latest",
        )

        self.assertIn(proc.returncode, (0, 1), proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertFalse(payload["user"]["runtime_probes_enabled"])

        checks = {item["id"]: item for item in payload["checks"]}
        self.assertEqual("warn", checks["nerdctl_runtime"]["status"])
        self.assertEqual("warn", checks["snapshotter_plugin"]["status"])
        self.assertEqual("warn", checks["cni_bridge_plugin"]["status"])
        self.assertEqual("warn", checks["image_local"]["status"])
        self.assertEqual("warn", checks["image_gated_startup_shell"]["status"])

        oneclick_state = payload["profiles"]["oneclick"]
        self.assertNotIn("nerdctl_runtime", oneclick_state["required_failures"])
        self.assertNotIn("snapshotter_plugin", oneclick_state["required_failures"])
        self.assertIn("snapshotter_plugin", oneclick_state["warnings"])

    def test_check_env_benchmark_profile_marks_ctr_as_benchmark_advisory(self):
        proc = self._run_check_env(
            "--json",
            "--no-runtime-probes",
            "--profile",
            "benchmark",
        )

        self.assertIn(proc.returncode, (0, 1), proc.stderr)
        payload = json.loads(proc.stdout)
        checks = {item["id"]: item for item in payload["checks"]}
        self.assertIn("cmd_ctr", checks)
        self.assertIn("benchmark", checks["cmd_ctr"]["advisory_for"])

    def test_check_env_honors_cpp_sampler_cc_for_host_launcher_probe(self):
        proc = self._run_check_env(
            "--json",
            "--no-runtime-probes",
            "--profile",
            "oneclick",
            env_overrides={"CPP_SAMPLER_CC": "definitely-missing-cc"},
        )

        self.assertIn(proc.returncode, (0, 1), proc.stderr)
        payload = json.loads(proc.stdout)
        checks = {item["id"]: item for item in payload["checks"]}
        self.assertEqual("warn", checks["host_gated_startup_launcher_static"]["status"])
        self.assertEqual("warn", checks["host_gated_startup_launcher_dynamic"]["status"])
        self.assertEqual("warn", checks["host_gated_startup_launcher"]["status"])
        self.assertIn(
            "requested compiler via CPP_SAMPLER_CC is unavailable: definitely-missing-cc",
            checks["host_gated_startup_launcher_static"]["detail"],
        )
        self.assertEqual("definitely-missing-cc", checks["host_gated_startup_launcher"]["value"])


if __name__ == "__main__":
    unittest.main()
