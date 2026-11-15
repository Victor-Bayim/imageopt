import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "oneclick_product.py"


def _runtime_requested() -> bool:
    if os.environ.get("CPP_SAMPLER_RUNTIME_E2E") == "1":
        return True
    return any(
        str(os.environ.get(name, "")).strip()
        for name in (
            "CPP_SAMPLER_RUNTIME_IMAGE_SHELL",
            "CPP_SAMPLER_RUNTIME_IMAGE",
            "CPP_SAMPLER_RUNTIME_IMAGE_DISTROLESS",
        )
    )


class RuntimeIntegrationEnvTests(unittest.TestCase):
    def test_runtime_requested_when_explicit_flag_is_set(self):
        with mock.patch.dict(os.environ, {"CPP_SAMPLER_RUNTIME_E2E": "1"}, clear=False):
            self.assertTrue(_runtime_requested())

    def test_runtime_requested_when_runtime_images_are_configured(self):
        with mock.patch.dict(
            os.environ,
            {
                "CPP_SAMPLER_RUNTIME_E2E": "",
                "CPP_SAMPLER_RUNTIME_IMAGE_SHELL": "docker.io/library/app:latest",
                "CPP_SAMPLER_RUNTIME_IMAGE_DISTROLESS": "docker.io/library/distroless-app:latest",
            },
            clear=False,
        ):
            self.assertTrue(_runtime_requested())


class RuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        if not _runtime_requested():
            self.skipTest(
                "set CPP_SAMPLER_RUNTIME_E2E=1 or configure CPP_SAMPLER_RUNTIME_IMAGE_SHELL/"
                "CPP_SAMPLER_RUNTIME_IMAGE_DISTROLESS to run runtime integration tests"
            )
        if shutil.which("sudo") is None:
            self.skipTest("sudo is required for runtime integration tests")

    def _runtime_image(self, kind: str) -> str:
        if kind == "shell":
            return (
                os.environ.get("CPP_SAMPLER_RUNTIME_IMAGE_SHELL", "").strip()
                or os.environ.get("CPP_SAMPLER_RUNTIME_IMAGE", "").strip()
            )
        if kind == "distroless":
            return os.environ.get("CPP_SAMPLER_RUNTIME_IMAGE_DISTROLESS", "").strip()
        return ""

    def _run_pipeline(self, image: str) -> tuple[dict, dict]:
        with tempfile.TemporaryDirectory(prefix="cpp_sampler_runtime_") as tmp:
            work_dir = Path(tmp)
            proc = subprocess.run(
                [
                    "sudo",
                    "-E",
                    "python3",
                    str(CLI),
                    "run",
                    image,
                    "--work-dir",
                    str(work_dir),
                    "--mode",
                    "deterministic",
                    "--startup-mode",
                    "auto",
                    "--duration",
                    "1",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stdout + proc.stderr)
            manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            report = json.loads((work_dir / "run_report.json").read_text(encoding="utf-8"))
            return manifest, report

    def test_run_pipeline_produces_manifest_and_report_for_shell_image(self):
        image = self._runtime_image("shell")
        if not image:
            self.skipTest("set CPP_SAMPLER_RUNTIME_IMAGE_SHELL or CPP_SAMPLER_RUNTIME_IMAGE to a locally available long-running shell image")

        manifest, report = self._run_pipeline(image)
        self.assertEqual("passed", manifest["stages"]["preflight"]["status"])
        self.assertEqual("passed", manifest["stages"]["sample"]["status"])
        self.assertEqual("passed", manifest["stages"]["report"]["status"])
        self.assertEqual("gated", report["sample"]["startup_mode_used"])
        self.assertIn(report["sample"]["startup_backend"], {"gated_shell", "gated_host_launcher_static"})
        self.assertIn(report["overall_status"], {"passed", "degraded"})

    def test_run_pipeline_produces_manifest_and_report_for_distroless_image(self):
        image = self._runtime_image("distroless")
        if not image:
            self.skipTest("set CPP_SAMPLER_RUNTIME_IMAGE_DISTROLESS to a locally available long-running distroless/minimal image")

        manifest, report = self._run_pipeline(image)
        self.assertEqual("passed", manifest["stages"]["preflight"]["status"])
        self.assertEqual("passed", manifest["stages"]["sample"]["status"])
        self.assertEqual("passed", manifest["stages"]["report"]["status"])
        self.assertEqual("gated", report["sample"]["startup_mode_used"])
        self.assertIn(report["sample"]["startup_backend"], {"gated_host_launcher_static", "gated_host_launcher_dynamic"})
        self.assertIn(report["overall_status"], {"passed", "degraded"})


if __name__ == "__main__":
    unittest.main()
