from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from orchestrator.llm import (
    build_provider_config,
    default_schema_mode_for_provider,
    describe_auth_failure,
    describe_missing_api_key,
    provider_requires_api_key,
    resolve_llm_provider_config,
)
from orchestrator.llm_providers.parsing import (
    load_structured_output_from_json_text,
    load_structured_output_from_yaml_text,
    structured_output_to_yaml,
    validate_structured_output,
)
from orchestrator.llm_providers.router import ProviderRouter
from orchestrator.llm_providers.types import LLMRequest, LLMResponse


MANUAL_SYSTEM_PROMPT = (
    "You are a precise system for selecting container image eStargz prefetch priorities. "
    "Output exactly one strict JSON object with keys P0, P1, P2, P3 and arrays of absolute file paths. "
    'The only valid top-level shape is {"P0":[],"P1":[],"P2":[],"P3":[]}. '
    "Do not output Markdown. Do not output code fences. Do not output explanations. "
    "Do not add extra keys. Every emitted path must come from the provided candidate list."
)
AUTO_SYSTEM_PROMPT = (
    "You are a precise system that outputs only a strict JSON object with keys P0, P1, P2, P3. "
    'Return exactly {"P0":[],"P1":[],"P2":[],"P3":[]} with lists of absolute file paths. '
    "This is a path-selection task over the candidate set in the user prompt. "
    "When the candidate set is non-empty, choose a small conservative non-empty subset of the strongest paths instead of returning all-empty lists. "
    "Only return all-empty lists when the candidate set itself is empty. "
    "Output exactly one JSON object. Do not output Markdown. Do not output code fences. "
    "Do not output explanations. No extra keys. Every emitted path must come from the provided candidate list."
)
MANUAL_RESPONSE_PRIORITIES = ("P0", "P1", "P2", "P3")
MANUAL_RESPONSE_CONTRACT = (
    "strict JSON object with P0/P1/P2/P3 absolute-path arrays only; "
    "legacy YAML with the same four priorities is still accepted for compatibility"
)
AUTO_RESPONSE_P0_MAX = 8
AUTO_RESPONSE_P1_MAX = 16


@dataclass
class LlmOptimizeResult:
    prompt_path: Path
    web_input_path: Path
    body_path: Path
    response_path: Path
    hints_raw_path: Path
    preferred_paths_path: Path
    grounding_audit_path: Path
    hints_path: Path
    estargz_list_path: Path
    selector_stats_hint_path: Path
    manual_instructions_path: Path
    import_script_path: Path
    result_path: Path
    stage_log_path: Path
    warnings: list[str]
    requested_mode: str
    mode: str
    mode_result: str
    llm_applied: bool
    final_hints_source: str
    resume_required: bool
    pause_reason: str | None
    manual_response_default_path: Path
    manual_response_format: str
    resume_command: str | None
    fallback_kind: str | None
    fallback_reason: str | None


