import unittest
from unittest import mock
from pathlib import Path
import json

from orchestrator.workloads import (
    CAP_EXEC_INJECT,
    CAP_HTTP_PREDICT,
    CAP_MODEL_MOUNT,
    CAP_READY_PROBE,
    ExecReadyHook,
    ExecInjectHook,
    HttpPredictHook,
    HttpReadyHook,
    LogRegexReadyHook,
    ModelMountHook,
    TcpReadyHook,
    WorkloadProfile,
    ready_hook_from_value,
    register_workload,
    resolve_benchmark_workload,
    resolve_sample_workload,
    validate_benchmark_workload_resolution,
    validate_sample_workload_resolution,
    workload_choices,
)
from orchestrator.workloads.builtin import builtin_workloads
from orchestrator.workloads import registry as workload_registry


class WorkloadResolutionTests(unittest.TestCase):
    def test_builtin_discovery_reads_plugin_modules(self):
        profiles = builtin_workloads()

        self.assertEqual(
            (
                "none",
                "ready-serving-bootstrap-v2",
                "ready-serving-layout-trap-v3",
                "ready-serving-layout-trap-v4",
                "ready-serving-layout-trap-v5",
                "tf-serving-efficientnet-v2-b0",
                "tf-serving-half-plus-two",
                "tfpy-half-plus-two-io1g",
            ),
            tuple(profile.name for profile in profiles),
        )

    def test_builtin_profiles_are_registered(self):
        self.assertIn("none", workload_choices())
        self.assertIn("ready-serving-bootstrap-v2", workload_choices())
        self.assertIn("ready-serving-layout-trap-v3", workload_choices())
        self.assertIn("ready-serving-layout-trap-v4", workload_choices())
        self.assertIn("ready-serving-layout-trap-v5", workload_choices())
        self.assertIn("tf-serving-efficientnet-v2-b0", workload_choices())
        self.assertIn("tf-serving-half-plus-two", workload_choices())
        self.assertIn("tfpy-half-plus-two-io1g", workload_choices())

    def test_sample_workload_uses_profile_defaults_for_ready_serving_bootstrap_v2(self):
        expected_body = (Path(__file__).resolve().parents[1] / "probes" / "sample_input.json").read_text(
            encoding="utf-8"
        ).strip()
        payload = resolve_sample_workload(
            profile_name="ready-serving-bootstrap-v2",
            ready_url=None,
            ready_timeout=None,
        )
        self.assertEqual("ready-serving-bootstrap-v2", payload["profile"])
        self.assertTrue(payload["self_starting_service"])
        self.assertEqual("http://127.0.0.1:8000/ready", payload["ready_url"])
        self.assertEqual("http://127.0.0.1:8000/predict", payload["predict_url"])
        self.assertEqual(expected_body, payload["predict_body"])
        self.assertIsNone(payload["inject_io_cmd"])
        self.assertEqual("", payload["model_dst_dir"])
        self.assertEqual("", payload["model_name"])
        self.assertEqual("", payload["model_host_dir"])
        self.assertEqual(
            [CAP_READY_PROBE, CAP_HTTP_PREDICT],
            payload["profile_capabilities"],
        )
        self.assertEqual("http_ready", payload["profile_hooks"]["ready"]["kind"])
        self.assertEqual("http_predict", payload["profile_hooks"]["predict"]["kind"])
        self.assertIsNone(payload["profile_hooks"]["inject"])
        self.assertIsNone(payload["profile_hooks"]["model_mount"])
        self.assertEqual(
            [CAP_READY_PROBE, CAP_HTTP_PREDICT],
            payload["effective_capabilities"],
        )

    def test_benchmark_workload_for_ready_serving_bootstrap_v2_does_not_require_model_host(self):
        expected_body = (Path(__file__).resolve().parents[1] / "probes" / "sample_input.json").read_text(
            encoding="utf-8"
        ).strip()
        payload = resolve_benchmark_workload(
            profile_name="ready-serving-bootstrap-v2",
            timeout_ready=None,
            ready_url=None,
            predict_url=None,
            predict_body=None,
            model_host_dir=None,
            model_dst_dir=None,
            model_name=None,
        )
        self.assertTrue(payload["self_starting_service"])
        self.assertEqual("http://127.0.0.1:8000/ready", payload["ready_url"])
        self.assertEqual("http://127.0.0.1:8000/predict", payload["predict_url"])
        self.assertEqual(expected_body, payload["predict_body"])
        self.assertEqual("", payload["model_host_dir"])
        self.assertEqual("", payload["model_dst_dir"])
        self.assertEqual("", payload["model_name"])
        self.assertEqual(
            [CAP_READY_PROBE, CAP_HTTP_PREDICT],
            payload["effective_capabilities"],
        )
        self.assertIsNone(payload["effective_hooks"]["inject"])
        self.assertIsNone(payload["effective_hooks"]["model_mount"])

    def test_sample_workload_uses_profile_defaults_for_ready_serving_layout_trap_v3(self):
        expected_body = (Path(__file__).resolve().parents[1] / "probes" / "sample_input.json").read_text(
            encoding="utf-8"
        ).strip()
        payload = resolve_sample_workload(
            profile_name="ready-serving-layout-trap-v3",
            ready_url=None,
            ready_timeout=None,
        )
        self.assertEqual("ready-serving-layout-trap-v3", payload["profile"])
        self.assertTrue(payload["self_starting_service"])
        self.assertEqual("http://127.0.0.1:8000/ready", payload["ready_url"])
        self.assertEqual("http://127.0.0.1:8000/predict", payload["predict_url"])
        self.assertEqual(expected_body, payload["predict_body"])
        self.assertIsNone(payload["inject_io_cmd"])
        self.assertEqual("", payload["model_dst_dir"])
        self.assertEqual("", payload["model_name"])
        self.assertEqual("", payload["model_host_dir"])
        self.assertEqual(
            [CAP_READY_PROBE, CAP_HTTP_PREDICT],
            payload["profile_capabilities"],
        )
        self.assertEqual("http_ready", payload["profile_hooks"]["ready"]["kind"])
        self.assertEqual("http_predict", payload["profile_hooks"]["predict"]["kind"])
        self.assertIsNone(payload["profile_hooks"]["inject"])
        self.assertIsNone(payload["profile_hooks"]["model_mount"])
        self.assertEqual(
            [CAP_READY_PROBE, CAP_HTTP_PREDICT],
            payload["effective_capabilities"],
        )

    def test_benchmark_workload_for_ready_serving_layout_trap_v3_does_not_require_model_host(self):
        expected_body = (Path(__file__).resolve().parents[1] / "probes" / "sample_input.json").read_text(
            encoding="utf-8"
        ).strip()
        payload = resolve_benchmark_workload(
            profile_name="ready-serving-layout-trap-v3",
            timeout_ready=None,
            ready_url=None,
            predict_url=None,
            predict_body=None,
            model_host_dir=None,
            model_dst_dir=None,
            model_name=None,
        )
        self.assertTrue(payload["self_starting_service"])
        self.assertEqual("http://127.0.0.1:8000/ready", payload["ready_url"])
        self.assertEqual("http://127.0.0.1:8000/predict", payload["predict_url"])
        self.assertEqual(expected_body, payload["predict_body"])
        self.assertEqual("", payload["model_host_dir"])
        self.assertEqual("", payload["model_dst_dir"])
        self.assertEqual("", payload["model_name"])
        self.assertEqual(
            [CAP_READY_PROBE, CAP_HTTP_PREDICT],
            payload["effective_capabilities"],
        )
        self.assertIsNone(payload["effective_hooks"]["inject"])
        self.assertIsNone(payload["effective_hooks"]["model_mount"])

    def test_sample_workload_uses_profile_defaults_for_ready_serving_layout_trap_v4(self):
        expected_body = (
            Path(__file__).resolve().parents[1]
            / "ready-serving-layout-trap-v4"
            / "probes"
            / "sample_input.json"
        ).read_text(encoding="utf-8").strip()
        payload = resolve_sample_workload(
            profile_name="ready-serving-layout-trap-v4",
            ready_url=None,
            ready_timeout=None,
        )
        self.assertEqual("ready-serving-layout-trap-v4", payload["profile"])
        self.assertTrue(payload["self_starting_service"])
        self.assertEqual("http://127.0.0.1:8000/ready", payload["ready_url"])
        self.assertEqual("http://127.0.0.1:8000/debug/state", payload["debug_state_url"])
        self.assertEqual("http://127.0.0.1:8000/predict", payload["predict_url"])
        self.assertEqual(expected_body, payload["predict_body"])
        self.assertEqual({"instances": [[1, 2, 3]]}, json.loads(payload["predict_body"]))
        self.assertIsNone(payload["inject_io_cmd"])
        self.assertEqual("", payload["model_dst_dir"])
        self.assertEqual("", payload["model_name"])
        self.assertEqual("", payload["model_host_dir"])
        self.assertEqual(
            [CAP_READY_PROBE, CAP_HTTP_PREDICT],
            payload["profile_capabilities"],
        )
        self.assertEqual("http_ready", payload["profile_hooks"]["ready"]["kind"])
        self.assertEqual("http_predict", payload["profile_hooks"]["predict"]["kind"])
        self.assertIsNone(payload["profile_hooks"]["inject"])
        self.assertIsNone(payload["profile_hooks"]["model_mount"])
        self.assertEqual(
            [CAP_READY_PROBE, CAP_HTTP_PREDICT],
            payload["effective_capabilities"],
        )

    def test_benchmark_workload_for_ready_serving_layout_trap_v4_does_not_require_model_host(self):
        expected_body = (
            Path(__file__).resolve().parents[1]
            / "ready-serving-layout-trap-v4"
            / "probes"
            / "sample_input.json"
        ).read_text(encoding="utf-8").strip()
        payload = resolve_benchmark_workload(
            profile_name="ready-serving-layout-trap-v4",
            timeout_ready=None,
            ready_url=None,
            predict_url=None,
            predict_body=None,
            model_host_dir=None,
            model_dst_dir=None,
            model_name=None,
        )
        self.assertTrue(payload["self_starting_service"])
        self.assertEqual("http://127.0.0.1:8000/ready", payload["ready_url"])
        self.assertEqual("http://127.0.0.1:8000/debug/state", payload["debug_state_url"])
        self.assertEqual("http://127.0.0.1:8000/predict", payload["predict_url"])
        self.assertEqual(expected_body, payload["predict_body"])
        self.assertEqual({"instances": [[1, 2, 3]]}, json.loads(payload["predict_body"]))
        self.assertEqual("", payload["model_host_dir"])
        self.assertEqual("", payload["model_dst_dir"])
        self.assertEqual("", payload["model_name"])
        self.assertEqual(
            [CAP_READY_PROBE, CAP_HTTP_PREDICT],
            payload["effective_capabilities"],
        )
        self.assertIsNone(payload["effective_hooks"]["inject"])
        self.assertIsNone(payload["effective_hooks"]["model_mount"])

    def test_sample_workload_uses_profile_defaults_for_ready_serving_layout_trap_v5(self):
        expected_body = (
            Path(__file__).resolve().parents[1]
            / "ready-serving-layout-trap-v5"
            / "probes"
            / "sample_input.json"
        ).read_text(encoding="utf-8").strip()
        payload = resolve_sample_workload(
            profile_name="ready-serving-layout-trap-v5",
            ready_url=None,
            ready_timeout=None,
        )
        self.assertEqual("ready-serving-layout-trap-v5", payload["profile"])
        self.assertTrue(payload["self_starting_service"])
        self.assertEqual("http://127.0.0.1:8000/ready", payload["ready_url"])
        self.assertEqual("http://127.0.0.1:8000/debug/state", payload["debug_state_url"])
        self.assertEqual("http://127.0.0.1:8000/predict", payload["predict_url"])
        self.assertEqual(expected_body, payload["predict_body"])
        self.assertEqual({"instances": [[1, 2, 3]]}, json.loads(payload["predict_body"]))
        self.assertIsNone(payload["inject_io_cmd"])
        self.assertEqual("", payload["model_dst_dir"])
        self.assertEqual("", payload["model_name"])
        self.assertEqual("", payload["model_host_dir"])
        self.assertEqual(
            [CAP_READY_PROBE, CAP_HTTP_PREDICT],
            payload["profile_capabilities"],
        )
        self.assertEqual("http_ready", payload["profile_hooks"]["ready"]["kind"])
        self.assertEqual("http_predict", payload["profile_hooks"]["predict"]["kind"])
        self.assertIsNone(payload["profile_hooks"]["inject"])
        self.assertIsNone(payload["profile_hooks"]["model_mount"])
        self.assertEqual(
            [CAP_READY_PROBE, CAP_HTTP_PREDICT],
            payload["effective_capabilities"],
        )

    def test_benchmark_workload_for_ready_serving_layout_trap_v5_does_not_require_model_host(self):
        expected_body = (
            Path(__file__).resolve().parents[1]
            / "ready-serving-layout-trap-v5"
            / "probes"
            / "sample_input.json"
        ).read_text(encoding="utf-8").strip()
        payload = resolve_benchmark_workload(
            profile_name="ready-serving-layout-trap-v5",
            timeout_ready=None,
            ready_url=None,
            predict_url=None,
            predict_body=None,
            model_host_dir=None,
            model_dst_dir=None,
            model_name=None,
        )
        self.assertTrue(payload["self_starting_service"])
        self.assertEqual("http://127.0.0.1:8000/ready", payload["ready_url"])
        self.assertEqual("http://127.0.0.1:8000/debug/state", payload["debug_state_url"])
        self.assertEqual("http://127.0.0.1:8000/predict", payload["predict_url"])
        self.assertEqual(expected_body, payload["predict_body"])
        self.assertEqual({"instances": [[1, 2, 3]]}, json.loads(payload["predict_body"]))
        self.assertEqual("", payload["model_host_dir"])
        self.assertEqual("", payload["model_dst_dir"])
        self.assertEqual("", payload["model_name"])
        self.assertEqual(
            [CAP_READY_PROBE, CAP_HTTP_PREDICT],
            payload["effective_capabilities"],
        )
        self.assertIsNone(payload["effective_hooks"]["inject"])
        self.assertIsNone(payload["effective_hooks"]["model_mount"])

    def test_sample_workload_uses_profile_defaults(self):
        payload = resolve_sample_workload(
            profile_name="tf-serving-half-plus-two",
            ready_url=None,
            ready_timeout=None,
        )
        self.assertEqual("tf-serving-half-plus-two", payload["profile"])
        self.assertEqual("http://127.0.0.1:8501/v1/models/half_plus_two", payload["ready_url"])
        self.assertEqual(30, payload["ready_timeout"])
        self.assertEqual("http://127.0.0.1:8501/v1/models/half_plus_two:predict", payload["predict_url"])
        self.assertIn('"instances"', payload["predict_body"])
        self.assertIn("/models/half_plus_two", payload["inject_io_cmd"])
        self.assertIn("saved_model.pb", payload["inject_io_cmd"])
        self.assertNotIn("find /models/half_plus_two -maxdepth 2 -type f", payload["inject_io_cmd"])
        self.assertEqual(
            [CAP_READY_PROBE, CAP_HTTP_PREDICT, CAP_EXEC_INJECT, CAP_MODEL_MOUNT],
            payload["profile_capabilities"],
        )
        self.assertEqual("http_ready", payload["profile_hooks"]["ready"]["kind"])
        self.assertEqual("http_predict", payload["profile_hooks"]["predict"]["kind"])
        self.assertEqual("exec_inject", payload["profile_hooks"]["inject"]["kind"])
        self.assertEqual("model_mount", payload["profile_hooks"]["model_mount"]["kind"])
        self.assertEqual(
            [CAP_READY_PROBE, CAP_HTTP_PREDICT, CAP_EXEC_INJECT, CAP_MODEL_MOUNT],
            payload["effective_capabilities"],
        )
        self.assertEqual("http_predict", payload["effective_hooks"]["predict"]["kind"])
        self.assertEqual("model_mount", payload["effective_hooks"]["model_mount"]["kind"])
        self.assertEqual("/models/half_plus_two", payload["model_dst_dir"])
        self.assertEqual("half_plus_two", payload["model_name"])
        self.assertEqual("", payload["model_host_dir"])

    def test_sample_workload_allows_model_mount_override(self):
        payload = resolve_sample_workload(
            profile_name="tf-serving-half-plus-two",
            ready_url=None,
            ready_timeout=None,
            model_host_dir="/tmp/model",
            model_dst_dir="/models/custom",
            model_name="custom-model",
        )

        self.assertEqual("/tmp/model", payload["model_host_dir"])
        self.assertEqual("/models/custom", payload["model_dst_dir"])
        self.assertEqual("custom-model", payload["model_name"])
        self.assertEqual("/models/custom", payload["effective_hooks"]["model_mount"]["dst_dir"])
        self.assertEqual("custom-model", payload["effective_hooks"]["model_mount"]["model_name"])

    def test_sample_workload_uses_profile_defaults_for_efficientnet(self):
        payload = resolve_sample_workload(
            profile_name="tf-serving-efficientnet-v2-b0",
            ready_url=None,
            ready_timeout=None,
        )
        self.assertEqual("tf-serving-efficientnet-v2-b0", payload["profile"])
        self.assertEqual(
            "http://127.0.0.1:8501/v1/models/efficientnet_v2_b0",
            payload["ready_url"],
        )
        self.assertEqual(
            "http://127.0.0.1:8501/v1/models/efficientnet_v2_b0:predict",
            payload["predict_url"],
        )
        self.assertIn("/models/efficientnet_v2_b0", payload["inject_io_cmd"])
        self.assertEqual("/models/efficientnet_v2_b0", payload["model_dst_dir"])
        self.assertEqual("efficientnet_v2_b0", payload["model_name"])
        self.assertEqual("", payload["model_host_dir"])

    def test_benchmark_workload_for_efficientnet_embedded_mode_keeps_model_host_empty(self):
        payload = resolve_benchmark_workload(
            profile_name="tf-serving-efficientnet-v2-b0",
            timeout_ready=None,
            ready_url=None,
            predict_url=None,
            predict_body=None,
            model_host_dir=None,
            model_dst_dir=None,
            model_name=None,
        )
        self.assertEqual("", payload["model_host_dir"])
        self.assertEqual("/models/efficientnet_v2_b0", payload["model_dst_dir"])
        self.assertEqual("efficientnet_v2_b0", payload["model_name"])
        self.assertEqual(
            "/models/efficientnet_v2_b0",
            payload["effective_hooks"]["model_mount"]["dst_dir"],
        )

    def test_sample_workload_uses_profile_defaults_for_tfpy_embedded_service(self):
        payload = resolve_sample_workload(
            profile_name="tfpy-half-plus-two-io1g",
            ready_url=None,
            ready_timeout=None,
        )
        self.assertEqual("tfpy-half-plus-two-io1g", payload["profile"])
        self.assertTrue(payload["self_starting_service"])
        self.assertEqual("http://127.0.0.1:8000/ready", payload["ready_url"])
        self.assertEqual("http://127.0.0.1:8000/predict", payload["predict_url"])
        self.assertEqual('{"instances": [[1.0]]}', payload["predict_body"])
        self.assertIsNone(payload["inject_io_cmd"])
        self.assertEqual("", payload["model_dst_dir"])
        self.assertEqual("", payload["model_name"])
        self.assertEqual("", payload["model_host_dir"])
        self.assertEqual(
            [CAP_READY_PROBE, CAP_HTTP_PREDICT],
            payload["profile_capabilities"],
        )
        self.assertEqual("http_ready", payload["profile_hooks"]["ready"]["kind"])
        self.assertEqual("http_predict", payload["profile_hooks"]["predict"]["kind"])
        self.assertIsNone(payload["profile_hooks"]["inject"])
        self.assertIsNone(payload["profile_hooks"]["model_mount"])

    def test_benchmark_workload_for_tfpy_embedded_service_does_not_require_model_host(self):
        payload = resolve_benchmark_workload(
            profile_name="tfpy-half-plus-two-io1g",
            timeout_ready=None,
            ready_url=None,
            predict_url=None,
            predict_body=None,
            model_host_dir=None,
            model_dst_dir=None,
            model_name=None,
        )
        self.assertTrue(payload["self_starting_service"])
        self.assertEqual("", payload["model_host_dir"])
        self.assertEqual("", payload["model_dst_dir"])
        self.assertEqual("", payload["model_name"])
        self.assertEqual(
            [CAP_READY_PROBE, CAP_HTTP_PREDICT],
            payload["effective_capabilities"],
        )
        self.assertIsNone(payload["effective_hooks"]["inject"])
        self.assertIsNone(payload["effective_hooks"]["model_mount"])

    def test_benchmark_workload_allows_explicit_overrides(self):
        payload = resolve_benchmark_workload(
            profile_name="tf-serving-half-plus-two",
            timeout_ready=45,
            ready_url="http://127.0.0.1:9000/ready",
            predict_url="",
            predict_body='{"instances":[42.0]}',
            model_host_dir="/tmp/model",
            model_dst_dir="/models/custom",
            model_name="custom-model",
        )
        self.assertEqual(45, payload["timeout_ready"])
        self.assertEqual("http://127.0.0.1:9000/ready", payload["ready_url"])
        self.assertEqual('{"instances":[42.0]}', payload["predict_body"])
        self.assertEqual("/tmp/model", payload["model_host_dir"])
        self.assertEqual("/models/custom", payload["model_dst_dir"])
        self.assertEqual("custom-model", payload["model_name"])
        self.assertEqual("/models/half_plus_two", payload["profile_hooks"]["model_mount"]["dst_dir"])
        self.assertEqual("/models/custom", payload["effective_hooks"]["model_mount"]["dst_dir"])
        self.assertEqual("custom-model", payload["effective_hooks"]["model_mount"]["model_name"])

    def test_none_workload_keeps_endpoints_empty(self):
        payload = resolve_benchmark_workload(
            profile_name="none",
            timeout_ready=None,
            ready_url=None,
            predict_url=None,
            predict_body=None,
            model_host_dir=None,
            model_dst_dir=None,
            model_name=None,
        )
        self.assertEqual("none", payload["profile"])
        self.assertIsNone(payload["ready_url"])
        self.assertEqual("", payload["predict_url"])
        self.assertEqual("", payload["predict_body"])
        self.assertEqual("", payload["model_dst_dir"])
        self.assertEqual([], payload["profile_capabilities"])
        self.assertEqual([], payload["effective_capabilities"])
        self.assertEqual(
            {"ready": None, "predict": None, "inject": None, "model_mount": None},
            payload["profile_hooks"],
        )
        self.assertEqual(
            {"ready": None, "predict": None, "inject": None, "model_mount": None},
            payload["effective_hooks"],
        )

    def test_custom_workload_can_be_registered_without_touching_cli(self):
        with mock.patch.dict(workload_registry._WORKLOAD_PROFILES, {}, clear=True):
            register_workload(
                WorkloadProfile(
                    name="custom-http",
                    description="custom ready/predict profile",
                    ready_url="http://127.0.0.1:8080/readyz",
                    predict_url="http://127.0.0.1:8080/predict",
                    predict_body='{"instances":[9.0]}',
                    inject_io_cmd="echo custom",
                    sample_ready_timeout=9,
                    benchmark_timeout=99,
                )
            )
            sample_payload = resolve_sample_workload(
                profile_name="custom-http",
                ready_url=None,
                ready_timeout=None,
            )
            bench_payload = resolve_benchmark_workload(
                profile_name="custom-http",
                timeout_ready=None,
                ready_url=None,
                predict_url=None,
                predict_body=None,
                model_host_dir=None,
                model_dst_dir=None,
                model_name=None,
            )
            self.assertEqual(("custom-http",), workload_choices())
            self.assertEqual("http://127.0.0.1:8080/readyz", sample_payload["ready_url"])
            self.assertEqual(9, sample_payload["ready_timeout"])
            self.assertEqual("http://127.0.0.1:8080/predict", bench_payload["predict_url"])
            self.assertEqual(99, bench_payload["timeout_ready"])
            self.assertEqual(
                [CAP_READY_PROBE, CAP_HTTP_PREDICT, CAP_EXEC_INJECT],
                sample_payload["profile_capabilities"],
            )
            self.assertEqual("http_predict", sample_payload["profile_hooks"]["predict"]["kind"])
            self.assertEqual("exec_inject", sample_payload["effective_hooks"]["inject"]["kind"])

    def test_effective_capabilities_include_cli_overrides(self):
        payload = resolve_sample_workload(
            profile_name="none",
            ready_url="http://127.0.0.1:8080/readyz",
            ready_timeout=None,
            predict_url="http://127.0.0.1:8080/predict",
            predict_body='{"instances":[1.0]}',
            inject_io_cmd="echo injected",
        )

        self.assertEqual([], payload["profile_capabilities"])
        self.assertEqual(
            [CAP_READY_PROBE, CAP_HTTP_PREDICT, CAP_EXEC_INJECT],
            payload["effective_capabilities"],
        )
        self.assertEqual("http_ready", payload["effective_hooks"]["ready"]["kind"])
        self.assertEqual("http_predict", payload["effective_hooks"]["predict"]["kind"])
        self.assertEqual("exec_inject", payload["effective_hooks"]["inject"]["kind"])

    def test_sample_validation_rejects_inject_io_without_effective_hooks(self):
        payload = resolve_sample_workload(
            profile_name="none",
            ready_url=None,
            ready_timeout=None,
            predict_url="http://127.0.0.1:8080/predict",
            predict_body=None,
            inject_io_cmd=None,
        )

        errors = validate_sample_workload_resolution(payload, inject_io=True)

        self.assertEqual(
            [
                "inject-io requires an effective predict hook or exec inject hook; choose a capable workload or pass both --predict-url/--predict-body and/or --inject-cmd",
                "predict-based inject-io requires both predict URL and predict body",
            ],
            errors,
        )

    def test_sample_validation_rejects_partial_predict_override_without_inject_io(self):
        payload = resolve_sample_workload(
            profile_name="none",
            ready_url=None,
            ready_timeout=None,
            predict_url="http://127.0.0.1:8080/predict",
            predict_body=None,
            inject_io_cmd=None,
        )

        errors = validate_sample_workload_resolution(payload, inject_io=False)

        self.assertEqual(
            ["sample workload predict overrides must set both predict URL and predict body"],
            errors,
        )

    def test_benchmark_validation_requires_ready_probe_and_complete_predict_pair(self):
        payload = resolve_benchmark_workload(
            profile_name="none",
            timeout_ready=None,
            ready_url=None,
            predict_url="http://127.0.0.1:8080/predict",
            predict_body=None,
            model_host_dir=None,
            model_dst_dir=None,
            model_name=None,
        )

        errors = validate_benchmark_workload_resolution(payload, mode="ready")

        self.assertEqual(
            [
                "ready mode requires an effective ready probe",
                "benchmark predict configuration requires both predict URL and predict body",
            ],
            errors,
        )

    def test_ready_hook_from_value_supports_tcp_scheme(self):
        hook = ready_hook_from_value("tcp://127.0.0.1:8888")

        self.assertIsInstance(hook, TcpReadyHook)
        self.assertEqual("tcp://127.0.0.1:8888", hook.summary()["url"])

    def test_sample_workload_accepts_tcp_ready_override(self):
        payload = resolve_sample_workload(
            profile_name="none",
            ready_url="tcp://127.0.0.1:4040",
            ready_timeout=None,
        )

        self.assertEqual("tcp_ready", payload["effective_hooks"]["ready"]["kind"])
        self.assertEqual(["ready_probe"], payload["effective_capabilities"])

    def test_sample_workload_accepts_exec_ready_override(self):
        payload = resolve_sample_workload(
            profile_name="none",
            ready_url=None,
            ready_exec="test -f /tmp/ready",
            ready_shell="bash",
            ready_timeout=None,
        )

        self.assertEqual("exec_ready", payload["effective_hooks"]["ready"]["kind"])
        self.assertEqual("test -f /tmp/ready", payload["ready_exec"])
        self.assertEqual("bash", payload["ready_shell"])
        self.assertEqual(["ready_probe"], payload["effective_capabilities"])

    def test_benchmark_workload_accepts_log_regex_ready_override(self):
        payload = resolve_benchmark_workload(
            profile_name="none",
            timeout_ready=None,
            ready_url=None,
            ready_exec=None,
            ready_log_regex=r"Server listening on .*:8888",
            ready_shell=None,
            predict_url=None,
            predict_body=None,
            model_host_dir=None,
            model_dst_dir=None,
            model_name=None,
        )

        self.assertEqual("log_regex_ready", payload["effective_hooks"]["ready"]["kind"])
        self.assertEqual(r"Server listening on .*:8888", payload["ready_log_regex"])
        self.assertEqual(["ready_probe"], payload["effective_capabilities"])

    def test_ready_probe_override_conflict_is_rejected(self):
        payload = resolve_benchmark_workload(
            profile_name="none",
            timeout_ready=None,
            ready_url="http://127.0.0.1:8080/readyz",
            ready_exec="test -f /tmp/ready",
            ready_log_regex=None,
            ready_shell="sh",
            predict_url=None,
            predict_body=None,
            model_host_dir=None,
            model_dst_dir=None,
            model_name=None,
        )

        errors = validate_benchmark_workload_resolution(payload, mode="ready")

        self.assertIn("ready probe accepts only one of ready URL, ready exec, or ready log regex", errors)

    def test_profile_accepts_explicit_hook_objects(self):
        profile = WorkloadProfile(
            name="hooked",
            description="hook driven",
            ready_hook=HttpReadyHook("http://127.0.0.1:8081/ready"),
            predict_hook=HttpPredictHook("http://127.0.0.1:8081/predict", '{"instances":[3.0]}', attempts=5),
            inject_hook=ExecInjectHook("echo hooked", shell="bash"),
            model_mount_hook=ModelMountHook("/models/hooked", "hooked-model"),
        )

        self.assertEqual("http://127.0.0.1:8081/ready", profile.ready_hook.url)
        self.assertEqual(5, profile.predict_hook.attempts)
        self.assertEqual("bash", profile.inject_hook.shell)
        self.assertEqual(
            {
                "kind": "model_mount",
                "dst_dir": "/models/hooked",
                "model_name": "hooked-model",
                "model_base_path_env": None,
            },
            profile.hook_summaries()["model_mount"],
        )


if __name__ == "__main__":
    unittest.main()
