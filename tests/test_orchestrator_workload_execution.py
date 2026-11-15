from dataclasses import dataclass
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from orchestrator.workloads import (
    ExecReadyHook,
    ExecInjectHook,
    HttpPredictHook,
    HttpReadyHook,
    LogRegexReadyHook,
    ModelMountHook,
    TcpReadyHook,
    benchmark_hook_parameters,
    register_benchmark_arg_builder,
    register_inject_handler,
    register_predict_handler,
    register_ready_handler,
    run_injection_hooks,
    wait_for_ready_hook,
)
from orchestrator.workloads import execution as workload_execution


@dataclass(frozen=True)
class CustomReadyHook:
    marker: str


@dataclass(frozen=True)
class CustomPredictHook:
    marker: str


@dataclass(frozen=True)
class CustomInjectHook:
    marker: str


class WorkloadExecutionTests(unittest.TestCase):
    def test_wait_for_ready_hook_retries_http_strategy(self):
        with mock.patch(
            "orchestrator.workloads.execution._http_get",
            side_effect=[RuntimeError("not ready"), b"ok"],
        ) as http_get:
            with mock.patch("orchestrator.workloads.execution.time.sleep") as sleep:
                observed = wait_for_ready_hook(HttpReadyHook("http://127.0.0.1:8080/readyz"), timeout_sec=2)

        self.assertTrue(observed)
        self.assertEqual(2, http_get.call_count)
        sleep.assert_called_once_with(1)

    def test_benchmark_hook_parameters_derive_shell_flags_from_hooks(self):
        payload = benchmark_hook_parameters(
            ready_hook=HttpReadyHook("http://127.0.0.1:9000/ready"),
            predict_hook=HttpPredictHook("http://127.0.0.1:9000/predict", '{"instances":[5.0]}', attempts=4),
            model_mount_hook=ModelMountHook("/models/demo", "demo-model"),
            model_host_dir="/srv/models",
        )

        self.assertEqual("http://127.0.0.1:9000/ready", payload["ready_url"])
        self.assertEqual("http://127.0.0.1:9000/predict", payload["predict_url"])
        self.assertEqual('{"instances":[5.0]}', payload["predict_body"])
        self.assertEqual("/srv/models", payload["model_host_dir"])
        self.assertEqual("/models/demo", payload["model_dst_dir"])
        self.assertEqual("demo-model", payload["model_name"])

    def test_benchmark_hook_parameters_support_tcp_ready_probe(self):
        payload = benchmark_hook_parameters(
            ready_hook=TcpReadyHook("127.0.0.1", 8888),
            predict_hook=None,
            model_mount_hook=None,
        )

        self.assertEqual("tcp://127.0.0.1:8888", payload["ready_url"])
        self.assertEqual("", payload["predict_url"])
        self.assertEqual("", payload["model_dst_dir"])

    def test_custom_handlers_can_be_registered_for_new_hook_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)

            def custom_predict_handler(hook, out_fh):
                out_fh.write(f"{hook.marker}\n")
                return {"predict_attempts": 1, "predict_successes": 1}

            with mock.patch.dict(workload_execution._READY_HANDLERS, {}, clear=True):
                with mock.patch.dict(workload_execution._PREDICT_HANDLERS, {}, clear=True):
                    with mock.patch.dict(workload_execution._INJECT_HANDLERS, {}, clear=True):
                        with mock.patch.dict(workload_execution._BENCHMARK_ARG_BUILDERS, {}, clear=True):
                            register_ready_handler(
                                CustomReadyHook,
                                lambda hook, ready_ctx: hook.marker == f"ready:{ready_ctx.timeout_sec}",
                            )
                            register_predict_handler(CustomPredictHook, custom_predict_handler)
                            register_inject_handler(
                                CustomInjectHook,
                                lambda repo_root, container_name, hook: hook.marker == container_name,
                            )
                            register_benchmark_arg_builder(
                                CustomReadyHook,
                                lambda hook: {"ready_url": f"custom://{hook.marker}"},
                            )

                            observed = wait_for_ready_hook(CustomReadyHook("ready:7"), timeout_sec=7)
                            injection = run_injection_hooks(
                                repo_root=work_dir,
                                work_dir=work_dir,
                                container_name="demo",
                                predict_hook=CustomPredictHook("predict-ok"),
                                inject_hook=CustomInjectHook("demo"),
                            )
                            bench_payload = benchmark_hook_parameters(
                                ready_hook=CustomReadyHook("bench"),
                                predict_hook=None,
                                model_mount_hook=None,
                            )

            self.assertTrue(observed)
            self.assertEqual(1, injection["predict_attempts"])
            self.assertEqual(1, injection["predict_successes"])
            self.assertTrue(injection["exec_inject_used"])
            self.assertEqual("custom://bench", bench_payload["ready_url"])
            self.assertIn("predict-ok", (work_dir / "inference.jsonl").read_text(encoding="utf-8"))

    def test_wait_for_ready_hook_supports_tcp_strategy(self):
        conn = mock.MagicMock()
        conn.__enter__.return_value = conn
        conn.__exit__.return_value = False
        with mock.patch(
            "orchestrator.workloads.execution.socket.create_connection",
            side_effect=[OSError("not ready"), conn],
        ) as create_connection:
            with mock.patch("orchestrator.workloads.execution.time.sleep") as sleep:
                observed = wait_for_ready_hook(TcpReadyHook("127.0.0.1", 9999), timeout_sec=2)

        self.assertTrue(observed)
        self.assertEqual(2, create_connection.call_count)
        sleep.assert_called_once_with(1)

    def test_benchmark_hook_parameters_support_exec_and_log_ready_probe(self):
        exec_payload = benchmark_hook_parameters(
            ready_hook=ExecReadyHook("test -f /tmp/ready", shell="bash"),
            predict_hook=None,
            model_mount_hook=None,
        )
        log_payload = benchmark_hook_parameters(
            ready_hook=LogRegexReadyHook("Server listening on .*"),
            predict_hook=None,
            model_mount_hook=None,
        )

        self.assertEqual("test -f /tmp/ready", exec_payload["ready_exec"])
        self.assertEqual("bash", exec_payload["ready_shell"])
        self.assertEqual("Server listening on .*", log_payload["ready_log_regex"])

    def test_wait_for_ready_hook_supports_exec_strategy(self):
        repo_root = Path("/repo")
        with mock.patch(
            "orchestrator.workloads.execution.nerdctl",
            side_effect=[
                mock.Mock(returncode=1),
                mock.Mock(returncode=0),
            ],
        ) as nerdctl_mock:
            with mock.patch("orchestrator.workloads.execution.time.sleep") as sleep:
                observed = wait_for_ready_hook(
                    ExecReadyHook("test -f /tmp/ready"),
                    timeout_sec=2,
                    repo_root=repo_root,
                    container_name="demo",
                )

        self.assertTrue(observed)
        self.assertEqual(2, nerdctl_mock.call_count)
        sleep.assert_called_once_with(1)

    def test_run_injection_hooks_uses_non_login_shell(self):
        repo_root = Path("/repo")
        work_dir = Path("/tmp/work")
        with mock.patch(
            "orchestrator.workloads.execution.nerdctl",
            return_value=mock.Mock(returncode=0),
        ) as nerdctl_mock:
            with mock.patch.object(Path, "write_text", return_value=0):
                run_injection_hooks(
                    repo_root=repo_root,
                    work_dir=work_dir,
                    container_name="demo",
                    predict_hook=None,
                    inject_hook=ExecInjectHook("echo hello", shell="bash"),
                )

        argv = nerdctl_mock.call_args.args[1]
        self.assertEqual(["exec", "demo", "bash", "-c", "echo hello"], argv)

    def test_wait_for_ready_hook_supports_log_regex_strategy(self):
        repo_root = Path("/repo")
        with mock.patch(
            "orchestrator.workloads.execution.nerdctl",
            side_effect=[
                mock.Mock(stdout="booting..."),
                mock.Mock(stdout="Server listening on 0.0.0.0:8888"),
            ],
        ) as nerdctl_mock:
            with mock.patch("orchestrator.workloads.execution.time.sleep") as sleep:
                observed = wait_for_ready_hook(
                    LogRegexReadyHook(r"Server listening on .*:8888"),
                    timeout_sec=2,
                    repo_root=repo_root,
                    container_name="demo",
                )

        self.assertTrue(observed)
        self.assertEqual(2, nerdctl_mock.call_count)
        sleep.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
