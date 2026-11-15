from __future__ import annotations

from collections.abc import Mapping

from orchestrator.workloads.base import (
    CAP_EXEC_INJECT,
    CAP_HTTP_PREDICT,
    CAP_MODEL_MOUNT,
    CAP_READY_PROBE,
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
    summarize_hook_set,
)
from orchestrator.workloads.registry import get_workload_profile


def _effective_capabilities(
    *,
    ready_probe: bool = False,
    predict_url: str | None = None,
    predict_body: str | None = None,
    inject_io_cmd: str | None = None,
    model_dst_dir: str | None = None,
    model_name: str | None = None,
) -> tuple[str, ...]:
    effective: list[str] = []
    if ready_probe:
        effective.append(CAP_READY_PROBE)
    if predict_url and predict_body:
        effective.append(CAP_HTTP_PREDICT)
    if inject_io_cmd:
        effective.append(CAP_EXEC_INJECT)
    if model_dst_dir or model_name:
        effective.append(CAP_MODEL_MOUNT)
    return tuple(dict.fromkeys(effective))


def _ready_override_payload(
    *,
    ready_url: str | None,
    ready_exec: str | None,
    ready_log_regex: str | None,
    ready_shell: str | None,
) -> tuple[HttpReadyHook | TcpReadyHook | ExecReadyHook | LogRegexReadyHook | None, bool]:
    override_count = sum(bool(item) for item in (ready_url, ready_exec, ready_log_regex))
    if ready_url:
        return ready_hook_from_value(ready_url), override_count > 1
    if ready_exec:
        return ExecReadyHook(ready_exec, shell=ready_shell or "sh"), override_count > 1
    if ready_log_regex:
        return LogRegexReadyHook(ready_log_regex), override_count > 1
    return None, override_count > 1


def _effective_ready_hook(
    profile,
    *,
    ready_url: str | None,
    ready_exec: str | None,
    ready_log_regex: str | None,
    ready_shell: str | None,
) -> tuple[HttpReadyHook | TcpReadyHook | ExecReadyHook | LogRegexReadyHook | None, bool]:
    override_hook, conflict = _ready_override_payload(
        ready_url=ready_url,
        ready_exec=ready_exec,
        ready_log_regex=ready_log_regex,
        ready_shell=ready_shell,
    )
    if override_hook is not None:
        return override_hook, conflict
    return profile.ready_hook, conflict


def _effective_predict_hook(
    predict_url: str | None,
    predict_body: str | None,
    *,
    attempts: int = 3,
) -> HttpPredictHook | None:
    if predict_url and predict_body:
        return HttpPredictHook(predict_url, predict_body, attempts=attempts)
    return None


def _effective_inject_hook(profile, inject_io_cmd: str | None) -> ExecInjectHook | None:
    if inject_io_cmd is not None:
        return ExecInjectHook(inject_io_cmd) if inject_io_cmd else None
    return profile.inject_hook


def _effective_model_mount_hook(
    profile,
    model_dst_dir: str | None,
    model_name: str | None,
) -> ModelMountHook | None:
    base = profile.model_mount_hook
    effective_dst_dir = model_dst_dir if model_dst_dir is not None else (None if base is None else base.dst_dir)
    effective_model_name = model_name if model_name is not None else (None if base is None else base.model_name)
    effective_model_base_path_env = None if base is None else base.model_base_path_env
    if effective_dst_dir or effective_model_name:
        return ModelMountHook(effective_dst_dir or "", effective_model_name, effective_model_base_path_env)
    return None


def _ready_hook_fields(
    hook: HttpReadyHook | TcpReadyHook | ExecReadyHook | LogRegexReadyHook | None,
) -> tuple[str | None, str, str, str]:
    if hook is None:
        return None, "", "", "sh"
    if isinstance(hook, (HttpReadyHook, TcpReadyHook)):
        return str(hook.summary().get("url") or ""), "", "", "sh"
    if isinstance(hook, ExecReadyHook):
        return None, hook.command, "", hook.shell
    if isinstance(hook, LogRegexReadyHook):
        return None, "", hook.pattern, "sh"
    return None, "", "", "sh"


