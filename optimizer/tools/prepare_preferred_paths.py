#!/usr/bin/env python3
import argparse
import json
import os
import re
import sys
from pathlib import Path


PREVIEW_LIMIT = 12
SPECIAL_PREFIXES = ("/proc/", "/sys/", "/dev/", "/run/", "/var/run/")


def parse_args():
    parser = argparse.ArgumentParser(description="Prepare guard-aware preferred paths from raw LLM YAML.")
    parser.add_argument("--hints-raw", required=True)
    parser.add_argument("--candidates", required=True)
    parser.add_argument("--selector-stats")
    parser.add_argument("--container-pid", type=int)
    parser.add_argument("--out", required=True)
    parser.add_argument("--audit-json", required=True)
    return parser.parse_args()


def load_json(path):
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def num_value(item, *keys, default=0.0):
    for key in keys:
        value = item.get(key)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except Exception:
            continue
    return float(default)


def bool_value(item, key, default=False):
    value = item.get(key)
    if value in (None, ""):
        return default
    return bool(value)


def normalize_path_kind(item):
    raw = str(item.get("path_kind") or "").strip().lower()
    if raw in ("regular", "directory", "other"):
        return raw
    if "is_regular_file" in item:
        return "regular" if bool(item.get("is_regular_file")) else "other"
    return ""


def candidate_aliases(item):
    aliases = []
    seen = set()
    for path in [item.get("path")] + list(item.get("aliases") or []):
        if not isinstance(path, str) or not path.startswith("/"):
            continue
        if path in seen:
            continue
        aliases.append(path)
        seen.add(path)
    return aliases


def parse_candidate(item):
    count = num_value(item, "weighted_count", "count", default=0.0)
    open_count = num_value(item, "weighted_open_count", "open_count", default=0.0)
    open_with_devino_count = num_value(
        item,
        "weighted_open_with_devino_count",
        "open_with_devino_count",
        default=0.0,
    )
    via_mapfiles_count = num_value(item, "weighted_via_mapfiles_count", "via_mapfiles_count", default=0.0)
    via_dict_count = num_value(item, "weighted_via_dict_count", "via_dict_count", default=0.0)
    via_maps_count = num_value(item, "weighted_via_maps_count", "via_maps_count", default=0.0)
    maps_ratio = item.get("maps_ratio")
    if maps_ratio in (None, ""):
        maps_ratio = (via_maps_count / count) if count > 0 else 0.0
    else:
        try:
            maps_ratio = float(maps_ratio)
        except Exception:
            maps_ratio = (via_maps_count / count) if count > 0 else 0.0
    maps_only = bool_value(item, "maps_only", default=(via_maps_count > 0 and abs(count - via_maps_count) < 1e-9))
    strong_evidence = (
        open_count > 0
        or via_mapfiles_count > 0
        or via_dict_count > 0
        or open_with_devino_count > 0
    )
    return {
        "path": item.get("path") or "",
        "aliases": candidate_aliases(item),
        "maps_ratio": float(maps_ratio),
        "maps_only": maps_only,
        "strong_evidence": strong_evidence,
        "path_kind": normalize_path_kind(item),
    }


def build_lookup(candidates):
    lookup = {}
    for raw in candidates:
        item = parse_candidate(raw)
        if not item["path"].startswith("/"):
            continue
        for alias in item["aliases"] or [item["path"]]:
            lookup.setdefault(alias, item)
    return lookup


def parse_hint_paths(text):
    paths = []
    for line in text.splitlines():
        match = re.match(r"^\s*-\s*(.+?)\s*$", line)
        if not match:
            continue
        value = match.group(1).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1].strip()
        paths.append(value)
    return paths


def invalid_reason(path):
    if not path:
        return "empty"
    if not path.startswith("/"):
        return "not_absolute"
    if "*" in path:
        return "glob"
    for prefix in SPECIAL_PREFIXES:
        if path.startswith(prefix):
            return "special_prefix"
    return None


def load_policy(stats):
    payload = stats or {}
    mode = str(payload.get("identity_guard_mode") or "normal")
    try:
        weak_maps_limit = float(payload.get("max_maps_ratio_without_strong_evidence") or 0.80)
    except Exception:
        weak_maps_limit = 0.80
    return {
        "mode": mode,
        "weak_maps_limit": weak_maps_limit,
    }


def reject_reason(candidate, policy):
    if candidate["path_kind"] and candidate["path_kind"] != "regular":
        return "non_regular_candidate"
    if policy["mode"] == "normal":
        return None
    if candidate["maps_only"]:
        return "guard_maps_only"
    if (not candidate["strong_evidence"]) and candidate["maps_ratio"] >= policy["weak_maps_limit"]:
        return "guard_low_confidence"
    return None


