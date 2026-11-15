import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.container_runtime import container_runtime_spec
from orchestrator.stages.sample import (
    _build_startup_gate_launcher,
    _local_ready_hook_port,
    _launch_container,
    run_sample_stage,
    run_workload_injection,
)
from orchestrator.workloads.hooks import ExecReadyHook, HttpReadyHook, TcpReadyHook


ROOT = Path(__file__).resolve().parents[1]


class SampleInjectionTests(unittest.TestCase):
    def test_run_workload_injection_uses_http_and_exec_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            with mock.patch(
                "orchestrator.workloads.execution._http_post",
                side_effect=[b'{"ok":1}', b'{"ok":2}', b'{"ok":3}'],
            ) as http_post:
                with mock.patch(
                    "orchestrator.workloads.execution.nerdctl",
                    return_value=subprocess.CompletedProcess(["nerdctl"], 0, "", ""),
                ) as nerdctl:
                    payload = run_workload_injection(
                        repo_root=ROOT,
                        work_dir=work_dir,
                        container_name="demo",
                        predict_url="http://127.0.0.1:9000/predict",
                        predict_body='{"instances":[1.0]}',
                        inject_io_cmd="echo hi >/tmp/x",
                    )

            self.assertTrue(payload["did_io"])
            self.assertEqual(3, payload["predict_attempts"])
            self.assertEqual(3, payload["predict_successes"])
            self.assertTrue(payload["exec_inject_used"])
            self.assertEqual(3, http_post.call_count)
            nerdctl.assert_called_once()
            inference_log = (work_dir / "inference.jsonl").read_text(encoding="utf-8")
            self.assertIn('{"ok":1}', inference_log)


class SamplePortSelectionTests(unittest.TestCase):
    def test_local_ready_hook_port_extracts_loopback_http_and_tcp_ports(self):
        self.assertEqual(8000, _local_ready_hook_port(HttpReadyHook("http://127.0.0.1:8000/ready")))
        self.assertEqual(8001, _local_ready_hook_port(TcpReadyHook("localhost", 8001)))
        self.assertIsNone(_local_ready_hook_port(HttpReadyHook("http://198.51.100.10:8000/ready")))


