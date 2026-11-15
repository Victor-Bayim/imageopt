#!/usr/bin/env python3
# optimizer/aggregate_candidates.py (identity-aware)

import argparse
import json
import sys

DROP_PREFIXES = ("/proc/", "/sys/", "/dev/", "/run/", "/var/run/")
PREVIEW_LIMIT = 12


def good_path(path):
    return bool(path) and path.startswith("/") and not any(path.startswith(prefix) for prefix in DROP_PREFIXES)


def normalize_path_kind(value):
    raw = str(value or "").strip().lower()
    if raw in ("regular", "directory", "other"):
        return raw
    return ""


def is_regular_path_kind(path_kind):
    return not path_kind or path_kind == "regular"


def iter_jsonl(path):
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                yield json.loads(ln)
            except Exception:
                try:
                    yield json.loads(json.loads(ln))
                except Exception:
                    continue


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("src", help="events_enriched.jsonl")
    p.add_argument("out", help="candidates.json")
    p.add_argument("--stats-json", help="optional aggregate stats json")
    return p.parse_args()


def finalize_candidate(st, min_ts, max_ts, total_events):
    if total_events <= 1:
        denom = 1.0
    else:
        denom = float(total_events - 1)

    st["first_ratio_idx"] = float(st["first_index"]) / denom
    if st["first_ts_ns"] > 0 and min_ts is not None and max_ts is not None and max_ts > min_ts:
        st["first_ratio_ts"] = float(st["first_ts_ns"] - min_ts) / float(max_ts - min_ts)
        st["first_ratio"] = st["first_ratio_ts"]
    else:
        st["first_ratio_ts"] = None
        st["first_ratio"] = st["first_ratio_idx"]

    weighted_count = float(st.get("weighted_count") or st.get("count") or 0.0)
    if weighted_count > 0:
        st["maps_only"] = bool(st["via_maps_count"] > 0 and st["count"] == st["via_maps_count"])
        st["open_devino_ratio"] = float(st["weighted_open_with_devino_count"]) / weighted_count
        st["maps_ratio"] = float(st["weighted_via_maps_count"]) / weighted_count
        st["mapfiles_ratio"] = float(st["weighted_via_mapfiles_count"]) / weighted_count
        st["dict_ratio"] = float(st["weighted_via_dict_count"]) / weighted_count
        st["open_ratio"] = float(st["weighted_via_open_count"]) / weighted_count
    else:
        st["maps_only"] = False
        st["open_devino_ratio"] = 0.0
        st["maps_ratio"] = 0.0
        st["mapfiles_ratio"] = 0.0
        st["dict_ratio"] = 0.0
        st["open_ratio"] = 0.0

    st["confidence_score"] = (
        1.3 * float(st["weighted_via_open_count"])
        + 1.0 * float(st["weighted_via_mapfiles_count"])
        + 0.7 * float(st["weighted_via_dict_count"])
        + 0.2 * float(st["weighted_via_maps_count"])
        + 0.4 * float(st["weighted_open_with_devino_count"])
    )

    aliases = st.get("aliases") or [st["path"]]
    seen = set()
    uniq = []
    for path in aliases:
        if good_path(path) and path not in seen:
            uniq.append(path)
            seen.add(path)
    if st["path"] in seen:
        uniq = [st["path"]] + [path for path in uniq if path != st["path"]]
    else:
        uniq.insert(0, st["path"])
    st["aliases"] = uniq
    st["alias_count"] = len(uniq)
    return st


def candidate_identity(st):
    dev = int(st.get("dev") or 0)
    ino = int(st.get("ino") or 0)
    if dev > 0 and ino > 0:
        return f"devino:{dev}:{ino}", "devino"
    return f"path:{st['path']}", "path"


def representative_sort_key(st):
    return (
        0 if st["via_mapfiles_count"] > 0 else 1,
        0 if st["via_dict_count"] > 0 else 1,
        0 if st["mmap_count"] > 0 else 1,
        0 if st["open_with_devino_count"] > 0 else 1,
        0 if st["open_count"] > 0 else 1,
        st["first_index"],
        -float(st["weighted_count"] or st["count"] or 0.0),
        len(st["path"]),
        st["path"],
    )


