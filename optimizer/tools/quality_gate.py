#!/usr/bin/env python3
import argparse
import json
import os
import re
import sys


def iter_jsonl(path):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for ln in f:
            line = ln.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                try:
                    yield json.loads(json.loads(line))
                except Exception:
                    continue


def count_raw(path):
    counts = {
        "raw_total_lines": 0,
        "raw_valid_json": 0,
        "raw_parse_fail": 0,
        "raw_open": 0,
        "raw_open_empty_path": 0,
        "raw_open_relative_path": 0,
        "raw_open_relative_at_fdcwd": 0,
        "raw_open_relative_explicit_dirfd": 0,
        "raw_open_relative_missing_dirfd": 0,
        "raw_mmap": 0,
        "raw_mmap_zero_devino": 0,
    }
    if not os.path.exists(path):
        return counts

    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for ln in f:
            line = ln.strip()
            if not line:
                continue
            counts["raw_total_lines"] += 1
            obj = None
            try:
                obj = json.loads(line)
            except Exception:
                try:
                    obj = json.loads(json.loads(line))
                except Exception:
                    counts["raw_parse_fail"] += 1
                    continue
            counts["raw_valid_json"] += 1
            typ = str(obj.get("type", "")).upper()
            if typ == "OPEN":
                counts["raw_open"] += 1
                path_value = str(obj.get("path", "") or "")
                if not path_value:
                    counts["raw_open_empty_path"] += 1
                elif not path_value.startswith("/"):
                    counts["raw_open_relative_path"] += 1
                    try:
                        dirfd = int(obj.get("dirfd"))
                    except Exception:
                        dirfd = None
                    if dirfd == -100:
                        counts["raw_open_relative_at_fdcwd"] += 1
                    elif dirfd is None:
                        counts["raw_open_relative_missing_dirfd"] += 1
                    else:
                        counts["raw_open_relative_explicit_dirfd"] += 1
            elif typ == "MMAP":
                counts["raw_mmap"] += 1
                try:
                    dev = int(obj.get("dev", 0) or 0)
                    ino = int(obj.get("ino", 0) or 0)
                except Exception:
                    dev = 0
                    ino = 0
                if dev == 0 or ino == 0:
                    counts["raw_mmap_zero_devino"] += 1
    return counts


def count_events(path):
    counts = {
        "total": 0,
        "open": 0,
        "open_with_devino": 0,
        "open_resolved_from_cwd": 0,
        "open_cwd_dirfd_ambiguous": 0,
        "open_stat_miss": 0,
        "open_unresolved_relative_kept": 0,
    }
    for obj in iter_jsonl(path):
        counts["total"] += 1
        if str(obj.get("type", "")).upper() != "OPEN":
            continue
        counts["open"] += 1
        resolution = str(obj.get("path_resolution", "") or "")
        risk = str(obj.get("path_resolution_risk", "") or "")
        try:
            dev = int(obj.get("dev", 0) or 0)
            ino = int(obj.get("ino", 0) or 0)
        except Exception:
            dev = 0
            ino = 0
        if dev > 0 and ino > 0:
            counts["open_with_devino"] += 1
        if resolution == "cwd":
            counts["open_resolved_from_cwd"] += 1
        if risk == "dirfd_unavailable":
            counts["open_cwd_dirfd_ambiguous"] += 1
        if str(obj.get("stat_lookup", "") or "") == "miss":
            counts["open_stat_miss"] += 1
        elif resolution == "unresolved_relative":
            counts["open_unresolved_relative_kept"] += 1
    return counts


def count_enriched(path):
    counts = {
        "total": 0,
        "open": 0,
        "mmap": 0,
        "mmap_via_maps": 0,
        "mmap_via_mapfiles": 0,
        "mmap_via_dict": 0,
    }
    for obj in iter_jsonl(path):
        counts["total"] += 1
        typ = str(obj.get("type", "")).upper()
        via = str(obj.get("via", "") or "")
        if typ == "OPEN":
            counts["open"] += 1
        elif typ == "MMAP":
            counts["mmap"] += 1
            if via == "maps":
                counts["mmap_via_maps"] += 1
            elif via == "mapfiles":
                counts["mmap_via_mapfiles"] += 1
            elif via == "dict":
                counts["mmap_via_dict"] += 1
    return counts


def load_metrics(path):
    metrics = {}
    if not os.path.exists(path):
        return metrics
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for ln in f:
            line = ln.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            name = parts[0].split("{", 1)[0]
            try:
                metrics[name] = float(parts[-1])
            except ValueError:
                continue
    return metrics


