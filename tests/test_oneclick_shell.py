import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "oneclick_esgz_optimize_with_llm.sh"


class OneclickShellScriptTests(unittest.TestCase):
    def _write_fake_runtime(
        self,
        tmp_path: Path,
        *,
        include_compiler: bool,
        shell_supported: bool = False,
    ) -> tuple[Path, Path, Path]:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        nerdctl_log = tmp_path / "nerdctl.log"
        sampler_path = tmp_path / "fake_sampler.sh"

        (bin_dir / "nerdctl").write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env bash
                set -euo pipefail
                printf '%s\\n' "$*" >> "{nerdctl_log}"
                if [[ "${{1:-}}" == "run" ]]; then
                  entrypoint_seen=0
                  for arg in "$@"; do
                    if [[ "${{entrypoint_seen}}" -eq 1 && "$arg" == "/bin/sh" ]]; then
                      if [[ "{1 if shell_supported else 0}" == "1" ]]; then
                        exit 0
                      fi
                      exit 1
                    fi
                    if [[ "$arg" == "--entrypoint" ]]; then
                      entrypoint_seen=1
                    else
                      entrypoint_seen=0
                    fi
                  done
                fi
                if [[ "${{1:-}}" == "image" && "${{2:-}}" == "inspect" ]]; then
                  cat <<'EOF'
                [{{"Config": {{"Entrypoint": ["/app/server"], "Cmd": ["--port", "8080"]}}}}]
                EOF
                  exit 0
                fi
                if [[ "${{1:-}}" == "inspect" && "${{2:-}}" == "-f" && "${{3:-}}" == "{{{{.State.Running}}}}" ]]; then
                  echo true
                  exit 0
                fi
                if [[ "${{1:-}}" == "inspect" && "${{2:-}}" == "-f" && "${{3:-}}" == "{{{{.State.Pid}}}}" ]]; then
                  echo 1
                  exit 0
                fi
                if [[ "${{1:-}}" == "ps" ]]; then
                  exit 0
                fi
                case "${{1:-}}" in
                  create|start|stop|rm|logs|exec)
                    exit 0
                    ;;
                esac
                exit 0
                """
            ),
            encoding="utf-8",
        )
        (bin_dir / "nerdctl").chmod(0o755)

        (bin_dir / "curl").write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                for arg in "$@"; do
                  if [[ "$arg" == http://127.0.0.1:*"/metrics" ]]; then
                    cat <<'EOF'
                file_events_total 1
                ringbuf_drops_total 0
                filtered_out_total 0
                sampling_drops_total 0
                file_sink_lines_total 1
                file_sink_bytes_total 128
                file_sink_write_failures_total 0
                EOF
                    exit 0
                  fi
                done
                exit 7
                """
            ),
            encoding="utf-8",
        )
        (bin_dir / "curl").chmod(0o755)

        (bin_dir / "ss").write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\ncat <<'EOF'\nState Recv-Q Send-Q Local Address:Port Peer Address:Port Process\nEOF\n",
            encoding="utf-8",
        )
        (bin_dir / "ss").chmod(0o755)

        if include_compiler:
            (bin_dir / "cc").write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    out=""
                    while [[ $# -gt 0 ]]; do
                      if [[ "$1" == "-o" ]]; then
                        out="$2"
                        shift 2
                        continue
                      fi
                      shift
                    done
                    cat > "$out" <<'EOF'
                    #!/usr/bin/env bash
                    exit 0
                    EOF
                    chmod +x "$out"
                    exit 0
                    """
                ),
                encoding="utf-8",
            )
            (bin_dir / "cc").chmod(0o755)

        sampler_path.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                sink=""
                while [[ $# -gt 0 ]]; do
                  case "$1" in
                    --file-sink)
                      sink="$2"
                      shift 2
                      ;;
                    *)
                      shift
                      ;;
                  esac
                done
                cat > "${sink}" <<'EOF'
                {"type":"OPEN","pid":1,"tgid":1,"path":"/etc/hosts","dev":0,"ino":0,"dirfd":-100,"ts_ns":1}
                EOF
                trap 'exit 0' TERM INT
                while true; do
                  sleep 1
                done
                """
            ),
            encoding="utf-8",
        )
        sampler_path.chmod(0o755)

        return bin_dir, nerdctl_log, sampler_path

    def test_gated_startup_uses_host_launcher_when_image_has_no_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_dir = tmp_path / "work"
            work_dir.mkdir()
            bin_dir, nerdctl_log, sampler_path = self._write_fake_runtime(
                tmp_path,
                include_compiler=True,
            )

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["CPP_SAMPLER_BIN"] = str(sampler_path)
            env["CPP_SAMPLER_CC"] = str(bin_dir / "cc")
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "docker.io/library/distroless-app:latest",
                    "--gated-startup",
                    "--no-env-check",
                    "--no-llm",
                    "--duration",
                    "0",
                    "--min-events",
                    "1",
                    "--min-hits",
                    "1",
                    "--max-mmap-maps-ratio",
                    "1.0",
                    "--allow-low-fidelity",
                    "--work-dir",
                    str(work_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stdout + proc.stderr)
            manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            quality_gate = json.loads((work_dir / "quality_gate.json").read_text(encoding="utf-8"))
            self.assertEqual("gated", manifest["startup"]["used"])
            self.assertEqual("gated_host_launcher_static", manifest["startup"]["backend"])
            self.assertEqual(
                "image lacks /bin/sh; host launcher is the only gated startup backend",
                manifest["startup"]["selection_reason"],
            )
            self.assertEqual("gated_host_launcher_static", quality_gate["startup_backend"])
            self.assertIn("/__cpp_sampler_gate_launcher", nerdctl_log.read_text(encoding="utf-8"))
            self.assertIn("宿主侧 launcher backend（static）", proc.stderr)

    def test_auto_startup_prefers_static_host_launcher_before_shell_when_shell_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_dir = tmp_path / "work"
            work_dir.mkdir()
            bin_dir, nerdctl_log, sampler_path = self._write_fake_runtime(
                tmp_path,
                include_compiler=True,
                shell_supported=True,
            )

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["CPP_SAMPLER_BIN"] = str(sampler_path)
            env["CPP_SAMPLER_CC"] = str(bin_dir / "cc")
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "docker.io/library/shell-app:latest",
                    "--no-env-check",
                    "--no-llm",
                    "--duration",
                    "0",
                    "--min-events",
                    "1",
                    "--min-hits",
                    "1",
                    "--max-mmap-maps-ratio",
                    "1.0",
                    "--allow-low-fidelity",
                    "--work-dir",
                    str(work_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stdout + proc.stderr)
            manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("gated_host_launcher_static", manifest["startup"]["backend"])
            self.assertEqual(
                "auto selected static host launcher before image-shell gating",
                manifest["startup"]["selection_reason"],
            )
            nerdctl_text = nerdctl_log.read_text(encoding="utf-8")
            self.assertIn("/__cpp_sampler_gate_launcher", nerdctl_text)
            self.assertNotIn("/__cpp_sampler_gate.sh", nerdctl_text)
            self.assertIn("自动优先使用宿主侧 launcher backend（static）", proc.stderr)

    def test_auto_startup_keeps_image_shell_when_static_host_launcher_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_dir = tmp_path / "work"
            work_dir.mkdir()
            bin_dir, nerdctl_log, sampler_path = self._write_fake_runtime(
                tmp_path,
                include_compiler=False,
                shell_supported=True,
            )

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["CPP_SAMPLER_BIN"] = str(sampler_path)
            env["CPP_SAMPLER_CC"] = str(tmp_path / "missing-cc")
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "docker.io/library/shell-app:latest",
                    "--no-env-check",
                    "--no-llm",
                    "--duration",
                    "0",
                    "--min-events",
                    "1",
                    "--min-hits",
                    "1",
                    "--max-mmap-maps-ratio",
                    "1.0",
                    "--allow-low-fidelity",
                    "--work-dir",
                    str(work_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stdout + proc.stderr)
            manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("gated_shell", manifest["startup"]["backend"])
            self.assertEqual(
                "auto kept image-shell gating because static host launcher is unavailable",
                manifest["startup"]["selection_reason"],
            )
            nerdctl_text = nerdctl_log.read_text(encoding="utf-8")
            self.assertIn("/__cpp_sampler_gate.sh", nerdctl_text)
            self.assertNotIn("/__cpp_sampler_gate_launcher", nerdctl_text)

    def test_explicit_gated_startup_fails_when_no_shell_and_no_host_launcher_compiler(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_dir = tmp_path / "work"
            work_dir.mkdir()
            bin_dir, _nerdctl_log, sampler_path = self._write_fake_runtime(
                tmp_path,
                include_compiler=False,
            )

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["CPP_SAMPLER_BIN"] = str(sampler_path)
            env["CPP_SAMPLER_CC"] = str(tmp_path / "missing-cc")
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "docker.io/library/distroless-app:latest",
                    "--gated-startup",
                    "--no-env-check",
                    "--no-llm",
                    "--duration",
                    "0",
                    "--work-dir",
                    str(work_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertNotEqual(0, proc.returncode)
            self.assertIn("宿主侧 gated startup launcher 不可用", proc.stderr)
            self.assertIn("gated startup 失败", proc.stderr)
