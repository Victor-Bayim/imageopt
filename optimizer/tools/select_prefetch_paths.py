#!/usr/bin/env python3
"""
选择预取路径（预算约束版）

输入 candidates.json（oneclick 聚合产物），根据访问强度、首次出现位置与文件大小，挑选在体积预算内的路径并输出：
  - ranked.yaml   ：按 P0/P1/P2/P3 分组
  - estargz.list  ：prefetch 列表（单列路径）
可选读取 LLM/人工指定的优先顺序（preferred 列表），仅在列表中选择并按其顺序排序，再应用预算/阈值。
同时支持“早期单次关键文件”通道：在预算允许下，补入少量 first_ratio 很早、体积较小、但仅命中 1 次的路径。
"""

import argparse
import json
import math
import sys
from pathlib import Path

MIN_SIZE_FALLBACK = 4096
PREVIEW_LIMIT = 12


def normalize_path_kind(value, is_regular_file=None):
    raw = str(value or "").strip().lower()
    if raw in ("regular", "directory", "other"):
        return raw
    if is_regular_file is not None:
        return "regular" if bool(is_regular_file) else "other"
    return ""


def is_regular_path_kind(path_kind):
    return not path_kind or path_kind == "regular"


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--candidates", required=True, help="candidates.json 路径")
    p.add_argument("--aggregate-stats", help="aggregate_stats.json 路径（可选，用于 identity guard）")
    p.add_argument("--out-yaml", required=True, help="输出 ranked.yaml")
    p.add_argument("--out-list", required=True, help="输出 estargz.list")
    p.add_argument("--stats-json", help="输出统计信息 JSON")
    p.add_argument("--budget-mb", type=float, default=0.0, help="体积预算（MB）")
    p.add_argument("--budget-percent", type=float, default=6.4, help="体积预算占候选总体积百分比")
    p.add_argument("--min-hits", type=int, default=2, help="最小访问次数阈值（open+MMAP）")
    p.add_argument("--p0-max", type=int, default=24, help="P0 上限")
    p.add_argument("--preferred", help="优先路径列表（每行一个绝对路径）")
    p.add_argument("--early-onehit-ratio", type=float, default=0.12, help="允许纳入单次命中的最晚 first_ratio")
    p.add_argument("--early-onehit-max", type=int, default=8, help="允许纳入的单次命中条目上限")
    p.add_argument("--early-onehit-max-size-kb", type=int, default=512, help="单次命中条目的最大体积（KB）")
    return p.parse_args()


def compute_score(
    first_ratio,
    count,
    open_count,
    mmap_count,
    open_with_devino_count,
    open_ratio,
    mapfiles_ratio,
    dict_ratio,
    maps_ratio,
    maps_only,
):
    base = 1.0 - min(max(first_ratio, 0.0), 1.0)
    freq = math.log2(1.0 + max(count, 0))
    mmap_bonus = 0.5 if mmap_count > 0 else 0.0
    open_bonus = 0.2 if open_count > 0 else 0.0
    mixed_bonus = 0.35 if open_count > 0 and mmap_count > 0 else 0.0
    devino_bonus = 0.30 * min(max(open_with_devino_count, 0.0) / max(count, 1.0), 1.0)
    source_bonus = 0.25 * open_ratio + 0.20 * mapfiles_ratio + 0.10 * dict_ratio
    maps_penalty = 0.55 * maps_ratio + (0.8 if maps_only else 0.0)
    return base + 0.7 * freq + mmap_bonus + open_bonus + mixed_bonus + devino_bonus + source_bonus - maps_penalty


def compute_confidence(via_open_count, via_mapfiles_count, via_dict_count, via_maps_count, open_with_devino_count):
    return (
        1.3 * float(via_open_count)
        + 1.0 * float(via_mapfiles_count)
        + 0.7 * float(via_dict_count)
        + 0.2 * float(via_maps_count)
        + 0.4 * float(open_with_devino_count)
    )


