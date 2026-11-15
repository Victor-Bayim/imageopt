from __future__ import annotations

from orchestrator.workloads.base import (
    CAP_EXEC_INJECT,
    CAP_HTTP_PREDICT,
    CAP_MODEL_MOUNT,
    CAP_READY_PROBE,
    WorkloadProfile,
)
from orchestrator.workloads.builtin import register_builtin_workloads
from orchestrator.workloads.execution import (
    benchmark_hook_parameters,
    register_benchmark_arg_builder,
    register_inject_handler,
    register_predict_handler,
    register_ready_handler,
    run_injection_hooks,
    wait_for_ready_hook,
)
from orchestrator.workloads.hooks import (
    ExecReadyHook,
    ExecInjectHook,
    HttpPredictHook,
    HttpReadyHook,
    LogRegexReadyHook,
    ModelMountHook,
    TcpReadyHook,
    ready_hook_from_value,
)
from orchestrator.workloads.registry import get_workload_profile, register_workload, workload_choices
from orchestrator.workloads.resolver import (
    resolve_benchmark_workload,
    resolve_sample_workload,
    validate_benchmark_workload_resolution,
    validate_sample_workload_resolution,
)

register_builtin_workloads()

__all__ = [
    "CAP_EXEC_INJECT",
    "CAP_HTTP_PREDICT",
    "CAP_MODEL_MOUNT",
    "CAP_READY_PROBE",
    "ExecReadyHook",
    "ExecInjectHook",
    "HttpPredictHook",
    "HttpReadyHook",
    "LogRegexReadyHook",
    "ModelMountHook",
    "TcpReadyHook",
    "WorkloadProfile",
    "benchmark_hook_parameters",
    "get_workload_profile",
    "register_benchmark_arg_builder",
    "register_inject_handler",
    "register_predict_handler",
    "register_ready_handler",
    "register_workload",
    "run_injection_hooks",
    "resolve_benchmark_workload",
    "resolve_sample_workload",
    "ready_hook_from_value",
    "validate_benchmark_workload_resolution",
    "validate_sample_workload_resolution",
    "wait_for_ready_hook",
    "workload_choices",
]