def merge_stats(items):
    rep = min(items, key=representative_sort_key)
    aliases = [rep["path"]]
    aliases.extend(sorted({item["path"] for item in items if item["path"] != rep["path"]}))
    first_ts_candidates = [int(item.get("first_ts_ns") or 0) for item in items if int(item.get("first_ts_ns") or 0) > 0]
    merged = {
        "path": rep["path"],
        "count": sum(int(item.get("count") or 0) for item in items),
        "weighted_count": sum(float(item.get("weighted_count") or 0.0) for item in items),
        "open_count": sum(int(item.get("open_count") or 0) for item in items),
        "weighted_open_count": sum(float(item.get("weighted_open_count") or 0.0) for item in items),
        "mmap_count": sum(int(item.get("mmap_count") or 0) for item in items),
        "weighted_mmap_count": sum(float(item.get("weighted_mmap_count") or 0.0) for item in items),
        "open_with_devino_count": sum(int(item.get("open_with_devino_count") or 0) for item in items),
        "weighted_open_with_devino_count": sum(float(item.get("weighted_open_with_devino_count") or 0.0) for item in items),
        "open_resolved_from_cwd_count": sum(int(item.get("open_resolved_from_cwd_count") or 0) for item in items),
        "weighted_open_resolved_from_cwd_count": sum(float(item.get("weighted_open_resolved_from_cwd_count") or 0.0) for item in items),
        "open_cwd_dirfd_ambiguous_count": sum(int(item.get("open_cwd_dirfd_ambiguous_count") or 0) for item in items),
        "weighted_open_cwd_dirfd_ambiguous_count": sum(float(item.get("weighted_open_cwd_dirfd_ambiguous_count") or 0.0) for item in items),
        "open_devino_stat_count": sum(int(item.get("open_devino_stat_count") or 0) for item in items),
        "weighted_open_devino_stat_count": sum(float(item.get("weighted_open_devino_stat_count") or 0.0) for item in items),
        "open_devino_raw_count": sum(int(item.get("open_devino_raw_count") or 0) for item in items),
        "weighted_open_devino_raw_count": sum(float(item.get("weighted_open_devino_raw_count") or 0.0) for item in items),
        "open_stat_miss_count": sum(int(item.get("open_stat_miss_count") or 0) for item in items),
        "weighted_open_stat_miss_count": sum(float(item.get("weighted_open_stat_miss_count") or 0.0) for item in items),
        "mmap_devino_raw_count": sum(int(item.get("mmap_devino_raw_count") or 0) for item in items),
        "weighted_mmap_devino_raw_count": sum(float(item.get("weighted_mmap_devino_raw_count") or 0.0) for item in items),
        "via_open_count": sum(int(item.get("via_open_count") or 0) for item in items),
        "via_mapfiles_count": sum(int(item.get("via_mapfiles_count") or 0) for item in items),
        "via_dict_count": sum(int(item.get("via_dict_count") or 0) for item in items),
        "via_maps_count": sum(int(item.get("via_maps_count") or 0) for item in items),
        "weighted_via_open_count": sum(float(item.get("weighted_via_open_count") or 0.0) for item in items),
        "weighted_via_mapfiles_count": sum(float(item.get("weighted_via_mapfiles_count") or 0.0) for item in items),
        "weighted_via_dict_count": sum(float(item.get("weighted_via_dict_count") or 0.0) for item in items),
        "weighted_via_maps_count": sum(float(item.get("weighted_via_maps_count") or 0.0) for item in items),
        "first_index": min(int(item.get("first_index") or 0) for item in items),
        "first_ts_ns": min(first_ts_candidates) if first_ts_candidates else 0,
        "size_bytes": max(int(item.get("size_bytes") or 0) for item in items),
        "dev": int(rep.get("dev") or 0),
        "ino": int(rep.get("ino") or 0),
        "first_raw_index": min(int(item.get("first_raw_index") or 0) for item in items if int(item.get("first_raw_index") or 0) > 0) if any(int(item.get("first_raw_index") or 0) > 0 for item in items) else 0,
        "identity": rep["identity"],
        "identity_source": rep["identity_source"],
        "aliases": aliases,
        "path_kind": str(rep.get("path_kind") or ""),
    }
    return merged