def load_json(path):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _can_apply_self_starting_legacy_adaptation(
    *,
    args,
    identity_paths_with_open_cwd_dirfd_ambiguous: int,
    export_open_stat_fail_ratio: float,
    paths_with_open_stat_miss_ratio: float,
    paths_with_devino_ratio: float,
    ringbuf_drops: int,
) -> bool:
    if not bool(args.self_starting_service):
        return False
    startup_mode = str(args.startup_mode or "").strip()
    if startup_mode != "legacy":
        return False
    if identity_paths_with_open_cwd_dirfd_ambiguous != 0:
        return False
    if export_open_stat_fail_ratio > 0.12:
        return False
    if paths_with_open_stat_miss_ratio > 0.12:
        return False
    if paths_with_devino_ratio < 0.85:
        return False
    if ringbuf_drops != 0:
        return False
    return True


def scan_sampler_log(path):
    stats = {
        "redis_dropped_events": 0,
        "redis_publish_failures": 0,
    }
    if not os.path.exists(path):
        return stats

    drop_pat = re.compile(r"redis queue dropped events=(\d+)")
    publish_fail_pat = re.compile(r"redis publish failures=(\d+)")
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for ln in f:
            drop_match = drop_pat.search(ln)
            if drop_match:
                stats["redis_dropped_events"] = max(
                    stats["redis_dropped_events"], int(drop_match.group(1))
                )
            publish_match = publish_fail_pat.search(ln)
            if publish_match:
                stats["redis_publish_failures"] = max(
                    stats["redis_publish_failures"], int(publish_match.group(1))
                )
    return stats


