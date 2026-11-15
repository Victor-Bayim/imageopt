from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import re
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import TextIO

from orchestrator.runtime import nerdctl
from orchestrator.workloads.hooks import (
    ExecReadyHook,
    ExecInjectHook,
    HttpPredictHook,
    HttpReadyHook,
    LogRegexReadyHook,
    ModelMountHook,
    TcpReadyHook,
)

@dataclass(frozen=True)
class ReadyContext:
    timeout_sec: int
    repo_root: Path | None = None
    container_name: str | None = None


ReadyHandler = Callable[[object, ReadyContext], bool]
PredictHandler = Callable[[object, TextIO], dict[str, int]]
InjectHandler = Callable[[Path, str, object], bool]
BenchmarkArgBuilder = Callable[[object], dict[str, str]]

_READY_HANDLERS: dict[type[object], ReadyHandler] = {}
_PREDICT_HANDLERS: dict[type[object], PredictHandler] = {}
_INJECT_HANDLERS: dict[type[object], InjectHandler] = {}
_BENCHMARK_ARG_BUILDERS: dict[type[object], BenchmarkArgBuilder] = {}


def _http_get(url: str, timeout: float = 1.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()


def _http_post(url: str, body: str, timeout: float = 2.0) -> bytes:
    req = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def _resolve_handler(registry: dict[type[object], Callable], hook: object, label: str) -> Callable:
    for cls in type(hook).__mro__:
        handler = registry.get(cls)
        if handler is not None:
            return handler
    raise TypeError(f"no {label} registered for hook type {type(hook).__name__}")


def register_ready_handler(
    hook_type: type[object],
    handler: ReadyHandler,
    *,
    replace: bool = False,
) -> None:
    if hook_type in _READY_HANDLERS and not replace:
        raise ValueError(f"ready handler already registered for {hook_type.__name__}")
    _READY_HANDLERS[hook_type] = handler


def register_predict_handler(
    hook_type: type[object],
    handler: PredictHandler,
    *,
    replace: bool = False,
) -> None:
    if hook_type in _PREDICT_HANDLERS and not replace:
        raise ValueError(f"predict handler already registered for {hook_type.__name__}")
    _PREDICT_HANDLERS[hook_type] = handler


def register_inject_handler(
    hook_type: type[object],
    handler: InjectHandler,
    *,
    replace: bool = False,
) -> None:
    if hook_type in _INJECT_HANDLERS and not replace:
        raise ValueError(f"inject handler already registered for {hook_type.__name__}")
    _INJECT_HANDLERS[hook_type] = handler


def register_benchmark_arg_builder(
    hook_type: type[object],
    handler: BenchmarkArgBuilder,
    *,
    replace: bool = False,
) -> None:
    if hook_type in _BENCHMARK_ARG_BUILDERS and not replace:
        raise ValueError(f"benchmark arg builder already registered for {hook_type.__name__}")
    _BENCHMARK_ARG_BUILDERS[hook_type] = handler


def _wait_http_ready_hook(hook: HttpReadyHook, ready_ctx: ReadyContext) -> bool:
    for _ in range(ready_ctx.timeout_sec):
        try:
            _http_get(hook.url, timeout=1.0)
            return True
        except Exception:
            time.sleep(1)
    return False


def _wait_tcp_ready_hook(hook: TcpReadyHook, ready_ctx: ReadyContext) -> bool:
    for _ in range(ready_ctx.timeout_sec):
        try:
            with socket.create_connection((hook.host, int(hook.port)), timeout=1.0):
                return True
        except Exception:
            time.sleep(1)
    return False


def _wait_exec_ready_hook(hook: ExecReadyHook, ready_ctx: ReadyContext) -> bool:
    if ready_ctx.repo_root is None or not ready_ctx.container_name:
        raise ValueError("exec ready hook requires repo_root and container_name")
    deadline = time.time() + ready_ctx.timeout_sec
    while time.time() < deadline:
        proc = nerdctl(
            ready_ctx.repo_root,
            ["exec", ready_ctx.container_name, hook.shell, "-lc", hook.command],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if proc.returncode == 0:
            return True
        time.sleep(1)
    return False


def _wait_log_regex_ready_hook(hook: LogRegexReadyHook, ready_ctx: ReadyContext) -> bool:
    if ready_ctx.repo_root is None or not ready_ctx.container_name:
        raise ValueError("log regex ready hook requires repo_root and container_name")
    deadline = time.time() + ready_ctx.timeout_sec
    while time.time() < deadline:
        proc = nerdctl(
            ready_ctx.repo_root,
            ["logs", "--tail", "200", ready_ctx.container_name],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if proc.stdout and re.search(hook.pattern, proc.stdout, flags=re.MULTILINE):
            return True
        time.sleep(1)
    return False


def wait_for_ready_hook(
    ready_hook: object | None,
    timeout_sec: int,
    *,
    repo_root: Path | None = None,
    container_name: str | None = None,
) -> bool | None:
    if ready_hook is None:
        return None
    handler = _resolve_handler(_READY_HANDLERS, ready_hook, "ready handler")
    return bool(handler(ready_hook, ReadyContext(timeout_sec=timeout_sec, repo_root=repo_root, container_name=container_name)))


def _run_http_predict_hook(hook: HttpPredictHook, out_fh: TextIO) -> dict[str, int]:
    predict_attempts = 0
    predict_successes = 0
    for _ in range(max(1, int(hook.attempts))):
        predict_attempts += 1
        try:
            response = _http_post(hook.url, hook.body, timeout=2.0)
        except Exception:
            continue
        predict_successes += 1
        out_fh.write(response.decode("utf-8", errors="ignore") + "\n")
    return {
        "predict_attempts": predict_attempts,
        "predict_successes": predict_successes,
    }


def _run_exec_inject_hook(repo_root: Path, container_name: str, hook: ExecInjectHook) -> bool:
    proc = nerdctl(
        repo_root,
        # Inject hooks should not use a login shell; otherwise /etc/profile and
        # ~/.profile reads pollute the sampling signal with shell startup noise.
        ["exec", container_name, hook.shell, "-c", hook.command],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc.returncode == 0


def run_injection_hooks(
    *,
    repo_root: Path,
    work_dir: Path,
    container_name: str,
    predict_hook: object | None,
    inject_hook: object | None,
) -> dict[str, object]:
    inference_log_path = work_dir / "inference.jsonl"
    inference_log_path.write_text("", encoding="utf-8")

    predict_attempts = 0
    predict_successes = 0
    exec_inject_used = False

    if predict_hook is not None:
        with inference_log_path.open("a", encoding="utf-8") as out_fh:
            handler = _resolve_handler(_PREDICT_HANDLERS, predict_hook, "predict handler")
            predict_payload = handler(predict_hook, out_fh)
            predict_attempts = int(predict_payload.get("predict_attempts", 0))
            predict_successes = int(predict_payload.get("predict_successes", 0))

    if inject_hook is not None:
        handler = _resolve_handler(_INJECT_HANDLERS, inject_hook, "inject handler")
        exec_inject_used = bool(handler(repo_root, container_name, inject_hook))

    return {
        "inference_log_path": str(inference_log_path),
        "predict_attempts": predict_attempts,
        "predict_successes": predict_successes,
        "exec_inject_used": exec_inject_used,
        "did_io": bool(predict_successes > 0 or exec_inject_used),
    }


def benchmark_hook_parameters(
    *,
    ready_hook: object | None,
    predict_hook: object | None,
    model_mount_hook: object | None,
    model_host_dir: str = "",
    debug_state_url: str = "",
) -> dict[str, str]:
    payload = {
        "ready_url": "",
        "ready_exec": "",
        "ready_log_regex": "",
        "ready_shell": "sh",
        "debug_state_url": debug_state_url or "",
        "predict_url": "",
        "predict_body": "",
        "model_host_dir": model_host_dir or "",
        "model_dst_dir": "",
        "model_name": "",
    }
    for hook in (ready_hook, predict_hook, model_mount_hook):
        if hook is None:
            continue
        builder = _resolve_handler(_BENCHMARK_ARG_BUILDERS, hook, "benchmark arg builder")
        payload.update(builder(hook))
    return payload


def _build_http_ready_args(hook: HttpReadyHook) -> dict[str, str]:
    return {"ready_url": hook.url}


def _build_tcp_ready_args(hook: TcpReadyHook) -> dict[str, str]:
    return {"ready_url": f"tcp://{hook.host}:{hook.port}"}


def _build_exec_ready_args(hook: ExecReadyHook) -> dict[str, str]:
    return {
        "ready_exec": hook.command,
        "ready_shell": hook.shell,
    }


def _build_log_regex_ready_args(hook: LogRegexReadyHook) -> dict[str, str]:
    return {"ready_log_regex": hook.pattern}


def _build_http_predict_args(hook: HttpPredictHook) -> dict[str, str]:
    return {
        "predict_url": hook.url,
        "predict_body": hook.body,
    }


def _build_model_mount_args(hook: ModelMountHook) -> dict[str, str]:
    return {
        "model_dst_dir": hook.dst_dir,
        "model_name": hook.model_name or "",
    }


register_ready_handler(HttpReadyHook, _wait_http_ready_hook, replace=True)
register_ready_handler(TcpReadyHook, _wait_tcp_ready_hook, replace=True)
register_ready_handler(ExecReadyHook, _wait_exec_ready_hook, replace=True)
register_ready_handler(LogRegexReadyHook, _wait_log_regex_ready_hook, replace=True)
register_predict_handler(HttpPredictHook, _run_http_predict_hook, replace=True)
register_inject_handler(ExecInjectHook, _run_exec_inject_hook, replace=True)
register_benchmark_arg_builder(HttpReadyHook, _build_http_ready_args, replace=True)
register_benchmark_arg_builder(TcpReadyHook, _build_tcp_ready_args, replace=True)
register_benchmark_arg_builder(ExecReadyHook, _build_exec_ready_args, replace=True)
register_benchmark_arg_builder(LogRegexReadyHook, _build_log_regex_ready_args, replace=True)
register_benchmark_arg_builder(HttpPredictHook, _build_http_predict_args, replace=True)
register_benchmark_arg_builder(ModelMountHook, _build_model_mount_args, replace=True)