def build_path_stats(src):
    stats = {}
    total = 0
    min_ts = None
    max_ts = None
    filtered_non_regular_events = 0
    filtered_non_regular_paths = set()
    filtered_non_regular_kind_counts = {}
    filtered_non_regular_preview = []

    for idx, obj in enumerate(iter_jsonl(src)):
        total += 1
        path = str(obj.get("path", ""))
        if not good_path(path):
            continue
        path_kind = normalize_path_kind(obj.get("path_kind"))
        if not is_regular_path_kind(path_kind):
            filtered_non_regular_events += 1
            filtered_non_regular_paths.add(path)
            filtered_non_regular_kind_counts[path_kind] = filtered_non_regular_kind_counts.get(path_kind, 0) + 1
            if len(filtered_non_regular_preview) < PREVIEW_LIMIT:
                filtered_non_regular_preview.append(
                    {
                        "path": path,
                        "path_kind": path_kind,
                        "type": str(obj.get("type") or "").upper(),
                        "via": str(obj.get("via") or ""),
                    }
                )
            continue

        typ = str(obj.get("type") or "").upper()
        via = str(obj.get("via") or "")
        ts = int(obj.get("ts_ns") or 0)
        size = int(obj.get("size_bytes") or 0)
        sample_rate = int(obj.get("sample_rate") or 0)
        raw_index = int(obj.get("raw_index") or 0)
        dev = int(obj.get("dev") or 0)
        ino = int(obj.get("ino") or 0)
        path_resolution = str(obj.get("path_resolution") or "")
        path_resolution_risk = str(obj.get("path_resolution_risk") or "")
        devino_source = str(obj.get("devino_source") or "")
        weight = 1.0
        if 0 < sample_rate <= 100:
            weight = 100.0 / float(sample_rate)

        if ts > 0:
            if min_ts is None or ts < min_ts:
                min_ts = ts
            if max_ts is None or ts > max_ts:
                max_ts = ts

        st = stats.get(path)
        if not st:
            st = {
                "path": path,
                "count": 0,
                "weighted_count": 0.0,
                "open_count": 0,
                "weighted_open_count": 0.0,
                "mmap_count": 0,
                "weighted_mmap_count": 0.0,
                "open_with_devino_count": 0,
                "weighted_open_with_devino_count": 0.0,
                "open_resolved_from_cwd_count": 0,
                "weighted_open_resolved_from_cwd_count": 0.0,
                "open_cwd_dirfd_ambiguous_count": 0,
                "weighted_open_cwd_dirfd_ambiguous_count": 0.0,
                "open_devino_stat_count": 0,
                "weighted_open_devino_stat_count": 0.0,
                "open_devino_raw_count": 0,
                "weighted_open_devino_raw_count": 0.0,
                "open_stat_miss_count": 0,
                "weighted_open_stat_miss_count": 0.0,
                "mmap_devino_raw_count": 0,
                "weighted_mmap_devino_raw_count": 0.0,
                "via_open_count": 0,
                "via_mapfiles_count": 0,
                "via_dict_count": 0,
                "via_maps_count": 0,
                "weighted_via_open_count": 0.0,
                "weighted_via_mapfiles_count": 0.0,
                "weighted_via_dict_count": 0.0,
                "weighted_via_maps_count": 0.0,
                "first_index": idx,
                "first_ts_ns": ts if ts > 0 else 0,
                "size_bytes": size if size > 0 else 0,
                "dev": dev if dev > 0 else 0,
                "ino": ino if ino > 0 else 0,
                "first_raw_index": raw_index if raw_index > 0 else 0,
                "path_kind": path_kind or "regular",
            }
            stats[path] = st

        st["count"] += 1
        st["weighted_count"] += weight

        if typ == "OPEN":
            st["open_count"] += 1
            st["weighted_open_count"] += weight
            if dev > 0 and ino > 0:
                st["open_with_devino_count"] += 1
                st["weighted_open_with_devino_count"] += weight
                if devino_source == "stat":
                    st["open_devino_stat_count"] += 1
                    st["weighted_open_devino_stat_count"] += weight
                elif devino_source == "raw":
                    st["open_devino_raw_count"] += 1
                    st["weighted_open_devino_raw_count"] += weight
            if str(obj.get("stat_lookup") or "") == "miss":
                st["open_stat_miss_count"] += 1
                st["weighted_open_stat_miss_count"] += weight
            if path_resolution == "cwd":
                st["open_resolved_from_cwd_count"] += 1
                st["weighted_open_resolved_from_cwd_count"] += weight
            if path_resolution_risk == "dirfd_unavailable":
                st["open_cwd_dirfd_ambiguous_count"] += 1
                st["weighted_open_cwd_dirfd_ambiguous_count"] += weight
        elif typ == "MMAP":
            st["mmap_count"] += 1
            st["weighted_mmap_count"] += weight
            if dev > 0 and ino > 0 and devino_source == "mmap_raw":
                st["mmap_devino_raw_count"] += 1
                st["weighted_mmap_devino_raw_count"] += weight

        if via == "open":
            st["via_open_count"] += 1
            st["weighted_via_open_count"] += weight
        elif via == "mapfiles":
            st["via_mapfiles_count"] += 1
            st["weighted_via_mapfiles_count"] += weight
        elif via == "dict":
            st["via_dict_count"] += 1
            st["weighted_via_dict_count"] += weight
        elif via == "maps":
            st["via_maps_count"] += 1
            st["weighted_via_maps_count"] += weight

        if st["first_ts_ns"] == 0 and ts > 0:
            st["first_ts_ns"] = ts
        if size > 0 and (st["size_bytes"] == 0 or size > st["size_bytes"]):
            st["size_bytes"] = size
        if st["dev"] == 0 and dev > 0:
            st["dev"] = dev
        if st["ino"] == 0 and ino > 0:
            st["ino"] = ino
        if st["first_raw_index"] == 0 and raw_index > 0:
            st["first_raw_index"] = raw_index
        if idx < st["first_index"]:
            st["first_index"] = idx
        if path_kind and not st.get("path_kind"):
            st["path_kind"] = path_kind

    return list(stats.values()), total, min_ts, max_ts, {
        "filtered_non_regular_events": filtered_non_regular_events,
        "filtered_non_regular_paths": len(filtered_non_regular_paths),
        "filtered_non_regular_kind_counts": filtered_non_regular_kind_counts,
        "filtered_non_regular_preview": filtered_non_regular_preview,
    }


