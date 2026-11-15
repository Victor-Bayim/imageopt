import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.llm_providers.base import LLMProvider
from orchestrator.llm_providers.mock import MockProvider
from orchestrator.llm_providers.types import LLMRequest, LLMResponse, ProviderConfig
from orchestrator.stages import llm_optimize as llm_stage


ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _build_work_dir(work_dir: Path, *, candidates: list[dict[str, object]] | None = None) -> None:
    effective_candidates = (
        candidates
        if candidates is not None
        else [
            {
                "path": "/app/a.so",
                "aliases": ["/app/a.so", "/layers/a.so"],
                "size_bytes": 4096,
                "weighted_count": 5,
                "weighted_open_count": 3,
                "weighted_mmap_count": 2,
                "weighted_open_with_devino_count": 3,
                "weighted_via_open_count": 3,
                "weighted_via_mapfiles_count": 0,
                "weighted_via_dict_count": 2,
                "weighted_via_maps_count": 0,
                "first_index": 1,
                "first_ratio": 0.02,
                "dev": 1,
                "ino": 11,
            }
        ]
    )
    _write_json(
        work_dir / "runtime_state.json",
        {
            "container_pid": 0,
            "startup_mode_used": "legacy",
        },
    )
    _write_json(
        work_dir / "candidates.json",
        effective_candidates,
    )
    candidate_count = len(effective_candidates)
    _write_json(
        work_dir / "aggregate_stats.json",
        {
            "identity_candidates": candidate_count,
            "identity_alias_groups": 0,
            "identity_merged_paths": 0,
            "devino_identity_candidates": candidate_count,
        },
    )
    _write_json(
        work_dir / "selector_stats_default.json",
        {
            "identity_guard_mode": "normal",
            "identity_guard_reason": "identity coverage is sufficient",
            "identity_paths_with_devino_ratio": 0.80,
            "effective_early_onehit_max": 8,
            "max_maps_ratio_without_strong_evidence": 0.80,
        },
    )
    (work_dir / "ranked.yaml").write_text("P0:\n  - /app/a.so\nP1: []\nP2: []\nP3: []\n", encoding="utf-8")
    (work_dir / "hints.yaml").write_text("", encoding="utf-8")
    (work_dir / "estargz.list").write_text("/app/a.so\n", encoding="utf-8")
    (work_dir / "selector_stats_hints.json").write_text("{}\n", encoding="utf-8")


def _mock_provider_config() -> ProviderConfig:
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


class _StaticProvider(LLMProvider):
    def __init__(self, response: LLMResponse) -> None:
        self._response = response

    def complete(self, request: LLMRequest) -> LLMResponse:
        del request
        return self._response


