import io
import unittest
from unittest import mock

from orchestrator.llm_providers.openai_compatible import OpenAICompatibleProvider
from orchestrator.llm_providers.openai_responses import OpenAIResponsesProvider
from orchestrator.llm_providers.parsing import (
    load_structured_output_from_json_text,
    validate_structured_output,
)
from orchestrator.llm_providers.types import LLMRequest, ProviderConfig


def _provider_config() -> ProviderConfig:
    return ProviderConfig(
        provider_type="openai_compatible",
        provider_name="openai",
        provider_source="explicit",
        model="gpt-4o-mini",
        model_source="explicit",
        api_base="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        api_key="demo-key",
        api_key_source="OPENAI_API_KEY",
    )


def _request() -> LLMRequest:
    return LLMRequest(
        system_prompt="Return strict JSON only.",
        user_prompt="Pick paths.",
        model="gpt-4o-mini",
        schema_mode="json_prompted",
        timeout_sec=30,
    )


def _responses_provider_config() -> ProviderConfig:
    return ProviderConfig(
        provider_type="openai_responses",
        provider_name="openai_responses",
        provider_source="explicit",
        model="gpt-4.1-mini",
        model_source="explicit",
        api_base="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        api_key="demo-key",
        api_key_source="OPENAI_API_KEY",
    )


class StructuredOutputParsingTests(unittest.TestCase):
    def test_strict_json_parsing_accepts_only_required_keys(self):
        payload = load_structured_output_from_json_text(
            '{"P0":["/app/a.so"],"P1":[],"P2":[],"P3":[]}'
        )
        self.assertEqual(["/app/a.so"], payload["P0"])

    def test_strict_json_parsing_rejects_extra_fields(self):
        with self.assertRaisesRegex(ValueError, "unsupported top-level keys: extra"):
            validate_structured_output(
                {"P0": [], "P1": [], "P2": [], "P3": [], "extra": []}
            )

    def test_strict_json_parsing_rejects_relative_paths(self):
        with self.assertRaisesRegex(ValueError, "must be an absolute path"):
            validate_structured_output(
                {"P0": ["relative.so"], "P1": [], "P2": [], "P3": []}
            )


class OpenAICompatibleProviderTests(unittest.TestCase):
    def test_openai_compatible_response_parsing_success(self):
        provider = OpenAICompatibleProvider(_provider_config())

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

            def getcode(self):
                return 200

            def read(self):
                return (
                    b'{"choices":[{"message":{"content":"{\\"P0\\":[\\"/app/a.so\\"],\\"P1\\":[],\\"P2\\":[],\\"P3\\":[]}"}}]}'
                )

        with mock.patch("orchestrator.llm_providers.openai_compatible.urllib.request.urlopen", return_value=_Response()):
            response = provider.complete(_request())

        self.assertTrue(response.ok)
        self.assertEqual(200, response.http_status)
        self.assertEqual(["/app/a.so"], response.structured_output["P0"])

    def test_openai_compatible_response_parsing_invalid_structured_output(self):
        provider = OpenAICompatibleProvider(_provider_config())

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

            def getcode(self):
                return 200

            def read(self):
                return b'{"choices":[{"message":{"content":"{\\"P0\\":[],\\"P1\\":[],\\"P2\\":[],\\"P3\\":[],\\"extra\\":[]}"}}]}'

        with mock.patch("orchestrator.llm_providers.openai_compatible.urllib.request.urlopen", return_value=_Response()):
            response = provider.complete(_request())

        self.assertFalse(response.ok)
        self.assertEqual("invalid_response", response.error_category)
        self.assertIn("unsupported top-level keys: extra", response.error_message or "")


class OpenAIResponsesProviderTests(unittest.TestCase):
    def test_openai_responses_minimal_success_path(self):
        provider = OpenAIResponsesProvider(_responses_provider_config())
        request = LLMRequest(
            system_prompt="Return strict JSON only.",
            user_prompt="Pick paths.",
            model="gpt-4.1-mini",
            schema_mode="native_structured",
            timeout_sec=30,
        )

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

            def getcode(self):
                return 200

            def read(self):
                return (
                    b'{"output":[{"type":"message","content":[{"type":"output_text","text":"{\\"P0\\":[\\"/app/a.so\\"],\\"P1\\":[],\\"P2\\":[],\\"P3\\":[]}"}]}]}'
                )

        with mock.patch("orchestrator.llm_providers.openai_responses.urllib.request.urlopen", return_value=_Response()):
            response = provider.complete(request)

        self.assertTrue(response.ok)
        self.assertEqual("native_structured", response.schema_mode)
        self.assertEqual(["/app/a.so"], response.structured_output["P0"])


if __name__ == "__main__":
    unittest.main()
