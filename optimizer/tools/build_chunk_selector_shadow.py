#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from optimizer.tools.build_toc_shadow import _build_toc_index, _load_json, _load_selected, _norm_path


PREVIEW_LIMIT = 8


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _load_candidate_index(path: Path) -> tuple[dict[str, dict], dict[str, str]]:
    payload = _load_json(path)
    rows = payload if isinstance(payload, list) else list((payload or {}).get("candidates") or [])
    candidates: dict[str, dict] = {}
    aliases_to_primary: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        primary = _norm_path(row.get("path"))
        if not primary:
            continue
        aliases = []
        seen: set[str] = set()
        for raw in list(row.get("aliases") or [primary]):
            alias = _norm_path(raw)
            if alias and alias not in seen:
                aliases.append(alias)
                seen.add(alias)
        if primary not in seen:
            aliases.insert(0, primary)
        candidate = {
            "path": primary,
            "size_bytes": _safe_int(row.get("size_bytes") or row.get("size")),
            "count": _safe_int(row.get("count")),
            "weighted_count": _safe_float(row.get("weighted_count") or row.get("count")),
            "confidence_score": _safe_float(row.get("confidence_score")),
            "first_ratio": _safe_float(row.get("first_ratio")),
            "aliases": aliases,
            "alias_count": _safe_int(row.get("alias_count") or len(aliases)),
            "identity": row.get("identity"),
            "identity_source": row.get("identity_source"),
        }
        candidates[primary] = candidate
        for alias in aliases:
            aliases_to_primary.setdefault(alias, primary)
    return candidates, aliases_to_primary


def _resolve_entry_for_candidate(
    *,
    toc_index: dict[str, dict],
    requested_path: str,
    candidate: dict | None,
) -> tuple[str | None, dict | None]:
    seen: set[str] = set()
    probe_paths: list[str] = []
    for raw in [requested_path] + list((candidate or {}).get("aliases") or []):
        path = _norm_path(raw)
        if path and path not in seen:
            probe_paths.append(path)
            seen.add(path)
    for path in probe_paths:
        entry = toc_index.get(path)
        if entry is not None:
            return path, entry
    return None, None


def _build_contribution_row(
    *,
    position: int,
    requested_path: str,
    candidate: dict,
    matched_path: str,
    entry: dict,
    seen_chunks: set[str],
) -> dict:
    total_bytes = 0
    total_compressed_bytes = 0
    incremental_bytes = 0
    incremental_compressed_bytes = 0
    new_chunk_count = 0
    shared_chunk_count = 0
    for chunk in list(entry.get("chunks") or []):
        chunk_key = str(chunk.get("key") or "")
        size = _safe_int(chunk.get("size"))
        compressed_size = _safe_int(chunk.get("compressed_size") or size)
        total_bytes += size
        total_compressed_bytes += compressed_size
        if chunk_key and chunk_key not in seen_chunks:
            seen_chunks.add(chunk_key)
            incremental_bytes += size
            incremental_compressed_bytes += compressed_size
            new_chunk_count += 1
        else:
            shared_chunk_count += 1
    overlap_bytes = max(total_bytes - incremental_bytes, 0)
    overlap_compressed_bytes = max(total_compressed_bytes - incremental_compressed_bytes, 0)
    incremental_ratio = (
        float(incremental_compressed_bytes) / float(total_compressed_bytes)
        if total_compressed_bytes > 0
        else 0.0
    )
    return {
        "position": position,
        "path": requested_path,
        "matched_toc_path": matched_path,
        "found_in_toc": True,
        "layer_digest": entry.get("layer_digest"),
        "weighted_count": _safe_float(candidate.get("weighted_count")),
        "confidence_score": _safe_float(candidate.get("confidence_score")),
        "first_ratio": _safe_float(candidate.get("first_ratio")),
        "alias_count": _safe_int(candidate.get("alias_count")),
        "chunk_count": len(list(entry.get("chunks") or [])),
        "new_chunk_count": new_chunk_count,
        "shared_chunk_count": shared_chunk_count,
        "total_bytes": total_bytes,
        "total_compressed_bytes": total_compressed_bytes,
        "incremental_bytes": incremental_bytes,
        "incremental_compressed_bytes": incremental_compressed_bytes,
        "overlap_bytes": overlap_bytes,
        "overlap_compressed_bytes": overlap_compressed_bytes,
        "incremental_compressed_ratio": incremental_ratio,
    }


