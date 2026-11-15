import unittest

from orchestrator.llm import resolve_llm_provider_config


class ProviderConfigResolutionTests(unittest.TestCase):
    def test_legacy_provider_alias_resolves_to_canonical_type_and_defaults(self):
        config = resolve_llm_provider_config(provider="groq")
        self.assertEqual("groq", config.provider)
        self.assertEqual("openai_compatible", config.provider_type)
        self.assertEqual("https://api.groq.com/openai/v1", config.api_base)
        self.assertEqual("GROQ_API_KEY", config.api_key_env)
        self.assertEqual("llama-3.1-8b-instant", config.model)

    def test_canonical_provider_fields_accept_reasoning_and_timeout(self):
        config = resolve_llm_provider_config(
            provider="openai_responses",
            model="gpt-4.1-mini",
            reasoning_effort="medium",
            timeout_sec=42,
        )
        self.assertEqual("openai_responses", config.provider)
        self.assertEqual("openai_responses", config.provider_type)
        self.assertEqual("gpt-4.1-mini", config.model)
        self.assertEqual("medium", config.reasoning_effort)
        self.assertEqual(42, config.timeout_sec)


if __name__ == "__main__":
    unittest.main()