def preview_entry(raw_path, candidate, reason):
    entry = {
        "path": raw_path,
        "reason": reason,
    }
    if candidate:
        entry["candidate_path"] = candidate["path"]
        entry["path_kind"] = candidate["path_kind"]
        entry["maps_ratio"] = float(candidate["maps_ratio"])
        entry["maps_only"] = bool(candidate["maps_only"])
        entry["strong_evidence"] = bool(candidate["strong_evidence"])
    return entry


def container_root_path(pid):
    if not pid or int(pid) <= 0:
        return None
    root = Path(f"/proc/{int(pid)}/root")
    if not root.exists():
        return None
    return root


def exists_in_container(root, path):
    if root is None:
        return True
    return os.path.exists(root / path.lstrip("/"))


def main():
    args = parse_args()
    candidates = load_json(args.candidates) or []
    selector_stats = load_json(args.selector_stats) or {}
    raw_text = Path(args.hints_raw).read_text(encoding="utf-8") if Path(args.hints_raw).exists() else ""
    lookup = build_lookup(candidates)
    policy = load_policy(selector_stats)
    raw_paths = parse_hint_paths(raw_text)
    container_root = container_root_path(args.container_pid)

    accepted = []
    accepted_seen = set()
    audit = {
        "identity_guard_mode": policy["mode"],
        "max_maps_ratio_without_strong_evidence": float(policy["weak_maps_limit"]),
        "container_pid": int(args.container_pid or 0),
        "container_root_check_enabled": bool(container_root),
        "llm_paths_total": len(raw_paths),
        "accepted_total": 0,
        "duplicate_skipped": 0,
        "invalid_path_skipped": 0,
        "special_path_skipped": 0,
        "unknown_candidate_skipped": 0,
        "non_regular_candidate_skipped": 0,
        "guard_maps_only_skipped": 0,
        "guard_low_confidence_skipped": 0,
        "missing_in_container_skipped": 0,
        "alias_paths_accepted": 0,
        "accepted_preview": [],
        "rejected_preview": [],
    }

    for raw_path in raw_paths:
        reason = invalid_reason(raw_path)
        candidate = None
        if reason is not None:
            if reason == "special_prefix":
                audit["special_path_skipped"] += 1
            else:
                audit["invalid_path_skipped"] += 1
            if len(audit["rejected_preview"]) < PREVIEW_LIMIT:
                audit["rejected_preview"].append(preview_entry(raw_path, None, reason))
            continue

        if raw_path in accepted_seen:
            audit["duplicate_skipped"] += 1
            if len(audit["rejected_preview"]) < PREVIEW_LIMIT:
                audit["rejected_preview"].append(preview_entry(raw_path, None, "duplicate"))
            continue

        candidate = lookup.get(raw_path)
        if candidate is None:
            audit["unknown_candidate_skipped"] += 1
            if len(audit["rejected_preview"]) < PREVIEW_LIMIT:
                audit["rejected_preview"].append(preview_entry(raw_path, None, "unknown_candidate"))
            continue

        reason = reject_reason(candidate, policy)
        if reason == "guard_maps_only":
            audit["guard_maps_only_skipped"] += 1
        elif reason == "guard_low_confidence":
            audit["guard_low_confidence_skipped"] += 1
        elif reason == "non_regular_candidate":
            audit["non_regular_candidate_skipped"] += 1
        if reason is not None:
            if len(audit["rejected_preview"]) < PREVIEW_LIMIT:
                audit["rejected_preview"].append(preview_entry(raw_path, candidate, reason))
            continue

        if not exists_in_container(container_root, raw_path):
            audit["missing_in_container_skipped"] += 1
            if len(audit["rejected_preview"]) < PREVIEW_LIMIT:
                audit["rejected_preview"].append(preview_entry(raw_path, candidate, "missing_in_container"))
            continue

        accepted.append(raw_path)
        accepted_seen.add(raw_path)
        if raw_path != candidate["path"]:
            audit["alias_paths_accepted"] += 1
        if len(audit["accepted_preview"]) < PREVIEW_LIMIT:
            audit["accepted_preview"].append(preview_entry(raw_path, candidate, "accepted"))

    audit["accepted_total"] = len(accepted)
    audit["existing_in_container_total"] = len(accepted)
    Path(args.out).write_text("".join(f"{path}\n" for path in accepted), encoding="utf-8")
    Path(args.audit_json).write_text(json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        (
            "[grounding] "
            f"raw={audit['llm_paths_total']} accepted={audit['accepted_total']} "
            f"unknown={audit['unknown_candidate_skipped']} "
            f"guard_low_confidence={audit['guard_low_confidence_skipped']} "
            f"missing_in_container={audit['missing_in_container_skipped']} "
            f"guard_maps_only={audit['guard_maps_only_skipped']}"
        ),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
