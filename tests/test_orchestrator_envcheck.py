import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.stages.envcheck import run_check_env_json, write_env_check_payload


ROOT = Path(__file__).resolve().parents[1]


class EnvCheckStageTests(unittest.TestCase):
    def test_run_check_env_json_parses_stdout_and_persists_payload(self):
        payload = {
            "schema_version": 1,
            "requested_profiles": ["oneclick"],
            "overall_status": "pass",
            "profiles": {"oneclick": {"status": "pass", "required_failures": [], "warnings": []}},
            "checks": [],
        }

        with mock.patch(
            "orchestrator.stages.envcheck.subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["bash", "check_env.sh"],
                0,
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                "",
            ),
        ):
            result = run_check_env_json(
                repo_root=ROOT,
                profile="oneclick",
                image="docker.io/library/app:latest",
                runtime_probes=False,
            )

        self.assertEqual("pass", result.payload["overall_status"])
        self.assertEqual(0, result.process.returncode)

        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "env_check.json"
            write_env_check_payload(out_path, result.payload)
            written = json.loads(out_path.read_text(encoding="utf-8"))

        self.assertEqual(payload, written)


if __name__ == "__main__":
    unittest.main()
