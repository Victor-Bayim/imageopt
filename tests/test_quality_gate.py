import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QUALITY_GATE = ROOT / "optimizer" / "tools" / "quality_gate.py"


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_metrics(path, metrics):
    with open(path, "w", encoding="utf-8") as f:
        for name, value in metrics.items():
            f.write(f"{name} {value}\n")


class QualityGateTests(unittest.TestCase):
    def run_quality_gate(
        self,
        sink_used,
        metrics_extra=None,
        sampler_log_text="",
        export_stats_payload=None,
        aggregate_stats_payload=None,
        raw_rows=None,
        event_rows=None,
        enriched_rows=None,
        startup_mode="gated",
        startup_backend=None,
        self_starting_service=False,
    ):
        metrics_extra = metrics_extra or {}
        if startup_backend is None:
            startup_backend = "gated_shell" if startup_mode == "gated" else startup_mode
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            raw = tmpdir / "raw.jsonl"
            events = tmpdir / "events.jsonl"
            enriched = tmpdir / "events_enriched.jsonl"
            metrics = tmpdir / "sampler.prom"
            sampler_log = tmpdir / "sampler.log"
            out = tmpdir / "quality.json"
            export_stats = tmpdir / "export_stats.json"
            aggregate_stats = tmpdir / "aggregate_stats.json"

            raw_rows = raw_rows or [
                {"type": "OPEN", "path": "/app/a.so", "dev": 1, "ino": 11},
                {"type": "MMAP", "path": "/app/a.so", "dev": 1, "ino": 11},
            ]
            event_rows = event_rows or [
                {
                    "type": "OPEN",
                    "path": "/app/a.so",
                    "dev": 1,
                    "ino": 11,
                    "path_resolution": "cwd",
                }
            ]
            enriched_rows = enriched_rows or [
                {
                    "type": "OPEN",
                    "path": "/app/a.so",
                    "dev": 1,
                    "ino": 11,
                    "path_resolution": "cwd",
                },
                {
                    "type": "MMAP",
                    "path": "/app/a.so",
                    "dev": 1,
                    "ino": 11,
                    "via": "mapfiles",
                },
            ]
            base_metrics = {
                "file_events_total": 2,
                "ringbuf_drops_total": 0,
                "filtered_out_total": 0,
                "sampling_drops_total": 0,
                "file_sink_lines_total": 2,
                "file_sink_bytes_total": 64,
                "file_sink_write_failures_total": 0,
                "redis_queue_dropped_total": 0,
                "redis_publish_failures_total": 0,
            }
            base_metrics.update(metrics_extra)

            write_jsonl(raw, raw_rows)
            write_jsonl(events, event_rows)
            write_jsonl(enriched, enriched_rows)
            write_metrics(metrics, base_metrics)
            sampler_log.write_text(sampler_log_text, encoding="utf-8")
            if export_stats_payload is not None:
                export_stats.write_text(
                    json.dumps(export_stats_payload, ensure_ascii=False),
                    encoding="utf-8",
                )
            if aggregate_stats_payload is not None:
                aggregate_stats.write_text(
                    json.dumps(aggregate_stats_payload, ensure_ascii=False),
                    encoding="utf-8",
                )

            cmd = [
                sys.executable,
                str(QUALITY_GATE),
                "--raw",
                str(raw),
                "--events",
                str(events),
                "--enriched",
                str(enriched),
                "--metrics",
                str(metrics),
                "--sampler-log",
                str(sampler_log),
                "--out",
                str(out),
                "--min-events",
                "2",
                "--max-mmap-maps-ratio",
                "0.5",
                "--run-id",
                "test-run",
                "--startup-mode",
                startup_mode,
                "--startup-backend",
                startup_backend,
                "--self-starting-service",
                "1" if self_starting_service else "0",
                "--event-sink-used",
                sink_used,
                "--redis-enabled",
                "1",
            ]
            if export_stats_payload is not None:
                cmd.extend(["--export-stats", str(export_stats)])
            if aggregate_stats_payload is not None:
                cmd.extend(["--aggregate-stats", str(aggregate_stats)])

            proc = subprocess.run(
                cmd,
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            payload = json.loads(out.read_text(encoding="utf-8"))
            return proc, payload

    def test_file_sink_write_failure_is_hard_fail(self):
        proc, payload = self.run_quality_gate(
            "file_sink",
            metrics_extra={"file_sink_write_failures_total": 1},
        )
        self.assertEqual(3, proc.returncode, proc.stdout + proc.stderr)
        self.assertFalse(payload["ok"])
        self.assertIn(
            "file_sink_write_failures_total=1 > 0",
            payload["reasons"],
        )

    def test_redis_secondary_path_issue_only_warns_when_file_sink_is_primary(self):
        proc, payload = self.run_quality_gate(
            "file_sink",
            metrics_extra={"redis_queue_dropped_total": 2},
        )
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertTrue(payload["ok"])
        self.assertIn(
            "redis queue dropped events=2, but local file sink remained primary",
            payload["warnings"],
        )

    def test_redis_sink_issue_is_hard_fail(self):
        proc, payload = self.run_quality_gate(
            "redis_sink",
            metrics_extra={"redis_queue_dropped_total": 2},
        )
        self.assertEqual(3, proc.returncode, proc.stdout + proc.stderr)
        self.assertFalse(payload["ok"])
        self.assertIn(
            "redis queue dropped events=2 > 0",
            payload["reasons"],
        )

    def test_low_identity_devino_coverage_is_reported(self):
        proc, payload = self.run_quality_gate(
            "file_sink",
            aggregate_stats_payload={
                "events_total": 32,
                "unique_paths": 12,
                "paths_with_devino": 0,
                "identity_candidates": 12,
                "devino_identity_candidates": 0,
                "identity_alias_groups": 0,
                "identity_merged_paths": 0,
            },
        )
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["identity"]["stats_present"])
        self.assertEqual(12, payload["identity"]["unique_paths"])
        self.assertIn(
            "aggregate stats show 0 paths with dev:ino; identity merging is effectively disabled",
            payload["warnings"],
        )

    def test_identity_warning_when_devino_comes_only_from_open_stat_backfill(self):
        proc, payload = self.run_quality_gate(
            "file_sink",
            aggregate_stats_payload={
                "events_total": 32,
                "unique_paths": 12,
                "paths_with_devino": 6,
                "paths_with_open_stat_devino": 6,
                "paths_with_open_raw_devino": 0,
                "paths_with_mmap_raw_devino": 0,
                "identity_candidates": 8,
                "devino_identity_candidates": 6,
                "identity_candidates_with_open_stat_devino": 6,
                "identity_candidates_with_mmap_raw_devino": 0,
                "identity_alias_groups": 1,
                "identity_merged_paths": 4,
            },
        )
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual(6, payload["identity"]["paths_with_open_stat_devino"])
        self.assertEqual(0, payload["identity"]["paths_with_mmap_raw_devino"])
        self.assertIn(
            "identity dev:ino coverage comes entirely from OPEN stat backfill; mmap raw dev:ino evidence is absent",
            payload["warnings"],
        )

    def test_identity_backfill_only_large_mmap_sample_is_hard_fail(self):
        proc, payload = self.run_quality_gate(
            "file_sink",
            raw_rows=[
                {"type": "OPEN", "path": "/app/a.so", "dev": 1, "ino": 11},
                {"type": "MMAP", "path": "/app/a.so", "dev": 1, "ino": 11},
                {"type": "MMAP", "path": "/app/a.so", "dev": 1, "ino": 11},
                {"type": "MMAP", "path": "/app/a.so", "dev": 1, "ino": 11},
                {"type": "MMAP", "path": "/app/a.so", "dev": 1, "ino": 11},
                {"type": "MMAP", "path": "/app/a.so", "dev": 1, "ino": 11},
            ],
            event_rows=[
                {
                    "type": "OPEN",
                    "path": "/app/a.so",
                    "dev": 1,
                    "ino": 11,
                    "devino_source": "stat",
                    "path_resolution": "cwd",
                }
            ],
            enriched_rows=[
                {
                    "type": "OPEN",
                    "path": "/app/a.so",
                    "dev": 1,
                    "ino": 11,
                    "devino_source": "stat",
                    "path_resolution": "cwd",
                },
                {"type": "MMAP", "path": "/app/a.so", "dev": 1, "ino": 11, "via": "dict"},
                {"type": "MMAP", "path": "/app/a.so", "dev": 1, "ino": 11, "via": "dict"},
                {"type": "MMAP", "path": "/app/a.so", "dev": 1, "ino": 11, "via": "dict"},
                {"type": "MMAP", "path": "/app/a.so", "dev": 1, "ino": 11, "via": "dict"},
                {"type": "MMAP", "path": "/app/a.so", "dev": 1, "ino": 11, "via": "dict"},
            ],
            aggregate_stats_payload={
                "events_total": 32,
                "unique_paths": 24,
                "paths_with_devino": 8,
                "paths_with_open_stat_devino": 8,
                "paths_with_open_raw_devino": 0,
                "paths_with_mmap_raw_devino": 0,
                "identity_candidates": 10,
                "devino_identity_candidates": 8,
                "identity_candidates_with_open_stat_devino": 8,
                "identity_candidates_with_mmap_raw_devino": 0,
                "identity_alias_groups": 1,
                "identity_merged_paths": 6,
            },
            metrics_extra={"file_events_total": 6, "file_sink_lines_total": 6, "file_sink_bytes_total": 256},
        )
        self.assertEqual(3, proc.returncode, proc.stdout + proc.stderr)
        self.assertFalse(payload["ok"])
        self.assertIn(
            "identity dev:ino coverage relies entirely on OPEN stat backfill on a mmap-bearing non-trivial sample",
            payload["reasons"],
        )

    def test_high_cwd_resolved_open_share_is_reported(self):
        proc, payload = self.run_quality_gate(
            "file_sink",
            aggregate_stats_payload={
                "events_total": 32,
                "unique_paths": 12,
                "paths_with_devino": 6,
                "paths_with_open_resolved_from_cwd": 5,
                "paths_with_open_cwd_dirfd_ambiguous": 5,
                "paths_with_open_stat_devino": 6,
                "paths_with_open_raw_devino": 0,
                "paths_with_mmap_raw_devino": 1,
                "identity_candidates": 8,
                "devino_identity_candidates": 6,
                "identity_candidates_with_open_cwd_dirfd_ambiguous": 5,
                "identity_candidates_with_open_stat_devino": 6,
                "identity_candidates_with_mmap_raw_devino": 1,
                "identity_alias_groups": 1,
                "identity_merged_paths": 4,
            },
        )
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual(5, payload["identity"]["paths_with_open_resolved_from_cwd"])
        self.assertEqual(5, payload["identity"]["paths_with_open_cwd_dirfd_ambiguous"])
        self.assertIn(
            "aggregate dirfd-ambiguous OPEN path ratio is high: 0.4167",
            payload["warnings"],
        )

    def test_high_cwd_resolved_open_share_without_mmap_raw_is_hard_fail(self):
        proc, payload = self.run_quality_gate(
            "file_sink",
            aggregate_stats_payload={
                "events_total": 32,
                "unique_paths": 24,
                "paths_with_devino": 8,
                "paths_with_open_resolved_from_cwd": 13,
                "paths_with_open_cwd_dirfd_ambiguous": 13,
                "paths_with_open_stat_devino": 8,
                "paths_with_open_raw_devino": 0,
                "paths_with_mmap_raw_devino": 0,
                "identity_candidates": 10,
                "devino_identity_candidates": 8,
                "identity_candidates_with_open_cwd_dirfd_ambiguous": 9,
                "identity_candidates_with_open_stat_devino": 8,
                "identity_candidates_with_mmap_raw_devino": 0,
                "identity_alias_groups": 1,
                "identity_merged_paths": 6,
            },
            raw_rows=[
                {"type": "OPEN", "path": "/app/a.so", "dev": 1, "ino": 11},
                {"type": "MMAP", "path": "/app/a.so", "dev": 1, "ino": 11},
                {"type": "MMAP", "path": "/app/a.so", "dev": 1, "ino": 11},
                {"type": "MMAP", "path": "/app/a.so", "dev": 1, "ino": 11},
                {"type": "MMAP", "path": "/app/a.so", "dev": 1, "ino": 11},
                {"type": "MMAP", "path": "/app/a.so", "dev": 1, "ino": 11},
            ],
            metrics_extra={"file_events_total": 6, "file_sink_lines_total": 6, "file_sink_bytes_total": 256},
        )
        self.assertEqual(3, proc.returncode, proc.stdout + proc.stderr)
        self.assertFalse(payload["ok"])
        self.assertIn(
            "high dirfd-ambiguous OPEN share with no mmap raw identity evidence on a non-trivial sample",
            payload["reasons"],
        )

    def test_open_stat_miss_share_is_reported(self):
        proc, payload = self.run_quality_gate(
            "file_sink",
            export_stats_payload={
                "relative_open_total": 3,
                "open_stat_fail": 1,
                "open_stat_fail_not_found": 1,
                "open_stat_fail_permission": 0,
                "open_stat_fail_other": 0,
                "open_stat_fail_cwd_resolved": 1,
            },
            aggregate_stats_payload={
                "events_total": 32,
                "unique_paths": 12,
                "paths_with_devino": 6,
                "paths_with_open_stat_devino": 4,
                "paths_with_open_stat_miss": 3,
                "paths_with_open_raw_devino": 0,
                "paths_with_mmap_raw_devino": 1,
                "identity_candidates": 8,
                "devino_identity_candidates": 6,
                "identity_candidates_with_open_stat_devino": 4,
                "identity_candidates_with_open_stat_miss": 3,
                "identity_candidates_with_mmap_raw_devino": 1,
                "identity_alias_groups": 1,
                "identity_merged_paths": 4,
            },
            event_rows=[
                {"type": "OPEN", "path": "/app/a.so", "dev": 1, "ino": 11},
                {"type": "OPEN", "path": "/app/missing.so", "stat_lookup": "miss", "stat_lookup_kind": "not_found"},
            ],
            enriched_rows=[
                {"type": "OPEN", "path": "/app/a.so", "dev": 1, "ino": 11},
                {"type": "OPEN", "path": "/app/missing.so", "stat_lookup": "miss", "stat_lookup_kind": "not_found"},
                {"type": "MMAP", "path": "/app/a.so", "dev": 1, "ino": 11, "via": "mapfiles"},
            ],
        )
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual(3, payload["identity"]["paths_with_open_stat_miss"])
        self.assertEqual(1, payload["export"]["open_stat_fail"])
        self.assertIn(
            "OPEN stat backfill could not resolve some emitted OPEN paths: fail=1 not_found=1 permission=0 other=0",
            payload["warnings"],
        )
        self.assertIn(
            "aggregate OPEN stat-miss path ratio is elevated: 0.2500",
            payload["warnings"],
        )

    def test_high_open_stat_miss_share_without_mmap_raw_is_hard_fail(self):
        proc, payload = self.run_quality_gate(
            "file_sink",
            export_stats_payload={
                "relative_open_total": 6,
                "open_stat_fail": 4,
                "open_stat_fail_not_found": 4,
                "open_stat_fail_permission": 0,
                "open_stat_fail_other": 0,
                "open_stat_fail_cwd_resolved": 3,
            },
            aggregate_stats_payload={
                "events_total": 32,
                "unique_paths": 24,
                "paths_with_devino": 8,
                "paths_with_open_stat_devino": 4,
                "paths_with_open_stat_miss": 10,
                "paths_with_open_raw_devino": 0,
                "paths_with_mmap_raw_devino": 0,
                "identity_candidates": 10,
                "devino_identity_candidates": 8,
                "identity_candidates_with_open_stat_devino": 4,
                "identity_candidates_with_open_stat_miss": 7,
                "identity_candidates_with_mmap_raw_devino": 0,
                "identity_alias_groups": 1,
                "identity_merged_paths": 6,
            },
            raw_rows=[
                {"type": "OPEN", "path": "/app/a.so", "dev": 1, "ino": 11},
                {"type": "MMAP", "path": "/app/a.so", "dev": 1, "ino": 11},
                {"type": "MMAP", "path": "/app/a.so", "dev": 1, "ino": 11},
                {"type": "MMAP", "path": "/app/a.so", "dev": 1, "ino": 11},
                {"type": "MMAP", "path": "/app/a.so", "dev": 1, "ino": 11},
                {"type": "MMAP", "path": "/app/a.so", "dev": 1, "ino": 11},
            ],
            metrics_extra={"file_events_total": 6, "file_sink_lines_total": 6, "file_sink_bytes_total": 256},
        )
        self.assertEqual(3, proc.returncode, proc.stdout + proc.stderr)
        self.assertFalse(payload["ok"])
        self.assertIn(
            "high OPEN stat-miss share with no mmap raw identity evidence on a non-trivial sample",
            payload["reasons"],
        )

    def test_legacy_startup_and_filtered_events_are_degraded_warnings(self):
        proc, payload = self.run_quality_gate(
            "file_sink",
            metrics_extra={"filtered_out_total": 7},
            startup_mode="legacy",
        )
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertTrue(payload["ok"])
        self.assertIn("filtered_out_total=7", payload["warnings"])
        self.assertIn(
            "startup mode is legacy; earliest cold-start accesses may be missed",
            payload["warnings"],
        )
        self.assertEqual("legacy", payload["startup_backend"])

    def test_relative_open_without_cwd_resolution_warns(self):
        proc, payload = self.run_quality_gate(
            "file_sink",
            raw_rows=[
                {"type": "OPEN", "path": "relative/module.py", "dev": 0, "ino": 0},
                {"type": "MMAP", "path": "/app/a.so", "dev": 1, "ino": 11},
            ],
            event_rows=[
                {
                    "type": "OPEN",
                    "path": "relative/module.py",
                    "dev": 0,
                    "ino": 0,
                    "path_resolution": "unresolved_relative",
                }
            ],
        )
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertTrue(payload["ok"])
        self.assertIn(
            "raw OPEN events contained relative paths, but none were resolved via cwd snapshot",
            payload["warnings"],
        )
        self.assertEqual(1, payload["counts"]["events_open_unresolved_relative_kept"])

    def test_relative_open_with_explicit_dirfd_warns_that_cwd_reconstruction_was_skipped(self):
        proc, payload = self.run_quality_gate(
            "file_sink",
            export_stats_payload={
                "relative_open_total": 1,
                "relative_open_resolved_total": 0,
                "relative_open_resolved_legacy_missing_dirfd": 0,
                "relative_open_resolved_at_fdcwd": 0,
                "relative_open_explicit_dirfd_skipped": 1,
                "relative_open_unresolved_no_cwd": 0,
            },
            raw_rows=[
                {"type": "OPEN", "path": "relative/module.py", "dirfd": 7, "dev": 0, "ino": 0},
                {"type": "MMAP", "path": "/app/a.so", "dev": 1, "ino": 11},
            ],
            event_rows=[
                {
                    "type": "OPEN",
                    "path": "/app/a.so",
                    "dev": 1,
                    "ino": 11,
                }
            ],
            enriched_rows=[
                {
                    "type": "OPEN",
                    "path": "/app/a.so",
                    "dev": 1,
                    "ino": 11,
                    "via": "open",
                },
                {
                    "type": "MMAP",
                    "path": "/app/a.so",
                    "dev": 1,
                    "ino": 11,
                    "via": "mapfiles",
                },
            ],
        )
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertTrue(payload["ok"])
        self.assertIn(
            "raw OPEN events contained relative paths with explicit non-AT_FDCWD dirfd; cwd reconstruction was intentionally skipped: 1",
            payload["warnings"],
        )
        self.assertTrue(payload["export"]["stats_present"])
        self.assertEqual(1, payload["export"]["relative_open_explicit_dirfd_skipped"])
        self.assertNotIn(
            "raw OPEN events contained relative paths, but none were resolved via cwd snapshot",
            payload["warnings"],
        )

    def test_maps_only_mmap_reconstruction_is_hard_fail(self):
        proc, payload = self.run_quality_gate(
            "file_sink",
            enriched_rows=[
                {
                    "type": "OPEN",
                    "path": "/app/a.so",
                    "dev": 1,
                    "ino": 11,
                    "path_resolution": "cwd",
                },
                {
                    "type": "MMAP",
                    "path": "/app/a.so",
                    "dev": 1,
                    "ino": 11,
                    "via": "maps",
                },
            ],
        )
        self.assertEqual(3, proc.returncode, proc.stdout + proc.stderr)
        self.assertFalse(payload["ok"])
        self.assertIn(
            "mmap via=maps ratio=1.0000 > max=0.5000",
            payload["reasons"],
        )

    def test_self_starting_legacy_clean_backfill_adaptation_allows_maps_only_mmap(self):
        event_rows = [
            {
                "type": "OPEN",
                "path": f"/app/file{i}.so",
                "dev": 1,
                "ino": 100 + i,
                "devino_source": "stat",
            }
            for i in range(12)
        ]
        raw_rows = list(event_rows) + [
            {"type": "MMAP", "path": "/app/file0.so", "dev": 1, "ino": 100},
            {"type": "MMAP", "path": "/app/file1.so", "dev": 1, "ino": 101},
            {"type": "MMAP", "path": "/app/file2.so", "dev": 1, "ino": 102},
            {"type": "MMAP", "path": "/app/file3.so", "dev": 1, "ino": 103},
            {"type": "MMAP", "path": "/app/file4.so", "dev": 1, "ino": 104},
        ]
        enriched_rows = [
            {**row, "via": "open"}
            for row in event_rows
        ] + [
            {"type": "MMAP", "path": "/app/file0.so", "dev": 1, "ino": 100, "via": "maps"},
            {"type": "MMAP", "path": "/app/file1.so", "dev": 1, "ino": 101, "via": "maps"},
            {"type": "MMAP", "path": "/app/file2.so", "dev": 1, "ino": 102, "via": "maps"},
            {"type": "MMAP", "path": "/app/file3.so", "dev": 1, "ino": 103, "via": "maps"},
            {"type": "MMAP", "path": "/app/file4.so", "dev": 1, "ino": 104, "via": "maps"},
        ]
        proc, payload = self.run_quality_gate(
            "file_sink",
            self_starting_service=True,
            startup_mode="legacy",
            startup_backend="legacy",
            raw_rows=raw_rows,
            event_rows=event_rows,
            enriched_rows=enriched_rows,
            aggregate_stats_payload={
                "events_total": 32,
                "unique_paths": 25,
                "paths_with_devino": 22,
                "paths_with_open_stat_devino": 22,
                "paths_with_open_raw_devino": 0,
                "paths_with_open_stat_miss": 2,
                "paths_with_open_resolved_from_cwd": 0,
                "paths_with_open_cwd_dirfd_ambiguous": 0,
                "paths_with_mmap_raw_devino": 0,
                "identity_candidates": 24,
                "devino_identity_candidates": 22,
                "identity_candidates_with_open_stat_devino": 22,
                "identity_candidates_with_open_stat_miss": 2,
                "identity_candidates_with_open_cwd_dirfd_ambiguous": 0,
                "identity_candidates_with_mmap_raw_devino": 0,
                "identity_alias_groups": 1,
                "identity_merged_paths": 6,
            },
            export_stats_payload={
                "relative_open_total": 0,
                "relative_open_resolved_total": 0,
                "relative_open_resolved_legacy_missing_dirfd": 0,
                "relative_open_resolved_at_fdcwd": 0,
                "relative_open_explicit_dirfd_skipped": 0,
                "relative_open_unresolved_no_cwd": 0,
                "open_stat_fail": 1,
                "open_stat_fail_not_found": 1,
                "open_stat_fail_permission": 0,
                "open_stat_fail_other": 0,
                "open_stat_fail_cwd_resolved": 0,
            },
            metrics_extra={"file_events_total": 17, "file_sink_lines_total": 17, "file_sink_bytes_total": 512},
        )
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual([], payload["reasons"])
        self.assertEqual(1, len(payload["adaptations_applied"]))
        self.assertEqual(
            "self_starting_legacy_maps_backfill_only",
            payload["adaptations_applied"][0]["id"],
        )
        self.assertIn(
            "quality gate adaptation applied for self-starting legacy workload; mmap maps-only reconstruction was accepted because OPEN stat backfill evidence is clean",
            payload["warnings"],
        )
        self.assertIn(
            "mmap via=maps ratio=1.0000 > max=0.5000",
            payload["warnings"],
        )
        self.assertIn(
            "identity dev:ino coverage relies entirely on OPEN stat backfill on a mmap-bearing non-trivial sample",
            payload["warnings"],
        )

    def test_self_starting_adaptation_does_not_apply_when_flag_is_false(self):
        event_rows = [
            {
                "type": "OPEN",
                "path": f"/app/file{i}.so",
                "dev": 1,
                "ino": 100 + i,
                "devino_source": "stat",
            }
            for i in range(12)
        ]
        raw_rows = list(event_rows) + [
            {"type": "MMAP", "path": "/app/file0.so", "dev": 1, "ino": 100},
            {"type": "MMAP", "path": "/app/file1.so", "dev": 1, "ino": 101},
            {"type": "MMAP", "path": "/app/file2.so", "dev": 1, "ino": 102},
            {"type": "MMAP", "path": "/app/file3.so", "dev": 1, "ino": 103},
            {"type": "MMAP", "path": "/app/file4.so", "dev": 1, "ino": 104},
        ]
        enriched_rows = [{**row, "via": "open"} for row in event_rows] + [
            {"type": "MMAP", "path": "/app/file0.so", "dev": 1, "ino": 100, "via": "maps"},
            {"type": "MMAP", "path": "/app/file1.so", "dev": 1, "ino": 101, "via": "maps"},
            {"type": "MMAP", "path": "/app/file2.so", "dev": 1, "ino": 102, "via": "maps"},
            {"type": "MMAP", "path": "/app/file3.so", "dev": 1, "ino": 103, "via": "maps"},
            {"type": "MMAP", "path": "/app/file4.so", "dev": 1, "ino": 104, "via": "maps"},
        ]
        proc, payload = self.run_quality_gate(
            "file_sink",
            self_starting_service=False,
            startup_mode="legacy",
            startup_backend="legacy",
            raw_rows=raw_rows,
            event_rows=event_rows,
            enriched_rows=enriched_rows,
            aggregate_stats_payload={
                "events_total": 32,
                "unique_paths": 25,
                "paths_with_devino": 22,
                "paths_with_open_stat_devino": 22,
                "paths_with_open_raw_devino": 0,
                "paths_with_open_stat_miss": 2,
                "paths_with_open_resolved_from_cwd": 0,
                "paths_with_open_cwd_dirfd_ambiguous": 0,
                "paths_with_mmap_raw_devino": 0,
                "identity_candidates": 24,
                "devino_identity_candidates": 22,
                "identity_candidates_with_open_stat_devino": 22,
                "identity_candidates_with_open_stat_miss": 2,
                "identity_candidates_with_open_cwd_dirfd_ambiguous": 0,
                "identity_candidates_with_mmap_raw_devino": 0,
                "identity_alias_groups": 1,
                "identity_merged_paths": 6,
            },
            export_stats_payload={
                "relative_open_total": 0,
                "relative_open_resolved_total": 0,
                "relative_open_resolved_legacy_missing_dirfd": 0,
                "relative_open_resolved_at_fdcwd": 0,
                "relative_open_explicit_dirfd_skipped": 0,
                "relative_open_unresolved_no_cwd": 0,
                "open_stat_fail": 1,
                "open_stat_fail_not_found": 1,
                "open_stat_fail_permission": 0,
                "open_stat_fail_other": 0,
                "open_stat_fail_cwd_resolved": 0,
            },
            metrics_extra={"file_events_total": 17, "file_sink_lines_total": 17, "file_sink_bytes_total": 512},
        )
        self.assertEqual(3, proc.returncode, proc.stdout + proc.stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual([], payload["adaptations_applied"])
        self.assertIn(
            "mmap via=maps ratio=1.0000 > max=0.5000",
            payload["reasons"],
        )
        self.assertIn(
            "identity dev:ino coverage relies entirely on OPEN stat backfill on a mmap-bearing non-trivial sample",
            payload["reasons"],
        )

    def test_self_starting_adaptation_does_not_apply_when_cwd_ambiguity_is_present(self):
        event_rows = [
            {
                "type": "OPEN",
                "path": f"/app/file{i}.so",
                "dev": 1,
                "ino": 100 + i,
                "devino_source": "stat",
            }
            for i in range(12)
        ]
        raw_rows = list(event_rows) + [
            {"type": "MMAP", "path": "/app/file0.so", "dev": 1, "ino": 100},
            {"type": "MMAP", "path": "/app/file1.so", "dev": 1, "ino": 101},
            {"type": "MMAP", "path": "/app/file2.so", "dev": 1, "ino": 102},
            {"type": "MMAP", "path": "/app/file3.so", "dev": 1, "ino": 103},
            {"type": "MMAP", "path": "/app/file4.so", "dev": 1, "ino": 104},
        ]
        enriched_rows = [{**row, "via": "open"} for row in event_rows] + [
            {"type": "MMAP", "path": "/app/file0.so", "dev": 1, "ino": 100, "via": "maps"},
            {"type": "MMAP", "path": "/app/file1.so", "dev": 1, "ino": 101, "via": "maps"},
            {"type": "MMAP", "path": "/app/file2.so", "dev": 1, "ino": 102, "via": "maps"},
            {"type": "MMAP", "path": "/app/file3.so", "dev": 1, "ino": 103, "via": "maps"},
            {"type": "MMAP", "path": "/app/file4.so", "dev": 1, "ino": 104, "via": "maps"},
        ]
        proc, payload = self.run_quality_gate(
            "file_sink",
            self_starting_service=True,
            startup_mode="legacy",
            startup_backend="legacy",
            raw_rows=raw_rows,
            event_rows=event_rows,
            enriched_rows=enriched_rows,
            aggregate_stats_payload={
                "events_total": 32,
                "unique_paths": 25,
                "paths_with_devino": 22,
                "paths_with_open_stat_devino": 22,
                "paths_with_open_raw_devino": 0,
                "paths_with_open_stat_miss": 2,
                "paths_with_open_resolved_from_cwd": 1,
                "paths_with_open_cwd_dirfd_ambiguous": 1,
                "paths_with_mmap_raw_devino": 0,
                "identity_candidates": 24,
                "devino_identity_candidates": 22,
                "identity_candidates_with_open_stat_devino": 22,
                "identity_candidates_with_open_stat_miss": 2,
                "identity_candidates_with_open_cwd_dirfd_ambiguous": 1,
                "identity_candidates_with_mmap_raw_devino": 0,
                "identity_alias_groups": 1,
                "identity_merged_paths": 6,
            },
            export_stats_payload={
                "relative_open_total": 1,
                "relative_open_resolved_total": 1,
                "relative_open_resolved_legacy_missing_dirfd": 1,
                "relative_open_resolved_at_fdcwd": 0,
                "relative_open_explicit_dirfd_skipped": 0,
                "relative_open_unresolved_no_cwd": 0,
                "open_stat_fail": 1,
                "open_stat_fail_not_found": 1,
                "open_stat_fail_permission": 0,
                "open_stat_fail_other": 0,
                "open_stat_fail_cwd_resolved": 0,
            },
            metrics_extra={"file_events_total": 17, "file_sink_lines_total": 17, "file_sink_bytes_total": 512},
        )
        self.assertEqual(3, proc.returncode, proc.stdout + proc.stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual([], payload["adaptations_applied"])
        self.assertIn(
            "mmap via=maps ratio=1.0000 > max=0.5000",
            payload["reasons"],
        )
        self.assertIn(
            "identity dev:ino coverage relies entirely on OPEN stat backfill on a mmap-bearing non-trivial sample",
            payload["reasons"],
        )

    def test_self_starting_adaptation_does_not_apply_when_export_open_stat_fail_ratio_exceeds_threshold(self):
        event_rows = [
            {
                "type": "OPEN",
                "path": f"/app/file{i}.so",
                "dev": 1,
                "ino": 100 + i,
                "devino_source": "stat",
            }
            for i in range(12)
        ]
        raw_rows = list(event_rows) + [
            {"type": "MMAP", "path": "/app/file0.so", "dev": 1, "ino": 100},
            {"type": "MMAP", "path": "/app/file1.so", "dev": 1, "ino": 101},
            {"type": "MMAP", "path": "/app/file2.so", "dev": 1, "ino": 102},
            {"type": "MMAP", "path": "/app/file3.so", "dev": 1, "ino": 103},
            {"type": "MMAP", "path": "/app/file4.so", "dev": 1, "ino": 104},
        ]
        enriched_rows = [{**row, "via": "open"} for row in event_rows] + [
            {"type": "MMAP", "path": "/app/file0.so", "dev": 1, "ino": 100, "via": "maps"},
            {"type": "MMAP", "path": "/app/file1.so", "dev": 1, "ino": 101, "via": "maps"},
            {"type": "MMAP", "path": "/app/file2.so", "dev": 1, "ino": 102, "via": "maps"},
            {"type": "MMAP", "path": "/app/file3.so", "dev": 1, "ino": 103, "via": "maps"},
            {"type": "MMAP", "path": "/app/file4.so", "dev": 1, "ino": 104, "via": "maps"},
        ]
        proc, payload = self.run_quality_gate(
            "file_sink",
            self_starting_service=True,
            startup_mode="legacy",
            startup_backend="legacy",
            raw_rows=raw_rows,
            event_rows=event_rows,
            enriched_rows=enriched_rows,
            aggregate_stats_payload={
                "events_total": 32,
                "unique_paths": 25,
                "paths_with_devino": 22,
                "paths_with_open_stat_devino": 22,
                "paths_with_open_raw_devino": 0,
                "paths_with_open_stat_miss": 2,
                "paths_with_open_resolved_from_cwd": 0,
                "paths_with_open_cwd_dirfd_ambiguous": 0,
                "paths_with_mmap_raw_devino": 0,
                "identity_candidates": 24,
                "devino_identity_candidates": 22,
                "identity_candidates_with_open_stat_devino": 22,
                "identity_candidates_with_open_stat_miss": 2,
                "identity_candidates_with_open_cwd_dirfd_ambiguous": 0,
                "identity_candidates_with_mmap_raw_devino": 0,
                "identity_alias_groups": 1,
                "identity_merged_paths": 6,
            },
            export_stats_payload={
                "relative_open_total": 0,
                "relative_open_resolved_total": 0,
                "relative_open_resolved_legacy_missing_dirfd": 0,
                "relative_open_resolved_at_fdcwd": 0,
                "relative_open_explicit_dirfd_skipped": 0,
                "relative_open_unresolved_no_cwd": 0,
                "open_stat_fail": 2,
                "open_stat_fail_not_found": 2,
                "open_stat_fail_permission": 0,
                "open_stat_fail_other": 0,
                "open_stat_fail_cwd_resolved": 0,
            },
            metrics_extra={"file_events_total": 17, "file_sink_lines_total": 17, "file_sink_bytes_total": 512},
        )
        self.assertEqual(3, proc.returncode, proc.stdout + proc.stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual([], payload["adaptations_applied"])
        self.assertIn(
            "mmap via=maps ratio=1.0000 > max=0.5000",
            payload["reasons"],
        )
        self.assertIn(
            "identity dev:ino coverage relies entirely on OPEN stat backfill on a mmap-bearing non-trivial sample",
            payload["reasons"],
        )

    def test_self_starting_adaptation_does_not_apply_when_other_hard_fail_exists(self):
        event_rows = [
            {
                "type": "OPEN",
                "path": f"/app/file{i}.so",
                "dev": 1,
                "ino": 100 + i,
                "devino_source": "stat",
            }
            for i in range(12)
        ]
        raw_rows = list(event_rows) + [
            {"type": "MMAP", "path": "/app/file0.so", "dev": 1, "ino": 100},
            {"type": "MMAP", "path": "/app/file1.so", "dev": 1, "ino": 101},
            {"type": "MMAP", "path": "/app/file2.so", "dev": 1, "ino": 102},
            {"type": "MMAP", "path": "/app/file3.so", "dev": 1, "ino": 103},
            {"type": "MMAP", "path": "/app/file4.so", "dev": 1, "ino": 104},
        ]
        enriched_rows = [{**row, "via": "open"} for row in event_rows] + [
            {"type": "MMAP", "path": "/app/file0.so", "dev": 1, "ino": 100, "via": "maps"},
            {"type": "MMAP", "path": "/app/file1.so", "dev": 1, "ino": 101, "via": "maps"},
            {"type": "MMAP", "path": "/app/file2.so", "dev": 1, "ino": 102, "via": "maps"},
            {"type": "MMAP", "path": "/app/file3.so", "dev": 1, "ino": 103, "via": "maps"},
            {"type": "MMAP", "path": "/app/file4.so", "dev": 1, "ino": 104, "via": "maps"},
        ]
        proc, payload = self.run_quality_gate(
            "file_sink",
            self_starting_service=True,
            startup_mode="legacy",
            startup_backend="legacy",
            raw_rows=raw_rows,
            event_rows=event_rows,
            enriched_rows=enriched_rows,
            aggregate_stats_payload={
                "events_total": 32,
                "unique_paths": 25,
                "paths_with_devino": 22,
                "paths_with_open_stat_devino": 22,
                "paths_with_open_raw_devino": 0,
                "paths_with_open_stat_miss": 2,
                "paths_with_open_resolved_from_cwd": 0,
                "paths_with_open_cwd_dirfd_ambiguous": 0,
                "paths_with_mmap_raw_devino": 0,
                "identity_candidates": 24,
                "devino_identity_candidates": 22,
                "identity_candidates_with_open_stat_devino": 22,
                "identity_candidates_with_open_stat_miss": 2,
                "identity_candidates_with_open_cwd_dirfd_ambiguous": 0,
                "identity_candidates_with_mmap_raw_devino": 0,
                "identity_alias_groups": 1,
                "identity_merged_paths": 6,
            },
            export_stats_payload={
                "relative_open_total": 0,
                "relative_open_resolved_total": 0,
                "relative_open_resolved_legacy_missing_dirfd": 0,
                "relative_open_resolved_at_fdcwd": 0,
                "relative_open_explicit_dirfd_skipped": 0,
                "relative_open_unresolved_no_cwd": 0,
                "open_stat_fail": 1,
                "open_stat_fail_not_found": 1,
                "open_stat_fail_permission": 0,
                "open_stat_fail_other": 0,
                "open_stat_fail_cwd_resolved": 0,
            },
            metrics_extra={
                "file_events_total": 17,
                "file_sink_lines_total": 17,
                "file_sink_bytes_total": 512,
                "ringbuf_drops_total": 1,
            },
        )
        self.assertEqual(3, proc.returncode, proc.stdout + proc.stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual([], payload["adaptations_applied"])
        self.assertIn("ringbuf_drops_total=1 > 0", payload["reasons"])
        self.assertIn(
            "mmap via=maps ratio=1.0000 > max=0.5000",
            payload["reasons"],
        )
        self.assertIn(
            "identity dev:ino coverage relies entirely on OPEN stat backfill on a mmap-bearing non-trivial sample",
            payload["reasons"],
        )

    def test_experimental_host_launcher_backend_is_reported(self):
        proc, payload = self.run_quality_gate(
            "file_sink",
            startup_mode="gated",
            startup_backend="gated_host_launcher_dynamic",
        )
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("gated_host_launcher_dynamic", payload["startup_backend"])
        self.assertEqual("gated", payload["startup"]["mode"])
        self.assertEqual("gated_host_launcher_dynamic", payload["startup"]["backend"])
        self.assertIn(
            "startup backend is experimental host launcher (dynamic); container runtime libc compatibility is required",
            payload["warnings"],
        )


if __name__ == "__main__":
    unittest.main()