def finalize_candidate(item):
    size = int(item.get("size") or 0)
    if size <= 0:
        size = MIN_SIZE_FALLBACK
    item["size"] = size

    count = float(item.get("count") or 0.0)
    open_count = float(item.get("open_count") or 0.0)
    mmap_count = float(item.get("mmap_count") or 0.0)
    open_with_devino_count = float(item.get("open_with_devino_count") or 0.0)
    via_open_count = float(item.get("via_open_count") or 0.0)
    via_mapfiles_count = float(item.get("via_mapfiles_count") or 0.0)
    via_dict_count = float(item.get("via_dict_count") or 0.0)
    via_maps_count = float(item.get("via_maps_count") or 0.0)
    first_ratio = float(item.get("first_ratio") or 0.0)

    if count > 0:
        maps_only = bool(via_maps_count > 0 and count == via_maps_count)
        open_devino_ratio = float(open_with_devino_count) / count
        open_ratio = float(via_open_count) / count
        mapfiles_ratio = float(via_mapfiles_count) / count
        dict_ratio = float(via_dict_count) / count
        maps_ratio = float(via_maps_count) / count
    else:
        maps_only = False
        open_devino_ratio = 0.0
        open_ratio = 0.0
        mapfiles_ratio = 0.0
        dict_ratio = 0.0
        maps_ratio = 0.0

    confidence_score = compute_confidence(
        via_open_count,
        via_mapfiles_count,
        via_dict_count,
        via_maps_count,
        open_with_devino_count,
    )
    score = compute_score(
        first_ratio,
        count,
        open_count,
        mmap_count,
        open_with_devino_count,
        open_ratio,
        mapfiles_ratio,
        dict_ratio,
        maps_ratio,
        maps_only,
    )
    item["maps_only"] = maps_only
    item["open_devino_ratio"] = open_devino_ratio
    item["open_ratio"] = open_ratio
    item["mapfiles_ratio"] = mapfiles_ratio
    item["dict_ratio"] = dict_ratio
    item["maps_ratio"] = maps_ratio
    item["confidence_score"] = confidence_score
    item["score"] = score
    item["score_per_byte"] = score / float(size)
    aliases = item.get("aliases") or [item["path"]]
    seen = set()
    uniq = []
    for path in aliases:
        if path and path not in seen:
            uniq.append(path)
            seen.add(path)
    if item["path"] in seen:
        uniq = [item["path"]] + [p for p in uniq if p != item["path"]]
    item["aliases"] = uniq
    item["alias_count"] = len(uniq)
    return item


def candidate_identity(item):
    dev = int(item.get("dev") or 0)
    ino = int(item.get("ino") or 0)
    if dev > 0 and ino > 0:
        return f"devino:{dev}:{ino}", "devino"
    return f"path:{item['path']}", "path"


def representative_sort_key(item):
    return (
        0 if item["via_mapfiles_count"] > 0 else 1,
        0 if item["via_dict_count"] > 0 else 1,
        0 if item["mmap_count"] > 0 else 1,
        0 if item["open_with_devino_count"] > 0 else 1,
        0 if item["open_count"] > 0 else 1,
        item["first_index"],
        -float(item["count"]),
        len(item["path"]),
        item["path"],
    )


def merge_candidates_by_identity(candidates):
    groups = {}
    for item in candidates:
        identity, identity_source = candidate_identity(item)
        grouped = dict(item)
        grouped["identity"] = identity
        grouped["identity_source"] = identity_source
        groups.setdefault(identity, []).append(grouped)

    merged = []
    alias_groups = 0
    merged_paths = 0
    alias_examples = []
    for identity, items in groups.items():
        if len(items) == 1:
            item = dict(items[0])
            item["aliases"] = item.get("aliases") or [item["path"]]
            merged.append(finalize_candidate(item))
            continue

        alias_groups += 1
        merged_paths += len(items) - 1
        rep = min(items, key=representative_sort_key)
        alias_paths = [rep["path"]]
        alias_paths.extend(sorted({item["path"] for item in items if item["path"] != rep["path"]}))
        merged_item = {
            "path": rep["path"],
            "size": max(int(item.get("size") or 0) for item in items),
            "count": sum(float(item.get("count") or 0.0) for item in items),
            "open_count": sum(float(item.get("open_count") or 0.0) for item in items),
            "mmap_count": sum(float(item.get("mmap_count") or 0.0) for item in items),
            "open_with_devino_count": sum(float(item.get("open_with_devino_count") or 0.0) for item in items),
            "via_open_count": sum(float(item.get("via_open_count") or 0.0) for item in items),
            "via_mapfiles_count": sum(float(item.get("via_mapfiles_count") or 0.0) for item in items),
            "via_dict_count": sum(float(item.get("via_dict_count") or 0.0) for item in items),
            "via_maps_count": sum(float(item.get("via_maps_count") or 0.0) for item in items),
            "first_index": min(int(item.get("first_index") or 0) for item in items),
            "first_ratio": min(float(item.get("first_ratio") or 0.0) for item in items),
            "dev": int(rep.get("dev") or 0),
            "ino": int(rep.get("ino") or 0),
            "path_kind": str(rep.get("path_kind") or ""),
            "identity": identity,
            "identity_source": rep.get("identity_source") or "devino",
            "aliases": alias_paths,
        }
        if len(alias_examples) < PREVIEW_LIMIT:
            alias_examples.append(
                {
                    "identity": identity,
                    "path": rep["path"],
                    "aliases": alias_paths,
                }
            )
        merged.append(finalize_candidate(merged_item))

    return merged, {
        "identity_candidates": len(merged),
        "identity_alias_groups": alias_groups,
        "identity_merged_paths": merged_paths,
        "identity_alias_examples": alias_examples,
    }


