from __future__ import annotations

import csv
import json
import re
import statistics
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from orchestrator.container_runtime import ContainerRuntimeSpec, inherit_container_runtime
from orchestrator.policy import evaluate_benchmark_policy
from orchestrator.stages.envcheck import run_check_env_json, write_env_check_payload
from orchestrator.workloads.execution import benchmark_hook_parameters
from orchestrator.workloads.hooks import (
    ExecReadyHook,
    HttpPredictHook,
    HttpReadyHook,
    LogRegexReadyHook,
    ModelMountHook,
    TcpReadyHook,
    ready_hook_from_value,
)


@dataclass
class BenchmarkResult:
    env_check_base_path: Path
    env_check_opt_path: Path
    policy_path: Path
    stage_log_path: Path
    csv_path: Path
    log_dir: Path
    result_path: Path
    base_image: str
    opt_image: str
    workload_profile: str
    valid: bool
    invalid_reasons: list[str]
    warnings: list[str]
    summary_payload: dict
    strict_input_path: Path | None = None
    strict_proof_path: Path | None = None


@dataclass(frozen=True)
class StrictRegistryRef:
    requested: str
    registry: str
    repository: str
    tag: str | None
    digest: str | None
    normalized_ref: str
    comparison_key: str
    insecure_registry: bool

    def repository_ref(self) -> str:
        return f"{self.registry}/{self.repository}"

    def with_digest(self, digest: str) -> str:
        return f"{self.repository_ref()}@{digest}"


@dataclass(frozen=True)
class RegistryContentClosure:
    requested_ref: str
    normalized_requested_ref: str
    resolved_ref: str
    root_digest: str | None
    manifest_digest: str
    config_digest: str
    layer_digests: tuple[str, ...]
    closure_digests: tuple[str, ...]

    def as_payload(self) -> dict[str, object]:
        return {
            "requested_ref": self.requested_ref,
            "normalized_requested_ref": self.normalized_requested_ref,
            "resolved_ref": self.resolved_ref,
            "root_digest": self.root_digest,
            "manifest_digest": self.manifest_digest,
            "config_digest": self.config_digest,
            "layer_digests": list(self.layer_digests),
            "closure_digests": list(self.closure_digests),
        }


_DIGEST_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+.-]*:[0-9A-Fa-f]{32,}$")
_BENCHMARK_COMPARISON_MODES = (
    "same-snapshotter",
    "raw-vs-estargz",
    "raw-full-vs-estargz-lazy",
    "raw-full-vs-estargz-lazy-triple",
)


def _comparison_mode_labels(comparison_mode: str) -> tuple[str, ...]:
    if comparison_mode == "raw-full-vs-estargz-lazy-triple":
        return ("baseline", "unoptimized_lazy", "optimized")
    return ("baseline", "optimized")


def _lazy_labels_for_comparison_mode(comparison_mode: str) -> tuple[str, ...]:
    if comparison_mode == "raw-full-vs-estargz-lazy-triple":
        return ("unoptimized_lazy", "optimized")
    if comparison_mode == "raw-full-vs-estargz-lazy":
        return ("optimized",)
    return ()


def _lazy_full_pull_warning_for_label(label: str) -> str:
    return f"{label} path appears to have performed a full pull; lazy-pull result may be invalid"


def _lazy_full_pull_invalid_reason_for_label(label: str) -> str:
    return f"{label} lazy path appears to have performed a full pull; result is invalid"


