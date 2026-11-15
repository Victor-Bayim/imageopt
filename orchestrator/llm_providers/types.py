from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


PriorityKey = Literal["P0", "P1", "P2", "P3"]
SchemaMode = Literal["native_structured", "json_prompted", "raw_text"]
ErrorCategory = Literal["external_dependency", "invalid_response", "auth_or_config", "local_internal"]
TransportStatus = Literal["ok", "timeout", "connection_error", "http_error", "local_error"]

PRIORITY_KEYS: tuple[PriorityKey, ...] = ("P0", "P1", "P2", "P3")
StructuredOutput = dict[str, list[str]]


@dataclass(frozen=True)
class ProviderConfig:
    provider_type: str
    provider_name: str
    provider_source: str
    model: str
    model_source: str
    api_base: str
    api_key_env: str | None
    api_key: str
    api_key_source: str | None
    reasoning_effort: str | None = None
    timeout_sec: int = 60


@dataclass(frozen=True)
class LLMRequest:
    system_prompt: str
    user_prompt: str
    model: str
    schema_mode: SchemaMode
    reasoning_effort: str | None = None
    timeout_sec: int = 60


@dataclass(frozen=True)
class LLMResponse:
    ok: bool
    provider_type: str
    schema_mode: SchemaMode
    transport_status: TransportStatus
    http_status: int | None
    raw_text: str
    raw_payload: object | None
    structured_output: StructuredOutput | None
    error_category: ErrorCategory | None = None
    error_message: str | None = None