class LlmOptimizeStageTests(unittest.TestCase):
    def test_auto_prompt_bundle_is_json_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            _build_work_dir(work_dir)

            result = llm_stage.run_llm_optimize_stage(
                repo_root=ROOT,
                work_dir=work_dir,
                llm_provider="mock",
            )

            prompt_text = result.prompt_path.read_text(encoding="utf-8")
            web_input_text = result.web_input_path.read_text(encoding="utf-8")
            body = json.loads(result.body_path.read_text(encoding="utf-8"))
            system_prompt = body["messages"][0]["content"]
            user_prompt = body["messages"][1]["content"]

            self.assertNotIn("yaml", prompt_text.lower())
            self.assertNotIn("```", prompt_text)
            self.assertIn('{"P0":[],"P1":[],"P2":[],"P3":[]}', prompt_text)
            self.assertIn("严格 JSON 对象", prompt_text)

            self.assertNotIn("yaml", web_input_text.lower())
            self.assertIn("Do not output Markdown.", web_input_text)
            self.assertIn("Do not output code fences.", web_input_text)
            self.assertIn("Do not output explanations.", web_input_text)

            self.assertNotIn("yaml", system_prompt.lower())
            self.assertNotIn("yaml", user_prompt.lower())
            self.assertIn('{"P0":[],"P1":[],"P2":[],"P3":[]}', system_prompt)
            self.assertIn('{"P0":[],"P1":[],"P2":[],"P3":[]}', user_prompt)

    def test_manual_prompt_bundle_requires_json_contract_with_task_background(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            _build_work_dir(work_dir)

            result = llm_stage.run_llm_optimize_stage(
                repo_root=ROOT,
                work_dir=work_dir,
                force_manual_export=True,
            )

            prompt_text = result.prompt_path.read_text(encoding="utf-8")
            web_input_text = result.web_input_path.read_text(encoding="utf-8")
            manual_instructions = result.manual_instructions_path.read_text(encoding="utf-8")

            self.assertEqual("manual_export", result.mode)
            self.assertIn("eStargz prefetch list 优化助手", prompt_text)
            self.assertIn('{"P0":[],"P1":[],"P2":[],"P3":[]}', prompt_text)
            self.assertNotIn("请产出一个 YAML", prompt_text)
            self.assertIn("strict JSON object", web_input_text)
            self.assertIn("legacy YAML", manual_instructions)
            self.assertIn("```json", manual_instructions)

    def test_api_mode_applies_grounded_hints_with_mock_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            _build_work_dir(work_dir)

            result = llm_stage.run_llm_optimize_stage(
                repo_root=ROOT,
                work_dir=work_dir,
                llm_provider="mock",
            )

            self.assertEqual("api", result.mode)
            self.assertEqual("llm-auto-success", result.mode_result)
            self.assertTrue(result.llm_applied)
            self.assertIn("/app/a.so", result.hints_path.read_text(encoding="utf-8"))
            llm_result = json.loads(result.result_path.read_text(encoding="utf-8"))
            self.assertEqual("mock", llm_result["provider"])
            self.assertEqual("mock", llm_result["provider_type"])
            self.assertEqual("raw_text", llm_result["schema_mode"])
            self.assertEqual("ok", llm_result["transport_status"])
            self.assertEqual(200, llm_result["http_status"])
            self.assertIsNone(llm_result["error_category"])

    def test_auto_response_is_order_deduped_and_capped_before_grounding(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            _build_work_dir(work_dir)
            payload = {
                "P0": ["/app/a.so"] + [f"/app/p0_{index}.so" for index in range(1, 12)],
                "P1": ["/app/a.so"] + [f"/app/p1_{index}.so" for index in range(1, 20)],
                "P2": ["/app/p1_1.so", "/app/p2_1.so"],
                "P3": ["/app/p2_1.so", "/app/p3_1.so"],
            }
            response = LLMResponse(
                ok=True,
                provider_type="openai_compatible",
                schema_mode="json_prompted",
                transport_status="ok",
                http_status=200,
                raw_text=json.dumps(payload, ensure_ascii=False),
                raw_payload={"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]},
                structured_output=payload,
            )
            with mock.patch.object(llm_stage.ProviderRouter, "build", return_value=_StaticProvider(response)):
                with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "demo-key"}, clear=False):
                    result = llm_stage.run_llm_optimize_stage(
                        repo_root=ROOT,
                        work_dir=work_dir,
                    )

            normalized_payload = llm_stage._load_manual_response_payload(
                result.hints_raw_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                ["/app/a.so"] + [f"/app/p0_{index}.so" for index in range(1, 8)],
                normalized_payload["P0"],
            )
            self.assertEqual(16, len(normalized_payload["P1"]))
            self.assertEqual(["/app/p2_1.so"], normalized_payload["P2"])
            self.assertEqual(["/app/p3_1.so"], normalized_payload["P3"])

    def test_invalid_response_falls_back_to_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            _build_work_dir(work_dir)
            provider = MockProvider(_mock_provider_config(), mode="invalid_response")

            with mock.patch.object(llm_stage.ProviderRouter, "build", return_value=provider):
                result = llm_stage.run_llm_optimize_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    llm_provider="mock",
                )

            self.assertEqual("api_fallback", result.mode)
            self.assertEqual("invalid_response", result.fallback_kind)
            self.assertFalse(result.llm_applied)
            llm_result = json.loads(result.result_path.read_text(encoding="utf-8"))
            self.assertEqual("invalid_response", llm_result["fallback_kind"])
            self.assertEqual("invalid_response", llm_result["error_category"])
            self.assertEqual("deterministic_ranked", llm_result["final_hints_source"])

    def test_fenced_yaml_response_still_falls_back_as_invalid_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            _build_work_dir(work_dir)
            raw_text = (
                "```yaml\n"
                "P0:\n"
                "- /app/a.so\n"
                "P1: []\n"
                "P2: []\n"
                "P3: []\n"
                "```"
            )
            response = LLMResponse(
                ok=False,
                provider_type="openai_compatible",
                schema_mode="json_prompted",
                transport_status="ok",
                http_status=200,
                raw_text=raw_text,
                raw_payload={"choices": [{"message": {"content": raw_text}}]},
                structured_output=None,
                error_category="invalid_response",
                error_message="schema validation failed: JSON parse failed: Expecting value: line 1 column 1 (char 0)",
            )
            with mock.patch.object(llm_stage.ProviderRouter, "build", return_value=_StaticProvider(response)):
                with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "demo-key"}, clear=False):
                    result = llm_stage.run_llm_optimize_stage(
                        repo_root=ROOT,
                        work_dir=work_dir,
                    )

            self.assertEqual("api_fallback", result.mode)
            self.assertEqual("invalid_response", result.fallback_kind)
            self.assertIn("```yaml", result.hints_raw_path.read_text(encoding="utf-8"))
            llm_result = json.loads(result.result_path.read_text(encoding="utf-8"))
            self.assertEqual("invalid_response", llm_result["fallback_kind"])
            self.assertEqual("invalid_response", llm_result["error_category"])

    def test_empty_structured_output_with_non_empty_candidates_is_invalid_response_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            _build_work_dir(work_dir)
            empty_payload = {"P0": [], "P1": [], "P2": [], "P3": []}
            response = LLMResponse(
                ok=True,
                provider_type="openai_compatible",
                schema_mode="json_prompted",
                transport_status="ok",
                http_status=200,
                raw_text=json.dumps(empty_payload, ensure_ascii=False),
                raw_payload={"choices": [{"message": {"content": json.dumps(empty_payload, ensure_ascii=False)}}]},
                structured_output=empty_payload,
            )
            with mock.patch.object(llm_stage.ProviderRouter, "build", return_value=_StaticProvider(response)):
                with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "demo-key"}, clear=False):
                    result = llm_stage.run_llm_optimize_stage(
                        repo_root=ROOT,
                        work_dir=work_dir,
                    )

            self.assertEqual("api_fallback", result.mode)
            self.assertEqual("invalid_response", result.fallback_kind)
            self.assertIn("empty/unhelpful", result.fallback_reason or "")
            llm_result = json.loads(result.result_path.read_text(encoding="utf-8"))
            self.assertEqual("invalid_response", llm_result["fallback_kind"])
            self.assertEqual("invalid_response", llm_result["error_category"])
            self.assertIn("empty/unhelpful", llm_result["fallback_reason"] or "")

    def test_empty_structured_output_with_empty_candidates_is_not_misclassified_as_invalid_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            _build_work_dir(work_dir, candidates=[])
            empty_payload = {"P0": [], "P1": [], "P2": [], "P3": []}
            response = LLMResponse(
                ok=True,
                provider_type="openai_compatible",
                schema_mode="json_prompted",
                transport_status="ok",
                http_status=200,
                raw_text=json.dumps(empty_payload, ensure_ascii=False),
                raw_payload={"choices": [{"message": {"content": json.dumps(empty_payload, ensure_ascii=False)}}]},
                structured_output=empty_payload,
            )
            with mock.patch.object(llm_stage.ProviderRouter, "build", return_value=_StaticProvider(response)):
                with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "demo-key"}, clear=False):
                    result = llm_stage.run_llm_optimize_stage(
                        repo_root=ROOT,
                        work_dir=work_dir,
                    )

            self.assertEqual("api_fallback", result.mode)
            self.assertNotEqual("invalid_response", result.fallback_kind)
            llm_result = json.loads(result.result_path.read_text(encoding="utf-8"))
            self.assertNotEqual("invalid_response", llm_result["fallback_kind"])
            self.assertNotEqual("invalid_response", llm_result["error_category"])

    def test_auth_or_config_error_hard_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            _build_work_dir(work_dir)
            response = LLMResponse(
                ok=False,
                provider_type="openai_compatible",
                schema_mode="json_prompted",
                transport_status="http_error",
                http_status=401,
                raw_text="LLM HTTP 401",
                raw_payload={"error": {"message": "bad auth"}},
                structured_output=None,
                error_category="auth_or_config",
                error_message="LLM HTTP 401",
            )
            with mock.patch.object(llm_stage.ProviderRouter, "build", return_value=_StaticProvider(response)):
                with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "demo-key"}, clear=False):
                    with self.assertRaisesRegex(RuntimeError, "LLM HTTP 401"):
                        llm_stage.run_llm_optimize_stage(
                            repo_root=ROOT,
                            work_dir=work_dir,
                        )

    def test_missing_api_key_hard_fails_for_auto_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            _build_work_dir(work_dir)
            with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False):
                with self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY"):
                    llm_stage.run_llm_optimize_stage(
                        repo_root=ROOT,
                        work_dir=work_dir,
                    )

    def test_manual_response_file_can_be_imported_without_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            _build_work_dir(work_dir)
            manual_response = work_dir / "manual_response.yaml"
            manual_response.write_text(
                '{\n  "P0": ["/app/a.so"],\n  "P1": [],\n  "P2": [],\n  "P3": []\n}\n',
                encoding="utf-8",
            )

            result = llm_stage.run_llm_optimize_stage(
                repo_root=ROOT,
                work_dir=work_dir,
                manual_response_file=str(manual_response),
            )

            self.assertEqual("manual_response", result.mode)
            self.assertEqual("llm-manual-completed", result.mode_result)
            self.assertTrue(result.llm_applied)
            llm_result = json.loads(result.result_path.read_text(encoding="utf-8"))
            self.assertEqual(str(manual_response.resolve()), llm_result["manual_response_file"])
            self.assertEqual("grounded_llm", llm_result["final_hints_source"])

    def test_legacy_yaml_manual_response_file_is_still_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            _build_work_dir(work_dir)
            manual_response = work_dir / "manual_response.yaml"
            manual_response.write_text("P0:\n  - /app/a.so\nP1: []\nP2: []\nP3: []\n", encoding="utf-8")

            result = llm_stage.run_llm_optimize_stage(
                repo_root=ROOT,
                work_dir=work_dir,
                manual_response_file=str(manual_response),
            )

            self.assertEqual("manual_response", result.mode)
            self.assertTrue(result.llm_applied)

    def test_manual_response_materializes_grounded_prefetch_list_into_formal_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            manual_paths = [f"/app/manual_{index:02d}.so" for index in range(21)]
            candidates = [
                {
                    "path": path,
                    "aliases": [path],
                    "size_bytes": 4096 + index,
                    "weighted_count": 10,
                    "weighted_open_count": 10,
                    "weighted_mmap_count": 0,
                    "weighted_open_with_devino_count": 10,
                    "weighted_via_open_count": 10,
                    "weighted_via_mapfiles_count": 0,
                    "weighted_via_dict_count": 10,
                    "weighted_via_maps_count": 0,
                    "first_index": index + 1,
                    "first_ratio": 0.01,
                    "dev": 1,
                    "ino": 100 + index,
                }
                for index, path in enumerate(manual_paths)
            ]
            _build_work_dir(work_dir, candidates=candidates)
            manual_response = work_dir / "manual_response.yaml"
            manual_response.write_text(
                "P0:\n" + "".join(f"  - {path}\n" for path in manual_paths) + "P1: []\nP2: []\nP3: []\n",
                encoding="utf-8",
            )

            result = llm_stage.run_llm_optimize_stage(
                repo_root=ROOT,
                work_dir=work_dir,
                manual_response_file=str(manual_response),
            )

            llm_result = json.loads(result.result_path.read_text(encoding="utf-8"))
            self.assertEqual(21, llm_result["accepted_total"])
            self.assertEqual(manual_paths, result.estargz_list_path.read_text(encoding="utf-8").splitlines())
            selector_stats = json.loads(result.selector_stats_hint_path.read_text(encoding="utf-8"))
            self.assertEqual("grounded_llm_manual", selector_stats["selector_source"])

    def test_invalid_paths_in_llm_output_warn_when_grounding_still_keeps_valid_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            _build_work_dir(
                work_dir,
                candidates=[
                    {
                        "path": "/app/a.so",
                        "aliases": ["/app/a.so"],
                        "size_bytes": 4096,
                        "weighted_count": 6,
                        "weighted_open_count": 4,
                        "weighted_mmap_count": 2,
                        "weighted_open_with_devino_count": 4,
                        "weighted_via_open_count": 4,
                        "weighted_via_mapfiles_count": 0,
                        "weighted_via_dict_count": 2,
                        "weighted_via_maps_count": 0,
                        "first_index": 1,
                        "first_ratio": 0.01,
                        "dev": 1,
                        "ino": 11,
                    }
                ],
            )
            payload = {
                "P0": ["/app/a.so", "/proc/1/maps", "/app/*"],
                "P1": ["/app/missing.so"],
                "P2": [],
                "P3": [],
            }
            response = LLMResponse(
                ok=True,
                provider_type="openai_compatible",
                schema_mode="json_prompted",
                transport_status="ok",
                http_status=200,
                raw_text=json.dumps(payload, ensure_ascii=False),
                raw_payload={"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]},
                structured_output=payload,
            )
            with mock.patch.object(llm_stage.ProviderRouter, "build", return_value=_StaticProvider(response)):
                with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "demo-key"}, clear=False):
                    result = llm_stage.run_llm_optimize_stage(
                        repo_root=ROOT,
                        work_dir=work_dir,
                    )

            self.assertTrue(result.llm_applied)
            self.assertIn("/app/a.so", result.estargz_list_path.read_text(encoding="utf-8"))
            self.assertTrue(any("grounding discarded" in item for item in result.warnings))
            llm_result = json.loads(result.result_path.read_text(encoding="utf-8"))
            self.assertTrue(any("grounding discarded" in item for item in llm_result["warnings"]))

    def test_empty_grounding_after_invalid_paths_falls_back_to_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            _build_work_dir(work_dir)
            payload = {
                "P0": ["/proc/1/maps", "/app/missing.so"],
                "P1": [],
                "P2": [],
                "P3": [],
            }
            response = LLMResponse(
                ok=True,
                provider_type="openai_compatible",
                schema_mode="json_prompted",
                transport_status="ok",
                http_status=200,
                raw_text=json.dumps(payload, ensure_ascii=False),
                raw_payload={"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]},
                structured_output=payload,
            )
            with mock.patch.object(llm_stage.ProviderRouter, "build", return_value=_StaticProvider(response)):
                with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "demo-key"}, clear=False):
                    result = llm_stage.run_llm_optimize_stage(
                        repo_root=ROOT,
                        work_dir=work_dir,
                    )

            self.assertEqual("api_fallback", result.mode)
            self.assertFalse(result.llm_applied)
            llm_result = json.loads(result.result_path.read_text(encoding="utf-8"))
            self.assertEqual("deterministic_ranked", llm_result["final_hints_source"])

    def test_budget_trim_after_grounding_emits_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            _build_work_dir(
                work_dir,
                candidates=[
                    {
                        "path": "/app/a.so",
                        "aliases": ["/app/a.so"],
                        "size_bytes": 4096,
                        "weighted_count": 8,
                        "weighted_open_count": 5,
                        "weighted_mmap_count": 3,
                        "weighted_open_with_devino_count": 5,
                        "weighted_via_open_count": 5,
                        "weighted_via_mapfiles_count": 0,
                        "weighted_via_dict_count": 3,
                        "weighted_via_maps_count": 0,
                        "first_index": 1,
                        "first_ratio": 0.01,
                        "dev": 1,
                        "ino": 11,
                    },
                    {
                        "path": "/app/b.so",
                        "aliases": ["/app/b.so"],
                        "size_bytes": 4096,
                        "weighted_count": 7,
                        "weighted_open_count": 4,
                        "weighted_mmap_count": 3,
                        "weighted_open_with_devino_count": 4,
                        "weighted_via_open_count": 4,
                        "weighted_via_mapfiles_count": 0,
                        "weighted_via_dict_count": 3,
                        "weighted_via_maps_count": 0,
                        "first_index": 2,
                        "first_ratio": 0.02,
                        "dev": 1,
                        "ino": 12,
                    },
                ],
            )
            payload = {"P0": ["/app/a.so", "/app/b.so"], "P1": [], "P2": [], "P3": []}
            response = LLMResponse(
                ok=True,
                provider_type="openai_compatible",
                schema_mode="json_prompted",
                transport_status="ok",
                http_status=200,
                raw_text=json.dumps(payload, ensure_ascii=False),
                raw_payload={"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]},
                structured_output=payload,
            )
            with mock.patch.object(llm_stage.ProviderRouter, "build", return_value=_StaticProvider(response)):
                with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "demo-key"}, clear=False):
                    result = llm_stage.run_llm_optimize_stage(
                        repo_root=ROOT,
                        work_dir=work_dir,
                        size_budget_percent=50.0,
                    )

            self.assertTrue(result.llm_applied)
            self.assertTrue(any("selector trimmed" in item for item in result.warnings))
            selector_stats = json.loads(result.selector_stats_hint_path.read_text(encoding="utf-8"))
            self.assertEqual(1, selector_stats["chosen"])
            self.assertEqual(["/app/a.so"], result.estargz_list_path.read_text(encoding="utf-8").splitlines())


if __name__ == "__main__":
    unittest.main()
