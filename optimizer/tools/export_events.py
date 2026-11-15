#!/usr/bin/env python3
# export_events.py v5
import argparse
import json
import os
import posixpath
import stat
import sys

DROP_PREFIXES = ("/proc/", "/sys/", "/dev/", "/run/", "/var/run/")
ALLOWED_TYPES = ("OPEN", "MMAP")
AT_FDCWD = -100

def good_path(p):
    if not p or not p.startswith("/"):
        return False
    if p.endswith("/"):
        return False
    return not any(p.startswith(d) for d in DROP_PREFIXES)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("out", help="output jsonl path")
    p.add_argument("--cwd-json", help="pid -> cwd snapshot json")
    p.add_argument("--container-pid", type=int, default=0, help="target container init pid for stat via /proc/<pid>/root")
    p.add_argument("--stats-json", help="optional export stats json path")
    return p.parse_args()


def load_cwds(path):
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {}
    out = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(v, str) and v.startswith("/"):
                out[str(k)] = v
    return out


def resolve_relative_open_path(obj, cwd_map):
    p = str(obj.get("path", "") or "")
    if not p or p.startswith("/"):
        return p, None
    pid = str(obj.get("pid", "") or "")
    tgid = str(obj.get("tgid", "") or "")
    cwd = cwd_map.get(pid) or cwd_map.get(tgid)
    if not cwd:
        return "", None
    # Normalize against the process cwd snapshot taken at the end of sampling.
    resolved = posixpath.normpath(posixpath.join(cwd, p))
    if not resolved.startswith("/"):
        return "", None
    return resolved, cwd


def normalize_dirfd(obj):
    if "dirfd" not in obj:
        return None
    try:
        return int(obj.get("dirfd"))
    except Exception:
        return None


def stat_in_container(container_pid, abs_path):
    if container_pid <= 0 or not abs_path.startswith("/"):
        return None, "invalid"
    host_path = f"/proc/{container_pid}/root{abs_path}"
    try:
        st = os.stat(host_path, follow_symlinks=True)
    except FileNotFoundError:
        return None, "not_found"
    except PermissionError:
        return None, "permission"
    except Exception:
        return None, "error"
    if stat.S_ISREG(st.st_mode):
        path_kind = "regular"
    elif stat.S_ISDIR(st.st_mode):
        path_kind = "directory"
    else:
        path_kind = "other"
    return {
        "dev": int(st.st_dev),
        "ino": int(st.st_ino),
        "size_bytes": int(st.st_size),
        "path_kind": path_kind,
    }, ""