def merge_by_identity(path_stats):
    groups = {}
    for st in path_stats:
        identity, identity_source = candidate_identity(st)
        st = dict(st)
        st["identity"] = identity
        st["identity_source"] = identity_source
        groups.setdefault(identity, []).append(st)

    merged = []
    alias_groups = 0
    merged_paths = 0
    devino_identity_candidates = 0
    alias_examples = []
    for identity, items in groups.items():
        if items and str(items[0].get("identity_source") or "") == "devino":
            devino_identity_candidates += 1
        if len(items) == 1:
            item = dict(items[0])
            item["aliases"] = item.get("aliases") or [item["path"]]
            merged.append(item)
            continue
        alias_groups += 1
        merged_paths += len(items) - 1
        item = merge_stats(items)
        if len(alias_examples) < PREVIEW_LIMIT:
            alias_examples.append(
                {
                    "identity": identity,
                    "path": item["path"],
                    "aliases": item["aliases"],
                }
            )
        merged.append(item)

    return merged, {
        "unique_paths": len(path_stats),
        "paths_with_devino": sum(
            1 for st in path_stats if int(st.get("dev") or 0) > 0 and int(st.get("ino") or 0) > 0
        ),
        "paths_with_open_resolved_from_cwd": sum(
            1 for st in path_stats if int(st.get("open_resolved_from_cwd_count") or 0) > 0
        ),
        "paths_with_open_cwd_dirfd_ambiguous": sum(
            1 for st in path_stats if int(st.get("open_cwd_dirfd_ambiguous_count") or 0) > 0
        ),
        "paths_with_open_stat_devino": sum(
            1 for st in path_stats if int(st.get("open_devino_stat_count") or 0) > 0
        ),
        "paths_with_open_raw_devino": sum(
            1 for st in path_stats if int(st.get("open_devino_raw_count") or 0) > 0
        ),
        "paths_with_open_stat_miss": sum(
            1 for st in path_stats if int(st.get("open_stat_miss_count") or 0) > 0
        ),
        "paths_with_mmap_raw_devino": sum(
            1 for st in path_stats if int(st.get("mmap_devino_raw_count") or 0) > 0
        ),
        "open_events_with_stat_devino": sum(
            int(st.get("open_devino_stat_count") or 0) for st in path_stats
        ),
        "open_events_resolved_from_cwd": sum(
            int(st.get("open_resolved_from_cwd_count") or 0) for st in path_stats
        ),
        "open_events_cwd_dirfd_ambiguous": sum(
            int(st.get("open_cwd_dirfd_ambiguous_count") or 0) for st in path_stats
        ),
        "open_events_with_raw_devino": sum(
            int(st.get("open_devino_raw_count") or 0) for st in path_stats
        ),
        "open_events_with_stat_miss": sum(
            int(st.get("open_stat_miss_count") or 0) for st in path_stats
        ),
        "mmap_events_with_raw_devino": sum(
            int(st.get("mmap_devino_raw_count") or 0) for st in path_stats
        ),
        "identity_candidates": len(merged),
        "devino_identity_candidates": devino_identity_candidates,
        "identity_candidates_with_open_resolved_from_cwd": sum(
            1 for st in merged if int(st.get("open_resolved_from_cwd_count") or 0) > 0
        ),
        "identity_candidates_with_open_cwd_dirfd_ambiguous": sum(
            1 for st in merged if int(st.get("open_cwd_dirfd_ambiguous_count") or 0) > 0
        ),
        "identity_candidates_with_open_stat_devino": sum(
            1 for st in merged if int(st.get("open_devino_stat_count") or 0) > 0
        ),
        "identity_candidates_with_open_stat_miss": sum(
            1 for st in merged if int(st.get("open_stat_miss_count") or 0) > 0
        ),
        "identity_candidates_with_mmap_raw_devino": sum(
            1 for st in merged if int(st.get("mmap_devino_raw_count") or 0) > 0
        ),
        "identity_alias_groups": alias_groups,
        "identity_merged_paths": merged_paths,
        "identity_alias_examples": alias_examples,
    }


