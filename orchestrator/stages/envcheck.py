from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class EnvCheckResult:
    process: subprocess.CompletedProcess[str]
    payload: dict


def run_check_env_json(
    *,
    repo_root: Path,
    profile: str,
    image: str = "",
    snapshotter: str = "",
    runtime_probes: bool = True,
) -> EnvCheckResult:
    cmd = [
        "bash",
        str(repo_root / "scripts" / "check_env.sh"),
        "--profile",
        profile,
        "--json",
    ]
    if snapshotter:
        cmd.extend(["--snapshotter", snapshotter])
    if image:
        cmd.extend(["--image", image])
    if not runtime_probes:
        cmd.append("--no-runtime-probes")

    proc = subprocess.run(
        cmd,
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if not stdout:
        details = [f"check_env.sh returned no JSON stdout", f"exit_code={proc.returncode}"]
        if stderr:
            details.append(f"stderr={stderr}")
        raise RuntimeError("; ".join(details))
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        details = [f"check_env.sh emitted invalid JSON: {exc}", f"exit_code={proc.returncode}"]
        if stderr:
            details.append(f"stderr={stderr}")
        details.append(f"stdout={stdout}")
        raise RuntimeError("; ".join(details)) from exc
    return EnvCheckResult(process=proc, payload=payload)


def write_env_check_payload(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
