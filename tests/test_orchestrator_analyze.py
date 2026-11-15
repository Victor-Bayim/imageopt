import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from orchestrator.manifest import ManifestStore
from orchestrator.stages.analyze import run_analyze_stage
from orchestrator.stages import analyze as analyze_module


ROOT = Path(__file__).resolve().parents[1]


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_metrics(path, metrics):
    with open(path, "w", encoding="utf-8") as f:
        for name, value in metrics.items():
            f.write(f"{name} {value}\n")


class AnalyzeStageTests(unittest.TestCase):
    def test_materialize_image_toc_uses_manifest_image_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            ManifestStore.create(
                work_dir / "manifest.json",
                run_id="20260323_000000",
                work_dir=work_dir,
                image="docker.io/library/app:latest",
                startup_mode="legacy",
                llm_enabled=False,
            )
            stage_log_path = work_dir / "analyze.log"
            stage_log_path.write_text("", encoding="utf-8")

            def fake_run_tool(argv, *, repo_root, stage_log_path):
                self.assertEqual("python3", argv[0])
                self.assertIn("--image", argv)
                self.assertIn("docker.io/library/app:latest", argv)
                out_path = Path(argv[argv.index("--out") + 1])
                out_path.write_text('{"entries":[],"layers":[]}\n', encoding="utf-8")
                return SimpleNamespace(returncode=0)

            with mock.patch.object(analyze_module, "_run_tool", side_effect=fake_run_tool):
                toc_path, warning = analyze_module._maybe_materialize_image_toc(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    stage_log_path=stage_log_path,
                )

            self.assertIsNone(warning)
            self.assertEqual(work_dir / "image_toc.json", toc_path)
            self.assertTrue(toc_path.exists())

    def test_analyze_stage_passes_self_starting_service_flag_to_quality_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            (work_dir / "runtime_state.json").write_text(
                json.dumps(
                    {
                        "container_name": "self",
                        "container_pid": 1,
                        "self_starting_service": True,
                        "startup_mode_requested": "legacy",
                        "startup_mode_used": "legacy",
                        "startup_backend": "legacy",
                        "event_sink": "file_sink",
                        "raw_events": str(work_dir / "raw_events.jsonl"),
                        "sampler_log": str(work_dir / "sampler.log"),
                        "sampler_metrics": str(work_dir / "sampler_metrics.prom"),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            write_jsonl(work_dir / "raw_events.jsonl", [{"type": "OPEN", "path": "/app/a.so", "dev": 1, "ino": 11}])
            write_jsonl(work_dir / "events.jsonl", [{"type": "OPEN", "path": "/app/a.so", "dev": 1, "ino": 11}])
            write_jsonl(
                work_dir / "events_enriched.jsonl",
                [{"type": "OPEN", "path": "/app/a.so", "dev": 1, "ino": 11, "via": "open"}],
            )
            write_metrics(
                work_dir / "sampler_metrics.prom",
                {
                    "file_events_total": 1,
                    "ringbuf_drops_total": 0,
                    "filtered_out_total": 0,
                    "sampling_drops_total": 0,
                    "file_sink_lines_total": 1,
                    "file_sink_bytes_total": 32,
                    "file_sink_write_failures_total": 0,
                    "redis_queue_dropped_total": 0,
                    "redis_publish_failures_total": 0,
                },
            )
            (work_dir / "sampler.log").write_text("", encoding="utf-8")

            observed = {"quality_gate_argv": None}

            def fake_run_tool(argv, *, repo_root, stage_log_path):
                if "quality_gate.py" in argv[1]:
                    observed["quality_gate_argv"] = list(argv)
                    out_path = Path(argv[argv.index("--out") + 1])
                    out_path.write_text(
                        json.dumps(
                            {
                                "ok": True,
                                "warnings": [],
                                "reasons": [],
                                "adaptations_applied": [],
                                "counts": {"total": 1},
                                "identity": {},
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    return SimpleNamespace(returncode=0)
                if "aggregate_candidates.py" in argv[1]:
                    Path(argv[2]).write_text(
                        json.dumps({"path": "/app/a.so"}, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
                    stats_path = Path(argv[argv.index("--stats-json") + 1])
                    stats_path.write_text(json.dumps({}, ensure_ascii=False) + "\n", encoding="utf-8")
                    return SimpleNamespace(returncode=0)
                if "select_prefetch_paths.py" in argv[1]:
                    Path(argv[argv.index("--out-yaml") + 1]).write_text("P0:\n  - /app/a.so\nP1:\nP2:\nP3:\n", encoding="utf-8")
                    Path(argv[argv.index("--out-list") + 1]).write_text("/app/a.so\n", encoding="utf-8")
                    Path(argv[argv.index("--stats-json") + 1]).write_text(
                        json.dumps({"chosen": 1, "preferred_count": 0}, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
                    return SimpleNamespace(returncode=0)
                raise AssertionError(f"unexpected argv: {argv}")

            with mock.patch.object(analyze_module, "_run_tool", side_effect=fake_run_tool):
                result = run_analyze_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    min_events=1,
                    size_budget_mb=1,
                )

            self.assertTrue(result.quality_payload["ok"])
            self.assertIsNotNone(observed["quality_gate_argv"])
            argv = observed["quality_gate_argv"]
            self.assertIn("--self-starting-service", argv)
            self.assertEqual("1", argv[argv.index("--self-starting-service") + 1])

    def test_analyze_stage_produces_deterministic_hints(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            (work_dir / "runtime_state.json").write_text(
                json.dumps(
                    {
                        "container_name": "self",
                        "container_pid": 1,
                        "startup_mode_requested": "legacy",
                        "startup_mode_used": "legacy",
                        "startup_backend": "legacy",
                        "event_sink": "file_sink",
                        "raw_events": str(work_dir / "raw_events.jsonl"),
                        "sampler_log": str(work_dir / "sampler.log"),
                        "sampler_metrics": str(work_dir / "sampler_metrics.prom"),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            write_jsonl(
                work_dir / "raw_events.jsonl",
                [
                    {"type": "OPEN", "path": "/app/a.so", "dev": 1, "ino": 11, "ts_ns": 1},
                    {"type": "MMAP", "dev": 1, "ino": 11, "ts_ns": 2},
                ],
            )
            write_jsonl(
                work_dir / "events.jsonl",
                [
                    {
                        "type": "OPEN",
                        "path": "/app/a.so",
                        "dev": 1,
                        "ino": 11,
                        "size_bytes": 4096,
                        "devino_source": "stat",
                        "path_resolution": "cwd",
                        "path_resolution_risk": "dirfd_unavailable",
                        "ts_ns": 1,
                        "raw_index": 1,
                    }
                ],
            )
            write_jsonl(
                work_dir / "events_enriched.jsonl",
                [
                    {
                        "type": "OPEN",
                        "path": "/app/a.so",
                        "dev": 1,
                        "ino": 11,
                        "size_bytes": 4096,
                        "via": "open",
                        "devino_source": "stat",
                        "path_resolution": "cwd",
                        "path_resolution_risk": "dirfd_unavailable",
                        "ts_ns": 1,
                        "raw_index": 1,
                    },
                    {
                        "type": "MMAP",
                        "path": "/app/a.so",
                        "dev": 1,
                        "ino": 11,
                        "size_bytes": 4096,
                        "via": "dict",
                        "devino_source": "mmap_raw",
                        "ts_ns": 2,
                        "raw_index": 2,
                    },
                ],
            )
            write_metrics(
                work_dir / "sampler_metrics.prom",
                {
                    "file_events_total": 2,
                    "ringbuf_drops_total": 0,
                    "filtered_out_total": 0,
                    "sampling_drops_total": 0,
                    "file_sink_lines_total": 2,
                    "file_sink_bytes_total": 64,
                    "file_sink_write_failures_total": 0,
                    "redis_queue_dropped_total": 0,
                    "redis_publish_failures_total": 0,
                },
            )
            (work_dir / "sampler.log").write_text("", encoding="utf-8")

            result = run_analyze_stage(
                repo_root=ROOT,
                work_dir=work_dir,
                min_events=2,
                size_budget_mb=1,
            )

            quality_payload = json.loads(result.quality_gate_path.read_text(encoding="utf-8"))
            selector_stats = json.loads(result.selector_stats_path.read_text(encoding="utf-8"))
            candidates = json.loads(result.candidates_path.read_text(encoding="utf-8"))
            aggregate_stats = json.loads(result.aggregate_stats_path.read_text(encoding="utf-8"))

            self.assertTrue(quality_payload["ok"])
            self.assertEqual("warning", quality_payload["quality_status"])
            self.assertEqual("benchmark_before_publish", quality_payload["recommendation"])
            self.assertIn("candidate pool is small: 1", quality_payload["warnings"])
            self.assertEqual([], quality_payload["blockers"])
            self.assertEqual("legacy", quality_payload["startup_backend"])
            self.assertEqual(1, aggregate_stats["paths_with_open_stat_devino"])
            self.assertEqual(1, aggregate_stats["paths_with_mmap_raw_devino"])
            self.assertEqual(1, aggregate_stats["paths_with_open_resolved_from_cwd"])
            self.assertEqual(1, aggregate_stats["paths_with_open_cwd_dirfd_ambiguous"])
            self.assertEqual(1, quality_payload["identity"]["paths_with_open_stat_devino"])
            self.assertEqual(1, quality_payload["identity"]["paths_with_mmap_raw_devino"])
            self.assertEqual(1, quality_payload["identity"]["paths_with_open_resolved_from_cwd"])
            self.assertEqual(1, quality_payload["identity"]["paths_with_open_cwd_dirfd_ambiguous"])
            self.assertGreaterEqual(selector_stats["chosen"], 1)
            self.assertEqual(0, selector_stats["preferred_count"])
            self.assertEqual(1, selector_stats["identity_paths_with_open_resolved_from_cwd"])
            self.assertEqual(1, selector_stats["identity_paths_with_open_cwd_dirfd_ambiguous"])
            self.assertEqual("/app/a.so\n", result.estargz_list_path.read_text(encoding="utf-8"))
            self.assertEqual(
                result.ranked_path.read_text(encoding="utf-8"),
                result.hints_path.read_text(encoding="utf-8"),
            )
            self.assertEqual("/app/a.so", candidates[0]["path"])
            self.assertEqual(1, candidates[0]["open_devino_stat_count"])
            self.assertEqual(1, candidates[0]["mmap_devino_raw_count"])

    def test_analyze_stage_budget_trim_is_advisory_when_hints_remain_nonempty(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            (work_dir / "runtime_state.json").write_text(
                json.dumps(
                    {
                        "container_name": "self",
                        "container_pid": 1,
                        "startup_mode_requested": "legacy",
                        "startup_mode_used": "legacy",
                        "startup_backend": "legacy",
                        "event_sink": "file_sink",
                        "raw_events": str(work_dir / "raw_events.jsonl"),
                        "sampler_log": str(work_dir / "sampler.log"),
                        "sampler_metrics": str(work_dir / "sampler_metrics.prom"),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            write_jsonl(work_dir / "raw_events.jsonl", [{"type": "OPEN", "path": "/app/a.so", "dev": 1, "ino": 11}])
            write_jsonl(work_dir / "events.jsonl", [{"type": "OPEN", "path": "/app/a.so", "dev": 1, "ino": 11}])
            write_jsonl(
                work_dir / "events_enriched.jsonl",
                [{"type": "OPEN", "path": "/app/a.so", "dev": 1, "ino": 11, "via": "open"}],
            )
            write_metrics(
                work_dir / "sampler_metrics.prom",
                {
                    "file_events_total": 1,
                    "ringbuf_drops_total": 0,
                    "filtered_out_total": 0,
                    "sampling_drops_total": 0,
                    "file_sink_lines_total": 1,
                    "file_sink_bytes_total": 32,
                    "file_sink_write_failures_total": 0,
                    "redis_queue_dropped_total": 0,
                    "redis_publish_failures_total": 0,
                },
            )
            (work_dir / "sampler.log").write_text("", encoding="utf-8")

            def fake_run_tool(argv, *, repo_root, stage_log_path):
                if "quality_gate.py" in argv[1]:
                    out_path = Path(argv[argv.index("--out") + 1])
                    out_path.write_text(
                        json.dumps(
                            {
                                "ok": True,
                                "warnings": [],
                                "reasons": [],
                                "counts": {"total": 1},
                                "identity": {"unique_paths": 2},
                                "ratios": {"aggregate_paths_with_devino_ratio": 0.75},
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    return SimpleNamespace(returncode=0)
                if "aggregate_candidates.py" in argv[1]:
                    Path(argv[2]).write_text(
                        json.dumps({"path": "/app/a.so"}, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
                    stats_path = Path(argv[argv.index("--stats-json") + 1])
                    stats_path.write_text(
                        json.dumps({"unique_paths": 2}, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
                    return SimpleNamespace(returncode=0)
                if "select_prefetch_paths.py" in argv[1]:
                    Path(argv[argv.index("--out-yaml") + 1]).write_text("P0:\n  - /app/a.so\nP1: []\nP2: []\nP3: []\n", encoding="utf-8")
                    Path(argv[argv.index("--out-list") + 1]).write_text("/app/a.so\n", encoding="utf-8")
                    Path(argv[argv.index("--stats-json") + 1]).write_text(
                        json.dumps(
                            {
                                "chosen": 1,
                                "preferred_count": 0,
                                "budget_skipped": 2,
                                "budget_bytes": 4096,
                                "chosen_bytes": 4096,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    return SimpleNamespace(returncode=0)
                raise AssertionError(f"unexpected argv: {argv}")

            with mock.patch.object(analyze_module, "_run_tool", side_effect=fake_run_tool):
                result = run_analyze_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    min_events=1,
                    size_budget_mb=1,
                )

            quality_payload = json.loads(result.quality_gate_path.read_text(encoding="utf-8"))
            self.assertEqual("warning", quality_payload["quality_status"])
            self.assertEqual("review_hints", quality_payload["recommendation"])
            self.assertIn(
                "selector trimmed 2 lower-priority path(s) at the budget boundary",
                quality_payload["warnings"],
            )
            self.assertEqual([], quality_payload["blockers"])

    def test_analyze_stage_writes_toc_shadow_when_image_toc_is_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            (work_dir / "runtime_state.json").write_text(
                json.dumps(
                    {
                        "container_name": "self",
                        "container_pid": 1,
                        "startup_mode_requested": "legacy",
                        "startup_mode_used": "legacy",
                        "startup_backend": "legacy",
                        "event_sink": "file_sink",
                        "raw_events": str(work_dir / "raw_events.jsonl"),
                        "sampler_log": str(work_dir / "sampler.log"),
                        "sampler_metrics": str(work_dir / "sampler_metrics.prom"),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            write_jsonl(
                work_dir / "raw_events.jsonl",
                [
                    {"type": "OPEN", "path": "/app/a.so", "dev": 1, "ino": 11, "ts_ns": 1},
                    {"type": "MMAP", "dev": 1, "ino": 11, "ts_ns": 2},
                ],
            )
            write_jsonl(
                work_dir / "events.jsonl",
                [
                    {
                        "type": "OPEN",
                        "path": "/app/a.so",
                        "dev": 1,
                        "ino": 11,
                        "size_bytes": 4096,
                        "devino_source": "stat",
                        "ts_ns": 1,
                        "raw_index": 1,
                    }
                ],
            )
            write_jsonl(
                work_dir / "events_enriched.jsonl",
                [
                    {
                        "type": "OPEN",
                        "path": "/app/a.so",
                        "dev": 1,
                        "ino": 11,
                        "size_bytes": 4096,
                        "via": "open",
                        "devino_source": "stat",
                        "ts_ns": 1,
                        "raw_index": 1,
                    },
                    {
                        "type": "MMAP",
                        "path": "/app/a.so",
                        "dev": 1,
                        "ino": 11,
                        "size_bytes": 4096,
                        "via": "mapfiles",
                        "devino_source": "mmap_raw",
                        "ts_ns": 2,
                        "raw_index": 2,
                    },
                ],
            )
            write_metrics(
                work_dir / "sampler_metrics.prom",
                {
                    "file_events_total": 2,
                    "ringbuf_drops_total": 0,
                    "filtered_out_total": 0,
                    "sampling_drops_total": 0,
                    "file_sink_lines_total": 2,
                    "file_sink_bytes_total": 64,
                    "file_sink_write_failures_total": 0,
                    "redis_queue_dropped_total": 0,
                    "redis_publish_failures_total": 0,
                },
            )
            (work_dir / "sampler.log").write_text("", encoding="utf-8")
            (work_dir / "image_toc.json").write_text(
                json.dumps(
                    {
                        "layers": [
                            {
                                "digest": "sha256:layer1",
                                "toc_source": "inline",
                                "toc_digest_verified": False,
                                "entry_count": 1,
                            }
                        ],
                        "entries": [
                            {
                                "path": "/app/a.so",
                                "layer_digest": "sha256:layer1",
                                "chunks": [
                                    {"offset": 0, "size": 4096, "compressed_size": 1024},
                                ],
                            }
                        ],
                        "warnings": ["sha256:layer1: TOC digest annotation mismatch"],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            result = run_analyze_stage(
                repo_root=ROOT,
                work_dir=work_dir,
                min_events=2,
                size_budget_mb=1,
            )

            self.assertIsNotNone(result.toc_shadow_path)
            self.assertIsNotNone(result.chunk_selector_shadow_path)
            payload = json.loads(result.toc_shadow_path.read_text(encoding="utf-8"))
            chunk_payload = json.loads(result.chunk_selector_shadow_path.read_text(encoding="utf-8"))
            self.assertEqual(1, payload["selected_paths_total"])
            self.assertEqual(1, payload["selected_paths_found"])
            self.assertEqual(1, payload["selected_unique_chunk_count"])
            self.assertEqual(1024, payload["selected_estimated_compressed_bytes"])
            self.assertEqual(1, chunk_payload["selected_paths_found"])
            self.assertEqual(0, chunk_payload["selected_zero_incremental_paths"])
            self.assertEqual([], chunk_payload["replacement_suggestions_preview"])
            self.assertIn("image TOC contains 1 warning(s)", result.warnings)
            self.assertIn("image TOC contains 1 layer(s) with unverified TOC digest", result.warnings)

    def test_analyze_stage_emits_experimental_chunk_shadow_selector_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            (work_dir / "runtime_state.json").write_text(
                json.dumps(
                    {
                        "container_name": "self",
                        "container_pid": 1,
                        "startup_mode_requested": "legacy",
                        "startup_mode_used": "legacy",
                        "startup_backend": "legacy",
                        "event_sink": "file_sink",
                        "raw_events": str(work_dir / "raw_events.jsonl"),
                        "sampler_log": str(work_dir / "sampler.log"),
                        "sampler_metrics": str(work_dir / "sampler_metrics.prom"),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            write_jsonl(
                work_dir / "raw_events.jsonl",
                [
                    {"type": "OPEN", "path": "/app/a.so", "dev": 1, "ino": 11, "ts_ns": 1},
                    {"type": "MMAP", "dev": 1, "ino": 11, "ts_ns": 2},
                ],
            )
            write_jsonl(
                work_dir / "events.jsonl",
                [
                    {
                        "type": "OPEN",
                        "path": "/app/a.so",
                        "dev": 1,
                        "ino": 11,
                        "size_bytes": 4096,
                        "devino_source": "stat",
                        "ts_ns": 1,
                        "raw_index": 1,
                    }
                ],
            )
            write_jsonl(
                work_dir / "events_enriched.jsonl",
                [
                    {
                        "type": "OPEN",
                        "path": "/app/a.so",
                        "dev": 1,
                        "ino": 11,
                        "size_bytes": 4096,
                        "via": "open",
                        "devino_source": "stat",
                        "ts_ns": 1,
                        "raw_index": 1,
                    },
                    {
                        "type": "MMAP",
                        "path": "/app/a.so",
                        "dev": 1,
                        "ino": 11,
                        "size_bytes": 4096,
                        "via": "mapfiles",
                        "devino_source": "mmap_raw",
                        "ts_ns": 2,
                        "raw_index": 2,
                    },
                ],
            )
            write_metrics(
                work_dir / "sampler_metrics.prom",
                {
                    "file_events_total": 2,
                    "ringbuf_drops_total": 0,
                    "filtered_out_total": 0,
                    "sampling_drops_total": 0,
                    "file_sink_lines_total": 2,
                    "file_sink_bytes_total": 64,
                    "file_sink_write_failures_total": 0,
                    "redis_queue_dropped_total": 0,
                    "redis_publish_failures_total": 0,
                },
            )
            (work_dir / "sampler.log").write_text("", encoding="utf-8")
            (work_dir / "image_toc.json").write_text(
                json.dumps(
                    {
                        "entries": [
                            {
                                "path": "/app/a.so",
                                "layer_digest": "sha256:layer1",
                                "chunks": [
                                    {"offset": 0, "size": 4096, "compressed_size": 1024},
                                ],
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            result = run_analyze_stage(
                repo_root=ROOT,
                work_dir=work_dir,
                min_events=2,
                size_budget_mb=1,
                selector_mode="chunk-shadow",
            )

            self.assertIsNotNone(result.chunk_shadow_ranked_path)
            self.assertIsNotNone(result.chunk_shadow_estargz_list_path)
            self.assertIsNotNone(result.chunk_shadow_hints_path)
            self.assertIsNotNone(result.chunk_shadow_selector_stats_path)
            selector_stats = json.loads(result.chunk_shadow_selector_stats_path.read_text(encoding="utf-8"))
            self.assertEqual("chunk-shadow", selector_stats["selector_mode"])
            self.assertEqual(1, selector_stats["chosen"])
            self.assertEqual("/app/a.so\n", result.chunk_shadow_estargz_list_path.read_text(encoding="utf-8"))
            self.assertIn("experimental chunk-shadow selector artifacts generated", result.warnings)

    def test_analyze_stage_uses_live_container_pid_for_filesystem_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            (work_dir / "runtime_state.json").write_text(
                json.dumps(
                    {
                        "container_name": "self",
                        "container_pid": 0,
                        "live_container_pid": 4321,
                        "startup_mode_requested": "legacy",
                        "startup_mode_used": "legacy",
                        "startup_backend": "legacy",
                        "event_sink": "file_sink",
                        "raw_events": str(work_dir / "raw_events.jsonl"),
                        "sampler_log": str(work_dir / "sampler.log"),
                        "sampler_metrics": str(work_dir / "sampler_metrics.prom"),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            write_jsonl(
                work_dir / "raw_events.jsonl",
                [
                    {"type": "OPEN", "path": "/app/a.so", "dev": 1, "ino": 11, "ts_ns": 1},
                    {"type": "MMAP", "dev": 1, "ino": 11, "ts_ns": 2},
                ],
            )
            write_jsonl(
                work_dir / "events.jsonl",
                [
                    {
                        "type": "OPEN",
                        "path": "/app/a.so",
                        "dev": 1,
                        "ino": 11,
                        "size_bytes": 4096,
                        "devino_source": "stat",
                        "ts_ns": 1,
                        "raw_index": 1,
                    }
                ],
            )
            write_jsonl(
                work_dir / "events_enriched.jsonl",
                [
                    {
                        "type": "OPEN",
                        "path": "/app/a.so",
                        "dev": 1,
                        "ino": 11,
                        "size_bytes": 4096,
                        "via": "open",
                        "devino_source": "stat",
                        "ts_ns": 1,
                        "raw_index": 1,
                    },
                    {
                        "type": "MMAP",
                        "path": "/app/a.so",
                        "dev": 1,
                        "ino": 11,
                        "size_bytes": 4096,
                        "via": "dict",
                        "devino_source": "mmap_raw",
                        "ts_ns": 2,
                        "raw_index": 2,
                    },
                ],
            )
            write_metrics(
                work_dir / "sampler_metrics.prom",
                {
                    "file_events_total": 2,
                    "ringbuf_drops_total": 0,
                    "filtered_out_total": 0,
                    "sampling_drops_total": 0,
                    "file_sink_lines_total": 2,
                    "file_sink_bytes_total": 64,
                    "file_sink_write_failures_total": 0,
                    "redis_queue_dropped_total": 0,
                    "redis_publish_failures_total": 0,
                },
            )
            (work_dir / "sampler.log").write_text("", encoding="utf-8")

            with mock.patch.object(analyze_module, "_container_root_size_bytes", return_value=128 * 1024 * 1024) as root_size_mock:
                result = run_analyze_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                    min_events=2,
                    size_budget_mb=0,
                    size_budget_percent=10.0,
                )

            root_size_mock.assert_called_once_with(4321)
            self.assertEqual(128 * 1024 * 1024, result.filesystem_total_bytes)
            self.assertGreater(result.size_budget_mb_effective, 0)


if __name__ == "__main__":
    unittest.main()
