from pathlib import Path
import subprocess
import unittest
from unittest import mock

from orchestrator.runtime import choose_available_port, container_name_for, nerdctl


class RuntimeHelpersTests(unittest.TestCase):
    def test_choose_available_port_skips_occupied_port(self):
        class FakeSocket:
            def setsockopt(self, *_args):
                return None

            def bind(self, addr):
                if addr[1] == 18081:
                    raise OSError("busy")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        with mock.patch("orchestrator.runtime.socket.socket", side_effect=lambda *_args, **_kwargs: FakeSocket()):
            chosen = choose_available_port(18081, maxscan=4)
        self.assertNotEqual(18081, chosen)
        self.assertIn(chosen, {18082, 18083, 18084})

    def test_container_name_is_sanitized(self):
        name = container_name_for("docker.io/library/tensorflow/serving:latest", "20260317_123456")
        self.assertEqual("docker.io-library-tensorflow-serving-lat-20260317_123456", name)

    def test_nerdctl_forwards_text_flag(self):
        with mock.patch(
            "orchestrator.runtime.subprocess.run",
            return_value=subprocess.CompletedProcess(["nerdctl"], 0, b"", b""),
        ) as run_mock:
            nerdctl(
                Path("/repo"),
                ["logs", "demo"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
            )

        self.assertFalse(run_mock.call_args.kwargs["text"])


if __name__ == "__main__":
    unittest.main()
