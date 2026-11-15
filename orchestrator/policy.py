from __future__ import annotations

from typing import Any

from orchestrator.llm import resolve_api_key
from orchestrator.product_policy import enforced_supports_payload


POLICY_VERSION = 1

REQUIRED_ONECLICK_CHECKS = (
    "os_linux",
    "repo_layout",
    "privileged_user",
    "cmd_nerdctl",
    "cmd_curl",
    "cmd_awk",
    "cmd_sed",
    "cmd_python3",
    "cmd_ss",
    "sampler_binary",
    "kernel_btf",
    "nerdctl_runtime",
)

REQUIRED_APPLY_CHECKS = (
    "os_linux",
    "repo_layout",
    "privileged_user",
    "cmd_nerdctl",
    "cmd_python3",
    "nerdctl_runtime",
)

REQUIRED_BENCHMARK_CHECKS = (
    "os_linux",
    "repo_layout",
    "privileged_user",
    "cmd_nerdctl",
    "cmd_curl",
    "cmd_awk",
    "cmd_ss",
    "cmd_ctr-remote",
    "cmd_jq_benchmark",
    "nerdctl_runtime",
    "ctr_remote_runtime",
    "snapshotter_plugin",
    "image_local",
)


def _check_map(env_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {check["id"]: check for check in env_payload.get("checks", [])}


def _status(checks: dict[str, dict[str, Any]], check_id: str) -> str:
    return str(checks.get(check_id, {}).get("status") or "missing")


def _launcher_capability_statuses(checks: dict[str, dict[str, Any]]) -> tuple[bool, bool]:
    aggregate_status = _status(checks, "host_gated_startup_launcher")
    static_status = _status(checks, "host_gated_startup_launcher_static")
    dynamic_status = _status(checks, "host_gated_startup_launcher_dynamic")

    static_available = static_status == "pass" or (static_status == "missing" and aggregate_status == "pass")
    dynamic_available = dynamic_status == "pass" or (dynamic_status == "missing" and aggregate_status == "pass")
    return static_available, dynamic_available


def evaluate_oneclick_policy(
    env_payload: dict[str, Any],
    *,
    startup_mode: str,
    startup_backend_preference: str = "auto",
    llm_enabled: bool,
) -> dict[str, Any]:
    checks = _check_map(env_payload)
    errors: list[str] = []
    warnings: list[str] = []

    for check_id in REQUIRED_ONECLICK_CHECKS:
        status = _status(checks, check_id)
        if status != "pass":
            summary = checks.get(check_id, {}).get("summary") or check_id
            errors.append(f"{summary} ({check_id}) status={status}")

    if startup_mode in {"auto", "gated"}:
        gated_status = _status(checks, "image_gated_startup_shell")
        launcher_static_available, launcher_dynamic_available = _launcher_capability_statuses(checks)
        if startup_backend_preference == "prefer-host-launcher":
            if launcher_static_available:
                warnings.append(
                    "startup backend preference will prefer the host launcher backend before image-shell gated startup"
                )
            elif launcher_dynamic_available:
                warnings.append(
                    "startup backend preference will use the experimental dynamic host launcher backend before image-shell gated startup because static launcher build support is unavailable"
                )
            elif gated_status == "pass":
                warnings.append(
                    "startup backend preference requested host launcher, but build support is unavailable; gated startup will fall back to image shell"
                )
            elif startup_mode == "gated":
                errors.append(
                    "gated startup with prefer-host-launcher requires either host launcher build support (cc/gcc/clang) or /bin/sh in the image"
                )
        elif startup_backend_preference == "prefer-image-shell":
            if gated_status == "pass":
                pass
            elif launcher_static_available:
                warnings.append(
                    "startup backend preference requested image-shell gating, but /bin/sh is unavailable; gated startup will fall back to the host launcher backend"
                )
            elif launcher_dynamic_available:
                warnings.append(
                    "startup backend preference requested image-shell gating, but /bin/sh is unavailable and static launcher build support is unavailable; gated startup will fall back to the experimental dynamic host launcher backend"
                )
            elif startup_mode == "gated":
                errors.append(
                    "gated startup with prefer-image-shell still requires either /bin/sh in the image or host launcher build support (cc/gcc/clang)"
                )
        elif gated_status == "pass":
            pass
        elif launcher_static_available:
            warnings.append("gated startup will use the host launcher backend because /bin/sh is unavailable in the image")
        elif launcher_dynamic_available:
            warnings.append(
                "gated startup will use the experimental dynamic host launcher backend because /bin/sh is unavailable and static launcher build support is unavailable"
            )
        else:
            if startup_mode == "gated":
                errors.append(
                    "gated startup requires either /bin/sh in the image or host launcher build support (cc/gcc/clang)"
                )

    snapshotter_status = _status(checks, "snapshotter_plugin")
    if snapshotter_status not in ("pass", "missing"):
        warnings.append("snapshotter plugin state is non-pass; optimization/benchmark path may be degraded")

    redis_status = _status(checks, "redis_ping")
    if redis_status == "fail":
        warnings.append("Redis is not part of the supported truth path; file sink remains the only default source of truth")

    api_key, api_key_source = resolve_api_key()
    if llm_enabled and not api_key:
        warnings.append(
            "LLM API mode was explicitly requested but no API key is set; run will export a web-LLM fallback prompt package"
        )

    return {
        "policy_version": POLICY_VERSION,
        "ok": not errors,
        "startup_mode": startup_mode,
        "startup_backend_preference": startup_backend_preference,
        "llm_enabled": llm_enabled,
        "llm_api_key_present": bool(api_key),
        "llm_api_key_source": api_key_source,
        "enforced_supports": enforced_supports_payload(),
        "errors": errors,
        "warnings": warnings,
    }


def evaluate_apply_policy(
    env_payload: dict[str, Any],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    checks = _check_map(env_payload)
    errors: list[str] = []
    warnings: list[str] = []

    for check_id in REQUIRED_APPLY_CHECKS:
        status = _status(checks, check_id)
        if status != "pass":
            summary = checks.get(check_id, {}).get("summary") or check_id
            errors.append(f"{summary} ({check_id}) status={status}")

    image_local_status = _status(checks, "image_local")
    if dry_run:
        if image_local_status != "pass":
            warnings.append("image_local is non-pass, but apply dry-run is allowed to continue without local image verification")
    elif image_local_status != "pass":
        errors.append("apply requires the source image to be locally inspectable via nerdctl")

    ctr_prefetch_status = _status(checks, "ctr_remote_prefetch_list")
    ctr_runtime_status = _status(checks, "ctr_remote_runtime")
    estargzify_status = _status(checks, "cmd_estargzify")
    stargzify_status = _status(checks, "cmd_stargzify")

    if ctr_prefetch_status == "pass":
        if ctr_runtime_status != "pass":
            errors.append("ctr-remote prefetch optimization is detected, but ctr-remote cannot connect to containerd")
    elif estargzify_status == "pass" or stargzify_status == "pass":
        warnings.append("apply will use estargzify/stargzify fallback instead of ctr-remote optimize --prefetch-list")
    else:
        errors.append("apply requires ctr-remote optimize --prefetch-list or estargzify/stargzify fallback")

    return {
        "policy_version": POLICY_VERSION,
        "ok": not errors,
        "dry_run": dry_run,
        "enforced_supports": enforced_supports_payload(),
        "errors": errors,
        "warnings": warnings,
    }


def evaluate_benchmark_policy(
    base_env_payload: dict[str, Any],
    opt_env_payload: dict[str, Any],
    *,
    require_local_image: bool = True,
    require_content_store_purge: bool = False,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []

    for label, payload in (("baseline", base_env_payload), ("optimized", opt_env_payload)):
        checks = _check_map(payload)
        for check_id in REQUIRED_BENCHMARK_CHECKS:
            if check_id == "image_local" and not require_local_image:
                continue
            status = _status(checks, check_id)
            if status != "pass":
                summary = checks.get(check_id, {}).get("summary") or check_id
                errors.append(f"{label}: {summary} ({check_id}) status={status}")
        if not require_local_image and _status(checks, "image_local") != "pass":
            warnings.append(
                f"{label}: image_local is non-pass, but benchmark pull_policy allows registry resolution at run time"
            )
        if require_content_store_purge and _status(checks, "cmd_ctr") != "pass":
            errors.append(f"{label}: strict content store purge requires ctr (cmd_ctr) status={_status(checks, 'cmd_ctr')}")
        if _status(checks, "cni_bridge_plugin") != "pass":
            warnings.append(
                f"{label}: CNI bridge plugin is unavailable; busy loopback ready-url probes cannot use published-port remap"
            )

    return {
        "policy_version": POLICY_VERSION,
        "ok": not errors,
        "enforced_supports": enforced_supports_payload(),
        "require_local_image": require_local_image,
        "require_content_store_purge": require_content_store_purge,
        "errors": errors,
        "warnings": warnings,
    }
