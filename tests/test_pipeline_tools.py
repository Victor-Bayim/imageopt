import json
import os
import subprocess
import sys
import tempfile
import unittest
import gzip
import hashlib
import io
import struct
import tarfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def make_estargz_footer(toc_offset: int) -> bytes:
    extra = b"SG" + struct.pack("<H", 16 + len("STARGZ")) + f"{toc_offset:016x}STARGZ".encode("ascii")
    base = gzip.compress(b"", compresslevel=0, mtime=0)
    header = bytearray(base[:10])
    header[3] |= 0x04
    return bytes(header) + struct.pack("<H", len(extra)) + extra + base[10:]


def make_external_estargz_footer() -> bytes:
    extra = b"SG" + struct.pack("<H", len("STARGZEXTERNALTOC")) + b"STARGZEXTERNALTOC"
    base = gzip.compress(b"", compresslevel=0, mtime=0)
    header = bytearray(base[:10])
    header[3] |= 0x04
    return bytes(header) + struct.pack("<H", len(extra)) + extra + base[10:]


def make_external_toc_layer(toc_bytes: bytes) -> bytes:
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w") as tf:
        info = tarfile.TarInfo("stargz.index.json")
        info.size = len(toc_bytes)
        tf.addfile(info, io.BytesIO(toc_bytes))
    return gzip.compress(tar_buffer.getvalue(), compresslevel=6, mtime=0)


