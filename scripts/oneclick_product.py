#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator.manifest import ManifestStore
from orchestrator.container_runtime import (
    ContainerRuntimeSpec,
    container_runtime_spec,
    inherit_container_runtime,
)
from orchestrator.llm import (
    FORMAL_LLM_PROVIDERS,
    describe_missing_api_key,
    provider_requires_api_key,
    resolve_llm_provider_config,
)
from orchestrator.runtime import nerdctl
from orchestrator.stages.analyze import run_analyze_stage
from orchestrator.stages.apply import run_apply_stage
from orchestrator.stages.benchmark import run_benchmark_stage
from orchestrator.stages.export_enrich import run_export_enrich_stage
from orchestrator.stages.llm_optimize import run_llm_optimize_stage
from orchestrator.stages.preflight import run_preflight
from orchestrator.stages.publish import run_publish_stage
from orchestrator.stages.report import run_report_stage
from orchestrator.stages.sample import run_sample_stage
from orchestrator.workloads import (
    resolve_benchmark_workload,
    resolve_sample_workload,
    validate_benchmark_workload_resolution,
    validate_sample_workload_resolution,
    workload_choices,
)

try:
    import yaml  # type: ignore

    _HAVE_PYYAML = True
except ImportError:
    _HAVE_PYYAML = False


FORMAL_RUN_MODES = ("deterministic", "llm-auto", "llm-manual")
FORMAL_RUN_PRESETS = (
    "tfpy-half-plus-two-io1g",
    "tf-serving-efficientnet-v2-b0",
    "tf-serving-half-plus-two",
)
MANUAL_RESPONSE_CONTRACT = (
    "strict JSON object with P0/P1/P2/P3 absolute-path arrays only; "
    "legacy YAML with the same four priorities is also accepted for compatibility"
)
RUN_CONFIG_FIELD_SPECS: dict[str, dict[str, object]] = {
    "preset": {
        "attr": "preset",
        "type": "str",
        "choices": FORMAL_RUN_PRESETS,
        "cli_options": ("--preset",),
    },
    "workload": {
        "attr": "workload",
        "type": "str",
        "choices": workload_choices(),
        "cli_options": ("--workload",),
    },
    "model_host": {
        "attr": "model_host",
        "type": "str",
        "cli_options": ("--model-host",),
    },
    "mode": {
        "attr": "run_mode",
        "type": "str",
        "choices": FORMAL_RUN_MODES,
        "cli_options": ("--mode", "--llm", "--manual-web", "--manual-response-file"),
    },
    "startup_mode": {
        "attr": "startup_mode",
        "type": "str",
        "choices": ("auto", "legacy", "gated"),
        "cli_options": ("--startup-mode",),
    },
    "apply": {
        "attr": "apply",
        "type": "bool",
        "cli_options": ("--apply",),
    },
    "benchmark": {
        "attr": "benchmark",
        "type": "bool",
        "cli_options": ("--benchmark",),
    },
    "bench_runs": {
        "attr": "bench_runs",
        "type": "int",
        "cli_options": ("--bench-runs",),
    },
    "dst_image": {
        "attr": "dst_image",
        "type": "str",
        "cli_options": ("--dst-image",),
    },
    "duration": {
        "attr": "duration",
        "type": "int",
        "cli_options": ("--duration",),
    },
    "llm_provider": {
        "attr": "llm_provider",
        "type": "str",
        "choices": FORMAL_LLM_PROVIDERS,
        "cli_options": ("--llm-provider",),
    },
    "llm_api_key_env": {
        "attr": "llm_api_key_env",
        "type": "str",
        "cli_options": ("--llm-api-key-env",),
    },
    "llm_api_base": {
        "attr": "llm_api_base",
        "type": "str",
        "cli_options": ("--llm-api-base",),
    },
    "llm_model": {
        "attr": "llm_model",
        "type": "str",
        "cli_options": ("--llm-model",),
    },
    "llm_reasoning_effort": {
        "attr": "llm_reasoning_effort",
        "type": "str",
        "cli_options": ("--llm-reasoning-effort",),
    },
    "llm_timeout_sec": {
        "attr": "llm_timeout_sec",
        "type": "int",
        "cli_options": ("--llm-timeout-sec",),
    },
    "ready_timeout": {
        "attr": "ready_timeout",
        "type": "int",
        "cli_options": ("--ready-timeout",),
    },
    "bench_workload": {
        "attr": "bench_workload",
        "type": "str",
        "choices": workload_choices(),
        "cli_options": ("--bench-workload",),
    },
    "bench_model_host": {
        "attr": "bench_model_host",
        "type": "str",
        "cli_options": ("--bench-model-host",),
    },
    "bench_mode": {
        "attr": "bench_mode",
        "type": "str",
        "choices": ("struct", "ready"),
        "cli_options": ("--bench-mode",),
    },
    "bench_timeout": {
        "attr": "bench_timeout",
        "type": "int",
        "cli_options": ("--bench-timeout",),
    },
    "bench_baseline_stargz": {
        "attr": "bench_baseline_stargz",
        "type": "bool",
        "cli_options": ("--bench-baseline-stargz",),
    },
    "bench_cold_mode": {
        "attr": "bench_cold_mode",
        "type": "str",
        "choices": ("local-cold", "registry-cold-strict"),
        "cli_options": ("--bench-cold-mode",),
    },
    "bench_comparison_mode": {
        "attr": "bench_comparison_mode",
        "type": "str",
        "choices": ("same-snapshotter", "raw-vs-estargz", "raw-full-vs-estargz-lazy", "raw-full-vs-estargz-lazy-triple"),
        "cli_options": ("--bench-comparison-mode",),
    },
}


def default_work_dir(repo_root: Path) -> Path:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return repo_root / "optimizer" / "work" / run_id


def manifest_image(manifest: ManifestStore, cli_image: str | None = None) -> str | None:
    if cli_image:
        return cli_image
    options_image = manifest.data.get("options", {}).get("image")
    if options_image:
        return str(options_image)
    legacy_image = manifest.data.get("image")
    if legacy_image:
        return str(legacy_image)
    return None


def resolved_selector_mode(manifest: ManifestStore, cli_selector_mode: str | None = None) -> str:
    if cli_selector_mode in {"file", "chunk-shadow"}:
        return cli_selector_mode
    manifest_selector_mode = manifest.data.get("options", {}).get("selector_mode")
    if manifest_selector_mode in {"file", "chunk-shadow"}:
        return str(manifest_selector_mode)
    return "file"


def _legacy_run_mode_from_flags(args: argparse.Namespace) -> str:
    if bool(getattr(args, "manual_web", False)) or bool(getattr(args, "manual_response_file", None)):
        return "llm-manual"
    if bool(getattr(args, "llm", False)):
        return "llm-auto"
    return "deterministic"


def _resolve_run_mode(args: argparse.Namespace) -> str:
    explicit_mode = str(getattr(args, "run_mode", "") or "").strip()
    legacy_mode = _legacy_run_mode_from_flags(args)
    if explicit_mode and explicit_mode != legacy_mode and legacy_mode != "deterministic":
        if explicit_mode == "llm-auto":
            raise ValueError("--mode llm-auto cannot be combined with --manual-web or --manual-response-file")
        if explicit_mode == "llm-manual":
            raise ValueError("--mode llm-manual cannot be combined with legacy --llm unless manual-mode inputs are also present")
        raise ValueError(
            "--mode deterministic cannot be combined with legacy LLM flags (--llm, --manual-web, --manual-response-file)"
        )
    if explicit_mode and legacy_mode == "deterministic":
        return explicit_mode
    return legacy_mode


def _run_uses_llm_api(args: argparse.Namespace) -> bool:
    return getattr(args, "command", None) == "run" and getattr(args, "run_mode", "deterministic") == "llm-auto"


def _argv_has_option(argv: list[str], option_name: str) -> bool:
    return any(token == option_name or token.startswith(f"{option_name}=") for token in argv)


def _tiny_yaml_parse_scalar(raw_value: str, *, line_number: int) -> object:
    value = raw_value.strip()
    if not value:
        raise ValueError(f"config YAML line {line_number} must use a scalar value")
    if value.startswith('"') or value.startswith("'"):
        if len(value) < 2 or value[-1] != value[0]:
            raise ValueError(f"config YAML line {line_number} has an unterminated quoted string")
        return value[1:-1]
    normalized = value.lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    if re.fullmatch(r"-?[0-9]+", value):
        return int(value)
    return value


def _tiny_yaml_load_mapping(content: str) -> dict[str, object]:
    payload: dict[str, object] = {}
    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line[:1].isspace():
            raise ValueError(f"config YAML line {line_number} uses unsupported nested indentation")
        if ":" not in line:
            raise ValueError(f"config YAML line {line_number} must use key: value syntax")
        key, raw_value = line.split(":", 1)
        normalized_key = key.strip()
        if not normalized_key:
            raise ValueError(f"config YAML line {line_number} is missing a key name")
        if normalized_key in payload:
            raise ValueError(f"config YAML line {line_number} redefines key {normalized_key!r}")
        payload[normalized_key] = _tiny_yaml_parse_scalar(raw_value, line_number=line_number)
    return payload


def _load_yaml_mapping(path: Path) -> dict[str, object]:
    content = path.read_text(encoding="utf-8")
    if _HAVE_PYYAML:
        try:
            payload = yaml.safe_load(content)  # type: ignore[name-defined]
        except Exception as exc:
            raise ValueError(f"failed to parse run config YAML {path}: {exc}") from exc
        if payload is None:
            return {}
        if not isinstance(payload, dict):
            raise ValueError(f"run config YAML {path} must contain a top-level mapping")
        return {str(key): value for key, value in payload.items()}
    try:
        return _tiny_yaml_load_mapping(content)
    except ValueError as exc:
        raise ValueError(f"failed to parse run config YAML {path}: {exc}") from exc


def _validate_run_config_payload(path: Path, payload: dict[str, object]) -> dict[str, object]:
    unsupported_fields = sorted(key for key in payload if key not in RUN_CONFIG_FIELD_SPECS)
    if unsupported_fields:
        raise ValueError(
            f"run config {path} contains unsupported fields: {', '.join(unsupported_fields)}"
        )
    normalized: dict[str, object] = {}
    for key, value in payload.items():
        spec = RUN_CONFIG_FIELD_SPECS[key]
        expected_type = str(spec["type"])
        if expected_type == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"run config field {key!r} must be a boolean")
        elif expected_type == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"run config field {key!r} must be an integer")
        elif expected_type == "str":
            if not isinstance(value, str):
                raise ValueError(f"run config field {key!r} must be a string")
        else:
            raise ValueError(f"internal error: unsupported config field type for {key!r}")
        choices = tuple(spec.get("choices") or ())
        if choices and value not in choices:
            raise ValueError(
                f"run config field {key!r} must be one of: {', '.join(str(item) for item in choices)}"
            )
        normalized[key] = value
    return normalized


