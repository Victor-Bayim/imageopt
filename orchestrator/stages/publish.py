from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

@dataclass
class PublishResult:
    stage_log_path: Path
    result_path: Path
    src_image: str
    dst_image: str
    namespace: str
    plain_http: bool


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _default_plain_http(ref: str) -> bool:
    head = ref.split("/", 1)[0]
    return head.startswith("127.0.0.1:") or head.startswith("localhost:")


def run_publish_stage(
    *,
    repo_root: Path,
    work_dir: Path,
    src_image: str,
    dst_image: str,
    namespace: str = "default",
    plain_http: bool | None = None,
) -> PublishResult:
    stage_log_path = work_dir / "publish.log"
    result_path = work_dir / "publish_result.json"
    effective_plain_http = _default_plain_http(dst_image) if plain_http is None else bool(plain_http)

    stage_log_path.write_text("", encoding="utf-8")
    tag_cmd = [
        "nerdctl",
        "--namespace",
        namespace,
        "tag",
        src_image,
        dst_image,
    ]
    push_cmd = [
        "nerdctl",
        "--namespace",
        namespace,
    ]
    if effective_plain_http:
        # nerdctl exposes registry transport policy as a global flag rather than
        # a `push` subcommand flag on the versions we support.
        push_cmd.append("--insecure-registry")
    push_cmd.extend(["push", dst_image])

    with stage_log_path.open("a", encoding="utf-8") as log_fh:
        for cmd in (tag_cmd, push_cmd):
            log_fh.write("+ " + " ".join(cmd) + "\n")
            proc = subprocess.run(
                cmd,
                cwd=repo_root,
                text=True,
                stdout=log_fh,
                stderr=log_fh,
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"publish stage failed, see {stage_log_path}")

    _write_json(
        result_path,
        {
            "status": "passed",
            "src_image": src_image,
            "dst_image": dst_image,
            "namespace": namespace,
            "plain_http": effective_plain_http,
            "publish_log": str(stage_log_path),
        },
    )
    return PublishResult(
        stage_log_path=stage_log_path,
        result_path=result_path,
        src_image=src_image,
        dst_image=dst_image,
        namespace=namespace,
        plain_http=effective_plain_http,
    )