class ExportEventsTests(unittest.TestCase):
    def test_raw_index_survives_bad_lines(self) -> None:
        script = REPO_ROOT / "optimizer/tools/export_events.py"
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            tracked = tmp_path / "tracked.txt"
            tracked.write_text("payload", encoding="utf-8")
            out_path = tmp_path / "events.jsonl"

            lines = [
                json.dumps({"type": "OPEN", "pid": 1, "tgid": 1, "path": str(tracked)}),
                "{not-json",
                json.dumps({"type": "OPEN", "pid": 1, "tgid": 1, "path": str(tracked)}),
            ]
            proc = subprocess.run(
                [sys.executable, str(script), "--container-pid", str(os.getpid()), str(out_path)],
                input="\n".join(lines) + "\n",
                text=True,
                cwd=REPO_ROOT,
                capture_output=True,
                check=True,
            )

            records = [
                json.loads(line)
                for line in out_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

            self.assertEqual([record["raw_index"] for record in records], [1, 3], proc.stderr)
            self.assertTrue(all(int(record.get("dev", 0)) > 0 for record in records), proc.stderr)
            self.assertTrue(all(int(record.get("ino", 0)) > 0 for record in records), proc.stderr)
            self.assertTrue(all(record.get("path_kind") == "regular" for record in records), proc.stderr)
            self.assertTrue(all(record.get("devino_source") == "stat" for record in records), proc.stderr)

    def test_relative_open_at_fdcwd_resolves_without_dirfd_ambiguity(self) -> None:
        script = REPO_ROOT / "optimizer/tools/export_events.py"
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            tracked = tmp_path / "tracked.txt"
            tracked.write_text("payload", encoding="utf-8")
            cwd_path = tmp_path / "cwd.json"
            out_path = tmp_path / "events.jsonl"
            cwd_path.write_text(json.dumps({str(os.getpid()): str(tmp_path)}), encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--cwd-json",
                    str(cwd_path),
                    "--container-pid",
                    str(os.getpid()),
                    str(out_path),
                ],
                input=json.dumps(
                    {
                        "type": "OPEN",
                        "pid": os.getpid(),
                        "tgid": os.getpid(),
                        "dirfd": -100,
                        "path": tracked.name,
                    }
                )
                + "\n",
                text=True,
                cwd=REPO_ROOT,
                capture_output=True,
                check=True,
            )

            records = [
                json.loads(line)
                for line in out_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

            self.assertEqual(1, len(records), proc.stderr)
            self.assertEqual(str(tracked), records[0]["path"])
            self.assertEqual("cwd", records[0]["path_resolution"])
            self.assertNotIn("path_resolution_risk", records[0])
            self.assertEqual(-100, records[0]["dirfd"])
            self.assertIn("explicit AT_FDCWD", records[0]["path_resolution_detail"])
            self.assertIn("resolved_relative_open_at_fdcwd=1", proc.stderr)

    def test_relative_open_with_explicit_dirfd_is_not_resolved_via_cwd(self) -> None:
        script = REPO_ROOT / "optimizer/tools/export_events.py"
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            tracked = tmp_path / "tracked.txt"
            tracked.write_text("payload", encoding="utf-8")
            cwd_path = tmp_path / "cwd.json"
            out_path = tmp_path / "events.jsonl"
            stats_path = tmp_path / "export_stats.json"
            cwd_path.write_text(json.dumps({str(os.getpid()): str(tmp_path)}), encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--cwd-json",
                    str(cwd_path),
                    "--container-pid",
                    str(os.getpid()),
                    "--stats-json",
                    str(stats_path),
                    str(out_path),
                ],
                input=json.dumps(
                    {
                        "type": "OPEN",
                        "pid": os.getpid(),
                        "tgid": os.getpid(),
                        "dirfd": 7,
                        "path": tracked.name,
                    }
                )
                + "\n",
                text=True,
                cwd=REPO_ROOT,
                capture_output=True,
                check=True,
            )

            self.assertEqual("", out_path.read_text(encoding="utf-8"))
            self.assertIn("dirfd_relative_open_unresolved=1", proc.stderr)
            stats = json.loads(stats_path.read_text(encoding="utf-8"))
            self.assertEqual(1, stats["relative_open_total"])
            self.assertEqual(1, stats["relative_open_explicit_dirfd_skipped"])
            self.assertEqual(0, stats["relative_open_resolved_total"])

    def test_open_stat_miss_is_recorded_as_provenance(self) -> None:
        script = REPO_ROOT / "optimizer/tools/export_events.py"
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            missing = tmp_path / "missing.bin"
            out_path = tmp_path / "events.jsonl"
            stats_path = tmp_path / "export_stats.json"

            proc = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--container-pid",
                    str(os.getpid()),
                    "--stats-json",
                    str(stats_path),
                    str(out_path),
                ],
                input=json.dumps(
                    {
                        "type": "OPEN",
                        "pid": os.getpid(),
                        "tgid": os.getpid(),
                        "path": str(missing),
                    }
                )
                + "\n",
                text=True,
                cwd=REPO_ROOT,
                capture_output=True,
                check=True,
            )

            rows = [
                json.loads(line)
                for line in out_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            stats = json.loads(stats_path.read_text(encoding="utf-8"))

            self.assertEqual(1, len(rows), proc.stderr)
            self.assertEqual("miss", rows[0]["stat_lookup"])
            self.assertEqual("not_found", rows[0]["stat_lookup_kind"])
            self.assertEqual(1, stats["open_stat_fail"])
            self.assertEqual(1, stats["open_stat_fail_not_found"])


class TocShadowTests(unittest.TestCase):
    def test_toc_shadow_audit_summarizes_selected_chunk_overlap(self) -> None:
        script = REPO_ROOT / "optimizer/tools/build_toc_shadow.py"
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            toc_path = tmp_path / "image_toc.json"
            candidates_path = tmp_path / "candidates.json"
            selected_path = tmp_path / "estargz.list"
            out_path = tmp_path / "toc_shadow.json"

            toc_path.write_text(
                json.dumps(
                    {
                        "entries": [
                            {
                                "path": "/app/a.so",
                                "layer_digest": "sha256:layer1",
                                "chunks": [
                                    {"offset": 0, "size": 4096, "compressed_size": 1024},
                                    {"offset": 4096, "size": 4096, "compressed_size": 1024},
                                ],
                            },
                            {
                                "path": "/app/b.so",
                                "layer_digest": "sha256:layer1",
                                "chunks": [
                                    {"offset": 4096, "size": 4096, "compressed_size": 1024},
                                ],
                            },
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            candidates_path.write_text(
                json.dumps(
                    [
                        {"path": "/app/a.so"},
                        {"path": "/app/b.so"},
                        {"path": "/app/missing.so"},
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            selected_path.write_text("/app/a.so\n/app/b.so\n", encoding="utf-8")

            subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--toc-json",
                    str(toc_path),
                    "--candidates",
                    str(candidates_path),
                    "--selected-list",
                    str(selected_path),
                    "--out",
                    str(out_path),
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=True,
            )

            payload = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(2, payload["toc_entry_count"])
            self.assertEqual(3, payload["candidate_paths_total"])
            self.assertEqual(2, payload["candidate_paths_found"])
            self.assertEqual(1, payload["candidate_paths_missing"])
            self.assertAlmostEqual(2.0 / 3.0, payload["candidate_paths_found_ratio"])
            self.assertEqual(2, payload["selected_paths_total"])
            self.assertEqual(2, payload["selected_paths_found"])
            self.assertEqual(0, payload["selected_paths_missing"])
            self.assertEqual(1.0, payload["selected_paths_found_ratio"])
            self.assertEqual(1, payload["selected_unique_layer_count"])
            self.assertEqual(2, payload["selected_unique_chunk_count"])
            self.assertEqual(1, payload["selected_shared_chunk_count"])
            self.assertEqual(12288, payload["selected_path_sum_bytes"])
            self.assertEqual(3072, payload["selected_path_sum_compressed_bytes"])
            self.assertEqual(8192, payload["selected_estimated_bytes"])
            self.assertEqual(2048, payload["selected_estimated_compressed_bytes"])
            self.assertEqual(4096, payload["selected_shared_chunk_bytes"])
            self.assertEqual(1024, payload["selected_shared_chunk_compressed_bytes"])
            self.assertEqual(4096, payload["selected_overlap_bytes"])
            self.assertEqual(1024, payload["selected_overlap_compressed_bytes"])
            self.assertAlmostEqual(4096.0 / 12288.0, payload["selected_overlap_ratio"])
            self.assertAlmostEqual(1024.0 / 3072.0, payload["selected_overlap_compressed_ratio"])
            self.assertEqual(2, payload["shared_chunks_top"][0]["path_count"])
            self.assertEqual([], payload["selected_missing_paths_preview"])
            self.assertIn("1 candidate path(s) were not found in image TOC", payload["warnings"])
            self.assertIn("selected paths show high chunk overlap (33.33% of path-sum bytes)", payload["warnings"])


class ChunkSelectorShadowTests(unittest.TestCase):
    def test_chunk_selector_shadow_flags_low_yield_selected_paths(self) -> None:
        script = REPO_ROOT / "optimizer/tools/build_chunk_selector_shadow.py"
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            toc_path = tmp_path / "image_toc.json"
            candidates_path = tmp_path / "candidates.json"
            selected_path = tmp_path / "estargz.list"
            out_path = tmp_path / "chunk_selector_shadow.json"

            toc_path.write_text(
                json.dumps(
                    {
                        "entries": [
                            {
                                "path": "/app/a.so",
                                "layer_digest": "sha256:layer1",
                                "chunks": [
                                    {"offset": 0, "size": 4096, "compressed_size": 1024},
                                    {"offset": 4096, "size": 4096, "compressed_size": 1024},
                                ],
                            },
                            {
                                "path": "/app/b.so",
                                "layer_digest": "sha256:layer1",
                                "chunks": [
                                    {"offset": 4096, "size": 4096, "compressed_size": 1024},
                                ],
                            },
                            {
                                "path": "/app/c.so",
                                "layer_digest": "sha256:layer2",
                                "chunks": [
                                    {"offset": 0, "size": 4096, "compressed_size": 1024},
                                ],
                            },
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            candidates_path.write_text(
                json.dumps(
                    [
                        {
                            "path": "/app/a.so",
                            "weighted_count": 5.0,
                            "count": 5,
                            "confidence_score": 7.5,
                            "first_ratio": 0.05,
                            "aliases": ["/app/a.so"],
                        },
                        {
                            "path": "/app/b.so",
                            "weighted_count": 2.0,
                            "count": 2,
                            "confidence_score": 2.5,
                            "first_ratio": 0.10,
                            "aliases": ["/app/b.so"],
                        },
                        {
                            "path": "/app/c.so",
                            "weighted_count": 3.0,
                            "count": 3,
                            "confidence_score": 3.5,
                            "first_ratio": 0.12,
                            "aliases": ["/app/c.so"],
                        },
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            selected_path.write_text("/app/a.so\n/app/b.so\n", encoding="utf-8")

            subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--toc-json",
                    str(toc_path),
                    "--candidates",
                    str(candidates_path),
                    "--selected-list",
                    str(selected_path),
                    "--out",
                    str(out_path),
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=True,
            )

            payload = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(2, payload["selected_paths_total"])
            self.assertEqual(2, payload["selected_paths_found"])
            self.assertEqual(0, payload["selected_paths_missing"])
            self.assertEqual(3072, payload["selected_path_sum_compressed_bytes"])
            self.assertEqual(2048, payload["selected_incremental_compressed_bytes"])
            self.assertEqual(1024, payload["selected_overlap_compressed_bytes"])
            self.assertAlmostEqual(1024.0 / 3072.0, payload["selected_overlap_compressed_ratio"])
            self.assertEqual(1, payload["selected_zero_incremental_paths"])
            self.assertEqual(1, payload["selected_overlap_heavy_paths"])
            self.assertEqual("/app/b.so", payload["zero_incremental_paths_preview"][0]["path"])
            self.assertEqual("/app/b.so", payload["overlap_heavy_paths_preview"][0]["path"])
            self.assertEqual(
                0,
                payload["overlap_heavy_paths_preview"][0]["incremental_compressed_bytes"],
            )
            self.assertEqual(1024, payload["shadow_incremental_compressed_bytes_delta"])
            self.assertEqual(-1024, payload["shadow_overlap_compressed_bytes_delta"])
            self.assertEqual(2, payload["shadow_alternative"]["selected_paths_total"])
            self.assertEqual(3072, payload["shadow_alternative"]["selected_path_sum_compressed_bytes"])
            self.assertEqual(3072, payload["shadow_alternative"]["selected_incremental_compressed_bytes"])
            self.assertEqual(["/app/c.so"], payload["shadow_alternative"]["shadow_only_paths_preview"])
            self.assertEqual(["/app/b.so"], payload["shadow_alternative"]["current_only_paths_preview"])
            self.assertEqual("/app/b.so", payload["replacement_suggestions_preview"][0]["replace_path"])
            self.assertEqual("/app/c.so", payload["replacement_suggestions_preview"][0]["add_path"])
            self.assertEqual(1024, payload["replacement_suggestions_preview"][0]["delta_incremental_compressed_bytes"])
            self.assertIn(
                "1 selected path(s) add zero new compressed TOC bytes in current order",
                payload["warnings"],
            )
            self.assertIn(
                "chunk-aware greedy shadow would add 1024 more compressed TOC bytes under the same path-sum budget",
                payload["warnings"],
            )
            self.assertIn(
                "shadow suggests replacing /app/b.so with /app/c.so (+1024 incremental compressed TOC bytes)",
                payload["warnings"],
            )


class ExportImageTocTests(unittest.TestCase):
    def test_export_image_toc_reads_inline_estargz_toc_from_blob_dir(self) -> None:
        script = REPO_ROOT / "optimizer/tools/export_image_toc.py"
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            manifest_path = tmp_path / "manifest.json"
            blob_dir = tmp_path / "blobs"
            blob_dir.mkdir(parents=True, exist_ok=True)
            out_path = tmp_path / "image_toc.json"

            toc_payload = {
                "version": 1,
                "entries": [
                    {
                        "name": "app/a.so",
                        "type": "reg",
                        "size": 8192,
                        "offset": 128,
                        "chunkSize": 4096,
                        "chunkDigest": "sha256:first",
                    },
                    {
                        "name": "app/a.so",
                        "type": "chunk",
                        "offset": 4096,
                        "chunkOffset": 4096,
                        "chunkSize": 0,
                        "chunkDigest": "sha256:second",
                    },
                ],
            }
            toc_bytes = json.dumps(toc_payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            blob_bytes = b"payload" + gzip.compress(toc_bytes, compresslevel=6, mtime=0)
            blob_bytes += make_estargz_footer(len(b"payload"))
            layer_digest = "sha256:" + ("ab" * 32)
            toc_digest = "sha256:" + hashlib.sha256(toc_bytes).hexdigest()
            (blob_dir / layer_digest.replace(":", "_")).write_bytes(blob_bytes)
            manifest_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 2,
                        "layers": [
                            {
                                "digest": layer_digest,
                                "annotations": {
                                    "containerd.io/snapshot/stargz/toc.digest": toc_digest,
                                },
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--manifest-json",
                    str(manifest_path),
                    "--blob-dir",
                    str(blob_dir),
                    "--out",
                    str(out_path),
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=True,
            )

            payload = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(1, payload["format_version"])
            self.assertEqual(1, len(payload["layers"]))
            self.assertTrue(payload["layers"][0]["toc_digest_verified"])
            self.assertEqual(1, len(payload["entries"]))
            self.assertEqual("/app/a.so", payload["entries"][0]["path"])
            self.assertEqual(2, len(payload["entries"][0]["chunks"]))
            self.assertEqual(4096, payload["entries"][0]["chunks"][0]["size"])
            self.assertEqual(4096, payload["entries"][0]["chunks"][1]["size"])

    def test_export_image_toc_reads_external_toc_from_toc_manifest(self) -> None:
        script = REPO_ROOT / "optimizer/tools/export_image_toc.py"
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            manifest_path = tmp_path / "manifest.json"
            toc_manifest_path = tmp_path / "toc_manifest.json"
            blob_dir = tmp_path / "blobs"
            blob_dir.mkdir(parents=True, exist_ok=True)
            out_path = tmp_path / "image_toc.json"

            toc_payload = {
                "version": 1,
                "entries": [
                    {
                        "name": "app/ext.so",
                        "type": "reg",
                        "size": 4096,
                        "offset": 256,
                        "chunkSize": 0,
                        "chunkDigest": "sha256:chunk",
                    }
                ],
            }
            toc_bytes = json.dumps(toc_payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            layer_digest = "sha256:" + ("cd" * 32)
            toc_layer_digest = "sha256:" + ("ef" * 32)
            toc_digest = "sha256:" + hashlib.sha256(toc_bytes).hexdigest()

            (blob_dir / layer_digest.replace(":", "_")).write_bytes(b"payload" + make_external_estargz_footer())
            (blob_dir / toc_layer_digest.replace(":", "_")).write_bytes(make_external_toc_layer(toc_bytes))
            manifest_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 2,
                        "layers": [
                            {
                                "digest": layer_digest,
                                "annotations": {
                                    "containerd.io/snapshot/stargz/toc.digest": toc_digest,
                                },
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            toc_manifest_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 2,
                        "layers": [
                            {
                                "digest": toc_layer_digest,
                                "annotations": {
                                    "containerd.io/snapshot/stargz/layer.digest": layer_digest,
                                },
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--manifest-json",
                    str(manifest_path),
                    "--toc-manifest-json",
                    str(toc_manifest_path),
                    "--blob-dir",
                    str(blob_dir),
                    "--out",
                    str(out_path),
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=True,
            )

            payload = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(1, len(payload["layers"]))
            self.assertEqual("external", payload["layers"][0]["toc_source"])
            self.assertEqual(toc_layer_digest, payload["layers"][0]["toc_layer_digest"])
            self.assertTrue(payload["layers"][0]["toc_digest_verified"])
            self.assertEqual("/app/ext.so", payload["entries"][0]["path"])
            self.assertEqual(1, len(payload["entries"][0]["chunks"]))
            self.assertEqual(4096, payload["entries"][0]["chunks"][0]["size"])


class SelectorTests(unittest.TestCase):
    def run_selector(
        self,
        candidates,
        *,
        preferred_lines=None,
        aggregate_stats=None,
        budget_mb="1",
        min_hits="2",
        early_onehit_max="8",
    ):
        script = REPO_ROOT / "optimizer/tools/select_prefetch_paths.py"
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            candidates_path = tmp_path / "candidates.json"
            out_yaml = tmp_path / "ranked.yaml"
            out_list = tmp_path / "estargz.list"
            stats_path = tmp_path / "selector_stats.json"
            aggregate_stats_path = tmp_path / "aggregate_stats.json"
            preferred_path = tmp_path / "preferred.txt"

            candidates_path.write_text(json.dumps(candidates), encoding="utf-8")
            cmd = [
                sys.executable,
                str(script),
                "--candidates",
                str(candidates_path),
                "--out-yaml",
                str(out_yaml),
                "--out-list",
                str(out_list),
                "--stats-json",
                str(stats_path),
                "--budget-mb",
                budget_mb,
                "--min-hits",
                min_hits,
                "--early-onehit-max",
                early_onehit_max,
            ]
            if preferred_lines is not None:
                preferred_path.write_text("\n".join(preferred_lines) + "\n", encoding="utf-8")
                cmd.extend(["--preferred", str(preferred_path)])
            if aggregate_stats is not None:
                aggregate_stats_path.write_text(json.dumps(aggregate_stats), encoding="utf-8")
                cmd.extend(["--aggregate-stats", str(aggregate_stats_path)])

            proc = subprocess.run(
                cmd,
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=True,
            )

            chosen = [line.strip() for line in out_list.read_text(encoding="utf-8").splitlines() if line.strip()]
            stats = json.loads(stats_path.read_text(encoding="utf-8"))
            return chosen, stats, proc

    def test_preferred_alias_hits_identity_merged_candidate(self) -> None:
        candidates = [
            {
                "path": "/canonical/libfoo.so",
                "size_bytes": 4096,
                "weighted_count": 5,
                "weighted_open_count": 5,
                "weighted_mmap_count": 0,
                "weighted_open_with_devino_count": 5,
                "weighted_via_open_count": 5,
                "weighted_via_mapfiles_count": 0,
                "weighted_via_dict_count": 0,
                "weighted_via_maps_count": 0,
                "first_index": 1,
                "first_ratio": 0.02,
                "dev": 11,
                "ino": 22,
            },
            {
                "path": "/alias/libfoo.so",
                "size_bytes": 4096,
                "weighted_count": 3,
                "weighted_open_count": 3,
                "weighted_mmap_count": 0,
                "weighted_open_with_devino_count": 3,
                "weighted_via_open_count": 3,
                "weighted_via_mapfiles_count": 0,
                "weighted_via_dict_count": 0,
                "weighted_via_maps_count": 0,
                "first_index": 2,
                "first_ratio": 0.03,
                "dev": 11,
                "ino": 22,
            },
            {
                "path": "/lib/libbar.so",
                "size_bytes": 8192,
                "weighted_count": 4,
                "weighted_open_count": 4,
                "weighted_mmap_count": 0,
                "weighted_open_with_devino_count": 4,
                "weighted_via_open_count": 4,
                "weighted_via_mapfiles_count": 0,
                "weighted_via_dict_count": 0,
                "weighted_via_maps_count": 0,
                "first_index": 10,
                "first_ratio": 0.20,
                "dev": 33,
                "ino": 44,
            },
        ]

        chosen, stats, _ = self.run_selector(
            candidates,
            preferred_lines=["/alias/libfoo.so"],
        )

        self.assertGreaterEqual(len(chosen), 1)
        self.assertEqual(chosen[0], "/canonical/libfoo.so")
        self.assertEqual(stats["preferred_count"], 1)
        self.assertEqual(stats["identity_alias_groups"], 1)

    def test_selector_skips_non_regular_candidates(self) -> None:
        candidates = [
            {
                "path": "/models/demo",
                "size_bytes": 4096,
                "weighted_count": 8,
                "weighted_open_count": 8,
                "weighted_open_with_devino_count": 8,
                "weighted_via_open_count": 8,
                "first_index": 1,
                "first_ratio": 0.01,
                "dev": 1,
                "ino": 2,
                "path_kind": "directory",
            },
            {
                "path": "/models/demo/1/saved_model.pb",
                "size_bytes": 12107,
                "weighted_count": 4,
                "weighted_open_count": 4,
                "weighted_open_with_devino_count": 4,
                "weighted_via_open_count": 4,
                "first_index": 2,
                "first_ratio": 0.02,
                "dev": 1,
                "ino": 3,
                "path_kind": "regular",
            },
        ]

        chosen, stats, _ = self.run_selector(candidates)

        self.assertEqual(chosen, ["/models/demo/1/saved_model.pb"])
        self.assertEqual(stats["non_regular_candidates_skipped"], 1)
        self.assertEqual({"directory": 1}, stats["non_regular_kind_counts"])

    def test_identity_guard_strict_disables_early_onehit(self) -> None:
        candidates = [
            {
                "path": "/lib/regular.so",
                "size_bytes": 4096,
                "weighted_count": 4,
                "weighted_open_count": 4,
                "weighted_mmap_count": 0,
                "weighted_open_with_devino_count": 0,
                "weighted_via_open_count": 4,
                "weighted_via_mapfiles_count": 0,
                "weighted_via_dict_count": 0,
                "weighted_via_maps_count": 0,
                "first_index": 1,
                "first_ratio": 0.02,
                "dev": 0,
                "ino": 0,
            },
            {
                "path": "/lib/onehit.cfg",
                "size_bytes": 1024,
                "weighted_count": 1,
                "weighted_open_count": 1,
                "weighted_mmap_count": 0,
                "weighted_open_with_devino_count": 0,
                "weighted_via_open_count": 1,
                "weighted_via_mapfiles_count": 0,
                "weighted_via_dict_count": 0,
                "weighted_via_maps_count": 0,
                "first_index": 2,
                "first_ratio": 0.01,
                "dev": 0,
                "ino": 0,
            },
        ]

        chosen, stats, _ = self.run_selector(
            candidates,
            aggregate_stats={
                "unique_paths": 24,
                "paths_with_devino": 0,
                "identity_candidates": 24,
                "devino_identity_candidates": 0,
                "identity_alias_groups": 0,
                "identity_merged_paths": 0,
            },
        )

        self.assertEqual(chosen, ["/lib/regular.so"])
        self.assertEqual(stats["identity_guard_mode"], "strict")
        self.assertEqual(stats["effective_early_onehit_max"], 0)

    def test_identity_guarded_mode_skips_maps_heavy_weak_evidence(self) -> None:
        candidates = [
            {
                "path": "/lib/strong.so",
                "size_bytes": 4096,
                "weighted_count": 4,
                "weighted_open_count": 4,
                "weighted_mmap_count": 0,
                "weighted_open_with_devino_count": 1,
                "weighted_via_open_count": 4,
                "weighted_via_mapfiles_count": 0,
                "weighted_via_dict_count": 0,
                "weighted_via_maps_count": 0,
                "first_index": 1,
                "first_ratio": 0.02,
                "dev": 1,
                "ino": 2,
            },
            {
                "path": "/lib/weak-mmaps.so",
                "size_bytes": 4096,
                "weighted_count": 2,
                "weighted_open_count": 0,
                "weighted_mmap_count": 2,
                "weighted_open_with_devino_count": 0,
                "weighted_via_open_count": 0,
                "weighted_via_mapfiles_count": 0,
                "weighted_via_dict_count": 0,
                "weighted_via_maps_count": 1.4,
                "first_index": 2,
                "first_ratio": 0.03,
                "dev": 0,
                "ino": 0,
            },
        ]

        chosen_normal, stats_normal, _ = self.run_selector(candidates)
        self.assertIn("/lib/weak-mmaps.so", chosen_normal)
        self.assertEqual(stats_normal["identity_guard_mode"], "normal")

        chosen_guarded, stats_guarded, _ = self.run_selector(
            candidates,
            aggregate_stats={
                "unique_paths": 12,
                "paths_with_devino": 1,
                "identity_candidates": 12,
                "devino_identity_candidates": 1,
                "identity_alias_groups": 0,
                "identity_merged_paths": 0,
            },
        )

        self.assertEqual(chosen_guarded, ["/lib/strong.so"])
        self.assertEqual(stats_guarded["identity_guard_mode"], "guarded")
        self.assertLess(stats_guarded["max_maps_ratio_without_strong_evidence"], 0.8)

    def test_identity_guarded_mode_when_devino_is_open_stat_only(self) -> None:
        candidates = [
            {
                "path": "/lib/strong.so",
                "size_bytes": 4096,
                "weighted_count": 4,
                "weighted_open_count": 4,
                "weighted_mmap_count": 0,
                "weighted_open_with_devino_count": 4,
                "weighted_via_open_count": 4,
                "weighted_via_mapfiles_count": 0,
                "weighted_via_dict_count": 0,
                "weighted_via_maps_count": 0,
                "first_index": 1,
                "first_ratio": 0.02,
                "dev": 1,
                "ino": 2,
            },
            {
                "path": "/lib/weak-mmaps.so",
                "size_bytes": 4096,
                "weighted_count": 2,
                "weighted_open_count": 0,
                "weighted_mmap_count": 2,
                "weighted_open_with_devino_count": 0,
                "weighted_via_open_count": 0,
                "weighted_via_mapfiles_count": 0,
                "weighted_via_dict_count": 0,
                "weighted_via_maps_count": 1.4,
                "first_index": 2,
                "first_ratio": 0.03,
                "dev": 0,
                "ino": 0,
            },
        ]

        chosen, stats, _ = self.run_selector(
            candidates,
            aggregate_stats={
                "unique_paths": 12,
                "paths_with_devino": 4,
                "paths_with_open_stat_devino": 4,
                "paths_with_mmap_raw_devino": 0,
                "identity_candidates": 10,
                "devino_identity_candidates": 4,
                "identity_alias_groups": 0,
                "identity_merged_paths": 0,
            },
        )

        self.assertEqual(chosen, ["/lib/strong.so"])
        self.assertEqual(stats["identity_guard_mode"], "guarded")
        self.assertEqual(
            "dev:ino evidence relies entirely on OPEN stat backfill",
            stats["identity_guard_reason"],
        )
        self.assertEqual(4, stats["identity_paths_with_open_stat_devino"])
        self.assertEqual(0, stats["identity_paths_with_mmap_raw_devino"])

    def test_identity_strict_mode_when_devino_is_open_stat_only_on_large_sample(self) -> None:
        candidates = [
            {
                "path": "/lib/regular.so",
                "size_bytes": 4096,
                "weighted_count": 4,
                "weighted_open_count": 4,
                "weighted_mmap_count": 0,
                "weighted_open_with_devino_count": 4,
                "weighted_via_open_count": 4,
                "weighted_via_mapfiles_count": 0,
                "weighted_via_dict_count": 0,
                "weighted_via_maps_count": 0,
                "first_index": 1,
                "first_ratio": 0.02,
                "dev": 1,
                "ino": 2,
            },
            {
                "path": "/lib/onehit.cfg",
                "size_bytes": 1024,
                "weighted_count": 1,
                "weighted_open_count": 1,
                "weighted_mmap_count": 0,
                "weighted_open_with_devino_count": 1,
                "weighted_via_open_count": 1,
                "weighted_via_mapfiles_count": 0,
                "weighted_via_dict_count": 0,
                "weighted_via_maps_count": 0,
                "first_index": 2,
                "first_ratio": 0.01,
                "dev": 1,
                "ino": 3,
            },
        ]

        chosen, stats, _ = self.run_selector(
            candidates,
            aggregate_stats={
                "unique_paths": 24,
                "paths_with_devino": 6,
                "paths_with_open_stat_devino": 6,
                "paths_with_mmap_raw_devino": 0,
                "identity_candidates": 20,
                "devino_identity_candidates": 6,
                "identity_alias_groups": 0,
                "identity_merged_paths": 0,
            },
        )

        self.assertEqual(chosen, ["/lib/regular.so"])
        self.assertEqual(stats["identity_guard_mode"], "strict")
        self.assertEqual(0, stats["effective_early_onehit_max"])
        self.assertEqual(
            "dev:ino evidence relies entirely on OPEN stat backfill",
            stats["identity_guard_reason"],
        )

    def test_identity_guarded_mode_when_cwd_resolved_open_share_is_high(self) -> None:
        candidates = [
            {
                "path": "/lib/strong.so",
                "size_bytes": 4096,
                "weighted_count": 4,
                "weighted_open_count": 4,
                "weighted_mmap_count": 0,
                "weighted_open_with_devino_count": 4,
                "weighted_via_open_count": 4,
                "weighted_via_mapfiles_count": 0,
                "weighted_via_dict_count": 0,
                "weighted_via_maps_count": 0,
                "first_index": 1,
                "first_ratio": 0.02,
                "dev": 1,
                "ino": 2,
            },
            {
                "path": "/lib/weak-mmaps.so",
                "size_bytes": 4096,
                "weighted_count": 2,
                "weighted_open_count": 0,
                "weighted_mmap_count": 2,
                "weighted_open_with_devino_count": 0,
                "weighted_via_open_count": 0,
                "weighted_via_mapfiles_count": 0,
                "weighted_via_dict_count": 0,
                "weighted_via_maps_count": 1.4,
                "first_index": 2,
                "first_ratio": 0.03,
                "dev": 0,
                "ino": 0,
            },
        ]

        chosen, stats, _ = self.run_selector(
            candidates,
            aggregate_stats={
                "unique_paths": 12,
                "paths_with_devino": 4,
                "paths_with_open_resolved_from_cwd": 5,
                "paths_with_open_cwd_dirfd_ambiguous": 5,
                "paths_with_open_stat_devino": 2,
                "paths_with_mmap_raw_devino": 0,
                "identity_candidates": 10,
                "devino_identity_candidates": 4,
                "identity_alias_groups": 0,
                "identity_merged_paths": 0,
            },
        )

        self.assertEqual(chosen, ["/lib/strong.so"])
        self.assertEqual(stats["identity_guard_mode"], "guarded")
        self.assertEqual(
            "high dirfd-ambiguous OPEN share with weak mmap raw identity evidence",
            stats["identity_guard_reason"],
        )
        self.assertEqual(5, stats["identity_paths_with_open_resolved_from_cwd"])
        self.assertEqual(5, stats["identity_paths_with_open_cwd_dirfd_ambiguous"])

    def test_identity_guarded_mode_when_open_stat_miss_share_is_elevated(self) -> None:
        candidates = [
            {
                "path": "/lib/strong.so",
                "size_bytes": 4096,
                "weighted_count": 4,
                "weighted_open_count": 4,
                "weighted_mmap_count": 0,
                "weighted_open_with_devino_count": 4,
                "weighted_via_open_count": 4,
                "weighted_via_mapfiles_count": 0,
                "weighted_via_dict_count": 0,
                "weighted_via_maps_count": 0,
                "first_index": 1,
                "first_ratio": 0.02,
                "dev": 1,
                "ino": 2,
            },
            {
                "path": "/lib/weak-mmaps.so",
                "size_bytes": 4096,
                "weighted_count": 2,
                "weighted_open_count": 0,
                "weighted_mmap_count": 2,
                "weighted_open_with_devino_count": 0,
                "weighted_via_open_count": 0,
                "weighted_via_mapfiles_count": 0,
                "weighted_via_dict_count": 0,
                "weighted_via_maps_count": 1.4,
                "first_index": 2,
                "first_ratio": 0.03,
                "dev": 0,
                "ino": 0,
            },
        ]

        chosen, stats, _ = self.run_selector(
            candidates,
            aggregate_stats={
                "unique_paths": 12,
                "paths_with_devino": 4,
                "paths_with_open_stat_devino": 2,
                "paths_with_open_stat_miss": 3,
                "paths_with_mmap_raw_devino": 0,
                "identity_candidates": 10,
                "devino_identity_candidates": 4,
                "identity_alias_groups": 0,
                "identity_merged_paths": 0,
            },
        )

        self.assertEqual(chosen, ["/lib/strong.so"])
        self.assertEqual(stats["identity_guard_mode"], "guarded")
        self.assertEqual(
            "elevated OPEN stat-miss share with weak mmap raw identity evidence",
            stats["identity_guard_reason"],
        )
        self.assertEqual(3, stats["identity_paths_with_open_stat_miss"])

    def test_preferred_weak_evidence_path_is_pruned_under_strict_guard(self) -> None:
        candidates = [
            {
                "path": "/lib/strong.so",
                "size_bytes": 4096,
                "weighted_count": 4,
                "weighted_open_count": 4,
                "weighted_mmap_count": 0,
                "weighted_open_with_devino_count": 0,
                "weighted_via_open_count": 4,
                "weighted_via_mapfiles_count": 0,
                "weighted_via_dict_count": 0,
                "weighted_via_maps_count": 0,
                "first_index": 1,
                "first_ratio": 0.02,
                "dev": 0,
                "ino": 0,
            },
            {
                "path": "/lib/weak-preferred.so",
                "size_bytes": 4096,
                "weighted_count": 2,
                "weighted_open_count": 0,
                "weighted_mmap_count": 2,
                "weighted_open_with_devino_count": 0,
                "weighted_via_open_count": 0,
                "weighted_via_mapfiles_count": 0,
                "weighted_via_dict_count": 0,
                "weighted_via_maps_count": 1.6,
                "first_index": 2,
                "first_ratio": 0.03,
                "dev": 0,
                "ino": 0,
            },
        ]

        chosen, stats, _ = self.run_selector(
            candidates,
            preferred_lines=["/lib/weak-preferred.so"],
            aggregate_stats={
                "unique_paths": 24,
                "paths_with_devino": 0,
                "identity_candidates": 24,
                "devino_identity_candidates": 0,
                "identity_alias_groups": 0,
                "identity_merged_paths": 0,
            },
        )

        self.assertEqual(chosen, ["/lib/strong.so"])
        self.assertTrue(stats["preferred_guard_active"])
        self.assertTrue(stats["preferred_fallback_used"])
        self.assertEqual(stats["preferred_low_confidence_skipped"], 1)

    def test_preferred_weak_evidence_path_survives_in_normal_mode(self) -> None:
        candidates = [
            {
                "path": "/lib/strong.so",
                "size_bytes": 4096,
                "weighted_count": 4,
                "weighted_open_count": 4,
                "weighted_mmap_count": 0,
                "weighted_open_with_devino_count": 0,
                "weighted_via_open_count": 4,
                "weighted_via_mapfiles_count": 0,
                "weighted_via_dict_count": 0,
                "weighted_via_maps_count": 0,
                "first_index": 1,
                "first_ratio": 0.02,
                "dev": 0,
                "ino": 0,
            },
            {
                "path": "/lib/weak-preferred.so",
                "size_bytes": 4096,
                "weighted_count": 2,
                "weighted_open_count": 0,
                "weighted_mmap_count": 2,
                "weighted_open_with_devino_count": 0,
                "weighted_via_open_count": 0,
                "weighted_via_mapfiles_count": 0,
                "weighted_via_dict_count": 0,
                "weighted_via_maps_count": 1.6,
                "first_index": 2,
                "first_ratio": 0.03,
                "dev": 0,
                "ino": 0,
            },
        ]

        chosen, stats, _ = self.run_selector(
            candidates,
            preferred_lines=["/lib/weak-preferred.so"],
        )

        self.assertEqual(chosen, ["/lib/weak-preferred.so"])
        self.assertFalse(stats["preferred_guard_active"])
        self.assertFalse(stats["preferred_fallback_used"])
        self.assertEqual(stats["preferred_low_confidence_skipped"], 0)


if __name__ == "__main__":
    unittest.main()
