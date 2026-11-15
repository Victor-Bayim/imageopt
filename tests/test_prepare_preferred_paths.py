import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "optimizer" / "tools" / "prepare_preferred_paths.py"


class PreparePreferredPathsTests(unittest.TestCase):
    def run_tool(self, hints_raw, candidates, selector_stats):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            hints_path = tmpdir / "hints_raw.yaml"
            candidates_path = tmpdir / "candidates.json"
            stats_path = tmpdir / "selector_stats.json"
            preferred_path = tmpdir / "preferred_paths.txt"
            audit_path = tmpdir / "grounding_audit.json"

            hints_path.write_text(hints_raw, encoding="utf-8")
            candidates_path.write_text(json.dumps(candidates), encoding="utf-8")
            stats_path.write_text(json.dumps(selector_stats), encoding="utf-8")

            proc = subprocess.run(
                [
                    "python3",
                    str(SCRIPT),
                    "--hints-raw",
                    str(hints_path),
                    "--candidates",
                    str(candidates_path),
                    "--selector-stats",
                    str(stats_path),
                    "--out",
                    str(preferred_path),
                    "--audit-json",
                    str(audit_path),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            )

            preferred = [
                line.strip()
                for line in preferred_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            return preferred, audit, proc

    def test_strict_mode_only_keeps_known_strong_candidates(self):
        candidates = [
            {
                "path": "/lib/strong.so",
                "aliases": ["/alias/strong.so"],
                "weighted_count": 4,
                "weighted_open_count": 4,
                "weighted_open_with_devino_count": 1,
                "weighted_via_open_count": 4,
                "weighted_via_mapfiles_count": 0,
                "weighted_via_dict_count": 0,
                "weighted_via_maps_count": 0,
                "maps_ratio": 0.0,
            },
            {
                "path": "/lib/weak.so",
                "weighted_count": 3,
                "weighted_open_count": 0,
                "weighted_open_with_devino_count": 0,
                "weighted_via_open_count": 0,
                "weighted_via_mapfiles_count": 0,
                "weighted_via_dict_count": 0,
                "weighted_via_maps_count": 2.7,
                "maps_ratio": 0.9,
            },
        ]
        hints_raw = """P0:
  - /alias/strong.so
  - /lib/weak.so
P1:
  - /not/in/candidates.so
  - relative/path
  - /proc/1/maps
  - /alias/strong.so
"""

        preferred, audit, _ = self.run_tool(
            hints_raw,
            candidates,
            {
                "identity_guard_mode": "strict",
                "max_maps_ratio_without_strong_evidence": 0.55,
            },
        )

        self.assertEqual(preferred, ["/alias/strong.so"])
        self.assertEqual(audit["accepted_total"], 1)
        self.assertEqual(audit["alias_paths_accepted"], 1)
        self.assertEqual(audit["guard_low_confidence_skipped"], 1)
        self.assertEqual(audit["unknown_candidate_skipped"], 1)
        self.assertEqual(audit["invalid_path_skipped"], 1)
        self.assertEqual(audit["special_path_skipped"], 1)
        self.assertEqual(audit["duplicate_skipped"], 1)

    def test_normal_mode_keeps_known_weak_candidate(self):
        candidates = [
            {
                "path": "/lib/weak.so",
                "weighted_count": 3,
                "weighted_open_count": 0,
                "weighted_open_with_devino_count": 0,
                "weighted_via_open_count": 0,
                "weighted_via_mapfiles_count": 0,
                "weighted_via_dict_count": 0,
                "weighted_via_maps_count": 2.7,
                "maps_ratio": 0.9,
            }
        ]
        hints_raw = """P0:
  - /lib/weak.so
"""

        preferred, audit, _ = self.run_tool(
            hints_raw,
            candidates,
            {
                "identity_guard_mode": "normal",
                "max_maps_ratio_without_strong_evidence": 0.8,
            },
        )

        self.assertEqual(preferred, ["/lib/weak.so"])
        self.assertEqual(audit["accepted_total"], 1)
        self.assertEqual(audit["guard_low_confidence_skipped"], 0)
        self.assertEqual(audit["identity_guard_mode"], "normal")

    def test_non_regular_candidate_is_rejected(self):
        candidates = [
            {
                "path": "/models/demo",
                "weighted_count": 4,
                "weighted_open_count": 4,
                "weighted_open_with_devino_count": 4,
                "weighted_via_open_count": 4,
                "path_kind": "directory",
            },
            {
                "path": "/models/demo/1/saved_model.pb",
                "weighted_count": 2,
                "weighted_open_count": 2,
                "weighted_via_open_count": 2,
                "path_kind": "regular",
            },
        ]
        hints_raw = """P0:
  - /models/demo
  - /models/demo/1/saved_model.pb
"""

        preferred, audit, _ = self.run_tool(
            hints_raw,
            candidates,
            {
                "identity_guard_mode": "normal",
                "max_maps_ratio_without_strong_evidence": 0.8,
            },
        )

        self.assertEqual(preferred, ["/models/demo/1/saved_model.pb"])
        self.assertEqual(audit["non_regular_candidate_skipped"], 1)

    def test_container_root_batch_check_skips_missing_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            existing = tmpdir / "present.so"
            existing.write_text("ok", encoding="utf-8")
            missing = tmpdir / "missing.so"

            hints_path = tmpdir / "hints_raw.yaml"
            candidates_path = tmpdir / "candidates.json"
            stats_path = tmpdir / "selector_stats.json"
            preferred_path = tmpdir / "preferred_paths.txt"
            audit_path = tmpdir / "grounding_audit.json"

            hints_path.write_text(
                f"P0:\n  - {existing}\n  - {missing}\n",
                encoding="utf-8",
            )
            candidates_path.write_text(
                json.dumps(
                    [
                        {"path": str(existing), "weighted_count": 2, "weighted_open_count": 2},
                        {"path": str(missing), "weighted_count": 2, "weighted_open_count": 2},
                    ]
                ),
                encoding="utf-8",
            )
            stats_path.write_text(
                json.dumps(
                    {
                        "identity_guard_mode": "normal",
                        "max_maps_ratio_without_strong_evidence": 0.8,
                    }
                ),
                encoding="utf-8",
            )

            subprocess.run(
                [
                    "python3",
                    str(SCRIPT),
                    "--hints-raw",
                    str(hints_path),
                    "--candidates",
                    str(candidates_path),
                    "--selector-stats",
                    str(stats_path),
                    "--container-pid",
                    str(os.getpid()),
                    "--out",
                    str(preferred_path),
                    "--audit-json",
                    str(audit_path),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            )

            preferred = [
                line.strip()
                for line in preferred_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            audit = json.loads(audit_path.read_text(encoding="utf-8"))

            self.assertEqual(preferred, [str(existing)])
            self.assertTrue(audit["container_root_check_enabled"])
            self.assertEqual(audit["missing_in_container_skipped"], 1)
            self.assertEqual(audit["existing_in_container_total"], 1)


if __name__ == "__main__":
    unittest.main()