def _summarize_selection(
    *,
    paths: list[str],
    toc_index: dict[str, dict],
    candidate_index: dict[str, dict],
    aliases_to_primary: dict[str, str],
    overlap_ratio_threshold: float,
) -> tuple[list[dict], list[str], list[dict], list[dict], dict]:
    seen_chunks: set[str] = set()
    rows: list[dict] = []
    missing: list[str] = []
    overlap_heavy_rows: list[dict] = []
    zero_incremental_rows: list[dict] = []
    totals = {
        "path_sum_bytes": 0,
        "path_sum_compressed_bytes": 0,
        "incremental_bytes": 0,
        "incremental_compressed_bytes": 0,
    }

    for position, requested_path in enumerate(paths, start=1):
        primary = aliases_to_primary.get(requested_path, requested_path)
        candidate = candidate_index.get(primary) or candidate_index.get(requested_path) or {}
        matched_path, entry = _resolve_entry_for_candidate(
            toc_index=toc_index,
            requested_path=requested_path,
            candidate=candidate,
        )
        if entry is None or matched_path is None:
            missing.append(requested_path)
            rows.append(
                {
                    "position": position,
                    "path": requested_path,
                    "found_in_toc": False,
                    "weighted_count": _safe_float(candidate.get("weighted_count")),
                    "confidence_score": _safe_float(candidate.get("confidence_score")),
                    "first_ratio": _safe_float(candidate.get("first_ratio")),
                }
            )
            continue

        row = _build_contribution_row(
            position=position,
            requested_path=requested_path,
            candidate=candidate,
            matched_path=matched_path,
            entry=entry,
            seen_chunks=seen_chunks,
        )
        rows.append(row)
        totals["path_sum_bytes"] += _safe_int(row.get("total_bytes"))
        totals["path_sum_compressed_bytes"] += _safe_int(row.get("total_compressed_bytes"))
        totals["incremental_bytes"] += _safe_int(row.get("incremental_bytes"))
        totals["incremental_compressed_bytes"] += _safe_int(row.get("incremental_compressed_bytes"))
        if _safe_int(row.get("incremental_compressed_bytes")) == 0:
            zero_incremental_rows.append(row)
        if (
            _safe_int(row.get("overlap_compressed_bytes")) > 0
            and _safe_float(row.get("incremental_compressed_ratio"), 1.0) < overlap_ratio_threshold
        ):
            overlap_heavy_rows.append(row)

    overlap_heavy_rows.sort(
        key=lambda row: (
            -_safe_int(row.get("overlap_compressed_bytes")),
            _safe_float(row.get("incremental_compressed_ratio"), 1.0),
            _safe_int(row.get("position")),
        )
    )
    zero_incremental_rows.sort(
        key=lambda row: (
            -_safe_int(row.get("total_compressed_bytes")),
            _safe_int(row.get("position")),
        )
    )
    return rows, missing, overlap_heavy_rows, zero_incremental_rows, totals


def _shadow_candidate_sort_key(row: dict) -> tuple:
    return (
        -_safe_float(row.get("shadow_density")),
        -_safe_float(row.get("weighted_count")),
        _safe_float(row.get("first_ratio"), 1.0),
        _safe_int(row.get("total_compressed_bytes")),
        str(row.get("path") or ""),
    )


