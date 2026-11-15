import unittest

from orchestrator.llm_providers.mock import MockProvider
from orchestrator.llm_providers.types import LLMRequest, ProviderConfig


def _provider_config() -> ProviderConfig:
    return ProviderConfig(
        provider_type="mock",
        provider_name="mock",
        provider_source="explicit",
        model="mock-llm",
        model_source="explicit",
        api_base="",
        api_key_env=None,
        api_key="",
        api_key_source=None,
    )


def _request() -> LLMRequest:
    return LLMRequest(
        system_prompt="Return strict JSON only.",
        user_prompt="Pick paths.",
        model="mock-llm",
        schema_mode="raw_text",
        timeout_sec=30,
    )


class MockProviderTests(unittest.TestCase):
    def test_mock_provider_happy_path(self):
        response = MockProvider(_provider_config(), mode="success").complete(_request())
        self.assertTrue(response.ok)
        self.assertEqual(["/app/a.so"], response.structured_output["P0"])

    def test_mock_provider_supports_error_modes(self):
        cases = {
            "invalid_response": "invalid_response",
            "external_dependency": "external_dependency",
            "auth_or_config": "auth_or_config",
            "local_internal": "local_internal",
        }
        for mode, category in cases.items():
            with self.subTest(mode=mode):
                response = MockProvider(_provider_config(), mode=mode).complete(_request())
                self.assertFalse(response.ok)
                self.assertEqual(category, response.error_category)

    def test_mock_provider_can_parse_injected_raw_text(self):
        response = MockProvider(
            _provider_config(),
            raw_text='{"P0":["/app/a.so"],"P1":[],"P2":[],"P3":[]}',
        ).complete(_request())
        self.assertTrue(response.ok)
        self.assertEqual(["/app/a.so"], response.structured_output["P0"])

    def test_mock_provider_reports_invalid_injected_raw_payload(self):
        response = MockProvider(
            _provider_config(),
            raw_payload={"P0": [], "P1": [], "P2": [], "P3": [], "extra": []},
        ).complete(_request())
        self.assertFalse(response.ok)
        self.assertEqual("invalid_response", response.error_category)


if __name__ == "__main__":
    unittest.main()
