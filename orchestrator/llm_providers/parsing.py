from __future__ import annotations

import json
import re

from orchestrator.llm_providers.types import PRIORITY_KEYS, StructuredOutput

try:
    import yaml  # type: ignore

    _HAVE_PYYAML = True
except ImportError:  # pragma: no cover - optional dependency
    _HAVE_PYYAML = False


def _normalize_text(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    fenced = re.findall(r"```(?:json|yaml|yml)?\s*(.*?)```", value, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        value = fenced[0].strip()
    return value


def _strip_optional_quotes(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[:1] == text[-1:] and text[:1] in {"'", '"'}:
        return text[1:-1]
    return text


def _tiny_yaml_load_structured_output(stream: str) -> dict[str, object]:
    data: dict[str, object] = {}
    current_key: str | None = None
    for line_number, raw_line in enumerate(stream.splitlines(), start=1):
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if raw_line[:1].isspace():
            if current_key is None:
                raise ValueError(f"line {line_number} starts a list item before any priority key")
            match = re.match(r"^\s*-\s+(.*)$", raw_line)
            if not match:
                raise ValueError(f"line {line_number} uses unsupported nested YAML; use simple path lists only")
            value = _strip_optional_quotes(match.group(1))
            if not value:
                raise ValueError(f"line {line_number} contains an empty path item")
            data.setdefault(current_key, [])
            if not isinstance(data[current_key], list):
                raise ValueError(f"line {line_number} cannot append list items to {current_key!r}")
            data[current_key].append(value)
            continue
        if ":" not in line:
            raise ValueError(f"line {line_number} must use KEY: value syntax")
        key, raw_value = line.split(":", 1)
        normalized_key = key.strip()
        if not normalized_key:
            raise ValueError(f"line {line_number} is missing a key name")
        if normalized_key in data:
            raise ValueError(f"line {line_number} redefines key {normalized_key!r}")
        value = raw_value.strip()
        if not value:
            data[normalized_key] = []
            current_key = normalized_key
            continue
        if value == "[]":
            data[normalized_key] = []
            current_key = None
            continue
        raise ValueError(
            f"line {line_number} uses unsupported inline YAML for {normalized_key!r}; "
            f"use `{normalized_key}: []` or an indented `- /absolute/path` list"
        )
    return data


def validate_structured_output(payload: object) -> StructuredOutput:
    if not isinstance(payload, dict):
        raise ValueError("top-level value must be a mapping/object")
    raw_mapping = {str(key): value for key, value in payload.items()}
    missing_keys = [key for key in PRIORITY_KEYS if key not in raw_mapping]
    if missing_keys:
        raise ValueError(f"missing required priorities: {', '.join(missing_keys)}")
    extra_keys = sorted(key for key in raw_mapping if key not in PRIORITY_KEYS)
    if extra_keys:
        raise ValueError(f"unsupported top-level keys: {', '.join(extra_keys)}")
    normalized_payload: StructuredOutput = {}
    for priority in PRIORITY_KEYS:
        value = raw_mapping.get(priority)
        if not isinstance(value, list):
            raise ValueError(f"{priority} must be a list of absolute file paths")
        cleaned_paths: list[str] = []
        for index, entry in enumerate(value, start=1):
            if not isinstance(entry, str):
                raise ValueError(f"{priority}[{index}] must be a string path")
            normalized_entry = entry.strip()
            if not normalized_entry:
                raise ValueError(f"{priority}[{index}] must not be empty")
            if not normalized_entry.startswith("/"):
                raise ValueError(f"{priority}[{index}] must be an absolute path, got {normalized_entry!r}")
            cleaned_paths.append(normalized_entry)
        normalized_payload[priority] = cleaned_paths
    return normalized_payload


def load_structured_output_from_json_text(text: str) -> StructuredOutput:
    normalized = _normalize_text(text)
    if not normalized:
        raise ValueError("response is empty after removing optional markdown fences")
    try:
        payload = json.loads(normalized)
    except Exception as exc:
        raise ValueError(f"JSON parse failed: {exc}") from exc
    return validate_structured_output(payload)


def load_structured_output_from_yaml_text(text: str) -> StructuredOutput:
    normalized = _normalize_text(text)
    if not normalized:
        raise ValueError("response is empty after removing optional markdown fences")
    try:
        if _HAVE_PYYAML:
            payload = yaml.safe_load(normalized)  # type: ignore[name-defined]
        else:
            payload = _tiny_yaml_load_structured_output(normalized)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"YAML parse failed: {exc}") from exc
    return validate_structured_output(payload)


def structured_output_to_yaml(payload: StructuredOutput) -> str:
    normalized = validate_structured_output(payload)
    lines: list[str] = []
    for priority in PRIORITY_KEYS:
        paths = list(normalized.get(priority) or [])
        if not paths:
            lines.append(f"{priority}: []")
            continue
        lines.append(f"{priority}:")
        for path in paths:
            lines.append(f"  - {path}")
    return "\n".join(lines) + "\n"
