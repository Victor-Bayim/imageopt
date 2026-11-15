from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from orchestrator.runtime import list_listening_pids


@dataclass
class ExportEnrichResult:
    events_path: Path
    export_stats_path: Path
    cwd_snapshot_path: Path
    maps_paths_path: Path
    mapfiles_devino_path: Path
    devino_dict_path: Path
    events_enriched_path: Path
    stage_log_path: Path


def _load_runtime_state(path: Path) -> dict:
    if not path.exists():
        raise RuntimeError(f"runtime state is missing: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if all(int(data.get(field) or 0) <= 0 for field in ("target_pid", "root_pid", "container_pid", "live_container_pid")):
        raise RuntimeError(f"runtime state does not contain a valid pid for export-enrich: {path}")
    return data


def _pid_alive(pid: int) -> bool:
    return pid > 0 and os.path.exists(f"/proc/{pid}")


def _local_ready_url_port(ready_url: str | None) -> int | None:
    if not ready_url:
        return None
    parsed = urlparse(ready_url)
    host = str(parsed.hostname or "").strip()
    if host not in {"127.0.0.1", "localhost", "0.0.0.0"}:
        return None
    if parsed.port is not None:
        return int(parsed.port)
    if parsed.scheme == "http":
        return 80
    if parsed.scheme == "https":
        return 443
    return None


def _self_starting_runtime_state(runtime_state: dict) -> bool:
    if bool(runtime_state.get("self_starting_service")):
        return True
    return str(runtime_state.get("workload_profile") or "").strip() == "tfpy-half-plus-two-io1g"


def _runtime_state_pid(runtime_state: dict, field_name: str) -> tuple[int, str]:
    pid = int(runtime_state.get(field_name) or 0)
    source = str(runtime_state.get(f"{field_name}_source") or field_name).strip() or field_name
    return pid, source


def _gated_ready_listener_prefers_container_pid(runtime_state: dict) -> bool:
    return (
        str(runtime_state.get("startup_backend") or "").strip() == "gated_host_launcher_static"
        and str(runtime_state.get("live_container_pid_source") or "").strip() == "ready_listener"
    )


def _resolve_export_container_pid(runtime_state: dict) -> tuple[int, str]:
    target_pid, target_pid_source = _runtime_state_pid(runtime_state, "target_pid")
    root_pid, root_pid_source = _runtime_state_pid(runtime_state, "root_pid")
    live_container_pid = int(runtime_state.get("live_container_pid") or 0)
    container_pid = int(runtime_state.get("container_pid") or 0)

    if _pid_alive(target_pid):
        return target_pid, target_pid_source
    if _pid_alive(root_pid):
        return root_pid, root_pid_source
    if _gated_ready_listener_prefers_container_pid(runtime_state) and _pid_alive(container_pid):
        return container_pid, "container_pid"
    if _pid_alive(live_container_pid):
        return live_container_pid, "live_container_pid"
    if _pid_alive(container_pid):
        return container_pid, "container_pid"

    if _self_starting_runtime_state(runtime_state):
        ready_port = _local_ready_url_port(str(runtime_state.get("ready_url") or "").strip() or None)
        if ready_port is not None:
            for pid in list_listening_pids(ready_port):
                if _pid_alive(pid):
                    return pid, "ready_listener"

    if container_pid > 0:
        raise RuntimeError(f"container pid is no longer alive: {container_pid}")
    raise RuntimeError("no live pid available for export-enrich")


def _run_tool(
    argv: list[str],
    *,
    repo_root: Path,
    stage_log_path: Path,
    stdin=None,
) -> None:
    with stage_log_path.open("a", encoding="utf-8") as log_fh:
        proc = subprocess.run(
            argv,
            cwd=repo_root,
            text=True,
            stdin=stdin,
            stdout=log_fh,
            stderr=log_fh,
            check=False,
        )
    if proc.returncode != 0:
        raise RuntimeError(f"tool failed (exit={proc.returncode}): {' '.join(argv)}")


def run_export_enrich_stage(
    *,
    repo_root: Path,
    work_dir: Path,
) -> ExportEnrichResult:
    runtime_state_path = work_dir / "runtime_state.json"
    runtime_state = _load_runtime_state(runtime_state_path)
    container_pid, container_pid_source = _resolve_export_container_pid(runtime_state)

    raw_events_path = Path(runtime_state.get("raw_events") or work_dir / "raw_events.jsonl")
    if not raw_events_path.exists() or raw_events_path.stat().st_size == 0:
        raise RuntimeError(f"file-sink raw events are missing or empty: {raw_events_path}")

    tools_dir = repo_root / "optimizer" / "tools"
    snapshot_maps = tools_dir / "snapshot_maps.py"
    export_events = tools_dir / "export_events.py"
    build_devino = tools_dir / "build_devino_dict.py"
    enrich_mmap = tools_dir / "enrich_mmap_events.py"

    stage_log_path = work_dir / "export_enrich.log"
    events_path = work_dir / "events.jsonl"
    export_stats_path = work_dir / "export_events_stats.json"
    cwd_snapshot_path = work_dir / "cwd_snapshot.json"
    maps_paths_path = work_dir / "maps_paths.txt"
    mapfiles_devino_path = work_dir / "mapfiles_devino.json"
    devino_dict_path = work_dir / "devino_dict.json"
    events_enriched_path = work_dir / "events_enriched.jsonl"

    stage_log_path.write_text(
        f"[handoff] using pid={container_pid} source={container_pid_source}\n",
        encoding="utf-8",
    )

    _run_tool(
        [
            "python3",
            str(snapshot_maps),
            "--container-pid",
            str(container_pid),
            "--out",
            str(maps_paths_path),
            "--mapfiles-json",
            str(mapfiles_devino_path),
            "--cwd-json",
            str(cwd_snapshot_path),
        ],
        repo_root=repo_root,
        stage_log_path=stage_log_path,
    )

    with raw_events_path.open("r", encoding="utf-8", errors="ignore") as raw_fh:
        _run_tool(
            [
                "python3",
                str(export_events),
                "--cwd-json",
                str(cwd_snapshot_path),
                "--container-pid",
                str(container_pid),
                "--stats-json",
                str(export_stats_path),
                str(events_path),
            ],
            repo_root=repo_root,
            stage_log_path=stage_log_path,
            stdin=raw_fh,
        )

    _run_tool(
        [
            "python3",
            str(build_devino),
            "--container-pid",
            str(container_pid),
            "--events",
            str(events_path),
            "--out",
            str(devino_dict_path),
        ],
        repo_root=repo_root,
        stage_log_path=stage_log_path,
    )

    _run_tool(
        [
            "python3",
            str(enrich_mmap),
            "--raw",
            str(raw_events_path),
            "--events",
            str(events_path),
            "--devino",
            str(devino_dict_path),
            "--maps",
            str(maps_paths_path),
            "--mapfiles",
            str(mapfiles_devino_path),
            "--out",
            str(events_enriched_path),
            "--container-pid",
            str(container_pid),
        ],
        repo_root=repo_root,
        stage_log_path=stage_log_path,
    )

    return ExportEnrichResult(
        events_path=events_path,
        export_stats_path=export_stats_path,
        cwd_snapshot_path=cwd_snapshot_path,
        maps_paths_path=maps_paths_path,
        mapfiles_devino_path=mapfiles_devino_path,
        devino_dict_path=devino_dict_path,
        events_enriched_path=events_enriched_path,
        stage_log_path=stage_log_path,
    )
