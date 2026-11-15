#!/usr/bin/env python3
# optimizer/tools/enrich_mmap_events.py  (keep frequency + order)
# 输入：
#   --raw:   原始事件 raw_events.jsonl（OPEN/MMAP 混合，可能 double-encoded）
#   --events: export_events.py 生成的过滤事件（OPEN 元数据增强版，可选）
#   --devino: build_devino_dict.py 生成的 { "dev:ino": {"paths":[...], "size_bytes":N}, ... }
#   --maps:  snapshot_maps.py 抓取的仍在映射的绝对路径列表
#   --mapfiles: snapshot_maps.py 抓取的 dev:ino -> path 映射（/proc/<pid>/map_files）
#   --container-pid: 用于做容器视角 stat 获取 size（可选但推荐）
# 输出：
#   --out: events_enriched.jsonl，每行 JSON：
#        { "type":"OPEN"|"MMAP", "path":"/abs", "via":"open|dict|maps",
#          "ts_ns": <int or 0>, "size_bytes": <int or 0> }
#
# 规则：
#  - OPEN / MMAP 来源于原始事件：不去重，逐条输出，保留顺序 = 真实频次。
#  - 对 MMAP 的 dev:ino 回填，仅取字典里的第一条路径（避免将一次 mmap 扩展为多条导致频次虚高）。
#  - maps 快照只做兜底：仅在该路径此前从事件中没出现过时，追加一条 via="maps" 的记录（ts_ns=0）。

import argparse, json, os, stat, sys

DROP_PREFIXES = ("/proc/", "/sys/", "/dev/", "/run/", "/var/run/")

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--raw", required=True)
    p.add_argument("--events")
    p.add_argument("--devino", required=True)
    p.add_argument("--maps", required=True)
    p.add_argument("--mapfiles")
    p.add_argument("--out", required=True)
    p.add_argument("--container-pid", type=int, default=0)
    return p.parse_args()

def good_abs_path(p: str) -> bool:
    if not p or not p.startswith("/"): return False
    if p.endswith("/"): return False
    for d in DROP_PREFIXES:
        if p.startswith(d): return False
    return True

def iter_jsonl(path):
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
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


def iter_raw_with_index(path):
    raw_index = 0
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for ln in f:
            line = ln.strip()
            if not line:
                continue
            raw_index += 1
            try:
                yield raw_index, json.loads(line)
            except Exception:
                try:
                    yield raw_index, json.loads(json.loads(line))
                except Exception:
                    continue

def host_view_path(container_pid: int, abs_path: str) -> str:
    if container_pid > 0:
        return f"/proc/{container_pid}/root{abs_path}"
    return abs_path