def _unique_preserving_order(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in values:
        if not item or item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return tuple(ordered)


def parse_strict_registry_ref(ref: str) -> StrictRegistryRef:
    raw = str(ref or "").strip()
    if not raw:
        raise ValueError("registry-cold-strict requires a non-empty image reference")
    if raw.count("@") > 1:
        raise ValueError(f"invalid image reference for registry-cold-strict: {raw}")

    name_part, digest = (raw.split("@", 1) + [None])[:2] if "@" in raw else (raw, None)
    if digest is not None and not _DIGEST_RE.fullmatch(digest):
        raise ValueError(f"registry-cold-strict requires a valid digest ref when '@' is present: {raw}")

    if "/" not in name_part:
        raise ValueError(
            "registry-cold-strict requires an explicit registry authority (for example docker.io/... or localhost:5001/...), not a bare local tag"
        )
    first_component, remainder = name_part.split("/", 1)
    if not remainder:
        raise ValueError(f"registry-cold-strict requires a repository path after the registry authority: {raw}")
    if "." not in first_component and ":" not in first_component and first_component != "localhost":
        raise ValueError(
            "registry-cold-strict requires an explicit registry authority (host[:port]/repo...), not an implicit local/tag-only reference"
        )
    if ":" in remainder.rsplit("/", 1)[-1]:
        repository, tag = remainder.rsplit(":", 1)
        if not tag:
            raise ValueError(f"registry-cold-strict requires a non-empty tag when ':' is present: {raw}")
    else:
        repository, tag = remainder, None
    if not repository:
        raise ValueError(f"registry-cold-strict requires a non-empty repository path: {raw}")
    normalized_registry = first_component.lower()
    normalized_repository = repository.lower()
    normalized_ref = f"{normalized_registry}/{normalized_repository}"
    if tag:
        normalized_ref += f":{tag}"
    if digest:
        normalized_ref += f"@{digest}"
    host_name = normalized_registry.split(":", 1)[0]
    insecure_registry = host_name == "localhost" or host_name.startswith("127.")
    return StrictRegistryRef(
        requested=raw,
        registry=normalized_registry,
        repository=normalized_repository,
        tag=tag,
        digest=digest,
        normalized_ref=normalized_ref,
        comparison_key=f"{normalized_registry}/{normalized_repository}",
        insecure_registry=insecure_registry,
    )


def validate_registry_cold_strict_pair(*, base_image: str, opt_image: str) -> tuple[StrictRegistryRef, StrictRegistryRef]:
    base_ref = parse_strict_registry_ref(base_image)
    opt_ref = parse_strict_registry_ref(opt_image)
    if base_ref.comparison_key != opt_ref.comparison_key:
        raise ValueError("registry-cold-strict requires baseline and optimized images to share the same registry/repository")
    return base_ref, opt_ref


def validate_registry_cold_strict_triple(
    *,
    base_image: str,
    unopt_image: str,
    opt_image: str,
) -> tuple[StrictRegistryRef, StrictRegistryRef, StrictRegistryRef]:
    base_ref = parse_strict_registry_ref(base_image)
    unopt_ref = parse_strict_registry_ref(unopt_image)
    opt_ref = parse_strict_registry_ref(opt_image)
    if base_ref.comparison_key != unopt_ref.comparison_key:
        raise ValueError("registry-cold-strict requires baseline and unoptimized images to share the same registry/repository")
    if unopt_ref.comparison_key != opt_ref.comparison_key:
        raise ValueError("registry-cold-strict requires unoptimized and optimized images to share the same registry/repository")
    return base_ref, unopt_ref, opt_ref


def _parse_platform(platform: str) -> tuple[str, str, str | None]:
    normalized = str(platform or "").strip()
    parts = normalized.split("/")
    if len(parts) not in {2, 3} or not parts[0] or not parts[1]:
        raise ValueError(f"unsupported platform for registry-cold-strict: {platform}")
    return parts[0], parts[1], parts[2] if len(parts) == 3 and parts[2] else None


def _manifest_document_from_inspect_payload(payload: dict) -> tuple[dict | None, dict | None]:
    descriptor = payload.get("Descriptor")
    if not isinstance(descriptor, dict):
        descriptor = payload.get("descriptor") if isinstance(payload.get("descriptor"), dict) else None
    for key in ("OCIManifest", "SchemaV2Manifest", "manifest"):
        document = payload.get(key)
        if isinstance(document, dict):
            return descriptor, document
    if any(key in payload for key in ("manifests", "config", "layers", "mediaType", "schemaVersion")):
        return descriptor, payload
    return descriptor, None


def _descriptor_digest(descriptor: dict | None) -> str | None:
    if not isinstance(descriptor, dict):
        return None
    digest = str(descriptor.get("digest") or "").strip()
    return digest or None


def _select_platform_manifest(index_document: dict, *, platform: str) -> str:
    manifests = list(index_document.get("manifests") or [])
    if not manifests:
        raise ValueError("registry-cold-strict could not find any manifests in the image index")
    os_name, arch, variant = _parse_platform(platform)
    candidates: list[dict] = []
    for item in manifests:
        if not isinstance(item, dict):
            continue
        manifest_platform = item.get("platform") if isinstance(item.get("platform"), dict) else {}
        if str(manifest_platform.get("os") or "") != os_name:
            continue
        if str(manifest_platform.get("architecture") or "") != arch:
            continue
        manifest_variant = str(manifest_platform.get("variant") or "") or None
        if variant is not None and manifest_variant != variant:
            continue
        candidates.append(item)
    if variant is None and len(candidates) > 1:
        no_variant = [item for item in candidates if not str(((item.get("platform") or {}).get("variant") or "")).strip()]
        if len(no_variant) == 1:
            candidates = no_variant
    if len(candidates) != 1:
        raise ValueError(
            f"registry-cold-strict requires exactly one manifest match for platform {platform}, found {len(candidates)}"
        )
    digest = str(candidates[0].get("digest") or "").strip()
    if not digest:
        raise ValueError(f"registry-cold-strict selected a manifest without a digest for platform {platform}")
    return digest


def _inspect_remote_manifest(*, ref: StrictRegistryRef, manifest_ref: str) -> dict:
    cmd = ["nerdctl", "manifest", "inspect", manifest_ref, "--verbose"]
    if ref.insecure_registry:
        cmd.append("--insecure")
    proc = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise RuntimeError(
            f"registry-cold-strict failed to inspect remote manifest {manifest_ref}: {stderr or f'exit_code={proc.returncode}'}"
        )
    stdout = (proc.stdout or "").strip()
    if not stdout:
        raise RuntimeError(f"registry-cold-strict manifest inspect produced no JSON for {manifest_ref}")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"registry-cold-strict manifest inspect emitted invalid JSON for {manifest_ref}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"registry-cold-strict manifest inspect returned non-object JSON for {manifest_ref}")
    return payload


def _resolve_registry_content_closure(
    *,
    ref: StrictRegistryRef,
    platform: str,
) -> RegistryContentClosure:
    root_payload = _inspect_remote_manifest(ref=ref, manifest_ref=ref.requested)
    root_descriptor, root_document = _manifest_document_from_inspect_payload(root_payload)
    if root_document is None:
        raise RuntimeError(f"registry-cold-strict could not parse a manifest document for {ref.requested}")
    root_digest = _descriptor_digest(root_descriptor) or ref.digest
    if "manifests" in root_document:
        if not root_digest:
            raise RuntimeError(
                f"registry-cold-strict could not determine the root index digest for {ref.requested}; fail closed"
            )
        manifest_digest = _select_platform_manifest(root_document, platform=platform)
        child_ref = ref.with_digest(manifest_digest)
        child_payload = _inspect_remote_manifest(ref=ref, manifest_ref=child_ref)
        child_descriptor, child_document = _manifest_document_from_inspect_payload(child_payload)
        if child_document is None or "config" not in child_document:
            raise RuntimeError(f"registry-cold-strict could not parse the child manifest for {child_ref}")
        manifest_digest = _descriptor_digest(child_descriptor) or manifest_digest
        config_digest = str((child_document.get("config") or {}).get("digest") or "").strip()
        if not config_digest:
            raise RuntimeError(f"registry-cold-strict child manifest is missing config.digest for {child_ref}")
        layer_digests = tuple(
            str(layer.get("digest") or "").strip()
            for layer in list(child_document.get("layers") or [])
            if str(layer.get("digest") or "").strip()
        )
        closure_digests = _unique_preserving_order([root_digest, manifest_digest, config_digest, *layer_digests])
        return RegistryContentClosure(
            requested_ref=ref.requested,
            normalized_requested_ref=ref.normalized_ref,
            resolved_ref=ref.with_digest(manifest_digest),
            root_digest=root_digest,
            manifest_digest=manifest_digest,
            config_digest=config_digest,
            layer_digests=layer_digests,
            closure_digests=closure_digests,
        )

    manifest_digest = root_digest or ref.digest
    if not manifest_digest:
        raise RuntimeError(
            f"registry-cold-strict could not determine the manifest digest for {ref.requested}; fail closed"
        )
    config_digest = str((root_document.get("config") or {}).get("digest") or "").strip()
    if not config_digest:
        raise RuntimeError(f"registry-cold-strict manifest is missing config.digest for {ref.requested}")
    layer_digests = tuple(
        str(layer.get("digest") or "").strip()
        for layer in list(root_document.get("layers") or [])
        if str(layer.get("digest") or "").strip()
    )
    closure_digests = _unique_preserving_order([manifest_digest, config_digest, *layer_digests])
    return RegistryContentClosure(
        requested_ref=ref.requested,
        normalized_requested_ref=ref.normalized_ref,
        resolved_ref=ref.with_digest(manifest_digest),
        root_digest=root_digest if root_digest and root_digest != manifest_digest else None,
        manifest_digest=manifest_digest,
        config_digest=config_digest,
        layer_digests=layer_digests,
        closure_digests=closure_digests,
    )


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _load_optional_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _run_env_check(
    *,
    repo_root: Path,
    image: str,
    snapshotter: str,
    runtime_probes: bool,
) -> tuple[subprocess.CompletedProcess[str], dict]:
    result = run_check_env_json(
        repo_root=repo_root,
        profile="benchmark",
        image=image,
        snapshotter=snapshotter,
        runtime_probes=runtime_probes,
    )
    return result.process, result.payload


def _merge_model_mount_into_runtime(
    runtime_spec: ContainerRuntimeSpec,
    *,
    model_host_dir: str,
    model_mount_hook: ModelMountHook | None,
) -> tuple[ContainerRuntimeSpec, bool]:
    if not model_host_dir or model_mount_hook is None:
        return runtime_spec, False
    dst_dir = str(model_mount_hook.dst_dir or "").strip()
    if not dst_dir:
        return runtime_spec, False
    host_path = Path(model_host_dir).expanduser().resolve()
    if not host_path.is_dir():
        raise RuntimeError(f"model host directory does not exist: {host_path}")
    mount_spec = f"type=bind,src={host_path},dst={dst_dir},readonly"
    mounts = list(runtime_spec.mounts)
    if mount_spec not in mounts:
        mounts.append(mount_spec)
    env = list(runtime_spec.env)
    env_keys = {item.split("=", 1)[0] for item in env if item}
    model_name = str(model_mount_hook.model_name or "").strip()
    if model_name and "MODEL_NAME" not in env_keys:
        env.append(f"MODEL_NAME={model_name}")
    model_base_path = str(model_mount_hook.model_base_path_env or "").strip() or dst_dir
    if model_base_path and "MODEL_BASE_PATH" not in env_keys:
        env.append(f"MODEL_BASE_PATH={model_base_path}")
    return inherit_container_runtime(runtime_spec, mounts=tuple(mounts), env=tuple(env)), True


def _default_base_snapshotter(snapshotter: str, *, baseline_stargz: bool = False) -> str:
    normalized = str(snapshotter or "").strip() or "stargz"
    if baseline_stargz:
        return normalized
    # The legacy benchmark helper is designed around comparing a baseline image
    # on the normal runtime snapshotter against an optimized image on stargz.
    # Keeping baseline on stargz by default can route it through stargz hard-
    # cold cleanup paths and surface broken parent-snapshot references.
    if normalized == "stargz":
        return "overlayfs"
    return normalized


def _default_snapshotters_for_comparison_mode(
    snapshotter: str,
    *,
    base_snapshotter: str | None,
    opt_snapshotter: str | None,
    baseline_stargz: bool,
    comparison_mode: str,
) -> tuple[str, str]:
    normalized_snapshotter = str(snapshotter or "").strip() or "stargz"
    if comparison_mode in {"raw-vs-estargz", "raw-full-vs-estargz-lazy", "raw-full-vs-estargz-lazy-triple"}:
        effective_base = str(base_snapshotter or "").strip() or (
            normalized_snapshotter if baseline_stargz else "overlayfs"
        )
        effective_opt = str(opt_snapshotter or "").strip() or "stargz"
        return effective_base, effective_opt
    effective_base = str(base_snapshotter or "").strip() or _default_base_snapshotter(
        normalized_snapshotter,
        baseline_stargz=baseline_stargz,
    )
    effective_opt = str(opt_snapshotter or "").strip() or normalized_snapshotter
    return effective_base, effective_opt


def _median_or_none(values: list[int]) -> float | None:
    if not values:
        return None
    return float(statistics.median(values))


def _post_pull_ready_ms_from_row(row: dict[str, str]) -> int | None:
    run_ms = _optional_nonnegative_csv_int(row.get("run_ms"))
    ready_wait_ms = _optional_nonnegative_csv_int(row.get("ready_wait_ms"))
    if run_ms is None or ready_wait_ms is None:
        return None
    return run_ms + ready_wait_ms


def _lazy_prepare_ms_from_row(row: dict[str, str]) -> int | None:
    return _optional_nonnegative_csv_int(row.get("lazy_prepare_ms"))


def _lazy_ready_ms_from_row(row: dict[str, str]) -> int | None:
    return _optional_nonnegative_csv_int(row.get("lazy_ready_ms"))


def _row_ready_ms_semantics(row: dict[str, str]) -> str:
    semantics = str(row.get("ready_ms_semantics") or "").strip()
    if semantics:
        return semantics
    mode = str(row.get("mode") or "").strip()
    return "end_to_end_ready_ms" if mode == "ready" else "run_ms"


def _row_flag_enabled(row: dict[str, str], field: str) -> bool:
    value = str(row.get(field) or "").strip().lower()
    return value in {"1", "true", "yes", "y", "on"}


def _required_success_samples(expected_runs: int) -> int:
    return 1 if expected_runs <= 1 else 2


def _ready_hook_uses_loopback_ready_url(
    ready_hook: HttpReadyHook | TcpReadyHook | ExecReadyHook | LogRegexReadyHook | None,
) -> bool:
    if isinstance(ready_hook, TcpReadyHook):
        return ready_hook.host in {"127.0.0.1", "localhost", "::1"}
    if isinstance(ready_hook, HttpReadyHook):
        parsed = urlparse(ready_hook.url)
        return (parsed.hostname or "") in {"127.0.0.1", "localhost", "::1"}
    return False


def _http_ready_probe_allows_status_only(
    *,
    self_starting_service: bool,
    mode: str,
    ready_hook: HttpReadyHook | TcpReadyHook | ExecReadyHook | LogRegexReadyHook | None,
) -> bool:
    return (
        bool(self_starting_service)
        and mode == "ready"
        and isinstance(ready_hook, HttpReadyHook)
    )


def _filter_policy_warnings(
    warnings: list[str],
    *,
    ready_hook: HttpReadyHook | TcpReadyHook | ExecReadyHook | LogRegexReadyHook | None,
) -> list[str]:
    if _ready_hook_uses_loopback_ready_url(ready_hook):
        return list(warnings)
    return [
        item
        for item in warnings
        if "CNI bridge plugin is unavailable; busy loopback ready-url probes cannot use published-port remap"
        not in item
    ]


def _root_cause_reason(label: str, info: dict[str, object]) -> str | None:
    errors = list(info.get("errors") or [])
    if len(errors) != 1:
        return None
    err = errors[0]
    if err == "network-prep-failed":
        return (
            f"{label} failed before container startup because ready-network preparation was impossible; "
            "free the host port, use --force-kill-port, switch to --ready-exec/--ready-log-regex, or install CNI bridge plugins"
        )
    if err == "nerdctl-run-failed":
        return f"{label} container failed to start under benchmark; inspect per-run *.runerr logs under benchmark log_dir"
    if err == "startup-gpu-oom":
        return (
            f"{label} container hit GPU out-of-memory during startup; reduce model/runtime footprint, "
            "free VRAM, or use a larger GPU before benchmarking"
        )
    if err == "startup-model-fetch-failed":
        return (
            f"{label} container failed while fetching model artifacts during startup; use a reachable proxy "
            "or pre-populate/cache the model and prefer offline startup for benchmarking"
        )
    if err == "startup-runtime-error":
        return (
            f"{label} container hit a fatal runtime initialization error before ready; inspect per-run coldstart logs "
            "under benchmark log_dir"
        )
    return None


def summarize_benchmark_rows(
    rows: list[dict[str, str]],
    *,
    expected_runs: int | None = None,
    comparison_mode: str = "same-snapshotter",
) -> dict:
    labels: dict[str, dict[str, object]] = {}
    inferred_runs = 0
    label_order = _comparison_mode_labels(comparison_mode)
    for label in label_order:
        subset = [row for row in rows if row.get("label") == label]
        ok_rows = [
            row for row in subset
            if row.get("status") == "ok" and int(row.get("ready_ms") or -1) >= 0
        ]
        ready_values = [int(row["ready_ms"]) for row in ok_rows]
        predict_values = [
            int(row["predict_extra_ms"])
            for row in ok_rows
            if int(row.get("predict_extra_ms") or -1) >= 0
        ]
        pull_values = [
            int(row["pull_ms"])
            for row in ok_rows
            if int(row.get("pull_ms") or -1) >= 0
        ]
        run_values = [
            int(row["run_ms"])
            for row in ok_rows
            if int(row.get("run_ms") or -1) >= 0
        ]
        ready_wait_values = [
            int(row["ready_wait_ms"])
            for row in ok_rows
            if int(row.get("ready_wait_ms") or -1) >= 0
        ]
        post_pull_ready_values = [
            value
            for value in (_post_pull_ready_ms_from_row(row) for row in ok_rows)
            if value is not None
        ]
        lazy_prepare_values = [
            value
            for value in (_lazy_prepare_ms_from_row(row) for row in ok_rows)
            if value is not None
        ]
        lazy_ready_values = [
            value
            for value in (_lazy_ready_ms_from_row(row) for row in ok_rows)
            if value is not None
        ]
        end_to_end_values = [
            int(row["end_to_end_ready_ms"])
            for row in ok_rows
            if int(row.get("end_to_end_ready_ms") or -1) >= 0
        ]
        service_bootstrap_values = [
            int(row["service_bootstrap_ms"])
            for row in ok_rows
            if int(row.get("service_bootstrap_ms") or -1) >= 0
        ]
        labels[label] = {
            "total_rows": len(subset),
            "ok_rows": len(ok_rows),
            "fail_rows": len(subset) - len(ok_rows),
            "median_ready_ms": _median_or_none(ready_values),
            "min_ready_ms": min(ready_values) if ready_values else None,
            "max_ready_ms": max(ready_values) if ready_values else None,
            "median_pull_ms": _median_or_none(pull_values),
            "min_pull_ms": min(pull_values) if pull_values else None,
            "max_pull_ms": max(pull_values) if pull_values else None,
            "median_run_ms": _median_or_none(run_values),
            "min_run_ms": min(run_values) if run_values else None,
            "max_run_ms": max(run_values) if run_values else None,
            "median_ready_wait_ms": _median_or_none(ready_wait_values),
            "min_ready_wait_ms": min(ready_wait_values) if ready_wait_values else None,
            "max_ready_wait_ms": max(ready_wait_values) if ready_wait_values else None,
            "median_post_pull_ready_ms": _median_or_none(post_pull_ready_values),
            "min_post_pull_ready_ms": min(post_pull_ready_values) if post_pull_ready_values else None,
            "max_post_pull_ready_ms": max(post_pull_ready_values) if post_pull_ready_values else None,
            "median_lazy_prepare_ms": _median_or_none(lazy_prepare_values),
            "min_lazy_prepare_ms": min(lazy_prepare_values) if lazy_prepare_values else None,
            "max_lazy_prepare_ms": max(lazy_prepare_values) if lazy_prepare_values else None,
            "median_lazy_ready_ms": _median_or_none(lazy_ready_values),
            "min_lazy_ready_ms": min(lazy_ready_values) if lazy_ready_values else None,
            "max_lazy_ready_ms": max(lazy_ready_values) if lazy_ready_values else None,
            "median_end_to_end_ready_ms": _median_or_none(end_to_end_values) or _median_or_none(ready_values),
            "min_end_to_end_ready_ms": min(end_to_end_values) if end_to_end_values else (min(ready_values) if ready_values else None),
            "max_end_to_end_ready_ms": max(end_to_end_values) if end_to_end_values else (max(ready_values) if ready_values else None),
            "median_full_pull_ready_ms": _median_or_none(end_to_end_values) or _median_or_none(ready_values),
            "median_predict_extra_ms": _median_or_none(predict_values),
            "median_service_bootstrap_ms": _median_or_none(service_bootstrap_values),
            "min_service_bootstrap_ms": min(service_bootstrap_values) if service_bootstrap_values else None,
            "max_service_bootstrap_ms": max(service_bootstrap_values) if service_bootstrap_values else None,
            "errors": sorted({row.get("err") or "" for row in subset if (row.get("status") != "ok")}),
        }
        inferred_runs = max(inferred_runs, len(subset))

    baseline = labels.get("baseline") or {}
    optimized = labels.get("optimized") or {}
    baseline_median = baseline.get("median_ready_ms")
    optimized_median = optimized.get("median_ready_ms")
    comparison = {
        "median_ready_delta_ms": None,
        "median_ready_improvement_pct": None,
        "median_pull_delta_ms": None,
        "median_run_delta_ms": None,
        "median_ready_wait_delta_ms": None,
        "median_post_pull_ready_delta_ms": None,
        "median_post_pull_ready_improvement_pct": None,
        "median_lazy_prepare_delta_ms": None,
        "median_lazy_ready_delta_ms": None,
        "median_lazy_ready_improvement_pct": None,
        "median_end_to_end_delta_ms": None,
        "median_end_to_end_improvement_pct": None,
        "median_service_bootstrap_delta_ms": None,
        "unoptimized_vs_raw_improvement_pct": None,
        "optimized_vs_raw_improvement_pct": None,
        "optimized_vs_unoptimized_improvement_pct": None,
    }
    if baseline_median is not None and optimized_median is not None:
        comparison["median_ready_delta_ms"] = float(optimized_median - baseline_median)
        if baseline_median > 0:
            comparison["median_ready_improvement_pct"] = float(
                (baseline_median - optimized_median) * 100.0 / baseline_median
            )
    for metric_name, comparison_key in (
        ("median_pull_ms", "median_pull_delta_ms"),
        ("median_run_ms", "median_run_delta_ms"),
        ("median_ready_wait_ms", "median_ready_wait_delta_ms"),
        ("median_post_pull_ready_ms", "median_post_pull_ready_delta_ms"),
        ("median_lazy_prepare_ms", "median_lazy_prepare_delta_ms"),
        ("median_lazy_ready_ms", "median_lazy_ready_delta_ms"),
        ("median_service_bootstrap_ms", "median_service_bootstrap_delta_ms"),
    ):
        baseline_value = baseline.get(metric_name)
        optimized_value = optimized.get(metric_name)
        if baseline_value is not None and optimized_value is not None:
            comparison[comparison_key] = float(optimized_value - baseline_value)
    baseline_post_pull = baseline.get("median_post_pull_ready_ms")
    optimized_post_pull = optimized.get("median_post_pull_ready_ms")
    if baseline_post_pull is not None and optimized_post_pull is not None and baseline_post_pull > 0:
        comparison["median_post_pull_ready_improvement_pct"] = float(
            (baseline_post_pull - optimized_post_pull) * 100.0 / baseline_post_pull
        )
    baseline_end_to_end = baseline.get("median_end_to_end_ready_ms")
    optimized_end_to_end = optimized.get("median_end_to_end_ready_ms")
    if baseline_end_to_end is not None and optimized_end_to_end is not None:
        comparison["median_end_to_end_delta_ms"] = float(optimized_end_to_end - baseline_end_to_end)
        if baseline_end_to_end > 0:
            comparison["median_end_to_end_improvement_pct"] = float(
                (baseline_end_to_end - optimized_end_to_end) * 100.0 / baseline_end_to_end
            )
    optimized_lazy_ready = optimized.get("median_lazy_ready_ms")
    if baseline_end_to_end is not None and optimized_lazy_ready is not None:
        comparison["median_lazy_ready_delta_ms"] = float(optimized_lazy_ready - baseline_end_to_end)
        if baseline_end_to_end > 0:
            comparison["median_lazy_ready_improvement_pct"] = float(
                (baseline_end_to_end - optimized_lazy_ready) * 100.0 / baseline_end_to_end
            )
    if comparison_mode == "raw-full-vs-estargz-lazy-triple":
        unoptimized = dict(labels.get("unoptimized_lazy") or {})
        baseline_full_pull_ready = baseline.get("median_full_pull_ready_ms")
        unoptimized_lazy_ready = unoptimized.get("median_lazy_ready_ms")
        if baseline_full_pull_ready is not None and unoptimized_lazy_ready is not None and baseline_full_pull_ready > 0:
            comparison["unoptimized_vs_raw_improvement_pct"] = float(
                (baseline_full_pull_ready - unoptimized_lazy_ready) * 100.0 / baseline_full_pull_ready
            )
        if baseline_full_pull_ready is not None and optimized_lazy_ready is not None and baseline_full_pull_ready > 0:
            comparison["optimized_vs_raw_improvement_pct"] = float(
                (baseline_full_pull_ready - optimized_lazy_ready) * 100.0 / baseline_full_pull_ready
            )
        if (
            unoptimized_lazy_ready is not None
            and optimized_lazy_ready is not None
            and unoptimized_lazy_ready > 0
        ):
            comparison["optimized_vs_unoptimized_improvement_pct"] = float(
                (unoptimized_lazy_ready - optimized_lazy_ready) * 100.0 / unoptimized_lazy_ready
            )

    required_success = _required_success_samples(expected_runs or inferred_runs or 1)
    invalid_reasons: list[str] = []
    for label in label_order:
        info = labels[label]
        if info["total_rows"] == 0:
            invalid_reasons.append(f"{label} produced 0 benchmark rows")
        elif info["ok_rows"] == 0:
            invalid_reasons.append(f"{label} produced 0 successful benchmark rows")
            root_cause = _root_cause_reason(label, info)
            if root_cause is not None:
                invalid_reasons.append(root_cause)
        elif info["ok_rows"] < required_success:
            invalid_reasons.append(
                f"{label} produced only {info['ok_rows']} successful benchmark rows; need at least {required_success}"
            )

    if (baseline_median is None or optimized_median is None) and not invalid_reasons:
        invalid_reasons.append(
            "benchmark comparison is unavailable because one or both labels lack median ready_ms"
        )
    for label in _lazy_labels_for_comparison_mode(comparison_mode):
        lazy_errors = [
            str(row.get("err") or "").strip()
            for row in rows
            if str(row.get("label") or "").strip() == label
        ]
        if any(err == f"{label}-lazy-path-performed-full-pull" for err in lazy_errors):
            invalid_reasons.append(_lazy_full_pull_invalid_reason_for_label(label))

    return {
        "rows_total": len(rows),
        "labels": labels,
        "comparison": comparison,
        "valid": not invalid_reasons,
        "invalid_reasons": invalid_reasons,
        "warnings": [],
    }


def _summary_top_level_fields(summary: dict[str, object]) -> tuple[float | None, float | None, float | None]:
    labels = dict(summary.get("labels") or {})
    comparison = dict(summary.get("comparison") or {})
    baseline_median = (labels.get("baseline") or {}).get("median_ready_ms")
    optimized_median = (labels.get("optimized") or {}).get("median_ready_ms")
    improvement_pct = comparison.get("median_ready_improvement_pct")
    return baseline_median, optimized_median, improvement_pct


def _summary_end_to_end_top_level_fields(
    summary: dict[str, object],
) -> tuple[float | None, float | None, float | None]:
    labels = dict(summary.get("labels") or {})
    comparison = dict(summary.get("comparison") or {})
    baseline_median = (labels.get("baseline") or {}).get("median_end_to_end_ready_ms")
    optimized_median = (labels.get("optimized") or {}).get("median_end_to_end_ready_ms")
    improvement_pct = comparison.get("median_end_to_end_improvement_pct")
    return baseline_median, optimized_median, improvement_pct


def _summary_post_pull_top_level_fields(
    summary: dict[str, object],
) -> tuple[float | None, float | None, float | None]:
    labels = dict(summary.get("labels") or {})
    comparison = dict(summary.get("comparison") or {})
    baseline_median = (labels.get("baseline") or {}).get("median_post_pull_ready_ms")
    optimized_median = (labels.get("optimized") or {}).get("median_post_pull_ready_ms")
    improvement_pct = comparison.get("median_post_pull_ready_improvement_pct")
    return baseline_median, optimized_median, improvement_pct


def _summary_lazy_top_level_fields(
    summary: dict[str, object],
) -> tuple[float | None, float | None, float | None, float | None, float | None]:
    labels = dict(summary.get("labels") or {})
    comparison = dict(summary.get("comparison") or {})
    baseline = dict(labels.get("baseline") or {})
    optimized = dict(labels.get("optimized") or {})
    return (
        baseline.get("median_pull_ms"),
        baseline.get("median_end_to_end_ready_ms"),
        optimized.get("median_lazy_prepare_ms"),
        optimized.get("median_lazy_ready_ms"),
        comparison.get("median_lazy_ready_improvement_pct"),
    )


def _summary_triple_lazy_top_level_fields(
    summary: dict[str, object],
) -> tuple[float | None, float | None, float | None, float | None, float | None, float | None]:
    labels = dict(summary.get("labels") or {})
    comparison = dict(summary.get("comparison") or {})
    baseline = dict(labels.get("baseline") or {})
    unoptimized = dict(labels.get("unoptimized_lazy") or {})
    optimized = dict(labels.get("optimized") or {})
    return (
        baseline.get("median_full_pull_ready_ms"),
        unoptimized.get("median_lazy_ready_ms"),
        optimized.get("median_lazy_ready_ms"),
        comparison.get("unoptimized_vs_raw_improvement_pct"),
        comparison.get("optimized_vs_raw_improvement_pct"),
        comparison.get("optimized_vs_unoptimized_improvement_pct"),
    )


_PULL_DOMINATED_WARNING = (
    "The current runtime path appears pull-dominated; lazy pulling benefits may be masked by full image transfer behavior."
)
_LAZY_FULL_PULL_WARNING = (
    "optimized path appears to have performed a full pull; lazy-pull result may be invalid"
)


def derive_benchmark_warnings(
    summary: dict[str, object],
    *,
    comparison_mode: str,
    rows: list[dict[str, object]] | None = None,
) -> list[str]:
    warnings = list(summary.get("warnings") or [])
    if comparison_mode in {"raw-full-vs-estargz-lazy", "raw-full-vs-estargz-lazy-triple"}:
        for label in _lazy_labels_for_comparison_mode(comparison_mode):
            warning_text = _LAZY_FULL_PULL_WARNING if label == "optimized" and comparison_mode == "raw-full-vs-estargz-lazy" else _lazy_full_pull_warning_for_label(label)
            if any(
                (
                    str(row.get("label") or "").strip() == label
                    and (
                        _row_flag_enabled(row, "lazy_full_pull_warning")
                        or str(row.get("err") or "").strip() == f"{label}-lazy-path-performed-full-pull"
                    )
                )
                for row in (rows or [])
            ) and warning_text not in warnings:
                warnings.append(warning_text)
        return warnings
    if comparison_mode != "raw-vs-estargz":
        return warnings

    labels = dict(summary.get("labels") or {})
    baseline = dict(labels.get("baseline") or {})
    optimized = dict(labels.get("optimized") or {})
    baseline_pull_ms = baseline.get("median_pull_ms")
    optimized_pull_ms = optimized.get("median_pull_ms")
    optimized_end_to_end_ready_ms = optimized.get("median_end_to_end_ready_ms")
    if (
        baseline_pull_ms is not None
        and optimized_pull_ms is not None
        and optimized_end_to_end_ready_ms not in {None, 0}
        and float(optimized_pull_ms) >= float(optimized_end_to_end_ready_ms) * 0.8
        and float(baseline_pull_ms) > 0
        and float(optimized_pull_ms) >= float(baseline_pull_ms) * 0.75
        and _PULL_DOMINATED_WARNING not in warnings
    ):
        warnings.append(_PULL_DOMINATED_WARNING)
    return warnings


def _optional_nonnegative_csv_int(value: str | None) -> int | None:
    try:
        parsed = int(value or -1)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _normalize_benchmark_rows(
    rows: list[dict[str, str]],
    *,
    base_image: str,
    unopt_image: str,
    opt_image: str,
    comparison_mode: str,
) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    for row in rows:
        item: dict[str, object] = dict(row)
        for field in (
            "pull_ms",
            "run_ms",
            "ready_wait_ms",
            "end_to_end_ready_ms",
            "ready_ms",
            "predict_extra_ms",
            "service_bootstrap_ms",
            "hot_files_loaded",
            "hot_bytes_loaded",
            "loaded_paths_count",
            "proof_exists",
            "proof_done",
            "lazy_prepare_ms",
            "lazy_ready_ms",
            "stargz_cache_bytes",
            "stargz_cache_bytes_before",
            "content_large_blob_present_before",
            "content_large_blob_present_after",
            "content_store_present_bytes_before",
            "content_store_present_bytes_after",
            "explicit_pull_executed",
            "full_pull_evidence",
            "lazy_guard_failed",
            "lazy_full_pull_warning",
        ):
            item[field] = _optional_nonnegative_csv_int(row.get(field))
        label = str(row.get("label") or "")
        item["timestamp"] = str(row.get("ts") or "")
        if label == "baseline":
            item["image"] = base_image
        elif label == "unoptimized_lazy":
            item["image"] = unopt_image
        elif label == "optimized":
            item["image"] = opt_image
        else:
            item["image"] = ""
        item["comparison_mode"] = comparison_mode
        item["post_pull_ready_ms"] = _post_pull_ready_ms_from_row(row)
        item["ready_ms_semantics"] = _row_ready_ms_semantics(row)
        item["error"] = str(row.get("err") or "")
        normalized.append(item)
    return normalized


def run_benchmark_stage(
    *,
    repo_root: Path,
    work_dir: Path,
    base_image: str,
    opt_image: str,
    unopt_image: str = "",
    selector_mode: str = "file",
    namespace: str = "default",
    runs: int = 3,
    timeout_ready: int = 180,
    snapshotter: str = "stargz",
    base_snapshotter: str | None = None,
    opt_snapshotter: str | None = None,
    baseline_stargz: bool = False,
    comparison_mode: str = "same-snapshotter",
    mode: str = "struct",
    ready_url: str = "",
    ready_exec: str = "",
    ready_log_regex: str = "",
    ready_shell: str = "sh",
    debug_state_url: str = "",
    predict_url: str = "",
    predict_body: str = "",
    model_host_dir: str = "",
    model_dst_dir: str = "",
    model_name: str = "",
    platform: str = "linux/amd64",
    hard_cold: bool = True,
    force_reset: bool = False,
    force_kill_port: bool = False,
    allow_busy_port: bool = False,
    reconcile_dirs: bool = False,
    drop_page_cache: bool = True,
    clear_stargz_cache: bool = True,
    shuffle_order: bool = False,
    log_dir: str | None = None,
    runtime_probes: bool = True,
    pull_policy: str = "never",
    drop_local_image_ref: bool = False,
    cold_mode: str = "local-cold",
    workload_profile: str = "none",
    workload_profile_capabilities: tuple[str, ...] = (),
    workload_effective_capabilities: tuple[str, ...] = (),
    workload_profile_hooks: dict[str, dict[str, object] | None] | None = None,
    workload_effective_hooks: dict[str, dict[str, object] | None] | None = None,
    self_starting_service: bool = False,
    ready_hook: HttpReadyHook | TcpReadyHook | ExecReadyHook | LogRegexReadyHook | None = None,
    predict_hook: HttpPredictHook | None = None,
    model_mount_hook: ModelMountHook | None = None,
    container_runtime: ContainerRuntimeSpec | None = None,
) -> BenchmarkResult:
    env_check_base_path = work_dir / "env_check_benchmark_base.json"
    env_check_opt_path = work_dir / "env_check_benchmark_opt.json"
    policy_path = work_dir / "benchmark_policy.json"
    stage_log_path = work_dir / "benchmark.log"
    csv_path = work_dir / "coldstart_compare.csv"
    log_dir_path = Path(log_dir).resolve() if log_dir else (work_dir / "coldstart_logs")
    result_path = work_dir / "benchmark_result.json"
    strict_input_path = work_dir / "benchmark_strict_input.json"
    strict_proof_path = work_dir / "benchmark_strict_proof.json"
    effective_container_runtime = container_runtime or ContainerRuntimeSpec()
    strict_registry_payload: dict[str, object] | None = None
    strict_proof_payload: dict[str, object] | None = None

    if comparison_mode not in set(_BENCHMARK_COMPARISON_MODES):
        raise ValueError(f"unsupported benchmark comparison_mode: {comparison_mode}")
    if comparison_mode == "raw-full-vs-estargz-lazy-triple" and not str(unopt_image or "").strip():
        raise ValueError("raw-full-vs-estargz-lazy-triple requires unopt_image")
    if cold_mode not in {"local-cold", "registry-cold-strict"}:
        raise ValueError(f"unsupported benchmark cold_mode: {cold_mode}")
    effective_base_snapshotter, effective_opt_snapshotter = _default_snapshotters_for_comparison_mode(
        snapshotter,
        base_snapshotter=base_snapshotter,
        opt_snapshotter=opt_snapshotter,
        baseline_stargz=baseline_stargz,
        comparison_mode=comparison_mode,
    )
    if comparison_mode == "raw-full-vs-estargz-lazy-triple":
        if effective_base_snapshotter != "overlayfs":
            raise ValueError("raw-full-vs-estargz-lazy-triple requires base_snapshotter=overlayfs")
        if effective_opt_snapshotter != "stargz":
            raise ValueError("raw-full-vs-estargz-lazy-triple requires opt_snapshotter=stargz")
    if cold_mode == "registry-cold-strict":
        if mode != "ready":
            raise ValueError("registry-cold-strict requires mode=ready")
        strict_unopt_ref: StrictRegistryRef | None = None
        if comparison_mode == "raw-full-vs-estargz-lazy-triple":
            strict_base_ref, strict_unopt_ref, strict_opt_ref = validate_registry_cold_strict_triple(
                base_image=base_image,
                unopt_image=unopt_image,
                opt_image=opt_image,
            )
        else:
            strict_base_ref, strict_opt_ref = validate_registry_cold_strict_pair(
                base_image=base_image,
                opt_image=opt_image,
            )
        if comparison_mode == "same-snapshotter":
            if str(snapshotter or "").strip() and str(snapshotter).strip() != "stargz":
                raise ValueError("registry-cold-strict requires snapshotter=stargz")
            effective_base_snapshotter = "stargz"
            effective_opt_snapshotter = "stargz"
        if pull_policy != "always":
            raise ValueError("registry-cold-strict requires pull_policy=always")
        if not drop_local_image_ref:
            raise ValueError("registry-cold-strict requires drop_local_image_ref=true")
        if not hard_cold:
            raise ValueError("registry-cold-strict requires hard_cold=true")
        if not drop_page_cache:
            raise ValueError("registry-cold-strict requires page-cache drop to remain enabled")
        if not clear_stargz_cache:
            raise ValueError("registry-cold-strict requires stargz cache cleanup to remain enabled")
        base_closure = _resolve_registry_content_closure(ref=strict_base_ref, platform=platform)
        unopt_closure: RegistryContentClosure | None = None
        if strict_unopt_ref is not None:
            unopt_closure = _resolve_registry_content_closure(ref=strict_unopt_ref, platform=platform)
        opt_closure = _resolve_registry_content_closure(ref=strict_opt_ref, platform=platform)
        shared_sets = [set(base_closure.closure_digests) & set(opt_closure.closure_digests)]
        if unopt_closure is not None:
            shared_sets.append(set(base_closure.closure_digests) & set(unopt_closure.closure_digests))
            shared_sets.append(set(unopt_closure.closure_digests) & set(opt_closure.closure_digests))
        shared_closure_digests = sorted(set().union(*shared_sets))
        strict_registry_payload = {
            "platform": platform,
            "content_purge_scope": "targeted_closure_union",
            "content_purge_broad_prune": False,
            "base": base_closure.as_payload(),
            "optimized": opt_closure.as_payload(),
            "shared_closure_digests": shared_closure_digests,
        }
        if unopt_closure is not None:
            strict_registry_payload["unoptimized"] = unopt_closure.as_payload()
        _write_json(strict_input_path, strict_registry_payload)
    opt_env_check_image = opt_image
    if comparison_mode in {"raw-full-vs-estargz-lazy", "raw-full-vs-estargz-lazy-triple"}:
        # The lazy benchmark must not touch the optimized image runtime path
        # before the helper's strict cold cleanup/proof completes.
        opt_env_check_image = ""
    base_proc, base_env_payload = _run_env_check(
        repo_root=repo_root,
        image=base_image,
        snapshotter=effective_base_snapshotter,
        runtime_probes=runtime_probes,
    )
    write_env_check_payload(env_check_base_path, base_env_payload)
    opt_proc, opt_env_payload = _run_env_check(
        repo_root=repo_root,
        image=opt_env_check_image,
        snapshotter=effective_opt_snapshotter,
        runtime_probes=runtime_probes,
    )
    write_env_check_payload(env_check_opt_path, opt_env_payload)

    policy_payload = evaluate_benchmark_policy(
        base_env_payload,
        opt_env_payload,
        require_local_image=(
            pull_policy == "never"
            and comparison_mode not in {"raw-full-vs-estargz-lazy", "raw-full-vs-estargz-lazy-triple"}
        ),
        require_content_store_purge=(cold_mode == "registry-cold-strict"),
    )
    policy_payload["base_env_check_exit_code"] = base_proc.returncode
    policy_payload["opt_env_check_exit_code"] = opt_proc.returncode
    policy_payload["base_image"] = base_image
    if comparison_mode == "raw-full-vs-estargz-lazy-triple":
        policy_payload["unopt_image"] = unopt_image
    policy_payload["opt_image"] = opt_image
    policy_payload["base_snapshotter"] = effective_base_snapshotter
    policy_payload["opt_snapshotter"] = effective_opt_snapshotter
    if ready_hook is not None:
        effective_ready_hook = ready_hook
    elif ready_exec:
        effective_ready_hook = ExecReadyHook(ready_exec, shell=ready_shell or "sh")
    elif ready_log_regex:
        effective_ready_hook = LogRegexReadyHook(ready_log_regex)
    else:
        effective_ready_hook = ready_hook_from_value(ready_url) if ready_url else None
    effective_predict_hook = predict_hook or (HttpPredictHook(predict_url, predict_body) if predict_url and predict_body else None)
    effective_model_mount_hook = model_mount_hook or (
        ModelMountHook(model_dst_dir, model_name or None) if model_dst_dir or model_name else None
    )
    policy_payload["warnings"] = _filter_policy_warnings(
        list(policy_payload.get("warnings") or []),
        ready_hook=effective_ready_hook,
    )
    _write_json(policy_path, policy_payload)
    if not policy_payload["ok"]:
        raise RuntimeError("; ".join(policy_payload.get("errors") or ["benchmark preflight failed"]))

    effective_container_runtime, runtime_consumed_model_mount = _merge_model_mount_into_runtime(
        effective_container_runtime,
        model_host_dir=model_host_dir,
        model_mount_hook=effective_model_mount_hook,
    )

    hook_args = benchmark_hook_parameters(
        ready_hook=effective_ready_hook,
        predict_hook=effective_predict_hook,
        model_mount_hook=None if runtime_consumed_model_mount else effective_model_mount_hook,
        model_host_dir="" if runtime_consumed_model_mount else model_host_dir,
        debug_state_url=debug_state_url,
    )

    stage_log_path.write_text("", encoding="utf-8")
    cmd = [
        "bash",
        str(repo_root / "scripts" / "coldstart_bench_strict.sh"),
        "--base",
        base_image,
        "--ns",
        namespace,
        "--runs",
        str(runs),
        "--timeout",
        str(timeout_ready),
        "--csv",
        str(csv_path),
        "--log-dir",
        str(log_dir_path),
        "--snap",
        snapshotter,
        "--base-snap",
        effective_base_snapshotter,
        "--opt-snap",
        effective_opt_snapshotter,
        "--comparison-mode",
        comparison_mode,
        "--mode",
        mode,
        "--platform",
        platform,
    ]
    if unopt_image:
        cmd.extend(["--unopt-image", unopt_image])
    cmd.extend(["--opt", opt_image])
    if hook_args["ready_url"]:
        cmd.extend(["--ready-url", hook_args["ready_url"]])
    if hook_args["ready_exec"]:
        cmd.extend(["--ready-exec", hook_args["ready_exec"]])
    if hook_args["ready_log_regex"]:
        cmd.extend(["--ready-log-regex", hook_args["ready_log_regex"]])
    if hook_args["ready_shell"] and hook_args["ready_shell"] != "sh":
        cmd.extend(["--ready-shell", hook_args["ready_shell"]])
    if hook_args["debug_state_url"]:
        cmd.extend(["--debug-state-url", hook_args["debug_state_url"]])
    if _http_ready_probe_allows_status_only(
        self_starting_service=self_starting_service,
        mode=mode,
        ready_hook=effective_ready_hook,
    ):
        cmd.append("--ready-http-status-ok-is-ready")
    if hook_args["model_dst_dir"]:
        cmd.extend(["--model-dst", hook_args["model_dst_dir"]])
    if hook_args["model_name"]:
        cmd.extend(["--model-name", hook_args["model_name"]])
    if hook_args["predict_url"]:
        cmd.extend(["--predict-url", hook_args["predict_url"]])
    if hook_args["predict_body"]:
        cmd.extend(["--predict-body", hook_args["predict_body"]])
    if hook_args["model_host_dir"]:
        cmd.extend(["--model-host", hook_args["model_host_dir"]])
    if effective_container_runtime.gpus:
        cmd.extend(["--gpus", effective_container_runtime.gpus])
    if effective_container_runtime.ipc:
        cmd.extend(["--ipc", effective_container_runtime.ipc])
    if effective_container_runtime.entrypoint:
        cmd.extend(["--container-entrypoint", effective_container_runtime.entrypoint])
    for item in effective_container_runtime.env:
        cmd.extend(["--container-env", item])
    for item in effective_container_runtime.mounts:
        cmd.extend(["--container-mount", item])
    for item in effective_container_runtime.command:
        cmd.extend(["--container-cmd", item])
    if hard_cold:
        cmd.append("--hard-cold")
    else:
        cmd.append("--soft-cold")
    if force_reset:
        cmd.append("--force-reset")
    if force_kill_port:
        cmd.append("--force-kill-port")
    if allow_busy_port:
        cmd.append("--allow-busy-port")
    if reconcile_dirs:
        cmd.append("--reconcile-dirs")
    if not drop_page_cache:
        cmd.append("--no-drop-cache")
    if not clear_stargz_cache:
        cmd.append("--no-clear-stargz")
    if shuffle_order:
        cmd.append("--shuffle")
    if pull_policy != "never":
        cmd.extend(["--pull-policy", pull_policy])
    if drop_local_image_ref:
        cmd.append("--drop-local-image-ref")
    cmd.extend(["--cold-mode", cold_mode])
    if strict_registry_payload is not None:
        cmd.extend(["--strict-input-json", str(strict_input_path)])
        cmd.extend(["--proof-json", str(strict_proof_path)])

    with stage_log_path.open("a", encoding="utf-8") as log_fh:
        proc = subprocess.run(
            cmd,
            cwd=repo_root,
            text=True,
            stdout=log_fh,
            stderr=log_fh,
            check=False,
        )
    if proc.returncode != 0:
        raise RuntimeError(f"benchmark stage failed, see {stage_log_path}")
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        raise RuntimeError("benchmark stage completed without coldstart_compare.csv")
    if cold_mode == "registry-cold-strict":
        strict_proof_payload = _load_optional_json(strict_proof_path)
        if strict_proof_payload is None:
            raise RuntimeError("registry-cold-strict helper completed without benchmark_strict_proof.json")
        if not bool(strict_proof_payload.get("strict_proof_zero", False)):
            raise RuntimeError("registry-cold-strict helper completed with non-zero strict proof")

    with csv_path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    summary = summarize_benchmark_rows(
        rows,
        expected_runs=runs,
        comparison_mode=comparison_mode,
    )
    normalized_rows = _normalize_benchmark_rows(
        rows,
        base_image=base_image,
        unopt_image=unopt_image,
        opt_image=opt_image,
        comparison_mode=comparison_mode,
    )
    baseline_median_ready_ms, optimized_median_ready_ms, improvement_pct = _summary_top_level_fields(summary)
    baseline_median_end_to_end_ready_ms, optimized_median_end_to_end_ready_ms, end_to_end_improvement_pct = _summary_end_to_end_top_level_fields(summary)
    baseline_median_post_pull_ready_ms, optimized_median_post_pull_ready_ms, post_pull_ready_improvement_pct = _summary_post_pull_top_level_fields(summary)
    (
        baseline_full_pull_ms,
        baseline_median_full_pull_ready_ms,
        optimized_lazy_prepare_ms,
        optimized_median_lazy_ready_ms,
        lazy_ready_improvement_pct,
    ) = _summary_lazy_top_level_fields(summary)
    (
        triple_baseline_median_full_pull_ready_ms,
        unoptimized_median_lazy_ready_ms,
        triple_optimized_median_lazy_ready_ms,
        unoptimized_vs_raw_improvement_pct,
        optimized_vs_raw_improvement_pct,
        optimized_vs_unoptimized_improvement_pct,
    ) = _summary_triple_lazy_top_level_fields(summary)
    benchmark_warnings = derive_benchmark_warnings(
        summary,
        comparison_mode=comparison_mode,
        rows=normalized_rows,
    )
    summary["warnings"] = benchmark_warnings
    benchmark_status = "passed" if bool(summary.get("valid", False)) else "failed"
    lazy_mode_enabled = comparison_mode in {"raw-full-vs-estargz-lazy", "raw-full-vs-estargz-lazy-triple"}
    triple_mode_enabled = comparison_mode == "raw-full-vs-estargz-lazy-triple"
    summary_payload = {
        "status": benchmark_status,
        "base_image": base_image,
        "unopt_image": unopt_image if triple_mode_enabled else "",
        "opt_image": opt_image,
        "selector_mode": selector_mode,
        "workload_profile": workload_profile,
        "self_starting_service": bool(self_starting_service),
        "workload_profile_capabilities": list(workload_profile_capabilities),
        "workload_effective_capabilities": list(workload_effective_capabilities),
        "workload_profile_hooks": dict(workload_profile_hooks or {}),
        "workload_effective_hooks": dict(workload_effective_hooks or {}),
        "container_runtime": effective_container_runtime.summary(),
        "namespace": namespace,
        "runs": runs,
        "mode": mode,
        "comparison_mode": comparison_mode,
        "ready_ms_semantics": (
            "baseline_full_pull_ready_ms_vs_unoptimized_lazy_ready_ms_vs_optimized_lazy_ready_ms"
            if triple_mode_enabled
            else (
                "baseline_full_pull_ready_ms_vs_optimized_lazy_ready_ms"
                if lazy_mode_enabled
                else ("end_to_end_ready_ms" if mode == "ready" else "run_ms")
            )
        ),
        "lazy_mode_enabled": lazy_mode_enabled,
        "cold_mode": cold_mode,
        "hard_cold": hard_cold,
        "pull_policy": pull_policy,
        "drop_local_image_ref": drop_local_image_ref,
        "snapshotter": snapshotter,
        "base_snapshotter": effective_base_snapshotter,
        "opt_snapshotter": effective_opt_snapshotter,
        "baseline_stargz": baseline_stargz,
        "csv": str(csv_path),
        "log_dir": str(log_dir_path),
        "baseline_median_ready_ms": baseline_median_ready_ms,
        "optimized_median_ready_ms": optimized_median_ready_ms,
        "improvement_pct": improvement_pct,
        "baseline_median_post_pull_ready_ms": baseline_median_post_pull_ready_ms,
        "optimized_median_post_pull_ready_ms": optimized_median_post_pull_ready_ms,
        "post_pull_ready_improvement_pct": post_pull_ready_improvement_pct,
        "baseline_median_end_to_end_ready_ms": baseline_median_end_to_end_ready_ms,
        "optimized_median_end_to_end_ready_ms": optimized_median_end_to_end_ready_ms,
        "end_to_end_improvement_pct": end_to_end_improvement_pct,
        "baseline_full_pull_ms": baseline_full_pull_ms if lazy_mode_enabled else None,
        "optimized_lazy_prepare_ms": optimized_lazy_prepare_ms if lazy_mode_enabled else None,
        "optimized_lazy_ready_ms": optimized_median_lazy_ready_ms if lazy_mode_enabled else None,
        "baseline_median_full_pull_ready_ms": (
            triple_baseline_median_full_pull_ready_ms if triple_mode_enabled else (baseline_median_full_pull_ready_ms if lazy_mode_enabled else None)
        ),
        "unoptimized_median_lazy_ready_ms": unoptimized_median_lazy_ready_ms if triple_mode_enabled else None,
        "optimized_median_lazy_ready_ms": (
            triple_optimized_median_lazy_ready_ms if triple_mode_enabled else (optimized_median_lazy_ready_ms if lazy_mode_enabled else None)
        ),
        "lazy_ready_improvement_pct": lazy_ready_improvement_pct if comparison_mode == "raw-full-vs-estargz-lazy" else None,
        "unoptimized_vs_raw_improvement_pct": unoptimized_vs_raw_improvement_pct if triple_mode_enabled else None,
        "optimized_vs_raw_improvement_pct": optimized_vs_raw_improvement_pct if triple_mode_enabled else None,
        "optimized_vs_unoptimized_improvement_pct": optimized_vs_unoptimized_improvement_pct if triple_mode_enabled else None,
        "rows": normalized_rows,
        "summary": summary,
        "valid": bool(summary.get("valid", False)),
        "invalid_reasons": list(summary.get("invalid_reasons") or []),
        "strict_registry": strict_registry_payload or {},
        "strict_proof": strict_proof_payload or {},
        "warnings": benchmark_warnings,
    }
    _write_json(result_path, summary_payload)

    return BenchmarkResult(
        env_check_base_path=env_check_base_path,
        env_check_opt_path=env_check_opt_path,
        policy_path=policy_path,
        stage_log_path=stage_log_path,
        csv_path=csv_path,
        log_dir=log_dir_path,
        result_path=result_path,
        base_image=base_image,
        opt_image=opt_image,
        workload_profile=workload_profile,
        valid=bool(summary.get("valid", False)),
        invalid_reasons=list(summary.get("invalid_reasons") or []),
        warnings=benchmark_warnings,
        summary_payload=summary_payload,
        strict_input_path=strict_input_path if strict_registry_payload is not None else None,
        strict_proof_path=strict_proof_path if strict_proof_payload is not None else None,
    )
