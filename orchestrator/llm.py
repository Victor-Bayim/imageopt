from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from orchestrator.llm_providers.types import ProviderConfig, SchemaMode


API_KEY_ENV_NAMES = (
    "LLMAPIKEY",
    "LLM_API_KEY",
    "llmapikey",
    "llm_api_key",
)
API_BASE_ENV_NAMES = (
    "LLMAPIBASE",
    "LLM_API_BASE",
    "llmapibase",
    "llm_api_base",
)
MODEL_ENV_NAMES = (
    "LLMMODEL",
    "LLM_MODEL",
    "llmmodel",
    "llm_model",
)

DEFAULT_PROVIDER = "openai"
DEFAULT_API_BASE = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_TIMEOUT_SEC = 60
DEFAULT_MOCK_MODEL = "mock-llm"
CANONICAL_LLM_PROVIDER_TYPES = ("openai_responses", "openai_compatible", "mock")


@dataclass(frozen=True)
class LlmProviderDefaults:
    provider_type: str
    api_base: str | None
    api_key_env: str | None
    model: str | None = None


@dataclass(frozen=True)
class ResolvedLlmProviderConfig:
    provider: str
    provider_type: str
    provider_source: str
    api_base: str
    api_base_source: str
    api_key_env: str | None
    api_key_env_source: str | None
    api_key: str
    api_key_source: str | None
    model: str
    model_source: str
    reasoning_effort: str | None = None
    timeout_sec: int = DEFAULT_TIMEOUT_SEC


LLM_PROVIDER_DEFAULTS: dict[str, LlmProviderDefaults] = {
    "openai": LlmProviderDefaults(
        provider_type="openai_compatible",
        api_base=DEFAULT_API_BASE,
        api_key_env="OPENAI_API_KEY",
        model=DEFAULT_MODEL,
    ),
    "groq": LlmProviderDefaults(
        provider_type="openai_compatible",
        api_base="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
        model="llama-3.1-8b-instant",
    ),
    "openrouter": LlmProviderDefaults(
        provider_type="openai_compatible",
        api_base="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        model="openrouter/auto",
    ),
    "cerebras": LlmProviderDefaults(
        provider_type="openai_compatible",
        api_base="https://api.cerebras.ai/v1",
        api_key_env="CEREBRAS_API_KEY",
        model=DEFAULT_MODEL,
    ),
    "custom": LlmProviderDefaults(
        provider_type="openai_compatible",
        api_base=None,
        api_key_env=None,
        model=DEFAULT_MODEL,
    ),
    "openai_compatible": LlmProviderDefaults(
        provider_type="openai_compatible",
        api_base=DEFAULT_API_BASE,
        api_key_env="OPENAI_API_KEY",
        model=DEFAULT_MODEL,
    ),
    "openai_responses": LlmProviderDefaults(
        provider_type="openai_responses",
        api_base=DEFAULT_API_BASE,
        api_key_env="OPENAI_API_KEY",
        model=DEFAULT_MODEL,
    ),
    "mock": LlmProviderDefaults(
        provider_type="mock",
        api_base="",
        api_key_env=None,
        model=DEFAULT_MOCK_MODEL,
    ),
}
FORMAL_LLM_PROVIDERS = tuple(LLM_PROVIDER_DEFAULTS.keys())


