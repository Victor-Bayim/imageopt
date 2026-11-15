from __future__ import annotations

from dataclasses import dataclass

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

CAP_READY_PROBE = "ready_probe"
CAP_HTTP_PREDICT = "http_predict"
CAP_EXEC_INJECT = "exec_inject"
CAP_MODEL_MOUNT = "model_mount"

KNOWN_WORKLOAD_CAPABILITIES = (
    CAP_READY_PROBE,
    CAP_HTTP_PREDICT,
    CAP_EXEC_INJECT,
    CAP_MODEL_MOUNT,
)


@dataclass(frozen=True)
class WorkloadProfile:
    name: str
    description: str
    self_starting_service: bool = False
    ready_url: str | None = None
    debug_state_url: str | None = None
    predict_url: str | None = None
    predict_body: str | None = None
    inject_io_cmd: str | None = None
    model_dst_dir: str | None = None
    model_name: str | None = None
    sample_ready_timeout: int = 30
    benchmark_timeout: int = 180
    capabilities: tuple[str, ...] | None = None
    ready_hook: HttpReadyHook | TcpReadyHook | ExecReadyHook | LogRegexReadyHook | None = None
    predict_hook: HttpPredictHook | None = None
    inject_hook: ExecInjectHook | None = None
    model_mount_hook: ModelMountHook | None = None

    def __post_init__(self) -> None:
        if self.ready_hook is not None and self.ready_url:
            parsed_ready_hook = ready_hook_from_value(self.ready_url)
            if self.ready_hook.summary() != parsed_ready_hook.summary():
                raise ValueError(f"ready hook/url mismatch for {self.name}")
        if self.predict_hook is not None:
            if self.predict_url and self.predict_hook.url != self.predict_url:
                raise ValueError(f"predict hook/url mismatch for {self.name}")
            if self.predict_body and self.predict_hook.body != self.predict_body:
                raise ValueError(f"predict hook/body mismatch for {self.name}")
        if self.inject_hook is not None and self.inject_io_cmd and self.inject_hook.command != self.inject_io_cmd:
            raise ValueError(f"inject hook/command mismatch for {self.name}")
        if self.model_mount_hook is not None:
            if self.model_dst_dir and self.model_mount_hook.dst_dir != self.model_dst_dir:
                raise ValueError(f"model mount hook/dst mismatch for {self.name}")
            if self.model_name and self.model_mount_hook.model_name != self.model_name:
                raise ValueError(f"model mount hook/name mismatch for {self.name}")

        ready_hook = self.ready_hook or (ready_hook_from_value(self.ready_url) if self.ready_url else None)
        predict_hook = self.predict_hook or (
            HttpPredictHook(self.predict_url, self.predict_body)
            if self.predict_url and self.predict_body
            else None
        )
        inject_hook = self.inject_hook or (ExecInjectHook(self.inject_io_cmd) if self.inject_io_cmd else None)
        model_mount_hook = self.model_mount_hook or (
            ModelMountHook(self.model_dst_dir or "", self.model_name)
            if self.model_dst_dir or self.model_name
            else None
        )
        object.__setattr__(self, "ready_hook", ready_hook)
        object.__setattr__(self, "predict_hook", predict_hook)
        object.__setattr__(self, "inject_hook", inject_hook)
        object.__setattr__(self, "model_mount_hook", model_mount_hook)

        if self.capabilities is None:
            inferred: list[str] = []
            if ready_hook is not None:
                inferred.append(CAP_READY_PROBE)
            if predict_hook is not None:
                inferred.append(CAP_HTTP_PREDICT)
            if inject_hook is not None:
                inferred.append(CAP_EXEC_INJECT)
            if model_mount_hook is not None:
                inferred.append(CAP_MODEL_MOUNT)
            capabilities = tuple(inferred)
        else:
            capabilities = tuple(dict.fromkeys(self.capabilities))
        unknown = sorted(set(capabilities) - set(KNOWN_WORKLOAD_CAPABILITIES))
        if unknown:
            raise ValueError(f"unknown workload capabilities for {self.name}: {', '.join(unknown)}")
        object.__setattr__(self, "capabilities", capabilities)

    def hook_summaries(self) -> dict[str, dict[str, object] | None]:
        return summarize_hook_set(
            ready_hook=self.ready_hook,
            predict_hook=self.predict_hook,
            inject_hook=self.inject_hook,
            model_mount_hook=self.model_mount_hook,
        )