def main():
    args = parse_args()
    path_stats, total_events, min_ts, max_ts, filter_stats = build_path_stats(args.src)
    merged_stats, aggregate_stats = merge_by_identity(path_stats)

    arr = [finalize_candidate(st, min_ts, max_ts, total_events) for st in merged_stats]
    arr.sort(
        key=lambda x: (
            x["first_index"],
            -float(x.get("confidence_score") or 0.0),
            -float(x.get("weighted_count") or x.get("count") or 0.0),
        )
    )

    with open(args.out, "w", encoding="utf-8") as fo:
        json.dump(arr, fo, ensure_ascii=False, indent=2)

    if args.stats_json:
        stats_payload = {
            "events_total": total_events,
            **aggregate_stats,
            **filter_stats,
        }
        with open(args.stats_json, "w", encoding="utf-8") as fo:
            json.dump(stats_payload, fo, ensure_ascii=False, indent=2)

    print(
        f"[aggregate] events={total_events} unique_paths={aggregate_stats['unique_paths']} "
        f"identity_candidates={aggregate_stats['identity_candidates']} "
        f"alias_groups={aggregate_stats['identity_alias_groups']} "
        f"merged_paths={aggregate_stats['identity_merged_paths']} "
        f"filtered_non_regular_events={filter_stats['filtered_non_regular_events']} -> {args.out}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
