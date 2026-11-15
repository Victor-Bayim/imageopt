#!/usr/bin/env python3
# optimizer/tools/build_devino_dict.py  (enhanced)
# 从过滤后的 events.jsonl 中提取 OPEN 路径，
# 在容器视角 (/proc/<pid>/root) 做 stat，建立
# dev:ino -> {paths: [...], size_bytes: N, path_kind: "regular"|"directory"|"other"}

import argparse, json, os, stat, sys

DROP_PREFIXES = ("/proc/", "/sys/", "/dev/", "/run/", "/var/run/")

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--container-pid", required=True, type=int)
    p.add_argument("--events", required=True)
    p.add_argument("--out", required=True)
    return p.parse_args()

def good_abs_path(p: str) -> bool:
    if not p or not p.startswith("/"): return False
    if p.endswith("/"): return False
    for d in DROP_PREFIXES:
        if p.startswith(d): return False
    return True

def iter_json_lines(path):
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


def classify_path_kind(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "regular"
    if stat.S_ISDIR(mode):
        return "directory"
    return "other"

def main():
    a = parse_args()
    root_prefix = f"/proc/{a.container_pid}/root"

    open_paths = set()
    for obj in iter_json_lines(a.events):
        if str(obj.get("type","")).upper() != "OPEN":
            continue
        p = str(obj.get("path",""))
        if good_abs_path(p):
            open_paths.add(p)

    devino = {}  # key: "dev:ino" -> {"paths":[...], "size_bytes": int, "path_kind": str}
    ok = fail = 0
    open_event_info = {}
    for obj in iter_json_lines(a.events):
        if str(obj.get("type","")).upper() != "OPEN":
            continue
        p = str(obj.get("path",""))
        if not good_abs_path(p):
            continue
        try:
            dev = int(obj.get("dev", 0) or 0)
            ino = int(obj.get("ino", 0) or 0)
        except Exception:
            dev = 0
            ino = 0
        size = int(obj.get("size_bytes", 0) or 0)
        path_kind = str(obj.get("path_kind") or "").strip().lower()
        if dev > 0 and ino > 0:
            open_event_info[p] = (dev, ino, size, path_kind)

    for p in sorted(open_paths):
        cached = open_event_info.get(p)
        if cached:
            dev, ino, size, path_kind = cached
            key = f"{dev}:{ino}"
            entry = devino.setdefault(key, {"paths": [], "size_bytes": 0, "path_kind": ""})
            if p not in entry["paths"]:
                entry["paths"].append(p)
            if size and (entry["size_bytes"] == 0 or size > entry["size_bytes"]):
                entry["size_bytes"] = size
            if path_kind and not entry["path_kind"]:
                entry["path_kind"] = path_kind
            ok += 1
            continue
        host_view = os.path.join(root_prefix, p.lstrip("/"))
        try:
            st = os.stat(host_view, follow_symlinks=True)
            key = f"{st.st_dev}:{st.st_ino}"
            entry = devino.setdefault(key, {"paths": [], "size_bytes": 0, "path_kind": ""})
            if p not in entry["paths"]:
                entry["paths"].append(p)
            # 取文件大小（可能后续同一个 dev:ino 多路径，保留非0或更大者）
            sz = int(st.st_size)
            if sz and (entry["size_bytes"] == 0 or sz > entry["size_bytes"]):
                entry["size_bytes"] = sz
            path_kind = classify_path_kind(st.st_mode)
            if path_kind and not entry["path_kind"]:
                entry["path_kind"] = path_kind
            ok += 1
        except Exception:
            fail += 1
            continue

    with open(a.out, "w", encoding="utf-8") as fo:
        json.dump(devino, fo, ensure_ascii=False, indent=2)

    print(f"[devino] OPEN_paths={len(open_paths)} ok={ok} fail={fail} keys={len(devino)} -> {a.out}", file=sys.stderr)

if __name__ == "__main__":
    main()
