from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from orchestrator.container_runtime import ContainerRuntimeSpec
from orchestrator.runtime import (
    choose_available_port,
    container_name_for,
    image_entry_config,
    image_supports_shell,
    inspect_container_pid,
    list_listening_pids,
    nerdctl,
)
from orchestrator.workloads.execution import _http_get, run_injection_hooks, wait_for_ready_hook
from orchestrator.workloads.hooks import (
    ExecReadyHook,
    ExecInjectHook,
    HttpPredictHook,
    HttpReadyHook,
    LogRegexReadyHook,
    TcpReadyHook,
    ready_hook_from_value,
)

SELF_STARTING_SERVICE_POST_READY_TRACE_WINDOW_SEC = 5


@dataclass
class SampleResult:
    runtime_state_path: Path
    raw_events_path: Path
    sampler_log_path: Path
    sampler_metrics_path: Path
    container_name: str
    container_pid: int
    startup_mode_used: str
    startup_backend: str
    prom_port: int
    workload_profile: str
    inference_log_path: Path


def _resolve_formal_sampler_binary(repo_root: Path) -> Path:
    override_raw = str(os.environ.get("CPP_SAMPLER_BIN") or "").strip()
    if override_raw:
        override_path = Path(override_raw).expanduser()
        if override_path.is_file() and os.access(override_path, os.X_OK):
            return override_path.resolve()
        raise RuntimeError(
            f"requested sampler binary via CPP_SAMPLER_BIN is unavailable: {override_path}"
        )

    candidates = (
        repo_root / "dist" / "bin" / "sampler_trace_ext",
        repo_root / "sampler" / "user" / "build" / "sampler_trace_ext",
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    searched = ", ".join(str(path) for path in candidates)
    raise RuntimeError(
        "formal sampler binary not found; searched "
        f"{searched}. Run `bash sampler/scripts/build_bpf.sh` first."
    )


def _sampler_launch_env(repo_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    dist_lib_dir = repo_root / "dist" / "lib"
    if dist_lib_dir.is_dir():
        current = str(env.get("LD_LIBRARY_PATH") or "").strip()
        entries = [str(dist_lib_dir.resolve())]
        if current:
            entries.append(current)
        env["LD_LIBRARY_PATH"] = ":".join(entries)
    return env


def _startup_backend_family(startup_backend: str) -> str:
    backend = str(startup_backend or "").strip()
    if backend == "legacy":
        return "legacy"
    if backend == "gated_shell":
        return "gated_shell"
    if backend.startswith("gated_host_launcher"):
        return "host_launcher"
    if backend:
        return backend
    return "unknown"


def _startup_fallback_reason(startup_warnings: list[str]) -> str:
    for item in startup_warnings:
        text = str(item or "").strip()
        prefix = "gated startup unavailable; fallback to legacy: "
        if text.startswith(prefix):
            return text[len(prefix) :].strip()
    return ""


def _normalize_startup_backend_preference(startup_backend_preference: str) -> str:
    value = str(startup_backend_preference or "").strip()
    if value in {"auto", "prefer-host-launcher", "prefer-image-shell"}:
        return value
    return "auto"


def _local_ready_hook_port(
    ready_hook: HttpReadyHook | TcpReadyHook | ExecReadyHook | LogRegexReadyHook | None,
) -> int | None:
    if isinstance(ready_hook, TcpReadyHook):
        if ready_hook.host in {"127.0.0.1", "localhost", "0.0.0.0"}:
            return int(ready_hook.port)
        return None
    if isinstance(ready_hook, HttpReadyHook):
        parsed = urlparse(ready_hook.url)
        host = str(parsed.hostname or "").strip()
        if host not in {"127.0.0.1", "localhost", "0.0.0.0"}:
            return None
        if parsed.port is not None:
            return int(parsed.port)
        if parsed.scheme == "http":
            return 80
        if parsed.scheme == "https":
            return 443
    return None


def _resolve_self_starting_handoff_pid(
    *,
    container_pid: int,
    ready_hook: HttpReadyHook | TcpReadyHook | ExecReadyHook | LogRegexReadyHook | None,
) -> tuple[int, str]:
    ready_port = _local_ready_hook_port(ready_hook)
    if ready_port is None:
        return container_pid, "container_pid"
    listener_pids = [pid for pid in list_listening_pids(ready_port) if pid > 0]
    if container_pid > 0 and container_pid in listener_pids:
        return container_pid, "container_pid"
    if listener_pids:
        return listener_pids[0], "ready_listener"
    return container_pid, "container_pid"


def _resolve_runtime_handoff_pids(
    *,
    container_pid: int,
    live_container_pid: int,
    live_container_pid_source: str,
    startup_backend: str,
) -> tuple[int, str, int, str]:
    root_pid = container_pid if container_pid > 0 else live_container_pid
    root_pid_source = "container_pid" if container_pid > 0 else "live_container_pid"

    target_pid = container_pid
    target_pid_source = "container_pid"
    if live_container_pid > 0:
        if str(startup_backend or "").strip() == "gated_host_launcher_static" and live_container_pid_source == "ready_listener":
            target_pid = container_pid
            target_pid_source = "container_pid"
        else:
            target_pid = live_container_pid
            target_pid_source = "live_container_pid"
    return root_pid, root_pid_source, target_pid, target_pid_source


def _startup_state_payload(
    *,
    startup_mode_requested: str,
    startup_mode_used: str,
    startup_backend: str,
    startup_backend_preference: str,
    startup_backend_selection_reason: str,
    startup_warnings: list[str],
) -> dict:
    backend_family = _startup_backend_family(startup_backend)
    fallback_reason = _startup_fallback_reason(startup_warnings)
    fallback_applied = bool(fallback_reason)
    requested = str(startup_mode_requested or "").strip()
    used = str(startup_mode_used or "").strip()
    preference = _normalize_startup_backend_preference(startup_backend_preference)
    return {
        "requested_mode": requested,
        "used_mode": used,
        "backend": startup_backend,
        "backend_preference": preference,
        "backend_selection_reason": str(startup_backend_selection_reason or "").strip(),
        "backend_family": backend_family,
        "gated_requested": requested in {"auto", "gated"},
        "gated_active": used == "gated",
        "experimental_backend": backend_family == "host_launcher",
        "fallback_applied": fallback_applied,
        "fallback_reason": fallback_reason,
        "warnings": list(startup_warnings),
    }


def _classify_sample_failure(
    *,
    error_message: str,
    recent_container_logs: str,
    startup_mode_used: str,
    startup_backend: str,
) -> dict:
    message = str(error_message or "")
    logs = str(recent_container_logs or "")
    logs_lower = logs.lower()
    backend_family = _startup_backend_family(startup_backend)

    kind = "sample_stage_failure"
    category = "unknown"
    evidence = ""

    if backend_family == "host_launcher":
        if "/__cpp_sampler_gate_launcher" in logs and "no such file or directory" in logs_lower:
            kind = "host_launcher_missing_runtime_dependency"
            category = "startup_attach"
            evidence = "container log reports missing host launcher binary or dynamic loader"
        elif "exec format error" in logs_lower:
            kind = "host_launcher_exec_format_error"
            category = "startup_attach"
            evidence = "container log reports exec format error for host launcher"
        elif "permission denied" in logs_lower:
            kind = "host_launcher_permission_denied"
            category = "startup_attach"
            evidence = "container log reports permission denied when launching host startup helper"
        elif "cannot execute" in logs_lower:
            kind = "host_launcher_exec_failure"
            category = "startup_attach"
            evidence = "container log reports host startup helper could not execute"

    if category == "unknown" and startup_mode_used == "gated" and "before gate release" in logs_lower:
        kind = "gated_container_exited_before_release"
        category = "startup_attach"
        evidence = "container exited before gated startup release file was created"
    elif category == "unknown" and "sampler /metrics did not become ready" in message:
        kind = "sampler_metrics_unavailable"
        category = "sampler_boot"
        evidence = "sampler metrics endpoint never became reachable"
    elif category == "unknown" and "sampler exited early" in message:
        kind = "sampler_early_exit"
        category = "sampler_boot"
        evidence = "sampler process exited before readiness checks completed"
    elif category == "unknown" and "ready probe did not observe service readiness" in message:
        kind = "service_not_ready"
        category = "service_readiness"
        evidence = "service-specific ready hook never observed readiness"

    return {
        "kind": kind,
        "category": category,
        "evidence": evidence,
        "startup_backend_family": backend_family,
    }

def _effective_container_argv(
    *,
    repo_root: Path,
    image: str,
    runtime_spec: ContainerRuntimeSpec,
) -> list[str]:
    entrypoint, image_cmd = image_entry_config(repo_root, image)
    effective_entrypoint = [runtime_spec.entrypoint] if runtime_spec.entrypoint else list(entrypoint)
    effective_cmd = list(runtime_spec.command) if runtime_spec.command else list(image_cmd)
    argv = effective_entrypoint + effective_cmd
    if not argv:
        raise RuntimeError("image has empty Entrypoint+Cmd and no runtime command override")
    return argv


def _prepare_gate_dir(gate_dir: Path) -> None:
    gate_dir.mkdir(parents=True, exist_ok=True)
    release_file = gate_dir / "release"
    if release_file.exists():
        release_file.unlink()


def _prepare_gated_wrapper(
    repo_root: Path,
    image: str,
    gate_dir: Path,
    wrapper_path: Path,
    runtime_spec: ContainerRuntimeSpec,
) -> None:
    _prepare_gate_dir(gate_dir)
    argv = _effective_container_argv(
        repo_root=repo_root,
        image=image,
        runtime_spec=runtime_spec,
    )
    wrapper = [
        "#!/bin/sh",
        "set -eu",
        'RELEASE_FILE="/__cpp_sampler_gate/release"',
        'while [ ! -f "$RELEASE_FILE" ]; do sleep 0.05; done',
        "exec " + " ".join(shlex.quote(arg) for arg in argv),
        "",
    ]
    wrapper_path.write_text("\n".join(wrapper), encoding="utf-8")
    wrapper_path.chmod(0o755)


def _build_startup_gate_launcher(
    *,
    repo_root: Path,
    work_dir: Path,
    allow_dynamic: bool = True,
) -> tuple[Path, str]:
    source_path = repo_root / "orchestrator" / "assets" / "startup_gate_launcher.c"
    if not source_path.exists():
        raise RuntimeError(f"startup gate launcher source is missing: {source_path}")

    preferred_compiler = str(os.environ.get("CPP_SAMPLER_CC") or "").strip()
    if preferred_compiler:
        compiler = ""
        if os.path.isabs(preferred_compiler) and os.access(preferred_compiler, os.X_OK):
            compiler = preferred_compiler
        else:
            compiler = shutil.which(preferred_compiler) or ""
        if not compiler:
            raise RuntimeError(
                "requested host C compiler via CPP_SAMPLER_CC is unavailable: "
                f"{preferred_compiler}"
            )
    else:
        compiler = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
        if not compiler:
            raise RuntimeError("no host C compiler available for gated startup launcher (need cc/gcc/clang)")

    launcher_path = work_dir / "startup_gate_launcher"
    attempts = [(["-O2", "-static", "-s"], "gated_host_launcher_static")]
    if allow_dynamic:
        attempts.append((["-O2", "-s"], "gated_host_launcher_dynamic"))
    errors: list[str] = []

    for flags, backend in attempts:
        proc = subprocess.run(
            [compiler, *flags, "-o", str(launcher_path), str(source_path)],
            cwd=repo_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if proc.returncode == 0 and launcher_path.exists():
            launcher_path.chmod(0o755)
            return launcher_path, backend
        detail = (proc.stderr or proc.stdout or f"exit={proc.returncode}").strip()
        errors.append(f"{backend}: {detail}")

    raise RuntimeError("failed to build host gated startup launcher; " + "; ".join(errors))


def _nerdctl_checked(
    repo_root: Path,
    args: list[str],
    *,
    action: str,
) -> subprocess.CompletedProcess[str]:
    proc = nerdctl(
        repo_root,
        args,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode == 0:
        return proc
    stderr = (proc.stderr or "").strip()
    stdout = (proc.stdout or "").strip()
    detail = stderr or stdout or f"exit code {proc.returncode}"
    raise RuntimeError(f"{action} failed: {detail}")


def _name_conflict_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "name-store error" in text or "is already used by id" in text


def _fallback_container_name(container_name: str) -> str:
    suffix = f"-r{int(time.time())}"
    max_base = max(8, 63 - len(suffix))
    return f"{container_name[:max_base]}{suffix}"


def _launch_container(
    *,
    repo_root: Path,
    image: str,
    run_id: str,
    work_dir: Path,
    startup_mode: str,
    startup_backend_preference: str = "auto",
    runtime_spec: ContainerRuntimeSpec,
) -> tuple[str, int, str, list[str], str, str]:
    warnings: list[str] = []
    container_name = container_name_for(image, run_id)
    gate_dir = work_dir / "startup_gate"
    gate_wrapper = work_dir / "startup_gate.sh"
    startup_backend_preference = _normalize_startup_backend_preference(startup_backend_preference)

    nerdctl(repo_root, ["rm", "-f", container_name], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def start_legacy() -> tuple[int, str, str]:
        nonlocal container_name
        args = [
            "run",
            "-d",
            "--name",
            container_name,
            "--network=host",
            *runtime_spec.to_nerdctl_args(include_entrypoint=True),
            image,
            *runtime_spec.command,
        ]
        _nerdctl_checked(
            repo_root,
            args,
            action="nerdctl run",
        )
        return inspect_container_pid(repo_root, container_name), "legacy", "legacy"

    def start_gated() -> tuple[int, str, str, str]:
        nonlocal container_name
        shell_supported = image_supports_shell(repo_root, image)

        def start_shell() -> tuple[int, str, str]:
            _prepare_gated_wrapper(repo_root, image, gate_dir, gate_wrapper, runtime_spec)
            _nerdctl_checked(
                repo_root,
                [
                    "create",
                    "--name",
                    container_name,
                    "--network=host",
                    *runtime_spec.to_nerdctl_args(include_entrypoint=False),
                    "--mount",
                    f"type=bind,src={gate_dir},dst=/__cpp_sampler_gate,readonly",
                    "--mount",
                    f"type=bind,src={gate_wrapper},dst=/__cpp_sampler_gate.sh,readonly",
                    "--entrypoint",
                    "/bin/sh",
                    image,
                    "/__cpp_sampler_gate.sh",
                ],
                action="nerdctl create",
            )
            backend = "gated_shell"
            _nerdctl_checked(repo_root, ["start", container_name], action="nerdctl start")
            time.sleep(0.2)
            return inspect_container_pid(repo_root, container_name), "gated", backend

        def start_host_launcher(*, allow_dynamic: bool = True, auto_selected: bool = False) -> tuple[int, str, str]:
            argv = _effective_container_argv(
                repo_root=repo_root,
                image=image,
                runtime_spec=runtime_spec,
            )
            _prepare_gate_dir(gate_dir)
            launcher_path, backend = _build_startup_gate_launcher(
                repo_root=repo_root,
                work_dir=work_dir,
                allow_dynamic=allow_dynamic,
            )
            if backend == "gated_host_launcher_dynamic":
                warnings.append(
                    "gated startup uses experimental host launcher backend (dynamic); container runtime libc compatibility is required"
                )
            elif auto_selected:
                warnings.append(
                    "gated startup auto-selected host launcher backend (static) before image-shell gating"
                )
            else:
                warnings.append(
                    "gated startup uses host launcher backend (static)"
                )
            _nerdctl_checked(
                repo_root,
                [
                    "create",
                    "--name",
                    container_name,
                    "--network=host",
                    *runtime_spec.to_nerdctl_args(include_entrypoint=False),
                    "--mount",
                    f"type=bind,src={gate_dir},dst=/__cpp_sampler_gate,readonly",
                    "--mount",
                    f"type=bind,src={launcher_path},dst=/__cpp_sampler_gate_launcher,readonly",
                    "--entrypoint",
                    "/__cpp_sampler_gate_launcher",
                    image,
                    "/__cpp_sampler_gate/release",
                    *argv,
                ],
                action="nerdctl create",
            )
            _nerdctl_checked(repo_root, ["start", container_name], action="nerdctl start")
            time.sleep(0.2)
            return inspect_container_pid(repo_root, container_name), "gated", backend

        if startup_backend_preference == "prefer-host-launcher":
            try:
                pid, mode_used, backend = start_host_launcher(allow_dynamic=True)
                return pid, mode_used, backend, "explicit preference selected host launcher before image-shell gating"
            except Exception as exc:
                if shell_supported:
                    warnings.append(
                        f"preferred host launcher backend unavailable; fallback to image-shell gated startup: {exc}"
                    )
                    pid, mode_used, backend = start_shell()
                    return pid, mode_used, backend, "explicit host-launcher preference fell back to image-shell gating"
                raise

        if startup_backend_preference == "prefer-image-shell":
            if shell_supported:
                pid, mode_used, backend = start_shell()
                return pid, mode_used, backend, "explicit preference kept image-shell gating"
            pid, mode_used, backend = start_host_launcher(allow_dynamic=True)
            return pid, mode_used, backend, "image lacks /bin/sh; host launcher is the only gated startup backend"

        if shell_supported:
            try:
                pid, mode_used, backend = start_host_launcher(allow_dynamic=False, auto_selected=True)
                return pid, mode_used, backend, "auto selected static host launcher before image-shell gating"
            except Exception:
                pid, mode_used, backend = start_shell()
                return pid, mode_used, backend, "auto kept image-shell gating because static host launcher is unavailable"
        pid, mode_used, backend = start_host_launcher(allow_dynamic=True)
        return pid, mode_used, backend, "image lacks /bin/sh; host launcher is the only gated startup backend"

    def run_with_name_conflict_retry(
        launcher,
        *,
        startup_label: str,
    ):
        nonlocal container_name
        try:
            return launcher()
        except RuntimeError as exc:
            if not _name_conflict_error(exc):
                raise
            fallback_name = _fallback_container_name(container_name)
            warnings.append(
                f"{startup_label} container name conflict on {container_name}; retry with {fallback_name}"
            )
            container_name = fallback_name
            nerdctl(
                repo_root,
                ["rm", "-f", container_name],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return launcher()

    if startup_mode == "legacy":
        pid, mode_used, startup_backend = run_with_name_conflict_retry(start_legacy, startup_label="legacy")
        startup_backend_selection_reason = "legacy startup mode requested"
    elif startup_mode == "gated":
        pid, mode_used, startup_backend, startup_backend_selection_reason = run_with_name_conflict_retry(
            start_gated,
            startup_label="gated",
        )
    else:
        try:
            pid, mode_used, startup_backend, startup_backend_selection_reason = run_with_name_conflict_retry(
                start_gated,
                startup_label="gated",
            )
        except Exception as exc:
            warnings.append(f"gated startup unavailable; fallback to legacy: {exc}")
            pid, mode_used, startup_backend = run_with_name_conflict_retry(start_legacy, startup_label="legacy")
            startup_backend_selection_reason = "auto startup mode fell back to legacy because gated startup was unavailable"

    return container_name, pid, mode_used, warnings, startup_backend, startup_backend_selection_reason


def _release_gate(work_dir: Path) -> None:
    release_file = work_dir / "startup_gate" / "release"
    release_file.parent.mkdir(parents=True, exist_ok=True)
    release_file.write_text("", encoding="utf-8")


def _stop_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _snapshot_metrics(prom_port: int, out_path: Path) -> bool:
    try:
        data = _http_get(f"http://127.0.0.1:{prom_port}/metrics", timeout=2.0)
    except Exception:
        out_path.write_text("", encoding="utf-8")
        return False
    out_path.write_bytes(data)
    return True


def _container_log_tail(
    *,
    repo_root: Path,
    container_name: str,
    tail_lines: int = 40,
) -> str:
    proc = nerdctl(
        repo_root,
        ["logs", "--tail", str(tail_lines), container_name],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    text = (proc.stdout or "").strip()
    return text


def _has_usable_sample_outputs(*, raw_events_path: Path, sampler_metrics_path: Path) -> bool:
    try:
        if raw_events_path.exists() and raw_events_path.stat().st_size > 0:
            return True
    except OSError:
        pass
    try:
        if sampler_metrics_path.exists() and sampler_metrics_path.stat().st_size > 0:
            return True
    except OSError:
        pass
    return False


def _startup_context(
    *,
    startup_mode_used: str,
    startup_backend: str,
    container_name: str,
    container_pid: int,
) -> str:
    parts: list[str] = []
    if startup_mode_used:
        parts.append(f"startup_mode={startup_mode_used}")
    if startup_backend:
        parts.append(f"startup_backend={startup_backend}")
    if container_name:
        parts.append(f"container={container_name}")
    if container_pid > 0:
        parts.append(f"pid={container_pid}")
    return ", ".join(parts)


def _failure_message(
    *,
    base: str,
    repo_root: Path,
    container_name: str,
    container_pid: int,
    startup_mode_used: str,
    startup_backend: str,
) -> tuple[str, str]:
    message = base
    context = _startup_context(
        startup_mode_used=startup_mode_used,
        startup_backend=startup_backend,
        container_name=container_name,
        container_pid=container_pid,
    )
    if context:
        message += f" ({context})"
    log_tail = ""
    if container_name:
        try:
            log_tail = _container_log_tail(
                repo_root=repo_root,
                container_name=container_name,
            )
        except Exception:
            log_tail = ""
    if log_tail:
        message += f"; recent container logs:\n{log_tail}"
    return message, log_tail


def _write_failure_runtime_state(
    *,
    runtime_state_path: Path,
    raw_events_path: Path,
    sampler_log_path: Path,
    sampler_metrics_path: Path,
    startup_mode_requested: str,
    startup_mode_used: str,
    startup_backend: str,
    startup_backend_preference: str,
    startup_backend_selection_reason: str,
    workload_profile: str,
    self_starting_service: bool,
    prom_port: int,
    effective_container_runtime: ContainerRuntimeSpec,
    container_name: str,
    container_pid: int,
    ready_url: str | None,
    ready_timeout: int,
    inject_io: bool,
    predict_url: str | None,
    predict_body: str | None,
    inject_io_cmd: str | None,
    startup_warnings: list[str],
    error_message: str,
    recent_container_logs: str,
) -> None:
    startup_state = _startup_state_payload(
        startup_mode_requested=startup_mode_requested,
        startup_mode_used=startup_mode_used,
        startup_backend=startup_backend,
        startup_backend_preference=startup_backend_preference,
        startup_backend_selection_reason=startup_backend_selection_reason,
        startup_warnings=startup_warnings,
    )
    failure_classification = _classify_sample_failure(
        error_message=error_message,
        recent_container_logs=recent_container_logs,
        startup_mode_used=startup_mode_used,
        startup_backend=startup_backend,
    )
    runtime_state = {
        "status": "failed",
        "container_name": container_name,
        "container_pid": container_pid,
        "startup_mode_requested": startup_mode_requested,
        "startup_mode_used": startup_mode_used,
        "startup_backend": startup_backend,
        "startup_backend_preference": startup_backend_preference,
        "startup_backend_selection_reason": startup_backend_selection_reason,
        "startup": startup_state,
        "workload_profile": workload_profile,
        "self_starting_service": self_starting_service,
        "prom_port": prom_port,
        "container_runtime": effective_container_runtime.summary(),
        "raw_events": str(raw_events_path),
        "sampler_log": str(sampler_log_path),
        "sampler_metrics": str(sampler_metrics_path),
        "ready_url": ready_url,
        "ready_timeout_sec": ready_timeout,
        "ready_observed": False,
        "inject_io_enabled": inject_io,
        "predict_url": predict_url,
        "predict_body_present": bool(predict_body),
        "inject_io_cmd_present": bool(inject_io_cmd),
        "event_sink": "file_sink",
        "warnings": list(startup_warnings),
        "failure": {
            "error": error_message,
            "recent_container_logs": recent_container_logs,
            **failure_classification,
        },
    }
    runtime_state_path.write_text(
        json.dumps(runtime_state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def run_workload_injection(
    *,
    repo_root: Path,
    work_dir: Path,
    container_name: str,
    predict_url: str | None,
    predict_body: str | None,
    inject_io_cmd: str | None,
) -> dict[str, object]:
    return run_injection_hooks(
        repo_root=repo_root,
        work_dir=work_dir,
        container_name=container_name,
        predict_hook=HttpPredictHook(predict_url, predict_body) if predict_url and predict_body else None,
        inject_hook=ExecInjectHook(inject_io_cmd) if inject_io_cmd else None,
    )


def run_sample_stage(
    *,
    repo_root: Path,
    work_dir: Path,
    image: str,
    run_id: str,
    startup_mode: str,
    startup_backend_preference: str = "auto",
    duration: int,
    prom_port_start: int,
    mmap_window: int,
    cg_refresh: int,
    ready_url: str | None,
    ready_timeout: int,
    workload_profile: str = "none",
    self_starting_service: bool = False,
    workload_profile_capabilities: tuple[str, ...] = (),
    workload_effective_capabilities: tuple[str, ...] = (),
    workload_profile_hooks: dict[str, dict[str, object] | None] | None = None,
    workload_effective_hooks: dict[str, dict[str, object] | None] | None = None,
    ready_hook: HttpReadyHook | TcpReadyHook | ExecReadyHook | LogRegexReadyHook | None = None,
    predict_hook: HttpPredictHook | None = None,
    inject_hook: ExecInjectHook | None = None,
    container_runtime: ContainerRuntimeSpec | None = None,
    inject_io: bool = False,
    predict_url: str | None = None,
    predict_body: str | None = None,
    inject_io_cmd: str | None = None,
) -> SampleResult:
    raw_events_path = work_dir / "raw_events.jsonl"
    sampler_log_path = work_dir / "sampler.log"
    sampler_metrics_path = work_dir / "sampler_metrics.prom"
    runtime_state_path = work_dir / "runtime_state.json"
    inference_log_path = work_dir / "inference.jsonl"
    sampler_bin = _resolve_formal_sampler_binary(repo_root)
    effective_ready_hook = ready_hook or (ready_hook_from_value(ready_url) if ready_url else None)

    prom_port = choose_available_port(prom_port_start)
    reserved_ready_port = _local_ready_hook_port(effective_ready_hook)
    if reserved_ready_port is not None and prom_port == reserved_ready_port:
        prom_port = choose_available_port(prom_port + 1)
    raw_events_path.write_text("", encoding="utf-8")

    container_name = ""
    container_pid = 0
    startup_mode_requested = startup_mode
    startup_mode_for_launch = (
        "legacy" if startup_mode_requested == "auto" and self_starting_service else startup_mode_requested
    )
    startup_mode_used = startup_mode_for_launch
    startup_backend = "unknown"
    startup_backend_selection_reason = ""
    startup_warnings: list[str] = []
    sampler_proc: subprocess.Popen[str] | None = None
    sampler_log_fh = None
    effective_container_runtime = container_runtime or ContainerRuntimeSpec()
    startup_backend_preference = _normalize_startup_backend_preference(startup_backend_preference)
    post_ready_duration = duration

    try:
        (
            container_name,
            container_pid,
            startup_mode_used,
            startup_warnings,
            startup_backend,
            startup_backend_selection_reason,
        ) = _launch_container(
            repo_root=repo_root,
            image=image,
            run_id=run_id,
            work_dir=work_dir,
            startup_mode=startup_mode_for_launch,
            startup_backend_preference=startup_backend_preference,
            runtime_spec=effective_container_runtime,
        )
        if (
            startup_mode_requested == "auto"
            and self_starting_service
            and startup_mode_used == "legacy"
            and startup_backend == "legacy"
        ):
            startup_backend_selection_reason = (
                "auto startup mode selected legacy because workload is a self-starting service"
            )

        sampler_log_fh = sampler_log_path.open("a", encoding="utf-8")
        sampler_proc = subprocess.Popen(
            [
                str(sampler_bin),
                "--pid",
                str(container_pid),
                "--prom-port",
                str(prom_port),
                "--sampling-rate",
                "100",
                "--mmap-window",
                str(mmap_window),
                "--cg-refresh",
                str(cg_refresh),
                "--file-sink",
                str(raw_events_path),
            ],
            cwd=repo_root,
            env=_sampler_launch_env(repo_root),
            text=True,
            stdout=sampler_log_fh,
            stderr=subprocess.STDOUT,
        )

        metrics_ready = False
        for _ in range(60):
            if sampler_proc.poll() is not None:
                detail, _ = _failure_message(
                    base=f"sampler exited early, see {sampler_log_path}",
                    repo_root=repo_root,
                    container_name=container_name,
                    container_pid=container_pid,
                    startup_mode_used=startup_mode_used,
                    startup_backend=startup_backend,
                )
                raise RuntimeError(detail)
            try:
                _http_get(f"http://127.0.0.1:{prom_port}/metrics", timeout=1.0)
                metrics_ready = True
                break
            except Exception:
                time.sleep(1)
        if not metrics_ready:
            detail, _ = _failure_message(
                base=f"sampler /metrics did not become ready on port {prom_port}",
                repo_root=repo_root,
                container_name=container_name,
                container_pid=container_pid,
                startup_mode_used=startup_mode_used,
                startup_backend=startup_backend,
            )
            raise RuntimeError(detail)

        if startup_mode_used == "gated":
            _release_gate(work_dir)

        effective_predict_hook = predict_hook or (HttpPredictHook(predict_url, predict_body) if predict_url and predict_body else None)
        effective_inject_hook = inject_hook or (ExecInjectHook(inject_io_cmd) if inject_io_cmd else None)
        ready_observed = wait_for_ready_hook(
            effective_ready_hook,
            ready_timeout,
            repo_root=repo_root,
            container_name=container_name,
        )
        if effective_ready_hook is not None and not ready_observed:
            detail, _ = _failure_message(
                base=f"ready probe did not observe service readiness within {ready_timeout}s",
                repo_root=repo_root,
                container_name=container_name,
                container_pid=container_pid,
                startup_mode_used=startup_mode_used,
                startup_backend=startup_backend,
            )
            raise RuntimeError(detail)

        injection_summary = {
            "inference_log_path": str(inference_log_path),
            "predict_attempts": 0,
            "predict_successes": 0,
            "exec_inject_used": False,
            "did_io": False,
        }
        if inject_io:
            injection_summary = run_injection_hooks(
                repo_root=repo_root,
                work_dir=work_dir,
                container_name=container_name,
                predict_hook=effective_predict_hook,
                inject_hook=effective_inject_hook,
            )
        elif self_starting_service:
            post_ready_duration = min(duration, SELF_STARTING_SERVICE_POST_READY_TRACE_WINDOW_SEC)

        time.sleep(post_ready_duration)
        _snapshot_metrics(prom_port, sampler_metrics_path)
        _stop_process(sampler_proc)
        sampler_proc = None
        sampler_log_fh.close()
        sampler_log_fh = None
        if not _has_usable_sample_outputs(
            raw_events_path=raw_events_path,
            sampler_metrics_path=sampler_metrics_path,
        ):
            raise RuntimeError("sample stage produced no usable output artifacts")

        live_container_pid = container_pid
        live_container_pid_source = "container_pid"
        if self_starting_service:
            live_container_pid, live_container_pid_source = _resolve_self_starting_handoff_pid(
                container_pid=container_pid,
                ready_hook=effective_ready_hook,
            )
        root_pid, root_pid_source, target_pid, target_pid_source = _resolve_runtime_handoff_pids(
            container_pid=container_pid,
            live_container_pid=live_container_pid,
            live_container_pid_source=live_container_pid_source,
            startup_backend=startup_backend,
        )

        runtime_state = {
            "container_name": container_name,
            "container_pid": container_pid,
            "live_container_pid": live_container_pid,
            "live_container_pid_source": live_container_pid_source,
            "root_pid": root_pid,
            "root_pid_source": root_pid_source,
            "target_pid": target_pid,
            "target_pid_source": target_pid_source,
            "startup_mode_requested": startup_mode_requested,
            "startup_mode_used": startup_mode_used,
            "startup_backend": startup_backend,
            "startup_backend_preference": startup_backend_preference,
            "startup_backend_selection_reason": startup_backend_selection_reason,
            "startup": _startup_state_payload(
                startup_mode_requested=startup_mode_requested,
                startup_mode_used=startup_mode_used,
                startup_backend=startup_backend,
                startup_backend_preference=startup_backend_preference,
                startup_backend_selection_reason=startup_backend_selection_reason,
                startup_warnings=startup_warnings,
            ),
            "workload_profile": workload_profile,
            "self_starting_service": self_starting_service,
            "workload_profile_capabilities": list(workload_profile_capabilities),
            "workload_effective_capabilities": list(workload_effective_capabilities),
            "workload_profile_hooks": dict(workload_profile_hooks or {}),
            "workload_effective_hooks": dict(workload_effective_hooks or {}),
            "prom_port": prom_port,
            "container_runtime": effective_container_runtime.summary(),
            "raw_events": str(raw_events_path),
            "sampler_log": str(sampler_log_path),
            "sampler_metrics": str(sampler_metrics_path),
            "ready_url": ready_url,
            "ready_timeout_sec": ready_timeout,
            "ready_observed": ready_observed,
            "inject_io_enabled": inject_io,
            "predict_url": predict_url,
            "predict_body_present": bool(predict_body),
            "inject_io_cmd_present": bool(inject_io_cmd),
            "inference_log": injection_summary["inference_log_path"],
            "injection": injection_summary,
            "event_sink": "file_sink",
            "warnings": startup_warnings,
        }
        runtime_state_path.write_text(
            json.dumps(runtime_state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return SampleResult(
            runtime_state_path=runtime_state_path,
            raw_events_path=raw_events_path,
            sampler_log_path=sampler_log_path,
            sampler_metrics_path=sampler_metrics_path,
            container_name=container_name,
            container_pid=container_pid,
            startup_mode_used=startup_mode_used,
            startup_backend=startup_backend,
            prom_port=prom_port,
            workload_profile=workload_profile,
            inference_log_path=inference_log_path,
        )
    except Exception as exc:
        _, recent_container_logs = _failure_message(
            base="sample stage failure",
            repo_root=repo_root,
            container_name=container_name,
            container_pid=container_pid,
            startup_mode_used=startup_mode_used,
            startup_backend=startup_backend,
        )
        _write_failure_runtime_state(
            runtime_state_path=runtime_state_path,
            raw_events_path=raw_events_path,
            sampler_log_path=sampler_log_path,
            sampler_metrics_path=sampler_metrics_path,
            startup_mode_requested=startup_mode_requested,
            startup_mode_used=startup_mode_used,
            startup_backend=startup_backend,
            startup_backend_preference=startup_backend_preference,
            startup_backend_selection_reason=startup_backend_selection_reason,
            workload_profile=workload_profile,
            self_starting_service=self_starting_service,
            prom_port=prom_port,
            effective_container_runtime=effective_container_runtime,
            container_name=container_name,
            container_pid=container_pid,
            ready_url=ready_url,
            ready_timeout=ready_timeout,
            inject_io=inject_io,
            predict_url=predict_url,
            predict_body=predict_body,
            inject_io_cmd=inject_io_cmd,
            startup_warnings=startup_warnings,
            error_message=str(exc),
            recent_container_logs=recent_container_logs,
        )
        if sampler_proc is not None:
            _stop_process(sampler_proc)
        if sampler_log_fh is not None:
            sampler_log_fh.close()
        if container_name:
            nerdctl(repo_root, ["stop", container_name], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            nerdctl(repo_root, ["rm", container_name], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        raise