def _build_shadow_alternative(
    *,
    toc_index: dict[str, dict],
    candidate_index: dict[str, dict],
    overlap_ratio_threshold: float,
    path_sum_compressed_budget: int,
    preview_limit: int,
    current_selected_paths: list[str],
) -> dict:
    if path_sum_compressed_budget <= 0:
        return {
            "strategy": "greedy_weighted_count_per_incremental_compressed_byte",
            "budget_path_sum_compressed_bytes": path_sum_compressed_budget,
            "candidate_pool": 0,
            "selected_paths_total": 0,
            "selected_path_sum_compressed_bytes": 0,
            "selected_incremental_compressed_bytes": 0,
            "selected_overlap_compressed_bytes": 0,
            "selected_overlap_compressed_ratio": 0.0,
            "selected_zero_incremental_paths": 0,
            "selected_overlap_heavy_paths": 0,
            "shadow_only_paths_preview": [],
            "current_only_paths_preview": current_selected_paths[: max(preview_limit, 0)],
            "replacement_suggestions_preview": [],
            "selected_preview": [],
            "selected_contributions": [],
        }
    candidate_pool: list[dict] = []
    for primary, candidate in candidate_index.items():
        matched_path, entry = _resolve_entry_for_candidate(
            toc_index=toc_index,
            requested_path=primary,
            candidate=candidate,
        )
        if entry is None or matched_path is None:
            continue
        total_compressed_bytes = sum(
            _safe_int(chunk.get("compressed_size") or chunk.get("size"))
            for chunk in list(entry.get("chunks") or [])
        )
        if total_compressed_bytes <= 0:
            continue
        candidate_pool.append(
            {
                "path": primary,
                "matched_toc_path": matched_path,
                "entry": entry,
                "candidate": candidate,
                "total_compressed_bytes": total_compressed_bytes,
            }
        )

    chosen_paths: list[str] = []
    chosen_path_set: set[str] = set()
    used_path_sum_compressed_bytes = 0
    seen_chunks: set[str] = set()
    shadow_rows: list[dict] = []

    while True:
        best: dict | None = None
        for row in candidate_pool:
            path = str(row.get("path") or "")
            if not path or path in chosen_path_set:
                continue
            total_compressed_bytes = _safe_int(row.get("total_compressed_bytes"))
            if path_sum_compressed_budget > 0 and used_path_sum_compressed_bytes + total_compressed_bytes > path_sum_compressed_budget:
                continue
            incremental_compressed_bytes = 0
            incremental_bytes = 0
            chunk_count = 0
            for chunk in list((row.get("entry") or {}).get("chunks") or []):
                chunk_count += 1
                chunk_key = str(chunk.get("key") or "")
                if chunk_key and chunk_key not in seen_chunks:
                    size = _safe_int(chunk.get("size"))
                    compressed_size = _safe_int(chunk.get("compressed_size") or size)
                    incremental_bytes += size
                    incremental_compressed_bytes += compressed_size
            if incremental_compressed_bytes <= 0:
                continue
            evaluated = {
                "path": path,
                "candidate": row.get("candidate") or {},
                "entry": row.get("entry") or {},
                "matched_toc_path": row.get("matched_toc_path"),
                "total_compressed_bytes": total_compressed_bytes,
                "incremental_compressed_bytes": incremental_compressed_bytes,
                "incremental_bytes": incremental_bytes,
                "chunk_count": chunk_count,
                "shadow_density": _safe_float((row.get("candidate") or {}).get("weighted_count")) / float(incremental_compressed_bytes),
                "weighted_count": _safe_float((row.get("candidate") or {}).get("weighted_count")),
                "first_ratio": _safe_float((row.get("candidate") or {}).get("first_ratio")),
            }
            if best is None or _shadow_candidate_sort_key(evaluated) < _shadow_candidate_sort_key(best):
                best = evaluated
        if best is None:
            break
        chosen_paths.append(best["path"])
        chosen_path_set.add(best["path"])
        used_path_sum_compressed_bytes += _safe_int(best.get("total_compressed_bytes"))
        row = _build_contribution_row(
            position=len(chosen_paths),
            requested_path=best["path"],
            candidate=best["candidate"],
            matched_path=str(best.get("matched_toc_path") or best["path"]),
            entry=best["entry"],
            seen_chunks=seen_chunks,
        )
        row["shadow_density"] = _safe_float(best.get("shadow_density"))
        shadow_rows.append(row)

    _, _, shadow_overlap_heavy, shadow_zero_incremental, shadow_totals = _summarize_selection(
        paths=chosen_paths,
        toc_index=toc_index,
        candidate_index=candidate_index,
        aliases_to_primary={path: path for path in candidate_index},
        overlap_ratio_threshold=overlap_ratio_threshold,
    )
    shadow_path_set = set(chosen_paths)
    current_path_set = set(current_selected_paths)
    shadow_only = [path for path in chosen_paths if path not in current_path_set]
    current_only = [path for path in current_selected_paths if path not in shadow_path_set]
    return {
        "strategy": "greedy_weighted_count_per_incremental_compressed_byte",
        "budget_path_sum_compressed_bytes": path_sum_compressed_budget,
        "candidate_pool": len(candidate_pool),
        "selected_paths_total": len(chosen_paths),
        "selected_path_sum_compressed_bytes": shadow_totals["path_sum_compressed_bytes"],
        "selected_incremental_compressed_bytes": shadow_totals["incremental_compressed_bytes"],
        "selected_overlap_compressed_bytes": max(
            shadow_totals["path_sum_compressed_bytes"] - shadow_totals["incremental_compressed_bytes"],
            0,
        ),
        "selected_overlap_compressed_ratio": (
            float(max(shadow_totals["path_sum_compressed_bytes"] - shadow_totals["incremental_compressed_bytes"], 0))
            / float(shadow_totals["path_sum_compressed_bytes"])
            if shadow_totals["path_sum_compressed_bytes"] > 0
            else 0.0
        ),
        "selected_zero_incremental_paths": len(shadow_zero_incremental),
        "selected_overlap_heavy_paths": len(shadow_overlap_heavy),
        "shadow_only_paths_preview": shadow_only[: max(preview_limit, 0)],
        "current_only_paths_preview": current_only[: max(preview_limit, 0)],
        "replacement_suggestions_preview": [],
        "selected_preview": shadow_rows[: max(preview_limit, 0)],
        "selected_contributions": shadow_rows,
    }


