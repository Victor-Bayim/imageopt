from __future__ import annotations

import json

from orchestrator.llm_providers.base import LLMProvider
from orchestrator.llm_providers.parsing import load_structured_output_from_json_text, validate_structured_output
from orchestrator.llm_providers.types import LLMRequest, LLMResponse, ProviderConfig


DEFAULT_SUCCESS_PAYLOAD = {
    "P0": ["/app/a.so"],
    "P1": [],
    "P2": [],
    "P3": [],
}


class MockProvider(LLMProvider):
    def __init__(
        self,
        config: ProviderConfig,
        *,
        mode: str = "success",
        raw_text: str | None = None,
        raw_payload: object | None = None,
    ) -> None:
        self._config = config
        self._mode = str(mode or "success").strip()
        self._raw_text = raw_text
        self._raw_payload = raw_payload

    def _response_from_raw(self, request: LLMRequest) -> LLMResponse:
        raw_payload = self._raw_payload
        raw_text = str(self._raw_text or "")
        if raw_payload is not None and not raw_text:
            if isinstance(raw_payload, str):
                raw_text = raw_payload
            else:
                raw_text = json.dumps(raw_payload, ensure_ascii=False)
        try:
            if raw_payload is not None and isinstance(raw_payload, dict) and set(raw_payload.keys()) == {
                "P0",
                "P1",
                "P2",
                "P3",
            }:
                structured_output = validate_structured_output(raw_payload)
            else:
                structured_output = load_structured_output_from_json_text(raw_text)
        except ValueError as exc:
            return LLMResponse(
                ok=False,
                provider_type=self._config.provider_type,
                schema_mode=request.schema_mode,
                transport_status="ok",
                http_status=200,
                raw_text=raw_text,
                raw_payload=raw_payload,
                structured_output=None,
                error_category="invalid_response",
                error_message=str(exc),
            )
        return LLMResponse(
            ok=True,
            provider_type=self._config.provider_type,
            schema_mode=request.schema_mode,
            transport_status="ok",
            http_status=200,
            raw_text=raw_text or json.dumps(structured_output, ensure_ascii=False),
            raw_payload=raw_payload if raw_payload is not None else structured_output,
            structured_output=structured_output,
        )

    def complete(self, request: LLMRequest) -> LLMResponse:
        if self._raw_text is not None or self._raw_payload is not None:
            return self._response_from_raw(request)
        if self._mode == "success":
            payload = validate_structured_output(DEFAULT_SUCCESS_PAYLOAD)
            return LLMResponse(
                ok=True,
                provider_type=self._config.provider_type,
                schema_mode=request.schema_mode,
                transport_status="ok",
                http_status=200,
                raw_text=json.dumps(payload, ensure_ascii=False),
                raw_payload=payload,
                structured_output=payload,
            )
        if self._mode == "invalid_response":
            return LLMResponse(
                ok=False,
                provider_type=self._config.provider_type,
                schema_mode=request.schema_mode,
                transport_status="ok",
                http_status=200,
                raw_text="not valid structured output",
                raw_payload={"message": "not valid structured output"},
                structured_output=None,
                error_category="invalid_response",
                error_message="mock invalid response",
            )
        if self._mode == "external_dependency":
            return LLMResponse(
                ok=False,
                provider_type=self._config.provider_type,
                schema_mode=request.schema_mode,
                transport_status="timeout",
                http_status=None,
                raw_text="",
                raw_payload=None,
                structured_output=None,
                error_category="external_dependency",
                error_message="mock external dependency",
            )
        if self._mode == "auth_or_config":
            return LLMResponse(
                ok=False,
                provider_type=self._config.provider_type,
                schema_mode=request.schema_mode,
                transport_status="http_error",
                http_status=401,
                raw_text="mock auth failure",
                raw_payload={"error": {"message": "mock auth failure"}},
                structured_output=None,
                error_category="auth_or_config",
                error_message="mock auth/config error",
            )
        if self._mode == "local_internal":
            return LLMResponse(
                ok=False,
                provider_type=self._config.provider_type,
                schema_mode=request.schema_mode,
                transport_status="local_error",
                http_status=None,
                raw_text="mock local internal failure",
                raw_payload=None,
                structured_output=None,
                error_category="local_internal",
                error_message="mock local internal error",
            )
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
            error_message=f"unsupported mock provider mode: {self._mode}",
        )