def build_result(args):
    raw_counts = count_raw(args.raw)
    event_counts = count_events(args.events)
    events_total = event_counts["total"]
    enriched = count_enriched(args.enriched)
    metrics = load_metrics(args.metrics)
    log_stats = scan_sampler_log(args.sampler_log)
    export_stats = load_json(args.export_stats)
    aggregate_stats = load_json(args.aggregate_stats)

    mmap_n = enriched["mmap"]
    via_maps = enriched["mmap_via_maps"]
    resolved_mmap = enriched["mmap_via_mapfiles"] + enriched["mmap_via_dict"]
    maps_ratio = (float(via_maps) / float(mmap_n)) if mmap_n > 0 else 0.0
    resolved_ratio = (float(resolved_mmap) / float(mmap_n)) if mmap_n > 0 else 1.0
    raw_valid = raw_counts["raw_valid_json"]
    raw_mmap = raw_counts["raw_mmap"]
    sampler_events = int(metrics.get("file_events_total", 0))
    ringbuf_drops = int(metrics.get("ringbuf_drops_total", 0))
    filtered_out = int(metrics.get("filtered_out_total", 0))
    sampling_drops = int(metrics.get("sampling_drops_total", 0))
    file_sink_lines = int(metrics.get("file_sink_lines_total", 0))
    file_sink_bytes = int(metrics.get("file_sink_bytes_total", 0))
    file_sink_failures = int(metrics.get("file_sink_write_failures_total", 0))
    redis_queue_dropped = max(
        int(metrics.get("redis_queue_dropped_total", 0)),
        int(log_stats["redis_dropped_events"]),
    )
    redis_publish_failures = max(
        int(metrics.get("redis_publish_failures_total", 0)),
        int(log_stats["redis_publish_failures"]),
    )

    raw_vs_sampler_ratio = (
        float(raw_valid) / float(sampler_events) if sampler_events > 0 else 0.0
    )
    events_vs_raw_ratio = (
        float(events_total) / float(raw_valid) if raw_valid > 0 else 0.0
    )
    raw_mmap_zero_ratio = (
        float(raw_counts["raw_mmap_zero_devino"]) / float(raw_mmap) if raw_mmap > 0 else 0.0
    )
    ringbuf_drop_ratio = (
        float(ringbuf_drops) / float(ringbuf_drops + sampler_events)
        if (ringbuf_drops + sampler_events) > 0
        else 0.0
    )
    open_devino_ratio = (
        float(event_counts["open_with_devino"]) / float(event_counts["open"])
        if event_counts["open"] > 0
        else 0.0
    )
    raw_vs_file_sink_ratio = (
        float(raw_valid) / float(file_sink_lines) if file_sink_lines > 0 else 0.0
    )
    file_sink_metrics_present = any(
        key in metrics
        for key in (
            "file_sink_lines_total",
            "file_sink_bytes_total",
            "file_sink_write_failures_total",
        )
    )

    identity_stats_present = aggregate_stats is not None
    identity_unique_paths = int((aggregate_stats or {}).get("unique_paths") or 0)
    identity_paths_with_devino = int((aggregate_stats or {}).get("paths_with_devino") or 0)
    identity_paths_without_devino = max(identity_unique_paths - identity_paths_with_devino, 0)
    identity_candidates = int((aggregate_stats or {}).get("identity_candidates") or 0)
    devino_identity_candidates = int((aggregate_stats or {}).get("devino_identity_candidates") or 0)
    identity_paths_with_open_resolved_from_cwd = int(
        (aggregate_stats or {}).get("paths_with_open_resolved_from_cwd") or 0
    )
    identity_paths_with_open_cwd_dirfd_ambiguous = int(
        (aggregate_stats or {}).get("paths_with_open_cwd_dirfd_ambiguous") or 0
    )
    if (
        identity_paths_with_open_cwd_dirfd_ambiguous == 0
        and "paths_with_open_cwd_dirfd_ambiguous" not in (aggregate_stats or {})
    ):
        identity_paths_with_open_cwd_dirfd_ambiguous = identity_paths_with_open_resolved_from_cwd
    identity_paths_with_open_stat_devino = int((aggregate_stats or {}).get("paths_with_open_stat_devino") or 0)
    identity_paths_with_open_raw_devino = int((aggregate_stats or {}).get("paths_with_open_raw_devino") or 0)
    identity_paths_with_open_stat_miss = int((aggregate_stats or {}).get("paths_with_open_stat_miss") or 0)
    identity_paths_with_mmap_raw_devino = int((aggregate_stats or {}).get("paths_with_mmap_raw_devino") or 0)
    identity_candidates_with_open_cwd_dirfd_ambiguous = int(
        (aggregate_stats or {}).get("identity_candidates_with_open_cwd_dirfd_ambiguous") or 0
    )
    if (
        identity_candidates_with_open_cwd_dirfd_ambiguous == 0
        and "identity_candidates_with_open_cwd_dirfd_ambiguous" not in (aggregate_stats or {})
    ):
        identity_candidates_with_open_cwd_dirfd_ambiguous = int(
            (aggregate_stats or {}).get("identity_candidates_with_open_resolved_from_cwd") or 0
        )
    identity_candidates_with_open_stat_devino = int(
        (aggregate_stats or {}).get("identity_candidates_with_open_stat_devino") or 0
    )
    identity_candidates_with_open_stat_miss = int(
        (aggregate_stats or {}).get("identity_candidates_with_open_stat_miss") or 0
    )
    identity_candidates_with_mmap_raw_devino = int(
        (aggregate_stats or {}).get("identity_candidates_with_mmap_raw_devino") or 0
    )
    identity_alias_groups = int((aggregate_stats or {}).get("identity_alias_groups") or 0)
    identity_merged_paths = int((aggregate_stats or {}).get("identity_merged_paths") or 0)
    paths_with_devino_ratio = (
        float(identity_paths_with_devino) / float(identity_unique_paths)
        if identity_unique_paths > 0
        else 0.0
    )
    paths_with_open_stat_devino_ratio = (
        float(identity_paths_with_open_stat_devino) / float(identity_unique_paths)
        if identity_unique_paths > 0
        else 0.0
    )
    paths_with_open_stat_miss_ratio = (
        float(identity_paths_with_open_stat_miss) / float(identity_unique_paths)
        if identity_unique_paths > 0
        else 0.0
    )
    paths_with_open_resolved_from_cwd_ratio = (
        float(identity_paths_with_open_resolved_from_cwd) / float(identity_unique_paths)
        if identity_unique_paths > 0
        else 0.0
    )
    paths_with_open_cwd_dirfd_ambiguous_ratio = (
        float(identity_paths_with_open_cwd_dirfd_ambiguous) / float(identity_unique_paths)
        if identity_unique_paths > 0
        else 0.0
    )
    paths_with_mmap_raw_devino_ratio = (
        float(identity_paths_with_mmap_raw_devino) / float(identity_unique_paths)
        if identity_unique_paths > 0
        else 0.0
    )
    identity_merged_paths_ratio = (
        float(identity_merged_paths) / float(identity_unique_paths)
        if identity_unique_paths > 0
        else 0.0
    )
    identity_alias_group_ratio = (
        float(identity_alias_groups) / float(identity_candidates)
        if identity_candidates > 0
        else 0.0
    )
    export_stats_present = export_stats is not None
    export_relative_open_total = int(
        (export_stats or {}).get("relative_open_total") or raw_counts["raw_open_relative_path"]
    )
    export_relative_open_resolved_total = int(
        (export_stats or {}).get("relative_open_resolved_total") or event_counts["open_resolved_from_cwd"]
    )
    export_relative_open_resolved_legacy_missing_dirfd = int(
        (export_stats or {}).get("relative_open_resolved_legacy_missing_dirfd")
        or event_counts["open_cwd_dirfd_ambiguous"]
    )
    export_relative_open_resolved_at_fdcwd = int(
        (export_stats or {}).get("relative_open_resolved_at_fdcwd") or 0
    )
    export_relative_open_explicit_dirfd_skipped = int(
        (export_stats or {}).get("relative_open_explicit_dirfd_skipped")
        or raw_counts["raw_open_relative_explicit_dirfd"]
    )
    export_relative_open_unresolved_no_cwd = int(
        (export_stats or {}).get("relative_open_unresolved_no_cwd") or 0
    )
    export_open_stat_fail = int((export_stats or {}).get("open_stat_fail") or 0)
    export_open_stat_fail_not_found = int((export_stats or {}).get("open_stat_fail_not_found") or 0)
    export_open_stat_fail_permission = int((export_stats or {}).get("open_stat_fail_permission") or 0)
    export_open_stat_fail_other = int((export_stats or {}).get("open_stat_fail_other") or 0)
    export_open_stat_fail_cwd_resolved = int((export_stats or {}).get("open_stat_fail_cwd_resolved") or 0)
    export_relative_open_explicit_dirfd_skip_ratio = (
        float(export_relative_open_explicit_dirfd_skipped) / float(export_relative_open_total)
        if export_relative_open_total > 0
        else 0.0
    )
    export_open_stat_fail_ratio = (
        float(export_open_stat_fail) / float(event_counts["open"]) if event_counts["open"] > 0 else 0.0
    )

    ok = True
    reasons = []
    warnings = []
    adaptations_applied = []
    startup_backend = str(args.startup_backend or "unknown")
    maps_ratio_reason = None
    backfill_only_reason = None

    if raw_valid == 0:
        ok = False
        reasons.append("raw_events valid JSON count is 0")
    if events_total == 0:
        ok = False
        reasons.append("filtered events.jsonl count is 0")
    if enriched["total"] < args.min_events:
        ok = False
        reasons.append(
            f"events_enriched total={enriched['total']} < min_events={args.min_events}"
        )
    if mmap_n > 0 and maps_ratio > args.max_mmap_maps_ratio:
        maps_ratio_reason = f"mmap via=maps ratio={maps_ratio:.4f} > max={args.max_mmap_maps_ratio:.4f}"
        ok = False
        reasons.append(maps_ratio_reason)
    if ringbuf_drops > 0:
        ok = False
        reasons.append(f"ringbuf_drops_total={ringbuf_drops} > 0")
    if args.event_sink_used == "redis_sink" and redis_queue_dropped > 0:
        ok = False
        reasons.append(f"redis queue dropped events={redis_queue_dropped} > 0")
    if args.event_sink_used == "redis_sink" and redis_publish_failures > 0:
        ok = False
        reasons.append(f"redis publish failures={redis_publish_failures} > 0")
    if sampler_events > 0 and raw_vs_sampler_ratio < 0.80:
        ok = False
        reasons.append(f"raw_events/sampler_events ratio={raw_vs_sampler_ratio:.4f} < 0.80")
    if file_sink_failures > 0:
        ok = False
        reasons.append(f"file_sink_write_failures_total={file_sink_failures} > 0")
    if args.event_sink_used == "file_sink" and file_sink_metrics_present and file_sink_lines == 0:
        ok = False
        reasons.append("file sink metrics report 0 written lines")
    if args.event_sink_used == "file_sink" and file_sink_lines > 0 and raw_vs_file_sink_ratio < 0.80:
        ok = False
        reasons.append(
            f"raw_events/file_sink_lines ratio={raw_vs_file_sink_ratio:.4f} < 0.80"
        )

    if not metrics:
        warnings.append("sampler metrics snapshot missing")
    if sampler_events == 0 and metrics:
        warnings.append("sampler metrics snapshot reports 0 file_events_total")
    if sampler_events > 0 and raw_vs_sampler_ratio < 0.95 and raw_vs_sampler_ratio >= 0.80:
        warnings.append(f"raw_events/sampler_events ratio is low: {raw_vs_sampler_ratio:.4f}")
    if raw_mmap > 0 and raw_mmap_zero_ratio > 0.50:
        warnings.append(f"raw mmap zero dev:ino ratio is high: {raw_mmap_zero_ratio:.4f}")
    if (
        raw_counts["raw_open_relative_path"] > 0
        and event_counts["open_resolved_from_cwd"] == 0
        and raw_counts["raw_open_relative_explicit_dirfd"] < raw_counts["raw_open_relative_path"]
    ):
        warnings.append(
            "raw OPEN events contained relative paths, but none were resolved via cwd snapshot"
        )
    if raw_counts["raw_open_relative_explicit_dirfd"] > 0:
        warnings.append(
            "raw OPEN events contained relative paths with explicit non-AT_FDCWD dirfd; cwd reconstruction was intentionally skipped: "
            f"{raw_counts['raw_open_relative_explicit_dirfd']}"
        )
    if event_counts["open"] > 0 and open_devino_ratio < 0.80:
        warnings.append(f"OPEN events with dev:ino ratio is low: {open_devino_ratio:.4f}")
    if mmap_n > 0 and resolved_ratio < 0.70:
        warnings.append(f"resolved mmap ratio is low: {resolved_ratio:.4f}")
    if raw_valid > 0 and events_vs_raw_ratio < 0.30:
        warnings.append(f"events/raw ratio is low: {events_vs_raw_ratio:.4f}")
    if event_counts["open_resolved_from_cwd"] > 0:
        warnings.append(
            f"resolved relative OPEN paths via cwd snapshot: {event_counts['open_resolved_from_cwd']}"
        )
    if event_counts["open_cwd_dirfd_ambiguous"] > 0:
        warnings.append(
            "resolved relative OPEN paths are dirfd-ambiguous because openat/openat2 dirfd is not captured: "
            f"{event_counts['open_cwd_dirfd_ambiguous']}"
        )
    if export_open_stat_fail > 0:
        warnings.append(
            "OPEN stat backfill could not resolve some emitted OPEN paths: "
            f"fail={export_open_stat_fail} not_found={export_open_stat_fail_not_found} "
            f"permission={export_open_stat_fail_permission} other={export_open_stat_fail_other}"
        )
    if export_open_stat_fail_cwd_resolved > 0:
        warnings.append(
            "OPEN stat backfill misses include cwd-resolved relative paths: "
            f"{export_open_stat_fail_cwd_resolved}"
        )
    if filtered_out > 0:
        warnings.append(f"filtered_out_total={filtered_out}")
    if sampling_drops > 0:
        warnings.append(f"sampling_drops_total={sampling_drops}")
    if args.startup_mode != "gated":
        warnings.append("startup mode is legacy; earliest cold-start accesses may be missed")
    elif startup_backend == "gated_host_launcher_dynamic":
        warnings.append(
            "startup backend is experimental host launcher (dynamic); container runtime libc compatibility is required"
        )
    elif startup_backend == "unknown":
        warnings.append("startup backend is unknown; startup fidelity cannot be audited")
    if args.event_sink_used != "file_sink":
        warnings.append(
            f"event sink used={args.event_sink_used or 'none'}; primary file sink did not provide usable raw events"
        )
    if args.event_sink_used == "file_sink" and not file_sink_metrics_present:
        warnings.append("file sink metrics are missing from sampler metrics snapshot")
    if args.event_sink_used == "file_sink" and file_sink_lines > 0 and raw_vs_file_sink_ratio < 0.95 and raw_vs_file_sink_ratio >= 0.80:
        warnings.append(f"raw_events/file_sink_lines ratio is low: {raw_vs_file_sink_ratio:.4f}")
    if args.event_sink_used == "file_sink" and file_sink_lines > 0 and file_sink_bytes == 0:
        warnings.append("file sink metrics report 0 written bytes despite non-zero line count")
    if args.event_sink_used == "file_sink" and redis_queue_dropped > 0:
        warnings.append(
            f"redis queue dropped events={redis_queue_dropped}, but local file sink remained primary"
        )
    if args.event_sink_used == "file_sink" and redis_publish_failures > 0:
        warnings.append(
            f"redis publish failures={redis_publish_failures}, but local file sink remained primary"
        )
    if args.aggregate_stats and not identity_stats_present:
        warnings.append("aggregate stats are missing or unreadable")
    if identity_stats_present and identity_unique_paths >= 10 and identity_paths_with_devino == 0:
        warnings.append("aggregate stats show 0 paths with dev:ino; identity merging is effectively disabled")
    elif identity_stats_present and identity_unique_paths >= 10 and paths_with_devino_ratio < 0.30:
        warnings.append(f"aggregate paths with dev:ino ratio is low: {paths_with_devino_ratio:.4f}")
    if (
        identity_stats_present
        and identity_unique_paths >= 20
        and identity_alias_groups == 0
        and paths_with_devino_ratio < 0.30
    ):
        warnings.append("identity alias groups are absent under low dev:ino coverage; selector may stay path-fragmented")
    if (
        identity_stats_present
        and "paths_with_mmap_raw_devino" in (aggregate_stats or {})
        and mmap_n > 0
        and identity_paths_with_devino > 0
        and identity_paths_with_open_stat_devino > 0
        and identity_paths_with_mmap_raw_devino == 0
    ):
        warnings.append("identity dev:ino coverage comes entirely from OPEN stat backfill; mmap raw dev:ino evidence is absent")
    if (
        identity_stats_present
        and identity_unique_paths >= 10
        and paths_with_open_cwd_dirfd_ambiguous_ratio >= 0.30
    ):
        warnings.append(
            f"aggregate dirfd-ambiguous OPEN path ratio is high: {paths_with_open_cwd_dirfd_ambiguous_ratio:.4f}"
        )
    if (
        identity_stats_present
        and identity_unique_paths >= 10
        and paths_with_open_stat_miss_ratio >= 0.20
    ):
        warnings.append(
            f"aggregate OPEN stat-miss path ratio is elevated: {paths_with_open_stat_miss_ratio:.4f}"
        )
    if (
        identity_stats_present
        and identity_unique_paths >= 20
        and mmap_n >= 5
        and identity_paths_with_devino >= 5
        and identity_paths_with_open_stat_devino == identity_paths_with_devino
        and identity_paths_with_mmap_raw_devino == 0
    ):
        backfill_only_reason = (
            "identity dev:ino coverage relies entirely on OPEN stat backfill on a mmap-bearing non-trivial sample"
        )
        ok = False
        reasons.append(backfill_only_reason)
    if (
        identity_stats_present
        and identity_unique_paths >= 20
        and paths_with_open_cwd_dirfd_ambiguous_ratio >= 0.50
        and identity_paths_with_mmap_raw_devino == 0
    ):
        ok = False
        reasons.append(
            "high dirfd-ambiguous OPEN share with no mmap raw identity evidence on a non-trivial sample"
        )
    if (
        identity_stats_present
        and identity_unique_paths >= 20
        and paths_with_open_stat_miss_ratio >= 0.40
        and identity_paths_with_mmap_raw_devino == 0
    ):
        ok = False
        reasons.append(
            "high OPEN stat-miss share with no mmap raw identity evidence on a non-trivial sample"
        )

    if (
        maps_ratio_reason is not None
        and backfill_only_reason is not None
        and set(reasons) == {maps_ratio_reason, backfill_only_reason}
        and _can_apply_self_starting_legacy_adaptation(
            args=args,
            identity_paths_with_open_cwd_dirfd_ambiguous=identity_paths_with_open_cwd_dirfd_ambiguous,
            export_open_stat_fail_ratio=export_open_stat_fail_ratio,
            paths_with_open_stat_miss_ratio=paths_with_open_stat_miss_ratio,
            paths_with_devino_ratio=paths_with_devino_ratio,
            ringbuf_drops=ringbuf_drops,
        )
    ):
        ok = True
        reasons = []
        demoted_reasons = [maps_ratio_reason, backfill_only_reason]
        warnings.extend(
            [
                "quality gate adaptation applied for self-starting legacy workload; mmap maps-only reconstruction was accepted because OPEN stat backfill evidence is clean",
                *demoted_reasons,
            ]
        )
        adaptations_applied.append(
            {
                "id": "self_starting_legacy_maps_backfill_only",
                "self_starting_service": True,
                "startup_mode": str(args.startup_mode or ""),
                "startup_backend": startup_backend,
                "demoted_reasons": demoted_reasons,
            }
        )

    return {
        "quality_version": 7,
        "run_id": args.run_id,
        "self_starting_service": bool(args.self_starting_service),
        "startup_mode": args.startup_mode,
        "startup_backend": startup_backend,
        "startup": {
            "mode": args.startup_mode,
            "backend": startup_backend,
        },
        "adaptations_applied": adaptations_applied,
        "event_sink": {
            "used": args.event_sink_used,
            "raw_path": args.raw,
            "redis_enabled": bool(args.redis_enabled),
            "file_sink_metrics_present": file_sink_metrics_present,
        },
        "export": {
            "stats_present": export_stats_present,
            "relative_open_total": export_relative_open_total,
            "relative_open_resolved_total": export_relative_open_resolved_total,
            "relative_open_resolved_legacy_missing_dirfd": export_relative_open_resolved_legacy_missing_dirfd,
            "relative_open_resolved_at_fdcwd": export_relative_open_resolved_at_fdcwd,
            "relative_open_explicit_dirfd_skipped": export_relative_open_explicit_dirfd_skipped,
            "relative_open_unresolved_no_cwd": export_relative_open_unresolved_no_cwd,
            "open_stat_fail": export_open_stat_fail,
            "open_stat_fail_not_found": export_open_stat_fail_not_found,
            "open_stat_fail_permission": export_open_stat_fail_permission,
            "open_stat_fail_other": export_open_stat_fail_other,
            "open_stat_fail_cwd_resolved": export_open_stat_fail_cwd_resolved,
        },
        "identity": {
            "stats_present": identity_stats_present,
            "unique_paths": identity_unique_paths,
            "paths_with_devino": identity_paths_with_devino,
            "paths_without_devino": identity_paths_without_devino,
            "paths_with_open_resolved_from_cwd": identity_paths_with_open_resolved_from_cwd,
            "paths_with_open_cwd_dirfd_ambiguous": identity_paths_with_open_cwd_dirfd_ambiguous,
            "paths_with_open_stat_devino": identity_paths_with_open_stat_devino,
            "paths_with_open_raw_devino": identity_paths_with_open_raw_devino,
            "paths_with_open_stat_miss": identity_paths_with_open_stat_miss,
            "paths_with_mmap_raw_devino": identity_paths_with_mmap_raw_devino,
            "identity_candidates": identity_candidates,
            "devino_identity_candidates": devino_identity_candidates,
            "identity_candidates_with_open_cwd_dirfd_ambiguous": identity_candidates_with_open_cwd_dirfd_ambiguous,
            "identity_candidates_with_open_stat_devino": identity_candidates_with_open_stat_devino,
            "identity_candidates_with_open_stat_miss": identity_candidates_with_open_stat_miss,
            "identity_candidates_with_mmap_raw_devino": identity_candidates_with_mmap_raw_devino,
            "identity_alias_groups": identity_alias_groups,
            "identity_merged_paths": identity_merged_paths,
        },
        "ok": ok,
        "min_events": args.min_events,
        "max_mmap_maps_ratio": args.max_mmap_maps_ratio,
        "counts": {
            **raw_counts,
            "events_filtered_total": events_total,
            "events_open_total": event_counts["open"],
            "events_open_with_devino": event_counts["open_with_devino"],
            "events_open_resolved_from_cwd": event_counts["open_resolved_from_cwd"],
            "events_open_cwd_dirfd_ambiguous": event_counts["open_cwd_dirfd_ambiguous"],
            "events_open_stat_miss": event_counts["open_stat_miss"],
            "events_open_unresolved_relative_kept": event_counts["open_unresolved_relative_kept"],
            "export_relative_open_total": export_relative_open_total,
            "export_relative_open_resolved_total": export_relative_open_resolved_total,
            "export_relative_open_resolved_legacy_missing_dirfd": export_relative_open_resolved_legacy_missing_dirfd,
            "export_relative_open_resolved_at_fdcwd": export_relative_open_resolved_at_fdcwd,
            "export_relative_open_explicit_dirfd_skipped": export_relative_open_explicit_dirfd_skipped,
            "export_relative_open_unresolved_no_cwd": export_relative_open_unresolved_no_cwd,
            "export_open_stat_fail": export_open_stat_fail,
            "export_open_stat_fail_not_found": export_open_stat_fail_not_found,
            "export_open_stat_fail_permission": export_open_stat_fail_permission,
            "export_open_stat_fail_other": export_open_stat_fail_other,
            "export_open_stat_fail_cwd_resolved": export_open_stat_fail_cwd_resolved,
            **enriched,
            "sampler_file_events_total": sampler_events,
            "sampler_ringbuf_drops_total": ringbuf_drops,
            "sampler_filtered_out_total": filtered_out,
            "sampler_sampling_drops_total": sampling_drops,
            "file_sink_lines_total": file_sink_lines,
            "file_sink_bytes_total": file_sink_bytes,
            "file_sink_write_failures_total": file_sink_failures,
            "redis_queue_dropped_total": redis_queue_dropped,
            "redis_publish_failures_total": redis_publish_failures,
            "identity_unique_paths": identity_unique_paths,
            "identity_paths_with_devino": identity_paths_with_devino,
            "identity_paths_without_devino": identity_paths_without_devino,
            "identity_paths_with_open_resolved_from_cwd": identity_paths_with_open_resolved_from_cwd,
            "identity_paths_with_open_cwd_dirfd_ambiguous": identity_paths_with_open_cwd_dirfd_ambiguous,
            "identity_paths_with_open_stat_devino": identity_paths_with_open_stat_devino,
            "identity_paths_with_open_raw_devino": identity_paths_with_open_raw_devino,
            "identity_paths_with_open_stat_miss": identity_paths_with_open_stat_miss,
            "identity_paths_with_mmap_raw_devino": identity_paths_with_mmap_raw_devino,
            "identity_candidates": identity_candidates,
            "devino_identity_candidates": devino_identity_candidates,
            "identity_candidates_with_open_cwd_dirfd_ambiguous": identity_candidates_with_open_cwd_dirfd_ambiguous,
            "identity_candidates_with_open_stat_devino": identity_candidates_with_open_stat_devino,
            "identity_candidates_with_open_stat_miss": identity_candidates_with_open_stat_miss,
            "identity_candidates_with_mmap_raw_devino": identity_candidates_with_mmap_raw_devino,
            "identity_alias_groups": identity_alias_groups,
            "identity_merged_paths": identity_merged_paths,
        },
        "ratios": {
            "mmap_via_maps_ratio": maps_ratio,
            "mmap_resolved_ratio": resolved_ratio,
            "raw_events_vs_sampler_events_ratio": raw_vs_sampler_ratio,
            "events_filtered_vs_raw_events_ratio": events_vs_raw_ratio,
            "open_with_devino_ratio": open_devino_ratio,
            "raw_mmap_zero_devino_ratio": raw_mmap_zero_ratio,
            "ringbuf_drop_ratio": ringbuf_drop_ratio,
            "raw_events_vs_file_sink_lines_ratio": raw_vs_file_sink_ratio,
            "export_relative_open_explicit_dirfd_skip_ratio": export_relative_open_explicit_dirfd_skip_ratio,
            "export_open_stat_fail_ratio": export_open_stat_fail_ratio,
            "aggregate_paths_with_devino_ratio": paths_with_devino_ratio,
            "aggregate_paths_with_open_resolved_from_cwd_ratio": paths_with_open_resolved_from_cwd_ratio,
            "aggregate_paths_with_open_cwd_dirfd_ambiguous_ratio": paths_with_open_cwd_dirfd_ambiguous_ratio,
            "aggregate_paths_with_open_stat_devino_ratio": paths_with_open_stat_devino_ratio,
            "aggregate_paths_with_open_stat_miss_ratio": paths_with_open_stat_miss_ratio,
            "aggregate_paths_with_mmap_raw_devino_ratio": paths_with_mmap_raw_devino_ratio,
            "identity_merged_paths_vs_unique_paths_ratio": identity_merged_paths_ratio,
            "identity_alias_groups_vs_candidates_ratio": identity_alias_group_ratio,
        },
        "warnings": warnings,
        "reasons": reasons,
    }


