import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.stages import export_enrich as export_enrich_module
from orchestrator.stages.export_enrich import run_export_enrich_stage


ROOT = Path(__file__).resolve().parents[1]


class ExportEnrichStageTests(unittest.TestCase):
    def test_resolve_export_container_pid_prefers_container_pid_for_gated_host_launcher_ready_listener(self):
        runtime_state = {
            "container_pid": 3044298,
            "live_container_pid": 2742574,
            "live_container_pid_source": "ready_listener",
            "startup_backend": "gated_host_launcher_static",
            "self_starting_service": True,
            "ready_url": "http://127.0.0.1:8000/ready",
        }

        with mock.patch.object(export_enrich_module, "_pid_alive", side_effect=lambda pid: pid in {3044298, 2742574}):
            pid, source = export_enrich_module._resolve_export_container_pid(runtime_state)

        self.assertEqual(3044298, pid)
        self.assertEqual("container_pid", source)

    def test_export_enrich_stage_produces_enriched_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            tracked = work_dir / "tracked.bin"
            tracked.write_bytes(b"payload")
            st = tracked.stat()

            raw_events = work_dir / "raw_events.jsonl"
            raw_rows = [
                {
                    "type": "OPEN",
                    "pid": os.getpid(),
                    "tgid": os.getpid(),
                    "path": str(tracked),
                    "ts_ns": 1,
                },
                {
                    "type": "MMAP",
                    "pid": os.getpid(),
                    "tgid": os.getpid(),
                    "dev": int(st.st_dev),
                    "ino": int(st.st_ino),
                    "ts_ns": 2,
                },
            ]
            raw_events.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in raw_rows),
                encoding="utf-8",
            )
            runtime_state = {
                "container_name": "self",
                "container_pid": os.getpid(),
                "startup_mode_used": "legacy",
                "prom_port": 18080,
                "raw_events": str(raw_events),
            }
            (work_dir / "runtime_state.json").write_text(
                json.dumps(runtime_state, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            result = run_export_enrich_stage(
                repo_root=ROOT,
                work_dir=work_dir,
            )

            events = [
                json.loads(line)
                for line in result.events_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            export_stats = json.loads(result.export_stats_path.read_text(encoding="utf-8"))
            enriched = [
                json.loads(line)
                for line in result.events_enriched_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

            self.assertEqual(1, len(events))
            self.assertEqual("OPEN", events[0]["type"])
            self.assertEqual(str(tracked), events[0]["path"])
            self.assertGreater(int(events[0].get("dev", 0)), 0)
            self.assertGreater(int(events[0].get("ino", 0)), 0)
            self.assertEqual("regular", events[0].get("path_kind"))
            self.assertEqual("stat", events[0].get("devino_source"))
            self.assertEqual(0, export_stats["relative_open_total"])

            mmap_rows = [
                row
                for row in enriched
                if row["type"] == "MMAP" and row.get("path") == str(tracked)
            ]
            self.assertEqual(1, len(mmap_rows))
            self.assertEqual("dict", mmap_rows[0]["via"])
            self.assertEqual("regular", mmap_rows[0].get("path_kind"))
            self.assertEqual("mmap_raw", mmap_rows[0].get("devino_source"))

    def test_export_enrich_stage_marks_relative_open_as_dirfd_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            tracked = work_dir / "tracked.bin"
            tracked.write_bytes(b"payload")
            raw_events = work_dir / "raw_events.jsonl"
            raw_rows = [
                {
                    "type": "OPEN",
                    "pid": os.getpid(),
                    "tgid": os.getpid(),
                    "path": tracked.name,
                    "ts_ns": 1,
                }
            ]
            raw_events.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in raw_rows),
                encoding="utf-8",
            )
            runtime_state = {
                "container_name": "self",
                "container_pid": os.getpid(),
                "startup_mode_used": "legacy",
                "prom_port": 18080,
                "raw_events": str(raw_events),
            }
            (work_dir / "runtime_state.json").write_text(
                json.dumps(runtime_state, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            prev_cwd = os.getcwd()
            try:
                os.chdir(work_dir)
                result = run_export_enrich_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                )
            finally:
                os.chdir(prev_cwd)

            events = [
                json.loads(line)
                for line in result.events_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            export_stats = json.loads(result.export_stats_path.read_text(encoding="utf-8"))
            enriched = [
                json.loads(line)
                for line in result.events_enriched_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            open_rows = [row for row in enriched if row.get("type") == "OPEN" and row.get("path") == str(tracked)]

            self.assertEqual(1, len(events))
            self.assertEqual(str(tracked), events[0]["path"])
            self.assertEqual("cwd", events[0].get("path_resolution"))
            self.assertEqual("dirfd_unavailable", events[0].get("path_resolution_risk"))
            self.assertIn("dirfd is not captured", events[0].get("path_resolution_detail", ""))
            self.assertEqual(1, export_stats["relative_open_total"])
            self.assertEqual(1, export_stats["relative_open_resolved_legacy_missing_dirfd"])
            self.assertEqual(1, len(open_rows))
            self.assertEqual("cwd", open_rows[0].get("path_resolution"))
            self.assertEqual("dirfd_unavailable", open_rows[0].get("path_resolution_risk"))

    def test_export_enrich_stage_prefers_live_handoff_pid_when_container_pid_is_dead(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            tracked = work_dir / "tracked.bin"
            tracked.write_bytes(b"payload")
            st = tracked.stat()

            raw_events = work_dir / "raw_events.jsonl"
            raw_rows = [
                {
                    "type": "OPEN",
                    "pid": os.getpid(),
                    "tgid": os.getpid(),
                    "path": str(tracked),
                    "ts_ns": 1,
                },
                {
                    "type": "MMAP",
                    "pid": os.getpid(),
                    "tgid": os.getpid(),
                    "dev": int(st.st_dev),
                    "ino": int(st.st_ino),
                    "ts_ns": 2,
                },
            ]
            raw_events.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in raw_rows),
                encoding="utf-8",
            )
            runtime_state = {
                "container_name": "self",
                "container_pid": 999999,
                "live_container_pid": os.getpid(),
                "self_starting_service": True,
                "ready_url": "http://127.0.0.1:8000/ready",
                "startup_mode_used": "legacy",
                "prom_port": 18080,
                "raw_events": str(raw_events),
            }
            (work_dir / "runtime_state.json").write_text(
                json.dumps(runtime_state, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            result = run_export_enrich_stage(
                repo_root=ROOT,
                work_dir=work_dir,
            )

            stage_log = result.stage_log_path.read_text(encoding="utf-8")
            self.assertIn(f"[handoff] using pid={os.getpid()} source=live_container_pid", stage_log)
            events = [
                json.loads(line)
                for line in result.events_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(1, len(events))
            self.assertEqual(str(tracked), events[0]["path"])

    def test_export_enrich_stage_prefers_explicit_target_pid_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            tracked = work_dir / "tracked.bin"
            tracked.write_bytes(b"payload")
            st = tracked.stat()

            raw_events = work_dir / "raw_events.jsonl"
            raw_rows = [
                {
                    "type": "OPEN",
                    "pid": os.getpid(),
                    "tgid": os.getpid(),
                    "path": str(tracked),
                    "ts_ns": 1,
                },
                {
                    "type": "MMAP",
                    "pid": os.getpid(),
                    "tgid": os.getpid(),
                    "dev": int(st.st_dev),
                    "ino": int(st.st_ino),
                    "ts_ns": 2,
                },
            ]
            raw_events.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in raw_rows),
                encoding="utf-8",
            )
            runtime_state = {
                "container_name": "self",
                "container_pid": 999999,
                "live_container_pid": 888888,
                "live_container_pid_source": "ready_listener",
                "root_pid": 999999,
                "root_pid_source": "container_pid",
                "target_pid": os.getpid(),
                "target_pid_source": "container_pid",
                "self_starting_service": True,
                "startup_backend": "gated_host_launcher_static",
                "ready_url": "http://127.0.0.1:8000/ready",
                "startup_mode_used": "gated",
                "prom_port": 18080,
                "raw_events": str(raw_events),
            }
            (work_dir / "runtime_state.json").write_text(
                json.dumps(runtime_state, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            result = run_export_enrich_stage(
                repo_root=ROOT,
                work_dir=work_dir,
            )

            stage_log = result.stage_log_path.read_text(encoding="utf-8")
            self.assertIn(f"[handoff] using pid={os.getpid()} source=container_pid", stage_log)

    def test_export_enrich_stage_resolves_at_fdcwd_without_dirfd_ambiguity(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            tracked = work_dir / "tracked.bin"
            tracked.write_bytes(b"payload")
            raw_events = work_dir / "raw_events.jsonl"
            raw_rows = [
                {
                    "type": "OPEN",
                    "pid": os.getpid(),
                    "tgid": os.getpid(),
                    "dirfd": -100,
                    "path": tracked.name,
                    "ts_ns": 1,
                }
            ]
            raw_events.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in raw_rows),
                encoding="utf-8",
            )
            runtime_state = {
                "container_name": "self",
                "container_pid": os.getpid(),
                "startup_mode_used": "legacy",
                "prom_port": 18080,
                "raw_events": str(raw_events),
            }
            (work_dir / "runtime_state.json").write_text(
                json.dumps(runtime_state, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            prev_cwd = os.getcwd()
            try:
                os.chdir(work_dir)
                result = run_export_enrich_stage(
                    repo_root=ROOT,
                    work_dir=work_dir,
                )
            finally:
                os.chdir(prev_cwd)

            events = [
                json.loads(line)
                for line in result.events_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            export_stats = json.loads(result.export_stats_path.read_text(encoding="utf-8"))
            enriched = [
                json.loads(line)
                for line in result.events_enriched_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            open_rows = [row for row in enriched if row.get("type") == "OPEN" and row.get("path") == str(tracked)]

            self.assertEqual(1, len(events))
            self.assertEqual(str(tracked), events[0]["path"])
            self.assertEqual("cwd", events[0].get("path_resolution"))
            self.assertEqual(-100, events[0].get("dirfd"))
            self.assertNotIn("path_resolution_risk", events[0])
            self.assertIn("explicit AT_FDCWD", events[0].get("path_resolution_detail", ""))
            self.assertEqual(1, export_stats["relative_open_total"])
            self.assertEqual(1, export_stats["relative_open_resolved_at_fdcwd"])
            self.assertEqual(1, len(open_rows))
            self.assertEqual(-100, open_rows[0].get("dirfd"))


if __name__ == "__main__":
    unittest.main()