def resolve_sample_workload(
    *,
    profile_name: str,
    ready_url: str | None,
    ready_exec: str | None = None,
    ready_log_regex: str | None = None,
    ready_shell: str | None = None,
    ready_timeout: int | None,
    predict_url: str | None = None,
    predict_body: str | None = None,
    inject_io_cmd: str | None = None,
    model_host_dir: str | None = None,
    model_dst_dir: str | None = None,
    model_name: str | None = None,
) -> dict[str, object]:
    profile = get_workload_profile(profile_name)
    effective_ready_hook, ready_probe_conflict = _effective_ready_hook(
        profile,
        ready_url=ready_url,
        ready_exec=ready_exec,
        ready_log_regex=ready_log_regex,
        ready_shell=ready_shell,
    )
    base_predict_hook = profile.predict_hook
    effective_predict_url = predict_url if predict_url is not None else (None if base_predict_hook is None else base_predict_hook.url)
    effective_predict_body = predict_body if predict_body is not None else (None if base_predict_hook is None else base_predict_hook.body)
    effective_predict_hook = _effective_predict_hook(
        effective_predict_url,
        effective_predict_body,
        attempts=3 if base_predict_hook is None else base_predict_hook.attempts,
    )
    effective_inject_hook = _effective_inject_hook(profile, inject_io_cmd)
    effective_model_host_dir = model_host_dir or ""
    effective_model_mount_hook = _effective_model_mount_hook(profile, model_dst_dir, model_name)
    effective_model_dst_dir = "" if effective_model_mount_hook is None else effective_model_mount_hook.dst_dir
    effective_model_name = "" if effective_model_mount_hook is None else (effective_model_mount_hook.model_name or "")
    effective_ready_url, effective_ready_exec, effective_ready_log_regex, effective_ready_shell = _ready_hook_fields(
        effective_ready_hook
    )
    effective_ready_timeout = int(ready_timeout if ready_timeout is not None else profile.sample_ready_timeout)
    effective_inject_io_cmd = None if effective_inject_hook is None else effective_inject_hook.command
    return {
        "profile": profile.name,
        "self_starting_service": bool(profile.self_starting_service),
        "debug_state_url": profile.debug_state_url or "",
        "profile_capabilities": list(profile.capabilities or ()),
        "profile_hooks": profile.hook_summaries(),
        "ready_probe_conflict": ready_probe_conflict,
        "effective_ready_hook": effective_ready_hook,
        "effective_predict_hook": effective_predict_hook,
        "effective_inject_hook": effective_inject_hook,
        "ready_url": effective_ready_url,
        "ready_exec": effective_ready_exec,
        "ready_log_regex": effective_ready_log_regex,
        "ready_shell": effective_ready_shell,
        "ready_timeout": effective_ready_timeout,
        "predict_url": effective_predict_url,
        "predict_body": effective_predict_body,
        "inject_io_cmd": effective_inject_io_cmd,
        "model_host_dir": effective_model_host_dir,
        "model_dst_dir": effective_model_dst_dir,
        "model_name": effective_model_name,
        "effective_model_mount_hook": effective_model_mount_hook,
        "effective_hooks": summarize_hook_set(
            ready_hook=effective_ready_hook,
            predict_hook=effective_predict_hook,
            inject_hook=effective_inject_hook,
            model_mount_hook=effective_model_mount_hook,
        ),
        "effective_capabilities": list(
            _effective_capabilities(
                ready_probe=effective_ready_hook is not None,
                predict_url=effective_predict_url,
                predict_body=effective_predict_body,
                inject_io_cmd=effective_inject_io_cmd,
                model_dst_dir=effective_model_dst_dir,
                model_name=effective_model_name,
            )
        ),
    }


