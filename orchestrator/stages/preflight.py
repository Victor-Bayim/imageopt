from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from orchestrator.policy import evaluate_oneclick_policy
from orchestrator.stages.envcheck import run_check_env_json, write_env_check_payload


@dataclass
class PreflightResult:
    env_check_path: Path
    policy_path: Path
    env_payload: dict
    policy_payload: dict
    exit_code: int


def run_preflight(
    *,
    repo_root: Path,
    work_dir: Path,
    image: str,
    startup_mode: str,
    startup_backend_preference: str = "auto",
    llm_enabled: bool,
    runtime_probes: bool,
) -> PreflightResult:
    env_check_path = work_dir / "env_check.json"
    policy_path = work_dir / "product_policy.json"
    env_result = run_check_env_json(
        repo_root=repo_root,
        profile="oneclick",
        image=image,
        runtime_probes=runtime_probes,
    )
    write_env_check_payload(env_check_path, env_result.payload)
    proc = env_result.process
    env_payload = env_result.payload
    policy_payload = evaluate_oneclick_policy(
        env_payload,
        startup_mode=startup_mode,
        startup_backend_preference=startup_backend_preference,
        llm_enabled=llm_enabled,
    )
    policy_payload["env_check_exit_code"] = proc.returncode
    policy_payload["env_check_stdout"] = proc.stdout.strip()
    policy_payload["env_check_stderr"] = proc.stderr.strip()
    policy_path.write_text(
        json.dumps(policy_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    return PreflightResult(
        env_check_path=env_check_path,
        policy_path=policy_path,
        env_payload=env_payload,
        policy_payload=policy_payload,
        exit_code=0 if policy_payload["ok"] else 1,
    )