class SampleStageLifecycleTests(unittest.TestCase):
    def test_build_startup_gate_launcher_prefers_cpp_sampler_cc(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)

            def fake_run(argv, **kwargs):
                (work_dir / "startup_gate_launcher").write_text("launcher", encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, "", "")

            with mock.patch.dict(os.environ, {"CPP_SAMPLER_CC": "custom-cc"}, clear=False):
                with mock.patch(
                    "orchestrator.stages.sample.shutil.which",
                    side_effect=lambda name: "/opt/toolchain/custom-cc" if name == "custom-cc" else None,
                ):
                    with mock.patch(
                        "orchestrator.stages.sample.subprocess.run",
                        side_effect=fake_run,
                    ) as run_mock:
                        launcher_path, backend = _build_startup_gate_launcher(
                            repo_root=ROOT,
                            work_dir=work_dir,
                            allow_dynamic=False,
                        )

        self.assertEqual(work_dir / "startup_gate_launcher", launcher_path)
        self.assertEqual("gated_host_launcher_static", backend)
        self.assertEqual("/opt/toolchain/custom-cc", run_mock.call_args.args[0][0])

    def test_build_startup_gate_launcher_fails_when_cpp_sampler_cc_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            with mock.patch.dict(os.environ, {"CPP_SAMPLER_CC": "missing-cc"}, clear=False):
                with mock.patch("orchestrator.stages.sample.shutil.which", return_value=None):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "requested host C compiler via CPP_SAMPLER_CC is unavailable: missing-cc",
                    ):
                        _build_startup_gate_launcher(
                            repo_root=ROOT,
                            work_dir=work_dir,
                        )

    def test_launch_container_surfaces_nerdctl_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            with mock.patch(
                "orchestrator.stages.sample.nerdctl",
                side_effect=[
                    subprocess.CompletedProcess(["nerdctl"], 0, "", ""),
                    subprocess.CompletedProcess(
                        ["nerdctl"],
                        1,
                        "",
                        "listen tcp 0.0.0.0:18000: bind: address already in use\n",
                    ),
                ],
            ):
                with self.assertRaisesRegex(RuntimeError, "address already in use"):
                    _launch_container(
                        repo_root=ROOT,
                        image="vllm/vllm-openai:latest",
                        run_id="20260319_010101",
                        work_dir=work_dir,
                        startup_mode="legacy",
                        runtime_spec=container_runtime_spec(
                            gpus="all",
                            ipc="host",
                            command=("TinyLlama/TinyLlama-1.1B-Chat-v1.0", "--port", "18000"),
                        ),
                    )

    def test_launch_container_retries_with_fallback_name_on_name_store_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            with mock.patch("orchestrator.stages.sample.time.time", return_value=1234567890):
                with mock.patch(
                    "orchestrator.stages.sample.nerdctl",
                    side_effect=[
                        subprocess.CompletedProcess(["nerdctl"], 0, "", ""),
                        subprocess.CompletedProcess(
                            ["nerdctl"],
                            1,
                            "",
                            'time="..." level=fatal msg="name-store error\\nname \\"demo\\" is already used by ID \\"abc\\""',
                        ),
                        subprocess.CompletedProcess(["nerdctl"], 0, "", ""),
                        subprocess.CompletedProcess(["nerdctl"], 0, "", ""),
                    ],
                ) as nerdctl_mock:
                    with mock.patch(
                        "orchestrator.stages.sample.inspect_container_pid",
                        return_value=4321,
                    ):
                        container_name, pid, mode_used, warnings, startup_backend, selection_reason = _launch_container(
                            repo_root=ROOT,
                            image="vllm/vllm-openai:latest",
                            run_id="demo",
                            work_dir=work_dir,
                            startup_mode="legacy",
                            runtime_spec=container_runtime_spec(
                                gpus="all",
                                command=("TinyLlama/TinyLlama-1.1B-Chat-v1.0",),
                            ),
                        )

        self.assertEqual(4321, pid)
        self.assertEqual("legacy", mode_used)
        self.assertEqual("legacy", startup_backend)
        self.assertEqual("legacy startup mode requested", selection_reason)
        self.assertIn("-r1234567890", container_name)
        self.assertTrue(any("name conflict" in warning for warning in warnings))
        run_calls = [call for call in nerdctl_mock.call_args_list if call.args[1][:1] == ["run"]]
        self.assertEqual(2, len(run_calls))
        self.assertNotEqual(run_calls[0].args[1][3], run_calls[1].args[1][3])

    def test_launch_container_uses_host_launcher_when_shell_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            launcher_path = work_dir / "startup_gate_launcher"
            launcher_path.write_text("", encoding="utf-8")
            with mock.patch(
                "orchestrator.stages.sample.nerdctl",
                side_effect=[
                    subprocess.CompletedProcess(["nerdctl"], 0, "", ""),
                    subprocess.CompletedProcess(["nerdctl"], 0, "", ""),
                    subprocess.CompletedProcess(["nerdctl"], 0, "", ""),
                ],
            ) as nerdctl_mock:
                with mock.patch(
                    "orchestrator.stages.sample.image_supports_shell",
                    return_value=False,
                ):
                    with mock.patch(
                        "orchestrator.stages.sample._effective_container_argv",
                        return_value=["/app/server", "--port", "18000"],
                    ):
                        with mock.patch(
                            "orchestrator.stages.sample._build_startup_gate_launcher",
                            return_value=(launcher_path, "gated_host_launcher_static"),
                        ):
                            with mock.patch(
                                "orchestrator.stages.sample.inspect_container_pid",
                                return_value=4325,
                            ):
                                container_name, pid, mode_used, warnings, startup_backend, selection_reason = _launch_container(
                                    repo_root=ROOT,
                                    image="docker.io/library/distroless-app:latest",
                                    run_id="20260323_010101",
                                    work_dir=work_dir,
                                    startup_mode="gated",
                                    runtime_spec=container_runtime_spec(
                                        env=("HF_HUB_OFFLINE=1",),
                                        command=("--port", "18000"),
                                    ),
                                )

        self.assertEqual("gated", mode_used)
        self.assertEqual("gated_host_launcher_static", startup_backend)
        self.assertEqual(4325, pid)
        self.assertEqual("create", nerdctl_mock.call_args_list[1].args[1][0])
        self.assertIn("/__cpp_sampler_gate_launcher", nerdctl_mock.call_args_list[1].args[1])
        self.assertIn("/__cpp_sampler_gate/release", nerdctl_mock.call_args_list[1].args[1])
        self.assertIn("/app/server", nerdctl_mock.call_args_list[1].args[1])
        self.assertTrue(any("host launcher backend" in item for item in warnings))
        self.assertEqual("image lacks /bin/sh; host launcher is the only gated startup backend", selection_reason)
        self.assertIn(container_name, nerdctl_mock.call_args_list[1].args[1])

    def test_launch_container_auto_prefers_static_host_launcher_before_shell_when_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            launcher_path = work_dir / "startup_gate_launcher"
            launcher_path.write_text("", encoding="utf-8")
            with mock.patch(
                "orchestrator.stages.sample.nerdctl",
                side_effect=[
                    subprocess.CompletedProcess(["nerdctl"], 0, "", ""),
                    subprocess.CompletedProcess(["nerdctl"], 0, "", ""),
                    subprocess.CompletedProcess(["nerdctl"], 0, "", ""),
                ],
            ) as nerdctl_mock:
                with mock.patch("orchestrator.stages.sample.image_supports_shell", return_value=True):
                    with mock.patch(
                        "orchestrator.stages.sample._effective_container_argv",
                        return_value=["/app/server", "--port", "18000"],
                    ):
                        with mock.patch(
                            "orchestrator.stages.sample._build_startup_gate_launcher",
                            return_value=(launcher_path, "gated_host_launcher_static"),
                        ):
                            with mock.patch(
                                "orchestrator.stages.sample.inspect_container_pid",
                                return_value=4330,
                            ):
                                _container_name, pid, mode_used, warnings, startup_backend, selection_reason = _launch_container(
                                    repo_root=ROOT,
                                    image="docker.io/library/app:latest",
                                    run_id="20260325_020202",
                                    work_dir=work_dir,
                                    startup_mode="gated",
                                    runtime_spec=container_runtime_spec(command=("--port", "18000")),
                                )

        self.assertEqual(4330, pid)
        self.assertEqual("gated", mode_used)
        self.assertEqual("gated_host_launcher_static", startup_backend)
        self.assertEqual("create", nerdctl_mock.call_args_list[1].args[1][0])
        self.assertIn("/__cpp_sampler_gate_launcher", nerdctl_mock.call_args_list[1].args[1])
        self.assertTrue(any("auto-selected host launcher backend" in item for item in warnings))
        self.assertEqual("auto selected static host launcher before image-shell gating", selection_reason)

    def test_launch_container_prefers_host_launcher_when_requested_even_if_shell_is_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            launcher_path = work_dir / "startup_gate_launcher"
            launcher_path.write_text("", encoding="utf-8")
            with mock.patch(
                "orchestrator.stages.sample.nerdctl",
                side_effect=[
                    subprocess.CompletedProcess(["nerdctl"], 0, "", ""),
                    subprocess.CompletedProcess(["nerdctl"], 0, "", ""),
                    subprocess.CompletedProcess(["nerdctl"], 0, "", ""),
                ],
            ) as nerdctl_mock:
                with mock.patch("orchestrator.stages.sample.image_supports_shell", return_value=True):
                    with mock.patch(
                        "orchestrator.stages.sample._effective_container_argv",
                        return_value=["/app/server", "--port", "18000"],
                    ):
                        with mock.patch(
                            "orchestrator.stages.sample._build_startup_gate_launcher",
                            return_value=(launcher_path, "gated_host_launcher_static"),
                        ):
                            with mock.patch(
                                "orchestrator.stages.sample.inspect_container_pid",
                                return_value=4330,
                            ):
                                _container_name, pid, mode_used, warnings, startup_backend, selection_reason = _launch_container(
                                    repo_root=ROOT,
                                    image="docker.io/library/app:latest",
                                    run_id="20260325_020202",
                                    work_dir=work_dir,
                                    startup_mode="gated",
                                    startup_backend_preference="prefer-host-launcher",
                                    runtime_spec=container_runtime_spec(command=("--port", "18000")),
                                )

        self.assertEqual(4330, pid)
        self.assertEqual("gated", mode_used)
        self.assertEqual("gated_host_launcher_static", startup_backend)
        self.assertEqual("create", nerdctl_mock.call_args_list[1].args[1][0])
        self.assertIn("/__cpp_sampler_gate_launcher", nerdctl_mock.call_args_list[1].args[1])
        self.assertTrue(any("host launcher backend" in item for item in warnings))
        self.assertEqual("explicit preference selected host launcher before image-shell gating", selection_reason)

    def test_launch_container_prefer_image_shell_avoids_host_launcher_when_shell_is_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            with mock.patch(
                "orchestrator.stages.sample.nerdctl",
                side_effect=[
                    subprocess.CompletedProcess(["nerdctl"], 0, "", ""),
                    subprocess.CompletedProcess(["nerdctl"], 0, "", ""),
                    subprocess.CompletedProcess(["nerdctl"], 0, "", ""),
                ],
            ) as nerdctl_mock:
                with mock.patch("orchestrator.stages.sample.image_supports_shell", return_value=True):
                    with mock.patch(
                        "orchestrator.stages.sample._effective_container_argv",
                        return_value=["/app/server", "--port", "18000"],
                    ):
                        with mock.patch(
                            "orchestrator.stages.sample.inspect_container_pid",
                            return_value=4331,
                        ):
                            _container_name, pid, mode_used, warnings, startup_backend, selection_reason = _launch_container(
                                repo_root=ROOT,
                                image="docker.io/library/app:latest",
                                run_id="20260325_020303",
                                work_dir=work_dir,
                                startup_mode="gated",
                                startup_backend_preference="prefer-image-shell",
                                runtime_spec=container_runtime_spec(command=("--port", "18000")),
                            )

        self.assertEqual(4331, pid)
        self.assertEqual("gated", mode_used)
        self.assertEqual("gated_shell", startup_backend)
        self.assertEqual("create", nerdctl_mock.call_args_list[1].args[1][0])
        self.assertIn("/bin/sh", nerdctl_mock.call_args_list[1].args[1])
        self.assertEqual([], warnings)
        self.assertEqual("explicit preference kept image-shell gating", selection_reason)

    def test_run_sample_stage_writes_runtime_state_and_releases_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            fake_proc = mock.Mock()
            fake_proc.poll.return_value = None

            with mock.patch("orchestrator.stages.sample.choose_available_port", return_value=18111):
                with mock.patch(
                    "orchestrator.stages.sample._launch_container",
                        return_value=(
                            "demo-container",
                            4321,
                            "gated",
                            ["gated fallback warning"],
                            "gated_shell",
                            "auto kept image-shell gating because static host launcher is unavailable",
                        ),
                ) as launch_mock:
                    with mock.patch(
                        "orchestrator.stages.sample.subprocess.Popen",
                        return_value=fake_proc,
                    ) as popen_mock:
                        with mock.patch("orchestrator.stages.sample._http_get", return_value=b"metric 1\n"):
                            with mock.patch("orchestrator.stages.sample._release_gate") as release_gate_mock:
                                with mock.patch(
                                    "orchestrator.stages.sample.wait_for_ready_hook",
                                    return_value=True,
                                ) as wait_ready_mock:
                                    with mock.patch(
                                        "orchestrator.stages.sample.run_injection_hooks",
                                        return_value={
                                            "inference_log_path": str(work_dir / "inference.jsonl"),
                                            "predict_attempts": 3,
                                            "predict_successes": 3,
                                            "exec_inject_used": True,
                                            "did_io": True,
                                        },
                                    ) as inject_mock:
                                        with mock.patch("orchestrator.stages.sample.time.sleep"):
                                            with mock.patch("orchestrator.stages.sample._stop_process") as stop_process_mock:
                                                result = run_sample_stage(
                                                    repo_root=ROOT,
                                                    work_dir=work_dir,
                                                    image="docker.io/library/app:latest",
                                                    run_id="20260318_010101",
                                                    startup_mode="auto",
                                                    duration=0,
                                                    prom_port_start=8000,
                                                    mmap_window=120,
                                                    cg_refresh=2,
                                                    ready_url=None,
                                                ready_timeout=15,
                                                workload_profile="none",
                                                ready_hook=ExecReadyHook("test -f /tmp/ready"),
                                                container_runtime=container_runtime_spec(
                                                    gpus="all",
                                                    ipc="host",
                                                    env=("HF_HUB_OFFLINE=1",),
                                                    mounts=("type=bind,src=/models,dst=/models,readonly",),
                                                    command=("TinyLlama/TinyLlama-1.1B-Chat-v1.0", "--port", "18000"),
                                                ),
                                                inject_io=True,
                                                predict_url="http://127.0.0.1:9000/predict",
                                                predict_body='{"instances":[1.0]}',
                                                    inject_io_cmd="echo hi >/tmp/x",
                                                )

            self.assertEqual("gated", result.startup_mode_used)
            self.assertEqual("gated_shell", result.startup_backend)
            self.assertEqual(18111, result.prom_port)
            runtime_spec = launch_mock.call_args.kwargs["runtime_spec"]
            self.assertEqual("all", runtime_spec.gpus)
            self.assertEqual("host", runtime_spec.ipc)
            self.assertIn("HF_HUB_OFFLINE=1", runtime_spec.env)
            self.assertIn("--port", runtime_spec.command)
            release_gate_mock.assert_called_once_with(work_dir)
            wait_ready_mock.assert_called_once()
            inject_mock.assert_called_once()
            stop_process_mock.assert_called_once_with(fake_proc)
            popen_argv = popen_mock.call_args.args[0]
            popen_env = popen_mock.call_args.kwargs["env"]
            self.assertEqual(str((ROOT / "dist" / "bin" / "sampler_trace_ext").resolve()), popen_argv[0])
            self.assertIn("--pid", popen_argv)
            self.assertIn("4321", popen_argv)
            self.assertIn("--file-sink", popen_argv)
            self.assertIn(str(work_dir / "raw_events.jsonl"), popen_argv)
            self.assertEqual(str((ROOT / "dist" / "lib").resolve()), popen_env["LD_LIBRARY_PATH"].split(":")[0])

            runtime_state = (work_dir / "runtime_state.json").read_text(encoding="utf-8")
            payload = json.loads(runtime_state)
            self.assertEqual("gated", payload["startup_mode_used"])
            self.assertEqual("gated_shell", payload["startup_backend"])
            self.assertEqual("auto", payload["startup_backend_preference"])
            self.assertEqual(
                "auto kept image-shell gating because static host launcher is unavailable",
                payload["startup_backend_selection_reason"],
            )
            self.assertEqual("gated_shell", payload["startup"]["backend_family"])
            self.assertEqual("auto", payload["startup"]["backend_preference"])
            self.assertEqual(
                "auto kept image-shell gating because static host launcher is unavailable",
                payload["startup"]["backend_selection_reason"],
            )
            self.assertFalse(payload["startup"]["fallback_applied"])
            self.assertTrue(payload["ready_observed"])
            self.assertEqual("file_sink", payload["event_sink"])
            self.assertEqual("all", payload["container_runtime"]["gpus"])
            self.assertEqual("host", payload["container_runtime"]["ipc"])
            self.assertEqual(["gated fallback warning"], payload["warnings"])
            self.assertTrue(payload["injection"]["did_io"])
            self.assertTrue((work_dir / "sampler_metrics.prom").exists())

    def test_run_sample_stage_records_startup_fallback_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            fake_proc = mock.Mock()
            fake_proc.poll.return_value = None

            with mock.patch("orchestrator.stages.sample.choose_available_port", return_value=18116):
                with mock.patch(
                    "orchestrator.stages.sample._launch_container",
                    return_value=(
                        "demo-container",
                        4326,
                        "legacy",
                        ["gated startup unavailable; fallback to legacy: image lacks /bin/sh and host launcher is unavailable"],
                        "legacy",
                        "auto startup mode fell back to legacy because gated startup was unavailable",
                    ),
                ):
                    with mock.patch(
                        "orchestrator.stages.sample.subprocess.Popen",
                        return_value=fake_proc,
                    ):
                        with mock.patch("orchestrator.stages.sample._http_get", return_value=b"metric 1\n"):
                            with mock.patch("orchestrator.stages.sample.wait_for_ready_hook", return_value=True):
                                with mock.patch("orchestrator.stages.sample.time.sleep"):
                                    with mock.patch("orchestrator.stages.sample._stop_process"):
                                        run_sample_stage(
                                            repo_root=ROOT,
                                            work_dir=work_dir,
                                            image="docker.io/library/app:latest",
                                            run_id="20260325_010101",
                                            startup_mode="auto",
                                            duration=0,
                                            prom_port_start=8000,
                                            mmap_window=120,
                                            cg_refresh=2,
                                            ready_url=None,
                                            ready_timeout=15,
                                        )

            payload = json.loads((work_dir / "runtime_state.json").read_text(encoding="utf-8"))
            self.assertEqual("legacy", payload["startup_mode_used"])
            self.assertTrue(payload["startup"]["fallback_applied"])
            self.assertEqual(
                "image lacks /bin/sh and host launcher is unavailable",
                payload["startup"]["fallback_reason"],
            )
            self.assertEqual("legacy", payload["startup"]["backend_family"])
            self.assertEqual(
                "auto startup mode fell back to legacy because gated startup was unavailable",
                payload["startup"]["backend_selection_reason"],
            )

    def test_run_sample_stage_hard_fails_when_no_usable_outputs_are_produced(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            fake_proc = mock.Mock()
            fake_proc.poll.return_value = None

            with mock.patch("orchestrator.stages.sample.choose_available_port", return_value=18117):
                with mock.patch(
                    "orchestrator.stages.sample._launch_container",
                    return_value=(
                        "demo-container",
                        4327,
                        "legacy",
                        [],
                        "legacy",
                        "legacy startup mode requested",
                    ),
                ):
                    with mock.patch(
                        "orchestrator.stages.sample.subprocess.Popen",
                        return_value=fake_proc,
                    ):
                        with mock.patch("orchestrator.stages.sample._http_get", return_value=b""):
                            with mock.patch("orchestrator.stages.sample.wait_for_ready_hook", return_value=True):
                                with mock.patch("orchestrator.stages.sample.time.sleep"):
                                    with mock.patch("orchestrator.stages.sample._stop_process"):
                                        with mock.patch(
                                            "orchestrator.stages.sample.nerdctl",
                                            return_value=subprocess.CompletedProcess(["nerdctl"], 0, "", ""),
                                        ):
                                            with self.assertRaisesRegex(RuntimeError, "no usable output artifacts"):
                                                run_sample_stage(
                                                    repo_root=ROOT,
                                                    work_dir=work_dir,
                                                    image="docker.io/library/app:latest",
                                                    run_id="20260325_010102",
                                                    startup_mode="legacy",
                                                    duration=0,
                                                    prom_port_start=8000,
                                                    mmap_window=120,
                                                    cg_refresh=2,
                                                    ready_url=None,
                                                    ready_timeout=15,
                                                )

    def test_run_sample_stage_auto_uses_legacy_for_self_starting_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            fake_proc = mock.Mock()
            fake_proc.poll.return_value = None

            with mock.patch("orchestrator.stages.sample.choose_available_port", return_value=18117):
                with mock.patch(
                    "orchestrator.stages.sample._launch_container",
                    return_value=(
                        "demo-container",
                        4327,
                        "legacy",
                        [],
                        "legacy",
                        "legacy startup mode requested",
                    ),
                ) as launch_mock:
                    with mock.patch(
                        "orchestrator.stages.sample.subprocess.Popen",
                        return_value=fake_proc,
                    ):
                        with mock.patch("orchestrator.stages.sample._http_get", return_value=b"metric 1\n"):
                            with mock.patch("orchestrator.stages.sample.wait_for_ready_hook", return_value=True):
                                with mock.patch("orchestrator.stages.sample._release_gate") as release_gate_mock:
                                    with mock.patch("orchestrator.stages.sample.time.sleep"):
                                        with mock.patch("orchestrator.stages.sample._stop_process"):
                                            run_sample_stage(
                                                repo_root=ROOT,
                                                work_dir=work_dir,
                                                image="127.0.0.1:5001/abtest/tfpy-half-plus-two-io1g:noopt",
                                                run_id="20260413_010101",
                                                startup_mode="auto",
                                                duration=0,
                                                prom_port_start=8000,
                                                mmap_window=120,
                                                cg_refresh=2,
                                                ready_url="http://127.0.0.1:8000/ready",
                                                ready_timeout=15,
                                                workload_profile="tfpy-half-plus-two-io1g",
                                                self_starting_service=True,
                                            )

            self.assertEqual("legacy", launch_mock.call_args.kwargs["startup_mode"])
            release_gate_mock.assert_not_called()
            payload = json.loads((work_dir / "runtime_state.json").read_text(encoding="utf-8"))
            self.assertEqual("auto", payload["startup_mode_requested"])
            self.assertEqual("legacy", payload["startup_mode_used"])
            self.assertEqual("legacy", payload["startup_backend"])
            self.assertEqual(
                "auto startup mode selected legacy because workload is a self-starting service",
                payload["startup_backend_selection_reason"],
            )
            self.assertEqual(
                "auto startup mode selected legacy because workload is a self-starting service",
                payload["startup"]["backend_selection_reason"],
            )
            self.assertFalse(payload["startup"]["fallback_applied"])

    def test_run_sample_stage_avoids_sampler_port_collision_with_ready_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            fake_proc = mock.Mock()
            fake_proc.poll.return_value = None

            with mock.patch(
                "orchestrator.stages.sample.choose_available_port",
                side_effect=[8000, 8001],
            ):
                with mock.patch(
                    "orchestrator.stages.sample._launch_container",
                    return_value=(
                        "demo-container",
                        4328,
                        "legacy",
                        [],
                        "legacy",
                        "legacy startup mode requested",
                    ),
                ):
                    with mock.patch(
                        "orchestrator.stages.sample.subprocess.Popen",
                        return_value=fake_proc,
                    ) as popen_mock:
                        with mock.patch("orchestrator.stages.sample._http_get", return_value=b"metric 1\n"):
                            with mock.patch("orchestrator.stages.sample.wait_for_ready_hook", return_value=True):
                                with mock.patch("orchestrator.stages.sample.time.sleep"):
                                    with mock.patch("orchestrator.stages.sample._stop_process"):
                                        result = run_sample_stage(
                                            repo_root=ROOT,
                                            work_dir=work_dir,
                                            image="127.0.0.1:5001/abtest/tfpy-half-plus-two-io1g:noopt",
                                            run_id="20260413_020202",
                                            startup_mode="legacy",
                                            duration=0,
                                            prom_port_start=8000,
                                            mmap_window=120,
                                            cg_refresh=2,
                                            ready_url="http://127.0.0.1:8000/ready",
                                            ready_timeout=15,
                                            workload_profile="tfpy-half-plus-two-io1g",
                                        )

            self.assertEqual(8001, result.prom_port)
            popen_argv = popen_mock.call_args.args[0]
            self.assertIn("--prom-port", popen_argv)
            self.assertIn("8001", popen_argv)
            payload = json.loads((work_dir / "runtime_state.json").read_text(encoding="utf-8"))
            self.assertEqual(8001, payload["prom_port"])

    def test_run_sample_stage_records_live_handoff_pid_for_self_starting_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            fake_proc = mock.Mock()
            fake_proc.poll.return_value = None

            with mock.patch("orchestrator.stages.sample.choose_available_port", return_value=18119):
                with mock.patch(
                    "orchestrator.stages.sample._launch_container",
                    return_value=(
                        "demo-container",
                        4330,
                        "legacy",
                        [],
                        "legacy",
                        "legacy startup mode requested",
                    ),
                ):
                    with mock.patch(
                        "orchestrator.stages.sample.subprocess.Popen",
                        return_value=fake_proc,
                    ):
                        with mock.patch("orchestrator.stages.sample._http_get", return_value=b"metric 1\n"):
                            with mock.patch("orchestrator.stages.sample.wait_for_ready_hook", return_value=True):
                                with mock.patch("orchestrator.stages.sample._stop_process"):
                                    with mock.patch("orchestrator.stages.sample.time.sleep"):
                                        with mock.patch(
                                            "orchestrator.stages.sample.list_listening_pids",
                                            return_value=[5330],
                                        ):
                                            run_sample_stage(
                                                repo_root=ROOT,
                                                work_dir=work_dir,
                                                image="127.0.0.1:5001/abtest/tfpy-half-plus-two-io1g:noopt",
                                                run_id="20260413_025959",
                                                startup_mode="legacy",
                                                duration=0,
                                                prom_port_start=8000,
                                                mmap_window=120,
                                                cg_refresh=2,
                                                ready_url="http://127.0.0.1:8000/ready",
                                                ready_timeout=15,
                                                workload_profile="tfpy-half-plus-two-io1g",
                                                self_starting_service=True,
                                            )

            payload = json.loads((work_dir / "runtime_state.json").read_text(encoding="utf-8"))
            self.assertTrue(payload["self_starting_service"])
            self.assertEqual(4330, payload["container_pid"])
            self.assertEqual(5330, payload["live_container_pid"])
            self.assertEqual("ready_listener", payload["live_container_pid_source"])
            self.assertEqual(4330, payload["root_pid"])
            self.assertEqual("container_pid", payload["root_pid_source"])
            self.assertEqual(5330, payload["target_pid"])
            self.assertEqual("live_container_pid", payload["target_pid_source"])

    def test_run_sample_stage_keeps_container_pid_as_target_for_gated_host_launcher_ready_listener(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            fake_proc = mock.Mock()
            fake_proc.poll.return_value = None

            with mock.patch("orchestrator.stages.sample.choose_available_port", return_value=18120):
                with mock.patch(
                    "orchestrator.stages.sample._launch_container",
                    return_value=(
                        "demo-container",
                        4331,
                        "gated",
                        [],
                        "gated_host_launcher_static",
                        "static host launcher selected",
                    ),
                ):
                    with mock.patch(
                        "orchestrator.stages.sample.subprocess.Popen",
                        return_value=fake_proc,
                    ):
                        with mock.patch("orchestrator.stages.sample._http_get", return_value=b"metric 1\n"):
                            with mock.patch("orchestrator.stages.sample.wait_for_ready_hook", return_value=True):
                                with mock.patch("orchestrator.stages.sample._release_gate"):
                                    with mock.patch("orchestrator.stages.sample._stop_process"):
                                        with mock.patch("orchestrator.stages.sample.time.sleep"):
                                            with mock.patch(
                                                "orchestrator.stages.sample.list_listening_pids",
                                                return_value=[5331],
                                            ):
                                                run_sample_stage(
                                                    repo_root=ROOT,
                                                    work_dir=work_dir,
                                                    image="127.0.0.1:5001/abtest/ready-serving-bootstrap-v2:base",
                                                    run_id="20260418_120000",
                                                    startup_mode="gated",
                                                    duration=0,
                                                    prom_port_start=8000,
                                                    mmap_window=120,
                                                    cg_refresh=2,
                                                    ready_url="http://127.0.0.1:8000/ready",
                                                    ready_timeout=15,
                                                    workload_profile="ready-serving-bootstrap-v2",
                                                    self_starting_service=True,
                                                )

            payload = json.loads((work_dir / "runtime_state.json").read_text(encoding="utf-8"))
            self.assertEqual("gated_host_launcher_static", payload["startup_backend"])
            self.assertEqual(4331, payload["container_pid"])
            self.assertEqual(5331, payload["live_container_pid"])
            self.assertEqual("ready_listener", payload["live_container_pid_source"])
            self.assertEqual(4331, payload["root_pid"])
            self.assertEqual("container_pid", payload["root_pid_source"])
            self.assertEqual(4331, payload["target_pid"])
            self.assertEqual("container_pid", payload["target_pid_source"])

    def test_run_sample_stage_caps_post_ready_trace_window_for_self_starting_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            fake_proc = mock.Mock()
            fake_proc.poll.return_value = None

            with mock.patch("orchestrator.stages.sample.choose_available_port", return_value=18118):
                with mock.patch(
                    "orchestrator.stages.sample._launch_container",
                    return_value=(
                        "demo-container",
                        4329,
                        "legacy",
                        [],
                        "legacy",
                        "legacy startup mode requested",
                    ),
                ):
                    with mock.patch(
                        "orchestrator.stages.sample.subprocess.Popen",
                        return_value=fake_proc,
                    ):
                        with mock.patch("orchestrator.stages.sample._http_get", return_value=b"metric 1\n"):
                            with mock.patch("orchestrator.stages.sample.wait_for_ready_hook", return_value=True):
                                with mock.patch("orchestrator.stages.sample.time.sleep") as sleep_mock:
                                    with mock.patch("orchestrator.stages.sample._stop_process"):
                                        run_sample_stage(
                                            repo_root=ROOT,
                                            work_dir=work_dir,
                                            image="127.0.0.1:5001/abtest/tfpy-half-plus-two-io1g:noopt",
                                            run_id="20260413_030303",
                                            startup_mode="legacy",
                                            duration=90,
                                            prom_port_start=8000,
                                            mmap_window=120,
                                            cg_refresh=2,
                                            ready_url="http://127.0.0.1:8000/ready",
                                            ready_timeout=15,
                                            workload_profile="tfpy-half-plus-two-io1g",
                                            self_starting_service=True,
                                            inject_io=False,
                                        )

            sleep_mock.assert_called_once_with(5)

    def test_run_sample_stage_cleans_up_container_when_metrics_never_become_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            fake_proc = mock.Mock()
            fake_proc.poll.return_value = None

            with mock.patch("orchestrator.stages.sample.choose_available_port", return_value=18112):
                with mock.patch(
                    "orchestrator.stages.sample._launch_container",
                    return_value=(
                        "demo-container",
                        4322,
                        "legacy",
                        [],
                        "legacy",
                        "legacy startup mode requested",
                    ),
                ):
                    with mock.patch(
                        "orchestrator.stages.sample.subprocess.Popen",
                        return_value=fake_proc,
                    ):
                        with mock.patch(
                            "orchestrator.stages.sample._http_get",
                            side_effect=RuntimeError("metrics down"),
                        ):
                            with mock.patch("orchestrator.stages.sample._container_log_tail", return_value="launcher failed"):
                                with mock.patch("orchestrator.stages.sample.time.sleep"):
                                    with mock.patch("orchestrator.stages.sample._stop_process") as stop_process_mock:
                                        with mock.patch(
                                            "orchestrator.stages.sample.nerdctl",
                                            return_value=subprocess.CompletedProcess(["nerdctl"], 0, "", ""),
                                        ) as nerdctl_mock:
                                            with self.assertRaisesRegex(
                                                RuntimeError,
                                                "sampler /metrics did not become ready on port 18112.*startup_backend=legacy",
                                            ):
                                                run_sample_stage(
                                                    repo_root=ROOT,
                                                    work_dir=work_dir,
                                                    image="docker.io/library/app:latest",
                                                    run_id="20260318_010102",
                                                    startup_mode="legacy",
                                                    duration=0,
                                                    prom_port_start=8000,
                                                    mmap_window=120,
                                                    cg_refresh=2,
                                                    ready_url=None,
                                                    ready_timeout=15,
                                                )

            stop_process_mock.assert_called_once_with(fake_proc)
            self.assertEqual(2, nerdctl_mock.call_count)
            stop_call = nerdctl_mock.call_args_list[0]
            rm_call = nerdctl_mock.call_args_list[1]
            self.assertEqual(["stop", "demo-container"], stop_call.args[1])
            self.assertEqual(["rm", "demo-container"], rm_call.args[1])
            failure_payload = json.loads((work_dir / "runtime_state.json").read_text(encoding="utf-8"))
            self.assertEqual("failed", failure_payload["status"])
            self.assertEqual("legacy", failure_payload["startup_backend"])
            self.assertEqual("launcher failed", failure_payload["failure"]["recent_container_logs"])
            self.assertEqual("sampler_boot", failure_payload["failure"]["category"])
            self.assertEqual("sampler_metrics_unavailable", failure_payload["failure"]["kind"])

    def test_run_sample_stage_reports_sampler_early_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            fake_proc = mock.Mock()
            fake_proc.poll.return_value = 7

            with mock.patch("orchestrator.stages.sample.choose_available_port", return_value=18113):
                with mock.patch(
                    "orchestrator.stages.sample._launch_container",
                    return_value=(
                        "demo-container",
                        4323,
                        "legacy",
                        [],
                        "legacy",
                        "legacy startup mode requested",
                    ),
                ):
                    with mock.patch(
                        "orchestrator.stages.sample.subprocess.Popen",
                        return_value=fake_proc,
                    ):
                        with mock.patch("orchestrator.stages.sample._container_log_tail", return_value="container exited before gate release"):
                            with mock.patch("orchestrator.stages.sample.time.sleep"):
                                with mock.patch("orchestrator.stages.sample._stop_process") as stop_process_mock:
                                    with mock.patch(
                                        "orchestrator.stages.sample.nerdctl",
                                        return_value=subprocess.CompletedProcess(["nerdctl"], 0, "", ""),
                                    ) as nerdctl_mock:
                                        with self.assertRaisesRegex(
                                            RuntimeError,
                                            "sampler exited early.*startup_backend=legacy",
                                        ):
                                            run_sample_stage(
                                                repo_root=ROOT,
                                                work_dir=work_dir,
                                                image="docker.io/library/app:latest",
                                                run_id="20260318_010103",
                                                startup_mode="legacy",
                                                duration=0,
                                                prom_port_start=8000,
                                                mmap_window=120,
                                                cg_refresh=2,
                                                ready_url=None,
                                                ready_timeout=15,
                                            )

            stop_process_mock.assert_called_once_with(fake_proc)
            self.assertEqual(2, nerdctl_mock.call_count)
            failure_payload = json.loads((work_dir / "runtime_state.json").read_text(encoding="utf-8"))
            self.assertEqual("failed", failure_payload["status"])
            self.assertIn("sampler exited early", failure_payload["failure"]["error"])
            self.assertEqual("sampler_boot", failure_payload["failure"]["category"])
            self.assertEqual("sampler_early_exit", failure_payload["failure"]["kind"])
            self.assertEqual(
                "container exited before gate release",
                failure_payload["failure"]["recent_container_logs"],
            )

    def test_run_sample_stage_reports_host_launcher_runtime_failure_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            fake_proc = mock.Mock()
            fake_proc.poll.return_value = None

            with mock.patch("orchestrator.stages.sample.choose_available_port", return_value=18115):
                with mock.patch(
                    "orchestrator.stages.sample._launch_container",
                    return_value=(
                        "demo-container",
                        4325,
                        "gated",
                        ["gated startup uses experimental host launcher backend (dynamic); container runtime libc compatibility is required"],
                        "gated_host_launcher_dynamic",
                        "image lacks /bin/sh; host launcher is the only gated startup backend",
                    ),
                ):
                    with mock.patch(
                        "orchestrator.stages.sample.subprocess.Popen",
                        return_value=fake_proc,
                    ):
                        with mock.patch(
                            "orchestrator.stages.sample._http_get",
                            side_effect=RuntimeError("metrics down"),
                        ):
                            with mock.patch(
                                "orchestrator.stages.sample._container_log_tail",
                                return_value="exec /__cpp_sampler_gate_launcher: no such file or directory",
                            ):
                                with mock.patch("orchestrator.stages.sample.time.sleep"):
                                    with mock.patch("orchestrator.stages.sample._stop_process") as stop_process_mock:
                                        with mock.patch(
                                            "orchestrator.stages.sample.nerdctl",
                                            return_value=subprocess.CompletedProcess(["nerdctl"], 0, "", ""),
                                        ) as nerdctl_mock:
                                            with self.assertRaisesRegex(
                                                RuntimeError,
                                                "gated_host_launcher_dynamic",
                                            ):
                                                run_sample_stage(
                                                    repo_root=ROOT,
                                                    work_dir=work_dir,
                                                    image="docker.io/library/distroless-app:latest",
                                                    run_id="20260323_020101",
                                                    startup_mode="gated",
                                                    duration=0,
                                                    prom_port_start=8000,
                                                    mmap_window=120,
                                                    cg_refresh=2,
                                                    ready_url=None,
                                                    ready_timeout=15,
                                                )

            stop_process_mock.assert_called_once_with(fake_proc)
            self.assertEqual(2, nerdctl_mock.call_count)
            failure_payload = json.loads((work_dir / "runtime_state.json").read_text(encoding="utf-8"))
            self.assertEqual("failed", failure_payload["status"])
            self.assertEqual("gated", failure_payload["startup_mode_used"])
            self.assertEqual("gated_host_launcher_dynamic", failure_payload["startup_backend"])
            self.assertTrue(any("experimental host launcher backend" in item for item in failure_payload["warnings"]))
            self.assertEqual("startup_attach", failure_payload["failure"]["category"])
            self.assertEqual(
                "host_launcher_missing_runtime_dependency",
                failure_payload["failure"]["kind"],
            )
            self.assertEqual(
                "exec /__cpp_sampler_gate_launcher: no such file or directory",
                failure_payload["failure"]["recent_container_logs"],
            )

    def test_run_sample_stage_fails_when_ready_probe_never_observes_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            fake_proc = mock.Mock()
            fake_proc.poll.return_value = None

            with mock.patch("orchestrator.stages.sample.choose_available_port", return_value=18114):
                with mock.patch(
                    "orchestrator.stages.sample._launch_container",
                    return_value=(
                        "demo-container",
                        4324,
                        "legacy",
                        [],
                        "legacy",
                        "legacy startup mode requested",
                    ),
                ):
                    with mock.patch(
                        "orchestrator.stages.sample.subprocess.Popen",
                        return_value=fake_proc,
                    ):
                        with mock.patch("orchestrator.stages.sample._http_get", return_value=b"metric 1\n"):
                            with mock.patch(
                                "orchestrator.stages.sample.wait_for_ready_hook",
                                return_value=False,
                            ):
                                with mock.patch(
                                    "orchestrator.stages.sample._container_log_tail",
                                    return_value="EngineCore failed to start\nCUDA error: out of memory",
                                ):
                                    with mock.patch("orchestrator.stages.sample.time.sleep"):
                                        with mock.patch("orchestrator.stages.sample._stop_process") as stop_process_mock:
                                            with mock.patch(
                                                "orchestrator.stages.sample.nerdctl",
                                                side_effect=[
                                                    subprocess.CompletedProcess(["nerdctl"], 0, "", ""),
                                                    subprocess.CompletedProcess(["nerdctl"], 0, "", ""),
                                                ],
                                            ) as nerdctl_mock:
                                                with self.assertRaisesRegex(
                                                    RuntimeError,
                                                    "ready probe did not observe service readiness within 15s",
                                                ):
                                                    run_sample_stage(
                                                        repo_root=ROOT,
                                                        work_dir=work_dir,
                                                        image="docker.io/library/app:latest",
                                                        run_id="20260320_010104",
                                                        startup_mode="legacy",
                                                        duration=0,
                                                        prom_port_start=8000,
                                                        mmap_window=120,
                                                        cg_refresh=2,
                                                        ready_url=None,
                                                        ready_timeout=15,
                                                        ready_hook=ExecReadyHook("test -f /tmp/ready"),
                                                    )

            stop_process_mock.assert_called_once_with(fake_proc)
            self.assertEqual(2, nerdctl_mock.call_count)


if __name__ == "__main__":
    unittest.main()