def _build_replacement_suggestions(
    *,
    current_only_paths: list[str],
    shadow_only_paths: list[str],
    current_rows: list[dict],
    shadow_rows: list[dict],
    preview_limit: int,
) -> list[dict]:
    current_index = {
        str(row.get("path") or ""): row
        for row in current_rows
        if isinstance(row, dict) and str(row.get("path") or "")
    }
    shadow_index = {
        str(row.get("path") or ""): row
        for row in shadow_rows
        if isinstance(row, dict) and str(row.get("path") or "")
    }
    suggestions: list[dict] = []
    for replace_path, add_path in zip(current_only_paths, shadow_only_paths):
        current_row = current_index.get(replace_path) or {}
        shadow_row = shadow_index.get(add_path) or {}
        suggestions.append(
            {
                "replace_path": replace_path,
                "add_path": add_path,
                "replace_incremental_compressed_bytes": _safe_int(
                    current_row.get("incremental_compressed_bytes")
                ),
                "replace_total_compressed_bytes": _safe_int(current_row.get("total_compressed_bytes")),
                "add_incremental_compressed_bytes": _safe_int(
                    shadow_row.get("incremental_compressed_bytes")
                ),
                "add_total_compressed_bytes": _safe_int(shadow_row.get("total_compressed_bytes")),
                "delta_incremental_compressed_bytes": _safe_int(
                    shadow_row.get("incremental_compressed_bytes")
                )
                - _safe_int(current_row.get("incremental_compressed_bytes")),
                "delta_total_compressed_bytes": _safe_int(shadow_row.get("total_compressed_bytes"))
                - _safe_int(current_row.get("total_compressed_bytes")),
            }
        )
        if len(suggestions) >= max(preview_limit, 0):
            break
    return suggestions