def main():
    args = parse_args()
    out = args.out
    cwd_map = load_cwds(args.cwd_json)
    n_in = n_out = 0
    n_parse_fail = 0
    n_relative_open = 0
    n_relative_resolved = 0
    n_relative_legacy_missing_dirfd_resolved = 0
    n_relative_at_fdcwd_resolved = 0
    n_relative_dirfd_unresolved = 0
    n_relative_unresolved_no_cwd = 0
    n_open_stat_ok = 0
    n_open_stat_fail = 0
    n_open_stat_fail_not_found = 0
    n_open_stat_fail_permission = 0
    n_open_stat_fail_other = 0
    n_open_stat_fail_cwd_resolved = 0
    with open(out, "w", encoding="utf-8") as fo:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            n_in += 1
            raw_index = n_in
            obj = None
            try:
                obj = json.loads(line)
            except Exception:
                try:
                    obj = json.loads(json.loads(line))
                except Exception:
                    n_parse_fail += 1
                    continue
            t = str(obj.get("type","")).upper()
            p = str(obj.get("path",""))
            if t not in ALLOWED_TYPES:
                continue
            if t == "OPEN" and p and not p.startswith("/"):
                n_relative_open += 1
                dirfd = normalize_dirfd(obj)
                resolved, cwd = resolve_relative_open_path(obj, cwd_map)
                if dirfd is not None and dirfd != AT_FDCWD:
                    obj["path_resolution"] = "unresolved_dirfd_relative"
                    obj["path_resolution_dirfd"] = dirfd
                    obj["path_resolution_risk"] = "dirfd_relative_unresolved"
                    obj["path_resolution_detail"] = (
                        "relative OPEN path uses openat/openat2 dirfd; cwd reconstruction is skipped without a resolved dirfd path"
                    )
                    n_relative_dirfd_unresolved += 1
                elif resolved:
                    obj["path_original"] = p
                    obj["path"] = resolved
                    obj["path_resolution"] = "cwd"
                    obj["path_resolution_cwd"] = cwd
                    if dirfd is None:
                        obj["path_resolution_risk"] = "dirfd_unavailable"
                        obj["path_resolution_detail"] = (
                            "relative OPEN path was resolved against cwd snapshot because openat/openat2 dirfd is not captured"
                        )
                        n_relative_legacy_missing_dirfd_resolved += 1
                    else:
                        obj["path_resolution_detail"] = (
                            "relative OPEN path was resolved against cwd snapshot with explicit AT_FDCWD"
                        )
                        n_relative_at_fdcwd_resolved += 1
                    n_relative_resolved += 1
                else:
                    obj["path_resolution"] = "unresolved_relative"
                    n_relative_unresolved_no_cwd += 1
            p = str(obj.get("path", ""))
            if not good_path(p):
                continue
            obj["raw_index"] = raw_index
            if t == "OPEN":
                try:
                    existing_dev = int(obj.get("dev", 0) or 0)
                    existing_ino = int(obj.get("ino", 0) or 0)
                except Exception:
                    existing_dev = 0
                    existing_ino = 0
                if existing_dev > 0 and existing_ino > 0 and not obj.get("devino_source"):
                    obj["devino_source"] = "raw"
                st, stat_error_kind = stat_in_container(args.container_pid, p)
                if st:
                    obj["stat_lookup"] = "ok"
                    if existing_dev <= 0 or existing_ino <= 0:
                        obj["dev"] = st["dev"]
                        obj["ino"] = st["ino"]
                        obj["devino_source"] = "stat"
                    if not obj.get("size_bytes"):
                        obj["size_bytes"] = st["size_bytes"]
                    obj["path_kind"] = st["path_kind"]
                    n_open_stat_ok += 1
                else:
                    obj["stat_lookup"] = "miss"
                    if stat_error_kind:
                        obj["stat_lookup_kind"] = stat_error_kind
                    n_open_stat_fail += 1
                    if obj.get("path_resolution") == "cwd":
                        n_open_stat_fail_cwd_resolved += 1
                    if stat_error_kind == "not_found":
                        n_open_stat_fail_not_found += 1
                    elif stat_error_kind == "permission":
                        n_open_stat_fail_permission += 1
                    else:
                        n_open_stat_fail_other += 1
            fo.write(json.dumps(obj, ensure_ascii=False) + "\n")
            n_out += 1
    if args.stats_json:
        stats_payload = {
            "export_version": 1,
            "input_lines": n_in,
            "parse_fail": n_parse_fail,
            "emitted_events": n_out,
            "relative_open_total": n_relative_open,
            "relative_open_resolved_total": n_relative_resolved,
            "relative_open_resolved_legacy_missing_dirfd": n_relative_legacy_missing_dirfd_resolved,
            "relative_open_resolved_at_fdcwd": n_relative_at_fdcwd_resolved,
            "relative_open_explicit_dirfd_skipped": n_relative_dirfd_unresolved,
            "relative_open_unresolved_no_cwd": n_relative_unresolved_no_cwd,
            "open_stat_ok": n_open_stat_ok,
            "open_stat_fail": n_open_stat_fail,
            "open_stat_fail_not_found": n_open_stat_fail_not_found,
            "open_stat_fail_permission": n_open_stat_fail_permission,
            "open_stat_fail_other": n_open_stat_fail_other,
            "open_stat_fail_cwd_resolved": n_open_stat_fail_cwd_resolved,
        }
        with open(args.stats_json, "w", encoding="utf-8") as fo:
            json.dump(stats_payload, fo, ensure_ascii=False, indent=2)
    print(
        f"[export] in={n_in}, parse_fail={n_parse_fail}, out={n_out}, "
        f"relative_open={n_relative_open}, resolved_relative_open={n_relative_resolved}, "
        f"resolved_relative_open_legacy_missing_dirfd={n_relative_legacy_missing_dirfd_resolved}, "
        f"resolved_relative_open_at_fdcwd={n_relative_at_fdcwd_resolved}, "
        f"dirfd_relative_open_unresolved={n_relative_dirfd_unresolved}, "
        f"relative_open_unresolved_no_cwd={n_relative_unresolved_no_cwd}, "
        f"open_stat_ok={n_open_stat_ok}, open_stat_fail={n_open_stat_fail}, "
        f"open_stat_fail_not_found={n_open_stat_fail_not_found}, "
        f"open_stat_fail_permission={n_open_stat_fail_permission}, "
        f"open_stat_fail_other={n_open_stat_fail_other}, "
        f"open_stat_fail_cwd_resolved={n_open_stat_fail_cwd_resolved} -> {out}",
        file=sys.stderr,
    )

if __name__ == "__main__":
    main()
