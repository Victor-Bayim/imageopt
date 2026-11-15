import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "optimizer" / "tools" / "build_llm_prompt.py"


class BuildLlmPromptTests(unittest.TestCase):
    def run_builder(self, selector_stats, *, output_contract="json"):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            candidates = tmpdir / "candidates.json"
            stats = tmpdir / "selector_stats.json"
            prompt = tmpdir / "prompt.txt"

            candidates.write_text(
                json.dumps(
                    [
                        {
                            "path": "/models/demo",
                            "size_bytes": 4096,
                            "weighted_count": 10,
                            "first_ratio": 0.01,
                            "alias_count": 1,
                            "open_ratio": 1.0,
                            "mapfiles_ratio": 0.0,
                            "dict_ratio": 0.0,
                            "maps_ratio": 0.0,
                            "open_devino_ratio": 1.0,
                            "confidence_score": 15.0,
                            "path_kind": "directory",
                        },
                        {
                            "path": "/lib/a.so",
                            "size_bytes": 4096,
                            "weighted_count": 4,
                            "first_ratio": 0.02,
                            "alias_count": 1,
                            "open_ratio": 1.0,
                            "mapfiles_ratio": 0.0,
                            "dict_ratio": 0.0,
                            "maps_ratio": 0.0,
                            "open_devino_ratio": 1.0,
                            "confidence_score": 6.2,
                            "path_kind": "regular",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            stats.write_text(json.dumps(selector_stats), encoding="utf-8")

            subprocess.run(
                [
                    "python3",
                    str(SCRIPT),
                    "--candidates",
                    str(candidates),
                    "--selector-stats",
                    str(stats),
                    "--out",
                    str(prompt),
                    "--output-contract",
                    output_contract,
                    "--budget-desc",
                    "32 MB",
                    "--min-hits",
                    "2",
                    "--p0-max",
                    "24",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            )
            return prompt.read_text(encoding="utf-8")

    def test_strict_guard_prompt_contains_conservative_instructions(self):
        prompt = self.run_builder(
            {
                "identity_guard_mode": "strict",
                "identity_guard_reason": "very low dev:ino coverage on a non-trivial sample",
                "identity_paths_with_devino_ratio": 0.05,
                "effective_early_onehit_max": 0,
                "max_maps_ratio_without_strong_evidence": 0.55,
            }
        )
        self.assertIn("identity_guard_mode: strict", prompt)
        self.assertIn("当前 deterministic selector 已禁用 early-onehit 扩展", prompt)
        self.assertIn("maps_ratio >= 0.55", prompt)
        self.assertIn('{"P0":[],"P1":[],"P2":[],"P3":[]}', prompt)
        self.assertIn("不允许输出 Markdown", prompt)
        self.assertIn("路径必须来自上面的候选列表", prompt)

    def test_normal_guard_prompt_stays_simple(self):
        prompt = self.run_builder(
            {
                "identity_guard_mode": "normal",
                "identity_guard_reason": "identity coverage is sufficient",
                "identity_paths_with_devino_ratio": 0.72,
                "effective_early_onehit_max": 8,
                "max_maps_ratio_without_strong_evidence": 0.8,
            }
        )
        self.assertIn("identity_guard_mode: normal", prompt)
        self.assertNotIn("当前样本的 identity 证据偏弱", prompt)
        self.assertNotIn("已禁用 early-onehit 扩展", prompt)
        self.assertNotIn("/models/demo |", prompt)
        self.assertIn("/lib/a.so |", prompt)
        self.assertIn("不要因为路径名字看起来重要就选", prompt)
        self.assertIn("输出前自检", prompt)


if __name__ == "__main__":
    unittest.main()