def build_shadow(
    *,
    toc_index: dict[str, dict],
    candidate_index: dict[str, dict],
    aliases_to_primary: dict[str, str],
    selected: list[str],
    overlap_ratio_threshold: float,
    preview_limit: int,
) -> dict:
    selected_rows, selected_missing, overlap_heavy_rows, zero_incremental_rows, selected_totals = _summarize_selection(
        paths=selected,
        toc_index=toc_index,
        candidate_index=candidate_index,
        aliases_to_primary=aliases_to_primary,
        overlap_ratio_threshold=overlap_ratio_threshold,
    )
    selected_found = sum(1 for row in selected_rows if row.get("found_in_toc"))
    overlap_compressed_ratio = (
        float(selected_totals["path_sum_compressed_bytes"] - selected_totals["incremental_compressed_bytes"])
        / float(selected_totals["path_sum_compressed_bytes"])
        if selected_totals["path_sum_compressed_bytes"] > 0
        else 0.0
    )
    warnings: list[str] = []
    if selected_missing:
        warnings.append(f"{len(selected_missing)} selected path(s) were not found in image TOC")
    if zero_incremental_rows:
        warnings.append(
            f"{len(zero_incremental_rows)} selected path(s) add zero new compressed TOC bytes in current order"
        )
    if overlap_heavy_rows:
        warnings.append(
            f"{len(overlap_heavy_rows)} selected path(s) contribute under {overlap_ratio_threshold:.0%} new compressed TOC bytes"
        )
    shadow_alternative = _build_shadow_alternative(
        toc_index=toc_index,
        candidate_index=candidate_index,
        overlap_ratio_threshold=overlap_ratio_threshold,
        path_sum_compressed_budget=selected_totals["path_sum_compressed_bytes"],
        preview_limit=preview_limit,
        current_selected_paths=selected,
    )
    shadow_delta = int(shadow_alternative.get("selected_incremental_compressed_bytes") or 0) - int(
        selected_totals["incremental_compressed_bytes"] or 0
    )
    shadow_overlap_delta = int(shadow_alternative.get("selected_overlap_compressed_bytes") or 0) - max(
        selected_totals["path_sum_compressed_bytes"] - selected_totals["incremental_compressed_bytes"],
        0,
    )
    replacement_suggestions = _build_replacement_suggestions(
        current_only_paths=list(shadow_alternative.get("current_only_paths_preview") or []),
        shadow_only_paths=list(shadow_alternative.get("shadow_only_paths_preview") or []),
        current_rows=selected_rows,
        shadow_rows=list(shadow_alternative.get("selected_contributions") or []),
        preview_limit=preview_limit,
    )
    shadow_alternative["replacement_suggestions_preview"] = replacement_suggestions
    if shadow_delta > 0:
        warnings.append(
            f"chunk-aware greedy shadow would add {shadow_delta} more compressed TOC bytes under the same path-sum budget"
        )
    if replacement_suggestions:
        top = replacement_suggestions[0]
        warnings.append(
            "shadow suggests replacing "
            f"{top.get('replace_path')} with {top.get('add_path')} "
            f"(+{top.get('delta_incremental_compressed_bytes')} incremental compressed TOC bytes)"
        )

    return {
        "audit_version": 1,
        "selected_paths_total": len(selected),
        "selected_paths_found": selected_found,
        "selected_paths_missing": len(selected_missing),
        "selected_path_sum_bytes": selected_totals["path_sum_bytes"],
        "selected_path_sum_compressed_bytes": selected_totals["path_sum_compressed_bytes"],
        "selected_incremental_bytes": selected_totals["incremental_bytes"],
        "selected_incremental_compressed_bytes": selected_totals["incremental_compressed_bytes"],
        "selected_overlap_bytes": max(selected_totals["path_sum_bytes"] - selected_totals["incremental_bytes"], 0),
        "selected_overlap_compressed_bytes": max(
            selected_totals["path_sum_compressed_bytes"] - selected_totals["incremental_compressed_bytes"],
            0,
        ),
        "selected_overlap_compressed_ratio": overlap_compressed_ratio,
        "selected_zero_incremental_paths": len(zero_incremental_rows),
        "selected_overlap_heavy_paths": len(overlap_heavy_rows),
        "selected_missing_paths_preview": selected_missing[: max(preview_limit, 0)],
        "zero_incremental_paths_preview": zero_incremental_rows[: max(preview_limit, 0)],
        "overlap_heavy_paths_preview": overlap_heavy_rows[: max(preview_limit, 0)],
        "selected_contributions": selected_rows,
        "shadow_alternative": shadow_alternative,
        "shadow_incremental_compressed_bytes_delta": shadow_delta,
        "shadow_overlap_compressed_bytes_delta": shadow_overlap_delta,
        "replacement_suggestions_preview": replacement_suggestions,
        "warnings": warnings,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit current selected paths under chunk-deduplicated TOC coverage.")
    parser.add_argument("--toc-json", required=True, help="path to image_toc.json")
    parser.add_argument("--candidates", required=True, help="path to candidates.json")
    parser.add_argument("--selected-list", required=True, help="path to estargz.list")
    parser.add_argument("--out", required=True, help="path to chunk_selector_shadow.json")
    parser.add_argument(
        "--overlap-ratio-threshold",
        type=float,
        default=0.50,
        help="flag selected paths whose new compressed-byte ratio falls below this threshold",
    )
    parser.add_argument(
        "--preview-limit",
        type=int,
        default=PREVIEW_LIMIT,
        help="number of preview rows to retain for overlap-heavy/zero-incremental paths",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    toc_path = Path(args.toc_json)
    candidates_path = Path(args.candidates)
    selected_path = Path(args.selected_list)
    out_path = Path(args.out)

    candidate_index, aliases_to_primary = _load_candidate_index(candidates_path)
    payload = build_shadow(
        toc_index=_build_toc_index(toc_path),
        candidate_index=candidate_index,
        aliases_to_primary=aliases_to_primary,
        selected=_load_selected(selected_path),
        overlap_ratio_threshold=max(min(float(args.overlap_ratio_threshold), 1.0), 0.0),
        preview_limit=max(int(args.preview_limit), 0),
    )
    payload["toc_source_path"] = str(toc_path)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
