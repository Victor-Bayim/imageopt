from __future__ import annotations

import json
import urllib.error
import urllib.request

from orchestrator.llm_providers.base import LLMProvider
from orchestrator.llm_providers.parsing import load_structured_output_from_json_text
from orchestrator.llm_providers.types import LLMRequest, LLMResponse, ProviderConfig


DEFAULT_HTTP_USER_AGENT = "cpp-sampler/1.1 llm-provider"


def _compact_error_text(raw_text: str, *, limit: int = 160) -> str:
    compact = " ".join(str(raw_text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def _extract_error_message(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        message = str(error.get("message") or "").strip()
        if message:
            return message
    return str(payload.get("message") or "").strip()


def _extract_chat_content(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = (choices[0] or {}).get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
        return "\n".join(part for part in parts if part)
    return ""


def _http_error_category(status: int) -> str:
    if status in {408, 429} or status >= 500:
        return "external_dependency"
    return "auth_or_config"


class OpenAICompatibleProvider(LLMProvider):
    def __init__(self, config: ProviderConfig) -> None:
        self._config = config

    def _request_body(self, request: LLMRequest) -> dict[str, object]:
        body: dict[str, object] = {
            "model": request.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
        }
        return body

    def complete(self, request: LLMRequest) -> LLMResponse:
        body = self._request_body(request)
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        http_status: int | None = None
        raw_body = ""
        parsed_payload: object | None = None
        transport_status = "ok"

        try:
            http_request = urllib.request.Request(
                self._config.api_base.rstrip("/") + "/chat/completions",
                data=payload,
                headers={
                    "Authorization": f"Bearer {self._config.api_key}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": DEFAULT_HTTP_USER_AGENT,
                },
                method="POST",
            )
            with urllib.request.urlopen(http_request, timeout=request.timeout_sec) as response:
                http_status = int(response.getcode())
                raw_body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            http_status = int(exc.code)
            transport_status = "http_error"
            raw_body = exc.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as exc:
            detail = str(exc.reason)
            transport_status = "timeout" if "timed out" in detail.lower() else "connection_error"
            return LLMResponse(
                ok=False,
                provider_type=self._config.provider_type,
                schema_mode=request.schema_mode,
                transport_status=transport_status,
                http_status=None,
                raw_text="",
                raw_payload=None,
                structured_output=None,
                error_category="external_dependency",
                error_message=f"LLM request failed: {detail}",
            )
        except Exception as exc:  # pragma: no cover - defensive
            return LLMResponse(
                ok=False,
                provider_type=self._config.provider_type,
                schema_mode=request.schema_mode,
                transport_status="local_error",
                http_status=None,
                raw_text="",
                raw_payload=None,
                structured_output=None,
                error_category="local_internal",
                error_message=f"LLM request failed: {exc}",
            )

        if raw_body:
            try:
                parsed_payload = json.loads(raw_body)
            except Exception:
                parsed_payload = raw_body

        if http_status != 200:
            detail = _extract_error_message(parsed_payload)
            if not detail:
                detail = _compact_error_text(raw_body)
            message = f"LLM HTTP {http_status}"
            if detail:
                message = f"{message}: {detail}"
            return LLMResponse(
                ok=False,
                provider_type=self._config.provider_type,
                schema_mode=request.schema_mode,
                transport_status=transport_status,
                http_status=http_status,
                raw_text=raw_body,
                raw_payload=parsed_payload,
                structured_output=None,
                error_category=_http_error_category(http_status),
                error_message=message,
            )

        if not isinstance(parsed_payload, dict):
            return LLMResponse(
                ok=False,
                provider_type=self._config.provider_type,
                schema_mode=request.schema_mode,
                transport_status="ok",
                http_status=200,
                raw_text=raw_body,
                raw_payload=parsed_payload,
                structured_output=None,
                error_category="invalid_response",
                error_message="LLM response is not valid JSON",
            )

        content = _extract_chat_content(parsed_payload)
        if not content.strip():
            return LLMResponse(
                ok=False,
                provider_type=self._config.provider_type,
                schema_mode=request.schema_mode,
                transport_status="ok",
                http_status=200,
                raw_text="",
                raw_payload=parsed_payload,
                structured_output=None,
                error_category="invalid_response",
                error_message="chat completion contained no usable content",
            )

        try:
            structured_output = load_structured_output_from_json_text(content)
        except ValueError as exc:
            return LLMResponse(
                ok=False,
                provider_type=self._config.provider_type,
                schema_mode=request.schema_mode,
                transport_status="ok",
                http_status=200,
                raw_text=content,
                raw_payload=parsed_payload,
                structured_output=None,
                error_category="invalid_response",
                error_message=f"schema validation failed: {exc}",
            )

        return LLMResponse(
            ok=True,
            provider_type=self._config.provider_type,
            schema_mode=request.schema_mode,
            transport_status="ok",
            http_status=200,
            raw_text=content,
            raw_payload=parsed_payload,
            structured_output=structured_output,
        )
