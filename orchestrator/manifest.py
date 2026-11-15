from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.product_policy import enforced_supports_payload


VALID_STAGE_STATUSES = {
    "pending",
    "in_progress",
    "passed",
    "failed",
    "paused",
    "skipped",
    "cancelled",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class ManifestStore:
    path: Path
    data: dict[str, Any]

    @classmethod
    def create(
        cls,
        path: Path,
        *,
        run_id: str,
        work_dir: Path,
        image: str,
        startup_mode: str,
        startup_backend_preference: str = "auto",
        llm_enabled: bool,
    ) -> "ManifestStore":
        work_dir = work_dir.resolve()
        data = {
            "manifest_version": 4,
            "run_id": run_id,
            "work_dir": str(work_dir),
            "status": "initialized",
            "control_plane": {
                "engine": "python-orchestrator",
                "engine_version": "0.1.0",
                "legacy_entrypoint": "scripts/oneclick_esgz_optimize_with_llm.sh",
            },
            "product_policy": enforced_supports_payload(),
            "options": {
                "image": image,
                "startup_mode": startup_mode,
                "startup_backend_preference": startup_backend_preference,
                "llm_enabled": llm_enabled,
            },
            "artifacts": {},
            "stages": {},
        }
        store = cls(path=path, data=data)
        store.save()
        return store

    @classmethod
    def load(cls, path: Path) -> "ManifestStore":
        data = json.loads(path.read_text(encoding="utf-8"))
        store = cls(path=path, data=data)
        store._normalize_loaded_data()
        return store

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def stage_begin(self, name: str, summary: str) -> None:
        stage = self.data.setdefault("stages", {}).setdefault(name, {})
        stage["status"] = "in_progress"
        stage["summary"] = summary
        stage["started_at_utc"] = utc_now_iso()
        stage.pop("warnings", None)
        stage.pop("error", None)
        stage.pop("pause_reason", None)
        stage.pop("resume_inputs", None)
        stage.pop("resume_command", None)
        self.data["status"] = f"{name}_in_progress"
        self.save()

    def stage_finish(
        self,
        name: str,
        *,
        status: str,
        summary: str,
        artifacts: dict[str, str] | None = None,
        warnings: list[str] | None = None,
        error: str | None = None,
        pause_reason: str | None = None,
        resume_inputs: dict[str, Any] | None = None,
        resume_command: str | None = None,
    ) -> None:
        if status not in VALID_STAGE_STATUSES:
            raise ValueError(f"unsupported stage status: {status}")
        if status == "paused" and not pause_reason:
            raise ValueError("paused stages require pause_reason")
        stage = self.data.setdefault("stages", {}).setdefault(name, {})
        stage["status"] = status
        stage["summary"] = summary
        stage["finished_at_utc"] = utc_now_iso()
        if artifacts:
            stage_artifacts = stage.setdefault("artifacts", {})
            global_artifacts = self.data.setdefault("artifacts", {})
            for key, value in artifacts.items():
                stage_artifacts[key] = value
                global_artifacts[key] = value
        if warnings:
            stage["warnings"] = list(warnings)
        else:
            stage.pop("warnings", None)
        if error:
            stage["error"] = error
        else:
            stage.pop("error", None)
        if status == "paused":
            stage["pause_reason"] = str(pause_reason)
            if resume_inputs:
                stage["resume_inputs"] = dict(resume_inputs)
            else:
                stage.pop("resume_inputs", None)
            if resume_command:
                stage["resume_command"] = str(resume_command)
            else:
                stage.pop("resume_command", None)
        else:
            stage.pop("pause_reason", None)
            stage.pop("resume_inputs", None)
            stage.pop("resume_command", None)

        self.data["status"] = "ready" if status == "passed" else f"{name}_{status}"
        self.save()

    def _normalize_loaded_data(self) -> None:
        stages = self.data.setdefault("stages", {})
        work_dir = Path(self.data.get("work_dir") or self.path.parent).resolve()
        for name, payload in stages.items():
            if not isinstance(payload, dict):
                continue
            status = str(payload.get("status") or "").strip()
            if status == "awaiting_manual_response":
                payload["status"] = "paused"
                if name == "llm_optimize":
                    payload.setdefault("pause_reason", "awaiting_manual_llm_response")
                    payload.setdefault(
                        "resume_inputs",
                        {
                            "manual_response_file": {
                                "path": str((work_dir / "hints_raw.manual.yaml").resolve()),
                                "format": "json",
                            }
                        },
                    )
                    resume_script = (
                        dict(payload.get("artifacts") or {}).get("resume_manual_web_script")
                        or self.data.setdefault("artifacts", {}).get("resume_manual_web_script")
                    )
                    if resume_script:
                        payload.setdefault("resume_command", f"sudo -E bash {resume_script}")
                    else:
                        payload.setdefault(
                            "resume_command",
                            "python3 scripts/oneclick_product.py llm-optimize "
                            f"--work-dir {work_dir} --manual-response-file {work_dir / 'hints_raw.manual.yaml'}",
                        )
        top_status = str(self.data.get("status") or "").strip()
        if top_status.endswith("_awaiting_manual_response"):
            self.data["status"] = top_status.replace("_awaiting_manual_response", "_paused")
