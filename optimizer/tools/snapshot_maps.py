#!/usr/bin/env python3
# snapshot_maps.py v1
import argparse, os, sys, json

SKIP_SUFFIX = (" (deleted)",)
SKIP_PREFIX = ("/proc/",)
SKIP_EXACT_PREFIXES = ("[vdso]", "[vsyscall]", "[stack]", "[heap]", "memfd:")

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--container-pid", required=True, type=int)
    p.add_argument("--out", required=True)
    p.add_argument("--pids-file")
    p.add_argument("--mapfiles-json")
    p.add_argument("--cwd-json")
    return p.parse_args()

def list_pids_same_mntns(container_pid: int):
    pids = []
    try:
        target = os.stat(f"/proc/{container_pid}/ns/mnt").st_ino
    except Exception:
        return pids
    for name in os.listdir("/proc"):
        if not name.isdigit(): continue
        mnt = f"/proc/{name}/ns/mnt"
        maps = f"/proc/{name}/maps"
        try:
            if os.path.exists(maps) and os.stat(mnt).st_ino == target:
                pids.append(int(name))
        except Exception:
            continue
    return pids

def good_path(s: str) -> bool:
    if not s or not s.startswith("/"): return False
    for k in SKIP_SUFFIX:
        if s.endswith(k): return False
    for k in SKIP_EXACT_PREFIXES:
        if s.startswith(k): return False
    for k in SKIP_PREFIX:
        if s.startswith(k): return False
    return True

def collect_mapfiles(pids):
    """Collect dev:ino -> path mapping from /proc/<pid>/map_files."""
    out = {}
    for pid in pids:
        base = f"/proc/{pid}/map_files"
        try:
            entries = os.listdir(base)
        except Exception:
            continue
        for ent in entries:
            full = os.path.join(base, ent)
            try:
                target = os.readlink(full)
            except Exception:
                continue
            if not good_path(target):
                continue
            try:
                st = os.stat(full)
            except Exception:
                continue
            key = f"{st.st_dev}:{st.st_ino}"
            if key not in out:
                out[key] = target
    return out


def collect_cwds(pids):
    out = {}
    for pid in pids:
        link = f"/proc/{pid}/cwd"
        try:
            cwd = os.readlink(link)
        except Exception:
            continue
        if not cwd or not cwd.startswith("/"):
            continue
        if cwd.endswith(" (deleted)"):
            continue
        out[str(pid)] = cwd
    return out

def main():
    a = parse_args()
    if a.pids_file:
        with open(a.pids_file, "r") as f:
            pids = [int(x.strip()) for x in f if x.strip().isdigit()]
    else:
        pids = list_pids_same_mntns(a.container_pid)

    out = set()
    n_read = 0
    for pid in pids:
        try:
            with open(f"/proc/{pid}/maps", "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    n_read += 1
                    parts = line.strip().split()
                    if len(parts) < 6:  # 无路径
                        continue
                    path = parts[-1]
                    if good_path(path):
                        out.add(path)
        except Exception:
            continue

    with open(a.out, "w", encoding="utf-8") as fo:
        for p in sorted(out):
            fo.write(p + "\n")

    print(f"[maps] pids={len(pids)} lines={n_read} out={len(out)} -> {a.out}", file=sys.stderr)

    if a.mapfiles_json:
        mf = collect_mapfiles(pids)
        with open(a.mapfiles_json, "w", encoding="utf-8") as jf:
            json.dump(mf, jf, ensure_ascii=False, indent=2)
        print(f"[mapfiles] entries={len(mf)} -> {a.mapfiles_json}", file=sys.stderr)

    if a.cwd_json:
        cwd_map = collect_cwds(pids)
        with open(a.cwd_json, "w", encoding="utf-8") as jf:
            json.dump(cwd_map, jf, ensure_ascii=False, indent=2)
        print(f"[cwd] entries={len(cwd_map)} -> {a.cwd_json}", file=sys.stderr)

if __name__ == "__main__":
    main()
