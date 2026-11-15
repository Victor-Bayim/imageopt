import csv
import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "coldstart_bench_strict.sh"


class ColdstartBenchScriptTests(unittest.TestCase):
    def _write_fake_runtime(
        self,
        tmp_path: Path,
        *,
        with_cni_bridge: bool = False,
        logs_payload: str = "",
        logs_to_stderr: bool = False,
        missing_inspect_images: tuple[str, ...] = (),
        initial_local_images: tuple[str, ...] | None = None,
        persistent_local_images: tuple[str, ...] = (),
        content_present_digests: tuple[str, ...] = (),
        debug_state_body: str | None = None,
        busy_ports: tuple[int, ...] = (8888,),
        ready_ok_ports: tuple[int, ...] = (39000,),
        manifest_inspect_payloads: dict[str, dict | str] | None = None,
        rpull_stderr: str = "",
        run_stderr: str = "",
        run_fail_match: str = "",
        run_exit_code: int = 0,
        findmnt_entries: tuple[str, ...] = (),
        stargz_snapshot_dir_entries: tuple[str, ...] = (),
    ) -> tuple[Path, Path, Path]:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        nerdctl_log = tmp_path / "nerdctl.log"
        curl_log = tmp_path / "curl.log"
        ss_log = tmp_path / "ss.log"
        ctr_log = tmp_path / "ctr.log"
        ctr_remote_log = tmp_path / "ctr_remote.log"
        systemctl_log = tmp_path / "systemctl.log"
        journalctl_log = tmp_path / "journalctl.log"
        sudo_log = tmp_path / "sudo.log"
        systemctl_state = tmp_path / "systemctl_active_state.txt"
        findmnt_state = tmp_path / "findmnt_state.txt"
        local_images_state = tmp_path / "local_images_state.txt"
        stargz_root = tmp_path / "stargz-root"
        stargz_snapshots_dir = stargz_root / "snapshotter" / "snapshots"
        stargz_snapshots_dir.mkdir(parents=True, exist_ok=True)
        systemctl_state.write_text("active\n", encoding="utf-8")
        local_images = initial_local_images or ("docker.io/library/app:base", "docker.io/library/app:opt")
        local_images_state.write_text(
            "".join(f"{item}\n" for item in dict.fromkeys(local_images)),
            encoding="utf-8",
        )
        findmnt_state.write_text("".join(f"{item}\n" for item in findmnt_entries), encoding="utf-8")
        for entry in stargz_snapshot_dir_entries:
            (stargz_snapshots_dir / entry / "fs").mkdir(parents=True, exist_ok=True)

        manifest_inspect_payloads = manifest_inspect_payloads or {}
        manifest_cases: list[str] = []
        for ref, payload in manifest_inspect_payloads.items():
            if isinstance(payload, str):
                payload_text = payload
            else:
                payload_text = json.dumps(payload)
            manifest_cases.extend(
                [
                    f'  "{ref}")',
                    "    cat <<'EOF'",
                    payload_text,
                    "EOF",
                    "    exit 0",
                    "    ;;",
                ]
            )
        busy_port_lines = [
            f'printf \'LISTEN 0      128    127.0.0.1:{port}   0.0.0.0:*     users:((\"busy\",pid=1234,fd=7))\\n\''
            for port in busy_ports
        ] or [":"]
        ready_port_checks: list[str] = []
        for port in ready_ok_ports:
            ready_port_checks.extend(
                [
                    f'if [[ "$url" == "http://127.0.0.1:{port}/ready" ]]; then',
                    '  printf \'{"state":"READY"}\\n\'',
                    "  exit 0",
                    "fi",
                ]
            )
        debug_port_checks: list[str] = []
        if debug_state_body is not None:
            for port in ready_ok_ports:
                debug_port_checks.extend(
                    [
                        f'if [[ "$url" == "http://127.0.0.1:{port}/debug/state" ]]; then',
                        '  printf \'%s\\n\' "' + debug_state_body.replace('"', '\\"') + '"',
                        "  exit 0",
                        "fi",
                    ]
                )

        (bin_dir / "ctr-remote").write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    f"printf '%s\\n' \"$*\" >> \"{ctr_remote_log}\"",
                    'if [[ "${1:-}" == "namespaces" && "${2:-}" == "create" ]]; then',
                    "  exit 0",
                    "fi",
                    'if [[ "${1:-}" == "-n" ]]; then',
                    "  shift 2",
                    "fi",
                    'if [[ "${1:-}" == "plugins" && "${2:-}" == "ls" ]]; then',
                    "  cat <<'EOF'",
                    "TYPE ID PLATFORMS STATUS",
                    "io.containerd.snapshotter.v1 overlayfs linux/amd64 ok",
                    "io.containerd.snapshotter.v1 stargz linux/amd64 ok",
                    "EOF",
                    "  exit 0",
                    "fi",
                    'if [[ "${1:-}" == "images" && "${2:-}" == "ls" ]]; then',
                    "  cat <<'EOF'",
                    "REF TYPE DIGEST SIZE PLATFORMS LABELS",
                    "EOF",
                    f'  if [[ -f "{local_images_state}" ]]; then',
                    f'    while IFS= read -r image_ref; do [[ -n "$image_ref" ]] && printf "%s %s %s %s %s %s\\n" "$image_ref" "application/vnd.oci.image.manifest.v1+json" "sha256:test" "1MiB" "linux/amd64" "-"; done < "{local_images_state}"',
                    "  fi",
                    "  exit 0",
                    "fi",
                    'if [[ "${1:-}" == "images" && "${2:-}" == "rpull" ]]; then',
                    f'  if [[ -n "{rpull_stderr}" ]]; then printf "%s\\n" "{rpull_stderr}" >&2; fi',
                    "  exit 0",
                    "fi",
                    'if [[ "${1:-}" == "snapshots" && "${2:-}" == "--snapshotter" && "${4:-}" == "ls" ]]; then',
                    "  cat <<'EOF'",
                    "KEY PARENT KIND",
                    "EOF",
                    '  count="${STRICT_TEST_GLOBAL_STARGZ_SNAPSHOT_RECORDS_REMAINING:-0}"',
                    '  for ((i=0; i<count; i++)); do printf "snap-%s parent-%s active\\n" "$i" "$i"; done',
                    "  exit 0",
                    "fi",
                    'if [[ "${1:-}" == "snapshots" && "${2:-}" == "--snapshotter" && "${4:-}" == "rm" ]]; then',
                    "  exit 0",
                    "fi",
                    "exit 0",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (bin_dir / "ctr-remote").chmod(0o755)

        (bin_dir / "ctr").write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    f"printf '%s\\n' \"$*\" >> \"{ctr_log}\"",
                    "content_digests=(" + " ".join(f'\"{item}\"' for item in content_present_digests) + ")",
                    'if [[ "${1:-}" == "-n" ]]; then',
                    "  shift 2",
                    "fi",
                    'if [[ "${1:-}" == "content" && ( "${2:-}" == "ls" || "${2:-}" == "list" ) ]]; then',
                    "  cat <<'EOF'",
                    "DIGEST SIZE AGE LABELS",
                    "EOF",
                    '  for digest in "${content_digests[@]}"; do',
                    '    printf "%s %s %s %s\\n" "$digest" "1MiB" "1m" "-"',
                    "  done",
                    "  exit 0",
                    "fi",
                    'if [[ "${1:-}" == "content" && "${2:-}" == "get" ]]; then',
                    '  digest="${3:-}"',
                    '  for present in "${content_digests[@]}"; do',
                    '    if [[ "$digest" == "$present" ]]; then',
                    '      printf "blob:%s\\n" "$digest"',
                    "      exit 0",
                    "    fi",
                    "  done",
                    "  exit 1",
                    "fi",
                    'if [[ "${1:-}" == "content" && ( "${2:-}" == "delete" || "${2:-}" == "del" || "${2:-}" == "rm" ) ]]; then',
                    "  exit 0",
                    "fi",
                    "exit 0",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (bin_dir / "ctr").chmod(0o755)

        (bin_dir / "nerdctl").write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    f"printf '%s\\n' \"$*\" >> \"{nerdctl_log}\"",
                    "missing_images=(" + " ".join(f'\"{item}\"' for item in missing_inspect_images) + ")",
                    "persistent_images=(" + " ".join(f'\"{item}\"' for item in persistent_local_images) + ")",
                    'if [[ "${1:-}" == "run" && "${2:-}" == "--help" ]]; then',
                    "  cat <<'EOF'",
                    "Usage: nerdctl run [OPTIONS] IMAGE [COMMAND] [ARG...]",
                    "      --cni string",
                    "EOF",
                    "  exit 0",
                    "fi",
                    'if [[ "${1:-}" == "manifest" && "${2:-}" == "inspect" ]]; then',
                    '  ref="${3:-}"',
                    '  case "$ref" in',
                    *manifest_cases,
                    "  esac",
                    '  echo "manifest inspect missing payload for: $ref" >&2',
                    "  exit 1",
                    "fi",
                    'if [[ "${1:-}" == "--namespace" ]]; then',
                    "  shift 2",
                    "fi",
                    'if [[ "${1:-}" == "image" && "${2:-}" == "inspect" ]]; then',
                    '  image_ref="${3:-}"',
                    '  for missing in "${missing_images[@]}"; do',
                    '    if [[ "$image_ref" == "$missing" ]]; then',
                    '      echo "image not available: $image_ref" >&2',
                    "      exit 1",
                    "    fi",
                    "  done",
                    f'  if [[ -f "{local_images_state}" ]] && grep -Fxq "$image_ref" "{local_images_state}"; then',
                    '    printf "{}\\n"',
                    "    exit 0",
                    "  fi",
                    '  for persistent in "${persistent_images[@]}"; do',
                    '    if [[ "$image_ref" == "$persistent" ]]; then',
                    '      printf "{}\\n"',
                    "      exit 0",
                    "    fi",
                    "  done",
                    '  echo "image not available: $image_ref" >&2',
                    "  exit 1",
                    "fi",
                    'if [[ "${1:-}" == "image" && "${2:-}" == "rm" ]]; then',
                    '  image_ref="${3:-}"',
                    '  for persistent in "${persistent_images[@]}"; do',
                    '    if [[ "$image_ref" == "$persistent" ]]; then',
                    "      exit 0",
                    "    fi",
                    "  done",
                    f'  if [[ -f "{local_images_state}" ]]; then',
                    f'    grep -Fxv "$image_ref" "{local_images_state}" > "{local_images_state}.tmp" || true',
                    f'    mv "{local_images_state}.tmp" "{local_images_state}"',
                    "  fi",
                    "  exit 0",
                    "fi",
                    'case "${1:-}" in',
                    "  run|stop|rm|logs|exec)",
                    '    if [[ "${1:-}" == "run" ]]; then',
                    f'      if [[ -n "{run_stderr}" ]]; then printf "%s\\n" "{run_stderr}" >&2; fi',
                    f'      if [[ -n "{run_fail_match}" && "$*" == *"{run_fail_match}"* ]]; then',
                    f'        exit {run_exit_code}',
                    "      fi",
                    "    fi",
                    f'    if [[ "${{1:-}}" == "logs" && -n "{logs_payload}" ]]; then',
                    "      cat <<'EOF'" + (" >&2" if logs_to_stderr else ""),
                    logs_payload,
                    "noise-line-1",
                    "noise-line-2",
                    "noise-line-3",
                    "EOF",
                    "    fi",
                    "    exit 0",
                    "    ;;",
                    "  *)",
                    "    exit 0",
                    "    ;;",
                    "esac",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (bin_dir / "nerdctl").chmod(0o755)

        (bin_dir / "mount").write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    'count="${STRICT_TEST_STARGZ_RUNTIME_MOUNTS_REMAINING:-0}"',
                    "for ((i=0; i<count; i++)); do",
                    '  printf "containerd-stargz-grpc on /run/stargz/%s type fuse.stargz (rw)\\n" "$i"',
                    "done",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (bin_dir / "mount").chmod(0o755)

        (bin_dir / "findmnt").write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    f'if [[ -s "{findmnt_state}" ]]; then',
                    f'  cat "{findmnt_state}"',
                    "  exit 0",
                    "fi",
                    'if [[ -n "${STRICT_TEST_FINDMNT_OUTPUT:-}" ]]; then',
                    '  printf "%s\\n" "${STRICT_TEST_FINDMNT_OUTPUT}"',
                    "  exit 0",
                    "fi",
                    'count="${STRICT_TEST_STARGZ_RUNTIME_MOUNTS_REMAINING:-0}"',
                    f'snapshots_dir="{stargz_snapshots_dir}"',
                    'for ((i=0; i<count; i++)); do',
                    '  printf "%s/test-%s/fs %s fuse.stargz rw,nosuid,nodev\\n" "$snapshots_dir" "$i" "containerd-stargz-grpc"',
                    "done",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (bin_dir / "findmnt").chmod(0o755)

        (bin_dir / "umount").write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    'if [[ "${STRICT_TEST_UMOUNT_FAILS:-0}" == "1" ]]; then',
                    "  exit 1",
                    "fi",
                    'target="${@: -1}"',
                    'if [[ -z "${target}" ]]; then',
                    "  exit 0",
                    "fi",
                    f'if [[ -f "{findmnt_state}" ]]; then',
                    "  python3 - \"$target\" <<'PY'",
                    "from pathlib import Path",
                    "import sys",
                    f"state_path = Path({str(findmnt_state)!r})",
                    "target = sys.argv[1]",
                    "lines = []",
                    "if state_path.exists():",
                    "    for raw in state_path.read_text(encoding='utf-8').splitlines():",
                    "        if not raw.strip():",
                    "            continue",
                    "        mount_target = raw.split(' ', 1)[0]",
                    "        if mount_target == target:",
                    "            continue",
                    "        lines.append(raw)",
                    "state_path.write_text(('\\n'.join(lines) + ('\\n' if lines else '')), encoding='utf-8')",
                    "PY",
                    "fi",
                    "exit 0",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (bin_dir / "umount").chmod(0o755)

        (bin_dir / "curl").write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    'url=""',
                    'for arg in "$@"; do',
                    '  case "$arg" in',
                    '    http://*|https://*)',
                    '      url="$arg"',
                    "      ;;",
                    "  esac",
                    "done",
                    f"printf '%s\\n' \"$url\" >> \"{curl_log}\"",
                    *ready_port_checks,
                    *debug_port_checks,
                    "exit 7",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (bin_dir / "curl").chmod(0o755)

        (bin_dir / "ss").write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    f"printf '%s\\n' \"$*\" >> \"{ss_log}\"",
                    "cat <<'EOF'",
                    "State Recv-Q Send-Q Local Address:Port Peer Address:Port Process",
                    "EOF",
                    *busy_port_lines,
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (bin_dir / "ss").chmod(0o755)

        (bin_dir / "sudo").write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    f"printf '%s\\n' \"$*\" >> \"{sudo_log}\"",
                    'if [[ "${1:-}" == "-n" && "${2:-}" == "true" ]]; then',
                    "  exit 0",
                    "fi",
                    'if [[ "${1:-}" == "-n" ]]; then',
                    "  shift",
                    "fi",
                    'case "${1:-}" in',
                    "  tee)",
                    "    cat >/dev/null",
                    "    exit 0",
                    "    ;;",
                    "  mkdir|rm|mv|systemctl)",
                    '    if [[ "${1:-}" == "systemctl" && "${STRICT_TEST_EXEC_SUDO_SYSTEMCTL:-0}" == "1" ]]; then',
                    '      exec "$@"',
                    "    fi",
                    '    if [[ "${1:-}" == "rm" && "${STRICT_TEST_ALLOW_SUDO_RM:-0}" == "1" ]]; then',
                    '      exec "$@"',
                    "    fi",
                    "    exit 0",
                    "    ;;",
                    "  bash)",
                    "    exit 0",
                    "    ;;",
                    "esac",
                    'exec "$@"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (bin_dir / "sudo").chmod(0o755)

        (bin_dir / "systemctl").write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    f"printf '%s\\n' \"$*\" >> \"{systemctl_log}\"",
                    f'state_file="{systemctl_state}"',
                    'current_state="$(cat "$state_file" 2>/dev/null || printf "active")"',
                    'if [[ "${1:-}" == "list-unit-files" ]]; then',
                    "  cat <<'EOF'",
                    "UNIT FILE STATE",
                    "containerd.service enabled",
                    "containerd-stargz-grpc.service enabled",
                    "EOF",
                    "  exit 0",
                    "fi",
                    'if [[ "${1:-}" == "stop" ]]; then',
                    '  rc="${STRICT_TEST_SYSTEMCTL_STOP_RC:-0}"',
                    '  next_state="${STRICT_TEST_SYSTEMCTL_AFTER_STOP_STATE:-inactive}"',
                    '  failed_next="${STRICT_TEST_SYSTEMCTL_AFTER_FAILED_STOP_STATE:-$next_state}"',
                    '  if [[ "$rc" -eq 0 ]]; then',
                    '    printf "%s\\n" "$next_state" > "$state_file"',
                    "  else",
                    '    printf "%s\\n" "$failed_next" > "$state_file"',
                    "  fi",
                    '  if [[ -n "${STRICT_TEST_SYSTEMCTL_STOP_OUTPUT:-}" ]]; then printf "%s\\n" "${STRICT_TEST_SYSTEMCTL_STOP_OUTPUT}"; fi',
                    '  exit "$rc"',
                    "fi",
                    'if [[ "${1:-}" == "start" ]]; then',
                    '  rc="${STRICT_TEST_SYSTEMCTL_START_RC:-0}"',
                    '  next_state="${STRICT_TEST_SYSTEMCTL_AFTER_START_STATE:-active}"',
                    '  failed_next="${STRICT_TEST_SYSTEMCTL_AFTER_FAILED_START_STATE:-inactive}"',
                    '  if [[ "$rc" -eq 0 ]]; then',
                    '    printf "%s\\n" "$next_state" > "$state_file"',
                    "  else",
                    '    printf "%s\\n" "$failed_next" > "$state_file"',
                    "  fi",
                    '  if [[ -n "${STRICT_TEST_SYSTEMCTL_START_OUTPUT:-}" ]]; then printf "%s\\n" "${STRICT_TEST_SYSTEMCTL_START_OUTPUT}"; fi',
                    '  exit "$rc"',
                    "fi",
                    'if [[ "${1:-}" == "is-active" ]]; then',
                    '  current_state="$(cat "$state_file" 2>/dev/null || printf "inactive")"',
                    '  if [[ "$current_state" == "active" ]]; then',
                    '    if [[ "${2:-}" != "--quiet" ]]; then printf "active\\n"; fi',
                    "    exit 0",
                    "  fi",
                    '  if [[ "${2:-}" != "--quiet" ]]; then printf "%s\\n" "$current_state"; fi',
                    "  exit 3",
                    "fi",
                    'if [[ "${1:-}" == "show" ]]; then',
                    '  current_state="$(cat "$state_file" 2>/dev/null || printf "inactive")"',
                    '  printf "LoadState=%s\\n" "${STRICT_TEST_SYSTEMCTL_LOAD_STATE:-loaded}"',
                    '  printf "ActiveState=%s\\n" "$current_state"',
                    '  printf "UnitFileState=%s\\n" "${STRICT_TEST_SYSTEMCTL_UNITFILE_STATE:-enabled}"',
                    "  exit 0",
                    "fi",
                    'if [[ "${1:-}" == "status" ]]; then',
                    '  current_state="$(cat "$state_file" 2>/dev/null || printf "inactive")"',
                    '  printf "ActiveState=%s\\n" "$current_state"',
                    "  exit 0",
                    "fi",
                    "exit 0",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (bin_dir / "systemctl").chmod(0o755)

        (bin_dir / "journalctl").write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    f"printf '%s\\n' \"$*\" >> \"{journalctl_log}\"",
                    'if [[ -n "${STRICT_TEST_JOURNALCTL_PAYLOAD:-}" ]]; then',
                    '  printf "%s\\n" "${STRICT_TEST_JOURNALCTL_PAYLOAD}"',
                    "fi",
                    "exit 0",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (bin_dir / "journalctl").chmod(0o755)
        if with_cni_bridge:
            cni_dir = tmp_path / "cni"
            cni_dir.mkdir()
            bridge = cni_dir / "bridge"
            bridge.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            bridge.chmod(0o755)
        return bin_dir, nerdctl_log, curl_log

    def _write_strict_input(self, path: Path, *, base_ref: str, opt_ref: str, base_closure: list[str], opt_closure: list[str]) -> Path:
        return self._write_strict_input_with_resolved_refs(
            path,
            base_requested_ref=base_ref,
            base_resolved_ref=base_ref,
            opt_requested_ref=opt_ref,
            opt_resolved_ref=opt_ref,
            base_closure=base_closure,
            opt_closure=opt_closure,
        )

    def _write_strict_input_with_resolved_refs(
        self,
        path: Path,
        *,
        base_requested_ref: str,
        base_resolved_ref: str,
        opt_requested_ref: str,
        opt_resolved_ref: str,
        base_closure: list[str],
        opt_closure: list[str],
    ) -> Path:
        path.write_text(
            json.dumps(
                {
                    "base": {
                        "requested_ref": base_requested_ref,
                        "resolved_ref": base_resolved_ref,
                        "closure_digests": base_closure,
                    },
                    "optimized": {
                        "requested_ref": opt_requested_ref,
                        "resolved_ref": opt_resolved_ref,
                        "closure_digests": opt_closure,
                    },
                    "shared_closure_digests": sorted(set(base_closure) & set(opt_closure)),
                    "platform": "linux/amd64",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def test_http_ready_probe_remaps_busy_loopback_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir, nerdctl_log, curl_log = self._write_fake_runtime(tmp_path, with_cni_bridge=True)
            csv_path = tmp_path / "out.csv"
            log_dir = tmp_path / "logs"

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["CNI_PATH"] = str(tmp_path / "cni")
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--base",
                    "docker.io/library/app:base",
                    "--opt",
                    "docker.io/library/app:opt",
                    "--runs",
                    "1",
                    "--mode",
                    "ready",
                    "--ready-url",
                    "http://127.0.0.1:8888/ready",
                    "--snap",
                    "overlayfs",
                    "--base-snap",
                    "overlayfs",
                    "--opt-snap",
                    "overlayfs",
                    "--soft-cold",
                    "--no-drop-cache",
                    "--no-clear-stargz",
                    "--csv",
                    str(csv_path),
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stdout + proc.stderr)
            csv_text = csv_path.read_text(encoding="utf-8")
            self.assertIn(",ok,", csv_text)
            self.assertIn("宿主端口 8888 已被占用", proc.stderr)
            nerdctl_args = nerdctl_log.read_text(encoding="utf-8")
            self.assertIn("-p 127.0.0.1:39000:8888", nerdctl_args)
            self.assertNotIn("--network=host", nerdctl_args)
            self.assertIn("http://127.0.0.1:39000/ready", curl_log.read_text(encoding="utf-8"))

    def test_ready_mode_collects_debug_state_metrics_without_failing_benchmark(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir, _nerdctl_log, curl_log = self._write_fake_runtime(
                tmp_path,
                with_cni_bridge=True,
                debug_state_body='{"bootstrap_duration_ms":786,"hot_files_loaded":128,"hot_bytes_loaded":268435456,"loaded_paths":["/srv/layout/a","/srv/layout/b"]}',
            )
            csv_path = tmp_path / "out.csv"
            log_dir = tmp_path / "logs"

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["CNI_PATH"] = str(tmp_path / "cni")
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--base",
                    "docker.io/library/app:base",
                    "--opt",
                    "docker.io/library/app:opt",
                    "--runs",
                    "1",
                    "--mode",
                    "ready",
                    "--ready-url",
                    "http://127.0.0.1:8888/ready",
                    "--debug-state-url",
                    "http://127.0.0.1:8888/debug/state",
                    "--snap",
                    "overlayfs",
                    "--base-snap",
                    "overlayfs",
                    "--opt-snap",
                    "overlayfs",
                    "--soft-cold",
                    "--no-drop-cache",
                    "--no-clear-stargz",
                    "--csv",
                    str(csv_path),
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stdout + proc.stderr)
            rows = list(csv.DictReader(csv_path.read_text(encoding="utf-8").splitlines()))
            self.assertEqual("786", rows[0]["service_bootstrap_ms"])
            self.assertEqual("128", rows[0]["hot_files_loaded"])
            self.assertEqual("268435456", rows[0]["hot_bytes_loaded"])
            self.assertEqual("2", rows[0]["loaded_paths_count"])
            curl_calls = curl_log.read_text(encoding="utf-8")
            self.assertIn("http://127.0.0.1:39000/ready", curl_calls)
            self.assertIn("http://127.0.0.1:39000/debug/state", curl_calls)

    def test_debug_state_failure_only_records_warning_and_null_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir, _nerdctl_log, _curl_log = self._write_fake_runtime(tmp_path, with_cni_bridge=True)
            csv_path = tmp_path / "out.csv"
            log_dir = tmp_path / "logs"

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["CNI_PATH"] = str(tmp_path / "cni")
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--base",
                    "docker.io/library/app:base",
                    "--opt",
                    "docker.io/library/app:opt",
                    "--runs",
                    "1",
                    "--mode",
                    "ready",
                    "--ready-url",
                    "http://127.0.0.1:8888/ready",
                    "--debug-state-url",
                    "http://127.0.0.1:8888/debug/state",
                    "--snap",
                    "overlayfs",
                    "--base-snap",
                    "overlayfs",
                    "--opt-snap",
                    "overlayfs",
                    "--soft-cold",
                    "--no-drop-cache",
                    "--no-clear-stargz",
                    "--csv",
                    str(csv_path),
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stdout + proc.stderr)
            rows = list(csv.DictReader(csv_path.read_text(encoding="utf-8").splitlines()))
            self.assertEqual("-1", rows[0]["service_bootstrap_ms"])
            self.assertEqual("-1", rows[0]["hot_files_loaded"])
            self.assertEqual("-1", rows[0]["hot_bytes_loaded"])
            self.assertEqual("-1", rows[0]["loaded_paths_count"])
            self.assertIn("debug/state fetch failed", proc.stderr)

    def test_ready_mode_csv_header_includes_pull_run_wait_fields_and_run_uses_pull_never(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir, nerdctl_log, _curl_log = self._write_fake_runtime(tmp_path, with_cni_bridge=True)
            csv_path = tmp_path / "out.csv"
            log_dir = tmp_path / "logs"

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["CNI_PATH"] = str(tmp_path / "cni")
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--base",
                    "docker.io/library/app:base",
                    "--opt",
                    "docker.io/library/app:opt",
                    "--runs",
                    "1",
                    "--mode",
                    "ready",
                    "--ready-url",
                    "http://127.0.0.1:8888/ready",
                    "--comparison-mode",
                    "raw-vs-estargz",
                    "--pull-policy",
                    "always",
                    "--snap",
                    "stargz",
                    "--base-snap",
                    "overlayfs",
                    "--opt-snap",
                    "stargz",
                    "--soft-cold",
                    "--no-drop-cache",
                    "--no-clear-stargz",
                    "--csv",
                    str(csv_path),
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stdout + proc.stderr)
            header = csv_path.read_text(encoding="utf-8").splitlines()[0]
            self.assertIn("pull_ms", header)
            self.assertIn("run_ms", header)
            self.assertIn("ready_wait_ms", header)
            self.assertIn("end_to_end_ready_ms", header)
            self.assertIn("service_bootstrap_ms", header)
            self.assertIn("hot_files_loaded", header)
            self.assertIn("hot_bytes_loaded", header)
            self.assertIn("loaded_paths_count", header)
            nerdctl_args = nerdctl_log.read_text(encoding="utf-8")
            self.assertIn("--namespace default pull --snapshotter overlayfs --platform linux/amd64 docker.io/library/app:base", nerdctl_args)
            self.assertIn("--namespace default pull --snapshotter stargz --platform linux/amd64 docker.io/library/app:opt", nerdctl_args)
            self.assertIn("run -d --name", nerdctl_args)
            self.assertIn("--pull=never", nerdctl_args)

    def test_raw_full_vs_estargz_lazy_skips_optimized_full_pull_and_uses_pull_always_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            opt_manifest_digest = "sha256:" + ("a" * 64)
            opt_config_digest = "sha256:" + ("b" * 64)
            opt_large_layer_digest = "sha256:" + ("c" * 64)
            manifest_payloads = {
                "docker.io/library/app:opt": {
                    "Descriptor": {"digest": opt_manifest_digest},
                    "OCIManifest": {
                        "schemaVersion": 2,
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "config": {
                            "mediaType": "application/vnd.oci.image.config.v1+json",
                            "digest": opt_config_digest,
                            "size": 123,
                        },
                        "layers": [
                            {
                                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                                "digest": opt_large_layer_digest,
                                "size": 8 * 1024 * 1024 * 1024,
                            }
                        ],
                    },
                }
            }
            bin_dir, nerdctl_log, _curl_log = self._write_fake_runtime(
                tmp_path,
                manifest_inspect_payloads=manifest_payloads,
            )
            csv_path = tmp_path / "out.csv"
            log_dir = tmp_path / "logs"

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--base",
                    "docker.io/library/app:base",
                    "--opt",
                    "docker.io/library/app:opt",
                    "--runs",
                    "1",
                    "--mode",
                    "ready",
                    "--ready-exec",
                    "test -f /tmp/ready",
                    "--comparison-mode",
                    "raw-full-vs-estargz-lazy",
                    "--pull-policy",
                    "always",
                    "--snap",
                    "stargz",
                    "--base-snap",
                    "overlayfs",
                    "--opt-snap",
                    "stargz",
                    "--soft-cold",
                    "--no-drop-cache",
                    "--no-clear-stargz",
                    "--csv",
                    str(csv_path),
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stdout + proc.stderr)
            rows = list(csv.DictReader(csv_path.read_text(encoding="utf-8").splitlines()))
            self.assertEqual("baseline", rows[0]["label"])
            self.assertEqual("optimized", rows[1]["label"])
            self.assertEqual("end_to_end_ready_ms", rows[0]["ready_ms_semantics"])
            self.assertEqual("lazy_ready_ms", rows[1]["ready_ms_semantics"])
            self.assertEqual("0", rows[1]["pull_ms"])
            self.assertIn("lazy_prepare_ms", rows[1])
            self.assertIn("lazy_ready_ms", rows[1])
            nerdctl_args = nerdctl_log.read_text(encoding="utf-8")
            self.assertIn(
                "--namespace default pull --snapshotter overlayfs --platform linux/amd64 docker.io/library/app:base",
                nerdctl_args,
            )
            self.assertNotIn(
                "--namespace default pull --snapshotter stargz --platform linux/amd64 docker.io/library/app:opt",
                nerdctl_args,
            )
            ctr_remote_args = (tmp_path / "ctr_remote.log").read_text(encoding="utf-8")
            self.assertIn(
                f"-n default images rpull --snapshotter stargz docker.io/library/app@{opt_manifest_digest}",
                ctr_remote_args,
            )
            self.assertIn("run -d --name", nerdctl_args)
            self.assertIn("--pull=never --network=none --snapshotter overlayfs --platform linux/amd64 -e MODEL_NAME=", nerdctl_args)
            self.assertIn("--pull=never --network=none --snapshotter stargz --platform linux/amd64 -e MODEL_NAME=", nerdctl_args)
            self.assertIn(f"docker.io/library/app@{opt_manifest_digest}", nerdctl_args)

    def test_raw_full_vs_estargz_lazy_fails_row_when_run_log_shows_full_pull(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            opt_manifest_digest = "sha256:" + ("d" * 64)
            opt_config_digest = "sha256:" + ("e" * 64)
            opt_large_layer_digest = "sha256:" + ("f" * 64)
            manifest_payloads = {
                "docker.io/library/app:opt": {
                    "Descriptor": {"digest": opt_manifest_digest},
                    "OCIManifest": {
                        "schemaVersion": 2,
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "config": {
                            "mediaType": "application/vnd.oci.image.config.v1+json",
                            "digest": opt_config_digest,
                            "size": 123,
                        },
                        "layers": [
                            {
                                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                                "digest": opt_large_layer_digest,
                                "size": 8 * 1024 * 1024 * 1024,
                            }
                        ],
                    },
                }
            }
            bin_dir, _nerdctl_log, _curl_log = self._write_fake_runtime(
                tmp_path,
                manifest_inspect_payloads=manifest_payloads,
                run_stderr="elapsed: 74.9s total: 8.1 Gi",
            )
            csv_path = tmp_path / "out.csv"
            log_dir = tmp_path / "logs"

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--base",
                    "docker.io/library/app:base",
                    "--opt",
                    "docker.io/library/app:opt",
                    "--runs",
                    "1",
                    "--mode",
                    "ready",
                    "--ready-exec",
                    "test -f /tmp/ready",
                    "--comparison-mode",
                    "raw-full-vs-estargz-lazy",
                    "--pull-policy",
                    "always",
                    "--snap",
                    "stargz",
                    "--base-snap",
                    "overlayfs",
                    "--opt-snap",
                    "stargz",
                    "--soft-cold",
                    "--no-drop-cache",
                    "--no-clear-stargz",
                    "--csv",
                    str(csv_path),
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stdout + proc.stderr)
            rows = list(csv.DictReader(csv_path.read_text(encoding="utf-8").splitlines()))
            optimized_row = rows[1]
            self.assertEqual("fail", optimized_row["status"])
            self.assertEqual("optimized-lazy-path-performed-full-pull", optimized_row["err"])
            self.assertEqual("1", optimized_row["full_pull_evidence"])
            self.assertEqual("1", optimized_row["lazy_guard_failed"])

    def test_raw_full_vs_estargz_lazy_runs_three_rounds_without_overwriting_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            opt_manifest_digest = "sha256:" + ("1" * 64)
            opt_config_digest = "sha256:" + ("2" * 64)
            opt_large_layer_digest = "sha256:" + ("3" * 64)
            manifest_payloads = {
                "docker.io/library/app:opt": {
                    "Descriptor": {"digest": opt_manifest_digest},
                    "OCIManifest": {
                        "schemaVersion": 2,
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "config": {
                            "mediaType": "application/vnd.oci.image.config.v1+json",
                            "digest": opt_config_digest,
                            "size": 123,
                        },
                        "layers": [
                            {
                                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                                "digest": opt_large_layer_digest,
                                "size": 8 * 1024 * 1024 * 1024,
                            }
                        ],
                    },
                }
            }
            bin_dir, nerdctl_log, _curl_log = self._write_fake_runtime(
                tmp_path,
                manifest_inspect_payloads=manifest_payloads,
            )
            csv_path = tmp_path / "out.csv"
            log_dir = tmp_path / "logs"

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--base",
                    "docker.io/library/app:base",
                    "--opt",
                    "docker.io/library/app:opt",
                    "--runs",
                    "3",
                    "--mode",
                    "ready",
                    "--ready-exec",
                    "test -f /tmp/ready",
                    "--comparison-mode",
                    "raw-full-vs-estargz-lazy",
                    "--pull-policy",
                    "always",
                    "--snap",
                    "stargz",
                    "--base-snap",
                    "overlayfs",
                    "--opt-snap",
                    "stargz",
                    "--soft-cold",
                    "--no-drop-cache",
                    "--no-clear-stargz",
                    "--csv",
                    str(csv_path),
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stdout + proc.stderr)
            nerdctl_args = nerdctl_log.read_text(encoding="utf-8")
            self.assertIn("--name cs-baseline-1", nerdctl_args)
            self.assertIn("--name cs-baseline-2", nerdctl_args)
            self.assertIn("--name cs-baseline-3", nerdctl_args)
            self.assertIn("--name cs-optimized-1", nerdctl_args)
            self.assertIn("--name cs-optimized-2", nerdctl_args)
            self.assertIn("--name cs-optimized-3", nerdctl_args)
            for idx in (1, 2, 3):
                self.assertTrue((log_dir / f"cs-optimized-{idx}.lazy.trace.log").exists())
                self.assertTrue((log_dir / f"cs-optimized-{idx}.runerr").exists())
                self.assertTrue((log_dir / f"cs-optimized-{idx}.run.cmd").exists())
                trace_text = (log_dir / f"cs-optimized-{idx}.lazy.trace.log").read_text(encoding="utf-8")
                self.assertIn(f"run_index={idx}", trace_text)

    def test_raw_full_vs_estargz_lazy_triple_runs_baseline_then_two_lazy_labels_with_isolated_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            unopt_manifest_digest = "sha256:" + ("1" * 64)
            unopt_config_digest = "sha256:" + ("2" * 64)
            unopt_large_layer_digest = "sha256:" + ("3" * 64)
            opt_manifest_digest = "sha256:" + ("4" * 64)
            opt_config_digest = "sha256:" + ("5" * 64)
            opt_large_layer_digest = "sha256:" + ("6" * 64)
            manifest_payloads = {
                "docker.io/library/app:unopt": {
                    "Descriptor": {"digest": unopt_manifest_digest},
                    "OCIManifest": {
                        "schemaVersion": 2,
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "config": {
                            "mediaType": "application/vnd.oci.image.config.v1+json",
                            "digest": unopt_config_digest,
                            "size": 123,
                        },
                        "layers": [
                            {
                                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                                "digest": unopt_large_layer_digest,
                                "size": 8 * 1024 * 1024 * 1024,
                            }
                        ],
                    },
                },
                "docker.io/library/app:opt": {
                    "Descriptor": {"digest": opt_manifest_digest},
                    "OCIManifest": {
                        "schemaVersion": 2,
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "config": {
                            "mediaType": "application/vnd.oci.image.config.v1+json",
                            "digest": opt_config_digest,
                            "size": 123,
                        },
                        "layers": [
                            {
                                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                                "digest": opt_large_layer_digest,
                                "size": 8 * 1024 * 1024 * 1024,
                            }
                        ],
                    },
                },
            }
            bin_dir, nerdctl_log, _curl_log = self._write_fake_runtime(
                tmp_path,
                manifest_inspect_payloads=manifest_payloads,
            )
            csv_path = tmp_path / "out.csv"
            log_dir = tmp_path / "logs"

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--base",
                    "docker.io/library/app:base",
                    "--unopt-image",
                    "docker.io/library/app:unopt",
                    "--opt",
                    "docker.io/library/app:opt",
                    "--runs",
                    "1",
                    "--mode",
                    "ready",
                    "--ready-exec",
                    "test -f /tmp/ready",
                    "--comparison-mode",
                    "raw-full-vs-estargz-lazy-triple",
                    "--pull-policy",
                    "always",
                    "--snap",
                    "stargz",
                    "--base-snap",
                    "overlayfs",
                    "--opt-snap",
                    "stargz",
                    "--soft-cold",
                    "--no-drop-cache",
                    "--no-clear-stargz",
                    "--csv",
                    str(csv_path),
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stdout + proc.stderr)
            rows = list(csv.DictReader(csv_path.read_text(encoding="utf-8").splitlines()))
            self.assertEqual(["baseline", "unoptimized_lazy", "optimized"], [row["label"] for row in rows])
            self.assertEqual("end_to_end_ready_ms", rows[0]["ready_ms_semantics"])
            self.assertEqual("lazy_ready_ms", rows[1]["ready_ms_semantics"])
            self.assertEqual("lazy_ready_ms", rows[2]["ready_ms_semantics"])
            self.assertEqual("0", rows[1]["pull_ms"])
            self.assertEqual("0", rows[2]["pull_ms"])

            nerdctl_args = nerdctl_log.read_text(encoding="utf-8")
            self.assertIn(
                "--namespace default pull --snapshotter overlayfs --platform linux/amd64 docker.io/library/app:base",
                nerdctl_args,
            )
            self.assertNotIn(
                "--namespace default pull --snapshotter stargz --platform linux/amd64 docker.io/library/app:unopt",
                nerdctl_args,
            )
            self.assertNotIn(
                "--namespace default pull --snapshotter stargz --platform linux/amd64 docker.io/library/app:opt",
                nerdctl_args,
            )
            self.assertIn("--name cs-unoptimized_lazy-1", nerdctl_args)
            self.assertIn("--name cs-optimized-1", nerdctl_args)

            ctr_remote_args = (tmp_path / "ctr_remote.log").read_text(encoding="utf-8")
            self.assertIn(
                f"-n default images rpull --snapshotter stargz docker.io/library/app@{unopt_manifest_digest}",
                ctr_remote_args,
            )
            self.assertIn(
                f"-n default images rpull --snapshotter stargz docker.io/library/app@{opt_manifest_digest}",
                ctr_remote_args,
            )

            self.assertTrue((log_dir / "cs-unoptimized_lazy-1.lazy.trace.log").exists())
            self.assertTrue((log_dir / "cs-unoptimized_lazy-1.lazy.prepare.log").exists())
            self.assertTrue((log_dir / "cs-unoptimized_lazy-1.runerr").exists())
            self.assertTrue((log_dir / "cs-optimized-1.lazy.trace.log").exists())
            self.assertTrue((log_dir / "cs-optimized-1.lazy.prepare.log").exists())
            self.assertTrue((log_dir / "cs-optimized-1.runerr").exists())

    def test_raw_full_vs_estargz_lazy_triple_hard_reset_runs_before_each_lazy_rpull(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            unopt_manifest_digest = "sha256:" + ("7" * 64)
            unopt_config_digest = "sha256:" + ("8" * 64)
            unopt_large_layer_digest = "sha256:" + ("9" * 64)
            opt_manifest_digest = "sha256:" + ("a" * 64)
            opt_config_digest = "sha256:" + ("b" * 64)
            opt_large_layer_digest = "sha256:" + ("c" * 64)
            manifest_payloads = {
                "docker.io/library/app:unopt": {
                    "Descriptor": {"digest": unopt_manifest_digest},
                    "OCIManifest": {
                        "schemaVersion": 2,
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "config": {
                            "mediaType": "application/vnd.oci.image.config.v1+json",
                            "digest": unopt_config_digest,
                            "size": 123,
                        },
                        "layers": [
                            {
                                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                                "digest": unopt_large_layer_digest,
                                "size": 8 * 1024 * 1024 * 1024,
                            }
                        ],
                    },
                },
                "docker.io/library/app:opt": {
                    "Descriptor": {"digest": opt_manifest_digest},
                    "OCIManifest": {
                        "schemaVersion": 2,
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "config": {
                            "mediaType": "application/vnd.oci.image.config.v1+json",
                            "digest": opt_config_digest,
                            "size": 123,
                        },
                        "layers": [
                            {
                                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                                "digest": opt_large_layer_digest,
                                "size": 8 * 1024 * 1024 * 1024,
                            }
                        ],
                    },
                },
            }
            bin_dir, _nerdctl_log, _curl_log = self._write_fake_runtime(
                tmp_path,
                manifest_inspect_payloads=manifest_payloads,
            )
            csv_path = tmp_path / "out.csv"
            log_dir = tmp_path / "logs"

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["STRICT_TEST_EXEC_SUDO_SYSTEMCTL"] = "1"
            env["STRICT_TEST_ALLOW_SUDO_RM"] = "1"
            env["LAZY_STARGZ_RESET_MODE"] = "hard-reset"
            env["STARGZ_GRPC_ROOT_DIR"] = str(tmp_path / "stargz-root")
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--base",
                    "docker.io/library/app:base",
                    "--unopt-image",
                    "docker.io/library/app:unopt",
                    "--opt",
                    "docker.io/library/app:opt",
                    "--runs",
                    "1",
                    "--mode",
                    "ready",
                    "--ready-exec",
                    "test -f /tmp/ready",
                    "--comparison-mode",
                    "raw-full-vs-estargz-lazy-triple",
                    "--pull-policy",
                    "always",
                    "--snap",
                    "stargz",
                    "--base-snap",
                    "overlayfs",
                    "--opt-snap",
                    "stargz",
                    "--soft-cold",
                    "--no-drop-cache",
                    "--no-clear-stargz",
                    "--csv",
                    str(csv_path),
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stdout + proc.stderr)
            ctr_remote_log = (tmp_path / "ctr_remote.log").read_text(encoding="utf-8")
            sudo_log = (tmp_path / "sudo.log").read_text(encoding="utf-8")
            systemctl_log = (tmp_path / "systemctl.log").read_text(encoding="utf-8")

            self.assertEqual(2, ctr_remote_log.count("images rpull --snapshotter stargz"))
            self.assertEqual(2, sudo_log.count("systemctl stop containerd-stargz-grpc.service"))
            self.assertEqual(2, sudo_log.count("systemctl start containerd-stargz-grpc.service"))
            self.assertEqual(2, sudo_log.count(f"rm -rf {tmp_path / 'stargz-root' / 'snapshotter'}"))
            self.assertEqual(4, systemctl_log.count("is-active --quiet containerd-stargz-grpc.service"))
            self.assertIn("lazy_reset_mode=hard-reset", (log_dir / "cs-unoptimized_lazy-1.lazy.trace.log").read_text(encoding="utf-8"))
            self.assertIn("lazy_reset_mode=hard-reset", (log_dir / "cs-optimized-1.lazy.trace.log").read_text(encoding="utf-8"))

    def test_raw_full_vs_estargz_lazy_triple_marks_unoptimized_full_pull_guard_with_label_specific_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            unopt_manifest_digest = "sha256:" + ("d" * 64)
            unopt_config_digest = "sha256:" + ("e" * 64)
            unopt_large_layer_digest = "sha256:" + ("f" * 64)
            opt_manifest_digest = "sha256:" + ("0" * 64)
            opt_config_digest = "sha256:" + ("1" * 64)
            opt_large_layer_digest = "sha256:" + ("2" * 64)
            manifest_payloads = {
                "docker.io/library/app:unopt": {
                    "Descriptor": {"digest": unopt_manifest_digest},
                    "OCIManifest": {
                        "schemaVersion": 2,
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "config": {
                            "mediaType": "application/vnd.oci.image.config.v1+json",
                            "digest": unopt_config_digest,
                            "size": 123,
                        },
                        "layers": [
                            {
                                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                                "digest": unopt_large_layer_digest,
                                "size": 8 * 1024 * 1024 * 1024,
                            }
                        ],
                    },
                },
                "docker.io/library/app:opt": {
                    "Descriptor": {"digest": opt_manifest_digest},
                    "OCIManifest": {
                        "schemaVersion": 2,
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "config": {
                            "mediaType": "application/vnd.oci.image.config.v1+json",
                            "digest": opt_config_digest,
                            "size": 123,
                        },
                        "layers": [
                            {
                                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                                "digest": opt_large_layer_digest,
                                "size": 8 * 1024 * 1024 * 1024,
                            }
                        ],
                    },
                },
            }
            bin_dir, _nerdctl_log, _curl_log = self._write_fake_runtime(
                tmp_path,
                manifest_inspect_payloads=manifest_payloads,
                content_present_digests=(unopt_large_layer_digest,),
            )
            csv_path = tmp_path / "out.csv"
            log_dir = tmp_path / "logs"

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--base",
                    "docker.io/library/app:base",
                    "--unopt-image",
                    "docker.io/library/app:unopt",
                    "--opt",
                    "docker.io/library/app:opt",
                    "--runs",
                    "1",
                    "--mode",
                    "ready",
                    "--ready-exec",
                    "test -f /tmp/ready",
                    "--comparison-mode",
                    "raw-full-vs-estargz-lazy-triple",
                    "--pull-policy",
                    "always",
                    "--snap",
                    "stargz",
                    "--base-snap",
                    "overlayfs",
                    "--opt-snap",
                    "stargz",
                    "--soft-cold",
                    "--no-drop-cache",
                    "--no-clear-stargz",
                    "--csv",
                    str(csv_path),
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stdout + proc.stderr)
            rows = list(csv.DictReader(csv_path.read_text(encoding="utf-8").splitlines()))
            unoptimized_row = rows[1]
            optimized_row = rows[2]
            self.assertEqual("unoptimized_lazy", unoptimized_row["label"])
            self.assertEqual("fail", unoptimized_row["status"])
            self.assertEqual("unoptimized_lazy-lazy-path-performed-full-pull", unoptimized_row["err"])
            self.assertEqual("1", unoptimized_row["full_pull_evidence"])
            self.assertEqual("1", unoptimized_row["lazy_guard_failed"])
            self.assertEqual("optimized", optimized_row["label"])
            self.assertEqual("ok", optimized_row["status"])

    def test_raw_full_vs_estargz_lazy_marks_entrypoint_missing_with_failure_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            opt_manifest_digest = "sha256:" + ("4" * 64)
            opt_config_digest = "sha256:" + ("5" * 64)
            opt_large_layer_digest = "sha256:" + ("6" * 64)
            manifest_payloads = {
                "docker.io/library/app:opt": {
                    "Descriptor": {"digest": opt_manifest_digest},
                    "OCIManifest": {
                        "schemaVersion": 2,
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "config": {
                            "mediaType": "application/vnd.oci.image.config.v1+json",
                            "digest": opt_config_digest,
                            "size": 123,
                        },
                        "layers": [
                            {
                                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                                "digest": opt_large_layer_digest,
                                "size": 8 * 1024 * 1024 * 1024,
                            }
                        ],
                    },
                }
            }
            run_stderr = 'exec: "/usr/local/bin/server": stat /usr/local/bin/server: no such file or directory'
            bin_dir, _nerdctl_log, _curl_log = self._write_fake_runtime(
                tmp_path,
                manifest_inspect_payloads=manifest_payloads,
                run_stderr=run_stderr,
                run_fail_match=f"docker.io/library/app@{opt_manifest_digest}",
                run_exit_code=1,
            )
            csv_path = tmp_path / "out.csv"
            log_dir = tmp_path / "logs"

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["STRICT_TEST_JOURNALCTL_PAYLOAD"] = "stargz journal line"
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--base",
                    "docker.io/library/app:base",
                    "--opt",
                    "docker.io/library/app:opt",
                    "--runs",
                    "1",
                    "--mode",
                    "ready",
                    "--ready-exec",
                    "test -f /tmp/ready",
                    "--comparison-mode",
                    "raw-full-vs-estargz-lazy",
                    "--pull-policy",
                    "always",
                    "--snap",
                    "stargz",
                    "--base-snap",
                    "overlayfs",
                    "--opt-snap",
                    "stargz",
                    "--soft-cold",
                    "--no-drop-cache",
                    "--no-clear-stargz",
                    "--csv",
                    str(csv_path),
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stdout + proc.stderr)
            rows = list(csv.DictReader(csv_path.read_text(encoding="utf-8").splitlines()))
            optimized_row = rows[1]
            self.assertEqual("fail", optimized_row["status"])
            self.assertEqual("stargz-rootfs-entrypoint-missing", optimized_row["err"])
            self.assertIn(
                "stat /usr/local/bin/server: no such file or directory",
                (log_dir / "cs-optimized-1.runerr").read_text(encoding="utf-8"),
            )
            self.assertIn("nerdctl --namespace default run", (log_dir / "cs-optimized-1.run.cmd").read_text(encoding="utf-8"))
            self.assertTrue((log_dir / "cs-optimized-1.lazy.prepare.log").exists())
            self.assertTrue((log_dir / "cs-optimized-1.lazy.findmnt.log").exists())
            self.assertTrue((log_dir / "cs-optimized-1.lazy.snapshots.log").exists())
            self.assertIn("stargz journal line", (log_dir / "cs-optimized-1.lazy.journal.log").read_text(encoding="utf-8"))

    def test_lazy_reset_hard_reset_stops_starts_is_active_and_removes_snapshotter_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            opt_manifest_digest = "sha256:" + ("7" * 64)
            opt_config_digest = "sha256:" + ("8" * 64)
            opt_large_layer_digest = "sha256:" + ("9" * 64)
            manifest_payloads = {
                "docker.io/library/app:opt": {
                    "Descriptor": {"digest": opt_manifest_digest},
                    "OCIManifest": {
                        "schemaVersion": 2,
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "config": {
                            "mediaType": "application/vnd.oci.image.config.v1+json",
                            "digest": opt_config_digest,
                            "size": 123,
                        },
                        "layers": [
                            {
                                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                                "digest": opt_large_layer_digest,
                                "size": 8 * 1024 * 1024 * 1024,
                            }
                        ],
                    },
                }
            }
            bin_dir, _nerdctl_log, _curl_log = self._write_fake_runtime(
                tmp_path,
                manifest_inspect_payloads=manifest_payloads,
            )
            csv_path = tmp_path / "out.csv"
            log_dir = tmp_path / "logs"

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["STRICT_TEST_EXEC_SUDO_SYSTEMCTL"] = "1"
            env["STRICT_TEST_ALLOW_SUDO_RM"] = "1"
            env["LAZY_STARGZ_RESET_MODE"] = "hard-reset"
            env["STARGZ_GRPC_ROOT_DIR"] = str(tmp_path / "stargz-root")
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--base",
                    "docker.io/library/app:base",
                    "--opt",
                    "docker.io/library/app:opt",
                    "--runs",
                    "2",
                    "--mode",
                    "ready",
                    "--ready-exec",
                    "test -f /tmp/ready",
                    "--comparison-mode",
                    "raw-full-vs-estargz-lazy",
                    "--pull-policy",
                    "always",
                    "--snap",
                    "stargz",
                    "--base-snap",
                    "overlayfs",
                    "--opt-snap",
                    "stargz",
                    "--soft-cold",
                    "--no-drop-cache",
                    "--no-clear-stargz",
                    "--csv",
                    str(csv_path),
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stdout + proc.stderr)
            systemctl_log = (tmp_path / "systemctl.log").read_text(encoding="utf-8")
            sudo_log = (tmp_path / "sudo.log").read_text(encoding="utf-8")
            self.assertNotIn("list-unit-files", systemctl_log)
            self.assertIn("stop containerd-stargz-grpc.service", systemctl_log)
            self.assertIn("start containerd-stargz-grpc.service", systemctl_log)
            self.assertIn("is-active --quiet containerd-stargz-grpc.service", systemctl_log)
            self.assertIn(f"rm -rf {tmp_path / 'stargz-root' / 'snapshotter'}", sudo_log)

    def test_raw_full_vs_estargz_lazy_runs_three_rounds_hard_reset_before_each_rpull(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            opt_manifest_digest = "sha256:" + ("a" * 64)
            opt_config_digest = "sha256:" + ("b" * 64)
            opt_large_layer_digest = "sha256:" + ("c" * 64)
            manifest_payloads = {
                "docker.io/library/app:opt": {
                    "Descriptor": {"digest": opt_manifest_digest},
                    "OCIManifest": {
                        "schemaVersion": 2,
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "config": {
                            "mediaType": "application/vnd.oci.image.config.v1+json",
                            "digest": opt_config_digest,
                            "size": 123,
                        },
                        "layers": [
                            {
                                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                                "digest": opt_large_layer_digest,
                                "size": 8 * 1024 * 1024 * 1024,
                            }
                        ],
                    },
                }
            }
            bin_dir, _nerdctl_log, _curl_log = self._write_fake_runtime(
                tmp_path,
                manifest_inspect_payloads=manifest_payloads,
                stargz_snapshot_dir_entries=("1", "2"),
            )
            csv_path = tmp_path / "out.csv"
            log_dir = tmp_path / "logs"

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["STRICT_TEST_EXEC_SUDO_SYSTEMCTL"] = "1"
            env["STRICT_TEST_ALLOW_SUDO_RM"] = "1"
            env["LAZY_STARGZ_RESET_MODE"] = "hard-reset"
            env["STARGZ_GRPC_ROOT_DIR"] = str(tmp_path / "stargz-root")
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--base",
                    "docker.io/library/app:base",
                    "--opt",
                    "docker.io/library/app:opt",
                    "--runs",
                    "3",
                    "--mode",
                    "ready",
                    "--ready-exec",
                    "test -f /tmp/ready",
                    "--comparison-mode",
                    "raw-full-vs-estargz-lazy",
                    "--pull-policy",
                    "always",
                    "--snap",
                    "stargz",
                    "--base-snap",
                    "overlayfs",
                    "--opt-snap",
                    "stargz",
                    "--soft-cold",
                    "--no-drop-cache",
                    "--no-clear-stargz",
                    "--csv",
                    str(csv_path),
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stdout + proc.stderr)
            ctr_remote_log = (tmp_path / "ctr_remote.log").read_text(encoding="utf-8")
            sudo_log = (tmp_path / "sudo.log").read_text(encoding="utf-8")
            systemctl_log = (tmp_path / "systemctl.log").read_text(encoding="utf-8")
            self.assertEqual(3, ctr_remote_log.count("images rpull --snapshotter stargz"))
            self.assertEqual(3, sudo_log.count("systemctl stop containerd-stargz-grpc.service"))
            self.assertEqual(3, sudo_log.count("systemctl start containerd-stargz-grpc.service"))
            self.assertEqual(3, sudo_log.count(f"rm -rf {tmp_path / 'stargz-root' / 'snapshotter'}"))
            self.assertEqual(6, systemctl_log.count("is-active --quiet containerd-stargz-grpc.service"))
            self.assertNotIn("list-unit-files", systemctl_log)

    def test_lazy_hard_reset_continues_when_stop_fails_but_service_is_inactive(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            opt_manifest_digest = "sha256:" + ("d" * 64)
            opt_config_digest = "sha256:" + ("e" * 64)
            opt_large_layer_digest = "sha256:" + ("f" * 64)
            manifest_payloads = {
                "docker.io/library/app:opt": {
                    "Descriptor": {"digest": opt_manifest_digest},
                    "OCIManifest": {
                        "schemaVersion": 2,
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "config": {
                            "mediaType": "application/vnd.oci.image.config.v1+json",
                            "digest": opt_config_digest,
                            "size": 123,
                        },
                        "layers": [
                            {
                                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                                "digest": opt_large_layer_digest,
                                "size": 8 * 1024 * 1024 * 1024,
                            }
                        ],
                    },
                }
            }
            bin_dir, _nerdctl_log, _curl_log = self._write_fake_runtime(
                tmp_path,
                manifest_inspect_payloads=manifest_payloads,
            )
            csv_path = tmp_path / "out.csv"
            log_dir = tmp_path / "logs"

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["STRICT_TEST_EXEC_SUDO_SYSTEMCTL"] = "1"
            env["STRICT_TEST_ALLOW_SUDO_RM"] = "1"
            env["STRICT_TEST_SYSTEMCTL_STOP_RC"] = "1"
            env["STRICT_TEST_SYSTEMCTL_AFTER_FAILED_STOP_STATE"] = "inactive"
            env["LAZY_STARGZ_RESET_MODE"] = "hard-reset"
            env["STARGZ_GRPC_ROOT_DIR"] = str(tmp_path / "stargz-root")
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--base",
                    "docker.io/library/app:base",
                    "--opt",
                    "docker.io/library/app:opt",
                    "--runs",
                    "1",
                    "--mode",
                    "ready",
                    "--ready-exec",
                    "test -f /tmp/ready",
                    "--comparison-mode",
                    "raw-full-vs-estargz-lazy",
                    "--pull-policy",
                    "always",
                    "--snap",
                    "stargz",
                    "--base-snap",
                    "overlayfs",
                    "--opt-snap",
                    "stargz",
                    "--soft-cold",
                    "--no-drop-cache",
                    "--no-clear-stargz",
                    "--csv",
                    str(csv_path),
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stdout + proc.stderr)
            rows = list(csv.DictReader(csv_path.read_text(encoding="utf-8").splitlines()))
            self.assertEqual("ok", rows[1]["status"])

    def test_lazy_hard_reset_fails_only_when_start_or_post_start_is_active_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            opt_manifest_digest = "sha256:" + ("1" * 64)
            opt_config_digest = "sha256:" + ("2" * 64)
            opt_large_layer_digest = "sha256:" + ("3" * 64)
            manifest_payloads = {
                "docker.io/library/app:opt": {
                    "Descriptor": {"digest": opt_manifest_digest},
                    "OCIManifest": {
                        "schemaVersion": 2,
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "config": {
                            "mediaType": "application/vnd.oci.image.config.v1+json",
                            "digest": opt_config_digest,
                            "size": 123,
                        },
                        "layers": [
                            {
                                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                                "digest": opt_large_layer_digest,
                                "size": 8 * 1024 * 1024 * 1024,
                            }
                        ],
                    },
                }
            }
            bin_dir, _nerdctl_log, _curl_log = self._write_fake_runtime(
                tmp_path,
                manifest_inspect_payloads=manifest_payloads,
            )
            csv_path = tmp_path / "out.csv"
            log_dir = tmp_path / "logs"

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["STRICT_TEST_EXEC_SUDO_SYSTEMCTL"] = "1"
            env["STRICT_TEST_ALLOW_SUDO_RM"] = "1"
            env["STRICT_TEST_SYSTEMCTL_AFTER_START_STATE"] = "inactive"
            env["LAZY_STARGZ_RESET_MODE"] = "hard-reset"
            env["STARGZ_GRPC_ROOT_DIR"] = str(tmp_path / "stargz-root")
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--base",
                    "docker.io/library/app:base",
                    "--opt",
                    "docker.io/library/app:opt",
                    "--runs",
                    "1",
                    "--mode",
                    "ready",
                    "--ready-exec",
                    "test -f /tmp/ready",
                    "--comparison-mode",
                    "raw-full-vs-estargz-lazy",
                    "--pull-policy",
                    "always",
                    "--snap",
                    "stargz",
                    "--base-snap",
                    "overlayfs",
                    "--opt-snap",
                    "stargz",
                    "--soft-cold",
                    "--no-drop-cache",
                    "--no-clear-stargz",
                    "--csv",
                    str(csv_path),
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stdout + proc.stderr)
            rows = list(csv.DictReader(csv_path.read_text(encoding="utf-8").splitlines()))
            self.assertEqual("fail", rows[1]["status"])
            self.assertEqual("optimized-lazy-reset-failed", rows[1]["err"])
            trace_text = (log_dir / "cs-optimized-1.lazy.trace.log").read_text(encoding="utf-8")
            self.assertIn("lazy_reset_service=containerd-stargz-grpc.service", trace_text)
            self.assertIn("lazy_reset_show_log=", trace_text)

    def test_busy_loopback_port_without_cni_fails_with_explicit_network_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir, _nerdctl_log, _curl_log = self._write_fake_runtime(tmp_path, with_cni_bridge=False)
            csv_path = tmp_path / "out.csv"
            log_dir = tmp_path / "logs"
            (tmp_path / "cni").mkdir()

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["CNI_PATH"] = str(tmp_path / "cni")
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--base",
                    "docker.io/library/app:base",
                    "--opt",
                    "docker.io/library/app:opt",
                    "--runs",
                    "1",
                    "--mode",
                    "ready",
                    "--ready-url",
                    "tcp://127.0.0.1:8888",
                    "--snap",
                    "overlayfs",
                    "--base-snap",
                    "overlayfs",
                    "--opt-snap",
                    "overlayfs",
                    "--soft-cold",
                    "--no-drop-cache",
                    "--no-clear-stargz",
                    "--csv",
                    str(csv_path),
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stdout + proc.stderr)
            csv_text = csv_path.read_text(encoding="utf-8")
            self.assertIn("network-prep-failed", csv_text)
            self.assertIn("CNI bridge plugin is unavailable", proc.stderr)

    def test_busy_published_port_uses_next_available_high_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir, nerdctl_log, curl_log = self._write_fake_runtime(
                tmp_path,
                with_cni_bridge=True,
                busy_ports=(39000,),
                ready_ok_ports=(39001,),
            )
            csv_path = tmp_path / "out.csv"
            log_dir = tmp_path / "logs"

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["CNI_PATH"] = str(tmp_path / "cni")
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--base",
                    "docker.io/library/app:base",
                    "--opt",
                    "docker.io/library/app:opt",
                    "--runs",
                    "1",
                    "--mode",
                    "ready",
                    "--ready-url",
                    "http://127.0.0.1:39000/ready",
                    "--snap",
                    "overlayfs",
                    "--base-snap",
                    "overlayfs",
                    "--opt-snap",
                    "overlayfs",
                    "--soft-cold",
                    "--no-drop-cache",
                    "--no-clear-stargz",
                    "--csv",
                    str(csv_path),
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stdout + proc.stderr)
            nerdctl_args = nerdctl_log.read_text(encoding="utf-8")
            self.assertIn("-p 127.0.0.1:39001:39000", nerdctl_args)
            self.assertNotIn("-p 127.0.0.1:39000:39000", nerdctl_args)
            self.assertIn("http://127.0.0.1:39001/ready", curl_log.read_text(encoding="utf-8"))

    def test_missing_optimized_image_fails_before_benchmark_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir, nerdctl_log, _curl_log = self._write_fake_runtime(
                tmp_path,
                missing_inspect_images=("docker.io/library/app:opt",),
            )
            csv_path = tmp_path / "out.csv"
            log_dir = tmp_path / "logs"

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--base",
                    "docker.io/library/app:base",
                    "--opt",
                    "docker.io/library/app:opt",
                    "--runs",
                    "10",
                    "--mode",
                    "ready",
                    "--ready-exec",
                    "test -f /tmp/ready",
                    "--snap",
                    "overlayfs",
                    "--base-snap",
                    "overlayfs",
                    "--opt-snap",
                    "overlayfs",
                    "--soft-cold",
                    "--no-drop-cache",
                    "--no-clear-stargz",
                    "--csv",
                    str(csv_path),
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(3, proc.returncode, msg=proc.stdout + proc.stderr)
            self.assertIn("optimized image is not available to nerdctl in namespace default", proc.stderr)
            self.assertIn("nerdctl --namespace default image inspect docker.io/library/app:opt", proc.stderr)
            nerdctl_args = nerdctl_log.read_text(encoding="utf-8")
            self.assertIn("--namespace default image inspect docker.io/library/app:base", nerdctl_args)
            self.assertIn("--namespace default image inspect docker.io/library/app:opt", nerdctl_args)
            self.assertNotIn("--name cs-optimized-", nerdctl_args)

    def test_exec_ready_probe_avoids_host_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir, nerdctl_log, _curl_log = self._write_fake_runtime(tmp_path)
            csv_path = tmp_path / "out.csv"
            log_dir = tmp_path / "logs"

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--base",
                    "docker.io/library/app:base",
                    "--opt",
                    "docker.io/library/app:opt",
                    "--runs",
                    "1",
                    "--mode",
                    "ready",
                    "--ready-exec",
                    "test -f /tmp/ready",
                    "--ready-shell",
                    "bash",
                    "--snap",
                    "overlayfs",
                    "--base-snap",
                    "overlayfs",
                    "--opt-snap",
                    "overlayfs",
                    "--soft-cold",
                    "--no-drop-cache",
                    "--no-clear-stargz",
                    "--csv",
                    str(csv_path),
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stdout + proc.stderr)
            csv_text = csv_path.read_text(encoding="utf-8")
            self.assertIn(",ok,", csv_text)
            nerdctl_args = nerdctl_log.read_text(encoding="utf-8")
            self.assertNotIn("--network=host", nerdctl_args)
            self.assertIn("--network=none", nerdctl_args)
            self.assertNotIn(" -p ", nerdctl_args)

    def test_registry_cold_strict_rejects_non_stargz_snapshotters_in_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir, _nerdctl_log, _curl_log = self._write_fake_runtime(tmp_path)
            csv_path = tmp_path / "out.csv"
            log_dir = tmp_path / "logs"
            strict_input = self._write_strict_input(
                tmp_path / "strict_input.json",
                base_ref="docker.io/library/app@sha256:" + ("1" * 64),
                opt_ref="docker.io/library/app@sha256:" + ("2" * 64),
                base_closure=["sha256:" + ("1" * 64)],
                opt_closure=["sha256:" + ("2" * 64)],
            )

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--base",
                    "docker.io/library/app:base",
                    "--opt",
                    "docker.io/library/app:opt",
                    "--runs",
                    "1",
                    "--mode",
                    "ready",
                    "--ready-exec",
                    "test -f /tmp/ready",
                    "--cold-mode",
                    "registry-cold-strict",
                    "--pull-policy",
                    "always",
                    "--drop-local-image-ref",
                    "--snap",
                    "stargz",
                    "--base-snap",
                    "overlayfs",
                    "--opt-snap",
                    "stargz",
                    "--strict-input-json",
                    str(strict_input),
                    "--csv",
                    str(csv_path),
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertNotEqual(0, proc.returncode)
            self.assertIn("registry-cold-strict requires stargz", proc.stderr)

    def test_registry_cold_strict_fails_closed_when_content_store_proof_is_non_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            digest_a = "sha256:" + ("a" * 64)
            digest_b = "sha256:" + ("b" * 64)
            bin_dir, _nerdctl_log, _curl_log = self._write_fake_runtime(
                tmp_path,
                content_present_digests=(digest_a, digest_b),
            )
            csv_path = tmp_path / "out.csv"
            proof_path = tmp_path / "proof.json"
            log_dir = tmp_path / "logs"
            strict_input = self._write_strict_input(
                tmp_path / "strict_input.json",
                base_ref=f"docker.io/library/app@{digest_a}",
                opt_ref=f"docker.io/library/app@{digest_b}",
                base_closure=[digest_a],
                opt_closure=[digest_b],
            )

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--base",
                    "docker.io/library/app:base",
                    "--opt",
                    "docker.io/library/app:opt",
                    "--runs",
                    "1",
                    "--mode",
                    "ready",
                    "--ready-exec",
                    "test -f /tmp/ready",
                    "--cold-mode",
                    "registry-cold-strict",
                    "--pull-policy",
                    "always",
                    "--drop-local-image-ref",
                    "--snap",
                    "stargz",
                    "--base-snap",
                    "stargz",
                    "--opt-snap",
                    "stargz",
                    "--strict-input-json",
                    str(strict_input),
                    "--proof-json",
                    str(proof_path),
                    "--csv",
                    str(csv_path),
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertNotEqual(0, proc.returncode)
            self.assertIn("strict proof failed", proc.stderr)
            proof_payload = json.loads(proof_path.read_text(encoding="utf-8"))
            self.assertFalse(proof_payload["strict_proof_zero"])
            self.assertGreater(proof_payload["required_zero"]["content_store_digests_remaining_base"], 0)
            self.assertGreater(proof_payload["required_zero"]["content_store_digests_remaining_opt"], 0)

    def test_registry_cold_strict_cleanup_removes_base_and_optimized_requested_and_resolved_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            base_requested = "docker.io/library/app:esgz-v2"
            base_resolved = "docker.io/library/app@sha256:" + ("1" * 64)
            opt_requested = "docker.io/library/app:optimized-v2"
            opt_resolved = "docker.io/library/app@sha256:" + ("2" * 64)
            bin_dir, nerdctl_log, _curl_log = self._write_fake_runtime(
                tmp_path,
                initial_local_images=(
                    "docker.io/library/app:base",
                    "docker.io/library/app:opt",
                    base_requested,
                    base_resolved,
                    opt_requested,
                    opt_resolved,
                ),
            )
            csv_path = tmp_path / "out.csv"
            proof_path = tmp_path / "proof.json"
            log_dir = tmp_path / "logs"
            strict_input = self._write_strict_input_with_resolved_refs(
                tmp_path / "strict_input.json",
                base_requested_ref=base_requested,
                base_resolved_ref=base_resolved,
                opt_requested_ref=opt_requested,
                opt_resolved_ref=opt_resolved,
                base_closure=["sha256:" + ("a" * 64)],
                opt_closure=["sha256:" + ("b" * 64)],
            )

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["STRICT_TEST_GLOBAL_STARGZ_SNAPSHOT_RECORDS_REMAINING"] = "273"
            env["STRICT_TEST_GLOBAL_STARGZ_LOCAL_SNAPSHOT_ENTRIES_REMAINING"] = "273"
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--base",
                    "docker.io/library/app:base",
                    "--opt",
                    "docker.io/library/app:opt",
                    "--runs",
                    "1",
                    "--mode",
                    "ready",
                    "--ready-exec",
                    "test -f /tmp/ready",
                    "--cold-mode",
                    "registry-cold-strict",
                    "--pull-policy",
                    "always",
                    "--drop-local-image-ref",
                    "--snap",
                    "stargz",
                    "--base-snap",
                    "stargz",
                    "--opt-snap",
                    "stargz",
                    "--strict-input-json",
                    str(strict_input),
                    "--proof-json",
                    str(proof_path),
                    "--csv",
                    str(csv_path),
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stdout + proc.stderr)
            nerdctl_args = nerdctl_log.read_text(encoding="utf-8")
            self.assertIn(f"--namespace default image rm {base_requested}", nerdctl_args)
            self.assertIn(f"--namespace default image rm {base_resolved}", nerdctl_args)
            self.assertIn(f"--namespace default image rm {opt_requested}", nerdctl_args)
            self.assertIn(f"--namespace default image rm {opt_resolved}", nerdctl_args)
            proof_payload = json.loads(proof_path.read_text(encoding="utf-8"))
            self.assertTrue(proof_payload["strict_proof_zero"])
            self.assertEqual(0, proof_payload["required_zero"]["local_image_refs_remaining_base"])
            self.assertEqual(0, proof_payload["required_zero"]["local_image_refs_remaining_opt"])
            self.assertTrue(proof_payload["required_true"]["base_local_ref_deleted"])
            self.assertTrue(proof_payload["required_true"]["opt_local_ref_deleted"])
            self.assertNotIn("stargz_snapshot_records_remaining", proof_payload["required_zero"])
            self.assertNotIn("stargz_local_snapshot_entries_remaining", proof_payload["required_zero"])
            self.assertEqual(273, proof_payload["diagnostic"]["global_stargz_snapshot_records_remaining"])
            self.assertEqual(273, proof_payload["diagnostic"]["global_stargz_local_snapshot_entries_remaining"])

    def test_registry_cold_strict_fails_when_optimized_local_ref_remains(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            opt_requested = "docker.io/library/app:optimized-v2"
            opt_resolved = "docker.io/library/app@sha256:" + ("2" * 64)
            bin_dir, _nerdctl_log, _curl_log = self._write_fake_runtime(
                tmp_path,
                initial_local_images=(
                    "docker.io/library/app:base",
                    "docker.io/library/app:opt",
                    opt_requested,
                    opt_resolved,
                ),
                persistent_local_images=(opt_requested, opt_resolved),
            )
            csv_path = tmp_path / "out.csv"
            proof_path = tmp_path / "proof.json"
            log_dir = tmp_path / "logs"
            strict_input = self._write_strict_input_with_resolved_refs(
                tmp_path / "strict_input.json",
                base_requested_ref="docker.io/library/app:esgz-v2",
                base_resolved_ref="docker.io/library/app@sha256:" + ("1" * 64),
                opt_requested_ref=opt_requested,
                opt_resolved_ref=opt_resolved,
                base_closure=["sha256:" + ("a" * 64)],
                opt_closure=["sha256:" + ("b" * 64)],
            )

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--base",
                    "docker.io/library/app:base",
                    "--opt",
                    "docker.io/library/app:opt",
                    "--runs",
                    "1",
                    "--mode",
                    "ready",
                    "--ready-exec",
                    "test -f /tmp/ready",
                    "--cold-mode",
                    "registry-cold-strict",
                    "--pull-policy",
                    "always",
                    "--drop-local-image-ref",
                    "--snap",
                    "stargz",
                    "--base-snap",
                    "stargz",
                    "--opt-snap",
                    "stargz",
                    "--strict-input-json",
                    str(strict_input),
                    "--proof-json",
                    str(proof_path),
                    "--csv",
                    str(csv_path),
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertNotEqual(0, proc.returncode)
            self.assertIn("strict proof failed", proc.stderr)
            proof_payload = json.loads(proof_path.read_text(encoding="utf-8"))
            self.assertFalse(proof_payload["strict_proof_zero"])
            self.assertGreater(proof_payload["required_zero"]["local_image_refs_remaining_opt"], 0)
            self.assertFalse(proof_payload["required_true"]["opt_local_ref_deleted"])

    def test_registry_cold_strict_fails_when_cache_or_runtime_mounts_remain(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir, _nerdctl_log, _curl_log = self._write_fake_runtime(tmp_path)
            csv_path = tmp_path / "out.csv"
            proof_path = tmp_path / "proof.json"
            log_dir = tmp_path / "logs"
            strict_input = self._write_strict_input(
                tmp_path / "strict_input.json",
                base_ref="docker.io/library/app@sha256:" + ("1" * 64),
                opt_ref="docker.io/library/app@sha256:" + ("2" * 64),
                base_closure=["sha256:" + ("a" * 64)],
                opt_closure=["sha256:" + ("b" * 64)],
            )

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["STRICT_TEST_STARGZ_RUNTIME_MOUNTS_REMAINING"] = "2"
            env["STRICT_TEST_STARGZ_CACHE_ENTRIES_REMAINING"] = "1"
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--base",
                    "docker.io/library/app:base",
                    "--opt",
                    "docker.io/library/app:opt",
                    "--runs",
                    "1",
                    "--mode",
                    "ready",
                    "--ready-exec",
                    "test -f /tmp/ready",
                    "--cold-mode",
                    "registry-cold-strict",
                    "--pull-policy",
                    "always",
                    "--drop-local-image-ref",
                    "--snap",
                    "stargz",
                    "--base-snap",
                    "stargz",
                    "--opt-snap",
                    "stargz",
                    "--strict-input-json",
                    str(strict_input),
                    "--proof-json",
                    str(proof_path),
                    "--csv",
                    str(csv_path),
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertNotEqual(0, proc.returncode)
            self.assertIn("strict proof failed", proc.stderr)
            proof_payload = json.loads(proof_path.read_text(encoding="utf-8"))
            self.assertFalse(proof_payload["strict_proof_zero"])
            self.assertEqual(2, proof_payload["required_zero"]["stargz_runtime_mounts_remaining"])
            self.assertEqual(1, proof_payload["required_zero"]["stargz_cache_entries_remaining"])

    def test_raw_full_vs_estargz_lazy_strict_cleanup_clears_preexisting_stargz_state_before_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            stargz_root = tmp_path / "stargz-root"
            snapshots_dir = stargz_root / "snapshotter" / "snapshots"
            findmnt_entries = (
                f"{snapshots_dir / '10' / 'fs'} containerd-stargz-grpc fuse.stargz rw,nosuid,nodev",
                f"{snapshots_dir / '11' / 'fs'} containerd-stargz-grpc fuse.stargz rw,nosuid,nodev",
            )
            bin_dir, _nerdctl_log, _curl_log = self._write_fake_runtime(
                tmp_path,
                findmnt_entries=findmnt_entries,
                stargz_snapshot_dir_entries=("10", "11"),
            )
            csv_path = tmp_path / "out.csv"
            proof_path = tmp_path / "proof.json"
            log_dir = tmp_path / "logs"
            strict_input = self._write_strict_input(
                tmp_path / "strict_input.json",
                base_ref="docker.io/library/app@sha256:" + ("1" * 64),
                opt_ref="docker.io/library/app@sha256:" + ("2" * 64),
                base_closure=["sha256:" + ("a" * 64)],
                opt_closure=["sha256:" + ("b" * 64)],
            )

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["STARGZ_GRPC_ROOT_DIR"] = str(stargz_root)
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--base",
                    "docker.io/library/app:base",
                    "--opt",
                    "docker.io/library/app:opt",
                    "--runs",
                    "1",
                    "--mode",
                    "ready",
                    "--ready-exec",
                    "test -f /tmp/ready",
                    "--comparison-mode",
                    "raw-full-vs-estargz-lazy",
                    "--cold-mode",
                    "registry-cold-strict",
                    "--pull-policy",
                    "always",
                    "--drop-local-image-ref",
                    "--base-snap",
                    "overlayfs",
                    "--opt-snap",
                    "stargz",
                    "--strict-input-json",
                    str(strict_input),
                    "--proof-json",
                    str(proof_path),
                    "--csv",
                    str(csv_path),
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stdout + proc.stderr)
            self.assertIn("precheck/env_check/smoke", proc.stderr)
            proof_payload = json.loads(proof_path.read_text(encoding="utf-8"))
            self.assertTrue(proof_payload["strict_proof_zero"])
            self.assertEqual(0, proof_payload["required_zero"]["stargz_runtime_mounts_remaining"])
            self.assertEqual([], proof_payload["diagnostic"]["stargz_runtime_mount_entries_remaining"])
            self.assertEqual(2, len(list(snapshots_dir.glob("*"))))
            self.assertEqual(
                2,
                len(proof_payload["diagnostic"]["diagnostic_only_stargz_local_snapshot_dirs_remaining_after_cleanup"]),
            )

    def test_registry_cold_strict_uses_findmnt_runtime_mounts_not_snapshot_dir_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            stargz_root = tmp_path / "stargz-root"
            bin_dir, _nerdctl_log, _curl_log = self._write_fake_runtime(
                tmp_path,
                stargz_snapshot_dir_entries=("20", "21"),
            )
            csv_path = tmp_path / "out.csv"
            proof_path = tmp_path / "proof.json"
            log_dir = tmp_path / "logs"
            strict_input = self._write_strict_input(
                tmp_path / "strict_input.json",
                base_ref="docker.io/library/app@sha256:" + ("1" * 64),
                opt_ref="docker.io/library/app@sha256:" + ("2" * 64),
                base_closure=["sha256:" + ("a" * 64)],
                opt_closure=["sha256:" + ("b" * 64)],
            )

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["STARGZ_GRPC_ROOT_DIR"] = str(stargz_root)
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--base",
                    "docker.io/library/app:base",
                    "--opt",
                    "docker.io/library/app:opt",
                    "--runs",
                    "1",
                    "--mode",
                    "ready",
                    "--ready-exec",
                    "test -f /tmp/ready",
                    "--comparison-mode",
                    "raw-full-vs-estargz-lazy",
                    "--cold-mode",
                    "registry-cold-strict",
                    "--pull-policy",
                    "always",
                    "--drop-local-image-ref",
                    "--base-snap",
                    "overlayfs",
                    "--opt-snap",
                    "stargz",
                    "--strict-input-json",
                    str(strict_input),
                    "--proof-json",
                    str(proof_path),
                    "--csv",
                    str(csv_path),
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stdout + proc.stderr)
            proof_payload = json.loads(proof_path.read_text(encoding="utf-8"))
            self.assertTrue(proof_payload["strict_proof_zero"])
            self.assertEqual(0, proof_payload["required_zero"]["stargz_runtime_mounts_remaining"])
            self.assertEqual(2, proof_payload["diagnostic"]["global_stargz_local_snapshot_entries_remaining"])
            self.assertEqual(
                "precheck/env_check/smoke before strict cleanup",
                proof_payload["diagnostic"]["stargz_state_source"],
            )
            self.assertEqual([], proof_payload["diagnostic"]["stargz_runtime_mount_entries_remaining"])
            self.assertEqual(
                2,
                len(proof_payload["diagnostic"]["diagnostic_only_stargz_local_snapshot_dirs_remaining_after_cleanup"]),
            )

    def test_raw_full_vs_estargz_lazy_strict_proof_still_fails_when_live_fuse_mounts_remain(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            stargz_root = tmp_path / "stargz-root"
            snapshots_dir = stargz_root / "snapshotter" / "snapshots"
            findmnt_entries = (
                f"{snapshots_dir / '30' / 'fs'} containerd-stargz-grpc fuse.stargz rw,nosuid,nodev",
            )
            bin_dir, _nerdctl_log, _curl_log = self._write_fake_runtime(
                tmp_path,
                findmnt_entries=findmnt_entries,
                stargz_snapshot_dir_entries=("30",),
            )
            csv_path = tmp_path / "out.csv"
            proof_path = tmp_path / "proof.json"
            log_dir = tmp_path / "logs"
            strict_input = self._write_strict_input(
                tmp_path / "strict_input.json",
                base_ref="docker.io/library/app@sha256:" + ("1" * 64),
                opt_ref="docker.io/library/app@sha256:" + ("2" * 64),
                base_closure=["sha256:" + ("a" * 64)],
                opt_closure=["sha256:" + ("b" * 64)],
            )

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["STARGZ_GRPC_ROOT_DIR"] = str(stargz_root)
            env["STRICT_TEST_UMOUNT_FAILS"] = "1"
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--base",
                    "docker.io/library/app:base",
                    "--opt",
                    "docker.io/library/app:opt",
                    "--runs",
                    "1",
                    "--mode",
                    "ready",
                    "--ready-exec",
                    "test -f /tmp/ready",
                    "--comparison-mode",
                    "raw-full-vs-estargz-lazy",
                    "--cold-mode",
                    "registry-cold-strict",
                    "--pull-policy",
                    "always",
                    "--drop-local-image-ref",
                    "--base-snap",
                    "overlayfs",
                    "--opt-snap",
                    "stargz",
                    "--strict-input-json",
                    str(strict_input),
                    "--proof-json",
                    str(proof_path),
                    "--csv",
                    str(csv_path),
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertNotEqual(0, proc.returncode)
            self.assertIn("strict proof failed", proc.stderr)
            proof_payload = json.loads(proof_path.read_text(encoding="utf-8"))
            self.assertFalse(proof_payload["strict_proof_zero"])
            self.assertEqual(1, proof_payload["required_zero"]["stargz_runtime_mounts_remaining"])
            self.assertEqual(
                1,
                len(proof_payload["diagnostic"]["diagnostic_only_stargz_local_snapshot_dirs_remaining_after_cleanup"]),
            )

    def test_strict_cleanup_does_not_delete_stargz_local_snapshot_dirs(self):
        script_text = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("strict cleanup: 删除 stargz local snapshot dir", script_text)
        self.assertNotIn("strict_remove_stargz_local_snapshot_dirs()", script_text)
        start = script_text.index("strict_cleanup_and_verify() {")
        end = script_text.index("validate_registry_cold_strict_contract", start)
        fn_body = script_text[start:end]
        self.assertNotIn("snapshotter/snapshots", fn_body)
        self.assertNotIn("rm -rf", fn_body)

    def test_lazy_hard_reset_stops_service_before_removing_snapshotter_and_starts_after(self):
        script_text = SCRIPT.read_text(encoding="utf-8")
        start = script_text.index("prepare_stargz_for_lazy_run() {")
        end = script_text.index("capture_stargz_runtime_mounts_snapshot() {", start)
        fn_body = script_text[start:end]
        stop_idx = fn_body.index('run_systemctl_logged "$tracef" "$name" "stop" sudo systemctl stop "$snapper"')
        rm_idx = fn_body.index('sudo rm -rf "${snapshotter_root}"')
        start_idx = fn_body.index('run_systemctl_logged "$tracef" "$name" "start" sudo systemctl start "$snapper"')

        self.assertLess(stop_idx, rm_idx)
        self.assertLess(rm_idx, start_idx)

    def test_lazy_hard_reset_runs_before_rpull_and_not_between_rpull_and_run(self):
        script_text = SCRIPT.read_text(encoding="utf-8")
        start = script_text.index("run_optimized_estargz_lazy() {")
        end = script_text.index("emit_measure_row() {", start)
        fn_body = script_text[start:end]
        reset_idx = fn_body.index('prepare_stargz_for_lazy_run "$run_idx" "$tracef"')
        rpull_idx = fn_body.index('local -a prepare_cmd=( ctr-remote -n "$NS" images rpull --snapshotter "$snap" )')
        run_idx = fn_body.index('local -a args=( run -d --name "$name" --pull=never )')
        self.assertLess(reset_idx, rpull_idx)
        self.assertLess(rpull_idx, run_idx)

    def test_ready_probe_applies_container_runtime_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir, nerdctl_log, _curl_log = self._write_fake_runtime(
                tmp_path,
                logs_payload="[I] service ready",
            )
            csv_path = tmp_path / "out.csv"
            log_dir = tmp_path / "logs"

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--base",
                    "docker.io/library/app:base",
                    "--opt",
                    "docker.io/library/app:opt",
                    "--runs",
                    "1",
                    "--mode",
                    "ready",
                    "--ready-log-regex",
                    "service ready",
                    "--gpus",
                    "all",
                    "--ipc",
                    "host",
                    "--container-env",
                    "HF_HUB_OFFLINE=1",
                    "--container-mount",
                    "type=bind,src=/models,dst=/models,readonly",
                    "--container-entrypoint",
                    "python3",
                    "--container-cmd",
                    "-m",
                    "--container-cmd",
                    "sglang.launch_server",
                    "--container-cmd",
                    "--model-path",
                    "--container-cmd",
                    "/models/demo",
                    "--snap",
                    "overlayfs",
                    "--base-snap",
                    "overlayfs",
                    "--opt-snap",
                    "overlayfs",
                    "--soft-cold",
                    "--no-drop-cache",
                    "--no-clear-stargz",
                    "--csv",
                    str(csv_path),
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stdout + proc.stderr)
            nerdctl_args = nerdctl_log.read_text(encoding="utf-8")
            self.assertIn("--gpus all", nerdctl_args)
            self.assertIn("--ipc host", nerdctl_args)
            self.assertIn("-e HF_HUB_OFFLINE=1", nerdctl_args)
            self.assertIn("--mount type=bind,src=/models,dst=/models,readonly", nerdctl_args)
            self.assertIn("--entrypoint python3", nerdctl_args)
            self.assertIn("docker.io/library/app:base -m sglang.launch_server --model-path /models/demo", nerdctl_args)

    def test_log_regex_probe_matches_without_pipefail_false_negative(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir, nerdctl_log, _curl_log = self._write_fake_runtime(
                tmp_path,
                logs_payload="[I] Jupyter Server 2.17.0 is running at:",
            )
            csv_path = tmp_path / "out.csv"
            log_dir = tmp_path / "logs"

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--base",
                    "docker.io/library/app:base",
                    "--opt",
                    "docker.io/library/app:opt",
                    "--runs",
                    "1",
                    "--mode",
                    "ready",
                    "--ready-log-regex",
                    "Jupyter Server .* is running",
                    "--timeout",
                    "1",
                    "--snap",
                    "overlayfs",
                    "--base-snap",
                    "overlayfs",
                    "--opt-snap",
                    "overlayfs",
                    "--soft-cold",
                    "--no-drop-cache",
                    "--no-clear-stargz",
                    "--csv",
                    str(csv_path),
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stdout + proc.stderr)
            csv_text = csv_path.read_text(encoding="utf-8")
            self.assertIn(",ok,", csv_text)
            nerdctl_args = nerdctl_log.read_text(encoding="utf-8")
            self.assertIn("logs --tail 200", nerdctl_args)

    def test_log_regex_probe_matches_logs_emitted_on_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir, _nerdctl_log, _curl_log = self._write_fake_runtime(
                tmp_path,
                logs_payload="[I] Jupyter Server 2.17.0 is running at:",
                logs_to_stderr=True,
            )
            csv_path = tmp_path / "out.csv"
            log_dir = tmp_path / "logs"

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--base",
                    "docker.io/library/app:base",
                    "--opt",
                    "docker.io/library/app:opt",
                    "--runs",
                    "1",
                    "--mode",
                    "ready",
                    "--ready-log-regex",
                    "Jupyter Server .* is running",
                    "--timeout",
                    "1",
                    "--snap",
                    "overlayfs",
                    "--base-snap",
                    "overlayfs",
                    "--opt-snap",
                    "overlayfs",
                    "--soft-cold",
                    "--no-drop-cache",
                    "--no-clear-stargz",
                    "--csv",
                    str(csv_path),
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stdout + proc.stderr)
            csv_text = csv_path.read_text(encoding="utf-8")
            self.assertIn(",ok,", csv_text)

    def test_log_regex_probe_fails_fast_on_gpu_oom(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir, _nerdctl_log, _curl_log = self._write_fake_runtime(
                tmp_path,
                logs_payload="torch.AcceleratorError: CUDA error: out of memory",
            )
            csv_path = tmp_path / "out.csv"
            log_dir = tmp_path / "logs"

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--base",
                    "docker.io/library/app:base",
                    "--opt",
                    "docker.io/library/app:opt",
                    "--runs",
                    "1",
                    "--mode",
                    "ready",
                    "--ready-log-regex",
                    "service ready",
                    "--timeout",
                    "2",
                    "--snap",
                    "overlayfs",
                    "--base-snap",
                    "overlayfs",
                    "--opt-snap",
                    "overlayfs",
                    "--soft-cold",
                    "--no-drop-cache",
                    "--no-clear-stargz",
                    "--csv",
                    str(csv_path),
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stdout + proc.stderr)
            csv_text = csv_path.read_text(encoding="utf-8")
            self.assertIn("startup-gpu-oom", csv_text)
            self.assertNotIn("ready-timeout", csv_text)
            self.assertIn("ready 前出现硬失败：startup-gpu-oom", proc.stderr)

    def test_log_regex_probe_fails_fast_on_model_fetch_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir, _nerdctl_log, _curl_log = self._write_fake_runtime(
                tmp_path,
                logs_payload=(
                    "ProxyError: HTTPSConnectionPool(host='huggingface.co', port=443): "
                    "Max retries exceeded with url: /foo"
                ),
            )
            csv_path = tmp_path / "out.csv"
            log_dir = tmp_path / "logs"

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--base",
                    "docker.io/library/app:base",
                    "--opt",
                    "docker.io/library/app:opt",
                    "--runs",
                    "1",
                    "--mode",
                    "ready",
                    "--ready-log-regex",
                    "service ready",
                    "--timeout",
                    "2",
                    "--snap",
                    "overlayfs",
                    "--base-snap",
                    "overlayfs",
                    "--opt-snap",
                    "overlayfs",
                    "--soft-cold",
                    "--no-drop-cache",
                    "--no-clear-stargz",
                    "--csv",
                    str(csv_path),
                    "--log-dir",
                    str(log_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stdout + proc.stderr)
            csv_text = csv_path.read_text(encoding="utf-8")
            self.assertIn("startup-model-fetch-failed", csv_text)
            self.assertNotIn("ready-timeout", csv_text)
            self.assertIn("ready 前出现硬失败：startup-model-fetch-failed", proc.stderr)

    def test_tcp_ready_probe_uses_bash_tcp_connect_check(self):
        script_text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('"/dev/tcp/127.0.0.1/${ready_port}"', script_text)
        self.assertNotIn('if [[ -n "$ready_port" ]] && port_in_use "$ready_port"; then', script_text)

    def test_stargz_cache_cleanup_preserves_metadata_and_avoids_service_restart(self):
        script_text = SCRIPT.read_text(encoding="utf-8")
        start = script_text.index("clear_stargz_cache_if_needed() {")
        end = script_text.index("drop_snapshot_records() {", start)
        fn_body = script_text[start:end]

        self.assertIn('snapshotter/cache', fn_body)
        self.assertIn('保留 metadata/snapshots', fn_body)
        self.assertNotIn('snapshotter/metadata', fn_body)
        self.assertNotIn('systemctl stop', fn_body)
        self.assertNotIn('systemctl start', fn_body)
        self.assertNotIn('systemctl restart', fn_body)


if __name__ == "__main__":
    unittest.main()