def _load_json(path: Path) -> dict:
    if not path.exists():
        raise RuntimeError(f"required JSON is missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _ensure_file(path: Path, label: str) -> None:
    if not path.exists():
        raise RuntimeError(f"{label} is missing: {path}")


def _run_tool(argv: list[str], *, repo_root: Path, stage_log_path: Path) -> subprocess.CompletedProcess[str]:
    with stage_log_path.open("a", encoding="utf-8") as log_fh:
        proc = subprocess.run(
            argv,
            cwd=repo_root,
            text=True,
            stdout=log_fh,
            stderr=log_fh,
            check=False,
        )
    return proc


def _budget_desc(size_budget_percent: float, size_budget_mb: int) -> str:
    if int(size_budget_mb or 0) > 0:
        return f"{int(size_budget_mb)} MB"
    return f"{float(size_budget_percent):g}% 的候选体积"


def _normalize_llm_text(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    fenced = re.findall(r"```(?:json|yaml|yml)?\s*(.*?)```", value, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        value = fenced[0].strip()
    return value + ("\n" if value else "")


def _manual_response_template() -> str:
    return '{\n  "P0": ["/absolute/path/from/candidates"],\n  "P1": [],\n  "P2": [],\n  "P3": []\n}\n'


def _strip_optional_quotes(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[:1] == text[-1:] and text[:1] in {"'", '"'}:
        return text[1:-1]
    return text


def _tiny_yaml_load_manual_response(stream: str) -> dict[str, object]:
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


def _load_manual_response_payload(text: str) -> object:
    normalized = _normalize_llm_text(text)
    last_error: ValueError | None = None
    for loader in (load_structured_output_from_json_text, load_structured_output_from_yaml_text):
        try:
            return loader(normalized)
        except ValueError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise ValueError("response is empty after normalization")


def _dump_manual_response_yaml(payload: dict[str, list[str]]) -> str:
    return structured_output_to_yaml(validate_structured_output(payload))


def _filter_grounded_manual_payload(
    payload: dict[str, list[str]],
    *,
    accepted_paths: set[str],
) -> dict[str, list[str]]:
    filtered: dict[str, list[str]] = {}
    seen: set[str] = set()
    for priority in MANUAL_RESPONSE_PRIORITIES:
        kept: list[str] = []
        for path in list(payload.get(priority) or []):
            if path not in accepted_paths or path in seen:
                continue
            kept.append(path)
            seen.add(path)
        filtered[priority] = kept
    return filtered


def _materialize_grounded_manual_outputs(
    *,
    grounded_input_text: str,
    preferred_paths_path: Path,
    hints_path: Path,
    estargz_list_path: Path,
    selector_stats_path: Path,
    selector_stats_hint_path: Path,
    llm_hints_path: Path,
    llm_estargz_list_path: Path,
    llm_selector_stats_path: Path,
    accepted_total: int,
) -> None:
    payload = _validate_response_payload_against_contract(grounded_input_text)
    accepted_paths = {
        line.strip()
        for line in preferred_paths_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    filtered_payload = _filter_grounded_manual_payload(payload, accepted_paths=accepted_paths)
    hints_yaml = _dump_manual_response_yaml(filtered_payload)
    estargz_lines = [
        path
        for priority in MANUAL_RESPONSE_PRIORITIES
        for path in list(filtered_payload.get(priority) or [])
    ]
    selector_stats_hint = _load_json(selector_stats_path)
    selector_stats_hint["selector_source"] = "grounded_llm_manual"
    selector_stats_hint["accepted_total"] = int(accepted_total)
    selector_stats_hint["selected_total"] = len(estargz_lines)

    llm_hints_path.write_text(hints_yaml, encoding="utf-8")
    llm_estargz_list_path.write_text(
        "".join(f"{path}\n" for path in estargz_lines),
        encoding="utf-8",
    )
    llm_selector_stats_path.write_text(
        json.dumps(selector_stats_hint, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    shutil.copyfile(llm_hints_path, hints_path)
    shutil.copyfile(llm_estargz_list_path, estargz_list_path)
    shutil.copyfile(llm_selector_stats_path, selector_stats_hint_path)


def _manual_response_error(*, path: Path, detail: str, manual_instructions_path: Path) -> RuntimeError:
    return RuntimeError(
        f"manual LLM response is invalid: {detail}; "
        f"expected {MANUAL_RESPONSE_CONTRACT}; "
        f"response={path}; see {manual_instructions_path}"
    )


def _validate_response_text_against_contract(text: str) -> str:
    return _dump_manual_response_yaml(_validate_response_payload_against_contract(text))


def _validate_response_payload_against_contract(text: str) -> dict[str, list[str]]:
    payload = _load_manual_response_payload(text)
    return validate_structured_output(payload)


def _effective_auto_p0_cap(*, p0_max: int) -> int:
    configured = int(p0_max or 0)
    if configured <= 0:
        return AUTO_RESPONSE_P0_MAX
    return min(configured, AUTO_RESPONSE_P0_MAX)


def _normalize_auto_response_payload(
    payload: dict[str, list[str]],
    *,
    p0_max: int,
) -> tuple[dict[str, list[str]], dict[str, object]]:
    original_counts = {priority: len(list(payload.get(priority) or [])) for priority in MANUAL_RESPONSE_PRIORITIES}
    deduped_payload: dict[str, list[str]] = {}
    seen: set[str] = set()
    duplicates_removed = 0

    for priority in MANUAL_RESPONSE_PRIORITIES:
        kept: list[str] = []
        for path in list(payload.get(priority) or []):
            if path in seen:
                duplicates_removed += 1
                continue
            seen.add(path)
            kept.append(path)
        deduped_payload[priority] = kept

    p0_cap = _effective_auto_p0_cap(p0_max=p0_max)
    p1_cap = AUTO_RESPONSE_P1_MAX
    p0_before = len(deduped_payload["P0"])
    p1_before = len(deduped_payload["P1"])
    p0_truncated = p0_before > p0_cap
    p1_truncated = p1_before > p1_cap
    deduped_payload["P0"] = deduped_payload["P0"][:p0_cap]
    deduped_payload["P1"] = deduped_payload["P1"][:p1_cap]

    audit = {
        "cross_priority_duplicates_removed": duplicates_removed,
        "p0_truncated": p0_truncated,
        "p1_truncated": p1_truncated,
        "p0_limit": p0_cap,
        "p1_limit": p1_cap,
        "final_counts": {
            priority: len(list(deduped_payload.get(priority) or []))
            for priority in MANUAL_RESPONSE_PRIORITIES
        },
    }
    return deduped_payload, audit


def _format_auto_normalization_warning(audit: dict[str, object]) -> str | None:
    duplicates_removed = int(audit.get("cross_priority_duplicates_removed") or 0)
    p0_truncated = bool(audit.get("p0_truncated"))
    p1_truncated = bool(audit.get("p1_truncated"))
    if duplicates_removed <= 0 and not p0_truncated and not p1_truncated:
        return None
    final_counts = audit.get("final_counts") or {}
    summary_parts = [f"removed {duplicates_removed} cross-priority duplicates"]
    if p0_truncated:
        summary_parts.append(
            f"P0 truncated to {int(audit.get('p0_limit') or 0)}"
        )
    if p1_truncated:
        summary_parts.append(
            f"P1 truncated to {int(audit.get('p1_limit') or 0)}"
        )
    summary_parts.append(
        "final counts "
        + ", ".join(
            f"{priority}={int((final_counts or {}).get(priority) or 0)}"
            for priority in MANUAL_RESPONSE_PRIORITIES
        )
    )
    return "llm-auto response normalized before grounding: " + "; ".join(summary_parts)


def _validate_manual_response_text(
    *,
    path: Path,
    text: str,
    manual_instructions_path: Path,
) -> str:
    try:
        return _validate_response_text_against_contract(text)
    except ValueError as exc:
        raise _manual_response_error(
            path=path,
            detail=str(exc),
            manual_instructions_path=manual_instructions_path,
        ) from exc


def _format_fallback_warning(reason: str) -> str:
    return f"{reason}; kept deterministic hints"


def _candidate_count(payload: object) -> int:
    if not isinstance(payload, list):
        return 0
    return len(payload)


def _structured_output_total_paths(payload: dict[str, list[str]]) -> int:
    return sum(len(list(payload.get(priority) or [])) for priority in MANUAL_RESPONSE_PRIORITIES)


def _prompt_contract_for_mode(requested_mode: str) -> str:
    del requested_mode
    return "json"


def _system_prompt_for_mode(requested_mode: str) -> str:
    return MANUAL_SYSTEM_PROMPT if requested_mode == "llm-manual" else AUTO_SYSTEM_PROMPT


def _write_manual_instructions(
    *,
    work_dir: Path,
    prompt_path: Path,
    web_input_path: Path,
    manual_instructions_path: Path,
    import_script_path: Path,
    manual_response_file: Path | None,
) -> None:
    suggested_response = manual_response_file or (work_dir / "hints_raw.manual.yaml")
    template = _manual_response_template()
    content = (
        "# Web LLM Manual Flow\n\n"
        "## 1. 提交给网页端大模型\n\n"
        "优先打开并复制 `web_llm_ready_to_paste.txt` 的全部内容，整段粘贴到网页端大模型。\n"
        "如果网页端支持单独的 system prompt，也可以改用 `prompt.txt` + 下面这段 system 指令：\n\n"
        "```text\n"
        f"{MANUAL_SYSTEM_PROMPT}\n"
        "```\n\n"
        "## 2. 手工 response 文件必须长什么样\n\n"
        f"- 只接受：{MANUAL_RESPONSE_CONTRACT}\n"
        "- 推荐直接保存严格 JSON；旧 YAML 仍可导入，但新的网页端流程应优先按 JSON 保存。\n"
        "- 不要附加解释、标题、Markdown 正文、代码块、额外字段、目录路径、通配符或相对路径。\n"
        "- 如果某个优先级没有路径，必须写成空数组 `[]`。\n\n"
        "推荐直接保存成下面这个结构：\n\n"
        "```json\n"
        f"{template}"
        "```\n\n"
        f"将网页端返回内容保存到 `{suggested_response}`。\n\n"
        "## 3. 导回并恢复\n\n"
        "优先运行下面这个导回脚本：\n\n"
        "```bash\n"
        f"bash {import_script_path}\n"
        "```\n\n"
        "如果存在 `resume_manual_web_llm.sh`，并且你是从 `run --mode llm-manual` 进入本流程，优先执行它，它会继续后续 apply/benchmark/report。\n\n"
        "也可以手动运行下面命令把网页端结果导回主流程：\n\n"
        "```bash\n"
        f"python3 scripts/oneclick_product.py llm-optimize --work-dir {work_dir} --manual-response-file {suggested_response}\n"
        "```\n\n"
        "导入时会先校验 JSON 合同；如果不是 JSON，会自动尝试兼容旧 YAML，再做 grounding。\n\n"
        "## 4. 导入成功后看哪些文件\n\n"
        f"- `llm_result.json`: 记录当前是 `manual_export` 还是 `manual_response`\n"
        f"- `llm_grounding_audit.json`: 记录 grounding 接受/拒绝的候选路径\n"
        f"- `hints.yaml`: grounding 后真正会被 apply 消费的 hints\n\n"
        f"网页端单文件输入：`{web_input_path}`\n"
        f"用户提示词文件：`{prompt_path}`\n"
    )
    manual_instructions_path.write_text(content, encoding="utf-8")


def _write_web_input_bundle(*, system_prompt: str, prompt_text: str, web_input_path: Path) -> None:
    content = (
        "请先严格遵守下面的 system 约束，再处理后面的用户请求。\n\n"
        "[system]\n"
        f"{system_prompt}\n\n"
        "[user]\n"
        f"{prompt_text.strip()}\n"
    )
    web_input_path.write_text(content, encoding="utf-8")


def _write_manual_import_script(
    *,
    repo_root: Path,
    work_dir: Path,
    import_script_path: Path,
    manual_response_file: Path | None,
) -> None:
    suggested_response = manual_response_file or (work_dir / "hints_raw.manual.yaml")
    content = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n\n"
        f"REPO_ROOT={str(repo_root)!r}\n"
        f"WORK_DIR={str(work_dir)!r}\n"
        f"DEFAULT_RESPONSE={str(suggested_response)!r}\n"
        'MANUAL_RESPONSE_FILE="${1:-$DEFAULT_RESPONSE}"\n\n'
        'if [[ ! -f "$MANUAL_RESPONSE_FILE" ]]; then\n'
        '  echo "[orchestrator] manual-response-file is missing: $MANUAL_RESPONSE_FILE" >&2\n'
        f'  echo "[orchestrator] expected {MANUAL_RESPONSE_CONTRACT}" >&2\n'
        "  exit 1\n"
        "fi\n\n"
        'cd "$REPO_ROOT"\n'
        'python3 scripts/oneclick_product.py llm-optimize --work-dir "$WORK_DIR" --manual-response-file "$MANUAL_RESPONSE_FILE"\n'
    )
    import_script_path.write_text(content, encoding="utf-8")
    import_script_path.chmod(0o755)


def _restore_deterministic_outputs(
    *,
    ranked_path: Path,
    selector_stats_path: Path,
    hints_path: Path,
    selector_stats_hint_path: Path,
) -> None:
    shutil.copyfile(ranked_path, hints_path)
    shutil.copyfile(selector_stats_path, selector_stats_hint_path)


def _request_body_preview(
    *,
    provider_type: str,
    request: LLMRequest,
) -> dict[str, object]:
    if provider_type == "openai_responses":
        preview: dict[str, object] = {
            "model": request.model,
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": request.system_prompt}],
                },
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": request.user_prompt}],
                },
            ],
            "schema_mode": request.schema_mode,
            "timeout_sec": request.timeout_sec,
        }
        if request.reasoning_effort:
            preview["reasoning_effort"] = request.reasoning_effort
        if request.schema_mode == "native_structured":
            preview["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "priority_paths",
                    "strict": True,
                }
            }
        return preview
    preview = {
        "model": request.model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": request.system_prompt},
            {"role": "user", "content": request.user_prompt},
        ],
        "schema_mode": request.schema_mode,
        "timeout_sec": request.timeout_sec,
    }
    if request.reasoning_effort:
        preview["reasoning_effort"] = request.reasoning_effort
    return preview


def _write_provider_response_artifact(*, response_path: Path, response: LLMResponse) -> None:
    if response.raw_payload is not None and not isinstance(response.raw_payload, str):
        response_path.write_text(
            json.dumps(response.raw_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return
    raw_text = str(response.raw_text or response.raw_payload or "").strip()
    if raw_text:
        response_path.write_text(raw_text + ("\n" if not raw_text.endswith("\n") else ""), encoding="utf-8")
        return
    response_path.write_text(
        json.dumps({"error": response.error_message or "empty response"}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _fallback_reason_for_response(response: LLMResponse) -> str:
    detail = str(response.error_message or "").strip() or "LLM request failed"
    if response.error_category == "invalid_response":
        return detail
    if response.error_category == "external_dependency":
        if response.http_status is None or response.transport_status in {"timeout", "connection_error"}:
            return f"transport/upstream error: {detail}"
        if response.http_status in {408, 429} or response.http_status >= 500:
            return f"http 429/5xx: {detail}"
        return f"transport/upstream error: {detail}"
    return detail


def _default_manual_response_path(work_dir: Path, manual_response_file: str | None) -> Path:
    return Path(manual_response_file).resolve() if manual_response_file else (work_dir / "hints_raw.manual.yaml").resolve()


def _default_resume_command(*, work_dir: Path, manual_response_path: Path) -> str:
    return (
        "python3 scripts/oneclick_product.py llm-optimize "
        f"--work-dir {work_dir} --manual-response-file {manual_response_path}"
    )


def _requested_mode(
    *,
    requested_mode: str | None,
    manual_response_path: Path | None,
    force_manual_export: bool,
) -> str:
    value = str(requested_mode or "").strip()
    if value in {"llm-auto", "llm-manual"}:
        return value
    if manual_response_path is not None or force_manual_export:
        return "llm-manual"
    return "llm-auto"


def _mode_result(*, requested_mode: str, mode: str, llm_applied: bool) -> str:
    if requested_mode == "llm-manual":
        if mode == "manual_export":
            return "llm-manual-paused"
        return "llm-manual-completed"
    if requested_mode == "llm-auto" and mode == "api" and llm_applied:
        return "llm-auto-success"
    return "llm-auto-fallback"


def _format_grounding_warning(audit: dict[str, object]) -> str | None:
    accepted_total = int(audit.get("accepted_total") or 0)
    if accepted_total <= 0:
        return None

    invalid = int(audit.get("invalid_path_skipped") or 0) + int(audit.get("special_path_skipped") or 0)
    unknown = int(audit.get("unknown_candidate_skipped") or 0)
    non_regular = int(audit.get("non_regular_candidate_skipped") or 0)
    guarded = int(audit.get("guard_maps_only_skipped") or 0) + int(audit.get("guard_low_confidence_skipped") or 0)
    missing = int(audit.get("missing_in_container_skipped") or 0)
    duplicate = int(audit.get("duplicate_skipped") or 0)

    summary_parts: list[str] = []
    if invalid > 0:
        summary_parts.append(f"{invalid} illegal/special path(s)")
    if unknown > 0:
        summary_parts.append(f"{unknown} non-candidate path(s)")
    if non_regular > 0:
        summary_parts.append(f"{non_regular} non-regular candidate(s)")
    if guarded > 0:
        summary_parts.append(f"{guarded} low-confidence/guarded path(s)")
    if missing > 0:
        summary_parts.append(f"{missing} path(s) missing in container")
    if duplicate > 0:
        summary_parts.append(f"{duplicate} duplicate path(s)")
    if not summary_parts:
        return None
    return (
        "LLM grounding discarded "
        + ", ".join(summary_parts)
        + f"; kept {accepted_total} grounded candidate path(s)"
    )


def _format_selector_budget_warning(selector_stats: dict[str, object]) -> str | None:
    chosen = int(selector_stats.get("chosen") or 0)
    if chosen <= 0:
        return None
    budget_skipped = int(selector_stats.get("budget_skipped") or 0)
    forced_first = bool(selector_stats.get("forced_first"))
    budget_bytes = int(selector_stats.get("budget_bytes") or 0)
    chosen_bytes = int(selector_stats.get("chosen_bytes") or 0)
    if budget_skipped <= 0 and not forced_first:
        return None
    if forced_first and budget_bytes > 0 and chosen_bytes > budget_bytes:
        return (
            f"selector budget is tighter than the strongest surviving path "
            f"(budget_bytes={budget_bytes}, chosen_bytes={chosen_bytes}); kept a forced-first result to stay non-empty"
        )
    return f"selector trimmed {budget_skipped} lower-priority path(s) to stay within budget while keeping {chosen} path(s)"


def run_llm_optimize_stage(
    *,
    repo_root: Path,
    work_dir: Path,
    size_budget_percent: float = 6.4,
    size_budget_mb: int = 0,
    min_hits: int = 2,
    p0_max: int = 24,
    early_onehit_ratio: float = 0.12,
    early_onehit_max: int = 8,
    early_onehit_max_size_kb: int = 512,
    llm_provider: str | None = None,
    llm_api_key_env: str | None = None,
    llm_api_base: str | None = None,
    llm_model: str | None = None,
    llm_reasoning_effort: str | None = None,
    llm_timeout_sec: int | None = None,
    llm_topn: int = 400,
    manual_response_file: str | None = None,
    force_manual_export: bool = False,
    requested_mode: str | None = None,
    request_timeout_sec: int | None = None,
) -> LlmOptimizeResult:
    runtime_state = _load_json(work_dir / "runtime_state.json")

    candidates_path = work_dir / "candidates.json"
    aggregate_stats_path = work_dir / "aggregate_stats.json"
    ranked_path = work_dir / "ranked.yaml"
    estargz_list_path = work_dir / "estargz.list"
    selector_stats_path = work_dir / "selector_stats_default.json"
    hints_path = work_dir / "hints.yaml"
    selector_stats_hint_path = work_dir / "selector_stats_hints.json"

    prompt_path = work_dir / "prompt.txt"
    web_input_path = work_dir / "web_llm_ready_to_paste.txt"
    body_path = work_dir / "body.json"
    response_path = work_dir / "llm_response.json"
    hints_raw_path = work_dir / "hints_raw.yaml"
    preferred_paths_path = work_dir / "preferred_paths.txt"
    grounding_audit_path = work_dir / "llm_grounding_audit.json"
    manual_instructions_path = work_dir / "llm_manual_instructions.md"
    import_script_path = work_dir / "import_manual_web_llm.sh"
    result_path = work_dir / "llm_result.json"
    stage_log_path = work_dir / "llm_optimize.log"

    llm_hints_path = work_dir / "hints_llm.yaml"
    llm_estargz_list_path = work_dir / "estargz_llm.list"
    llm_selector_stats_path = work_dir / "selector_stats_llm.json"

    build_prompt_tool = repo_root / "optimizer" / "tools" / "build_llm_prompt.py"
    prepare_preferred_tool = repo_root / "optimizer" / "tools" / "prepare_preferred_paths.py"
    select_tool = repo_root / "optimizer" / "tools" / "select_prefetch_paths.py"

    _ensure_file(candidates_path, "candidates")
    _ensure_file(aggregate_stats_path, "aggregate stats")
    _ensure_file(ranked_path, "deterministic ranked yaml")
    _ensure_file(estargz_list_path, "deterministic estargz list")
    _ensure_file(selector_stats_path, "deterministic selector stats")
    _ensure_file(build_prompt_tool, "build_llm_prompt.py")
    _ensure_file(prepare_preferred_tool, "prepare_preferred_paths.py")
    _ensure_file(select_tool, "select_prefetch_paths.py")
    analyze_candidate_count = _candidate_count(_load_json(candidates_path))

    stage_log_path.write_text("", encoding="utf-8")
    response_path.write_text("", encoding="utf-8")
    hints_raw_path.write_text("", encoding="utf-8")
    preferred_paths_path.write_text("", encoding="utf-8")
    grounding_audit_path.write_text("{}\n", encoding="utf-8")
    llm_hints_path.write_text("", encoding="utf-8")
    llm_estargz_list_path.write_text("", encoding="utf-8")
    llm_selector_stats_path.write_text("{}\n", encoding="utf-8")

    _restore_deterministic_outputs(
        ranked_path=ranked_path,
        selector_stats_path=selector_stats_path,
        hints_path=hints_path,
        selector_stats_hint_path=selector_stats_hint_path,
    )

    try:
        llm_config = resolve_llm_provider_config(
            provider=llm_provider,
            api_base=llm_api_base,
            api_key_env=llm_api_key_env,
            model=llm_model,
            reasoning_effort=llm_reasoning_effort,
            timeout_sec=(llm_timeout_sec if llm_timeout_sec is not None else request_timeout_sec),
        )
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    provider = llm_config.provider
    provider_type = llm_config.provider_type
    provider_source = llm_config.provider_source
    api_key = llm_config.api_key
    api_key_source = llm_config.api_key_source
    api_key_env = llm_config.api_key_env
    api_key_env_source = llm_config.api_key_env_source
    api_base = llm_config.api_base
    api_base_source = llm_config.api_base_source
    model = llm_config.model
    model_source = llm_config.model_source
    reasoning_effort = llm_config.reasoning_effort
    timeout_sec = llm_config.timeout_sec
    manual_response_path = Path(manual_response_file).resolve() if manual_response_file else None
    default_manual_response_path = _default_manual_response_path(work_dir, manual_response_file)
    requested_mode = _requested_mode(
        requested_mode=requested_mode,
        manual_response_path=manual_response_path,
        force_manual_export=force_manual_export,
    )
    prompt_output_contract = _prompt_contract_for_mode(requested_mode)
    system_prompt = _system_prompt_for_mode(requested_mode)
    resume_command = _default_resume_command(
        work_dir=work_dir,
        manual_response_path=default_manual_response_path,
    )

    prompt_proc = _run_tool(
        [
            "python3",
            str(build_prompt_tool),
            "--candidates",
            str(candidates_path),
            "--selector-stats",
            str(selector_stats_path),
            "--out",
            str(prompt_path),
            "--output-contract",
            prompt_output_contract,
            "--budget-desc",
            _budget_desc(size_budget_percent, size_budget_mb),
            "--min-hits",
            str(min_hits),
            "--p0-max",
            str(p0_max),
            "--topn",
            str(max(llm_topn, 0)),
        ],
        repo_root=repo_root,
        stage_log_path=stage_log_path,
    )
    if prompt_proc.returncode != 0:
        raise RuntimeError(f"build_llm_prompt failed, see {stage_log_path}")

    prompt_text = prompt_path.read_text(encoding="utf-8")
    schema_mode = default_schema_mode_for_provider(provider_type)
    llm_request = LLMRequest(
        system_prompt=system_prompt,
        user_prompt=prompt_text,
        model=model,
        schema_mode=schema_mode,
        reasoning_effort=reasoning_effort,
        timeout_sec=timeout_sec,
    )
    body = _request_body_preview(provider_type=provider_type, request=llm_request)
    body_path.write_text(
        json.dumps(body, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_web_input_bundle(system_prompt=system_prompt, prompt_text=prompt_text, web_input_path=web_input_path)
    _write_manual_import_script(
        repo_root=repo_root,
        work_dir=work_dir,
        import_script_path=import_script_path,
        manual_response_file=manual_response_path,
    )
    _write_manual_instructions(
        work_dir=work_dir,
        prompt_path=prompt_path,
        web_input_path=web_input_path,
        manual_instructions_path=manual_instructions_path,
        import_script_path=import_script_path,
        manual_response_file=manual_response_path,
    )

    warnings: list[str] = []
    mode = "manual_export"
    llm_applied = False
    final_hints_source = "deterministic_ranked"
    raw_text = ""
    grounded_input_text = ""
    api_attempted = False
    api_http_status: int | None = None
    transport_status: str | None = None
    error_category: str | None = None
    fallback_kind: str | None = None
    fallback_reason: str | None = None
    auto_normalization_audit: dict[str, object] | None = None

    if manual_response_path is not None:
        if not manual_response_path.exists():
            raise RuntimeError(f"manual LLM response is missing: {manual_response_path}")
        grounded_input_text = _validate_manual_response_text(
            path=manual_response_path,
            text=manual_response_path.read_text(encoding="utf-8"),
            manual_instructions_path=manual_instructions_path,
        )
        raw_text = grounded_input_text
        hints_raw_path.write_text(raw_text, encoding="utf-8")
        mode = "manual_response"
    elif force_manual_export:
        mode = "manual_export"
    else:
        if provider_requires_api_key(llm_config) and not api_key:
            raise RuntimeError(describe_missing_api_key(llm_config))
        api_attempted = True
        provider_config = build_provider_config(llm_config)
        provider_client = ProviderRouter().build(provider_config)
        response = provider_client.complete(llm_request)
        if (
            response.ok
            and response.structured_output is not None
            and analyze_candidate_count > 0
            and _structured_output_total_paths(response.structured_output) == 0
        ):
            response = LLMResponse(
                ok=False,
                provider_type=response.provider_type,
                schema_mode=response.schema_mode,
                transport_status=response.transport_status,
                http_status=response.http_status,
                raw_text=response.raw_text,
                raw_payload=response.raw_payload,
                structured_output=None,
                error_category="invalid_response",
                error_message="structured output is valid JSON but empty/unhelpful for a non-empty candidate set",
            )
        api_http_status = response.http_status
        transport_status = response.transport_status
        error_category = response.error_category
        raw_text = str(response.raw_text or "")
        _write_provider_response_artifact(response_path=response_path, response=response)
        if response.ok and response.structured_output is not None:
            normalized_payload, auto_normalization_audit = _normalize_auto_response_payload(
                response.structured_output,
                p0_max=p0_max,
            )
            warning_text = _format_auto_normalization_warning(auto_normalization_audit)
            if warning_text:
                warnings.append(warning_text)
            grounded_input_text = _dump_manual_response_yaml(normalized_payload)
            hints_raw_path.write_text(grounded_input_text, encoding="utf-8")
            mode = "api"
        elif response.error_category in {"external_dependency", "invalid_response"}:
            if raw_text.strip():
                hints_raw_path.write_text(raw_text + ("\n" if not raw_text.endswith("\n") else ""), encoding="utf-8")
            fallback_kind = response.error_category
            fallback_reason = _fallback_reason_for_response(response)
            warnings.append(_format_fallback_warning(fallback_reason))
            mode = "api_fallback"
        elif response.error_category == "auth_or_config":
            raise RuntimeError(
                describe_auth_failure(
                    llm_config,
                    error_text=response.error_message
                    or (f"LLM HTTP {response.http_status}" if response.http_status is not None else "LLM auth failed"),
                )
            )
        else:
            raise RuntimeError(response.error_message or "LLM provider failed with a local internal error")

    preferred_count = 0
    accepted_total = 0

    if grounded_input_text.strip():
        prepare_proc = _run_tool(
            [
                "python3",
                str(prepare_preferred_tool),
                "--hints-raw",
                str(hints_raw_path),
                "--candidates",
                str(candidates_path),
                "--selector-stats",
                str(selector_stats_path),
                "--container-pid",
                str(int(runtime_state.get("container_pid") or 0)),
                "--out",
                str(preferred_paths_path),
                "--audit-json",
                str(grounding_audit_path),
            ],
            repo_root=repo_root,
            stage_log_path=stage_log_path,
        )
        if prepare_proc.returncode != 0:
            raise RuntimeError(f"prepare_preferred_paths failed, see {stage_log_path}")

        preferred_count = sum(1 for line in preferred_paths_path.read_text(encoding="utf-8").splitlines() if line.strip())
        grounding_audit_payload = _load_json(grounding_audit_path)
        try:
            accepted_total = int(grounding_audit_payload.get("accepted_total") or 0)
        except Exception:
            accepted_total = preferred_count
        grounding_warning = _format_grounding_warning(grounding_audit_payload)
        if grounding_warning:
            warnings.append(grounding_warning)

        if preferred_count > 0:
            if mode == "manual_response":
                _materialize_grounded_manual_outputs(
                    grounded_input_text=grounded_input_text,
                    preferred_paths_path=preferred_paths_path,
                    hints_path=hints_path,
                    estargz_list_path=estargz_list_path,
                    selector_stats_path=selector_stats_path,
                    selector_stats_hint_path=selector_stats_hint_path,
                    llm_hints_path=llm_hints_path,
                    llm_estargz_list_path=llm_estargz_list_path,
                    llm_selector_stats_path=llm_selector_stats_path,
                    accepted_total=accepted_total,
                )
            else:
                select_proc = _run_tool(
                    [
                        "python3",
                        str(select_tool),
                        "--candidates",
                        str(candidates_path),
                        "--aggregate-stats",
                        str(aggregate_stats_path),
                        "--out-yaml",
                        str(llm_hints_path),
                        "--out-list",
                        str(llm_estargz_list_path),
                        "--stats-json",
                        str(llm_selector_stats_path),
                        "--budget-percent",
                        str(size_budget_percent),
                        "--budget-mb",
                        str(size_budget_mb),
                        "--min-hits",
                        str(min_hits),
                        "--p0-max",
                        str(p0_max),
                        "--early-onehit-ratio",
                        str(early_onehit_ratio),
                        "--early-onehit-max",
                        str(early_onehit_max),
                        "--early-onehit-max-size-kb",
                        str(early_onehit_max_size_kb),
                        "--preferred",
                        str(preferred_paths_path),
                    ],
                    repo_root=repo_root,
                    stage_log_path=stage_log_path,
                )
                if select_proc.returncode != 0:
                    raise RuntimeError(f"select_prefetch_paths failed, see {stage_log_path}")
                selector_budget_warning = _format_selector_budget_warning(_load_json(llm_selector_stats_path))
                if selector_budget_warning:
                    warnings.append(selector_budget_warning)
                shutil.copyfile(llm_hints_path, hints_path)
                shutil.copyfile(llm_estargz_list_path, estargz_list_path)
                shutil.copyfile(llm_selector_stats_path, selector_stats_hint_path)
            llm_applied = True
            final_hints_source = "grounded_llm"
        else:
            if requested_mode == "llm-auto":
                mode = "api_fallback"
                fallback_kind = fallback_kind or "external_dependency"
                if accepted_total > 0:
                    fallback_reason = fallback_reason or (
                        f"200 but unusable response: grounding produced {accepted_total} accepted candidates "
                        "but 0 preferred paths survived selection"
                    )
                else:
                    fallback_reason = fallback_reason or "200 but unusable response: grounding accepted 0 candidate paths"
                warnings.append(_format_fallback_warning(fallback_reason))
            else:
                warnings.append("LLM returned no grounded candidate paths; kept deterministic hints")

    mode_result = _mode_result(
        requested_mode=requested_mode,
        mode=mode,
        llm_applied=llm_applied,
    )
    resume_required = mode == "manual_export"
    pause_reason = "awaiting_manual_llm_response" if mode == "manual_export" else None

    result_payload = {
        "requested_mode": requested_mode,
        "mode": mode,
        "mode_result": mode_result,
        "provider": provider,
        "provider_type": provider_type,
        "provider_source": provider_source,
        "model": model,
        "model_source": model_source,
        "api_base": api_base,
        "api_base_source": api_base_source,
        "api_key_env": api_key_env,
        "api_key_env_source": api_key_env_source,
        "api_key_present": bool(api_key),
        "api_key_source": api_key_source,
        "api_attempted": api_attempted,
        "api_http_status": api_http_status,
        "http_status": api_http_status,
        "transport_status": transport_status,
        "schema_mode": schema_mode,
        "error_category": error_category,
        "llm_reasoning_effort": reasoning_effort,
        "llm_timeout_sec": timeout_sec,
        "manual_response_file": str(manual_response_path) if manual_response_path else None,
        "manual_response_default_path": str(default_manual_response_path),
        "manual_response_format": "json",
        "resume_required": resume_required,
        "pause_reason": pause_reason,
        "resume_command": resume_command if resume_required else None,
        "llm_raw_nonempty": bool(raw_text.strip()),
        "preferred_count": preferred_count,
        "accepted_total": accepted_total,
        "llm_applied": llm_applied,
        "final_hints_source": final_hints_source,
        "auto_normalization": auto_normalization_audit,
        "fallback_kind": fallback_kind,
        "fallback_reason": fallback_reason,
        "warnings": warnings,
        "artifacts": {
            "prompt": str(prompt_path),
            "web_llm_input": str(web_input_path),
            "llm_body": str(body_path),
            "llm_response": str(response_path),
            "hints_raw": str(hints_raw_path),
            "preferred_paths": str(preferred_paths_path),
            "llm_grounding_audit": str(grounding_audit_path),
            "llm_manual_instructions": str(manual_instructions_path),
            "llm_manual_import_script": str(import_script_path),
            "hints": str(hints_path),
            "estargz_list": str(estargz_list_path),
            "selector_stats_hints": str(selector_stats_hint_path),
        },
    }
    result_path.write_text(
        json.dumps(result_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    return LlmOptimizeResult(
        prompt_path=prompt_path,
        web_input_path=web_input_path,
        body_path=body_path,
        response_path=response_path,
        hints_raw_path=hints_raw_path,
        preferred_paths_path=preferred_paths_path,
        grounding_audit_path=grounding_audit_path,
        hints_path=hints_path,
        estargz_list_path=estargz_list_path,
        selector_stats_hint_path=selector_stats_hint_path,
        manual_instructions_path=manual_instructions_path,
        import_script_path=import_script_path,
        result_path=result_path,
        stage_log_path=stage_log_path,
        warnings=warnings,
        requested_mode=requested_mode,
        mode=mode,
        mode_result=mode_result,
        llm_applied=llm_applied,
        final_hints_source=final_hints_source,
        resume_required=resume_required,
        pause_reason=pause_reason,
        manual_response_default_path=default_manual_response_path,
        manual_response_format="json",
        resume_command=resume_command if resume_required else None,
        fallback_kind=fallback_kind,
        fallback_reason=fallback_reason,
    )