def resolve_benchmark_workload(
    *,
    profile_name: str,
    timeout_ready: int | None,
    ready_url: str | None,
    ready_exec: str | None = None,
    ready_log_regex: str | None = None,
    ready_shell: str | None = None,
    predict_url: str | None,
    predict_body: str | None,
    model_host_dir: str | None,
    model_dst_dir: str | None,
    model_name: str | None,
) -> dict[str, object]:
    profile = get_workload_profile(profile_name)
    effective_timeout = int(timeout_ready if timeout_ready is not None else profile.benchmark_timeout)
    effective_ready_hook, ready_probe_conflict = _effective_ready_hook(
        profile,
        ready_url=ready_url,
        ready_exec=ready_exec,
        ready_log_regex=ready_log_regex,
        ready_shell=ready_shell,
    )
    base_predict_hook = profile.predict_hook
    effective_predict_url = predict_url if predict_url is not None else (None if base_predict_hook is None else base_predict_hook.url)
    effective_predict_body = predict_body if predict_body is not None else (None if base_predict_hook is None else base_predict_hook.body)
    effective_predict_hook = _effective_predict_hook(
        effective_predict_url,
        effective_predict_body,
        attempts=3 if base_predict_hook is None else base_predict_hook.attempts,
    )
    effective_ready_url, effective_ready_exec, effective_ready_log_regex, effective_ready_shell = _ready_hook_fields(
        effective_ready_hook
    )
    effective_model_host_dir = model_host_dir or ""
    effective_model_mount_hook = _effective_model_mount_hook(profile, model_dst_dir, model_name)
    effective_model_dst_dir = "" if effective_model_mount_hook is None else effective_model_mount_hook.dst_dir
    effective_model_name = "" if effective_model_mount_hook is None else (effective_model_mount_hook.model_name or "")
    return {
        "profile": profile.name,
        "self_starting_service": bool(profile.self_starting_service),
        "debug_state_url": profile.debug_state_url or "",
        "profile_capabilities": list(profile.capabilities or ()),
        "profile_hooks": profile.hook_summaries(),
        "ready_probe_conflict": ready_probe_conflict,
        "effective_ready_hook": effective_ready_hook,
        "effective_predict_hook": effective_predict_hook,
        "effective_model_mount_hook": effective_model_mount_hook,
        "timeout_ready": effective_timeout,
        "ready_url": effective_ready_url,
        "ready_exec": effective_ready_exec,
        "ready_log_regex": effective_ready_log_regex,
        "ready_shell": effective_ready_shell,
        "predict_url": effective_predict_url or "",
        "predict_body": effective_predict_body or "",
        "model_host_dir": effective_model_host_dir,
        "model_dst_dir": effective_model_dst_dir,
        "model_name": effective_model_name,
        "effective_hooks": summarize_hook_set(
            ready_hook=effective_ready_hook,
            predict_hook=effective_predict_hook,
            model_mount_hook=effective_model_mount_hook,
        ),
        "effective_capabilities": list(
            _effective_capabilities(
                ready_probe=effective_ready_hook is not None,
                predict_url=effective_predict_url,
                predict_body=effective_predict_body,
                model_dst_dir=effective_model_dst_dir,
                model_name=effective_model_name,
            )
        ),
    }


def validate_sample_workload_resolution(
    payload: Mapping[str, object],
    *,
    inject_io: bool,
) -> list[str]:
    errors: list[str] = []
    predict_url = str(payload.get("predict_url") or "")
    predict_body = str(payload.get("predict_body") or "")
    inject_io_cmd = str(payload.get("inject_io_cmd") or "")
    effective_capabilities = set(payload.get("effective_capabilities") or [])
    if bool(payload.get("ready_probe_conflict")):
        errors.append("ready probe accepts only one of ready URL, ready exec, or ready log regex")

    if inject_io and CAP_HTTP_PREDICT not in effective_capabilities and CAP_EXEC_INJECT not in effective_capabilities:
        errors.append(
            "inject-io requires an effective predict hook or exec inject hook; choose a capable workload or pass both --predict-url/--predict-body and/or --inject-cmd"
        )
    if inject_io and bool(predict_url) != bool(predict_body):
        errors.append("predict-based inject-io requires both predict URL and predict body")
    if not inject_io and not inject_io_cmd and bool(predict_url) != bool(predict_body):
        errors.append("sample workload predict overrides must set both predict URL and predict body")
    return errors


def validate_benchmark_workload_resolution(
    payload: Mapping[str, object],
    *,
    mode: str,
) -> list[str]:
    errors: list[str] = []
    predict_url = str(payload.get("predict_url") or "")
    predict_body = str(payload.get("predict_body") or "")
    effective_capabilities = set(payload.get("effective_capabilities") or [])
    if bool(payload.get("ready_probe_conflict")):
        errors.append("ready probe accepts only one of ready URL, ready exec, or ready log regex")

    if mode == "ready" and CAP_READY_PROBE not in effective_capabilities:
        errors.append("ready mode requires an effective ready probe")
    if bool(predict_url) != bool(predict_body):
        errors.append("benchmark predict configuration requires both predict URL and predict body")
    return errors