def _load_run_config(path_value: str | None) -> tuple[Path | None, dict[str, object]]:
    if not path_value:
        return None, {}
    path = Path(path_value).expanduser().resolve()
    if not path.exists():
        raise ValueError(f"run config file does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"run config path is not a file: {path}")
    payload = _load_yaml_mapping(path)
    return path, _validate_run_config_payload(path, payload)


def _preset_defaults(preset_name: str | None) -> dict[str, str]:
    normalized = str(preset_name or "").strip()
    if normalized == "tfpy-half-plus-two-io1g":
        return {
            "workload": "tfpy-half-plus-two-io1g",
            "run_mode": "deterministic",
            "startup_mode": "auto",
        }
    if normalized == "tf-serving-efficientnet-v2-b0":
        return {
            "workload": "tf-serving-efficientnet-v2-b0",
            "run_mode": "deterministic",
            "startup_mode": "auto",
        }
    if normalized == "tf-serving-half-plus-two":
        return {
            "workload": "tf-serving-half-plus-two",
            "run_mode": "deterministic",
            "startup_mode": "auto",
        }
    return {}


def _recommended_benchmark_mode(workload_name: str | None) -> str | None:
    normalized = str(workload_name or "").strip()
    if normalized == "ready-serving-bootstrap-v2":
        return "ready"
    if normalized == "ready-serving-layout-trap-v3":
        return "ready"
    if normalized == "ready-serving-layout-trap-v4":
        return "ready"
    if normalized == "ready-serving-layout-trap-v5":
        return "ready"
    if normalized == "tfpy-half-plus-two-io1g":
        return "ready"
    if normalized == "tf-serving-efficientnet-v2-b0":
        return "ready"
    if normalized == "tf-serving-half-plus-two":
        return "ready"
    return None


def _auto_dst_image_for_apply(src_image: str) -> str:
    normalized = str(src_image or "").strip()
    if not normalized:
        raise ValueError("--apply without --dst-image requires a source image reference")
    if "@" in normalized:
        raise ValueError(
            "--apply without --dst-image only supports tag/name image references; "
            "digest references must pass --dst-image explicitly"
        )
    last_slash = normalized.rfind("/")
    last_colon = normalized.rfind(":")
    if last_colon > last_slash:
        repo = normalized[:last_colon]
        tag = normalized[last_colon + 1 :]
        if not tag:
            raise ValueError(f"cannot auto-generate --dst-image from malformed image reference: {normalized}")
        return f"{repo}:{tag}-optimized"
    return f"{normalized}:optimized"


def _normalize_run_parameters(args: argparse.Namespace, *, raw_argv: list[str]) -> tuple[dict[str, dict[str, str]], Path | None]:
    if getattr(args, "command", None) != "run":
        return {}, None
    resolved: dict[str, dict[str, str]] = {}
    config_path, config_payload = _load_run_config(getattr(args, "config", None))
    for field_name, spec in RUN_CONFIG_FIELD_SPECS.items():
        if field_name not in config_payload:
            continue
        cli_options = tuple(spec.get("cli_options") or ())
        if any(_argv_has_option(raw_argv, option_name) for option_name in cli_options):
            continue
        attr_name = str(spec["attr"])
        value = config_payload[field_name]
        setattr(args, attr_name, value)
        resolved[field_name] = {
            "value": str(value),
            "source": f"defaulted from --config {config_path}",
        }

    preset_name = str(getattr(args, "preset", "") or "").strip()
    for option_name, value in _preset_defaults(preset_name).items():
        current_value = getattr(args, option_name, None)
        if current_value is None:
            setattr(args, option_name, value)
            resolved[option_name] = {
                "value": str(value),
                "source": f"defaulted from --preset {preset_name}",
            }

    if getattr(args, "workload", None) is None:
        args.workload = "none"
    if getattr(args, "startup_mode", None) is None:
        args.startup_mode = "auto"

    if getattr(args, "bench_workload", None) is None:
        args.bench_workload = args.workload
        resolved["bench_workload"] = {
            "value": str(args.bench_workload),
            "source": f"inherited from --workload ({args.workload})",
        }

    if getattr(args, "bench_model_host", None) is None and getattr(args, "model_host", None):
        args.bench_model_host = args.model_host
        resolved["bench_model_host"] = {
            "value": str(args.bench_model_host),
            "source": f"inherited from --model-host ({args.model_host})",
        }

    recommended_bench_mode = _recommended_benchmark_mode(getattr(args, "bench_workload", None))
    if getattr(args, "bench_mode", None) is None:
        args.bench_mode = recommended_bench_mode or "struct"
        if recommended_bench_mode is not None:
            resolved["bench_mode"] = {
                "value": str(args.bench_mode),
                "source": f"recommended default for --bench-workload ({args.bench_workload})",
            }
        else:
            resolved["bench_mode"] = {
                "value": str(args.bench_mode),
                "source": "defaulted because no workload-specific benchmark mode recommendation applies",
            }

    if getattr(args, "apply", False) and not getattr(args, "dst_image", None):
        args.dst_image = _auto_dst_image_for_apply(args.image)
        resolved["dst_image"] = {
            "value": str(args.dst_image),
            "source": f"auto-generated from source image ({args.image})",
        }

    return resolved, config_path


def _apply_benchmark_cold_mode_defaults(args: argparse.Namespace, *, raw_argv: list[str]) -> None:
    def _assign_or_reject(option_name: str, attr_name: str, expected_value: object, message: str) -> None:
        current_value = getattr(args, attr_name)
        if _argv_has_option(raw_argv, option_name) and current_value != expected_value:
            raise ValueError(message)
        setattr(args, attr_name, expected_value)

    if getattr(args, "command", None) == "benchmark" and getattr(args, "cold_mode", "local-cold") == "registry-cold-strict":
        _assign_or_reject("--mode", "mode", "ready", "--cold-mode registry-cold-strict requires --mode ready")
        if getattr(args, "comparison_mode", "same-snapshotter") == "same-snapshotter":
            _assign_or_reject(
                "--snapshotter",
                "snapshotter",
                "stargz",
                "--cold-mode registry-cold-strict requires --snapshotter stargz",
            )
            if _argv_has_option(raw_argv, "--base-snapshotter") and getattr(args, "base_snapshotter", None) != "stargz":
                raise ValueError("--cold-mode registry-cold-strict requires --base-snapshotter stargz")
            if _argv_has_option(raw_argv, "--opt-snapshotter") and getattr(args, "opt_snapshotter", None) != "stargz":
                raise ValueError("--cold-mode registry-cold-strict requires --opt-snapshotter stargz")
            setattr(args, "base_snapshotter", "stargz")
            setattr(args, "opt_snapshotter", "stargz")
            setattr(args, "baseline_stargz", True)
        _assign_or_reject(
            "--pull-policy",
            "pull_policy",
            "always",
            "--cold-mode registry-cold-strict requires --pull-policy always",
        )
        if _argv_has_option(raw_argv, "--soft-cold"):
            raise ValueError("--cold-mode registry-cold-strict does not allow --soft-cold")
        setattr(args, "hard_cold", True)
        if _argv_has_option(raw_argv, "--no-drop-cache"):
            raise ValueError("--cold-mode registry-cold-strict does not allow --no-drop-cache")
        setattr(args, "drop_page_cache", True)
        if _argv_has_option(raw_argv, "--no-clear-stargz"):
            raise ValueError("--cold-mode registry-cold-strict does not allow --no-clear-stargz")
        setattr(args, "clear_stargz_cache", True)
        setattr(args, "drop_local_image_ref", True)

    if getattr(args, "command", None) == "run" and getattr(args, "bench_cold_mode", "local-cold") == "registry-cold-strict":
        if _argv_has_option(raw_argv, "--bench-mode") and getattr(args, "bench_mode", None) != "ready":
            raise ValueError("--bench-cold-mode registry-cold-strict requires --bench-mode ready")
        setattr(args, "bench_mode", "ready")
        if getattr(args, "bench_comparison_mode", "same-snapshotter") == "same-snapshotter":
            if _argv_has_option(raw_argv, "--bench-snapshotter") and getattr(args, "bench_snapshotter", None) != "stargz":
                raise ValueError("--bench-cold-mode registry-cold-strict requires --bench-snapshotter stargz")
            setattr(args, "bench_snapshotter", "stargz")
            if _argv_has_option(raw_argv, "--bench-base-snapshotter") and getattr(args, "bench_base_snapshotter", None) != "stargz":
                raise ValueError("--bench-cold-mode registry-cold-strict requires --bench-base-snapshotter stargz")
            if _argv_has_option(raw_argv, "--bench-opt-snapshotter") and getattr(args, "bench_opt_snapshotter", None) != "stargz":
                raise ValueError("--bench-cold-mode registry-cold-strict requires --bench-opt-snapshotter stargz")
            setattr(args, "bench_base_snapshotter", "stargz")
            setattr(args, "bench_opt_snapshotter", "stargz")
            setattr(args, "bench_baseline_stargz", True)
        if _argv_has_option(raw_argv, "--bench-pull-policy") and getattr(args, "bench_pull_policy", None) != "always":
            raise ValueError("--bench-cold-mode registry-cold-strict requires --bench-pull-policy always")
        setattr(args, "bench_pull_policy", "always")
        if _argv_has_option(raw_argv, "--bench-soft-cold"):
            raise ValueError("--bench-cold-mode registry-cold-strict does not allow --bench-soft-cold")
        setattr(args, "bench_soft_cold", False)
        if _argv_has_option(raw_argv, "--bench-no-drop-cache"):
            raise ValueError("--bench-cold-mode registry-cold-strict does not allow --bench-no-drop-cache")
        setattr(args, "bench_no_drop_cache", False)
        if _argv_has_option(raw_argv, "--bench-no-clear-stargz"):
            raise ValueError("--bench-cold-mode registry-cold-strict does not allow --bench-no-clear-stargz")
        setattr(args, "bench_no_clear_stargz", False)
        setattr(args, "bench_drop_local_image_ref", True)


def _apply_benchmark_comparison_mode_defaults(args: argparse.Namespace, *, raw_argv: list[str]) -> None:
    if getattr(args, "command", None) == "benchmark":
        if getattr(args, "comparison_mode", "same-snapshotter") not in {"raw-vs-estargz", "raw-full-vs-estargz-lazy", "raw-full-vs-estargz-lazy-triple"}:
            return
        if not _argv_has_option(raw_argv, "--base-snapshotter") and not getattr(args, "baseline_stargz", False):
            setattr(args, "base_snapshotter", "overlayfs")
        if not _argv_has_option(raw_argv, "--opt-snapshotter"):
            setattr(args, "opt_snapshotter", "stargz")
        return

    if getattr(args, "command", None) == "run":
        if getattr(args, "bench_comparison_mode", "same-snapshotter") not in {"raw-vs-estargz", "raw-full-vs-estargz-lazy", "raw-full-vs-estargz-lazy-triple"}:
            return
        if not _argv_has_option(raw_argv, "--bench-base-snapshotter") and not getattr(args, "bench_baseline_stargz", False):
            setattr(args, "bench_base_snapshotter", "overlayfs")
        if not _argv_has_option(raw_argv, "--bench-opt-snapshotter"):
            setattr(args, "bench_opt_snapshotter", "stargz")


def _validate_benchmark_comparison_mode_inputs(args: argparse.Namespace) -> None:
    triple_mode = "raw-full-vs-estargz-lazy-triple"
    if (
        getattr(args, "command", None) == "benchmark"
        and getattr(args, "comparison_mode", "same-snapshotter") == triple_mode
        and not str(getattr(args, "unopt_image", "") or "").strip()
    ):
        raise ValueError("--comparison-mode raw-full-vs-estargz-lazy-triple requires --unopt-image")
    if (
        getattr(args, "command", None) == "run"
        and bool(getattr(args, "benchmark", False))
        and getattr(args, "bench_comparison_mode", "same-snapshotter") == triple_mode
        and not str(getattr(args, "bench_unopt_image", "") or "").strip()
    ):
        raise ValueError("--bench-comparison-mode raw-full-vs-estargz-lazy-triple requires --bench-unopt-image")


def _apply_direct_benchmark_mode_defaults(args: argparse.Namespace, *, raw_argv: list[str]) -> None:
    if getattr(args, "command", None) != "benchmark":
        return
    if _argv_has_option(raw_argv, "--mode"):
        return
    workload_name = str(getattr(args, "workload", "") or "").strip()
    if workload_name not in {
        "ready-serving-bootstrap-v2",
        "ready-serving-layout-trap-v3",
        "ready-serving-layout-trap-v4",
        "ready-serving-layout-trap-v5",
    }:
        return
    recommended_mode = _recommended_benchmark_mode(workload_name)
    if recommended_mode is not None:
        setattr(args, "mode", recommended_mode)


def optimized_image_from_manifest(
    manifest: ManifestStore,
    *,
    work_dir: Path,
    cli_image: str | None = None,
    selector_mode: str = "file",
) -> str | None:
    if cli_image:
        return cli_image
    candidates: list[object] = []
    if selector_mode == "chunk-shadow":
        candidates.extend(
            [
                manifest.data.get("artifacts", {}).get("optimized_image_chunk_shadow"),
                work_dir / "optimized_image_chunk_shadow.txt",
            ]
        )
    else:
        candidates.extend(
            [
                manifest.data.get("artifacts", {}).get("optimized_image"),
                work_dir / "optimized_image.txt",
            ]
        )
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
    return None


def _load_optional_json_dict(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _update_json_dict(path: Path, updates: dict) -> None:
    payload = _load_optional_json_dict(path) or {}
    payload.update(updates)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _resolve_publish_source_image(
    manifest: ManifestStore,
    *,
    work_dir: Path,
    cli_src_image: str | None,
    use_optimized: bool,
    selector_mode: str,
) -> tuple[str | None, bool, str]:
    if cli_src_image and use_optimized:
        raise ValueError("--src-image and --optimized cannot be used together")
    if use_optimized:
        src_image = optimized_image_from_manifest(
            manifest,
            work_dir=work_dir,
            selector_mode=selector_mode,
        )
        return src_image, True, selector_mode

    src_image = manifest_image(manifest, cli_src_image)
    if not src_image:
        return None, False, selector_mode
    for candidate_mode in ("file", "chunk-shadow"):
        optimized_image = optimized_image_from_manifest(
            manifest,
            work_dir=work_dir,
            selector_mode=candidate_mode,
        )
        if optimized_image and optimized_image == src_image:
            return src_image, True, candidate_mode
    return src_image, False, selector_mode
def _report_stage_artifacts(result: object) -> dict[str, str]:
    return {
        "run_report_json": str(result.report_json_path),
        "run_report_md": str(result.report_md_path),
    }


def _load_json_dict(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _stage_display_name(stage_name: str) -> str:
    return str(stage_name or "").replace("_", "-")


def _benchmark_summary_label_order(comparison_mode: object) -> tuple[str, ...]:
    if str(comparison_mode or "").strip() == "raw-full-vs-estargz-lazy-triple":
        return ("baseline", "unoptimized_lazy", "optimized")
    return ("baseline", "optimized")


def _benchmark_lazy_full_pull_warning(label: str) -> str:
    return f"{label} path appears to have performed a full pull; lazy-pull result may be invalid"


def _benchmark_lazy_full_pull_invalid_reason(label: str) -> str:
    return f"{label} lazy path appears to have performed a full pull; result is invalid"


def _print_stage_start(stage_name: str) -> None:
    print(f"[orchestrator] stage-start {_stage_display_name(stage_name)}")


def _print_stage_pass(stage_name: str) -> None:
    print(f"[orchestrator] stage-pass {_stage_display_name(stage_name)}")


def _print_stage_fail(stage_name: str, *, detail: str | None = None) -> None:
    print(f"[orchestrator] stage-fail {_stage_display_name(stage_name)}", file=sys.stderr)
    if detail:
        print(f"  - {detail}", file=sys.stderr)


def _print_manual_pause_summary(
    *,
    work_dir: Path,
    web_input_path: Path,
    manual_instructions_path: Path,
    manual_response_path: Path,
    resume_command: str,
) -> None:
    print(f"[orchestrator] llm-manual paused work_dir={work_dir}")
    print(f"  - copy_to_web: paste {web_input_path} into the web LLM")
    print(f"  - manual_instructions: follow {manual_instructions_path}")
    print(f"  - save_response_to: write the strict JSON response to {manual_response_path}")
    print("  - response_must_be: strict JSON with only P0/P1/P2/P3 absolute-path arrays")
    print(f"  - resume_with: {resume_command}")


def _print_llm_result_summary(result: object) -> None:
    payload = _load_json_dict(getattr(result, "result_path", None))
    mode_result = str(
        payload.get("mode_result")
        or getattr(result, "mode_result", None)
        or getattr(result, "mode", None)
        or "unknown"
    )
    llm_applied = bool(payload.get("llm_applied", getattr(result, "llm_applied", False)))
    final_hints_source = str(
        payload.get("final_hints_source")
        or getattr(result, "final_hints_source", None)
        or "unknown"
    )
    print(
        f"[orchestrator] llm-summary mode_result={mode_result} "
        f"llm_applied={llm_applied} final_hints_source={final_hints_source}"
    )
    accepted_total = payload.get("accepted_total")
    if accepted_total is not None:
        print(f"  - accepted_total={accepted_total}")
    fallback_kind = str(payload.get("fallback_kind") or getattr(result, "fallback_kind", None) or "").strip()
    fallback_reason = str(payload.get("fallback_reason") or getattr(result, "fallback_reason", None) or "").strip()
    if not llm_applied and final_hints_source == "deterministic_ranked":
        detail = "  - fallback_to=deterministic"
        if fallback_kind:
            detail += f" kind={fallback_kind}"
        print(detail)
        if fallback_reason:
            print(f"  - fallback_reason={fallback_reason}")


def _print_apply_result_summary(result: object) -> None:
    payload = _load_json_dict(getattr(result, "apply_result_path", None))
    status = str(payload.get("status") or "passed")
    optimized_image_written = bool(payload.get("optimized_image_written", False))
    optimized_image = str(
        payload.get("optimized_image")
        or getattr(result, "dst_image", None)
        or ""
    ).strip()
    print(
        f"[orchestrator] apply-summary status={status} "
        f"optimized_image_written={optimized_image_written}"
    )
    if optimized_image:
        print(f"  - optimized_image={optimized_image}")
    quality_status = str(payload.get("quality_status") or "").strip()
    if quality_status:
        print(f"  - quality_status={quality_status}")
    recommendation = str(payload.get("recommendation") or "").strip()
    if recommendation:
        print(f"  - recommendation={recommendation}")


def _benchmark_summary_fields(summary_payload: dict) -> tuple[object, object, object, object]:
    status = summary_payload.get("status")
    comparison_mode = summary_payload.get("comparison_mode")
    if "summary" in summary_payload:
        summary = dict(summary_payload.get("summary") or {})
        labels = dict(summary.get("labels") or {})
        comparison = dict(summary.get("comparison") or {})
        if comparison_mode == "raw-full-vs-estargz-lazy-triple":
            baseline_median = (labels.get("baseline") or {}).get("median_full_pull_ready_ms")
            optimized_median = (labels.get("optimized") or {}).get("median_lazy_ready_ms")
            improvement_pct = comparison.get("optimized_vs_raw_improvement_pct")
        elif comparison_mode == "raw-full-vs-estargz-lazy":
            baseline_median = (labels.get("baseline") or {}).get("median_end_to_end_ready_ms")
            optimized_median = (labels.get("optimized") or {}).get("median_lazy_ready_ms")
            improvement_pct = comparison.get("median_lazy_ready_improvement_pct")
        else:
            baseline_median = (labels.get("baseline") or {}).get("median_ready_ms")
            optimized_median = (labels.get("optimized") or {}).get("median_ready_ms")
            improvement_pct = comparison.get("median_ready_improvement_pct")
        return status, baseline_median, optimized_median, improvement_pct

    benchmark_payload = summary_payload
    comparison = dict(benchmark_payload.get("comparison") or {})
    if comparison_mode == "raw-full-vs-estargz-lazy-triple":
        baseline_median = benchmark_payload.get("baseline_median_full_pull_ready_ms")
        optimized_median = benchmark_payload.get("optimized_median_lazy_ready_ms")
        improvement_pct = comparison.get(
            "optimized_vs_raw_improvement_pct",
            benchmark_payload.get("optimized_vs_raw_improvement_pct"),
        )
    elif comparison_mode == "raw-full-vs-estargz-lazy":
        baseline_median = benchmark_payload.get("baseline_median_full_pull_ready_ms")
        optimized_median = benchmark_payload.get("optimized_median_lazy_ready_ms")
        improvement_pct = comparison.get("median_lazy_ready_improvement_pct", benchmark_payload.get("lazy_ready_improvement_pct"))
    else:
        baseline_median = benchmark_payload.get("baseline_median_ready_ms")
        optimized_median = benchmark_payload.get("optimized_median_ready_ms")
        improvement_pct = comparison.get("median_ready_improvement_pct")
    return status, baseline_median, optimized_median, improvement_pct


def _benchmark_summary_metrics(summary_payload: dict) -> dict[str, object]:
    status = summary_payload.get("status")
    if "summary" in summary_payload:
        payload = summary_payload
        summary = dict(summary_payload.get("summary") or {})
    else:
        payload = summary_payload
        summary = {}
    labels = dict(summary.get("labels") or {})
    comparison = dict(summary.get("comparison") or {})
    baseline_label = dict(labels.get("baseline") or {})
    unoptimized_label = dict(labels.get("unoptimized_lazy") or {})
    optimized_label = dict(labels.get("optimized") or {})

    baseline_ready = baseline_label.get("median_ready_ms", payload.get("baseline_median_ready_ms"))
    optimized_ready = optimized_label.get("median_ready_ms", payload.get("optimized_median_ready_ms"))
    ready_improvement_pct = comparison.get(
        "median_ready_improvement_pct",
        payload.get("improvement_pct"),
    )

    baseline_post_pull = baseline_label.get(
        "median_post_pull_ready_ms",
        payload.get("baseline_median_post_pull_ready_ms"),
    )
    optimized_post_pull = optimized_label.get(
        "median_post_pull_ready_ms",
        payload.get("optimized_median_post_pull_ready_ms"),
    )
    post_pull_improvement_pct = comparison.get(
        "median_post_pull_ready_improvement_pct",
        payload.get("post_pull_ready_improvement_pct"),
    )

    baseline_end_to_end = baseline_label.get(
        "median_end_to_end_ready_ms",
        payload.get("baseline_median_end_to_end_ready_ms", baseline_ready),
    )
    optimized_end_to_end = optimized_label.get(
        "median_end_to_end_ready_ms",
        payload.get("optimized_median_end_to_end_ready_ms", optimized_ready),
    )
    end_to_end_improvement_pct = comparison.get(
        "median_end_to_end_improvement_pct",
        payload.get("end_to_end_improvement_pct", ready_improvement_pct),
    )
    baseline_pull_ms = baseline_label.get("median_pull_ms", payload.get("baseline_full_pull_ms"))
    baseline_full_pull_ready = baseline_label.get(
        "median_full_pull_ready_ms",
        payload.get("baseline_median_full_pull_ready_ms", baseline_end_to_end),
    )
    unoptimized_lazy_prepare = unoptimized_label.get(
        "median_lazy_prepare_ms",
        payload.get("unoptimized_lazy_prepare_ms"),
    )
    unoptimized_lazy_ready = unoptimized_label.get(
        "median_lazy_ready_ms",
        payload.get("unoptimized_median_lazy_ready_ms", payload.get("unoptimized_lazy_ready_ms")),
    )
    optimized_lazy_prepare = optimized_label.get(
        "median_lazy_prepare_ms",
        payload.get("optimized_lazy_prepare_ms"),
    )
    optimized_lazy_ready = optimized_label.get(
        "median_lazy_ready_ms",
        payload.get("optimized_median_lazy_ready_ms", payload.get("optimized_lazy_ready_ms")),
    )
    lazy_ready_improvement_pct = comparison.get(
        "median_lazy_ready_improvement_pct",
        payload.get("lazy_ready_improvement_pct"),
    )
    unoptimized_vs_raw_improvement_pct = comparison.get(
        "unoptimized_vs_raw_improvement_pct",
        payload.get("unoptimized_vs_raw_improvement_pct"),
    )
    optimized_vs_raw_improvement_pct = comparison.get(
        "optimized_vs_raw_improvement_pct",
        payload.get("optimized_vs_raw_improvement_pct"),
    )
    optimized_vs_unoptimized_improvement_pct = comparison.get(
        "optimized_vs_unoptimized_improvement_pct",
        payload.get("optimized_vs_unoptimized_improvement_pct"),
    )

    has_post_pull = (
        baseline_post_pull is not None
        and optimized_post_pull is not None
        and post_pull_improvement_pct is not None
    )
    has_triple_lazy_ready = (
        payload.get("comparison_mode") == "raw-full-vs-estargz-lazy-triple"
        and str(status or "") == "passed"
        and baseline_full_pull_ready is not None
        and unoptimized_lazy_ready is not None
        and optimized_lazy_ready is not None
    )
    has_lazy_ready = (
        payload.get("comparison_mode") == "raw-full-vs-estargz-lazy"
        and str(status or "") == "passed"
        and baseline_full_pull_ready is not None
        and optimized_lazy_ready is not None
        and lazy_ready_improvement_pct is not None
    )
    return {
        "status": status,
        "comparison_mode": payload.get("comparison_mode"),
        "ready_ms_semantics": payload.get("ready_ms_semantics"),
        "warnings": list(payload.get("warnings") or []),
        "invalid_reasons": list(payload.get("invalid_reasons") or []),
        "has_lazy_ready": has_lazy_ready,
        "has_post_pull": has_post_pull,
        "baseline_ready": baseline_ready,
        "optimized_ready": optimized_ready,
        "ready_improvement_pct": ready_improvement_pct,
        "baseline_pull_ms": baseline_pull_ms,
        "baseline_full_pull_ready": baseline_full_pull_ready,
        "unoptimized_lazy_prepare": unoptimized_lazy_prepare,
        "unoptimized_lazy_ready": unoptimized_lazy_ready,
        "optimized_lazy_prepare": optimized_lazy_prepare,
        "optimized_lazy_ready": optimized_lazy_ready,
        "lazy_ready_improvement_pct": lazy_ready_improvement_pct,
        "unoptimized_vs_raw_improvement_pct": unoptimized_vs_raw_improvement_pct,
        "optimized_vs_raw_improvement_pct": optimized_vs_raw_improvement_pct,
        "optimized_vs_unoptimized_improvement_pct": optimized_vs_unoptimized_improvement_pct,
        "baseline_post_pull": baseline_post_pull,
        "optimized_post_pull": optimized_post_pull,
        "post_pull_improvement_pct": post_pull_improvement_pct,
        "baseline_end_to_end": baseline_end_to_end,
        "optimized_end_to_end": optimized_end_to_end,
        "end_to_end_improvement_pct": end_to_end_improvement_pct,
        "has_triple_lazy_ready": has_triple_lazy_ready,
    }


def _benchmark_summary_csv_path(summary_payload: dict) -> Path | None:
    csv_value = summary_payload.get("csv")
    if csv_value:
        return Path(str(csv_value))
    benchmark_payload = summary_payload.get("benchmark")
    if isinstance(benchmark_payload, dict):
        nested_csv = benchmark_payload.get("csv")
        if nested_csv:
            return Path(str(nested_csv))
    return None


def _benchmark_row_ready_ms(row: dict[str, str] | None) -> int | None:
    if row is None:
        return None
    try:
        ready_ms = int(str(row.get("ready_ms") or "").strip())
    except Exception:
        return None
    return ready_ms if ready_ms >= 0 else None


def _benchmark_row_post_pull_ready_ms(row: dict[str, str] | None) -> int | None:
    if row is None:
        return None
    try:
        run_ms = int(str(row.get("run_ms") or "").strip())
        ready_wait_ms = int(str(row.get("ready_wait_ms") or "").strip())
    except Exception:
        return None
    if run_ms < 0 or ready_wait_ms < 0:
        return None
    return run_ms + ready_wait_ms


def _benchmark_row_end_to_end_ready_ms(row: dict[str, str] | None) -> int | None:
    if row is None:
        return None
    try:
        end_to_end_ready_ms = int(str(row.get("end_to_end_ready_ms") or "").strip())
    except Exception:
        return _benchmark_row_ready_ms(row)
    return end_to_end_ready_ms if end_to_end_ready_ms >= 0 else _benchmark_row_ready_ms(row)


def _benchmark_row_lazy_prepare_ms(row: dict[str, str] | None) -> int | None:
    if row is None:
        return None
    try:
        lazy_prepare_ms = int(str(row.get("lazy_prepare_ms") or "").strip())
    except Exception:
        return None
    return lazy_prepare_ms if lazy_prepare_ms >= 0 else None


def _benchmark_row_lazy_ready_ms(row: dict[str, str] | None) -> int | None:
    if row is None:
        return None
    try:
        lazy_ready_ms = int(str(row.get("lazy_ready_ms") or "").strip())
    except Exception:
        ready_semantics = str(row.get("ready_ms_semantics") or "").strip()
        if ready_semantics == "lazy_ready_ms":
            return _benchmark_row_ready_ms(row)
        return None
    return lazy_ready_ms if lazy_ready_ms >= 0 else None


def _append_benchmark_row_fields(
    parts: list[str],
    *,
    label: str,
    row: dict[str, str] | None,
    primary_metric: str,
) -> tuple[int | None, int | None]:
    if row is None:
        parts.append(f"{label}=missing")
        return None, None
    primary_value = None
    if primary_metric == "post_pull_ready_ms":
        primary_value = _benchmark_row_post_pull_ready_ms(row)
        if primary_value is not None:
            parts.append(f"{label}_post_pull_ready_ms={primary_value}")
    elif primary_metric == "lazy_ready_ms":
        lazy_metric_label = label[:-5] if label.endswith("_lazy") else label
        if label == "baseline":
            primary_value = _benchmark_row_end_to_end_ready_ms(row)
            if primary_value is not None:
                parts.append(f"{label}_full_pull_ready_ms={primary_value}")
            else:
                parts.append(f"{label}_full_pull_ready_ms=unavailable")
        else:
            primary_value = _benchmark_row_lazy_ready_ms(row)
            if primary_value is not None:
                parts.append(f"{lazy_metric_label}_lazy_ready_ms={primary_value}")
            else:
                parts.append(f"{lazy_metric_label}_lazy_ready_ms=unavailable")
            lazy_prepare_ms = _benchmark_row_lazy_prepare_ms(row)
            if lazy_prepare_ms is not None:
                parts.append(f"{lazy_metric_label}_lazy_prepare_ms={lazy_prepare_ms}")
    ready_ms = _benchmark_row_ready_ms(row)
    if primary_metric not in {"post_pull_ready_ms", "lazy_ready_ms"} or primary_value is None:
        primary_value = ready_ms
    if primary_metric not in {"post_pull_ready_ms", "lazy_ready_ms"}:
        if ready_ms is not None:
            parts.append(f"{label}_ready_ms={ready_ms}")
        else:
            parts.append(f"{label}_ready_ms=unavailable")
    elif primary_value is None:
        if primary_metric == "post_pull_ready_ms":
            parts.append(f"{label}_post_pull_ready_ms=unavailable")
    if primary_metric == "post_pull_ready_ms" and ready_ms is not None:
        parts.append(f"{label}_ready_ms={ready_ms}")
    elif primary_metric == "post_pull_ready_ms":
        parts.append(f"{label}_ready_ms=unavailable")
    status = str(row.get("status") or "").strip()
    if status and status != "ok":
        parts.append(f"{label}_status={status}")
    err = str(row.get("err") or "").strip()
    if err:
        parts.append(f"{label}_err={err}")
    return primary_value, ready_ms


def _benchmark_run_comparison_lines(summary_payload: dict, *, primary_metric: str = "ready_ms") -> list[str]:
    csv_path = _benchmark_summary_csv_path(summary_payload)
    if csv_path is None or not csv_path.is_file():
        return []
    try:
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except Exception:
        return []

    label_order = _benchmark_summary_label_order(summary_payload.get("comparison_mode"))
    rows_by_label = {
        label: [row for row in rows if str(row.get("label") or "").strip() == label]
        for label in label_order
    }
    run_count = max((len(label_rows) for label_rows in rows_by_label.values()), default=0)
    if run_count == 0:
        return []

    lines: list[str] = []
    for index in range(run_count):
        parts = [f"run{index + 1}"]
        primary_values: dict[str, int | None] = {}
        for label in label_order:
            row = rows_by_label[label][index] if index < len(rows_by_label[label]) else None
            primary_value, _ready_value = _append_benchmark_row_fields(
                parts,
                label=label,
                row=row,
                primary_metric=primary_metric,
            )
            primary_values[label] = primary_value
        baseline_primary = primary_values.get("baseline")
        unoptimized_primary = primary_values.get("unoptimized_lazy")
        optimized_primary = primary_values.get("optimized")
        if len(label_order) == 2:
            if baseline_primary is not None and optimized_primary is not None:
                parts.append(f"delta_ms={optimized_primary - baseline_primary:+d}")
        else:
            if baseline_primary is not None and unoptimized_primary is not None:
                parts.append(f"unoptimized_vs_baseline_delta_ms={unoptimized_primary - baseline_primary:+d}")
            if baseline_primary is not None and optimized_primary is not None:
                parts.append(f"optimized_vs_baseline_delta_ms={optimized_primary - baseline_primary:+d}")
            if unoptimized_primary is not None and optimized_primary is not None:
                parts.append(f"optimized_vs_unoptimized_delta_ms={optimized_primary - unoptimized_primary:+d}")
        lines.append(" ".join(parts))
    return lines


def _print_benchmark_summary(summary_payload: dict) -> None:
    metrics = _benchmark_summary_metrics(summary_payload)
    line = f"[orchestrator] benchmark-summary status={metrics['status'] or 'unknown'}"
    primary_metric = "ready_ms"
    if metrics["has_triple_lazy_ready"]:
        primary_metric = "lazy_ready_ms"
        line += (
            f" baseline_median_full_pull_ready_ms={metrics['baseline_full_pull_ready']} "
            f"unoptimized_median_lazy_ready_ms={metrics['unoptimized_lazy_ready']} "
            f"optimized_median_lazy_ready_ms={metrics['optimized_lazy_ready']}"
        )
    elif metrics["has_lazy_ready"]:
        primary_metric = "lazy_ready_ms"
        line += (
            f" baseline_median_full_pull_ready_ms={metrics['baseline_full_pull_ready']} "
            f"optimized_median_lazy_ready_ms={metrics['optimized_lazy_ready']}"
        )
    elif metrics["has_post_pull"]:
        primary_metric = "post_pull_ready_ms"
        line += (
            f" baseline_median_post_pull_ready_ms={metrics['baseline_post_pull']} "
            f"optimized_median_post_pull_ready_ms={metrics['optimized_post_pull']}"
        )
    elif metrics["baseline_ready"] is not None and metrics["optimized_ready"] is not None:
        line += (
            f" baseline_median_ready_ms={metrics['baseline_ready']} "
            f"optimized_median_ready_ms={metrics['optimized_ready']}"
        )
    print(line)
    if (
        metrics["comparison_mode"] == "raw-full-vs-estargz-lazy"
        and str(metrics["status"] or "") != "passed"
        and (
            "optimized lazy path appears to have performed a full pull; result is invalid" in metrics["invalid_reasons"]
            or "optimized path appears to have performed a full pull; lazy-pull result may be invalid" in metrics["warnings"]
        )
    ):
        print("  - optimized lazy path appears to have performed a full pull; result is invalid")
    if metrics["comparison_mode"] == "raw-full-vs-estargz-lazy-triple":
        for label in ("unoptimized_lazy", "optimized"):
            if (
                _benchmark_lazy_full_pull_invalid_reason(label) in metrics["invalid_reasons"]
                or _benchmark_lazy_full_pull_warning(label) in metrics["warnings"]
            ):
                print(f"  - {_benchmark_lazy_full_pull_invalid_reason(label)}")
    if metrics["has_triple_lazy_ready"]:
        if metrics["unoptimized_vs_raw_improvement_pct"] is not None:
            print(f"  - unoptimized_vs_raw_improvement_pct={float(metrics['unoptimized_vs_raw_improvement_pct']):.2f}%")
        if metrics["optimized_vs_raw_improvement_pct"] is not None:
            print(f"  - optimized_vs_raw_improvement_pct={float(metrics['optimized_vs_raw_improvement_pct']):.2f}%")
        if metrics["optimized_vs_unoptimized_improvement_pct"] is not None:
            print(
                "  - optimized_vs_unoptimized_improvement_pct="
                f"{float(metrics['optimized_vs_unoptimized_improvement_pct']):.2f}%"
            )
        print("  - metric=baseline_full_pull_ready_ms_vs_unoptimized_lazy_ready_ms_vs_optimized_lazy_ready_ms")
        if metrics["baseline_pull_ms"] is not None:
            print(f"  - baseline median pull_ms={metrics['baseline_pull_ms']}")
        if metrics["unoptimized_lazy_prepare"] is not None:
            print(f"  - unoptimized_lazy median lazy_prepare_ms={metrics['unoptimized_lazy_prepare']}")
        if metrics["optimized_lazy_prepare"] is not None:
            print(f"  - optimized median lazy_prepare_ms={metrics['optimized_lazy_prepare']}")
        if metrics["baseline_post_pull"] is not None:
            print(f"  - baseline median post_pull_ready_ms={metrics['baseline_post_pull']}")
        if metrics["unoptimized_lazy_ready"] is not None:
            print(
                "  - unoptimized_lazy median post_pull_ready_ms/lazy_ready_ms="
                f"{metrics['unoptimized_lazy_ready']}"
            )
        if metrics["optimized_post_pull"] is not None or metrics["optimized_lazy_ready"] is not None:
            value = metrics["optimized_post_pull"] if metrics["optimized_post_pull"] is not None else metrics["optimized_lazy_ready"]
            print(f"  - optimized median post_pull_ready_ms/lazy_ready_ms={value}")
    elif metrics["has_lazy_ready"]:
        print(f"  - lazy_ready_improvement_pct={float(metrics['lazy_ready_improvement_pct']):.2f}%")
        print("  - metric=baseline_full_pull_ready_ms_vs_optimized_lazy_ready_ms")
        if metrics["baseline_pull_ms"] is not None:
            print(f"  - baseline median pull_ms={metrics['baseline_pull_ms']}")
        if metrics["optimized_lazy_prepare"] is not None:
            print(f"  - optimized median lazy_prepare_ms={metrics['optimized_lazy_prepare']}")
        if metrics["baseline_post_pull"] is not None:
            print(f"  - baseline median post_pull_ready_ms={metrics['baseline_post_pull']}")
        if metrics["optimized_post_pull"] is not None or metrics["optimized_lazy_ready"] is not None:
            value = metrics["optimized_post_pull"] if metrics["optimized_post_pull"] is not None else metrics["optimized_lazy_ready"]
            print(f"  - optimized median post_pull_ready_ms/lazy_ready_ms={value}")
    elif metrics["has_post_pull"]:
        print(f"  - post_pull_ready_improvement_pct={float(metrics['post_pull_improvement_pct']):.2f}%")
        if metrics["comparison_mode"] == "raw-vs-estargz":
            print(
                f"  - metric=post_pull_ready_ms ready_ms_semantics={metrics['ready_ms_semantics'] or 'unknown'}"
            )
        if metrics["baseline_end_to_end"] is not None and metrics["optimized_end_to_end"] is not None:
            line = (
                "  - end_to_end_ready_ms: "
                f"baseline_median={metrics['baseline_end_to_end']} "
                f"optimized_median={metrics['optimized_end_to_end']}"
            )
            if metrics["end_to_end_improvement_pct"] is not None:
                line += f" improvement_pct={float(metrics['end_to_end_improvement_pct']):.2f}%"
            print(line)
    elif metrics["ready_improvement_pct"] is not None:
        print(f"  - improvement_pct={float(metrics['ready_improvement_pct']):.2f}%")
    elif metrics["baseline_ready"] is None or metrics["optimized_ready"] is None:
        print("  - benchmark_ready_time=unavailable")
    for item in _benchmark_run_comparison_lines(summary_payload, primary_metric=primary_metric):
        print(f"  - {item}")


def _print_run_result_summary(summary_payload: dict, *, manifest_path: Path) -> None:
    llm_payload = dict(summary_payload.get("llm") or {})
    apply_payload = dict(summary_payload.get("apply") or {})
    benchmark_payload = dict(summary_payload.get("benchmark") or {})
    mode = str(summary_payload.get("mode") or "").strip()
    llm_applied = bool(llm_payload.get("llm_applied", False))
    final_hints_source = str(llm_payload.get("final_hints_source") or "").strip()
    if not final_hints_source and mode == "deterministic":
        final_hints_source = "deterministic_ranked"
    optimized_image = str(
        apply_payload.get("optimized_image")
        or benchmark_payload.get("opt_image")
        or ""
    ).strip()
    print(
        f"[orchestrator] final-summary run_status={summary_payload.get('overall_status') or 'unknown'} "
        f"mode={mode or 'unknown'} mode_result={summary_payload.get('mode_result') or 'unknown'}"
    )
    print(f"  - llm_applied={llm_applied}")
    print(f"  - hints_source={final_hints_source or 'unknown'}")
    if optimized_image:
        print(f"  - optimized_image={optimized_image}")
    _benchmark_status, baseline_median, optimized_median, improvement_pct = _benchmark_summary_fields(
        benchmark_payload
    )
    if benchmark_payload and baseline_median is not None and optimized_median is not None:
        if benchmark_payload.get("comparison_mode") == "raw-full-vs-estargz-lazy-triple":
            unoptimized_median = benchmark_payload.get("unoptimized_median_lazy_ready_ms")
            parts = [f"  - baseline_median_full_pull_ready_ms={baseline_median}"]
            if unoptimized_median is not None:
                parts.append(f"unoptimized_median_lazy_ready_ms={unoptimized_median}")
            parts.append(f"optimized_median_lazy_ready_ms={optimized_median}")
            print(" ".join(parts))
            unoptimized_vs_raw = benchmark_payload.get("unoptimized_vs_raw_improvement_pct")
            if unoptimized_vs_raw is not None:
                print(f"  - unoptimized_vs_raw_improvement_pct={float(unoptimized_vs_raw):.2f}%")
            if improvement_pct is not None:
                print(f"  - optimized_vs_raw_improvement_pct={float(improvement_pct):.2f}%")
            optimized_vs_unoptimized = benchmark_payload.get("optimized_vs_unoptimized_improvement_pct")
            if optimized_vs_unoptimized is not None:
                print(f"  - optimized_vs_unoptimized_improvement_pct={float(optimized_vs_unoptimized):.2f}%")
        else:
            print(
                f"  - baseline_median_ready_ms={baseline_median} "
                f"optimized_median_ready_ms={optimized_median}"
            )
            if improvement_pct is not None:
                print(f"  - benchmark_improvement_pct={float(improvement_pct):.2f}%")
    print(f"  - manifest={manifest_path}")


def _print_workload_validation_failure(*, stage: str, work_dir: Path, errors: list[str]) -> int:
    print(f"[orchestrator] {stage}=fail work_dir={work_dir}", file=sys.stderr)
    for item in errors:
        print(f"  - {item}", file=sys.stderr)
    print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
    return 1


def _sample_stage_artifacts_from_work_dir(work_dir: Path) -> dict[str, str]:
    candidates = {
        "runtime_state": work_dir / "runtime_state.json",
        "raw_events": work_dir / "raw_events.jsonl",
        "sampler_log": work_dir / "sampler.log",
        "sampler_metrics": work_dir / "sampler_metrics.prom",
    }
    return {
        name: str(path)
        for name, path in candidates.items()
        if path.exists()
    }


def _add_llm_optimize_args(parser: argparse.ArgumentParser, *, legacy_compat: bool = False) -> None:
    parser.add_argument(
        "--llm-provider",
        choices=FORMAL_LLM_PROVIDERS,
        help=(
            "LLM provider adapter/default set; canonical provider types are openai_responses, openai_compatible, "
            "and mock, while legacy aliases (openai, groq, openrouter, cerebras, custom) still map to provider defaults"
        ),
    )
    parser.add_argument(
        "--llm-api-key-env",
        help="override the environment variable name used to read the LLM API key; custom provider requires this",
    )
    parser.add_argument(
        "--llm-api-base",
        help="override the OpenAI-compatible chat completions base URL; custom provider requires this",
    )
    parser.add_argument(
        "--llm-model",
        help="override the LLM model id; explicit value wins over config and provider defaults",
    )
    parser.add_argument(
        "--llm-reasoning-effort",
        help="optional provider-level reasoning effort hint; passed through only when the selected provider supports it",
    )
    parser.add_argument(
        "--llm-timeout-sec",
        type=int,
        help="provider request timeout in seconds; CLI explicit value wins over config and provider defaults",
    )
    parser.add_argument("--llm-topn", type=int, default=400, help="top-N candidate paths embedded into the prompt")
    parser.add_argument(
        "--manual-web",
        action="store_true",
        help=(
            "legacy compatibility alias for --mode llm-manual; export a web-LLM bundle instead of attempting the API"
            if legacy_compat
            else (
                "force manual web-LLM export instead of attempting the API; "
                "response should come back as strict JSON with P0/P1/P2/P3 absolute file paths in absolute-path arrays "
                "(legacy YAML is still accepted)"
            )
        ),
    )
    parser.add_argument(
        "--manual-response-file",
        help=(
            "legacy compatibility input for --mode llm-manual; import a strict JSON response produced from prompt.txt "
            "using P0/P1/P2/P3 absolute-path lists/arrays (legacy YAML still accepted)"
            if legacy_compat
            else (
                "import a strict JSON response produced from prompt.txt in a web LLM instead of calling the API; "
                "must contain only P0/P1/P2/P3 absolute file paths in absolute-path lists/arrays (legacy YAML still accepted)"
            )
        ),
    )


def _add_selector_mode_arg(parser: argparse.ArgumentParser, *, default: str | None = "file") -> None:
    parser.add_argument(
        "--selector-mode",
        choices=("file", "chunk-shadow"),
        default=default,
        help=(
            "deterministic selector mode; chunk-shadow emits experimental parallel artifacts without changing default hints"
            if default is not None
            else "selector mode; when omitted, inherit manifest analyze selector mode if present"
        ),
    )


def _add_startup_backend_preference_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--startup-backend-preference",
        choices=("auto", "prefer-host-launcher", "prefer-image-shell"),
        default="auto",
        help=(
            "backend preference for gated startup; auto may prefer the static host launcher when available, "
            "prefer-host-launcher forces host launcher first, and prefer-image-shell forces image-shell gating first"
        ),
    )


def _llm_stage_summary(result) -> str:
    if result.llm_applied:
        if result.mode == "api":
            return "LLM API prioritized hints were grounded and applied"
        if result.mode == "manual_response":
            return "Manual web-LLM response was grounded and applied"
    if result.mode == "manual_export":
        return "Prompt package exported for web-LLM fallback; deterministic hints remain active"
    return "LLM optimization fell back to deterministic hints"


def _llm_stage_artifacts(result) -> dict[str, str]:
    return {
        "prompt": str(result.prompt_path),
        "web_llm_input": str(result.web_input_path),
        "llm_body": str(result.body_path),
        "llm_response": str(result.response_path),
        "hints_raw": str(result.hints_raw_path),
        "preferred_paths": str(result.preferred_paths_path),
        "llm_grounding_audit": str(result.grounding_audit_path),
        "llm_manual_instructions": str(result.manual_instructions_path),
        "llm_manual_import_script": str(result.import_script_path),
        "llm_result": str(result.result_path),
        "hints": str(result.hints_path),
        "estargz_list": str(result.estargz_list_path),
        "selector_stats_hints": str(result.selector_stats_hint_path),
        "llm_optimize_log": str(result.stage_log_path),
    }


def _add_container_runtime_args(
    parser: argparse.ArgumentParser,
    *,
    flag_prefix: str = "",
    attr_prefix: str = "",
    help_prefix: str = "container",
) -> None:
    parser.add_argument(
        f"--{flag_prefix}container-gpus",
        dest=f"{attr_prefix}container_gpus",
        help=f"{help_prefix} GPU selector, e.g. 'all'",
    )
    parser.add_argument(
        f"--{flag_prefix}container-ipc",
        dest=f"{attr_prefix}container_ipc",
        help=f"{help_prefix} IPC mode, e.g. host",
    )
    parser.add_argument(
        f"--{flag_prefix}container-env",
        dest=f"{attr_prefix}container_env",
        action="append",
        default=None,
        help=f"repeatable {help_prefix} environment entry (KEY=VALUE or KEY)",
    )
    parser.add_argument(
        f"--{flag_prefix}container-mount",
        dest=f"{attr_prefix}container_mount",
        action="append",
        default=None,
        help=f"repeatable raw nerdctl --mount spec for {help_prefix}",
    )
    parser.add_argument(
        f"--{flag_prefix}container-entrypoint",
        dest=f"{attr_prefix}container_entrypoint",
        help=f"{help_prefix} entrypoint override",
    )
    parser.add_argument(
        f"--{flag_prefix}container-cmd",
        dest=f"{attr_prefix}container_cmd",
        action="append",
        default=None,
        help=f"repeatable command token replacing image CMD for {help_prefix}",
    )


def _container_runtime_from_args(args: argparse.Namespace, *, attr_prefix: str = "") -> ContainerRuntimeSpec:
    return container_runtime_spec(
        gpus=getattr(args, f"{attr_prefix}container_gpus", None),
        ipc=getattr(args, f"{attr_prefix}container_ipc", None),
        entrypoint=getattr(args, f"{attr_prefix}container_entrypoint", None),
        env=getattr(args, f"{attr_prefix}container_env", None),
        mounts=getattr(args, f"{attr_prefix}container_mount", None),
        command=getattr(args, f"{attr_prefix}container_cmd", None),
    )


def _container_runtime_from_payload(payload: object) -> ContainerRuntimeSpec:
    if not isinstance(payload, dict):
        return ContainerRuntimeSpec()
    return container_runtime_spec(
        gpus=payload.get("gpus"),
        ipc=payload.get("ipc"),
        entrypoint=payload.get("entrypoint"),
        env=payload.get("env"),
        mounts=payload.get("mounts"),
        command=payload.get("command"),
    )


def _container_runtime_cli_provided(args: argparse.Namespace, *, attr_prefix: str = "") -> bool:
    return any(
        getattr(args, f"{attr_prefix}{field}", None) is not None
        for field in (
            "container_gpus",
            "container_ipc",
            "container_entrypoint",
            "container_env",
            "container_mount",
            "container_cmd",
        )
    )


def _merge_model_mount_into_runtime(
    runtime_spec: ContainerRuntimeSpec,
    *,
    model_host_dir: str | None,
    model_mount_hook: object | None,
) -> ContainerRuntimeSpec:
    if not model_host_dir or model_mount_hook is None:
        return runtime_spec
    dst_dir = str(getattr(model_mount_hook, "dst_dir", "") or "").strip()
    if not dst_dir:
        return runtime_spec
    host_path = Path(model_host_dir).expanduser().resolve()
    if not host_path.is_dir():
        raise RuntimeError(f"model host directory does not exist: {host_path}")
    mount_spec = f"type=bind,src={host_path},dst={dst_dir},readonly"
    mounts = list(runtime_spec.mounts)
    if mount_spec not in mounts:
        mounts.append(mount_spec)
    env = list(runtime_spec.env)
    env_keys = {item.split("=", 1)[0] for item in env if item}
    model_name = str(getattr(model_mount_hook, "model_name", "") or "").strip()
    # Keep startup semantics aligned with workload-defined model mount/name for
    # images such as TensorFlow Serving that derive argv from MODEL_* env vars.
    if model_name and "MODEL_NAME" not in env_keys:
        env.append(f"MODEL_NAME={model_name}")
    model_base_path = str(getattr(model_mount_hook, "model_base_path_env", "") or "").strip() or dst_dir
    if model_base_path and "MODEL_BASE_PATH" not in env_keys:
        env.append(f"MODEL_BASE_PATH={model_base_path}")
    return inherit_container_runtime(runtime_spec, mounts=tuple(mounts), env=tuple(env))


def _resolve_container_runtime(
    *,
    args: argparse.Namespace,
    manifest: ManifestStore,
    option_key: str,
    attr_prefix: str = "",
    inherit: ContainerRuntimeSpec | None = None,
) -> ContainerRuntimeSpec:
    base = inherit or ContainerRuntimeSpec()
    if _container_runtime_cli_provided(args, attr_prefix=attr_prefix):
        cli_runtime = _container_runtime_from_args(args, attr_prefix=attr_prefix)
        return inherit_container_runtime(
            base,
            gpus=cli_runtime.gpus,
            ipc=cli_runtime.ipc,
            entrypoint=cli_runtime.entrypoint,
            env=cli_runtime.env,
            mounts=cli_runtime.mounts,
            command=cli_runtime.command,
        )
    manifest_runtime = _container_runtime_from_payload(
        manifest.data.get("options", {}).get(option_key)
    )
    if inherit is not None and manifest_runtime == ContainerRuntimeSpec():
        return base
    return inherit_container_runtime(
        base,
        gpus=manifest_runtime.gpus,
        ipc=manifest_runtime.ipc,
        entrypoint=manifest_runtime.entrypoint,
        env=manifest_runtime.env,
        mounts=manifest_runtime.mounts,
        command=manifest_runtime.command,
    )


def _manual_response_default_path(work_dir: Path) -> Path:
    return work_dir / "hints_raw.manual.yaml"


def _llm_pause_resume_inputs(result) -> dict[str, dict[str, str]]:
    return {
        "manual_response_file": {
            "path": str(result.manual_response_default_path),
            "format": result.manual_response_format,
        }
    }


def _strip_option(argv: list[str], option_name: str) -> list[str]:
    cleaned: list[str] = []
    skip_next = False
    for value in argv:
        if skip_next:
            skip_next = False
            continue
        if value == option_name:
            skip_next = True
            continue
        if value.startswith(f"{option_name}="):
            continue
        cleaned.append(value)
    return cleaned


def _has_option(argv: list[str], option_name: str) -> bool:
    return any(value == option_name or value.startswith(f"{option_name}=") for value in argv)


def _write_manual_web_resume_script(
    *,
    repo_root: Path,
    work_dir: Path,
    argv: list[str],
) -> Path:
    script_path = work_dir / "resume_manual_web_llm.sh"
    response_path = _manual_response_default_path(work_dir)
    rerun_args = _strip_option(list(argv), "--manual-response-file")
    if not _has_option(rerun_args, "--work-dir"):
        rerun_args.extend(["--work-dir", str(work_dir)])
    command = shlex.join(
        [
            "python3",
            str(repo_root / "scripts" / "oneclick_product.py"),
            *rerun_args,
        ]
    ) + ' --manual-response-file "$MANUAL_RESPONSE_FILE"'
    content = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n\n"
        f"REPO_ROOT={str(repo_root)!r}\n"
        f"WORK_DIR={str(work_dir)!r}\n"
        f"DEFAULT_RESPONSE={str(response_path)!r}\n"
        'MANUAL_RESPONSE_FILE="${1:-$DEFAULT_RESPONSE}"\n\n'
        'if [[ ! -f "$MANUAL_RESPONSE_FILE" ]]; then\n'
        '  echo "[orchestrator] manual-response-file is missing: $MANUAL_RESPONSE_FILE" >&2\n'
        f'  echo "[orchestrator] expected {MANUAL_RESPONSE_CONTRACT}" >&2\n'
        "  exit 1\n"
        "fi\n\n"
        'cd "$REPO_ROOT"\n'
        f"sudo -E {command}\n"
    )
    script_path.write_text(content, encoding="utf-8")
    script_path.chmod(0o755)
    return script_path


def _cleanup_sample_container_before_benchmark(*, repo_root: Path, work_dir: Path) -> None:
    runtime_state_path = work_dir / "runtime_state.json"
    if not runtime_state_path.exists():
        return
    try:
        runtime_state = json.loads(runtime_state_path.read_text(encoding="utf-8"))
    except Exception:
        return
    container_name = str(runtime_state.get("container_name") or "").strip()
    if not container_name:
        return
    inspect_proc = nerdctl(
        repo_root,
        ["inspect", "-f", "{{.State.Running}}", container_name],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if inspect_proc.returncode != 0:
        return
    nerdctl(
        repo_root,
        ["stop", container_name],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    nerdctl(
        repo_root,
        ["rm", container_name],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Formal Python control plane for cpp-sampler; legacy shell paths are retained only as helpers/reference."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    preflight = sub.add_parser(
        "preflight",
        help="Create a staged manifest and run strict product-policy preflight.",
    )
    preflight.add_argument("image", help="target image reference")
    preflight.add_argument(
        "--work-dir",
        help="existing or new work directory; default is optimizer/work/<ts>",
    )
    preflight.add_argument(
        "--startup-mode",
        choices=("auto", "legacy", "gated"),
        default="auto",
        help="startup mode requested by the future orchestrator run",
    )
    _add_startup_backend_preference_arg(preflight)
    preflight.add_argument(
        "--llm",
        action="store_true",
        help="explicitly enable LLM; default product policy keeps it disabled",
    )
    preflight.add_argument(
        "--no-runtime-probes",
        action="store_true",
        help="pass through to scripts/check_env.sh",
    )

    sample = sub.add_parser(
        "sample",
        help="Run preflight if needed, then launch the container and capture raw_events.jsonl via file sink.",
    )
    sample.add_argument("image", help="target image reference")
    sample.add_argument("--work-dir", help="existing or new work directory")
    sample.add_argument(
        "--startup-mode",
        choices=("auto", "legacy", "gated"),
        default="auto",
    )
    _add_startup_backend_preference_arg(sample)
    sample.add_argument("--duration", type=int, default=90, help="sampling window in seconds")
    sample.add_argument("--prom-port", type=int, default=8000, help="preferred Prometheus port start")
    sample.add_argument("--mmap-window", type=int, default=120, help="mmap capture window seconds")
    sample.add_argument("--cg-refresh", type=int, default=2, help="cgroup whitelist refresh seconds")
    sample.add_argument("--workload", choices=workload_choices(), default="none", help="workload profile for readiness defaults")
    sample.add_argument("--inject-io", action="store_true", help="enable workload-specific I/O stimulation after readiness")
    sample.add_argument("--ready-url", help="optional workload readiness probe URL")
    sample.add_argument("--ready-exec", help="optional in-container readiness command; succeeds on exit code 0")
    sample.add_argument("--ready-log-regex", help="optional container log regex treated as readiness signal")
    sample.add_argument("--ready-shell", default="sh", help="shell for --ready-exec (default: sh)")
    sample.add_argument("--predict-url", help="optional workload predict URL for injection")
    sample.add_argument("--predict-body", help="optional JSON body for workload predict injection")
    sample.add_argument("--inject-cmd", help="optional shell command to execute inside the container for I/O stimulation")
    sample.add_argument("--model-host", help="optional host model directory to mount for sample workload")
    sample.add_argument("--model-dst", help="optional in-container model destination directory override")
    sample.add_argument("--model-name", help="optional model name override for sample workload")
    sample.add_argument("--ready-timeout", type=int, default=None, help="readiness timeout in seconds; if omitted, inherits the workload default")
    sample.add_argument("--llm", action="store_true", help="explicitly enable LLM in manifest options")
    sample.add_argument("--no-runtime-probes", action="store_true")
    _add_container_runtime_args(sample)

    export_enrich = sub.add_parser(
        "export-enrich",
        help="Consume raw_events.jsonl and runtime_state.json to produce events.jsonl and events_enriched.jsonl.",
    )
    export_enrich.add_argument(
        "--work-dir",
        required=True,
        help="existing work directory created by preflight/sample",
    )

    analyze = sub.add_parser(
        "analyze",
        help="Run aggregate + quality gate + deterministic selector to produce hints.yaml and estargz.list.",
    )
    analyze.add_argument("--work-dir", required=True, help="existing work directory created by export-enrich")
    analyze.add_argument("--min-events", type=int, default=30)
    analyze.add_argument("--max-mmap-maps-ratio", type=float, default=0.50)
    analyze.add_argument("--size-budget-percent", type=float, default=6.4)
    analyze.add_argument("--size-budget-mb", type=int, default=0)
    analyze.add_argument("--min-hits", type=int, default=2)
    analyze.add_argument("--p0-max", type=int, default=24)
    analyze.add_argument("--early-onehit-ratio", type=float, default=0.12)
    analyze.add_argument("--early-onehit-max", type=int, default=8)
    analyze.add_argument("--early-onehit-max-size-kb", type=int, default=512)
    _add_selector_mode_arg(analyze)
    analyze.add_argument(
        "--allow-low-fidelity",
        action="store_true",
        help="legacy compatibility flag; low-fidelity quality findings are advisory by default",
    )

    llm_optimize = sub.add_parser(
        "llm-optimize",
        help=(
            "Run the formal LLM optimization stage on top of analyze artifacts; deterministic output remains the baseline."
        ),
    )
    llm_optimize.add_argument("--work-dir", required=True, help="existing work directory created by analyze")
    llm_optimize.add_argument("--size-budget-percent", type=float, default=6.4)
    llm_optimize.add_argument("--size-budget-mb", type=int, default=0)
    llm_optimize.add_argument("--min-hits", type=int, default=2)
    llm_optimize.add_argument("--p0-max", type=int, default=24)
    llm_optimize.add_argument("--early-onehit-ratio", type=float, default=0.12)
    llm_optimize.add_argument("--early-onehit-max", type=int, default=8)
    llm_optimize.add_argument("--early-onehit-max-size-kb", type=int, default=512)
    _add_llm_optimize_args(llm_optimize)

    apply = sub.add_parser(
        "apply",
        help="Run the formal apply stage from an existing work dir; helper scripts may be used internally.",
    )
    apply.add_argument("--work-dir", required=True, help="existing work directory containing hints.yaml or estargz.list")
    apply.add_argument("--src-image", help="override source image; default uses manifest image")
    apply.add_argument("--dst-image", help="destination optimized image reference")
    apply.add_argument("--namespace", default="default", help="containerd namespace for image import/optimize")
    apply.add_argument("--dry-run", action="store_true", help="validate and log apply command without importing image")
    apply.add_argument("--no-runtime-probes", action="store_true")
    _add_selector_mode_arg(apply, default=None)

    publish = sub.add_parser(
        "publish",
        help="Tag and push a local image to a registry reference for registry-based remote experiments.",
    )
    publish.add_argument("--work-dir", required=True, help="existing work directory for publish artifacts")
    publish.add_argument("--src-image", help="override source image; default uses manifest image")
    publish.add_argument(
        "--optimized",
        action="store_true",
        help="publish the optimized image artifact from apply output instead of the original manifest image",
    )
    publish.add_argument("--dst-image", required=True, help="registry destination image reference")
    publish.add_argument("--namespace", default="default", help="containerd namespace for tag/push")
    publish.add_argument(
        "--plain-http",
        dest="plain_http",
        action="store_true",
        default=None,
        help="force insecure-registry mode for local/plain-HTTP registry pushes",
    )
    publish.add_argument(
        "--https",
        dest="plain_http",
        action="store_false",
        help="force standard HTTPS registry push behavior",
    )
    _add_selector_mode_arg(publish, default=None)

    benchmark = sub.add_parser(
        "benchmark",
        help="Run the formal benchmark stage from an existing work dir; helper scripts may be used internally.",
    )
    benchmark.add_argument("--work-dir", required=True, help="existing work directory containing baseline and optimized image metadata")
    benchmark.add_argument("--base-image", help="override baseline image; default uses manifest image")
    benchmark.add_argument(
        "--unopt-image",
        help="override unoptimized eStargz image; required only for --comparison-mode raw-full-vs-estargz-lazy-triple",
    )
    benchmark.add_argument("--opt-image", help="override optimized image; default uses optimized_image.txt")
    _add_selector_mode_arg(benchmark, default=None)
    benchmark.add_argument("--namespace", default="default")
    benchmark.add_argument("--runs", type=int, default=3)
    benchmark.add_argument("--timeout", type=int, default=None, help="readiness timeout in seconds; if omitted, inherits the workload default")
    benchmark.add_argument("--snapshotter", default="stargz")
    benchmark.add_argument("--base-snapshotter")
    benchmark.add_argument("--opt-snapshotter")
    benchmark.add_argument(
        "--comparison-mode",
        choices=("same-snapshotter", "raw-vs-estargz", "raw-full-vs-estargz-lazy", "raw-full-vs-estargz-lazy-triple"),
        default="same-snapshotter",
        help="benchmark image/snapshotter comparison contract; default preserves existing behavior",
    )
    benchmark.add_argument(
        "--baseline-stargz",
        action="store_true",
        help="explicitly run baseline on the benchmark snapshotter too; default behavior remains overlayfs-vs-stargz",
    )
    benchmark.add_argument(
        "--mode",
        choices=("struct", "ready"),
        default="struct",
        help="benchmark mode for this direct benchmark command; defaults to struct unless the workload recommends ready",
    )
    benchmark.add_argument(
        "--workload",
        choices=workload_choices(),
        default="none",
        help="workload profile for ready/predict/model defaults; direct benchmark uses this value directly",
    )
    benchmark.add_argument("--ready-url")
    benchmark.add_argument("--ready-exec", help="optional in-container readiness command; succeeds on exit code 0")
    benchmark.add_argument("--ready-log-regex", help="optional container log regex treated as readiness signal")
    benchmark.add_argument("--ready-shell", default="sh", help="shell for --ready-exec (default: sh)")
    benchmark.add_argument("--predict-url")
    benchmark.add_argument("--predict-body")
    benchmark.add_argument("--model-host")
    benchmark.add_argument("--model-dst")
    benchmark.add_argument("--model-name")
    benchmark.add_argument("--platform", default="linux/amd64")
    benchmark.add_argument("--hard-cold", dest="hard_cold", action="store_true", default=True)
    benchmark.add_argument("--soft-cold", dest="hard_cold", action="store_false")
    benchmark.add_argument("--force-reset", action="store_true")
    benchmark.add_argument("--force-kill-port", action="store_true")
    benchmark.add_argument("--allow-busy-port", action="store_true")
    benchmark.add_argument("--reconcile-dirs", action="store_true")
    benchmark.add_argument("--no-drop-cache", dest="drop_page_cache", action="store_false", default=True)
    benchmark.add_argument("--no-clear-stargz", dest="clear_stargz_cache", action="store_false", default=True)
    benchmark.add_argument("--shuffle", action="store_true")
    benchmark.add_argument("--log-dir")
    benchmark.add_argument("--pull-policy", choices=("never", "missing", "always"), default="never")
    benchmark.add_argument("--drop-local-image-ref", action="store_true")
    benchmark.add_argument(
        "--cold-mode",
        choices=("local-cold", "registry-cold-strict"),
        default="local-cold",
        help="benchmark cold-state contract (default: local-cold)",
    )
    benchmark.add_argument("--no-runtime-probes", action="store_true")
    _add_container_runtime_args(benchmark)

    report = sub.add_parser(
        "report",
        help="Generate a unified run report from manifest and stage artifacts.",
    )
    report.add_argument("--work-dir", required=True, help="existing work directory containing manifest.json")

    run = sub.add_parser(
        "run",
        help=(
            "Run the formal product chain from the Python control plane with deterministic (default), llm-auto, "
            "or llm-manual mode selection, resuming from passed stages in manifest.json."
        ),
    )
    run.add_argument("image", help="target image reference")
    run.add_argument("--work-dir", help="existing or new work directory")
    run.add_argument(
        "--config",
        help="optional run YAML config file providing defaults; priority is CLI explicit args > config > preset > built-in defaults",
    )
    run.add_argument(
        "--preset",
        choices=FORMAL_RUN_PRESETS,
        help=(
            "named default-value set for common run scenarios "
            "(currently: tfpy-half-plus-two-io1g, tf-serving-efficientnet-v2-b0, tf-serving-half-plus-two); "
            "CLI explicit arguments still win"
        ),
    )
    run.add_argument(
        "--startup-mode",
        choices=("auto", "legacy", "gated"),
        default=None,
    )
    _add_startup_backend_preference_arg(run)
    run.add_argument("--duration", type=int, default=90, help="sampling window in seconds")
    run.add_argument("--prom-port", type=int, default=8000, help="preferred Prometheus port start")
    run.add_argument("--mmap-window", type=int, default=120, help="mmap capture window seconds")
    run.add_argument("--cg-refresh", type=int, default=2, help="cgroup whitelist refresh seconds")
    run.add_argument(
        "--workload",
        choices=workload_choices(),
        default=None,
        help="sample workload profile for readiness defaults; if omitted, a preset may supply it, else defaults to none",
    )
    run.add_argument("--inject-io", action="store_true", help="enable workload-specific I/O stimulation after readiness")
    run.add_argument("--ready-url", help="optional workload readiness probe URL")
    run.add_argument("--ready-exec", help="optional in-container readiness command; succeeds on exit code 0")
    run.add_argument("--ready-log-regex", help="optional container log regex treated as readiness signal")
    run.add_argument("--ready-shell", default="sh", help="shell for --ready-exec (default: sh)")
    run.add_argument("--predict-url", help="optional workload predict URL for injection")
    run.add_argument("--predict-body", help="optional JSON body for workload predict injection")
    run.add_argument("--inject-cmd", help="optional shell command to execute inside the container for I/O stimulation")
    run.add_argument("--model-host", help="optional host model directory to mount for sample workload")
    run.add_argument("--model-dst", help="optional in-container model destination directory override")
    run.add_argument("--model-name", help="optional model name override for sample workload")
    run.add_argument("--ready-timeout", type=int, default=None, help="readiness timeout in seconds; if omitted, inherits the workload default")
    run.add_argument(
        "--mode",
        dest="run_mode",
        choices=FORMAL_RUN_MODES,
        help=(
            "product run mode: deterministic (default) skips llm-optimize, llm-auto requires an API key and "
            "uses provider-aware API defaults with fallback only on upstream/unusable API responses, llm-manual exports/imports the web-LLM flow using a strict JSON response "
            "(legacy YAML import remains compatible); "
            "preset defaults only apply when this is omitted"
        ),
    )
    run.add_argument(
        "--llm",
        action="store_true",
        help="legacy compatibility alias for --mode llm-auto; prefer --mode for new usage",
    )
    run.add_argument("--no-runtime-probes", action="store_true")
    _add_container_runtime_args(run)
    _add_llm_optimize_args(run, legacy_compat=True)
    run.add_argument("--min-events", type=int, default=30)
    run.add_argument("--max-mmap-maps-ratio", type=float, default=0.50)
    run.add_argument("--size-budget-percent", type=float, default=6.4)
    run.add_argument("--size-budget-mb", type=int, default=0)
    run.add_argument("--min-hits", type=int, default=2)
    run.add_argument("--p0-max", type=int, default=24)
    run.add_argument("--early-onehit-ratio", type=float, default=0.12)
    run.add_argument("--early-onehit-max", type=int, default=8)
    run.add_argument("--early-onehit-max-size-kb", type=int, default=512)
    _add_selector_mode_arg(run)
    run.add_argument(
        "--allow-low-fidelity",
        action="store_true",
        help="legacy compatibility flag; low-fidelity quality findings are advisory by default",
    )
    run.add_argument(
        "--apply",
        action="store_true",
        help="include the formal apply stage after analyze / mode-specific optimization",
    )
    run.add_argument(
        "--dst-image",
        help="destination optimized image reference for --apply; if omitted, run auto-generates <src-tag>-optimized or :optimized",
    )
    run.add_argument("--namespace", default="default", help="containerd namespace for --apply")
    run.add_argument("--apply-dry-run", action="store_true", help="run apply in dry-run mode")
    run.add_argument(
        "--benchmark",
        action="store_true",
        help="include the formal benchmark stage after apply, or against an existing optimized image artifact",
    )
    run.add_argument(
        "--bench-unopt-image",
        help="unoptimized eStargz image override required only for --bench-comparison-mode raw-full-vs-estargz-lazy-triple",
    )
    run.add_argument("--bench-opt-image", help="optimized image override for --benchmark")
    run.add_argument("--bench-runs", type=int, default=3)
    run.add_argument(
        "--bench-timeout",
        type=int,
        default=None,
        help="benchmark readiness timeout in seconds; if omitted, inherits the benchmark workload default",
    )
    run.add_argument("--bench-snapshotter", default="stargz")
    run.add_argument("--bench-base-snapshotter")
    run.add_argument("--bench-opt-snapshotter")
    run.add_argument(
        "--bench-comparison-mode",
        choices=("same-snapshotter", "raw-vs-estargz", "raw-full-vs-estargz-lazy", "raw-full-vs-estargz-lazy-triple"),
        default="same-snapshotter",
        help="benchmark image/snapshotter comparison contract for --benchmark; default preserves existing behavior",
    )
    run.add_argument(
        "--bench-baseline-stargz",
        action="store_true",
        help="explicitly benchmark baseline on the benchmark snapshotter too; default run behavior remains overlayfs-vs-stargz",
    )
    run.add_argument(
        "--bench-mode",
        choices=("struct", "ready"),
        help=(
            "benchmark mode; if omitted, run uses the workload recommendation "
            "(for example: ready-serving-bootstrap-v2 -> ready, tfpy-half-plus-two-io1g -> ready, tf-serving-efficientnet-v2-b0 -> ready, tf-serving-half-plus-two -> ready) "
            "or falls back to struct"
        ),
    )
    run.add_argument(
        "--bench-workload",
        choices=workload_choices(),
        help="benchmark workload profile; if omitted, inherits --workload",
    )
    run.add_argument("--bench-ready-url")
    run.add_argument("--bench-ready-exec", help="optional in-container benchmark readiness command; succeeds on exit code 0")
    run.add_argument("--bench-ready-log-regex", help="optional benchmark container log regex treated as readiness signal")
    run.add_argument("--bench-ready-shell", default="sh", help="shell for --bench-ready-exec (default: sh)")
    run.add_argument("--bench-predict-url")
    run.add_argument("--bench-predict-body")
    run.add_argument("--bench-model-host", help="benchmark host model directory; if omitted, inherits --model-host")
    run.add_argument("--bench-model-dst")
    run.add_argument("--bench-model-name")
    run.add_argument("--bench-platform", default="linux/amd64")
    run.add_argument("--bench-soft-cold", action="store_true", help="use soft-cold mode for --benchmark")
    run.add_argument("--bench-force-reset", action="store_true")
    run.add_argument("--bench-force-kill-port", action="store_true")
    run.add_argument("--bench-allow-busy-port", action="store_true")
    run.add_argument("--bench-reconcile-dirs", action="store_true")
    run.add_argument("--bench-no-drop-cache", action="store_true")
    run.add_argument("--bench-no-clear-stargz", action="store_true")
    run.add_argument("--bench-shuffle", action="store_true")
    run.add_argument("--bench-log-dir")
    run.add_argument("--bench-pull-policy", choices=("never", "missing", "always"), default="never")
    run.add_argument("--bench-drop-local-image-ref", action="store_true")
    run.add_argument(
        "--bench-cold-mode",
        choices=("local-cold", "registry-cold-strict"),
        default="local-cold",
        help="benchmark cold-state contract for --benchmark (default: local-cold)",
    )
    _add_container_runtime_args(run, flag_prefix="bench-", attr_prefix="bench_", help_prefix="benchmark container")
    args = parser.parse_args()
    if args.command == "run":
        try:
            args.run_mode = _resolve_run_mode(args)
        except ValueError as exc:
            parser.error(str(exc))
    return args


def main() -> int:
    raw_argv = list(sys.argv[1:])
    args = parse_args()
    run_parameter_resolution: dict[str, dict[str, str]] = {}
    run_config_path: Path | None = None
    if args.command == "run":
        try:
            run_parameter_resolution, run_config_path = _normalize_run_parameters(args, raw_argv=raw_argv)
        except ValueError as exc:
            print("[orchestrator] run=invalid", file=sys.stderr)
            print(f"  - {exc}", file=sys.stderr)
            return 2
    _apply_direct_benchmark_mode_defaults(args, raw_argv=raw_argv)
    _apply_benchmark_comparison_mode_defaults(args, raw_argv=raw_argv)
    try:
        _validate_benchmark_comparison_mode_inputs(args)
    except ValueError as exc:
        print("[orchestrator] run=invalid", file=sys.stderr)
        print(f"  - {exc}", file=sys.stderr)
        return 2
    try:
        _apply_benchmark_cold_mode_defaults(args, raw_argv=raw_argv)
    except ValueError as exc:
        print("[orchestrator] run=invalid", file=sys.stderr)
        print(f"  - {exc}", file=sys.stderr)
        return 2
    if args.command == "run" and args.run_mode == "llm-auto":
        try:
            llm_config = resolve_llm_provider_config(
                provider=getattr(args, "llm_provider", None),
                api_base=getattr(args, "llm_api_base", None),
                api_key_env=getattr(args, "llm_api_key_env", None),
                model=getattr(args, "llm_model", None),
                reasoning_effort=getattr(args, "llm_reasoning_effort", None),
                timeout_sec=getattr(args, "llm_timeout_sec", None),
            )
        except ValueError as exc:
            print("[orchestrator] run=invalid", file=sys.stderr)
            print(f"  - {exc}", file=sys.stderr)
            return 2
        if provider_requires_api_key(llm_config) and not llm_config.api_key:
            print("[orchestrator] run=invalid", file=sys.stderr)
            print(
                f"  - --mode llm-auto requires an API key: {describe_missing_api_key(llm_config)}; "
                "it will not silently fall back to manual mode",
                file=sys.stderr,
            )
            return 2
    repo_root = REPO_ROOT
    work_dir = Path(args.work_dir).resolve() if getattr(args, "work_dir", None) else default_work_dir(repo_root)
    work_dir.mkdir(parents=True, exist_ok=True)
    run_id = work_dir.name
    manifest_path = work_dir / "manifest.json"
    if manifest_path.exists():
        manifest = ManifestStore.load(manifest_path)
    else:
        manifest_seed_image = (
            getattr(args, "image", None)
            or getattr(args, "src_image", None)
            or getattr(args, "base_image", None)
            or getattr(args, "opt_image", None)
        )
        if not manifest_seed_image:
            raise RuntimeError("a new manifest requires an image argument")
        manifest = ManifestStore.create(
            manifest_path,
            run_id=run_id,
            work_dir=work_dir,
            image=manifest_seed_image,
            startup_mode=getattr(args, "startup_mode", "legacy"),
            startup_backend_preference=getattr(args, "startup_backend_preference", "auto"),
            llm_enabled=_run_uses_llm_api(args) if args.command == "run" else bool(getattr(args, "llm", False)),
        )

    options = manifest.data.setdefault("options", {})
    if args.command in {"sample", "run"} and hasattr(args, "workload"):
        options["sample_workload"] = getattr(args, "workload")
    if hasattr(args, "startup_backend_preference"):
        options["startup_backend_preference"] = getattr(args, "startup_backend_preference")
    if args.command == "run":
        options["mode"] = args.run_mode
        options["llm_enabled"] = _run_uses_llm_api(args)
        options["preset"] = getattr(args, "preset", None)
        if run_config_path is not None:
            options["config"] = str(run_config_path)
        else:
            options.pop("config", None)
        if run_parameter_resolution:
            options["run_parameter_resolution"] = run_parameter_resolution
        else:
            options.pop("run_parameter_resolution", None)
    if args.command in {"analyze", "run"} and hasattr(args, "selector_mode"):
        options["selector_mode"] = getattr(args, "selector_mode")
    elif args.command == "apply" and hasattr(args, "selector_mode") and getattr(args, "selector_mode", None):
        options["apply_selector_mode"] = getattr(args, "selector_mode")
    elif args.command == "benchmark" and hasattr(args, "selector_mode") and getattr(args, "selector_mode", None):
        options["benchmark_selector_mode"] = getattr(args, "selector_mode")
    if args.command == "benchmark" and hasattr(args, "workload"):
        options["benchmark_workload"] = getattr(args, "workload")
    elif args.command == "run" and getattr(args, "benchmark", False) and hasattr(args, "bench_workload"):
        options["benchmark_workload"] = getattr(args, "bench_workload")
    if args.command == "benchmark" and hasattr(args, "cold_mode"):
        options["benchmark_cold_mode"] = getattr(args, "cold_mode")
    elif args.command == "run" and getattr(args, "benchmark", False) and hasattr(args, "bench_cold_mode"):
        options["benchmark_cold_mode"] = getattr(args, "bench_cold_mode")
    if args.command == "benchmark" and hasattr(args, "comparison_mode"):
        options["benchmark_comparison_mode"] = getattr(args, "comparison_mode")
    elif args.command == "run" and getattr(args, "benchmark", False) and hasattr(args, "bench_comparison_mode"):
        options["benchmark_comparison_mode"] = getattr(args, "bench_comparison_mode")
    if hasattr(args, "manual_web"):
        options["manual_web"] = bool(getattr(args, "manual_web"))
    manifest.save()
    if args.command == "run" and run_parameter_resolution:
        print("[orchestrator] run-args=normalized")
        for option_name, entry in run_parameter_resolution.items():
            print(f"  - {option_name}={entry.get('value')} ({entry.get('source')})")

    if args.command == "preflight":
        manifest.stage_begin("preflight", "Validate fixed product support boundaries")
        result = run_preflight(
            repo_root=repo_root,
            work_dir=work_dir,
            image=args.image,
            startup_mode=args.startup_mode,
            startup_backend_preference=args.startup_backend_preference,
            llm_enabled=_run_uses_llm_api(args),
            runtime_probes=not args.no_runtime_probes,
        )
        if result.policy_payload["ok"]:
            manifest.stage_finish(
                "preflight",
                status="passed",
                summary="Product support boundary accepted",
                artifacts={
                    "env_check": str(result.env_check_path),
                    "product_policy": str(result.policy_path),
                },
                warnings=result.policy_payload.get("warnings") or [],
            )
            print(f"[orchestrator] preflight=pass work_dir={work_dir}")
            print(f"[orchestrator] manifest={work_dir / 'manifest.json'}")
            return 0

        manifest.stage_finish(
            "preflight",
            status="failed",
            summary="Product support boundary rejected this environment",
            artifacts={
                "env_check": str(result.env_check_path),
                "product_policy": str(result.policy_path),
            },
            warnings=result.policy_payload.get("warnings") or [],
            error="; ".join(result.policy_payload.get("errors") or ["preflight failed"]),
        )
        print(f"[orchestrator] preflight=fail work_dir={work_dir}", file=sys.stderr)
        for item in result.policy_payload.get("errors") or []:
            print(f"  - {item}", file=sys.stderr)
        print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
        return 1

    if args.command == "sample":
        sample_container_runtime = _resolve_container_runtime(
            args=args,
            manifest=manifest,
            option_key="sample_container_runtime",
        )
        sample_workload = resolve_sample_workload(
            profile_name=args.workload,
            ready_url=args.ready_url,
            ready_exec=args.ready_exec,
            ready_log_regex=args.ready_log_regex,
            ready_shell=args.ready_shell,
            ready_timeout=args.ready_timeout,
            predict_url=args.predict_url,
            predict_body=args.predict_body,
            inject_io_cmd=args.inject_cmd,
            model_host_dir=args.model_host,
            model_dst_dir=args.model_dst,
            model_name=args.model_name,
        )
        try:
            sample_container_runtime = _merge_model_mount_into_runtime(
                sample_container_runtime,
                model_host_dir=str(sample_workload["model_host_dir"] or ""),
                model_mount_hook=sample_workload["effective_model_mount_hook"],
            )
        except RuntimeError as exc:
            return _print_workload_validation_failure(
                stage="sample",
                work_dir=work_dir,
                errors=[str(exc)],
            )
        options["sample_container_runtime"] = sample_container_runtime.summary()
        manifest.save()
        sample_workload_errors = validate_sample_workload_resolution(sample_workload, inject_io=args.inject_io)
        if sample_workload_errors:
            return _print_workload_validation_failure(
                stage="sample",
                work_dir=work_dir,
                errors=sample_workload_errors,
            )
        preflight_stage = manifest.data.get("stages", {}).get("preflight", {})
        if preflight_stage.get("status") != "passed":
            manifest.stage_begin("preflight", "Validate fixed product support boundaries")
            result = run_preflight(
                repo_root=repo_root,
                work_dir=work_dir,
                image=args.image,
                startup_mode=args.startup_mode,
                startup_backend_preference=args.startup_backend_preference,
                llm_enabled=_run_uses_llm_api(args),
                runtime_probes=not args.no_runtime_probes,
            )
            if not result.policy_payload["ok"]:
                manifest.stage_finish(
                    "preflight",
                    status="failed",
                    summary="Product support boundary rejected this environment",
                    artifacts={
                        "env_check": str(result.env_check_path),
                        "product_policy": str(result.policy_path),
                    },
                    warnings=result.policy_payload.get("warnings") or [],
                    error="; ".join(result.policy_payload.get("errors") or ["preflight failed"]),
                )
                print(f"[orchestrator] sample=fail work_dir={work_dir}", file=sys.stderr)
                for item in result.policy_payload.get("errors") or []:
                    print(f"  - {item}", file=sys.stderr)
                print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
                return 1
            manifest.stage_finish(
                "preflight",
                status="passed",
                summary="Product support boundary accepted",
                artifacts={
                    "env_check": str(result.env_check_path),
                    "product_policy": str(result.policy_path),
                },
                warnings=result.policy_payload.get("warnings") or [],
            )

        manifest.stage_begin("sample", "Launch container and capture raw events via file sink")
        try:
            result = run_sample_stage(
                repo_root=repo_root,
                work_dir=work_dir,
                image=args.image,
                run_id=run_id,
                startup_mode=args.startup_mode,
                startup_backend_preference=args.startup_backend_preference,
                duration=args.duration,
                prom_port_start=args.prom_port,
                mmap_window=args.mmap_window,
                cg_refresh=args.cg_refresh,
                ready_url=sample_workload["ready_url"],
                ready_timeout=int(sample_workload["ready_timeout"]),
                workload_profile=str(sample_workload["profile"]),
                self_starting_service=bool(sample_workload.get("self_starting_service", False)),
                workload_profile_capabilities=tuple(sample_workload["profile_capabilities"]),
                workload_effective_capabilities=tuple(sample_workload["effective_capabilities"]),
                workload_profile_hooks=sample_workload["profile_hooks"],
                workload_effective_hooks=sample_workload["effective_hooks"],
                ready_hook=sample_workload["effective_ready_hook"],
                predict_hook=sample_workload["effective_predict_hook"],
                inject_hook=sample_workload["effective_inject_hook"],
                container_runtime=sample_container_runtime,
                inject_io=args.inject_io,
                predict_url=sample_workload["predict_url"],
                predict_body=sample_workload["predict_body"],
                inject_io_cmd=sample_workload["inject_io_cmd"],
            )
        except Exception as exc:
            manifest.stage_finish(
                "sample",
                status="failed",
                summary="Container launch or sampling failed",
                artifacts=_sample_stage_artifacts_from_work_dir(work_dir),
                error=str(exc),
            )
            print(f"[orchestrator] sample=fail work_dir={work_dir}", file=sys.stderr)
            print(f"  - {exc}", file=sys.stderr)
            print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
            return 1

        manifest.stage_finish(
            "sample",
            status="passed",
            summary="Container launched and raw sampling artifacts captured",
            artifacts={
                "runtime_state": str(result.runtime_state_path),
                "raw_events": str(result.raw_events_path),
                "sampler_log": str(result.sampler_log_path),
                "sampler_metrics": str(result.sampler_metrics_path),
            },
        )
        print(f"[orchestrator] sample=pass work_dir={work_dir}")
        print(f"[orchestrator] manifest={work_dir / 'manifest.json'}")
        return 0

    if args.command == "export-enrich":
        sample_stage = manifest.data.get("stages", {}).get("sample", {})
        if sample_stage.get("status") != "passed":
            print("[orchestrator] export-enrich=fail work_dir={}".format(work_dir), file=sys.stderr)
            print("  - sample stage must pass before export-enrich", file=sys.stderr)
            print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
            return 1

        manifest.stage_begin("export_enrich", "Export raw file-sink events and enrich OPEN/MMAP metadata")
        try:
            result = run_export_enrich_stage(
                repo_root=repo_root,
                work_dir=work_dir,
            )
        except Exception as exc:
            manifest.stage_finish(
                "export_enrich",
                status="failed",
                summary="Event export/enrich stage failed",
                error=str(exc),
            )
            print(f"[orchestrator] export-enrich=fail work_dir={work_dir}", file=sys.stderr)
            print(f"  - {exc}", file=sys.stderr)
            print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
            return 1

        manifest.stage_finish(
            "export_enrich",
            status="passed",
            summary="Raw events exported and MMAP/Open metadata enriched",
            artifacts={
                "events": str(result.events_path),
                "export_events_stats": str(result.export_stats_path),
                "cwd_snapshot": str(result.cwd_snapshot_path),
                "maps_paths": str(result.maps_paths_path),
                "mapfiles_devino": str(result.mapfiles_devino_path),
                "devino_dict": str(result.devino_dict_path),
                "events_enriched": str(result.events_enriched_path),
                "export_enrich_log": str(result.stage_log_path),
            },
        )
        print(f"[orchestrator] export-enrich=pass work_dir={work_dir}")
        print(f"[orchestrator] manifest={work_dir / 'manifest.json'}")
        return 0

    if args.command == "analyze":
        export_stage = manifest.data.get("stages", {}).get("export_enrich", {})
        if export_stage.get("status") != "passed":
            print("[orchestrator] analyze=fail work_dir={}".format(work_dir), file=sys.stderr)
            print("  - export_enrich stage must pass before analyze", file=sys.stderr)
            print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
            return 1

        manifest.stage_begin("analyze", "Aggregate candidates, enforce quality gate, and build deterministic hints")
        try:
            result = run_analyze_stage(
                repo_root=repo_root,
                work_dir=work_dir,
                min_events=args.min_events,
                max_mmap_maps_ratio=args.max_mmap_maps_ratio,
                size_budget_percent=args.size_budget_percent,
                size_budget_mb=args.size_budget_mb,
                min_hits=args.min_hits,
                p0_max=args.p0_max,
                early_onehit_ratio=args.early_onehit_ratio,
                early_onehit_max=args.early_onehit_max,
                early_onehit_max_size_kb=args.early_onehit_max_size_kb,
                selector_mode=args.selector_mode,
                allow_low_fidelity=args.allow_low_fidelity,
            )
        except Exception as exc:
            manifest.stage_finish(
                "analyze",
                status="failed",
                summary="Aggregate/quality/select stage failed",
                error=str(exc),
            )
            print(f"[orchestrator] analyze=fail work_dir={work_dir}", file=sys.stderr)
            print(f"  - {exc}", file=sys.stderr)
            print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
            return 1

        summary = "Deterministic hints produced from exported events"
        if str(result.quality_payload.get("quality_status") or "") not in {"", "good"}:
            summary = (
                "Deterministic hints produced with advisory quality warnings "
                f"({result.quality_payload.get('quality_status')})"
            )
        analyze_artifacts = {
            "quality_gate": str(result.quality_gate_path),
            "candidates": str(result.candidates_path),
            "aggregate_stats": str(result.aggregate_stats_path),
            "ranked": str(result.ranked_path),
            "estargz_list": str(result.estargz_list_path),
            "selector_stats_default": str(result.selector_stats_path),
            "selector_stats_hints": str(result.selector_stats_hint_path),
            "hints": str(result.hints_path),
            "analyze_log": str(result.stage_log_path),
        }
        if result.image_toc_path is not None:
            analyze_artifacts["image_toc"] = str(result.image_toc_path)
        if result.toc_shadow_path is not None:
            analyze_artifacts["toc_shadow"] = str(result.toc_shadow_path)
        if result.chunk_selector_shadow_path is not None:
            analyze_artifacts["chunk_selector_shadow"] = str(result.chunk_selector_shadow_path)
        if result.chunk_shadow_ranked_path is not None:
            analyze_artifacts["ranked_chunk_shadow"] = str(result.chunk_shadow_ranked_path)
        if result.chunk_shadow_estargz_list_path is not None:
            analyze_artifacts["estargz_list_chunk_shadow"] = str(result.chunk_shadow_estargz_list_path)
        if result.chunk_shadow_hints_path is not None:
            analyze_artifacts["hints_chunk_shadow"] = str(result.chunk_shadow_hints_path)
        if result.chunk_shadow_selector_stats_path is not None:
            analyze_artifacts["selector_stats_chunk_shadow"] = str(result.chunk_shadow_selector_stats_path)
        manifest.stage_finish(
            "analyze",
            status="passed",
            summary=summary,
            artifacts=analyze_artifacts,
            warnings=result.warnings + list(result.quality_payload.get("warnings") or []),
        )
        print(f"[orchestrator] analyze=pass work_dir={work_dir}")
        print(f"[orchestrator] manifest={work_dir / 'manifest.json'}")
        return 0

    if args.command == "llm-optimize":
        analyze_stage = manifest.data.get("stages", {}).get("analyze", {})
        if analyze_stage.get("status") != "passed":
            print("[orchestrator] llm-optimize=fail work_dir={}".format(work_dir), file=sys.stderr)
            print("  - analyze stage must pass before llm-optimize", file=sys.stderr)
            print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
            return 1

        manifest.stage_begin("llm_optimize", "Build prompt.txt and apply grounded LLM priority optimization")
        try:
            result = run_llm_optimize_stage(
                repo_root=repo_root,
                work_dir=work_dir,
                size_budget_percent=args.size_budget_percent,
                size_budget_mb=args.size_budget_mb,
                min_hits=args.min_hits,
                p0_max=args.p0_max,
                early_onehit_ratio=args.early_onehit_ratio,
                early_onehit_max=args.early_onehit_max,
                early_onehit_max_size_kb=args.early_onehit_max_size_kb,
                llm_provider=args.llm_provider,
                llm_api_key_env=args.llm_api_key_env,
                llm_api_base=args.llm_api_base,
                llm_model=args.llm_model,
                llm_reasoning_effort=args.llm_reasoning_effort,
                llm_timeout_sec=args.llm_timeout_sec,
                llm_topn=args.llm_topn,
                manual_response_file=args.manual_response_file,
                force_manual_export=bool(args.manual_web),
                requested_mode=("llm-manual" if args.manual_web or args.manual_response_file else "llm-auto"),
            )
        except Exception as exc:
            manifest.stage_finish(
                "llm_optimize",
                status="failed",
                summary="LLM optimization stage failed",
                error=str(exc),
            )
            print(f"[orchestrator] llm-optimize=fail work_dir={work_dir}", file=sys.stderr)
            print(f"  - {exc}", file=sys.stderr)
            print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
            return 1

        llm_artifacts = _llm_stage_artifacts(result)
        if result.mode == "manual_export":
            manifest.stage_finish(
                "llm_optimize",
                status="paused",
                summary="Prompt package exported for manual web-LLM input",
                artifacts=llm_artifacts,
                warnings=result.warnings,
                pause_reason=result.pause_reason,
                resume_inputs=_llm_pause_resume_inputs(result),
                resume_command=result.resume_command,
            )
            _print_manual_pause_summary(
                work_dir=work_dir,
                web_input_path=result.web_input_path,
                manual_instructions_path=result.manual_instructions_path,
                manual_response_path=result.manual_response_default_path,
                resume_command=str(result.resume_command or ""),
            )
            print(f"[orchestrator] manifest={work_dir / 'manifest.json'}")
            return 0

        manifest.stage_finish(
            "llm_optimize",
            status="passed",
            summary=_llm_stage_summary(result),
            artifacts=llm_artifacts,
            warnings=result.warnings,
        )
        print(f"[orchestrator] llm-optimize=pass work_dir={work_dir}")
        _print_llm_result_summary(result)
        print(f"[orchestrator] manifest={work_dir / 'manifest.json'}")
        return 0

    if args.command == "apply":
        src_image = manifest_image(manifest, getattr(args, "src_image", None))
        if not src_image:
            print("[orchestrator] apply=fail work_dir={}".format(work_dir), file=sys.stderr)
            print("  - source image is missing; use --src-image or provide a manifest with image metadata", file=sys.stderr)
            print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
            return 1

        effective_selector_mode = resolved_selector_mode(manifest, getattr(args, "selector_mode", None))
        manifest.stage_begin("apply", "Apply hints.yaml/estargz.list to produce an optimized image")
        try:
            result = run_apply_stage(
                repo_root=repo_root,
                work_dir=work_dir,
                src_image=src_image,
                selector_mode=effective_selector_mode,
                dst_image=args.dst_image,
                namespace=args.namespace,
                dry_run=args.dry_run,
                runtime_probes=not args.no_runtime_probes,
            )
        except KeyboardInterrupt:
            manifest.stage_finish(
                "apply",
                status="cancelled",
                summary="Apply stage cancelled by user",
                error="interrupted by user",
            )
            print(f"[orchestrator] apply=cancelled work_dir={work_dir}", file=sys.stderr)
            print("  - interrupted by user", file=sys.stderr)
            print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
            return 130
        except Exception as exc:
            manifest.stage_finish(
                "apply",
                status="failed",
                summary="Apply stage failed",
                error=str(exc),
            )
            print(f"[orchestrator] apply=fail work_dir={work_dir}", file=sys.stderr)
            print(f"  - {exc}", file=sys.stderr)
            print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
            return 1

        summary = "Optimized image created from current hints"
        if effective_selector_mode != "file":
            summary = f"Experimental {effective_selector_mode} optimized image created from current hints"
        if args.dry_run:
            summary = "Apply dry-run completed; optimized image import was not executed"
        if effective_selector_mode == "file":
            artifacts = {
                "env_check_apply": str(result.env_check_path),
                "apply_policy": str(result.policy_path),
                "apply_log": str(result.stage_log_path),
                "apply_result": str(result.apply_result_path),
            }
            if result.optimized_image_path is not None:
                artifacts["optimized_image"] = str(result.optimized_image_path)
        else:
            artifacts = {
                "env_check_apply_chunk_shadow": str(result.env_check_path),
                "apply_policy_chunk_shadow": str(result.policy_path),
                "apply_log_chunk_shadow": str(result.stage_log_path),
                "apply_result_chunk_shadow": str(result.apply_result_path),
            }
            if result.optimized_image_path is not None:
                artifacts["optimized_image_chunk_shadow"] = str(result.optimized_image_path)
        manifest.stage_finish(
            "apply",
            status="passed",
            summary=summary,
            artifacts=artifacts,
            warnings=result.warnings,
        )
        print(f"[orchestrator] apply=pass work_dir={work_dir}")
        _print_apply_result_summary(result)
        print(f"[orchestrator] manifest={work_dir / 'manifest.json'}")
        return 0

    if args.command == "publish":
        selector_mode = resolved_selector_mode(manifest, getattr(args, "selector_mode", None))
        try:
            src_image, optimized_publish, optimized_publish_selector_mode = _resolve_publish_source_image(
                manifest,
                work_dir=work_dir,
                cli_src_image=getattr(args, "src_image", None),
                use_optimized=bool(getattr(args, "optimized", False)),
                selector_mode=selector_mode,
            )
        except ValueError as exc:
            print(f"[orchestrator] publish=fail work_dir={work_dir}", file=sys.stderr)
            print(f"  - {exc}", file=sys.stderr)
            print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
            return 1
        if not src_image:
            print(f"[orchestrator] publish=fail work_dir={work_dir}", file=sys.stderr)
            if getattr(args, "optimized", False) or optimized_publish:
                print(
                    f"  - publish requires an optimized image artifact for selector mode {optimized_publish_selector_mode}",
                    file=sys.stderr,
                )
            else:
                print("  - publish requires a source image", file=sys.stderr)
            print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
            return 1
        manifest.stage_begin("publish", "Tag and push a local image to a registry reference")
        try:
            result = run_publish_stage(
                repo_root=repo_root,
                work_dir=work_dir,
                src_image=src_image,
                dst_image=args.dst_image,
                namespace=args.namespace,
                plain_http=args.plain_http,
            )
        except Exception as exc:
            manifest.stage_finish(
                "publish",
                status="failed",
                summary="Publish stage failed",
                error=str(exc),
            )
            print(f"[orchestrator] publish=fail work_dir={work_dir}", file=sys.stderr)
            print(f"  - {exc}", file=sys.stderr)
            print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
            return 1
        _update_json_dict(
            result.result_path,
            {
                "source_kind": "optimized" if optimized_publish else "manifest",
                "optimized_publish": bool(optimized_publish),
                "selector_mode": optimized_publish_selector_mode if optimized_publish else None,
            },
        )
        manifest.stage_finish(
            "publish",
            status="passed",
            summary=(
                "Registry publish completed from optimized image"
                if optimized_publish
                else "Registry publish completed"
            ),
            artifacts={
                "publish_log": str(result.stage_log_path),
                "publish_result": str(result.result_path),
            },
        )
        print(f"[orchestrator] publish=pass work_dir={work_dir}")
        print(f"  - src_image={result.src_image}")
        print(f"  - dst_image={result.dst_image}")
        print(f"[orchestrator] manifest={work_dir / 'manifest.json'}")
        return 0

    if args.command == "benchmark":
        benchmark_container_runtime = _resolve_container_runtime(
            args=args,
            manifest=manifest,
            option_key="benchmark_container_runtime",
        )
        options["benchmark_container_runtime"] = benchmark_container_runtime.summary()
        manifest.save()
        benchmark_workload = resolve_benchmark_workload(
            profile_name=args.workload,
            timeout_ready=args.timeout,
            ready_url=args.ready_url,
            ready_exec=args.ready_exec,
            ready_log_regex=args.ready_log_regex,
            ready_shell=args.ready_shell,
            predict_url=args.predict_url,
            predict_body=args.predict_body,
            model_host_dir=args.model_host,
            model_dst_dir=args.model_dst,
            model_name=args.model_name,
        )
        benchmark_workload_errors = validate_benchmark_workload_resolution(
            benchmark_workload,
            mode=args.mode,
        )
        if benchmark_workload_errors:
            return _print_workload_validation_failure(
                stage="benchmark",
                work_dir=work_dir,
                errors=benchmark_workload_errors,
            )
        base_image = manifest_image(manifest, getattr(args, "base_image", None))
        effective_selector_mode = resolved_selector_mode(manifest, getattr(args, "selector_mode", None))
        opt_image = optimized_image_from_manifest(
            manifest,
            work_dir=work_dir,
            cli_image=getattr(args, "opt_image", None),
            selector_mode=effective_selector_mode,
        )
        if not base_image or not opt_image:
            print("[orchestrator] benchmark=fail work_dir={}".format(work_dir), file=sys.stderr)
            print("  - benchmark requires both baseline image and optimized image metadata", file=sys.stderr)
            print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
            return 1

        _cleanup_sample_container_before_benchmark(repo_root=repo_root, work_dir=work_dir)
        manifest.stage_begin("benchmark", "Benchmark baseline and optimized images under strict cold-start policy")
        try:
            result = run_benchmark_stage(
                repo_root=repo_root,
                work_dir=work_dir,
                base_image=base_image,
                opt_image=opt_image,
                unopt_image=str(getattr(args, "unopt_image", "") or ""),
                selector_mode=effective_selector_mode,
                namespace=args.namespace,
                runs=args.runs,
                timeout_ready=int(benchmark_workload["timeout_ready"]),
                snapshotter=args.snapshotter,
                base_snapshotter=args.base_snapshotter,
                opt_snapshotter=args.opt_snapshotter,
                baseline_stargz=args.baseline_stargz,
                comparison_mode=args.comparison_mode,
                mode=args.mode,
                ready_url=str(benchmark_workload["ready_url"] or ""),
                debug_state_url=str(benchmark_workload.get("debug_state_url") or ""),
                predict_url=str(benchmark_workload["predict_url"] or ""),
                predict_body=str(benchmark_workload["predict_body"] or ""),
                model_host_dir=str(benchmark_workload["model_host_dir"] or ""),
                model_dst_dir=str(benchmark_workload["model_dst_dir"] or ""),
                model_name=str(benchmark_workload["model_name"] or ""),
                platform=args.platform,
                hard_cold=args.hard_cold,
                force_reset=args.force_reset,
                force_kill_port=args.force_kill_port,
                allow_busy_port=args.allow_busy_port,
                reconcile_dirs=args.reconcile_dirs,
                drop_page_cache=args.drop_page_cache,
                clear_stargz_cache=args.clear_stargz_cache,
                shuffle_order=args.shuffle,
                log_dir=args.log_dir,
                runtime_probes=not args.no_runtime_probes,
                pull_policy=args.pull_policy,
                drop_local_image_ref=args.drop_local_image_ref,
                cold_mode=args.cold_mode,
                workload_profile=str(benchmark_workload["profile"]),
                self_starting_service=bool(benchmark_workload.get("self_starting_service", False)),
                workload_profile_capabilities=tuple(benchmark_workload["profile_capabilities"]),
                workload_effective_capabilities=tuple(benchmark_workload["effective_capabilities"]),
                workload_profile_hooks=benchmark_workload["profile_hooks"],
                workload_effective_hooks=benchmark_workload["effective_hooks"],
                ready_hook=benchmark_workload["effective_ready_hook"],
                predict_hook=benchmark_workload["effective_predict_hook"],
                model_mount_hook=benchmark_workload["effective_model_mount_hook"],
                container_runtime=benchmark_container_runtime,
            )
        except KeyboardInterrupt:
            manifest.stage_finish(
                "benchmark",
                status="cancelled",
                summary="Benchmark stage cancelled by user",
                error="interrupted by user",
            )
            print(f"[orchestrator] benchmark=cancelled work_dir={work_dir}", file=sys.stderr)
            print("  - interrupted by user", file=sys.stderr)
            print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
            return 130
        except Exception as exc:
            manifest.stage_finish(
                "benchmark",
                status="failed",
                summary="Benchmark stage failed",
                error=str(exc),
            )
            print(f"[orchestrator] benchmark=fail work_dir={work_dir}", file=sys.stderr)
            print(f"  - {exc}", file=sys.stderr)
            print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
            return 1

        benchmark_artifacts = {
            "env_check_benchmark_base": str(result.env_check_base_path),
            "env_check_benchmark_opt": str(result.env_check_opt_path),
            "benchmark_policy": str(result.policy_path),
            "benchmark_log": str(result.stage_log_path),
            "benchmark_csv": str(result.csv_path),
            "benchmark_log_dir": str(result.log_dir),
            "benchmark_result": str(result.result_path),
        }
        if not result.valid:
            manifest.stage_finish(
                "benchmark",
                status="failed",
                summary="Benchmark stage produced no valid comparison",
                artifacts=benchmark_artifacts,
                warnings=result.warnings,
                error="; ".join(result.invalid_reasons),
            )
            print(f"[orchestrator] benchmark=fail work_dir={work_dir}", file=sys.stderr)
            for reason in result.invalid_reasons:
                print(f"  - {reason}", file=sys.stderr)
            print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
            return 1

        manifest.stage_finish(
            "benchmark",
            status="passed",
            summary="Cold-start benchmark completed",
            artifacts=benchmark_artifacts,
            warnings=result.warnings,
        )
        print(f"[orchestrator] benchmark=pass work_dir={work_dir}")
        _print_benchmark_summary(getattr(result, "summary_payload", {}) or {})
        print(f"[orchestrator] manifest={work_dir / 'manifest.json'}")
        return 0

    if args.command == "report":
        manifest.stage_begin("report", "Generate a unified run report from current stage artifacts")
        try:
            result = run_report_stage(
                work_dir=work_dir,
                manifest=manifest,
            )
        except Exception as exc:
            manifest.stage_finish(
                "report",
                status="failed",
                summary="Report stage failed",
                error=str(exc),
            )
            print(f"[orchestrator] report=fail work_dir={work_dir}", file=sys.stderr)
            print(f"  - {exc}", file=sys.stderr)
            print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
            return 1

        manifest.stage_finish(
            "report",
            status="passed",
            summary=f"Unified run report generated ({result.summary_payload.get('overall_status')})",
            artifacts=_report_stage_artifacts(result),
        )
        print(f"[orchestrator] report=pass work_dir={work_dir}")
        print(f"[orchestrator] manifest={work_dir / 'manifest.json'}")
        return 0

    if args.command == "run":
        sample_container_runtime = _resolve_container_runtime(
            args=args,
            manifest=manifest,
            option_key="sample_container_runtime",
        )
        sample_workload = resolve_sample_workload(
            profile_name=args.workload,
            ready_url=args.ready_url,
            ready_exec=args.ready_exec,
            ready_log_regex=args.ready_log_regex,
            ready_shell=args.ready_shell,
            ready_timeout=args.ready_timeout,
            predict_url=args.predict_url,
            predict_body=args.predict_body,
            inject_io_cmd=args.inject_cmd,
            model_host_dir=args.model_host,
            model_dst_dir=args.model_dst,
            model_name=args.model_name,
        )
        try:
            sample_container_runtime = _merge_model_mount_into_runtime(
                sample_container_runtime,
                model_host_dir=str(sample_workload["model_host_dir"] or ""),
                model_mount_hook=sample_workload["effective_model_mount_hook"],
            )
        except RuntimeError as exc:
            return _print_workload_validation_failure(
                stage="run",
                work_dir=work_dir,
                errors=[str(exc)],
            )
        benchmark_container_runtime = _resolve_container_runtime(
            args=args,
            manifest=manifest,
            option_key="benchmark_container_runtime",
            attr_prefix="bench_",
            inherit=sample_container_runtime,
        )
        options["sample_container_runtime"] = sample_container_runtime.summary()
        options["benchmark_container_runtime"] = benchmark_container_runtime.summary()
        manifest.save()
        benchmark_workload = resolve_benchmark_workload(
            profile_name=args.bench_workload,
            timeout_ready=args.bench_timeout,
            ready_url=args.bench_ready_url,
            ready_exec=args.bench_ready_exec,
            ready_log_regex=args.bench_ready_log_regex,
            ready_shell=args.bench_ready_shell,
            predict_url=args.bench_predict_url,
            predict_body=args.bench_predict_body,
            model_host_dir=args.bench_model_host,
            model_dst_dir=args.bench_model_dst,
            model_name=args.bench_model_name,
        )
        sample_workload_errors = validate_sample_workload_resolution(sample_workload, inject_io=args.inject_io)
        if sample_workload_errors:
            return _print_workload_validation_failure(
                stage="run",
                work_dir=work_dir,
                errors=sample_workload_errors,
            )
        if args.benchmark:
            benchmark_workload_errors = validate_benchmark_workload_resolution(
                benchmark_workload,
                mode=args.bench_mode,
            )
            if benchmark_workload_errors:
                return _print_workload_validation_failure(
                    stage="run",
                    work_dir=work_dir,
                    errors=benchmark_workload_errors,
                )
        if manifest.data.get("stages", {}).get("preflight", {}).get("status") != "passed":
            _print_stage_start("preflight")
            manifest.stage_begin("preflight", "Validate fixed product support boundaries")
            result = run_preflight(
                repo_root=repo_root,
                work_dir=work_dir,
                image=args.image,
                startup_mode=args.startup_mode,
                startup_backend_preference=args.startup_backend_preference,
                llm_enabled=_run_uses_llm_api(args),
                runtime_probes=not args.no_runtime_probes,
            )
            if not result.policy_payload["ok"]:
                manifest.stage_finish(
                    "preflight",
                    status="failed",
                    summary="Product support boundary rejected this environment",
                    artifacts={
                        "env_check": str(result.env_check_path),
                        "product_policy": str(result.policy_path),
                    },
                    warnings=result.policy_payload.get("warnings") or [],
                    error="; ".join(result.policy_payload.get("errors") or ["preflight failed"]),
                )
                _print_stage_fail(
                    "preflight",
                    detail=str((result.policy_payload.get("errors") or ["preflight failed"])[0]),
                )
                print(f"[orchestrator] run=fail work_dir={work_dir}", file=sys.stderr)
                for item in result.policy_payload.get("errors") or []:
                    print(f"  - {item}", file=sys.stderr)
                print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
                return 1
            manifest.stage_finish(
                "preflight",
                status="passed",
                summary="Product support boundary accepted",
                artifacts={
                    "env_check": str(result.env_check_path),
                    "product_policy": str(result.policy_path),
                },
                warnings=result.policy_payload.get("warnings") or [],
            )
            _print_stage_pass("preflight")

        if manifest.data.get("stages", {}).get("sample", {}).get("status") != "passed":
            _print_stage_start("sample")
            manifest.stage_begin("sample", "Launch container and capture raw events via file sink")
            try:
                result = run_sample_stage(
                    repo_root=repo_root,
                    work_dir=work_dir,
                    image=args.image,
                    run_id=run_id,
                    startup_mode=args.startup_mode,
                    startup_backend_preference=args.startup_backend_preference,
                    duration=args.duration,
                    prom_port_start=args.prom_port,
                    mmap_window=args.mmap_window,
                    cg_refresh=args.cg_refresh,
                    ready_url=sample_workload["ready_url"],
                    ready_timeout=int(sample_workload["ready_timeout"]),
                    workload_profile=str(sample_workload["profile"]),
                    self_starting_service=bool(sample_workload.get("self_starting_service", False)),
                    workload_profile_capabilities=tuple(sample_workload["profile_capabilities"]),
                    workload_effective_capabilities=tuple(sample_workload["effective_capabilities"]),
                    workload_profile_hooks=sample_workload["profile_hooks"],
                    workload_effective_hooks=sample_workload["effective_hooks"],
                    ready_hook=sample_workload["effective_ready_hook"],
                    predict_hook=sample_workload["effective_predict_hook"],
                    inject_hook=sample_workload["effective_inject_hook"],
                    container_runtime=sample_container_runtime,
                    inject_io=args.inject_io,
                    predict_url=sample_workload["predict_url"],
                    predict_body=sample_workload["predict_body"],
                    inject_io_cmd=sample_workload["inject_io_cmd"],
                )
            except Exception as exc:
                manifest.stage_finish(
                    "sample",
                    status="failed",
                    summary="Container launch or sampling failed",
                    artifacts=_sample_stage_artifacts_from_work_dir(work_dir),
                    error=str(exc),
                )
                _print_stage_fail("sample", detail=str(exc))
                print(f"[orchestrator] run=fail work_dir={work_dir}", file=sys.stderr)
                print(f"  - {exc}", file=sys.stderr)
                print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
                return 1

            manifest.stage_finish(
                "sample",
                status="passed",
                summary="Container launched and raw sampling artifacts captured",
                artifacts={
                    "runtime_state": str(result.runtime_state_path),
                    "raw_events": str(result.raw_events_path),
                    "sampler_log": str(result.sampler_log_path),
                    "sampler_metrics": str(result.sampler_metrics_path),
                },
            )
            _print_stage_pass("sample")

        if manifest.data.get("stages", {}).get("export_enrich", {}).get("status") != "passed":
            _print_stage_start("export_enrich")
            manifest.stage_begin("export_enrich", "Export raw file-sink events and enrich OPEN/MMAP metadata")
            try:
                result = run_export_enrich_stage(
                    repo_root=repo_root,
                    work_dir=work_dir,
                )
            except Exception as exc:
                manifest.stage_finish(
                    "export_enrich",
                    status="failed",
                    summary="Event export/enrich stage failed",
                    error=str(exc),
                )
                _print_stage_fail("export_enrich", detail=str(exc))
                print(f"[orchestrator] run=fail work_dir={work_dir}", file=sys.stderr)
                print(f"  - {exc}", file=sys.stderr)
                print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
                return 1

            manifest.stage_finish(
                "export_enrich",
                status="passed",
                summary="Raw events exported and MMAP/Open metadata enriched",
                artifacts={
                    "events": str(result.events_path),
                    "export_events_stats": str(result.export_stats_path),
                    "cwd_snapshot": str(result.cwd_snapshot_path),
                    "maps_paths": str(result.maps_paths_path),
                    "mapfiles_devino": str(result.mapfiles_devino_path),
                    "devino_dict": str(result.devino_dict_path),
                    "events_enriched": str(result.events_enriched_path),
                    "export_enrich_log": str(result.stage_log_path),
                },
            )
            _print_stage_pass("export_enrich")

        if manifest.data.get("stages", {}).get("analyze", {}).get("status") != "passed":
            _print_stage_start("analyze")
            manifest.stage_begin("analyze", "Aggregate candidates, enforce quality gate, and build deterministic hints")
            try:
                result = run_analyze_stage(
                    repo_root=repo_root,
                    work_dir=work_dir,
                    min_events=args.min_events,
                    max_mmap_maps_ratio=args.max_mmap_maps_ratio,
                    size_budget_percent=args.size_budget_percent,
                    size_budget_mb=args.size_budget_mb,
                    min_hits=args.min_hits,
                    p0_max=args.p0_max,
                    early_onehit_ratio=args.early_onehit_ratio,
                    early_onehit_max=args.early_onehit_max,
                    early_onehit_max_size_kb=args.early_onehit_max_size_kb,
                    selector_mode=args.selector_mode,
                    allow_low_fidelity=args.allow_low_fidelity,
                )
            except Exception as exc:
                manifest.stage_finish(
                    "analyze",
                    status="failed",
                    summary="Aggregate/quality/select stage failed",
                    error=str(exc),
                )
                _print_stage_fail("analyze", detail=str(exc))
                print(f"[orchestrator] run=fail work_dir={work_dir}", file=sys.stderr)
                print(f"  - {exc}", file=sys.stderr)
                print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
                return 1

            summary = "Deterministic hints produced from exported events"
            if str(result.quality_payload.get("quality_status") or "") not in {"", "good"}:
                summary = (
                    "Deterministic hints produced with advisory quality warnings "
                    f"({result.quality_payload.get('quality_status')})"
                )
            analyze_artifacts = {
                "quality_gate": str(result.quality_gate_path),
                "candidates": str(result.candidates_path),
                "aggregate_stats": str(result.aggregate_stats_path),
                "ranked": str(result.ranked_path),
                "estargz_list": str(result.estargz_list_path),
                "selector_stats_default": str(result.selector_stats_path),
                "selector_stats_hints": str(result.selector_stats_hint_path),
                "hints": str(result.hints_path),
                "analyze_log": str(result.stage_log_path),
            }
            if result.image_toc_path is not None:
                analyze_artifacts["image_toc"] = str(result.image_toc_path)
            if result.toc_shadow_path is not None:
                analyze_artifacts["toc_shadow"] = str(result.toc_shadow_path)
            if result.chunk_selector_shadow_path is not None:
                analyze_artifacts["chunk_selector_shadow"] = str(result.chunk_selector_shadow_path)
            if result.chunk_shadow_ranked_path is not None:
                analyze_artifacts["ranked_chunk_shadow"] = str(result.chunk_shadow_ranked_path)
            if result.chunk_shadow_estargz_list_path is not None:
                analyze_artifacts["estargz_list_chunk_shadow"] = str(result.chunk_shadow_estargz_list_path)
            if result.chunk_shadow_hints_path is not None:
                analyze_artifacts["hints_chunk_shadow"] = str(result.chunk_shadow_hints_path)
            if result.chunk_shadow_selector_stats_path is not None:
                analyze_artifacts["selector_stats_chunk_shadow"] = str(result.chunk_shadow_selector_stats_path)
            manifest.stage_finish(
                "analyze",
                status="passed",
                summary=summary,
                artifacts=analyze_artifacts,
                warnings=result.warnings + list(result.quality_payload.get("warnings") or []),
            )
            _print_stage_pass("analyze")

        llm_stage_status = manifest.data.get("stages", {}).get("llm_optimize", {}).get("status")
        downstream_already_materialized = any(
            manifest.data.get("stages", {}).get(stage_name, {}).get("status") == "passed"
            for stage_name in ("apply", "benchmark", "report")
        )
        should_run_llm_stage = args.run_mode in {"llm-auto", "llm-manual"}
        if should_run_llm_stage and llm_stage_status != "passed" and not downstream_already_materialized:
            _print_stage_start("llm_optimize")
            manifest.stage_begin("llm_optimize", "Build prompt.txt and apply grounded LLM priority optimization")
            try:
                result = run_llm_optimize_stage(
                    repo_root=repo_root,
                    work_dir=work_dir,
                    size_budget_percent=args.size_budget_percent,
                    size_budget_mb=args.size_budget_mb,
                    min_hits=args.min_hits,
                    p0_max=args.p0_max,
                    early_onehit_ratio=args.early_onehit_ratio,
                    early_onehit_max=args.early_onehit_max,
                    early_onehit_max_size_kb=args.early_onehit_max_size_kb,
                    llm_provider=args.llm_provider,
                    llm_api_key_env=args.llm_api_key_env,
                    llm_api_base=args.llm_api_base,
                    llm_model=args.llm_model,
                    llm_reasoning_effort=args.llm_reasoning_effort,
                    llm_timeout_sec=args.llm_timeout_sec,
                    llm_topn=args.llm_topn,
                    manual_response_file=args.manual_response_file,
                    force_manual_export=args.run_mode == "llm-manual",
                    requested_mode=args.run_mode,
                )
            except Exception as exc:
                manifest.stage_finish(
                    "llm_optimize",
                    status="failed",
                    summary="LLM optimization stage failed",
                    error=str(exc),
                )
                _print_stage_fail("llm_optimize", detail=str(exc))
                print(f"[orchestrator] run=fail work_dir={work_dir}", file=sys.stderr)
                print(f"  - {exc}", file=sys.stderr)
                print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
                return 1

            llm_artifacts = _llm_stage_artifacts(result)
            if args.run_mode == "llm-auto" and result.mode == "manual_export":
                manifest.stage_finish(
                    "llm_optimize",
                    status="failed",
                    summary="LLM auto mode unexpectedly downgraded to manual export",
                    artifacts=llm_artifacts,
                    warnings=result.warnings,
                    error="--mode llm-auto must not silently switch to manual export",
                )
                _print_stage_fail(
                    "llm_optimize",
                    detail="--mode llm-auto must not silently switch to manual export",
                )
                print(f"[orchestrator] run=fail work_dir={work_dir}", file=sys.stderr)
                print("  - --mode llm-auto must not silently switch to manual export", file=sys.stderr)
                print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
                return 1
            manual_pause_requested = bool(
                args.run_mode == "llm-manual" and not args.manual_response_file and result.mode == "manual_export"
            )
            if manual_pause_requested:
                resume_script_path = _write_manual_web_resume_script(
                    repo_root=repo_root,
                    work_dir=work_dir,
                    argv=raw_argv,
                )
                llm_artifacts["resume_manual_web_script"] = str(resume_script_path)
                manifest.stage_finish(
                    "llm_optimize",
                    status="paused",
                    summary="Prompt package exported for manual web-LLM; resume script generated for remaining stages",
                    artifacts=llm_artifacts,
                    warnings=result.warnings,
                    pause_reason=result.pause_reason,
                    resume_inputs=_llm_pause_resume_inputs(result),
                    resume_command=f"sudo -E bash {resume_script_path}",
                )
                _print_manual_pause_summary(
                    work_dir=work_dir,
                    web_input_path=result.web_input_path,
                    manual_instructions_path=result.manual_instructions_path,
                    manual_response_path=result.manual_response_default_path,
                    resume_command=f"sudo -E bash {resume_script_path}",
                )
                print(f"[orchestrator] manifest={work_dir / 'manifest.json'}")
                return 0

            manifest.stage_finish(
                "llm_optimize",
                status="passed",
                summary=_llm_stage_summary(result),
                artifacts=llm_artifacts,
                warnings=result.warnings,
            )
            _print_stage_pass("llm_optimize")
            _print_llm_result_summary(result)

        if args.apply and manifest.data.get("stages", {}).get("apply", {}).get("status") != "passed":
            _print_stage_start("apply")
            manifest.stage_begin("apply", "Apply hints.yaml/estargz.list to produce an optimized image")
            try:
                result = run_apply_stage(
                    repo_root=repo_root,
                    work_dir=work_dir,
                    src_image=args.image,
                    selector_mode=args.selector_mode,
                    dst_image=args.dst_image,
                    namespace=args.namespace,
                    dry_run=args.apply_dry_run,
                    runtime_probes=not args.no_runtime_probes,
                )
            except KeyboardInterrupt:
                manifest.stage_finish(
                    "apply",
                    status="cancelled",
                    summary="Apply stage cancelled by user",
                    error="interrupted by user",
                )
                _print_stage_fail("apply", detail="interrupted by user")
                print(f"[orchestrator] run=cancelled work_dir={work_dir}", file=sys.stderr)
                print("  - apply interrupted by user", file=sys.stderr)
                print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
                return 130
            except Exception as exc:
                manifest.stage_finish(
                    "apply",
                    status="failed",
                    summary="Apply stage failed",
                    error=str(exc),
                )
                _print_stage_fail("apply", detail=str(exc))
                print(f"[orchestrator] run=fail work_dir={work_dir}", file=sys.stderr)
                print(f"  - {exc}", file=sys.stderr)
                print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
                return 1

            summary = "Optimized image created from current hints"
            if args.selector_mode != "file":
                summary = f"Experimental {args.selector_mode} optimized image created from current hints"
            if args.apply_dry_run:
                summary = "Apply dry-run completed; optimized image import was not executed"
            if args.selector_mode == "file":
                artifacts = {
                    "env_check_apply": str(result.env_check_path),
                    "apply_policy": str(result.policy_path),
                    "apply_log": str(result.stage_log_path),
                    "apply_result": str(result.apply_result_path),
                }
                if result.optimized_image_path is not None:
                    artifacts["optimized_image"] = str(result.optimized_image_path)
            else:
                artifacts = {
                    "env_check_apply_chunk_shadow": str(result.env_check_path),
                    "apply_policy_chunk_shadow": str(result.policy_path),
                    "apply_log_chunk_shadow": str(result.stage_log_path),
                    "apply_result_chunk_shadow": str(result.apply_result_path),
                }
                if result.optimized_image_path is not None:
                    artifacts["optimized_image_chunk_shadow"] = str(result.optimized_image_path)
            manifest.stage_finish(
                "apply",
                status="passed",
                summary=summary,
                artifacts=artifacts,
                warnings=result.warnings,
            )
            _print_stage_pass("apply")
            _print_apply_result_summary(result)

        if args.benchmark and manifest.data.get("stages", {}).get("benchmark", {}).get("status") != "passed":
            opt_image = optimized_image_from_manifest(
                manifest,
                work_dir=work_dir,
                cli_image=args.bench_opt_image,
                selector_mode=args.selector_mode,
            )
            if not opt_image:
                print(f"[orchestrator] run=fail work_dir={work_dir}", file=sys.stderr)
                print("  - benchmark requested but optimized image is missing; run --apply or pass --bench-opt-image", file=sys.stderr)
                print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
                return 1

            _cleanup_sample_container_before_benchmark(repo_root=repo_root, work_dir=work_dir)
            _print_stage_start("benchmark")
            manifest.stage_begin("benchmark", "Benchmark baseline and optimized images under strict cold-start policy")
            try:
                result = run_benchmark_stage(
                    repo_root=repo_root,
                    work_dir=work_dir,
                    base_image=args.image,
                    opt_image=opt_image,
                    unopt_image=str(getattr(args, "bench_unopt_image", "") or ""),
                    selector_mode=args.selector_mode,
                    namespace=args.namespace,
                    runs=args.bench_runs,
                    timeout_ready=int(benchmark_workload["timeout_ready"]),
                    snapshotter=args.bench_snapshotter,
                    base_snapshotter=args.bench_base_snapshotter,
                    opt_snapshotter=args.bench_opt_snapshotter,
                    baseline_stargz=args.bench_baseline_stargz,
                    comparison_mode=args.bench_comparison_mode,
                    mode=args.bench_mode,
                    ready_url=str(benchmark_workload["ready_url"] or ""),
                    debug_state_url=str(benchmark_workload.get("debug_state_url") or ""),
                    predict_url=str(benchmark_workload["predict_url"] or ""),
                    predict_body=str(benchmark_workload["predict_body"] or ""),
                    model_host_dir=str(benchmark_workload["model_host_dir"] or ""),
                    model_dst_dir=str(benchmark_workload["model_dst_dir"] or ""),
                    model_name=str(benchmark_workload["model_name"] or ""),
                    platform=args.bench_platform,
                    hard_cold=not args.bench_soft_cold,
                    force_reset=args.bench_force_reset,
                    force_kill_port=args.bench_force_kill_port,
                    allow_busy_port=args.bench_allow_busy_port,
                    reconcile_dirs=args.bench_reconcile_dirs,
                    drop_page_cache=not args.bench_no_drop_cache,
                    clear_stargz_cache=not args.bench_no_clear_stargz,
                    shuffle_order=args.bench_shuffle,
                    log_dir=args.bench_log_dir,
                    runtime_probes=not args.no_runtime_probes,
                    pull_policy=args.bench_pull_policy,
                    drop_local_image_ref=args.bench_drop_local_image_ref,
                    cold_mode=args.bench_cold_mode,
                    workload_profile=str(benchmark_workload["profile"]),
                    self_starting_service=bool(benchmark_workload.get("self_starting_service", False)),
                    workload_profile_capabilities=tuple(benchmark_workload["profile_capabilities"]),
                    workload_effective_capabilities=tuple(benchmark_workload["effective_capabilities"]),
                    workload_profile_hooks=benchmark_workload["profile_hooks"],
                    workload_effective_hooks=benchmark_workload["effective_hooks"],
                    ready_hook=benchmark_workload["effective_ready_hook"],
                    predict_hook=benchmark_workload["effective_predict_hook"],
                    model_mount_hook=benchmark_workload["effective_model_mount_hook"],
                    container_runtime=benchmark_container_runtime,
                )
            except KeyboardInterrupt:
                manifest.stage_finish(
                    "benchmark",
                    status="cancelled",
                    summary="Benchmark stage cancelled by user",
                    error="interrupted by user",
                )
                _print_stage_fail("benchmark", detail="interrupted by user")
                print(f"[orchestrator] run=cancelled work_dir={work_dir}", file=sys.stderr)
                print("  - benchmark interrupted by user", file=sys.stderr)
                print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
                return 130
            except Exception as exc:
                manifest.stage_finish(
                    "benchmark",
                    status="failed",
                    summary="Benchmark stage failed",
                    error=str(exc),
                )
                _print_stage_fail("benchmark", detail=str(exc))
                print(f"[orchestrator] run=fail work_dir={work_dir}", file=sys.stderr)
                print(f"  - {exc}", file=sys.stderr)
                print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
                return 1

            benchmark_artifacts = {
                "env_check_benchmark_base": str(result.env_check_base_path),
                "env_check_benchmark_opt": str(result.env_check_opt_path),
                "benchmark_policy": str(result.policy_path),
                "benchmark_log": str(result.stage_log_path),
                "benchmark_csv": str(result.csv_path),
                "benchmark_log_dir": str(result.log_dir),
                "benchmark_result": str(result.result_path),
            }
            if not result.valid:
                manifest.stage_finish(
                    "benchmark",
                    status="failed",
                    summary="Benchmark stage produced no valid comparison",
                    artifacts=benchmark_artifacts,
                    warnings=result.warnings,
                    error="; ".join(result.invalid_reasons),
                )
                _print_stage_fail(
                    "benchmark",
                    detail=str((result.invalid_reasons or ["benchmark produced no valid comparison"])[0]),
                )
                print(f"[orchestrator] run=fail work_dir={work_dir}", file=sys.stderr)
                for reason in result.invalid_reasons:
                    print(f"  - {reason}", file=sys.stderr)
                print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
                return 1

            manifest.stage_finish(
                "benchmark",
                status="passed",
                summary="Cold-start benchmark completed",
                artifacts=benchmark_artifacts,
                warnings=result.warnings,
            )
            _print_stage_pass("benchmark")
            _print_benchmark_summary(getattr(result, "summary_payload", {}) or {})

        _print_stage_start("report")
        manifest.stage_begin("report", "Generate a unified run report from current stage artifacts")
        try:
            result = run_report_stage(
                work_dir=work_dir,
                manifest=manifest,
            )
        except Exception as exc:
            manifest.stage_finish(
                "report",
                status="failed",
                summary="Report stage failed",
                error=str(exc),
            )
            _print_stage_fail("report", detail=str(exc))
            print(f"[orchestrator] run=fail work_dir={work_dir}", file=sys.stderr)
            print(f"  - {exc}", file=sys.stderr)
            print(f"[orchestrator] manifest={work_dir / 'manifest.json'}", file=sys.stderr)
            return 1

        manifest.stage_finish(
            "report",
            status="passed",
            summary=f"Unified run report generated ({result.summary_payload.get('overall_status')})",
            artifacts=_report_stage_artifacts(result),
        )
        _print_stage_pass("report")

        print(f"[orchestrator] run=pass work_dir={work_dir}")
        _print_run_result_summary(result.summary_payload, manifest_path=work_dir / "manifest.json")
        return 0

    raise AssertionError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
