import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.stages.publish import run_publish_stage


ROOT = Path(__file__).resolve().parents[1]


class PublishStageTests(unittest.TestCase):
    def test_publish_stage_uses_insecure_registry_for_local_plain_http_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            commands = []

            def fake_run(argv, **kwargs):
                commands.append(list(argv))
                return subprocess.CompletedProcess(argv, 0, "", "")

            with mock.patch("orchestrator.stages.publish.subprocess.run", side_effect=fake_run):
                result = run_publish_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    src_image="docker.io/library/app:base",
                    dst_image="127.0.0.1:5001/abtest/app:base",
                )

            self.assertEqual(2, len(commands))
            self.assertEqual(
                ["nerdctl", "--namespace", "default", "tag", "docker.io/library/app:base", "127.0.0.1:5001/abtest/app:base"],
                commands[0],
            )
            self.assertEqual(
                ["nerdctl", "--namespace", "default", "--insecure-registry", "push", "127.0.0.1:5001/abtest/app:base"],
                commands[1],
            )
            self.assertTrue(result.plain_http)
            payload = json.loads(result.result_path.read_text(encoding="utf-8"))
            self.assertEqual("passed", payload["status"])
            self.assertEqual("docker.io/library/app:base", payload["src_image"])
            self.assertEqual("127.0.0.1:5001/abtest/app:base", payload["dst_image"])


if __name__ == "__main__":
    unittest.main()