def _first_env_value(
    names: tuple[str, ...],
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[str, str | None]:
    source = env or os.environ
    for name in names:
        value = str(source.get(name) or "").strip()
        if value:
            return value, name
    return "", None


def _normalized_text(value: str | None) -> str:
    return str(value or "").strip()


def resolve_api_key(
    *,
    env: Mapping[str, str] | None = None,
    env_names: tuple[str, ...] | None = None,
) -> tuple[str, str | None]:
    return _first_env_value(env_names or API_KEY_ENV_NAMES, env=env)


def _resolve_provider(provider: str | None) -> tuple[str, str]:
    normalized = _normalized_text(provider).lower()
    if not normalized:
        return DEFAULT_PROVIDER, "built_in_default"
    if normalized not in LLM_PROVIDER_DEFAULTS:
        allowed = ", ".join(FORMAL_LLM_PROVIDERS)
        raise ValueError(f"unsupported llm provider {normalized!r}; expected one of: {allowed}")
    return normalized, "explicit"


def _resolve_timeout(timeout_sec: int | None) -> int:
    value = int(timeout_sec or DEFAULT_TIMEOUT_SEC)
    if value <= 0:
        raise ValueError("llm timeout must be a positive integer")
    return value


def _resolve_reasoning_effort(reasoning_effort: str | None) -> str | None:
    value = _normalized_text(reasoning_effort)
    return value or None


def resolve_llm_provider_config(
    *,
    provider: str | None = None,
    api_base: str | None = None,
    api_key_env: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    timeout_sec: int | None = None,
    env: Mapping[str, str] | None = None,
) -> ResolvedLlmProviderConfig:
    resolved_provider, provider_source = _resolve_provider(provider)
    provider_defaults = LLM_PROVIDER_DEFAULTS[resolved_provider]

    explicit_api_base = _normalized_text(api_base)
    if explicit_api_base:
        resolved_api_base = explicit_api_base
        api_base_source = "explicit"
    elif provider_defaults.api_base is not None:
        resolved_api_base = str(provider_defaults.api_base)
        api_base_source = f"provider_default:{resolved_provider}"
    elif resolved_provider == "custom":
        raise ValueError("llm provider 'custom' requires --llm-api-base or config field llm_api_base")
    else:
        resolved_api_base = DEFAULT_API_BASE
        api_base_source = "built_in_default"

    explicit_api_key_env = _normalized_text(api_key_env)
    if explicit_api_key_env:
        resolved_api_key_env = explicit_api_key_env
        api_key_env_source = "explicit"
        api_key_lookup_names = (resolved_api_key_env,)
    elif provider_defaults.api_key_env:
        resolved_api_key_env = str(provider_defaults.api_key_env)
        api_key_env_source = f"provider_default:{resolved_provider}"
        api_key_lookup_names = (resolved_api_key_env,) + tuple(
            name for name in API_KEY_ENV_NAMES if name != resolved_api_key_env
        )
    elif resolved_provider == "custom":
        raise ValueError("llm provider 'custom' requires --llm-api-key-env or config field llm_api_key_env")
    else:
        resolved_api_key_env = None
        api_key_env_source = None
        api_key_lookup_names = ()

    resolved_api_key = ""
    api_key_source: str | None = None
    if api_key_lookup_names:
        resolved_api_key, api_key_source = resolve_api_key(env=env, env_names=api_key_lookup_names)

    explicit_model = _normalized_text(model)
    if explicit_model:
        resolved_model = explicit_model
        model_source = "explicit"
    elif provider_defaults.model:
        resolved_model = str(provider_defaults.model)
        model_source = f"provider_default:{resolved_provider}"
    else:
        resolved_model = DEFAULT_MODEL
        model_source = "built_in_default"

    return ResolvedLlmProviderConfig(
        provider=resolved_provider,
        provider_type=provider_defaults.provider_type,
        provider_source=provider_source,
        api_base=resolved_api_base,
        api_base_source=api_base_source,
        api_key_env=resolved_api_key_env,
        api_key_env_source=api_key_env_source,
        api_key=resolved_api_key,
        api_key_source=api_key_source,
        model=resolved_model,
        model_source=model_source,
        reasoning_effort=_resolve_reasoning_effort(reasoning_effort),
        timeout_sec=_resolve_timeout(timeout_sec),
    )


def build_provider_config(config: ResolvedLlmProviderConfig) -> ProviderConfig:
    return ProviderConfig(
        provider_type=config.provider_type,
        provider_name=config.provider,
        provider_source=config.provider_source,
        model=config.model,
        model_source=config.model_source,
        api_base=config.api_base,
        api_key_env=config.api_key_env,
        api_key=config.api_key,
        api_key_source=config.api_key_source,
        reasoning_effort=config.reasoning_effort,
        timeout_sec=config.timeout_sec,
    )


def default_schema_mode_for_provider(provider_type: str) -> SchemaMode:
    if provider_type == "openai_responses":
        return "native_structured"
    if provider_type == "openai_compatible":
        return "json_prompted"
    return "raw_text"


def provider_requires_api_key(config: ResolvedLlmProviderConfig) -> bool:
    return config.provider_type != "mock"


def describe_missing_api_key(config: ResolvedLlmProviderConfig) -> str:
    expected_env = config.api_key_env or "an explicit llm_api_key_env"
    if config.provider_type == "mock":
        return "mock provider does not require an API key"
    if config.provider == "custom":
        return (
            "llm provider 'custom' expected an API key in env "
            f"{expected_env}, but that environment variable is not set"
        )
    if config.api_key_env_source == f"provider_default:{config.provider}":
        return (
            f"llm provider '{config.provider}' expected an API key in env {expected_env}; "
            f"legacy {', '.join(API_KEY_ENV_NAMES[:2])} are also accepted"
        )
    return (
        f"llm provider '{config.provider}' expected an API key in env {expected_env}, "
        "but it is not set"
    )


def describe_auth_failure(
    config: ResolvedLlmProviderConfig,
    *,
    error_text: str | None,
) -> str:
    detail = _normalized_text(error_text) or "LLM authentication failed"
    provider_defaults = LLM_PROVIDER_DEFAULTS[config.provider]
    mismatch_hints: list[str] = []
    if provider_defaults.api_base and config.api_base.rstrip("/") != str(provider_defaults.api_base).rstrip("/"):
        mismatch_hints.append(
            f"provider {config.provider} defaults to api_base={provider_defaults.api_base}, "
            f"but resolved api_base={config.api_base}"
        )
    if provider_defaults.api_key_env and config.api_key_env and config.api_key_env != provider_defaults.api_key_env:
        mismatch_hints.append(
            f"provider {config.provider} defaults to api_key_env={provider_defaults.api_key_env}, "
            f"but resolved api_key_env={config.api_key_env}"
        )
    if mismatch_hints:
        return f"{detail}; provider/base URL/key mismatch likely: {'; '.join(mismatch_hints)}"
    key_env = config.api_key_source or config.api_key_env or "unknown"
    return (
        f"{detail}; API key appears invalid or unauthorized for provider={config.provider} "
        f"provider_type={config.provider_type} api_base={config.api_base} api_key_env={key_env}"
    )


def resolve_api_base(
    cli_value: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    value = str(cli_value or "").strip()
    if value:
        return value, "cli"
    env_value, env_name = _first_env_value(API_BASE_ENV_NAMES, env=env)
    if env_value:
        return env_value, env_name or "env"
    return DEFAULT_API_BASE, "default"


def resolve_model(
    cli_value: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    value = str(cli_value or "").strip()
    if value:
        return value, "cli"
    env_value, env_name = _first_env_value(MODEL_ENV_NAMES, env=env)
    if env_value:
        return env_value, env_name or "env"
    return DEFAULT_MODEL, "default"
