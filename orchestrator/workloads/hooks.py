from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class HttpReadyHook:
    url: str

    def summary(self) -> dict[str, object]:
        return {
            "kind": "http_ready",
            "url": self.url,
        }


@dataclass(frozen=True)
class TcpReadyHook:
    host: str
    port: int

    def summary(self) -> dict[str, object]:
        return {
            "kind": "tcp_ready",
            "host": self.host,
            "port": self.port,
            "url": f"tcp://{self.host}:{self.port}",
        }


@dataclass(frozen=True)
class ExecReadyHook:
    command: str
    shell: str = "sh"

    def summary(self) -> dict[str, object]:
        return {
            "kind": "exec_ready",
            "shell": self.shell,
            "command_present": bool(self.command),
        }


@dataclass(frozen=True)
class LogRegexReadyHook:
    pattern: str

    def summary(self) -> dict[str, object]:
        return {
            "kind": "log_regex_ready",
            "pattern": self.pattern,
        }


@dataclass(frozen=True)
class HttpPredictHook:
    url: str
    body: str
    attempts: int = 3

    def summary(self) -> dict[str, object]:
        return {
            "kind": "http_predict",
            "url": self.url,
            "body_present": bool(self.body),
            "attempts": self.attempts,
        }


@dataclass(frozen=True)
class ExecInjectHook:
    command: str
    shell: str = "sh"

    def summary(self) -> dict[str, object]:
        return {
            "kind": "exec_inject",
            "shell": self.shell,
            "command_present": bool(self.command),
        }


@dataclass(frozen=True)
class ModelMountHook:
    dst_dir: str
    model_name: str | None = None
    model_base_path_env: str | None = None

    def summary(self) -> dict[str, object]:
        return {
            "kind": "model_mount",
            "dst_dir": self.dst_dir,
            "model_name": self.model_name,
            "model_base_path_env": self.model_base_path_env,
        }


def ready_hook_from_value(value: str) -> HttpReadyHook | TcpReadyHook:
    parsed = urlparse(value)
    if parsed.scheme == "tcp":
        host = parsed.hostname
        port = parsed.port
        if not host or port is None:
            raise ValueError(f"invalid tcp ready hook: {value}")
        return TcpReadyHook(host=host, port=port)
    return HttpReadyHook(value)


def summarize_hook_set(
    *,
    ready_hook: HttpReadyHook | TcpReadyHook | ExecReadyHook | LogRegexReadyHook | None = None,
    predict_hook: HttpPredictHook | None = None,
    inject_hook: ExecInjectHook | None = None,
    model_mount_hook: ModelMountHook | None = None,
) -> dict[str, dict[str, object] | None]:
    return {
        "ready": None if ready_hook is None else ready_hook.summary(),
        "predict": None if predict_hook is None else predict_hook.summary(),
        "inject": None if inject_hook is None else inject_hook.summary(),
        "model_mount": None if model_mount_hook is None else model_mount_hook.summary(),
    }