def parse_args():
    parser = argparse.ArgumentParser(description="Evaluate sampling quality and persist a JSON report.")
    parser.add_argument("--raw", required=True)
    parser.add_argument("--events", required=True)
    parser.add_argument("--enriched", required=True)
    parser.add_argument("--metrics", required=True)
    parser.add_argument("--sampler-log", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--min-events", type=int, required=True)
    parser.add_argument("--max-mmap-maps-ratio", type=float, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--startup-mode", required=True)
    parser.add_argument("--startup-backend", default="unknown")
    parser.add_argument("--self-starting-service", type=int, choices=(0, 1), default=0)
    parser.add_argument("--event-sink-used", required=True)
    parser.add_argument("--redis-enabled", type=int, choices=(0, 1), required=True)
    parser.add_argument("--export-stats")
    parser.add_argument("--aggregate-stats")
    return parser.parse_args()


def main():
    args = parse_args()
    result = build_result(args)
    with open(args.out, "w", encoding="utf-8") as fo:
        json.dump(result, fo, ensure_ascii=False, indent=2)

    print(
        "[quality] "
        f"ok={result['ok']} raw={result['counts']['raw_valid_json']} "
        f"filtered={result['counts']['events_filtered_total']} "
        f"enriched={result['counts']['total']} "
        f"mmap={result['counts']['mmap']} "
        f"maps_ratio={result['ratios']['mmap_via_maps_ratio']:.4f} "
        f"paths_with_devino={result['counts']['identity_paths_with_devino']} "
        f"alias_groups={result['counts']['identity_alias_groups']} "
        f"ringbuf_drops={result['counts']['sampler_ringbuf_drops_total']} "
        f"file_sink_failures={result['counts']['file_sink_write_failures_total']} "
        f"redis_dropped={result['counts']['redis_queue_dropped_total']}"
    )
    if not result["ok"]:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
