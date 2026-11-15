from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from pathlib import Path


def choose_available_port(start: int, maxscan: int = 400) -> int:
    for port in range(start, start + maxscan):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"no free TCP port found in range [{start}, {start + maxscan})")


def container_name_for(image: str, run_id: str) -> str:
    safe = re.sub(r"[:/]+", "-", image)[:40]
    return f"{safe}-{run_id}"


def run_command(
    argv: list[str],
    *,
    cwd: Path,
    stdout=None,
    stderr=None,
    check: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        text=text,
        stdout=stdout,
        stderr=stderr,
        check=check,
    )


def nerdctl(
    repo_root: Path,
    args: list[str],
    *,
    stdout=None,
    stderr=None,
    check: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess[str]:
    return run_command(
        ["nerdctl", *args],
        cwd=repo_root,
        stdout=stdout,
        stderr=stderr,
        check=check,
        text=text,
    )


def inspect_container_pid(repo_root: Path, container_name: str) -> int:
    proc = nerdctl(
        repo_root,
        ["inspect", "-f", "{{.State.Pid}}", container_name],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    value = (proc.stdout or "").strip()
    if not value.isdigit():
        raise RuntimeError(f"cannot parse container pid from inspect output: {value!r}")
    pid = int(value)
    if pid <= 0:
        raise RuntimeError(f"container pid is not valid: {pid}")
    return pid


def _listening_socket_inodes(port: int) -> set[str]:
    if port <= 0:
        return set()
    inodes: set[str] = set()
    for proc_path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(proc_path, "r", encoding="utf-8") as fh:
                next(fh, None)
                for line in fh:
                    parts = line.split()
                    if len(parts) < 10:
                        continue
                    local_address = parts[1]
                    state = parts[3]
                    inode = parts[9]
                    try:
                        _, port_hex = local_address.rsplit(":", 1)
                        local_port = int(port_hex, 16)
                    except Exception:
                        continue
                    if local_port == port and state == "0A" and inode.isdigit():
                        inodes.add(inode)
        except Exception:
            continue
    return inodes


def list_listening_pids(port: int) -> list[int]:
    socket_inodes = _listening_socket_inodes(port)
    if not socket_inodes:
        return []

    pids: list[int] = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        fd_dir = Path("/proc") / name / "fd"
        try:
            for fd_name in os.listdir(fd_dir):
                try:
                    target = os.readlink(fd_dir / fd_name)
                except Exception:
                    continue
                if not (target.startswith("socket:[") and target.endswith("]")):
                    continue
                inode = target[8:-1]
                if inode in socket_inodes:
                    pids.append(int(name))
                    break
        except Exception:
            continue
    return sorted(set(pids))


def image_supports_shell(repo_root: Path, image: str) -> bool:
    proc = nerdctl(
        repo_root,
        [
            "run",
            "--rm",
            "--pull=never",
            "--net=none",
            "--entrypoint",
            "/bin/sh",
            image,
            "-lc",
            "test -x /bin/sh",
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc.returncode == 0


def image_entry_config(repo_root: Path, image: str) -> tuple[list[str], list[str]]:
    proc = nerdctl(
        repo_root,
        ["image", "inspect", image],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    data = json.loads(proc.stdout)
    obj = data[0] if isinstance(data, list) else data
    cfg = obj.get("Config") or {}
    entry = cfg.get("Entrypoint") or []
    cmd = cfg.get("Cmd") or []
    if isinstance(entry, str):
        entry = [entry]
    if isinstance(cmd, str):
        cmd = [cmd]
    return list(entry), list(cmd)


def image_entry_argv(repo_root: Path, image: str) -> list[str]:
    entry, cmd = image_entry_config(repo_root, image)
    argv = list(entry) + list(cmd)
    if not argv:
        raise RuntimeError("image has empty Entrypoint+Cmd; gated startup unsupported")
    return argv