def load_candidates(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    skipped_non_regular = 0
    skipped_kind_counts = {}
    skipped_preview = []
    for obj in data:
        candidate_path = obj.get("path") or ""
        if not candidate_path.startswith("/"):
            continue
        path_kind = normalize_path_kind(obj.get("path_kind"), obj.get("is_regular_file"))
        if not is_regular_path_kind(path_kind):
            skipped_non_regular += 1
            skipped_kind_counts[path_kind] = skipped_kind_counts.get(path_kind, 0) + 1
            if len(skipped_preview) < PREVIEW_LIMIT:
                skipped_preview.append({"path": candidate_path, "path_kind": path_kind})
            continue
        size = int(obj.get("size_bytes") or 0)
        raw_open = float(obj.get("open_count") or 0)
        raw_mmap = float(obj.get("mmap_count") or 0)
        raw_count = float(obj.get("count") or (raw_open + raw_mmap))
        item = {
            "path": candidate_path,
            "size": size,
            "count": float(obj.get("weighted_count") or raw_count),
            "open_count": float(obj.get("weighted_open_count") or raw_open),
            "mmap_count": float(obj.get("weighted_mmap_count") or raw_mmap),
            "open_with_devino_count": float(obj.get("weighted_open_with_devino_count") or obj.get("open_with_devino_count") or 0.0),
            "via_open_count": float(obj.get("weighted_via_open_count") or obj.get("via_open_count") or 0.0),
            "via_mapfiles_count": float(obj.get("weighted_via_mapfiles_count") or obj.get("via_mapfiles_count") or 0.0),
            "via_dict_count": float(obj.get("weighted_via_dict_count") or obj.get("via_dict_count") or 0.0),
            "via_maps_count": float(obj.get("weighted_via_maps_count") or obj.get("via_maps_count") or 0.0),
            "first_index": int(obj.get("first_index") or 0),
            "first_ratio": float(obj.get("first_ratio") or 0.0),
            "dev": int(obj.get("dev") or 0),
            "ino": int(obj.get("ino") or 0),
            "path_kind": path_kind or "regular",
            "aliases": [path for path in (obj.get("aliases") or [candidate_path]) if isinstance(path, str) and path.startswith("/")],
        }
        if not item["aliases"]:
            item["aliases"] = [candidate_path]
        out.append(finalize_candidate(item))
    return out, {
        "candidate_rows_total": len(data),
        "non_regular_candidates_skipped": skipped_non_regular,
        "non_regular_kind_counts": skipped_kind_counts,
        "non_regular_rejected_preview": skipped_preview,
    }


def load_optional_json(path):
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def stat_or_default(payload, key, default):
    if payload is None:
        return default
    if key in payload:
        return payload[key]
    return default


def load_preferred(path):
    if not path:
        return None
    lines = []
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln.startswith("/"):
                lines.append(ln)
    return lines


def preferred_rank(item, index):
    ranks = []
    for path in item.get("aliases") or [item["path"]]:
        if path in index:
            ranks.append(index[path])
    if not ranks:
        return None
    return min(ranks)


def preview_item(item, reason):
    out = {
        "path": item["path"],
        "reason": reason,
        "size": int(item["size"]),
        "count": float(item["count"]),
        "open_count": float(item["open_count"]),
        "mmap_count": float(item["mmap_count"]),
        "first_ratio": float(item["first_ratio"]),
        "score": float(item["score"]),
        "score_per_byte": float(item["score_per_byte"]),
        "maps_only": bool(item["maps_only"]),
        "open_devino_ratio": float(item["open_devino_ratio"]),
        "open_ratio": float(item["open_ratio"]),
        "mapfiles_ratio": float(item["mapfiles_ratio"]),
        "dict_ratio": float(item["dict_ratio"]),
        "maps_ratio": float(item["maps_ratio"]),
        "confidence_score": float(item["confidence_score"]),
    }
    if int(item.get("alias_count") or 0) > 1:
        out["alias_count"] = int(item["alias_count"])
        out["aliases"] = list(item.get("aliases") or [])
    if item.get("identity_source") == "devino":
        out["identity"] = item.get("identity")
    return out


def has_strong_evidence(item):
    return (
        item["open_count"] > 0
        or item["via_mapfiles_count"] > 0
        or item["via_dict_count"] > 0
        or item["open_with_devino_count"] > 0
    )


def build_identity_policy(raw_candidates, identity_stats, aggregate_stats, early_onehit_max):
    unique_paths = int(stat_or_default(aggregate_stats, "unique_paths", len(raw_candidates)) or 0)
    paths_with_devino = int(
        stat_or_default(
            aggregate_stats,
            "paths_with_devino",
            sum(1 for item in raw_candidates if int(item.get("dev") or 0) > 0 and int(item.get("ino") or 0) > 0),
        )
        or 0
    )
    paths_with_open_stat_devino = int(
        stat_or_default(
            aggregate_stats,
            "paths_with_open_stat_devino",
            0,
        )
        or 0
    )
    paths_with_open_stat_miss = int(
        stat_or_default(
            aggregate_stats,
            "paths_with_open_stat_miss",
            0,
        )
        or 0
    )
    paths_with_open_resolved_from_cwd = int(
        stat_or_default(
            aggregate_stats,
            "paths_with_open_resolved_from_cwd",
            0,
        )
        or 0
    )
    paths_with_open_cwd_dirfd_ambiguous = int(
        stat_or_default(
            aggregate_stats,
            "paths_with_open_cwd_dirfd_ambiguous",
            paths_with_open_resolved_from_cwd,
        )
        or 0
    )
    paths_with_mmap_raw_devino = int(
        stat_or_default(
            aggregate_stats,
            "paths_with_mmap_raw_devino",
            0,
        )
        or 0
    )
    identity_candidates = int(
        stat_or_default(
            aggregate_stats,
            "identity_candidates",
            identity_stats.get("identity_candidates", len(raw_candidates)),
        )
        or 0
    )
    identity_alias_groups = int(
        stat_or_default(
            aggregate_stats,
            "identity_alias_groups",
            identity_stats.get("identity_alias_groups", 0),
        )
        or 0
    )
    identity_merged_paths = int(
        stat_or_default(
            aggregate_stats,
            "identity_merged_paths",
            identity_stats.get("identity_merged_paths", 0),
        )
        or 0
    )
    paths_with_devino_ratio = float(paths_with_devino) / float(unique_paths) if unique_paths > 0 else 0.0
    paths_with_open_stat_devino_ratio = (
        float(paths_with_open_stat_devino) / float(unique_paths) if unique_paths > 0 else 0.0
    )
    paths_with_open_resolved_from_cwd_ratio = (
        float(paths_with_open_resolved_from_cwd) / float(unique_paths) if unique_paths > 0 else 0.0
    )
    paths_with_open_cwd_dirfd_ambiguous_ratio = (
        float(paths_with_open_cwd_dirfd_ambiguous) / float(unique_paths) if unique_paths > 0 else 0.0
    )
    paths_with_open_stat_miss_ratio = (
        float(paths_with_open_stat_miss) / float(unique_paths) if unique_paths > 0 else 0.0
    )
    paths_with_mmap_raw_devino_ratio = (
        float(paths_with_mmap_raw_devino) / float(unique_paths) if unique_paths > 0 else 0.0
    )
    devino_open_stat_share = (
        float(paths_with_open_stat_devino) / float(paths_with_devino) if paths_with_devino > 0 else 0.0
    )
    devino_open_cwd_dirfd_ambiguous_share = (
        float(paths_with_open_cwd_dirfd_ambiguous) / float(paths_with_devino) if paths_with_devino > 0 else 0.0
    )
    devino_mmap_raw_share = (
        float(paths_with_mmap_raw_devino) / float(paths_with_devino) if paths_with_devino > 0 else 0.0
    )

    mode = "normal"
    reason = "identity coverage is sufficient"
    max_maps_ratio_without_strong_evidence = 0.80
    effective_early_onehit_max = max(0, early_onehit_max)

    if unique_paths >= 20 and paths_with_devino_ratio < 0.15:
        mode = "strict"
        reason = "very low dev:ino coverage on a non-trivial sample"
        max_maps_ratio_without_strong_evidence = 0.55
        effective_early_onehit_max = 0
    elif unique_paths >= 10 and paths_with_devino_ratio < 0.30:
        mode = "guarded"
        reason = "low dev:ino coverage; reduce weak-evidence selection"
        max_maps_ratio_without_strong_evidence = 0.65
        effective_early_onehit_max = min(effective_early_onehit_max, 2)

    if mode == "guarded" and identity_alias_groups == 0 and identity_merged_paths == 0 and paths_with_devino_ratio < 0.20:
        reason = "low dev:ino coverage with no observed alias merges"
        max_maps_ratio_without_strong_evidence = min(max_maps_ratio_without_strong_evidence, 0.60)

    if (
        unique_paths >= 20
        and paths_with_devino >= 5
        and paths_with_open_stat_devino == paths_with_devino
        and paths_with_mmap_raw_devino == 0
    ):
        mode = "strict"
        reason = "dev:ino evidence relies entirely on OPEN stat backfill"
        max_maps_ratio_without_strong_evidence = min(max_maps_ratio_without_strong_evidence, 0.55)
        effective_early_onehit_max = 0
    elif (
        mode == "normal"
        and unique_paths >= 10
        and paths_with_devino >= 3
        and paths_with_open_stat_devino == paths_with_devino
        and paths_with_mmap_raw_devino == 0
    ):
        mode = "guarded"
        reason = "dev:ino evidence relies entirely on OPEN stat backfill"
        max_maps_ratio_without_strong_evidence = min(max_maps_ratio_without_strong_evidence, 0.65)
        effective_early_onehit_max = min(effective_early_onehit_max, 2)

    if (
        unique_paths >= 20
        and paths_with_open_cwd_dirfd_ambiguous_ratio >= 0.50
        and paths_with_mmap_raw_devino == 0
    ):
        mode = "strict"
        reason = "high dirfd-ambiguous OPEN share with no mmap raw identity evidence"
        max_maps_ratio_without_strong_evidence = min(max_maps_ratio_without_strong_evidence, 0.55)
        effective_early_onehit_max = 0
    elif (
        mode == "normal"
        and unique_paths >= 10
        and paths_with_open_cwd_dirfd_ambiguous_ratio >= 0.30
        and paths_with_mmap_raw_devino_ratio < 0.10
    ):
        mode = "guarded"
        reason = "high dirfd-ambiguous OPEN share with weak mmap raw identity evidence"
        max_maps_ratio_without_strong_evidence = min(max_maps_ratio_without_strong_evidence, 0.65)
        effective_early_onehit_max = min(effective_early_onehit_max, 2)

    if (
        unique_paths >= 20
        and paths_with_open_stat_miss_ratio >= 0.40
        and paths_with_mmap_raw_devino == 0
    ):
        mode = "strict"
        reason = "high OPEN stat-miss share with no mmap raw identity evidence"
        max_maps_ratio_without_strong_evidence = min(max_maps_ratio_without_strong_evidence, 0.55)
        effective_early_onehit_max = 0
    elif (
        mode == "normal"
        and unique_paths >= 10
        and paths_with_open_stat_miss_ratio >= 0.20
        and paths_with_mmap_raw_devino_ratio < 0.10
    ):
        mode = "guarded"
        reason = "elevated OPEN stat-miss share with weak mmap raw identity evidence"
        max_maps_ratio_without_strong_evidence = min(max_maps_ratio_without_strong_evidence, 0.65)
        effective_early_onehit_max = min(effective_early_onehit_max, 2)

    return {
        "mode": mode,
        "reason": reason,
        "unique_paths": unique_paths,
        "paths_with_devino": paths_with_devino,
        "paths_with_devino_ratio": paths_with_devino_ratio,
        "paths_with_open_resolved_from_cwd": paths_with_open_resolved_from_cwd,
        "paths_with_open_resolved_from_cwd_ratio": paths_with_open_resolved_from_cwd_ratio,
        "paths_with_open_cwd_dirfd_ambiguous": paths_with_open_cwd_dirfd_ambiguous,
        "paths_with_open_cwd_dirfd_ambiguous_ratio": paths_with_open_cwd_dirfd_ambiguous_ratio,
        "paths_with_open_stat_devino": paths_with_open_stat_devino,
        "paths_with_open_stat_devino_ratio": paths_with_open_stat_devino_ratio,
        "paths_with_open_stat_miss": paths_with_open_stat_miss,
        "paths_with_open_stat_miss_ratio": paths_with_open_stat_miss_ratio,
        "paths_with_mmap_raw_devino": paths_with_mmap_raw_devino,
        "paths_with_mmap_raw_devino_ratio": paths_with_mmap_raw_devino_ratio,
        "devino_open_stat_share": devino_open_stat_share,
        "devino_open_cwd_dirfd_ambiguous_share": devino_open_cwd_dirfd_ambiguous_share,
        "devino_mmap_raw_share": devino_mmap_raw_share,
        "identity_candidates": identity_candidates,
        "identity_alias_groups": identity_alias_groups,
        "identity_merged_paths": identity_merged_paths,
        "max_maps_ratio_without_strong_evidence": max_maps_ratio_without_strong_evidence,
        "effective_early_onehit_max": effective_early_onehit_max,
    }


def select_paths(
    candidates,
    preferred,
    budget_bytes,
    min_hits,
    p0_max,
    early_onehit_ratio,
    early_onehit_max,
    early_onehit_max_size_bytes,
    identity_policy,
):
    preferred_guard_active = bool(preferred) and identity_policy.get("mode") != "normal"
    if preferred:
        index = {p: i for i, p in enumerate(preferred)}
        filtered = []
        for candidate in candidates:
            rank = preferred_rank(candidate, index)
            if rank is None:
                continue
            candidate = dict(candidate)
            candidate["_preferred_rank"] = rank
            filtered.append(candidate)
        filtered.sort(key=lambda x: x["_preferred_rank"])
    else:
        filtered = sorted(
            candidates,
            key=lambda x: (-x["score_per_byte"], x["first_index"], -x["count"]),
        )

    chosen = []
    budget_used = 0
    early_onehit_chosen = 0
    forced_first = False
    effective_early_onehit_max = int(identity_policy.get("effective_early_onehit_max", early_onehit_max))
    weak_evidence_maps_ratio_limit = float(
        identity_policy.get("max_maps_ratio_without_strong_evidence", 0.80)
    )
    stats = {
        "regular_eligible": 0,
        "early_onehit_eligible": 0,
        "under_min_hits_skipped": 0,
        "early_onehit_limit_skipped": 0,
        "preferred_guard_active": preferred_guard_active,
        "preferred_maps_only_skipped": 0,
        "preferred_low_confidence_skipped": 0,
        "maps_only_skipped": 0,
        "low_confidence_skipped": 0,
        "budget_skipped": 0,
        "preferred_fallback_used": False,
        "chosen_preview": [],
        "rejected_preview": [],
    }
    for item in filtered:
        total_hits = item["open_count"] + item["mmap_count"]
        strong_evidence = has_strong_evidence(item)
        is_regular = total_hits >= min_hits
        is_early_onehit = (
            total_hits > 0
            and total_hits < min_hits
            and effective_early_onehit_max > 0
            and item["first_ratio"] <= early_onehit_ratio
            and item["size"] <= early_onehit_max_size_bytes
            and not item["maps_only"]
        )

        if (not is_regular) and (not is_early_onehit):
            stats["under_min_hits_skipped"] += 1
            if len(stats["rejected_preview"]) < PREVIEW_LIMIT:
                stats["rejected_preview"].append(preview_item(item, "under_min_hits"))
            continue
        if is_regular:
            stats["regular_eligible"] += 1
        if is_early_onehit:
            stats["early_onehit_eligible"] += 1
        if is_early_onehit and early_onehit_chosen >= effective_early_onehit_max:
            stats["early_onehit_limit_skipped"] += 1
            if len(stats["rejected_preview"]) < PREVIEW_LIMIT:
                stats["rejected_preview"].append(preview_item(item, "early_onehit_limit"))
            continue
        if item["maps_only"] and (not preferred or preferred_guard_active):
            stats["maps_only_skipped"] += 1
            if preferred_guard_active:
                stats["preferred_maps_only_skipped"] += 1
            if len(stats["rejected_preview"]) < PREVIEW_LIMIT:
                reject_reason = "maps_only"
                if preferred_guard_active:
                    reject_reason = f"preferred_identity_guard_{identity_policy.get('mode')}_maps_only"
                stats["rejected_preview"].append(preview_item(item, reject_reason))
            continue
        if (
            not strong_evidence
            and item["maps_ratio"] >= weak_evidence_maps_ratio_limit
            and (not preferred or preferred_guard_active)
        ):
            stats["low_confidence_skipped"] += 1
            if preferred_guard_active:
                stats["preferred_low_confidence_skipped"] += 1
            if len(stats["rejected_preview"]) < PREVIEW_LIMIT:
                reject_reason = "low_confidence"
                if preferred_guard_active:
                    reject_reason = f"preferred_identity_guard_{identity_policy.get('mode')}"
                elif identity_policy.get("mode") != "normal":
                    reject_reason = f"identity_guard_{identity_policy.get('mode')}"
                stats["rejected_preview"].append(preview_item(item, reject_reason))
            continue

        size = max(item["size"], MIN_SIZE_FALLBACK)
        if budget_bytes > 0 and chosen and budget_used + size > budget_bytes:
            # 若当前为空则至少纳入首个元素，即便超预算
            stats["budget_skipped"] += 1
            if len(stats["rejected_preview"]) < PREVIEW_LIMIT:
                stats["rejected_preview"].append(preview_item(item, "over_budget"))
            continue
        item["select_reason"] = "early_onehit" if is_early_onehit else "regular"
        chosen.append(item)
        budget_used += size
        if len(stats["chosen_preview"]) < PREVIEW_LIMIT:
            stats["chosen_preview"].append(preview_item(item, item["select_reason"]))
        if is_early_onehit:
            early_onehit_chosen += 1

    # 若 preferred 提供但没命中 min_hits，可退回按 score 选择
    if not chosen and preferred:
        chosen, budget_used, retry_stats = select_paths(
            candidates,
            preferred=None,
            budget_bytes=budget_bytes,
            min_hits=min_hits,
            p0_max=p0_max,
            early_onehit_ratio=early_onehit_ratio,
            early_onehit_max=early_onehit_max,
            early_onehit_max_size_bytes=early_onehit_max_size_bytes,
            identity_policy=identity_policy,
        )
        retry_stats["preferred_fallback_used"] = True
        retry_stats["preferred_guard_active"] = preferred_guard_active or bool(
            retry_stats.get("preferred_guard_active", False)
        )
        retry_stats["preferred_maps_only_skipped"] = int(retry_stats.get("preferred_maps_only_skipped", 0)) + int(
            stats.get("preferred_maps_only_skipped", 0)
        )
        retry_stats["preferred_low_confidence_skipped"] = int(
            retry_stats.get("preferred_low_confidence_skipped", 0)
        ) + int(stats.get("preferred_low_confidence_skipped", 0))
        retry_stats["preferred_rejected_preview"] = list(stats.get("rejected_preview", []))[:PREVIEW_LIMIT]
        return chosen, budget_used, retry_stats

    if not chosen and filtered:
        # 预算太小导致为空时，强行选择首个（最优）条目
        filtered[0]["select_reason"] = "forced_first"
        chosen.append(filtered[0])
        budget_used = max(filtered[0]["size"], MIN_SIZE_FALLBACK)
        forced_first = True
        if len(stats["chosen_preview"]) < PREVIEW_LIMIT:
            stats["chosen_preview"].append(preview_item(filtered[0], "forced_first"))

    stats["early_onehit_chosen"] = early_onehit_chosen
    stats["forced_first"] = forced_first
    return chosen, budget_used, stats


def write_outputs(chosen, out_yaml, out_list, p0_max):
    paths = [c["path"] for c in chosen]
    p0 = paths[:p0_max]
    rest = paths[p0_max:]
    p1 = rest[0::3]
    p2 = rest[1::3]
    p3 = rest[2::3]

    with open(out_yaml, "w", encoding="utf-8") as f:
        f.write("P0:\n")
        for p in p0:
            f.write(f"  - {p}\n")
        f.write("P1:\n")
        for p in p1:
            f.write(f"  - {p}\n")
        f.write("P2:\n")
        for p in p2:
            f.write(f"  - {p}\n")
        f.write("P3:\n")
        for p in p3:
            f.write(f"  - {p}\n")

    with open(out_list, "w", encoding="utf-8") as f:
        for p in paths:
            f.write(f"{p}\n")


def main():
    args = parse_args()
    raw_candidates, candidate_filter_stats = load_candidates(args.candidates)
    candidates, identity_stats = merge_candidates_by_identity(raw_candidates)
    aggregate_stats = load_optional_json(args.aggregate_stats)
    if not candidates:
        print("[selector] no candidates found", file=sys.stderr)
        Path(args.out_yaml).write_text("P0:\nP1:\nP2:\nP3:\n", encoding="utf-8")
        Path(args.out_list).write_text("", encoding="utf-8")
        if args.stats_json:
            empty_stats = {"chosen": 0, **candidate_filter_stats}
            Path(args.stats_json).write_text(json.dumps(empty_stats, indent=2, ensure_ascii=False), encoding="utf-8")
        return 0

    total_size = sum(max(c["size"], MIN_SIZE_FALLBACK) for c in candidates)
    budget_bytes = 0
    if args.budget_mb and args.budget_mb > 0:
        budget_bytes = int(args.budget_mb * 1024 * 1024)
    else:
        pct = max(args.budget_percent, 0.0)
        budget_bytes = int(total_size * (pct / 100.0))

    preferred = load_preferred(args.preferred)
    early_onehit_max_size_bytes = max(args.early_onehit_max_size_kb, 0) * 1024
    identity_policy = build_identity_policy(
        raw_candidates,
        identity_stats,
        aggregate_stats,
        args.early_onehit_max,
    )
    chosen, used, extra_stats = select_paths(
        candidates,
        preferred,
        budget_bytes,
        args.min_hits,
        args.p0_max,
        args.early_onehit_ratio,
        args.early_onehit_max,
        early_onehit_max_size_bytes,
        identity_policy,
    )

    write_outputs(chosen, args.out_yaml, args.out_list, args.p0_max)

    if args.stats_json:
        stats = {
            "candidate_rows_total": int(candidate_filter_stats.get("candidate_rows_total", len(raw_candidates))),
            "total_candidates": len(raw_candidates),
            "non_regular_candidates_skipped": int(candidate_filter_stats.get("non_regular_candidates_skipped", 0)),
            "non_regular_kind_counts": candidate_filter_stats.get("non_regular_kind_counts", {}),
            "identity_candidates": int(
                stat_or_default(aggregate_stats, "identity_candidates", identity_stats.get("identity_candidates", len(candidates))) or 0
            ),
            "identity_alias_groups": int(
                stat_or_default(aggregate_stats, "identity_alias_groups", identity_stats.get("identity_alias_groups", 0)) or 0
            ),
            "identity_merged_paths": int(
                stat_or_default(aggregate_stats, "identity_merged_paths", identity_stats.get("identity_merged_paths", 0)) or 0
            ),
            "devino_identity_candidates": int(stat_or_default(aggregate_stats, "devino_identity_candidates", 0) or 0),
            "identity_guard_mode": identity_policy["mode"],
            "identity_guard_reason": identity_policy["reason"],
            "identity_unique_paths": int(identity_policy["unique_paths"]),
            "identity_paths_with_devino": int(identity_policy["paths_with_devino"]),
            "identity_paths_with_devino_ratio": float(identity_policy["paths_with_devino_ratio"]),
            "identity_paths_with_open_resolved_from_cwd": int(identity_policy["paths_with_open_resolved_from_cwd"]),
            "identity_paths_with_open_resolved_from_cwd_ratio": float(identity_policy["paths_with_open_resolved_from_cwd_ratio"]),
            "identity_paths_with_open_cwd_dirfd_ambiguous": int(identity_policy["paths_with_open_cwd_dirfd_ambiguous"]),
            "identity_paths_with_open_cwd_dirfd_ambiguous_ratio": float(identity_policy["paths_with_open_cwd_dirfd_ambiguous_ratio"]),
            "identity_paths_with_open_stat_devino": int(identity_policy["paths_with_open_stat_devino"]),
            "identity_paths_with_open_stat_devino_ratio": float(identity_policy["paths_with_open_stat_devino_ratio"]),
            "identity_paths_with_open_stat_miss": int(identity_policy["paths_with_open_stat_miss"]),
            "identity_paths_with_open_stat_miss_ratio": float(identity_policy["paths_with_open_stat_miss_ratio"]),
            "identity_paths_with_mmap_raw_devino": int(identity_policy["paths_with_mmap_raw_devino"]),
            "identity_paths_with_mmap_raw_devino_ratio": float(identity_policy["paths_with_mmap_raw_devino_ratio"]),
            "identity_devino_open_stat_share": float(identity_policy["devino_open_stat_share"]),
            "identity_devino_open_cwd_dirfd_ambiguous_share": float(identity_policy["devino_open_cwd_dirfd_ambiguous_share"]),
            "identity_devino_mmap_raw_share": float(identity_policy["devino_mmap_raw_share"]),
            "effective_early_onehit_max": int(identity_policy["effective_early_onehit_max"]),
            "max_maps_ratio_without_strong_evidence": float(identity_policy["max_maps_ratio_without_strong_evidence"]),
            "total_size_bytes": total_size,
            "budget_bytes": budget_bytes,
            "chosen": len(chosen),
            "chosen_bytes": used,
            "preferred_count": len(preferred) if preferred else 0,
            "preferred_guard_active": bool(extra_stats.get("preferred_guard_active", False)),
            "early_onehit_ratio": args.early_onehit_ratio,
            "early_onehit_max": args.early_onehit_max,
            "early_onehit_max_size_bytes": early_onehit_max_size_bytes,
            "early_onehit_chosen": int(extra_stats.get("early_onehit_chosen", 0)),
            "forced_first": bool(extra_stats.get("forced_first", False)),
            "preferred_fallback_used": bool(extra_stats.get("preferred_fallback_used", False)),
            "regular_eligible": int(extra_stats.get("regular_eligible", 0)),
            "early_onehit_eligible": int(extra_stats.get("early_onehit_eligible", 0)),
            "under_min_hits_skipped": int(extra_stats.get("under_min_hits_skipped", 0)),
            "early_onehit_limit_skipped": int(extra_stats.get("early_onehit_limit_skipped", 0)),
            "maps_only_candidates": int(sum(1 for c in candidates if c.get("maps_only"))),
            "preferred_maps_only_skipped": int(extra_stats.get("preferred_maps_only_skipped", 0)),
            "maps_only_skipped": int(extra_stats.get("maps_only_skipped", 0)),
            "preferred_low_confidence_skipped": int(extra_stats.get("preferred_low_confidence_skipped", 0)),
            "low_confidence_skipped": int(extra_stats.get("low_confidence_skipped", 0)),
            "budget_skipped": int(extra_stats.get("budget_skipped", 0)),
            "chosen_with_open": int(sum(1 for c in chosen if c.get("open_count", 0) > 0)),
            "chosen_with_open_devino": int(sum(1 for c in chosen if c.get("open_with_devino_count", 0) > 0)),
            "chosen_with_mapfiles": int(sum(1 for c in chosen if c.get("via_mapfiles_count", 0) > 0)),
            "chosen_with_dict": int(sum(1 for c in chosen if c.get("via_dict_count", 0) > 0)),
            "chosen_with_maps": int(sum(1 for c in chosen if c.get("via_maps_count", 0) > 0)),
            "chosen_maps_only": int(sum(1 for c in chosen if c.get("maps_only"))),
            "chosen_with_aliases": int(sum(1 for c in chosen if int(c.get("alias_count") or 0) > 1)),
            "identity_alias_examples": identity_stats.get("identity_alias_examples", []),
            "non_regular_rejected_preview": candidate_filter_stats.get("non_regular_rejected_preview", []),
            "preferred_rejected_preview": extra_stats.get("preferred_rejected_preview", []),
            "chosen_preview": extra_stats.get("chosen_preview", []),
            "rejected_preview": extra_stats.get("rejected_preview", []),
        }
        Path(args.stats_json).write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"[selector] chosen={len(chosen)} budget_bytes={budget_bytes} used={used}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