def classify_path_kind(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "regular"
    if stat.S_ISDIR(mode):
        return "directory"
    return "other"

def stat_metadata(container_pid: int, abs_path: str) -> tuple[int, str]:
    try:
        hv = host_view_path(container_pid, abs_path)
        st = os.stat(hv, follow_symlinks=True)
        return int(st.st_size), classify_path_kind(st.st_mode)
    except Exception:
        return 0, ""

def load_devino(devino_path):
    try:
        raw = json.load(open(devino_path, "r", encoding="utf-8"))
    except Exception:
        return {}
    # 兼容老格式：value 可能是 ["path1", ...]
    norm = {}
    for k, v in raw.items():
        if isinstance(v, dict):
            paths = [p for p in v.get("paths", []) if good_abs_path(p)]
            sz = int(v.get("size_bytes", 0) or 0)
            path_kind = str(v.get("path_kind") or "").strip().lower()
        elif isinstance(v, list):
            paths = [p for p in v if good_abs_path(p)]
            sz = 0
            path_kind = ""
        else:
            paths, sz, path_kind = [], 0, ""
        # 去重但保持相对顺序
        seen = set(); uniq = []
        for p in paths:
            if p not in seen:
                uniq.append(p); seen.add(p)
        norm[k] = {"paths": uniq, "size_bytes": sz, "path_kind": path_kind}
    return norm

def load_mapfiles(path):
    if not path:
        return {}
    try:
        raw = json.load(open(path, "r", encoding="utf-8"))
    except Exception:
        return {}
    norm = {}
    for k, v in raw.items():
        if isinstance(v, str):
            p = v
        elif isinstance(v, list):
            p = v[0] if v else ""
        elif isinstance(v, dict):
            paths = v.get("paths") or []
            p = paths[0] if paths else (v.get("path") or "")
        else:
            p = ""
        if good_abs_path(p):
            norm[k] = p
    return norm


def load_exported_open_events(path):
    if not path:
        return {}
    out = {}
    for obj in iter_jsonl(path):
        if str(obj.get("type", "")).upper() != "OPEN":
            continue
        try:
            raw_index = int(obj.get("raw_index", 0) or 0)
        except Exception:
            raw_index = 0
        if raw_index <= 0:
            continue
        out[raw_index] = obj
    return out

def main():
    a = parse_args()
    devino = load_devino(a.devino)
    mapfiles = load_mapfiles(a.mapfiles)
    exported_open = load_exported_open_events(a.events)

    out = []
    out_paths = set()   # 已经通过事件输出过的“路径集合”（用于 maps 兜底去重）
    open_cnt = 0
    mmap_mapfiles_used = 0
    mmap_dict_used = 0
    mmap_dict_miss = 0
    maps_added = 0

    # 1) 单遍处理 raw_events，保持 OPEN/MMAP 的原始顺序
    for raw_index, obj in iter_raw_with_index(a.raw):
        typ = str(obj.get("type","")).upper()
        if typ == "OPEN":
            src = exported_open.get(raw_index, obj)
            p = str(src.get("path",""))
            if not good_abs_path(p):
                continue
            ts = int(src.get("ts_ns") or src.get("ts") or obj.get("ts_ns") or obj.get("ts") or 0)
            sr = int(src.get("sample_rate") or obj.get("sample_rate") or 0)
            size = int(src.get("size_bytes") or 0)
            path_kind = str(src.get("path_kind") or "").strip().lower()
            if size == 0 or not path_kind:
                stat_size_value, stat_path_kind = stat_metadata(a.container_pid, p)
                if size == 0:
                    size = stat_size_value
                if not path_kind:
                    path_kind = stat_path_kind
            rec = {
                "type": "OPEN",
                "path": p,
                "via": "open",
                "ts_ns": ts,
                "size_bytes": size,
                "sample_rate": sr,
                "raw_index": raw_index,
            }
            try:
                dev = int(src.get("dev", 0) or 0)
                ino = int(src.get("ino", 0) or 0)
            except Exception:
                dev = 0
                ino = 0
            if dev > 0:
                rec["dev"] = dev
            if ino > 0:
                rec["ino"] = ino
            try:
                dirfd = int(src.get("dirfd"))
            except Exception:
                dirfd = None
            if dirfd is not None:
                rec["dirfd"] = dirfd
            if src.get("devino_source"):
                rec["devino_source"] = src.get("devino_source")
            if src.get("stat_lookup"):
                rec["stat_lookup"] = src.get("stat_lookup")
            if src.get("stat_lookup_kind"):
                rec["stat_lookup_kind"] = src.get("stat_lookup_kind")
            if src.get("path_resolution"):
                rec["path_resolution"] = src.get("path_resolution")
            if src.get("path_resolution_risk"):
                rec["path_resolution_risk"] = src.get("path_resolution_risk")
            if src.get("path_resolution_detail"):
                rec["path_resolution_detail"] = src.get("path_resolution_detail")
            if src.get("path_original"):
                rec["path_original"] = src.get("path_original")
            if path_kind:
                rec["path_kind"] = path_kind
            out.append(rec)
            out_paths.add(p)
            open_cnt += 1
            continue

        if typ != "MMAP":
            continue

        ts = int(obj.get("ts_ns") or obj.get("ts") or 0)
        sr = int(obj.get("sample_rate") or 0)
        try:
            dev = int(obj.get("dev", 0)); ino = int(obj.get("ino", 0))
        except Exception:
            dev = 0; ino = 0
        if dev == 0 or ino == 0:
            mmap_dict_miss += 1
            continue
        key = f"{dev}:{ino}"

        p = mapfiles.get(key)
        via = "mapfiles" if p else "dict"
        entry = None
        if not p:
            entry = devino.get(key)
            if not entry or not entry["paths"]:
                mmap_dict_miss += 1
                continue
            p = entry["paths"][0]  # 只取第一条，避免膨胀

        if not good_abs_path(p):
            mmap_dict_miss += 1
            continue

        size = 0
        path_kind = ""
        if entry:
            size = int(entry.get("size_bytes", 0) or 0)
            path_kind = str(entry.get("path_kind") or "").strip().lower()
        if size == 0 or not path_kind:
            stat_size_value, stat_path_kind = stat_metadata(a.container_pid, p)
            if size == 0:
                size = stat_size_value
            if not path_kind:
                path_kind = stat_path_kind

        rec = {
            "type": "MMAP",
            "path": p,
            "via": via,
            "ts_ns": ts,
            "size_bytes": size,
            "sample_rate": sr,
            "raw_index": raw_index,
            "dev": dev,
            "ino": ino,
            "devino_source": "mmap_raw",
        }
        if path_kind:
            rec["path_kind"] = path_kind
        out.append(rec)
        out_paths.add(p)
        if via == "mapfiles":
            mmap_mapfiles_used += 1
        else:
            mmap_dict_used += 1

    # 3) maps 兜底：仅当该路径此前未在 out_paths 中出现时，追加一条 via="maps" 的记录（ts=0）
    try:
        with open(a.maps, "r", encoding="utf-8") as f:
            for ln in f:
                p = ln.strip()
                if not good_abs_path(p):
                    continue
                if p in out_paths:
                    continue
                size, path_kind = stat_metadata(a.container_pid, p)
                rec = {"type":"MMAP","path":p,"via":"maps","ts_ns":0,"size_bytes":size,"sample_rate":0,"raw_index":0}
                if path_kind:
                    rec["path_kind"] = path_kind
                out.append(rec)
                out_paths.add(p)
                maps_added += 1
    except Exception:
        pass

    with open(a.out, "w", encoding="utf-8") as fo:
        for rec in out:
            fo.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"[enrich] OPEN={open_cnt} MMAP_mapfiles_used={mmap_mapfiles_used} MMAP_dict_used={mmap_dict_used} MMAP_dict_miss={mmap_dict_miss} MMAP_maps_added={maps_added} -> {a.out}",
          file=sys.stderr)

if __name__ == "__main__":
    main()
